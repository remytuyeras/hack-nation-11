# player_helpers.py
import os, sys, time, threading, math, random, secrets
from typing import Any, Dict, Optional
from collections import deque

import pygame

from .mod_keypress_gate import EDGE, latch_keypress
from .mod_key_map import default_keymap

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

# ===== UI defaults =====
DEFAULT_WIN_W, DEFAULT_WIN_H = 800, 600
PLAYER_RADIUS = 10
FPS = 60

# Tile/world
TILE = 32  # draw-sized tile
# Base greens (opaque; no alpha ops anywhere)
GRASS_A = (95, 159, 53)
GRASS_B = (106, 170, 60)

ME = (0, 200, 255)
OTHER = (0, 120, 180)
HUD = (235, 235, 235)

# Resolve paths relative to this file
HERE = os.path.dirname(os.path.abspath(__file__))

# ===== Global (filled in main by agent) =====
PID: Optional[str] = None  # set by agent after identity

INPUT = {"w": False, "a": False, "s": False, "d": False}
SNAP: Dict[str, Any] = {
    "type": "world_state",
    "bounds": {"w": 10000, "h": 8000, "pr": PLAYER_RADIUS},
    "players": [],
    "overlays": [],
    "ts": None
}

# Read-only chat log (append-only; rendered in side panel)
CHAT_LOG = deque(maxlen=50)
CHAT_LOCK = threading.Lock()
# Dedupe by (pid, text) with a short cool-down
LAST_CHAT: Dict[tuple[str, str], float] = {}
CHAT_DEDUPE_SECS = 2.0  # > overlay TTL so we only log once per press

# Strong dedupe: per-PID highest seen overlay sequence (watermark)
# --- Per-consumer sequence registry (player-side dedupe) --------------------
import threading
from collections import defaultdict

class _SeqRegistry:
    """
    Keep a last-seen seq per (consumer_key, pid).
    Example usage from handlers:
        if isinstance(seq, int) and H.SEQ.seen("chat_fold", pid, seq):
            continue
    """
    def __init__(self):
        self._by_consumer = defaultdict(dict)  # consumer_key -> {pid: last_seq}
        self._lock = threading.Lock()

    def seen(self, consumer: str, pid: str, seq: int) -> bool:
        """
        Return True if this consumer already saw >= seq for pid.
        Otherwise record seq and return False.
        """
        with self._lock:
            last = self._by_consumer[consumer].get(pid, -1)
            if seq <= last:
                return True
            self._by_consumer[consumer][pid] = seq
            return False

    def reset(self, consumer: str | None = None) -> None:
        with self._lock:
            if consumer is None:
                self._by_consumer.clear()
            else:
                self._by_consumer.pop(consumer, None)

# Initialize once on module import (safe under hot-reload)
if "SEQ" not in globals():
    SEQ = _SeqRegistry()


LOCK = threading.Lock()
RUNNING = True

# ===== Default client config (can be overridden by --config) =====
DEFAULT_PLAYER_CONFIG: Dict[str, Any] = {
    "host": None,
    "port": None,
    "logger": {
        "log_level": "INFO",
        "enable_console_log": True,
        "console_log_format": "\u001b[92m%(asctime)s\u001b[0m - \u001b[94m%(name)s\u001b[0m - %(levelname)s - %(message)s",
        "enable_file_log": True,
        "enable_json_log": False,
        "log_file_path": "logs/",
        "log_format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        "max_file_size": 1000000,
        "backup_count": 3,
        "date_format": "%Y-%m-%d %H:%M:%S.%f",
        "log_keys": None
    },
    "hyper_parameters": {
        "receiver": {"max_bytes_per_line": 65536, "read_timeout_seconds": None},
        "sender": {
            "concurrency_limit": 16, "batch_drain": False, "queue_maxsize": 128,
            "event_bridge_maxsize": 2000, "max_worker_errors": 3
        },
        "reconnection": {
            "retry_delay_seconds": 3, "primary_retry_limit": 5,
            "default_host": "127.0.0.1", "default_port": 8888, "default_retry_limit": 3
        }
    }
}

# ===== Identity persistence (collision-proof) =====
def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return None

def _write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.strip() + "\n")

def _rand_id() -> str:
    return f"p{secrets.randbelow(900000) + 100000}"

def load_or_create_identity(id_arg: Optional[str]) -> str:
    """
    --id alias:
      if HERE/alias.id exists -> reuse its content as the ID,
      else create HERE/alias.id containing 'alias' and use 'alias'.
    No --id:
      mint new random ID and create HERE/<id>.id with that content.
    """
    if id_arg:
        idfile = os.path.join(HERE, f"{id_arg}.id")
        existing = _read_text(idfile)
        if existing:
            return existing
        _write_text(idfile, id_arg)
        return id_arg

    while True:
        new_id = _rand_id()
        idfile = os.path.join(HERE, f"{new_id}.id")
        if not os.path.exists(idfile):
            _write_text(idfile, new_id)
            return new_id

# ===== World seed persistence =====
SEED_FILE = os.path.join(HERE, "world_seed.txt")

def load_or_create_world_seed(seed_arg: Optional[str]) -> str:
    if seed_arg and seed_arg.strip():
        seed = seed_arg.strip()
        with open(SEED_FILE, "w", encoding="utf-8") as f:
            f.write(seed + "\n")
        return seed

    if os.path.exists(SEED_FILE):
        try:
            with open(SEED_FILE, "r", encoding="utf-8") as f:
                existing = f.read().strip()
                if existing:
                    return existing
        except Exception:
            pass

    seed = f"world-{random.randint(1000, 9999)}-{random.randint(1000, 9999)}"
    with open(SEED_FILE, "w", encoding="utf-8") as f:
        f.write(seed + "\n")
    return seed

# ===== Seeded opaque grass (no alpha / no blending) =====
def _fnv1a32(s: str) -> int:
    """Stable 32-bit hash for (seed, tileX, tileY)."""
    h = 2166136261
    for b in s.encode("utf-8"):
        h ^= b
        h = (h * 16777619) & 0xFFFFFFFF
    return h

def _tile_shade(seed: str, ix: int, iy: int) -> float:
    """Deterministic 0..1 from seed + tile index."""
    h = _fnv1a32(f"{seed}|{ix}|{iy}")
    # map to [0,1]
    return ((h >> 8) & 0xFFFFFF) / 0xFFFFFF

def _mix(a: tuple, b: tuple, t: float) -> tuple:
    return (
        int(a[0] + (b[0] - a[0]) * t),
        int(a[1] + (b[1] - a[1]) * t),
        int(a[2] + (b[2] - a[2]) * t),
    )

def draw_grass_seeded(screen: pygame.Surface, seed: str, cam_x: float, cam_y: float):
    """
    Draw a 2×2 checker inside each tile using **opaque** fills only.
    """
    w, h = screen.get_size()
    start_ix = int(math.floor(cam_x / TILE))
    start_iy = int(math.floor(cam_y / TILE))
    off_x = - (cam_x - start_ix * TILE)
    off_y = - (cam_y - start_iy * TILE)
    cols = w // TILE + 3
    rows = h // TILE + 3

    for r in range(rows):
        for c in range(cols):
            ix = start_ix + c
            iy = start_iy + r
            x = int(off_x + c * TILE)
            y = int(off_y + r * TILE)

            t = _tile_shade(seed, ix, iy)  # 0..1
            # Limit variation to a gentle band around the base colors
            t_small = 0.25 * (t - 0.5)  # [-0.125..+0.125]
            cA = _mix(GRASS_A, GRASS_B, 0.5 + t_small)
            cB = _mix(GRASS_B, GRASS_A, 0.5 - t_small)

            half = TILE // 2
            # 2×2 checker, pure RGB fills
            pygame.draw.rect(screen, cA, (x, y, half, half))
            pygame.draw.rect(screen, cB, (x + half, y, half, half))
            pygame.draw.rect(screen, cB, (x, y + half, half, half))
            pygame.draw.rect(screen, cA, (x + half, y + half, half, half))

# --- Seeded grass with per-tile cache (opaque RGB) ---
class TileCache:
    """
    LRU cache of TILE×TILE pre-rendered grass tiles.
    Keyed by (seed, ix, iy). Keeps surfaces fully opaque (no alpha).
    """
    def __init__(self, cap: int = 4096):
        self.cap = cap
        self.store: dict[tuple[str, int, int], pygame.Surface] = {}
        self.order: list[tuple[str, int, int]] = []  # simple FIFO/LRU

    def get(self, seed: str, ix: int, iy: int) -> pygame.Surface:
        key = (seed, ix, iy)
        surf = self.store.get(key)
        if surf is not None:
            return surf
        surf = self._make_tile(seed, ix, iy)
        self.store[key] = surf
        self.order.append(key)
        if len(self.order) > self.cap:
            old = self.order.pop(0)
            self.store.pop(old, None)
        return surf

    def _make_tile(self, seed: str, ix: int, iy: int) -> pygame.Surface:
        """
        Build one TILE×TILE pixel-art grass tile:
        - Two nearby green shades picked per tile (seeded),
        - 4×4 Bayer dithering to distribute bright/dark pixels,
        - A few single-pixel 'blade' flecks,
        - 100% opaque RGB, no alpha or blending flags.
        """
        rng = random.Random(_fnv1a32(f"{seed}|{ix}|{iy}|bayer"))

        # Gentle brightness variation per tile
        t = _tile_shade(seed, ix, iy)          # 0..1
        mid_mix = 0.45 + 0.10 * (t - 0.5)      # around 0.45..0.55
        base_mid = _mix(GRASS_A, GRASS_B, mid_mix)

        # Create two close shades around that midpoint (A=dark, B=light)
        def clamp(v): return max(0, min(255, v))
        def tint(col, dv):
            return (clamp(col[0] + dv), clamp(col[1] + dv), clamp(col[2] + dv))

        var = 15
        dark = tint(base_mid, -var - rng.randint(0, 4))
        lite = tint(base_mid, +var + rng.randint(0, 4))

        # 4×4 Bayer threshold matrix (values 0..15)
        B4 = (
            ( 0,  8,  2, 10),
            (12,  4, 14,  6),
            ( 3, 11,  1,  9),
            (15,  7, 13,  5),
        )
        threshold = max(4, min(12, int(8 + (t - 0.5) * 8 + rng.randint(-2, 2))))

        surf = pygame.Surface((TILE, TILE)).convert()
        surf.lock()
        try:
            for y in range(TILE):
                row = B4[y & 3]
                for x in range(TILE):
                    if row[x & 3] < threshold:
                        surf.set_at((x, y), lite)
                    else:
                        surf.set_at((x, y), dark)

            flecks = rng.randint(4, 8)
            blade = tint(lite, +6)
            for _ in range(flecks):
                x = rng.randrange(0, TILE)
                y = rng.randrange(0, TILE)
                surf.set_at((x, y), blade)
                if rng.random() < 0.3 and y+1 < TILE:
                    surf.set_at((x, y+1), blade)
        finally:
            surf.unlock()

        return surf

def draw_grass_seeded_cached(screen: pygame.Surface, cache: TileCache, seed: str,
                             cam_x: float, cam_y: float) -> None:
    """
    Draw visible TILE-grid area using cached TILE×TILE surfaces.
    """
    w, h = screen.get_size()
    start_ix = int(math.floor(cam_x / TILE))
    start_iy = int(math.floor(cam_y / TILE))
    off_x = - (cam_x - start_ix * TILE)
    off_y = - (cam_y - start_iy * TILE)
    cols = w // TILE + 3
    rows = h // TILE + 3

    # Fill every visible tile from cache
    for r in range(rows):
        for c in range(cols):
            ix = start_ix + c
            iy = start_iy + r
            x = int(off_x + c * TILE)
            y = int(off_y + r * TILE)
            screen.blit(cache.get(seed, ix, iy), (x, y))


# ===== Helpers =====
def world_to_screen(px: float, py: float, cam_x: float, cam_y: float) -> tuple[int, int]:
    return int(px - cam_x), int(py - cam_y)

def find_me(players: list[dict]) -> Optional[dict]:
    for p in players:
        if p.get("pid") == PID:
            return p
    return None


def _draw_status_panel(screen, font, win_w, win_h):
    # Pull a dict exported by the agent; fall back to sane defaults.
    # IMPORTANT: we read HUD_STATE (not HUD) so HUD can stay the RGB color tuple.
    hud = globals().get("HUD_STATE", {}) if isinstance(globals().get("HUD_STATE", {}), dict) else {}

    mode    = str(hud.get("mode", "combat"))
    target  = str(hud.get("target", "-"))
    weapon  = str(hud.get("weapon", "-"))
    defense = str(hud.get("defense", "-"))
    skill   = str(hud.get("skill", "-"))
    mastery = str(hud.get("mastery", "0"))
    recipe  = str(hud.get("recipe_id", "-"))
    product = str(hud.get("recipe_out", "-"))

    anger   = hud.get("anger", None)
    fear    = hud.get("fear", None)

    # panel geometry (bottom-left)
    margin = 12
    panel_w = 280
    panel_h = 150
    panel_x = margin
    panel_y = win_w - panel_w  # keep off avatar area if your avatar sits bottom-left
    panel_y = win_h - panel_h - margin  # final placement

    # card
    pygame.draw.rect(screen, (255, 255, 255), (panel_x, panel_y, panel_w, panel_h), width=1, border_radius=6)

    lines = [
        f"[{mode}]  tgt={target}",
        f"wpn={weapon}   def={defense}",
        f"skill={skill} (m{mastery})",
        f"craft={recipe} → {product}",
    ]

    # Only show emotions if provided, and render safely if not numeric
    if (anger is not None) or (fear is not None):
        def _fmt(v):
            try:
                return f"{float(v):.1f}"
            except Exception:
                return "…"
        lines.append(f"emo: anger={_fmt(anger)} fear={_fmt(fear)}")

    pad_x, pad_y = 10, 8
    y = panel_y + pad_y
    for line in lines:
        surf = font.render(line, True, (255, 255, 255))
        screen.blit(surf, (panel_x + pad_x, y))
        y += surf.get_height() + 4


# ===== UI loop (resizable window, camera follows player, optional avatar) =====
def ui_loop(avatar_path: Optional[str], world_seed: str):
    pygame.init()

    keymap = default_keymap()
    EDGE.set_keymap(keymap)

    flags = pygame.RESIZABLE
    screen = pygame.display.set_mode((DEFAULT_WIN_W, DEFAULT_WIN_H), flags)
    pygame.display.set_caption(f"Summoner Free-Roam — {PID}")
    clock = pygame.time.Clock()
    font = pygame.font.Font(None, 22)

    tile_cache = TileCache(cap=4096)

    my_avatar = None
    if avatar_path:
        apath = avatar_path if os.path.isabs(avatar_path) else os.path.join(HERE, avatar_path)
        try:
            surf = pygame.image.load(apath).convert_alpha()
            size = max(PLAYER_RADIUS * 5, 24)
            my_avatar = pygame.transform.smoothscale(surf, (size, size))
        except Exception as e:
            print(f"[Player] Could not load avatar '{apath}': {e}")

    win_w, win_h = screen.get_size()
    cam_x, cam_y = 0.0, 0.0

    while RUNNING:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return
            elif event.type == pygame.VIDEORESIZE:
                win_w, win_h = event.w, event.h
                screen = pygame.display.set_mode((win_w, win_h), flags)

        pressed = pygame.key.get_pressed()
        with LOCK:
            INPUT["w"] = bool(pressed[pygame.K_w] or pressed[pygame.K_UP])
            INPUT["a"] = bool(pressed[pygame.K_a] or pressed[pygame.K_LEFT])
            INPUT["s"] = bool(pressed[pygame.K_s] or pressed[pygame.K_DOWN])
            INPUT["d"] = bool(pressed[pygame.K_d] or pressed[pygame.K_RIGHT])
            snapshot = dict(SNAP)

        # Build the set of currently-held friendly key names and feed EDGE
        pressed_names = set()
        for name, code in keymap.items():
            try:
                if pressed[code]:
                    pressed_names.add(name)
            except IndexError:
                pass
        EDGE.update_pressed(pressed_names)

        for name in pressed_names:
            if EDGE.edge_down(name):       # true only on this frame
                latch_keypress(name)       # <- NEW: survives until next send poll

        bounds = snapshot.get("bounds", {"w": 10000, "h": 8000, "pr": PLAYER_RADIUS})
        players = snapshot.get("players", [])
        me = find_me(players)

        if me is not None:
            target_cx, target_cy = me["x"], me["y"]
        else:
            target_cx, target_cy = bounds["w"] * 0.5, bounds["h"] * 0.5

        cam_x = max(0.0, min(target_cx - win_w / 2.0, bounds["w"] - win_w))
        cam_y = max(0.0, min(target_cy - win_h / 2.0, bounds["h"] - win_h))

        # Draw seeded grass (pure RGB fills only)
        # draw_grass_seeded(screen, world_seed, cam_x, cam_y)
        draw_grass_seeded_cached(screen, tile_cache, world_seed, cam_x, cam_y)

        # Players + collect screen positions by pid
        pid_to_screen: Dict[str, tuple[int, int]] = {}
        for p in players:
            sx, sy = world_to_screen(p["x"], p["y"], cam_x, cam_y)
            pid_to_screen[p.get("pid","?")] = (sx, sy)
            if p.get("pid") == PID and my_avatar is not None:
                screen.blit(my_avatar, my_avatar.get_rect(center=(sx, sy)))
            else:
                pygame.draw.circle(screen, ME if p.get("pid") == PID else OTHER, (sx, sy), PLAYER_RADIUS)

        # --- Speech bubbles (Windows only) ---
        if sys.platform.startswith("win"):
            render_overlays = list(snapshot.get("overlays", []))
        else:
            render_overlays = []

        # Draw bubbles after all players are positioned
        for ov in render_overlays:
            if not isinstance(ov, dict):
                continue
            pid = ov.get("pid"); chat_text = ov.get("chat")
            if not pid or not isinstance(chat_text, str) or not chat_text.strip():
                continue
            pos = pid_to_screen.get(pid)
            if not pos:
                continue
            sx, sy = pos
            pad_x, pad_y = 8, 4
            text_surf = font.render(chat_text, True, (0, 0, 0))
            tw, th = text_surf.get_size()
            bx = sx - (tw // 2) - pad_x
            by = sy - PLAYER_RADIUS - th - 12
            bw = tw + pad_x * 2
            bh = th + pad_y * 2
            pygame.draw.rect(screen, (255, 255, 255), (bx, by, bw, bh), border_radius=6)
            pygame.draw.rect(screen, (0, 0, 0), (bx, by, bw, bh), width=1, border_radius=6)
            screen.blit(text_surf, (bx + pad_x, by + pad_y))

        # Right-side chat (transparent bg, white border/text)
        margin = 12
        panel_w = 260
        panel_x = win_w - panel_w - margin
        panel_y = 48
        panel_h = min(win_h - panel_y - margin, 260)

        pygame.draw.rect(screen, (255, 255, 255), (panel_x, panel_y, panel_w, panel_h), width=1, border_radius=6)

        title = "CHAT"
        title_surf = font.render(title, True, (255, 255, 255))
        title_x = panel_x + (panel_w - title_surf.get_width()) // 2
        title_y = panel_y + 6
        screen.blit(title_surf, (title_x, title_y))

        sep_top = title_y + title_surf.get_height() + 6
        pygame.draw.line(screen, (255, 255, 255), (panel_x + 6, sep_top), (panel_x + panel_w - 6, sep_top), width=1)

        with CHAT_LOCK:
            items = list(CHAT_LOG)[-10:]

        pad_x = 8
        line_y = sep_top + 8
        for _, pid, text_line in items:
            pid_short = str(pid)[:6]
            line = f"{pid_short}: {text_line}"
            surf = font.render(line, True, (255, 255, 255))

            maxw = panel_w - (pad_x * 2)
            if surf.get_width() > maxw:
                truncated = line
                while surf.get_width() > maxw and len(truncated) > 3:
                    truncated = truncated[:-4] + "…"
                    surf = font.render(truncated, True, (255, 255, 255))

            screen.blit(surf, (panel_x + pad_x, line_y))
            line_y += surf.get_height() + 4
            if line_y > panel_y + panel_h - 8:
                break

        ts = snapshot.get("ts")
        coords = f"x={me['x']:.1f}  y={me['y']:.1f}" if me else "x=…  y=…"
        text = f"ID {PID}   players={len(players)}   {coords}   seed='{world_seed}'"
        if ts is not None:
            text += f"   t={ts:.2f}"
        screen.blit(font.render(text, True, HUD), (10, 10))

        _draw_status_panel(screen, font, win_w, win_h)

        pygame.display.flip()
        clock.tick(FPS)

    pygame.quit()
