import asyncio, time, math, random
from typing import Dict, Any, Optional
from summoner.aurora import SummonerAgent
from summoner.protocol.process import Direction
import argparse
from pathlib import Path

from db_sdk import Database
from db_models import (
    Actor, ActorPower, Inventory,
    create_all_gm, sqlite_bootstrap,
    ensure_actor_on_connect, load_resources,
    inv_bulk_add, actor_damage, power_set,
)

from gm_cmds import (
    process_structured_cmd, set_players_reference, load_combat_rules,
    INVENTORY as GM_INV  # in-memory mirror used by gm_cmds
)

MAP_W, MAP_H = 10000, 8000
PLAYER_RADIUS = 10
PLAYER_SPEED = 4.0

SIM_STEP_MS = 16.6667
BROADCAST_EVERY_MS = 15.0

SPAWN_CX, SPAWN_CY = MAP_W / 2, MAP_H / 2
SPAWN_RING_R = 140.0
SPAWN_JITTER = 18.0

DB: Optional[Database] = None
RESOURCES: Dict[str, Any] = {}

from asyncio import Queue, QueueEmpty
OUTBOX: Queue[dict] = Queue()

def enqueue_reply(msg: dict) -> None:
    try:
        OUTBOX.put_nowait(dict(msg))
    except Exception as e:
        agent.logger.debug("[GM/outbox] put_nowait failed: %s", e)

class Player:
    __slots__ = ("pid", "x", "y", "vx", "vy", "keys", "overlay", "ov_seq")
    def __init__(self, pid: str, idx: int):
        self.pid = pid
        if idx == 0:
            base_x, base_y = SPAWN_CX, SPAWN_CY
        else:
            angle = (idx * 137.508) * math.pi / 180.0
            base_x = SPAWN_CX + math.cos(angle) * SPAWN_RING_R
            base_y = SPAWN_CY + math.sin(angle) * SPAWN_RING_R
        self.x = max(PLAYER_RADIUS, min(MAP_W - PLAYER_RADIUS, base_x + random.uniform(-SPAWN_JITTER, SPAWN_JITTER)))
        self.y = max(PLAYER_RADIUS, min(MAP_H - PLAYER_RADIUS, base_y + random.uniform(-SPAWN_JITTER, SPAWN_JITTER)))
        self.vx = 0.0
        self.vy = 0.0
        self.keys = {"w": False, "a": False, "s": False, "d": False}
        self.overlay: dict[str, Any] | None = None
        self.ov_seq = 0

players: Dict[str, Player] = {}

DB = Database(Path(__file__).with_name("gm.db"))

res_path = Path(__file__).with_name("resources.json")
RESOURCES = load_resources(res_path)
load_combat_rules(str(res_path))

set_players_reference(players)

ALLOWED_OVERLAY_KEYS = {"chat", "cmd"}
MAX_CHAT_LEN = 160

def sanitize_overlay(overlay: dict) -> dict:
    output: dict[str, Any] = {}
    for key in ALLOWED_OVERLAY_KEYS:
        if key not in overlay:
            continue
        value = overlay[key]
        if key == "chat" and isinstance(value, str):
            value = value.strip()
            if not value:
                continue
            if len(value) > MAX_CHAT_LEN:
                value = value[:MAX_CHAT_LEN - 1] + "…"
        output[key] = value
    return output

def collect_overlays() -> list[dict[str, Any]]:
    now = time.time()
    output = []
    for player in players.values():
        if player.overlay and player.overlay.get("t_expire", 0) > now:
            output.append({"pid": player.pid, **player.overlay["data"]})
        else:
            player.overlay = None
    return output

def clamp(v, lo, hi): return max(lo, min(hi, v))

def apply_inputs(dt_ms: float):
    for player in players.values():
        dx = (-1 if player.keys["a"] else 0) + (1 if player.keys["d"] else 0)
        dy = (-1 if player.keys["w"] else 0) + (1 if player.keys["s"] else 0)
        if dx and dy:
            inv = 1 / math.sqrt(2.0)
            dx *= inv; dy *= inv
        step_scale = (dt_ms / SIM_STEP_MS) if dt_ms else 1.0
        player.vx = dx * PLAYER_SPEED
        player.vy = dy * PLAYER_SPEED
        player.x = clamp(player.x + player.vx * step_scale, PLAYER_RADIUS, MAP_W - PLAYER_RADIUS)
        player.y = clamp(player.y + player.vy * step_scale, PLAYER_RADIUS, MAP_H - PLAYER_RADIUS)

async def sim_loop():
    acc = 0.0
    last_ms = time.perf_counter() * 1000.0
    while True:
        now_ms = time.perf_counter() * 1000.0
        dt = now_ms - last_ms
        last_ms = now_ms
        acc += dt
        while acc >= SIM_STEP_MS:
            apply_inputs(SIM_STEP_MS)
            acc -= SIM_STEP_MS
        await asyncio.sleep(0.001)

def world_state() -> Dict[str, Any]:
    # Include per-player inventory so clients can render/gate equips locally.
    return {
        "type": "world_state",
        "ts": time.time(),
        "bounds": {"w": MAP_W, "h": MAP_H, "pr": PLAYER_RADIUS},
        "players": [
            {
                "pid": p.pid,
                "x": p.x,
                "y": p.y,
                "inventory": GM_INV.get(p.pid, {}),  # <<< NEW: expose inventory snapshot
            }
            for p in players.values()
        ],
        "overlays": collect_overlays(),
    }

async def _prime_gmcmds_inventory_from_db(pid: str) -> None:
    rows = await Inventory.find(DB, where={"pid": pid})
    GM_INV[pid] = {r["item"]: int(r["qty"]) for r in rows}

async def _apply_effects_to_db(effects: Dict[str, Any]) -> None:
    if not effects:
        return

    # Inventory deltas
    inv_fx = effects.get("inventory") or {}
    for pid, deltas in inv_fx.items():
        # Persist
        await inv_bulk_add(DB, pid, {k: int(v) for k, v in (deltas or {}).items()})
        # Mirror to in-memory GM_INV (keep in sync for subsequent commands)
        inv_map = GM_INV.setdefault(pid, {})
        for item, delta in (deltas or {}).items():
            inv_map[item] = int(inv_map.get(item, 0)) + int(delta)
            if inv_map[item] <= 0:
                # keep map tidy; optional
                inv_map.pop(item, None)

    # Health
    hp_fx = effects.get("health") or {}
    for pid, delta in hp_fx.items():
        await actor_damage(DB, pid, float(delta))

    # Skills
    sk_fx = effects.get("skills") or {}
    for pid, skills in sk_fx.items():
        for ptype, mastery in (skills or {}).items():
            try:
                m = float(mastery)
            except Exception:
                m = 1.0
            await power_set(DB, pid, ptype, mastery_mult=m)

agent = SummonerAgent(name="GameMasterAgent")

@agent.hook(Direction.RECEIVE)
async def rx_normalize(payload: Any) -> Optional[dict]:
    if isinstance(payload, str):
        agent.logger.warning("[GM] received str payload; ignoring")
        return None
    if isinstance(payload, dict) and "content" in payload and isinstance(payload["content"], dict):
        return payload["content"]
    return payload

@agent.keyed_receive("directions", key_by="pid")
async def on_tick(msg: dict) -> None:
    if not isinstance(msg, dict) or msg.get("type") != "tick":
        return None
    pid = msg.get("pid")
    if not pid:
        return None

    player = players.get(pid)
    if player is None:
        # New player joins via movement; create, seed DB, and prime gm_cmds mirror.
        player = Player(pid, idx=len(players))
        players[pid] = player
        agent.logger.info(f"[GM] join {pid} at ({player.x:.1f},{player.y:.1f})")
        try:
            await ensure_actor_on_connect(DB, pid, RESOURCES)
            await _prime_gmcmds_inventory_from_db(pid)  # <<< NEW
        except Exception as e:
            agent.logger.debug("[GM] ensure/prime failed for %s: %s", pid, e)

    keys = msg.get("keys") or {}
    player.keys["w"] = bool(keys.get("w")); player.keys["a"] = bool(keys.get("a"))
    player.keys["s"] = bool(keys.get("s")); player.keys["d"] = bool(keys.get("d"))
    return None

@agent.keyed_receive("overlay", key_by="pid", seq_by="seq")
async def on_overlay(msg: dict) -> None:
    pid = msg.get("pid")
    overlay = msg.get("overlay") if isinstance(msg, dict) else None
    if not pid or not isinstance(overlay, dict):
        return None

    player = players.get(pid)
    if player is None:
        player = Player(pid, idx=len(players))
        players[pid] = player
        agent.logger.info(f"[GM] join {pid} at ({player.x:.1f},{player.y:.1f})")
        # Seed DB + gm_cmds mirror if overlay arrives first
        try:
            await ensure_actor_on_connect(DB, pid, RESOURCES)
            await _prime_gmcmds_inventory_from_db(pid)  # <<< keep mirror valid
        except Exception as e:
            agent.logger.debug("[GM] ensure/prime failed for %s: %s", pid, e)

    filtered = sanitize_overlay(overlay)
    if not filtered:
        return None

    if "chat" in filtered:
        ttl_ms = max(0, int(overlay.get("ttl_ms", 1500)))
        now = time.time()
        player.ov_seq += 1
        player.overlay = {
            "data": {**filtered, "seq": player.ov_seq},
            "t_expire": now + (ttl_ms / 1000.0),
        }

    cmd = filtered.get("cmd")
    if isinstance(cmd, dict):
        agent.logger.info("[GM/cmd] from=%s kind=%s payload=%s", pid, cmd.get("kind"), cmd)
        status = process_structured_cmd(pid, cmd)
        try:
            await _apply_effects_to_db(status.get("effects") or {})
        except Exception as e:
            agent.logger.debug("[GM/cmd] effects persist failed: %s", e)
        enqueue_reply(status)

    return None

@agent.send("gm/replies", multi=True)
async def drain_replies() -> list[dict]:
    if OUTBOX.empty():
        return []
    batch = []
    try:
        batch.append(OUTBOX.get_nowait())
        for _ in range(7):
            batch.append(OUTBOX.get_nowait())
    except QueueEmpty:
        pass
    return batch

@agent.send("gm/reply")
async def send_world() -> dict:
    await asyncio.sleep(BROADCAST_EVERY_MS / 1000.0)
    return world_state()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run a Summoner client with a specified config.")
    parser.add_argument('--config', dest='config_path', required=False, help='The relative path to the config file (JSON) for the client (e.g., --config configs/client_config.json)')
    args = parser.parse_args()

    agent.loop.run_until_complete(sqlite_bootstrap(DB))
    agent.loop.run_until_complete(create_all_gm(DB))

    agent.loop.create_task(sim_loop())

    try:
        agent.run(host="127.0.0.1", port=8888, config_path=args.config_path or "configs/agent_config.json")
    finally:
        asyncio.run(DB.close()) 

