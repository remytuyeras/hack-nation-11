# gm_cmds.py
"""
Structured command dispatcher for the Game Master.

- No text parsing: the Player sends a dict under overlay["cmd"].
- Aurora-level replay protection should be enabled in your GM via @keyed_receive(..., seq_by="seq").
- This module is intentionally in-memory and decoupled; swap to DB-backed ops later.

Typical Player envelope:
{
  "type": "overlay",
  "seq": 123,                          # GM handles replay via @keyed_receive(seq_by="seq")
  "overlay": {
    "cmd": { "kind": "...", ... }      # make | rep | trade | accept | cancel | attack | counter | learn | teach
  }
}

Typical GM response:
{
  "type":   "cmd_status",
  "status": "accepted" | "matched" | "rejected" | "error",
  "kind":   "<command>",
  "from":   "<pid>",
  "txid":   "t-abc",                   # only for offers
  "reason": "<short_code>",            # for rejected/error
  "effects": { ... }                   # for matched; describes state deltas
}

Integration sketch (inside your GM):
    from gm_cmds import (
        process_structured_cmd, load_combat_rules, set_players_reference
    )
    load_combat_rules("resources.json")           # enables attack/counter lookups
    set_players_reference(players)                # {pid: Player} with .x .y floats

    @agent.keyed_receive("overlay", key_by="pid", seq_by="seq")
    async def on_overlay(msg: dict):
        pid = msg.get("pid")
        ov = msg.get("overlay")
        if not pid or not isinstance(ov, dict):
            return None
        cmd = ov.get("cmd")
        if isinstance(cmd, dict):
            status = process_structured_cmd(pid, cmd)
            # Option A: enqueue into your regular GM broadcast stream
            await agent.send_now("gm/reply", status)
"""

from __future__ import annotations

import json
import math
import time
import itertools
from typing import Dict, Any, Optional, Union
import pathlib

# ---------------------------------------------------------------------------
# In-memory state (swap to DB when ready)
# ---------------------------------------------------------------------------

INVENTORY: Dict[str, Dict[str, int]] = {}    # pid -> {item: qty}
RESERVED:  Dict[str, Dict[str, Any]] = {}    # txid -> {pid, items}
PENDING:   Dict[str, Dict[str, Any]] = {}    # txid -> offer dict

OFFER_TTL_MS    = 5000
PROXIMITY_R     = 220.0

_TX = itertools.count(1)
def new_txid() -> str: return f"t-{next(_TX):x}"

def inv_get(pid: str) -> Dict[str, int]:
    return INVENTORY.setdefault(pid, {})

def inv_has(pid: str, need: Dict[str, int]) -> bool:
    inv = inv_get(pid)
    try:
        return all(inv.get(k, 0) >= int(v) and int(v) >= 0 for k, v in (need or {}).items())
    except Exception:
        return False

def inv_add(pid: str, delta: Dict[str, int]) -> None:
    inv = inv_get(pid)
    for k, v in (delta or {}).items():
        try:
            iv = int(v)
        except Exception:
            continue
        inv[k] = max(0, int(inv.get(k, 0)) + iv)

def reserve(pid: str, txid: str, items: Dict[str, int]) -> bool:
    """Move items from inventory into a reservation bucket for txid."""
    if not inv_has(pid, items):
        return False
    inv_add(pid, {k: -int(v) for k, v in items.items()})
    RESERVED[txid] = {"pid": pid, "items": dict(items)}
    return True

def release(txid: str) -> None:
    """Release a reservation back to its owner's inventory."""
    r = RESERVED.pop(txid, None)
    if r:
        inv_add(r["pid"], r["items"])

def now_ms() -> int: return int(time.time() * 1000)

# ---------------------------------------------------------------------------
# Proximity (needs Player objects with .x, .y)
# ---------------------------------------------------------------------------

_players_ref: Dict[str, Any] = {}

def set_players_reference(players_dict: Dict[str, Any]) -> None:
    """Call once from the GM to allow proximity checks."""
    global _players_ref
    _players_ref = players_dict

def distance(a: str, b: str) -> float:
    pa, pb = _players_ref.get(a), _players_ref.get(b)
    if not pa or not pb:
        return float("inf")
    try:
        return math.hypot(float(pa.x) - float(pb.x), float(pa.y) - float(pb.y))
    except Exception:
        return float("inf")

def in_range(a: str, b: str) -> bool:
    return distance(a, b) <= PROXIMITY_R

# ---------------------------------------------------------------------------
# Combat rules (from resources.json["combat"])
# ---------------------------------------------------------------------------

COMBAT: dict = {}                              # entire combat block
DEFENSE: Dict[str, Dict[str, Union[int, str]]] = {}  # pid -> {"until": ts_ms, "item": str}


def load_combat_rules(resources_path: Optional[str] = None) -> None:
    """
    Load combat rules from resources.json. See your current file for shape.
    """
    path = pathlib.Path(resources_path) if resources_path else pathlib.Path(__file__).with_name("resources.json")
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f) or {}
    global COMBAT
    COMBAT = data.get("combat", {}) or {}


def _c_items() -> dict:
    return COMBAT.get("items", {}) or {}

def _req_attack() -> bool:
    return bool((COMBAT.get("requires") or {}).get("attack_power", False))

def _req_defense() -> bool:
    return bool((COMBAT.get("requires") or {}).get("defense_power", False))

def _attack_tag(item: str) -> Optional[str]:
    info = _c_items().get(item) or {}
    tag = info.get("attack")
    return tag if isinstance(tag, str) and tag else None

def _defense_tag(item: Optional[str]) -> str:
    if not item:
        return "none"
    info = _c_items().get(item) or {}
    tag = info.get("defense")
    return tag if isinstance(tag, str) and tag else "none"

def _base_damage(item: str) -> float:
    info = _c_items().get(item) or {}
    if isinstance(info.get("damage"), (int, float)):
        return float(info["damage"])
    return float(COMBAT.get("base_damage", 1))

def _opposition_mult(atk: str, dfn: str) -> float:
    row = (COMBAT.get("opposition") or {}).get(atk) or {}
    vs = row.get("vs") or {}
    m = vs.get(dfn)
    return float(m) if isinstance(m, (int, float)) else 1.0

def _defense_item_if_active(pid: str) -> Optional[str]:
    slot = DEFENSE.get(pid)
    if not slot:
        return None
    until = int(slot.get("until", 0))
    if now_ms() > until:
        DEFENSE.pop(pid, None)
        return None
    return str(slot.get("item")) if slot.get("item") else None

# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def process_structured_cmd(pid: str, cmd: Dict[str, Any]) -> Dict[str, Any]:
    """
    Entrypoint for one structured command. Returns a cmd_status dict.
    """
    sweep_expired_offers()
    kind = (cmd.get("kind") or "").lower()
    try:
        if kind == "make":    return _do_make(pid, cmd)
        if kind == "rep":     return _do_rep(pid, cmd)
        if kind == "trade":   return _do_trade(pid, cmd)
        if kind == "accept":  return _do_accept(pid, cmd)
        if kind == "cancel":  return _do_cancel(pid, cmd)
        if kind == "attack":  return _do_attack(pid, cmd)
        if kind == "counter": return _do_counter(pid, cmd)
        if kind == "learn":   return _do_learn_teach(pid, cmd, is_learn=True)
        if kind == "teach":   return _do_learn_teach(pid, cmd, is_learn=False)
        return {"type": "cmd_status", "status": "error", "reason": "unknown_kind", "from": pid}
    except Exception as e:
        return {"type": "cmd_status", "status": "error", "reason": "exception", "detail": str(e), "from": pid}

# ---------------------------------------------------------------------------
# Handlers (minimal but consistent)
# ---------------------------------------------------------------------------

def _do_make(pid: str, cmd: Dict[str, Any]) -> Dict[str, Any]:
    items = cmd.get("items") or {}
    if not isinstance(items, dict) or not items:
        return {"type": "cmd_status", "status": "error", "reason": "bad_make", "from": pid}

    effects = {"inventory": {pid: {}}}
    for out_item, qty in items.items():
        try:
            q = int(qty)
        except Exception:
            return {"type": "cmd_status", "status": "error", "reason": "bad_qty", "item": out_item, "from": pid}
        if q <= 0:
            return {"type": "cmd_status", "status": "error", "reason": "bad_qty", "item": out_item, "from": pid}

        if out_item == "bread":
            need = {"dough": q}
            if not inv_has(pid, need):
                return {"type": "cmd_status", "status": "rejected", "reason": "insufficient_inputs", "item": out_item, "from": pid}
            # apply in-memory
            inv_add(pid, {k: -v for k, v in need.items()})
            inv_add(pid, {out_item: q})
            # report full deltas
            for k, v in need.items():
                effects["inventory"][pid][k] = effects["inventory"][pid].get(k, 0) - int(v)
            effects["inventory"][pid][out_item] = effects["inventory"][pid].get(out_item, 0) + q
        else:
            return {"type": "cmd_status", "status": "rejected", "reason": "unknown_recipe", "item": out_item, "from": pid}

    return {"type": "cmd_status", "status": "matched", "kind": "make", "effects": effects, "from": pid}

def _do_rep(pid: str, cmd: Dict[str, Any]) -> Dict[str, Any]:
    """
    Local reputation bump:
      { "kind":"rep", "target":"bob", "delta": 1 }
    Persist + clamp/rate-limit when you wire DB.
    """
    target, delta = cmd.get("target"), cmd.get("delta")
    if not isinstance(target, str) or not isinstance(delta, int):
        return {"type": "cmd_status", "status": "error", "reason": "bad_rep", "from": pid}
    return {"type": "cmd_status", "status": "matched", "kind": "rep", "from": pid, "target": target, "delta": delta}

def _do_trade(pid: str, cmd: Dict[str, Any]) -> Dict[str, Any]:
    """
    Bilateral trade offer:
      { "kind":"trade", "to":"bob", "give":{"wood":2}, "want":{"rock":1} }
    Items offered by proposer are reserved. Use "accept" to commit.
    """
    to, give, want = cmd.get("to"), cmd.get("give") or {}, cmd.get("want") or {}
    if not isinstance(to, str) or not give or not want:
        return {"type": "cmd_status", "status": "error", "reason": "bad_trade", "from": pid}
    if not inv_has(pid, give):
        return {"type": "cmd_status", "status": "rejected", "reason": "insufficient_inventory", "from": pid}

    txid = new_txid()
    if not reserve(pid, txid, give):
        return {"type": "cmd_status", "status": "rejected", "reason": "reserve_failed", "from": pid}

    PENDING[txid] = {
        "type": "trade",
        "from": pid,
        "to": to,
        "give": dict(give),
        "want": dict(want),
        "ts": now_ms(),
        "ttl": OFFER_TTL_MS,
    }
    return {"type": "cmd_status", "status": "accepted", "kind": "trade",
            "txid": txid, "from": pid, "to": to, "give": give, "want": want}

def _do_accept(pid: str, cmd: Dict[str, Any]) -> Dict[str, Any]:
    """
    Accept a pending offer:
      { "kind":"accept", "txid":"t-abc" }
    """
    txid = cmd.get("txid")
    off = PENDING.get(txid)
    if not isinstance(txid, str) or not off:
        return {"type": "cmd_status", "status": "error", "reason": "unknown_txid", "from": pid}

    if now_ms() > int(off.get("ts", 0)) + int(off.get("ttl", 0)):
        release(txid)
        PENDING.pop(txid, None)
        return {"type": "cmd_status", "status": "rejected", "reason": "expired", "txid": txid, "from": pid}

    otype = off.get("type")
    if otype == "trade":
        return _commit_trade(txid, off, pid)
    if otype in ("learn", "teach"):
        return _commit_learn_teach(txid, off, pid)
    return {"type": "cmd_status", "status": "error", "reason": "bad_offer_type", "txid": txid, "from": pid}

def _do_cancel(pid: str, cmd: Dict[str, Any]) -> Dict[str, Any]:
    """
    Cancel a pending offer (proposer only):
      { "kind":"cancel", "txid":"t-abc" }
    """
    txid = cmd.get("txid")
    off = PENDING.get(txid)
    if not isinstance(txid, str) or not off:
        return {"type": "cmd_status", "status": "error", "reason": "unknown_txid", "from": pid}
    if off.get("from") != pid:
        return {"type": "cmd_status", "status": "rejected", "reason": "not_owner", "txid": txid, "from": pid}
    release(txid)
    PENDING.pop(txid, None)
    return {"type": "cmd_status", "status": "matched", "kind": "cancel", "txid": txid, "from": pid}

def _commit_trade(txid: str, off: Dict[str, Any], acceptor: str) -> Dict[str, Any]:
    proposer, counterparty = off["from"], off["to"]
    if acceptor != counterparty:
        return {"type": "cmd_status", "status": "rejected", "reason": "not_counterparty", "txid": txid, "from": acceptor}

    if not in_range(proposer, counterparty):
        return {"type": "cmd_status", "status": "rejected", "reason": "not_in_range", "txid": txid, "from": acceptor}

    if not inv_has(counterparty, off["want"]):
        return {"type": "cmd_status", "status": "rejected", "reason": "insufficient_inventory", "txid": txid, "from": acceptor}

    # Counterparty pays their side
    inv_add(counterparty, {k: -int(v) for k, v in off["want"].items()})

    # Move proposer’s reserved bundle to counterparty (consumes reservation)
    consume(txid, grant_to=counterparty)

    # Proposer receives what they wanted
    inv_add(proposer, off["want"])

    PENDING.pop(txid, None)

    # Build clear per-pid deltas
    eff_counterparty = {**{k: int(v) for k, v in off["give"].items()},
                        **{k: -int(v) for k, v in off["want"].items()}}
    effects = {"inventory": {
        proposer:     {k: int(v) for k, v in off["want"].items()},
        counterparty: eff_counterparty,
    }}
    return {"type": "cmd_status", "status": "matched", "kind": "trade", "txid": txid, "from": acceptor, "effects": effects}


def _do_attack(pid: str, cmd: Dict[str, Any]) -> Dict[str, Any]:
    """
    Minimal combat via opposition matrix:
      { "kind":"attack", "target":"bob", "with":"knife" }
    """
    target, weapon = cmd.get("target"), cmd.get("with")
    if not isinstance(target, str) or not isinstance(weapon, str):
        return {"type": "cmd_status", "status": "error", "reason": "bad_attack", "from": pid}
    if not in_range(pid, target):
        return {"type": "cmd_status", "status": "rejected", "reason": "not_in_range", "from": pid}

    atk_tag = _attack_tag(weapon)
    if _req_attack() and not atk_tag:
        return {"type": "cmd_status", "status": "rejected", "reason": "invalid_weapon", "from": pid}

    active_def_item = _defense_item_if_active(target)
    dfn_tag = _defense_tag(active_def_item)

    eff_atk = atk_tag or "none"
    dmg_base = _base_damage(weapon)
    mult = _opposition_mult(eff_atk, dfn_tag)
    damage = max(0, int(round(dmg_base * mult)))

    effects = {"health": {target: -damage}, "combat": {"attack": eff_atk, "defense": dfn_tag, "mult": mult}}
    return {"type": "cmd_status", "status": "matched", "kind": "attack", "from": pid, "effects": effects}

def _do_counter(pid: str, cmd: Dict[str, Any]) -> Dict[str, Any]:
    """
    Arm a short defense window:
      { "kind":"counter", "target":"alice", "with":"plate_iron" }
    Window is 1000ms; used only for opposition lookup.
    """
    target, item = cmd.get("target"), cmd.get("with")
    if not isinstance(target, str) or not isinstance(item, str):
        return {"type": "cmd_status", "status": "error", "reason": "bad_counter", "from": pid}

    dfn_tag = _defense_tag(item)
    if _req_defense() and dfn_tag == "none":
        return {"type": "cmd_status", "status": "rejected", "reason": "invalid_defense", "from": pid}

    DEFENSE[pid] = {"until": now_ms() + 1000, "item": item}
    return {"type": "cmd_status", "status": "accepted", "kind": "counter", "from": pid, "target": target, "with": item}

def _do_learn_teach(pid: str, cmd: Dict[str, Any], is_learn: bool) -> Dict[str, Any]:
    """
    Skill transfer offer:
      Learn: { "kind":"learn", "to":"teacher", "power":{"type":"brew","mastery":2}, "pay":{"bottle_glass":1} }
      Teach: { "kind":"teach", "to":"learner", "power":{"type":"weave","mastery":1}, "pay":{"rope":1} }
    Payer’s items are reserved until acceptance.
    """
    to, power, pay = cmd.get("to"), cmd.get("power") or {}, cmd.get("pay") or {}
    if not isinstance(to, str) or not isinstance(power, dict) or not pay:
        return {"type": "cmd_status", "status": "error", "reason": "bad_learn_teach", "from": pid}

    payer = pid if is_learn else to
    if not inv_has(payer, pay):
        return {"type": "cmd_status", "status": "rejected", "reason": "insufficient_inventory", "from": pid}

    txid = new_txid()
    if not reserve(payer, txid, pay):
        return {"type": "cmd_status", "status": "rejected", "reason": "reserve_failed", "from": pid}

    PENDING[txid] = {
        "type": "learn" if is_learn else "teach",
        "from": pid,
        "to": to,
        "power": dict(power),
        "pay": dict(pay),
        "ts": now_ms(),
        "ttl": OFFER_TTL_MS,
    }
    return {"type": "cmd_status", "status": "accepted", "kind": ("learn" if is_learn else "teach"),
            "txid": txid, "from": pid, "to": to, "power": power, "pay": pay}

def _commit_learn_teach(txid: str, off: Dict[str, Any], acceptor: str) -> Dict[str, Any]:
    learner = off["from"] if off["type"] == "learn" else off["to"]
    teacher = off["to"]   if off["type"] == "learn" else off["from"]

    if acceptor not in (learner, teacher):
        return {"type": "cmd_status", "status": "rejected", "reason": "not_counterparty", "txid": txid, "from": acceptor}
    if not in_range(learner, teacher):
        return {"type": "cmd_status", "status": "rejected", "reason": "not_in_range", "txid": txid, "from": acceptor}

    # Move the RESERVED pay to the non-payer directly; do NOT release back to payer
    res = RESERVED.get(txid)
    if res:
        payer = res["pid"]
        pay_to = teacher if payer == learner else learner
        consume(txid, grant_to=pay_to)

    PENDING.pop(txid, None)

    power    = off.get("power") or {}
    ptype    = str(power.get("type", ""))
    mastery  = power.get("mastery")
    mastery  = int(mastery) if isinstance(mastery, int) else 1

    effects = {"skills": {learner: {ptype: mastery}}}
    return {"type": "cmd_status", "status": "matched", "kind": off["type"], "txid": txid, "from": acceptor, "effects": effects}

def consume(txid: str, grant_to: Optional[str] = None) -> None:
    """
    Finalize a reservation. If grant_to is provided, the reserved items are credited
    to that pid. Otherwise they are simply discarded (burned).
    """
    r = RESERVED.pop(txid, None)
    if not r:
        return
    if grant_to:
        inv_add(grant_to, r["items"])

def sweep_expired_offers() -> None:
    now = now_ms()
    for txid, off in list(PENDING.items()):
        if now > int(off.get("ts", 0)) + int(off.get("ttl", 0)):
            # Return reserved items to owner
            release(txid)
            PENDING.pop(txid, None)
            

