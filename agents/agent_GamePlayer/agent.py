# agent.py
import argparse
import asyncio
import json
import threading
import time
from collections import deque
from typing import Any, Dict, Optional, Tuple, List

from summoner.client import SummonerClient
from summoner.protocol.process import Direction

from hackathon_utils import send_on_keypress, H

from db_sdk import Database
from db_models import create_all_player, Emotion, Reputation, MemoryKV

from pathlib import Path


# ============================================================================
# 0) PLAYER DB & EMOTION/MEMORY HELPERS
# ============================================================================
PLAYER_DB: Optional[Database] = None
PID: Optional[str] = None  # set in __main__
client = SummonerClient(name="GamePlayerAgent")

async def init_player_db(pid: str):
    global PLAYER_DB
    
    PLAYER_DB = Database(Path(__file__).with_name(f"player_{pid}.db"))
    await create_all_player(PLAYER_DB)

# --- MemoryKV helpers (player-owned persistence) ---
async def kv_get(k: str, default: Optional[str] = None) -> Optional[str]:
    row = await MemoryKV.find(PLAYER_DB, where={"owner_pid": PID, "k": k})
    return (row[0]["v"] if row else default)

async def kv_set(k: str, v: str) -> None:
    # UPSERT without insert_or_update()
    row = await MemoryKV.find(PLAYER_DB, where={"owner_pid": PID, "k": k})
    if row:
        await MemoryKV.update(
            PLAYER_DB,
            where={"id": row[0]["id"]},
            fields={"v": v},
        )
    else:
        await MemoryKV.insert(
            PLAYER_DB,
            owner_pid=PID,
            k=k,
            v=v,
        )


# --- Emotion helpers (DB-backed) ---
async def _emo_get_async(dst_pid: str, label: str) -> float:
    row = await Emotion.find(PLAYER_DB, where={"src_pid": PID, "dst_pid": dst_pid, "label": label})
    return float(row[0]["value"]) if row else 0.0

async def _emo_bump_async(dst_pid: str, label: str, delta: float) -> float:
    curr = await _emo_get_async(dst_pid, label)
    val = max(0.0, min(1.0, curr + float(delta)))
    row = await Emotion.find(PLAYER_DB, where={"src_pid": PID, "dst_pid": dst_pid, "label": label})
    if row:
        await Emotion.update(PLAYER_DB, where={"id": row[0]["id"]}, fields={"value": val})
    else:
        await Emotion.insert(PLAYER_DB, src_pid=PID, dst_pid=dst_pid, label=label, value=val)
    return val


# ============================================================================
# 1) GLOBAL OVERLAY SEQ & COMBO STATE
# ============================================================================
_OVERLAY_SEQ = 0
_OVERLAY_SEQ_LOCK = asyncio.Lock()

async def next_overlay_seq_async() -> int:
    global _OVERLAY_SEQ
    async with _OVERLAY_SEQ_LOCK:
        _OVERLAY_SEQ += 1
        return _OVERLAY_SEQ

# Client-local input state
COMBO = {
    "armed_attack": False,
    "armed_counter": False,
    "target": None,
    "weapon": None,    # not equipped until validated
    "defense": None,   # not equipped until validated
}

COMBO_LOCK = threading.Lock()

def _set(key: str, value):
    with COMBO_LOCK:
        COMBO[key] = value

def _toggle(key: str):
    with COMBO_LOCK:
        COMBO[key] = not bool(COMBO[key])

def _snapshot() -> dict:
    with COMBO_LOCK:
        return dict(COMBO)


# ============================================================================
# 2) MODES, TX TRACKING, OVERLAY HELPERS
# ============================================================================
MODE_ORDER = ["combat", "trade", "social", "craft"]
MODE = "combat"

def _set_mode(m: str):
    global MODE
    MODE = m

def _cycle_mode():
    i = MODE_ORDER.index(MODE)
    _set_mode(MODE_ORDER[(i + 1) % len(MODE_ORDER)])

# Recent TX (trade/learn/teach) you initiated or saw
LAST_TXID: Optional[str] = None
LAST_TX_KIND: Optional[str] = None
_RECENT_TX: deque[Tuple[str, str]] = deque(maxlen=8)  # (txid, kind)

def _remember_tx(txid: Optional[str], kind: Optional[str]):
    global LAST_TXID, LAST_TX_KIND
    if isinstance(txid, str) and txid:
        LAST_TXID, LAST_TX_KIND = txid, kind
        _RECENT_TX.append((txid, kind or "?"))

def _toast(text: str, ttl_ms: int = 900) -> dict:
    return {"type": "overlay", "overlay": {"chat": text, "ttl_ms": ttl_ms}}

def _cmd(kind: str, **fields) -> dict:
    return {"type": "overlay", "overlay": {"cmd": {"kind": kind, **fields}}}


# ============================================================================
# 3) TARGETING & PROXIMITY (CLIENT UX)
# ============================================================================
def _list_targets(exclude_self: bool = True) -> List[str]:
    with H.LOCK:
        plist = [p.get("pid") for p in (H.SNAP.get("players") or []) if isinstance(p, dict)]
    uniq = [p for p in plist if isinstance(p, str)]
    if exclude_self and PID in uniq:
        uniq = [p for p in uniq if p != PID]
    cur = _snapshot().get("target")
    if isinstance(cur, str) and cur and cur not in uniq:
        uniq.append(cur)
    return uniq

def _self_pos() -> Optional[Tuple[float, float]]:
    with H.LOCK:
        me = next((p for p in (H.SNAP.get("players") or []) if p.get("pid") == PID), None)
    if not me:
        return None
    try:
        return float(me["x"]), float(me["y"])
    except Exception:
        return None

def _dist2(ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = ax - bx, ay - by
    return dx * dx + dy * dy

def _targets_in_range(max_r: float = 220.0, include_self: bool = False) -> List[str]:
    me = _self_pos()
    if not me:
        return []
    mx, my = me
    out: List[Tuple[str, float]] = []
    with H.LOCK:
        for p in (H.SNAP.get("players") or []):
            pid = p.get("pid")
            if not isinstance(pid, str):
                continue
            if not include_self and pid == PID:
                continue
            try:
                px, py = float(p["x"]), float(p["y"])
            except Exception:
                continue
            d2 = _dist2(mx, my, px, py)
            if d2 <= max_r * max_r:
                out.append((pid, d2))
    out.sort(key=lambda t: t[1])
    return [pid for pid, _ in out]

PROX_FILTER = True
def _toggle_prox_filter():
    global PROX_FILTER
    PROX_FILTER = not PROX_FILTER

def _list_targets_for_cycle() -> List[str]:
    return _targets_in_range() if PROX_FILTER else _list_targets()

def _cycle_target(next_: bool = True):
    tlist = _list_targets_for_cycle()
    if not tlist:
        return None
    cur = _snapshot().get("target")
    if cur not in tlist:
        _set("target", tlist[0])
        return tlist[0]
    idx = tlist.index(cur)
    idx = (idx + (1 if next_ else -1)) % len(tlist)
    _set("target", tlist[idx])
    return tlist[idx]

def _in_range_selected(max_r: float = 220.0) -> bool:
    t = _snapshot().get("target")
    return bool(t and t in _targets_in_range(max_r))


def _my_player_row() -> Optional[dict]:
    with H.LOCK:
        for p in (H.SNAP.get("players") or []):
            if p.get("pid") == PID:
                return p
    return None

def _my_inventory() -> Dict[str, int]:
    me = _my_player_row()
    if not isinstance(me, dict):
        return {}
    inv = me.get("inventory") or me.get("inv") or {}
    return dict(inv) if isinstance(inv, dict) else {}

def _has_item(item: Optional[str], qty: int = 1) -> bool:
    if not item:
        return False
    return _my_inventory().get(item, 0) >= int(qty)


# ============================================================================
# 4) SKILLS & CRAFT CATALOGS
# ============================================================================
SKILL_LIST = [
    "cook", "weave", "brew", "smelt", "glasswork", "hammer", "carve", "enchant",
    "mill", "mix", "bake", "mine", "harvest", "forage", "tan",
]
SKILL_IDX = 0
SKILL_MASTERY = 1  # 0..3 client-side; GM can clamp

def _cur_skill() -> Tuple[str, int]:
    return SKILL_LIST[SKILL_IDX % len(SKILL_LIST)], int(SKILL_MASTERY)

def _cycle_skill(next_: bool = True):
    global SKILL_IDX
    SKILL_IDX = (SKILL_IDX + (1 if next_ else -1)) % len(SKILL_LIST)

def _bump_mastery(delta: int):
    global SKILL_MASTERY
    SKILL_MASTERY = max(0, min(3, SKILL_MASTERY + int(delta)))

# Craft carousel: (recipe_id, produces_hint)
CRAFT_LIST = [
    ("mk_bread",        "bread"),
    ("mk_broth",        "broth"),
    ("mk_cooked_meat",  "cooked_meat"),
    ("mk_potion_heal",  "potion_heal"),
    ("mk_rope",         "rope"),
    ("mk_bottle_glass", "bottle_glass"),
    ("mk_thread",       "thread"),
    ("mk_cloth",        "cloth"),
    ("mk_leather",      "leather"),
    ("mk_ingot_iron",   "ingot_iron"),
    ("mk_ingot_copper", "ingot_copper"),
    ("mk_wire_copper",  "wire_copper"),
    ("mk_plate_iron",   "plate_iron"),
    ("mk_gear_simple",  "gear_simple"),
    ("mk_pie_berry",    "pie_berry"),
    ("mk_stew_hearty",  "stew_hearty"),
]
CRAFT_IDX = 0

def _cur_recipe() -> tuple[str, str]:
    rid, prod = CRAFT_LIST[CRAFT_IDX % len(CRAFT_LIST)]
    return rid, prod

def _cycle_recipe(next_: bool = True):
    global CRAFT_IDX
    CRAFT_IDX = (CRAFT_IDX + (1 if next_ else -1)) % len(CRAFT_LIST)


# --- HUD helpers (base + async emotion augmentation) ---
def _compose_hud_dict() -> Dict[str, Any]:
    s = _snapshot()
    skill, mast = _cur_skill()
    rid, prod   = _cur_recipe()
    return {
        "mode": MODE,
        "target": s.get("target") or "-",
        "weapon": s.get("weapon") or "-",
        "defense": s.get("defense") or "-",
        "skill": skill,
        "mastery": int(mast),
        "recipe_id": rid,
        "recipe_out": prod,
        # anger/fear will be filled asynchronously
    }

async def _hud_refresh_emotions_for(target: Optional[str]) -> None:
    if not target or target == "-":
        return
    try:
        anger = await _emo_get_async(target, "anger")
        fear  = await _emo_get_async(target, "fear")
        # merge into current HUD state exported to pygame
        hud = dict(getattr(H, "HUD_STATE", {}))
        hud["anger"] = anger
        hud["fear"]  = fear
        H.HUD_STATE = hud
    except Exception:
        # non-fatal; just keep going with the base HUD
        pass

def _hud_refresh() -> None:
    """
    Export immediately a base HUD (no await), then enrich with emotions
    in the background if a target exists.
    """
    base = _compose_hud_dict()
    H.HUD_STATE = base
    tgt = base.get("target")
    # schedule async emotion fill; do not block caller
    try:
        asyncio.get_running_loop()
        asyncio.create_task(_hud_refresh_emotions_for(tgt))
    except RuntimeError:
        # no running loop yet (e.g., during init) — ignore
        pass



# ============================================================================
# 5) GATES, HOOKS & NORMALIZATION
# ============================================================================
EMOTION_THRESHOLDS = {
    "anger_block_trade": 0.6,  # block trade/learn/teach
    "fear_block_attack": 0.7,  # block attack
}

async def _gate(kind: str, dst_pid: Optional[str]) -> bool:
    if not dst_pid:
        return False
    if kind in ("trade", "learn", "teach"):
        if await _emo_get_async(dst_pid, "anger") >= EMOTION_THRESHOLDS["anger_block_trade"]:
            return False
    if kind == "attack":
        if await _emo_get_async(dst_pid, "fear") >= EMOTION_THRESHOLDS["fear_block_attack"]:
            return False
    return True

@client.hook(Direction.RECEIVE)
async def rx_normalize(payload: Any) -> Optional[dict]:
    if isinstance(payload, dict) and "content" in payload and isinstance(payload["content"], dict):
        return payload["content"]
    return payload

@client.hook(Direction.SEND)
async def tx_stamp_pid(payload: Any) -> Optional[dict]:
    if isinstance(payload, str):
        client.logger.warning("[Player] SEND hook got str payload; ignoring")
        return None
    if isinstance(payload, dict):
        # normalize cmd field 'with_' -> 'with'
        if payload.get("type") == "overlay":
            overlay = payload.get("overlay")
            if isinstance(overlay, dict):
                cmd = overlay.get("cmd")
                if isinstance(cmd, dict) and "with_" in cmd and "with" not in cmd:
                    cmd["with"] = cmd.pop("with_")
        if "pid" not in payload:
            payload["pid"] = PID
        if payload.get("type") == "overlay" and "seq" not in payload:
            payload["seq"] = await next_overlay_seq_async()
    return payload


# ============================================================================
# 6) RECEIVE ROUTES
# ============================================================================
@client.receive("world_state")
async def on_world(msg: dict) -> None:
    if not isinstance(msg, dict) or msg.get("type") != "world_state":
        return None

    now = time.time()
    # atomically copy world snapshot
    with H.LOCK:
        SNAP = H.SNAP
        SNAP["ts"] = msg.get("ts")
        if "bounds"   in msg: SNAP["bounds"]   = msg["bounds"]
        if "players"  in msg: SNAP["players"]  = msg["players"]
        if "overlays" in msg: SNAP["overlays"] = msg["overlays"]

    # fold chat overlays into read-only side chat with strong dedupe
    for overlay in (msg.get("overlays") or []):
        if not isinstance(overlay, dict):
            continue
        pid = overlay.get("pid")
        chat_text = overlay.get("chat")
        if not pid or not isinstance(chat_text, str):
            continue
        text = chat_text.strip()
        if not text:
            continue

        seq = overlay.get("seq")
        if isinstance(seq, int):
            if H.SEQ.seen("chat_fold", pid, seq):
                continue

        key = (pid, text) if not isinstance(seq, int) else (pid, text, seq)
        last_ts = H.LAST_CHAT.get(key)
        if (last_ts is None) or (now - last_ts > H.CHAT_DEDUPE_SECS):
            H.LAST_CHAT[key] = now
            with H.CHAT_LOCK:
                H.CHAT_LOG.append((now, pid, text))

# @client.receive("act_on_some_key")
# async def on_act_on_some_key(msg: dict) -> None:
#     """Example reactive consumer; left as-is but renamed to avoid shadowing."""
#     if not isinstance(msg, dict) or msg.get("type") != "world_state":
#         return None
#     if getattr(on_act_on_some_key, "_switch", 0) == 0:
#         return None

    some_key = "chat"
    for overlay in (msg.get("overlays") or []):
        if not isinstance(overlay, dict):
            continue
        pid = overlay.get("pid")
        if not pid:
            continue
        seq = overlay.get("seq")
        if isinstance(seq, int) and H.SEQ.seen("act_on_some_key", pid, seq):
            continue
        key_info = overlay.get(some_key)
        print(pid, key_info)


# ============================================================================
# 7) SEND ROUTES — TICK & CHAT
# ============================================================================
@client.send("directions")
async def tick() -> dict:
    await asyncio.sleep(0.2)  # 5 Hz movement inputs
    with H.LOCK:
        keys = dict(H.INPUT)
    return {"type": "tick", "ts": time.time(), "keys": keys}

@client.send("chat")
@send_on_keypress("h", overlay_ttl_ms=1500)
async def send_chat_hello_immediate() -> dict:
    return {"type": "overlay", "overlay": {"chat": f"hello, how are you? I am {PID}", "ttl_ms": 1500}}

# @client.send("switch")
# @send_on_keypress("l", overlay_ttl_ms=100)
# async def send_switch_immediate() -> dict:
#     on_act_on_some_key._switch = 0 if getattr(on_act_on_some_key, "_switch", 0) else 1
#     return {"type": "overlay", "overlay": {"chat": f"I am in {on_act_on_some_key._switch}", "ttl_ms": 100}}


# ============================================================================
# 8) SEND ROUTES — GAMEPLAY BINDS
# ============================================================================

# --- Targeting & Modes ---
@client.send("target/nearest")
@send_on_keypress("g", overlay_ttl_ms=700)
async def target_nearest() -> Optional[dict]:
    nearby = _targets_in_range()
    if not nearby:
        return _toast("no one nearby")
    _set("target", nearby[0])
    _hud_refresh()  # NEW
    asyncio.create_task(kv_set("target", nearby[0]))
    return _toast(f"target: {nearby[0]}")

@client.send("target/prox_toggle")
@send_on_keypress("v", overlay_ttl_ms=900)
async def toggle_prox_filter() -> dict:
    _toggle_prox_filter()
    return _toast(f"cycle uses proximity: {'on' if PROX_FILTER else 'off'}")

@client.send("mode/cycle")
@send_on_keypress("m", overlay_ttl_ms=900)
async def cycle_mode() -> dict:
    _cycle_mode()
    _hud_refresh()  # NEW
    asyncio.create_task(kv_set("mode", MODE))
    return _toast(f"mode: {MODE}")

@client.send("target/next")
@send_on_keypress("tab", overlay_ttl_ms=600)
async def next_target() -> Optional[dict]:
    t = _cycle_target(next_=True)
    if t:
        _hud_refresh()  # NEW
        asyncio.create_task(kv_set("target", t))
    return _toast(f"target: {t}") if t else _toast("target: none")

@client.send("target/prev")
@send_on_keypress("`", overlay_ttl_ms=600)
async def prev_target() -> Optional[dict]:
    t = _cycle_target(next_=False)
    if t:
        _hud_refresh()  # NEW
        asyncio.create_task(kv_set("target", t))
    return _toast(f"target: {t}") if t else _toast("target: none")


# --- Social: reputation / emotions ---
@client.send("rep/up")
@send_on_keypress("+", overlay_ttl_ms=600)
async def rep_up() -> Optional[dict]:
    s = _snapshot()
    if MODE != "social":
        return _toast("set mode: social (M)")
    if not s.get("target"):
        return _toast("no target")
    return _cmd("rep", target=s["target"], delta=+1)

@client.send("rep/down")
@send_on_keypress("-", overlay_ttl_ms=600)
async def rep_down() -> Optional[dict]:
    s = _snapshot()
    if MODE != "social":
        return _toast("set mode: social (M)")
    if not s.get("target"):
        return _toast("no target")
    return _cmd("rep", target=s["target"], delta=-1)

@client.send("emo/angry_up")
@send_on_keypress("'", overlay_ttl_ms=600)
async def angry_up() -> Optional[dict]:
    t = _snapshot().get("target")
    if not t:
        return _toast("no target")
    val = await _emo_bump_async(t, "anger", +0.1)
    _hud_refresh()  # moved AFTER bump
    return _toast(f"anger({t})={val:.1f}")

@client.send("emo/angry_down")
@send_on_keypress("\"", overlay_ttl_ms=600)
async def angry_down() -> Optional[dict]:
    t = _snapshot().get("target")
    if not t:
        return _toast("no target")
    val = await _emo_bump_async(t, "anger", -0.1)
    _hud_refresh()  # moved AFTER bump
    return _toast(f"anger({t})={val:.1f}")

@client.send("emo/fear_up")
@send_on_keypress("\\", overlay_ttl_ms=600)
async def fear_up() -> Optional[dict]:
    t = _snapshot().get("target")
    if not t:
        return _toast("no target")
    val = await _emo_bump_async(t, "fear", +0.1)
    _hud_refresh()  # moved AFTER bump
    return _toast(f"fear({t})={val:.1f}")

# --- Social: learn/teach with skill carousel ---
@client.send("skill/next")
@send_on_keypress("]", overlay_ttl_ms=700)
async def skill_next() -> dict:
    _cycle_skill(True)
    _hud_refresh()  # NEW
    s, m = _cur_skill()
    asyncio.create_task(kv_set("skill_type", s))
    asyncio.create_task(kv_set("skill_mastery", str(m)))
    return _toast(f"skill: {s} m{m}")

@client.send("skill/prev")
@send_on_keypress("[", overlay_ttl_ms=700)
async def skill_prev() -> dict:
    _cycle_skill(False)
    _hud_refresh()  # NEW
    s, m = _cur_skill()
    asyncio.create_task(kv_set("skill_type", s))
    asyncio.create_task(kv_set("skill_mastery", str(m)))
    return _toast(f"skill: {s} m{m}")

@client.send("skill/mastery/up")
@send_on_keypress("=", overlay_ttl_ms=700)
async def mastery_up() -> dict:
    _bump_mastery(+1)
    _hud_refresh()  # NEW
    s, m = _cur_skill()
    asyncio.create_task(kv_set("skill_mastery", str(m)))
    return _toast(f"skill: {s} m{m}")

@client.send("skill/mastery/down")
@send_on_keypress(";", overlay_ttl_ms=700)
async def mastery_down() -> dict:
    _bump_mastery(-1)
    _hud_refresh()  # NEW
    s, m = _cur_skill()
    asyncio.create_task(kv_set("skill_mastery", str(m)))
    return _toast(f"skill: {s} m{m}")

@client.send("skill/learn")
@send_on_keypress("l", overlay_ttl_ms=900)
async def learn_from_target() -> Optional[dict]:
    s = _snapshot()
    if MODE != "social":
        return _toast("set mode: social (M)")
    t = s.get("target")
    if not t:
        return _toast("no target")
    if not await _gate("learn", t):
        return _toast("won't learn (anger gate)")
    ptype, mast = _cur_skill()
    return _cmd("learn", to=t, power={"type": ptype, "mastery": mast}, pay={"bottle_glass": 1})

@client.send("skill/teach")
@send_on_keypress("k", overlay_ttl_ms=900)
async def teach_target() -> Optional[dict]:
    s = _snapshot()
    if MODE != "social":
        return _toast("set mode: social (M)")
    t = s.get("target")
    if not t:
        return _toast("no target")
    if not await _gate("teach", t):
        return _toast("won't teach (anger gate)")
    ptype, mast = _cur_skill()
    return _cmd("teach", to=t, power={"type": ptype, "mastery": mast}, pay={"rope": 1})

# --- Trade ---
@client.send("trade/propose")
@send_on_keypress("p", overlay_ttl_ms=900)
async def trade_propose() -> Optional[dict]:
    s = _snapshot()
    if MODE != "trade":
        return _toast("set mode: trade (M)")
    t = s.get("target")
    if not t:
        return _toast("no target")
    if not _in_range_selected():
        return _toast("target not in range")
    if not await _gate("trade", t):
        return _toast("won't trade (anger gate)")
    return _cmd("trade", to=t, give={"bread": 1}, want={"wood": 1})

@client.send("trade/accept")
@send_on_keypress("o", overlay_ttl_ms=900)
async def trade_accept_last() -> Optional[dict]:
    if MODE != "trade":
        return _toast("set mode: trade (M)")
    if not LAST_TXID:
        return _toast("no tx to accept")
    return _cmd("accept", txid=LAST_TXID)

@client.send("trade/cancel")
@send_on_keypress("i", overlay_ttl_ms=900)
async def trade_cancel_last() -> Optional[dict]:
    if MODE != "trade":
        return _toast("set mode: trade (M)")
    if not LAST_TXID:
        return _toast("no tx to cancel")
    return _cmd("cancel", txid=LAST_TXID)

# --- Craft carousel / actions ---
@client.send("craft/next")
@send_on_keypress(".", overlay_ttl_ms=700)
async def craft_next() -> dict:
    if MODE != "craft":
        return _toast("set mode: craft (M)")
    _cycle_recipe(True)
    _hud_refresh()  # NEW
    rid, prod = _cur_recipe()
    asyncio.create_task(kv_set("craft_recipe", rid))
    return _toast(f"recipe: {rid} → {prod}")

@client.send("craft/prev")
@send_on_keypress(",", overlay_ttl_ms=700)
async def craft_prev() -> dict:
    if MODE != "craft":
        return _toast("set mode: craft (M)")
    _cycle_recipe(False)
    _hud_refresh()  # NEW
    rid, prod = _cur_recipe()
    asyncio.create_task(kv_set("craft_recipe", rid))
    return _toast(f"recipe: {rid} → {prod}")

@client.send("craft/one")
@send_on_keypress("n", overlay_ttl_ms=900)  # 'n' for craft-now
async def craft_one() -> dict:
    if MODE != "craft":
        return _toast("set mode: craft (M)")
    rid, _ = _cur_recipe()
    return _cmd("craft", recipe=rid, times=1)

@client.send("craft/max")
@send_on_keypress("/", overlay_ttl_ms=900)
async def craft_max() -> dict:
    if MODE != "craft":
        return _toast("set mode: craft (M)")
    rid, _ = _cur_recipe()
    return _cmd("craft", recipe=rid, times="max")

# --- Combat toggles & equips ---
@client.send("toggle/attack")
@send_on_keypress("q", overlay_ttl_ms=120)
async def toggle_attack() -> Optional[dict]:
    _toggle("armed_attack")
    return None

@client.send("toggle/counter")
@send_on_keypress("e", overlay_ttl_ms=120)
async def toggle_counter() -> Optional[dict]:
    _toggle("armed_counter")
    return None

@client.send("choose/weapon/knife")
@send_on_keypress("1", overlay_ttl_ms=120)
async def choose_weapon_knife() -> Optional[dict]:
    if not _has_item("knife"):
        return _toast("you don't have a knife")
    _set("weapon", "knife")
    _hud_refresh()  # NEW
    asyncio.create_task(kv_set("weapon", "knife"))
    return _toast("weapon: knife")

@client.send("choose/weapon/pickaxe")
@send_on_keypress("2", overlay_ttl_ms=120)
async def choose_weapon_pickaxe() -> Optional[dict]:
    if not _has_item("pickaxe"):
        return _toast("you don't have a pickaxe")
    _set("weapon", "pickaxe")
    _hud_refresh()  # NEW
    asyncio.create_task(kv_set("weapon", "pickaxe"))
    return _toast("weapon: pickaxe")

@client.send("choose/weapon/crystal")
@send_on_keypress("3", overlay_ttl_ms=120)
async def choose_weapon_crystal() -> Optional[dict]:
    if not _has_item("crystal_shard"):
        return _toast("you don't have a crystal shard")
    _set("weapon", "crystal_shard")
    _hud_refresh()  # NEW
    asyncio.create_task(kv_set("weapon", "crystal_shard"))
    return _toast("weapon: crystal shard")

@client.send("choose/defense/plate")
@send_on_keypress("4", overlay_ttl_ms=120)
async def choose_def_plate() -> Optional[dict]:
    if not _has_item("plate_iron"):
        return _toast("you don't have plate iron")
    _set("defense", "plate_iron")
    _hud_refresh()  # NEW
    asyncio.create_task(kv_set("defense", "plate_iron"))
    return _toast("defense: plate iron")

@client.send("choose/defense/cloth")
@send_on_keypress("5", overlay_ttl_ms=120)
async def choose_def_cloth() -> Optional[dict]:
    if not _has_item("cloth"):
        return _toast("you don't have cloth armor")
    _set("defense", "cloth")
    _hud_refresh()  # NEW
    asyncio.create_task(kv_set("defense", "cloth"))
    return _toast("defense: cloth")

@client.send("choose/defense/amulet")
@send_on_keypress("6", overlay_ttl_ms=120)
async def choose_def_amulet() -> Optional[dict]:
    if not _has_item("amulet_minor"):
        return _toast("you don't have an amulet")
    _set("defense", "amulet_minor")
    _hud_refresh()  # NEW
    asyncio.create_task(kv_set("defense", "amulet_minor"))
    return _toast("defense: amulet")

@client.send("combo/commit")
@send_on_keypress(" ", overlay_ttl_ms=100)
async def commit_combo() -> Optional[dict]:
    s = _snapshot()

    # Counter: require equipped defense that you own
    if s["armed_counter"] and s["defense"]:
        if not _has_item(s["defense"]):
            return _toast("no such defense equipped")
        _set("armed_counter", False)
        return _cmd("counter", target=s["target"] or PID, with_=s["defense"])

    # Attack: require target in range, allowed by gate, and owned weapon
    if s["armed_attack"] and s["target"] and s["weapon"]:
        if not _in_range_selected():
            return _toast("target not in range")
        if not await _gate("attack", s["target"]):
            return _toast("won't attack (fear gate)")
        if not _has_item(s["weapon"]):
            return _toast("no such weapon equipped")
        _set("armed_attack", False)
        return _cmd("attack", target=s["target"], with_=s["weapon"])

    return None


# ============================================================================
# 9) GM CMD STATUS (ASYNC)
# ============================================================================
async def _handle_one_cmd_status(msg: dict):
    status = msg.get("status", "?")
    kind   = msg.get("kind", "?")
    src    = msg.get("from", "?")
    txid   = msg.get("txid", "-")
    reason = msg.get("reason")
    effects = msg.get("effects") or {}

    if kind in ("trade", "learn", "teach") and isinstance(txid, str):
        if status in ("accepted", "matched"):
            _remember_tx(txid, kind)

    if reason:
        client.logger.info(f"[GM/cmd] {status} {kind} from={src} txid={txid} reason={reason}")
    else:
        client.logger.info(f"[GM/cmd] {status} {kind} from={src} txid={txid}")

    if "health" in effects:
        client.logger.info(f"[GM/effects/health] {effects['health']}")
    if "inventory" in effects:
        client.logger.info(f"[GM/effects/inventory] {effects['inventory']}")
    if "skills" in effects:
        client.logger.info(f"[GM/effects/skills] {effects['skills']}")
    if "combat" in effects:
        client.logger.info(f"[GM/effects/combat] {effects['combat']}")

    # Mirror GM rep to local Reputation
    if kind == "rep" and status in ("matched", "accepted"):
        target = msg.get("target")
        try:
            delta = float(msg.get("delta", 0))
        except Exception:
            delta = 0.0
        if target:
            row = await Reputation.find(PLAYER_DB, where={"src_pid": PID, "dst_pid": target})
            base = float(row[0]["score"]) if row else 0.0
            val  = max(-1.0, min(1.0, base + delta))
            if row:
                await Reputation.update(PLAYER_DB, where={"id": row[0]["id"]}, fields={"score": val})
            else:
                await Reputation.insert(PLAYER_DB, src_pid=PID, dst_pid=target, score=val)

    # HUD toast
    now = time.time()
    if status == "matched":
        text = f"{kind} ✓"
    elif status == "accepted":
        tx_str = str(msg.get("txid", ""))
        text = f"{kind} ; txid={tx_str}" if tx_str else f"{kind} ; accepted"
    elif status == "rejected":
        text = f"{kind} ; reason={reason or msg.get('detail') or 'rejected'}"
    elif status == "error":
        text = f"{kind} ; reason={reason or msg.get('detail') or 'error'}"
    else:
        text = f"{kind} ; status={status}"
    try:
        with H.CHAT_LOCK:
            H.CHAT_LOG.append((now, "GM", text))
    except Exception:
        pass

@client.receive("gm/replies")
async def on_gm_replies(msg):
    if isinstance(msg, list):
        for item in msg:
            if isinstance(item, dict) and item.get("type") == "cmd_status":
                await _handle_one_cmd_status(item)
        return
    if isinstance(msg, dict) and msg.get("type") == "cmd_status":
        await _handle_one_cmd_status(msg)


# ============================================================================
# 10) RUNNER & MAIN
# ============================================================================
def run_client(host: Optional[str], port: Optional[int], config_path: Optional[str], config_dict: Dict[str, Any]):
    # Avoid installing signal handlers in a non-main thread
    if hasattr(client, "set_termination_signals"):
        client.set_termination_signals = lambda *a, **k: None
    asyncio.set_event_loop(asyncio.new_event_loop())

    effective_cfg = dict(config_dict)
    hp = dict(effective_cfg.get("hyper_parameters", {}))
    effective_cfg["hyper_parameters"] = hp

    client.run(
        host=host if host is not None else "127.0.0.1",
        port=port if port is not None else 8888,
        config_path=config_path,
        config_dict=effective_cfg if config_path is None else None,
    )

async def _restore_player_state():
    """Restore UI/UX-facing state from MemoryKV."""
    try:
        last_mode = await kv_get("mode")
        if last_mode in MODE_ORDER:
            _set_mode(last_mode)

        last_target = await kv_get("target")
        if last_target:
            _set("target", last_target)

        # craft
        rid = await kv_get("craft_recipe")
        if rid:
            global CRAFT_IDX
            try:
                idx = [r for r,_ in CRAFT_LIST].index(rid)
                CRAFT_IDX = idx
            except ValueError:
                pass

        # skills
        s = await kv_get("skill_type")
        m = await kv_get("skill_mastery")
        if s in SKILL_LIST:
            global SKILL_IDX
            SKILL_IDX = SKILL_LIST.index(s)
        if m is not None:
            global SKILL_MASTERY
            try:
                SKILL_MASTERY = max(0, min(3, int(m)))
            except Exception:
                pass

        # equip
        w = await kv_get("weapon")
        d = await kv_get("defense")
        if w and _has_item(w):
            _set("weapon", w)
        if d and _has_item(d):
            _set("defense", d)

        if d and _has_item(d):
            _set("defense", d)
    except Exception:
        pass

    _hud_refresh()  # NEW: reflect restored state in HUD

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run a Summoner Player with persistent identity, seeded grass, and optional avatar.")
    parser.add_argument("--config", dest="config_path", required=False, help="Path to JSON config for the client.")
    parser.add_argument("--host", type=str, default=None, help="Server host (overrides config).")
    parser.add_argument("--port", type=int, default=None, help="Server port (overrides config).")
    parser.add_argument("--avatar", type=str, default="wizard.png", help="Path to a PNG with transparency (relative/absolute).")
    parser.add_argument("--id", type=str, default=None, help="Persistent ID alias. If missing, a new <id>.id is created.")
    parser.add_argument("--seed", type=str, default="lava", help="Deterministic world appearance seed (stored in world_seed.txt).")
    args = parser.parse_args()

    # Identity
    PID = H.load_or_create_identity(args.id)
    client.logger.info(f"[Player] Using persistent ID: {PID}")
    client.name = f"Player_{PID}"

    # World seed
    world_seed = H.load_or_create_world_seed(args.seed)

    # Config
    if args.config_path:
        try:
            with open(args.config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            print(f"[Player] Failed to load config {args.config_path}: {e}")
            cfg = H.DEFAULT_PLAYER_CONFIG
    else:
        cfg = H.DEFAULT_PLAYER_CONFIG

    # Expose PID to helpers (for rendering, etc.)
    H.PID = PID

    # Start Summoner client in background
    t = threading.Thread(
        target=run_client, name="summoner-client", daemon=True,
        args=(args.host, args.port, args.config_path, cfg)
    )
    t.start()

    try:
        # DB init + restore state before entering UI loop
        asyncio.run(init_player_db(PID))
        asyncio.run(_restore_player_state())
        _hud_refresh()  # seed HUD right away

        H.ui_loop(args.avatar, world_seed)

    except (asyncio.CancelledError, KeyboardInterrupt):
        pass

    finally:
        H.RUNNING = False
        # Ensure DB is closed exactly once, and only if it was created
        if PLAYER_DB is not None:
            try:
                asyncio.run(PLAYER_DB.close())
            except RuntimeError:
                # If an event loop is already running (unlikely here), schedule close
                loop = asyncio.new_event_loop()
                loop.run_until_complete(PLAYER_DB.close())
                loop.close()

