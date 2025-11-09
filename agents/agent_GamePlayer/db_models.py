# db_models.py
from typing import Any, Dict, Optional, Union, Tuple
from db_sdk import Field, Model, Database
from pathlib import Path
import json, time
import hashlib, random


# ======================================================================
# ========== GM-OWNED (AUTHORITATIVE WORLD STATE & HELPERS) ============
# ======================================================================
# These tables define facts everyone must agree on. They should live in
# the GM database and be broadcast via world_state / cmd_status effects.

class Actor(Model):
    __tablename__ = "actors"
    pid       = Field("TEXT", primary_key=True)                     # player or npc id
    kind      = Field("TEXT", default="player")                     # 'player' | 'npc'
    nickname  = Field("TEXT", default=None)
    x         = Field("REAL", default=0.0)
    y         = Field("REAL", default=0.0)
    health    = Field("REAL", default=1.0, check="health >= 0.0 AND health <= 1.0")
    morality  = Field("REAL", default=0.0, check="morality >= -1.0 AND morality <= 1.0")
    last_seen = Field("TEXT", on_update=True)

class ActorPower(Model):
    __tablename__ = "actor_powers"
    id         = Field("INTEGER", primary_key=True)
    pid        = Field("TEXT", nullable=False)
    power      = Field("TEXT", nullable=False)
    time_s     = Field("REAL", default=None, check="time_s IS NULL OR time_s > 0")
    value_mult = Field("REAL", default=1.0, check="value_mult > 0")

class Inventory(Model):
    __tablename__ = "inventories"
    id   = Field("INTEGER", primary_key=True)
    pid  = Field("TEXT", nullable=False)
    item = Field("TEXT", nullable=False)
    qty  = Field("INTEGER", default=0, check="qty >= 0")

class CmdLog(Model):
    __tablename__ = "cmd_log"
    id      = Field("INTEGER", primary_key=True)
    ts      = Field("REAL", nullable=False)
    src_pid = Field("TEXT", nullable=False)
    kind    = Field("TEXT", nullable=False)
    payload = Field("TEXT", nullable=False)  # JSON-encoded original command
    status  = Field("TEXT", nullable=False)  # accepted/matched/rejected/error
    reason  = Field("TEXT", default=None)

# ---- GM: ACTOR helpers ------------------------------------------------
async def actor_upsert(db: Database, pid: str, kind: str = "player",
                       nickname: Optional[str] = None,
                       x: float = 0.0, y: float = 0.0) -> None:
    rows = await Actor.find(db, where={"pid": pid})
    if rows:
        await Actor.update(db, where={"pid": pid}, fields={"x": x, "y": y})
    else:
        await Actor.insert(db, pid=pid, kind=kind, nickname=nickname, x=x, y=y)

async def actor_move(db: Database, pid: str, x: float, y: float) -> None:
    await Actor.update(db, where={"pid": pid}, fields={"x": x, "y": y})

async def actor_damage(db: Database, pid: str, delta_hp: float) -> float:
    row = await Actor.find(db, where={"pid": pid})
    if not row: 
        return 0.0
    row = row[0]
    hp = max(0.0, min(1.0, float(row["health"]) + float(delta_hp)))
    await Actor.update(db, where={"pid": pid}, fields={"health": hp})
    return hp

async def actor_set_morality(db: Database, pid: str, val: float) -> float:
    val = max(-1.0, min(1.0, float(val)))
    await Actor.update(db, where={"pid": pid}, fields={"morality": val})
    return val

# ---- GM: POWERS helpers ----------------------------------------------
async def power_set(db: Database, pid: str, power: str,
                    mastery_mult: float = 1.0, time_s: Optional[float] = None) -> None:
    rows = await ActorPower.find(db, where={"pid": pid, "power": power})
    fields = {"value_mult": mastery_mult, "time_s": time_s}
    if rows:
        await ActorPower.update(db, where={"id": rows[0]["id"]}, fields=fields)
    else:
        await ActorPower.insert(db, pid=pid, power=power, **fields)

# ---- GM: INVENTORY helpers -------------------------------------------
async def inv_get_qty(db: Database, pid: str, item: str) -> int:
    row = await Inventory.find(db, where={"pid": pid, "item": item})
    return int(row[0]["qty"]) if row else 0

async def inv_has(db: Database, pid: str, need: Dict[str, int]) -> bool:
    if not need:
        return True
    for it, q in need.items():
        if int(q) <= 0:
            continue
        have = await inv_get_qty(db, pid, it)
        if have < int(q):
            return False
    return True

async def inv_add(db: Database, pid: str, item: str, delta: int) -> None:
    row = await Inventory.find(db, where={"pid": pid, "item": item})
    if row:
        new_q = max(0, int(row[0]["qty"]) + int(delta))
        await Inventory.update(db, where={"id": row[0]["id"]}, fields={"qty": new_q})
    else:
        await Inventory.insert(db, pid=pid, item=item, qty=max(0, int(delta)))

async def inv_bulk_add(db: Database, pid: str, delta: Dict[str, int]) -> None:
    for it, q in (delta or {}).items():
        await inv_add(db, pid, it, int(q))


# ======================================================================
# ========== PLAYER-OWNED (PERSONAL PERSPECTIVE STATE & OPS) ===========
# ======================================================================
# These tables reflect what ONE observer thinks/feels/remembers.
# They can live in a per-player DB/file. The GM does not need to be
# authoritative for them (though it may facilitate syncing).

class Reputation(Model):
    __tablename__ = "reputations"
    id      = Field("INTEGER", primary_key=True)
    src_pid = Field("TEXT", nullable=False)                      # who holds the opinion
    dst_pid = Field("TEXT", nullable=False)                      # about whom
    score   = Field("REAL", default=0.0, check="score >= -1.0 AND score <= 1.0")
    # optional: context = Field("TEXT", default="global")

class Emotion(Model):
    __tablename__ = "emotions"
    id      = Field("INTEGER", primary_key=True)
    src_pid = Field("TEXT", nullable=False)                      # feeler
    dst_pid = Field("TEXT", nullable=False)                      # target
    label   = Field("TEXT", nullable=False)                      # 'anger', 'friendliness', ...
    value   = Field("REAL", default=0.0, check="value >= 0.0 AND value <= 1.0")
    # optional: context = Field("TEXT", default="global")

class MemoryKV(Model):
    __tablename__ = "memory_kv"
    id        = Field("INTEGER", primary_key=True)
    owner_pid = Field("TEXT", nullable=False)
    k         = Field("TEXT", nullable=False)
    v         = Field("TEXT", default="")

# ---- PLAYER: social helpers ------------------------------------------
async def rep_bump(db: Database, src: str, dst: str, delta: float) -> None:
    row = await Reputation.find(db, where={"src_pid": src, "dst_pid": dst})
    base = float(row[0]["score"]) if row else 0.0
    val = max(-1.0, min(1.0, base + float(delta)))
    if row:
        await Reputation.update(db, where={"id": row[0]["id"]}, fields={"score": val})
    else:
        await Reputation.insert(db, src_pid=src, dst_pid=dst, score=val)

async def emotion_bump(db: Database, src: str, dst: str, label: str, delta: float) -> None:
    row = await Emotion.find(db, where={"src_pid": src, "dst_pid": dst, "label": label})
    base = float(row[0]["value"]) if row else 0.0
    val = max(0.0, min(1.0, base + float(delta)))
    if row:
        await Emotion.update(db, where={"id": row[0]["id"]}, fields={"value": val})
    else:
        await Emotion.insert(db, src_pid=src, dst_pid=dst, label=label, value=val)


# ======================================================================
# ========================== SCHEMA HELPERS =============================
# ======================================================================

async def create_all_gm(db: Union[Database, str]) -> None:
    """
    Create tables and indexes for the **GM-owned** authoritative database.
    Includes: Actor, ActorPower, Inventory, CmdLog.
    """
    # Tables
    for m in (Actor, ActorPower, Inventory, CmdLog):
        await m.create_table(db)

    # Unique / perf indexes
    await Inventory.create_index(db, name="uq_inventory_pid_item", columns=["pid", "item"], unique=True)
    await ActorPower.create_index(db, name="uq_actor_power", columns=["pid", "power"], unique=True)

    # Common read paths
    await Inventory.create_index(db, name="ix_inventory_pid", columns=["pid"])


async def create_all_player(db: Union[Database, str]) -> None:
    """
    Create tables and indexes for the **Player-owned** personal database.
    Includes: Reputation, Emotion, MemoryKV.
    """
    # Tables
    for m in (Reputation, Emotion, MemoryKV):
        await m.create_table(db)

    # Unique / perf indexes
    await Reputation.create_index(db, name="uq_rep_src_dst", columns=["src_pid", "dst_pid"], unique=True)
    await Emotion.create_index(db, name="uq_emote_src_dst_label", columns=["src_pid", "dst_pid", "label"], unique=True)

    # Common read paths
    await Reputation.create_index(db, name="ix_rep_dst", columns=["dst_pid"])
    await Emotion.create_index(db, name="ix_emote_dst", columns=["dst_pid"])


# ======================================================================
# ========================== DB HELPERS ================================
# ======================================================================

async def world_state_snapshot(db: Database) -> dict:
    actors = await Actor.find(db)
    powers = await ActorPower.find(db)
    inv    = await Inventory.find(db)

    by_pid = {a["pid"]: {
        "pid": a["pid"], "kind": a["kind"], "nickname": a["nickname"],
        "pos": {"x": a["x"], "y": a["y"]},
        "health": a["health"], "morality": a["morality"],
        "powers": {}, "inventory": {}
    } for a in actors}

    for p in powers:
        by_pid.setdefault(p["pid"], {}).setdefault("powers", {})[p["power"]] = {
            "value_mult": p["value_mult"], "time_s": p["time_s"]
        }
    for r in inv:
        by_pid.setdefault(r["pid"], {}).setdefault("inventory", {})[r["item"]] = r["qty"]

    return {"actors": by_pid}


async def log_cmd(db: Database, src_pid: str, kind: str, payload: dict,
                  status: str, reason: Optional[str] = None) -> Optional[int]:
    return await CmdLog.insert(
        db,
        ts=time.time(), src_pid=src_pid, kind=kind,
        payload=json.dumps(payload, separators=(",", ":")),
        status=status, reason=reason
    )


def load_resources(resources_path: Path) -> dict:
    with resources_path.open("r", encoding="utf-8") as f:
        return json.load(f)

# --- Atomic recipe application (single transaction, race-safe) --------
async def apply_recipe(db: Database, pid: str, recipe: dict) -> Tuple[bool, str]:
    """
    recipe example:
      {
        "requires": {"wood": 2},
        "consumes": {"wood": 2},
        "produces": {"plank": 1}
      }
    Returns: (ok, "ok" | "missing_requirements" | "invalid_recipe" | "error")
    """
    def _to_posint_map(d: Dict[str, Any]) -> Optional[Dict[str, int]]:
        out: Dict[str, int] = {}
        for k, v in (d or {}).items():
            try:
                iv = int(v)
            except Exception:
                return None
            out[str(k)] = iv
        return out

    requires = _to_posint_map(recipe.get("requires", {}))
    consumes = _to_posint_map(recipe.get("consumes", {}))
    produces = _to_posint_map(recipe.get("produces", {}))

    if requires is None or consumes is None or produces is None:
        return False, "invalid_recipe"
    # negative quantities don’t make sense here
    if any(q < 0 for q in consumes.values()) or any(q < 0 for q in produces.values()):
        return False, "invalid_recipe"

    # Quick pre-check (non-atomic)
    if not await inv_has(db, pid, requires):
        return False, "missing_requirements"

    try:
        await db.execute("BEGIN IMMEDIATE")

        # Re-check under lock
        for it, need in requires.items():
            if need <= 0:
                continue
            row = await db.fetchone(
                "SELECT qty FROM inventories WHERE pid=? AND item=?",
                (pid, it)
            )
            have = int(row["qty"]) if row else 0
            if have < need:
                await db.execute("ROLLBACK")
                return False, "missing_requirements"

        # Apply consumes
        for it, q in consumes.items():
            if q <= 0:
                continue
            row = await db.fetchone(
                "SELECT id, qty FROM inventories WHERE pid=? AND item=?",
                (pid, it)
            )
            have = int(row["qty"]) if row else 0
            if have < q:
                await db.execute("ROLLBACK")
                return False, "missing_requirements"
            await db.execute(
                "UPDATE inventories SET qty=? WHERE id=?",
                (have - q, int(row["id"])) if row else (0, None)  # row should exist; guard anyway
            )

        # Apply produces
        for it, q in produces.items():
            if q <= 0:
                continue
            row = await db.fetchone(
                "SELECT id, qty FROM inventories WHERE pid=? AND item=?",
                (pid, it)
            )
            if row:
                await db.execute(
                    "UPDATE inventories SET qty=? WHERE id=?",
                    (int(row["qty"]) + q, int(row["id"]))
                )
            else:
                await db.execute(
                    "INSERT INTO inventories(pid, item, qty) VALUES(?,?,?)",
                    (pid, it, q)
                )

        await db.commit()
        return True, "ok"

    except Exception:
        try:
            await db.execute("ROLLBACK")
        except Exception:
            pass
        return False, "error"

async def ensure_actor_on_connect(db: Database, pid: str, resources: dict) -> dict:
    npcs     = resources.get("npcs") or {}
    defaults = resources.get("default_player") or {}
    is_npc   = pid in npcs

    await actor_upsert(db, pid=pid, kind=("npc" if is_npc else "player"))

    existing_inv = await Inventory.find(db, where={"pid": pid}, fields=["id"])
    if not existing_inv:
        if is_npc:
            seed_inv = (npcs.get(pid, {}) or {}).get("inventory") or {}
        else:
            base_inv = (defaults.get("inventory") or {}).copy()
            rand_inv = grant_random_starter(resources, pid)  # <- new
            # merge base + random
            for k, v in (rand_inv or {}).items():
                base_inv[k] = int(base_inv.get(k, 0)) + int(v)
            seed_inv = base_inv
        await inv_bulk_add(db, pid, seed_inv or {})

    existing_powers = await ActorPower.find(db, where={"pid": pid}, fields=["id"])
    if not existing_powers:
        if is_npc:
            seed_powers = (npcs.get(pid, {}) or {}).get("powers") or []
        else:
            seed_powers = defaults.get("powers") or []
        # accept both {"power": {...}} and bare {...}
        for pe in seed_powers:
            p = pe.get("power", pe)
            ptype = p.get("type")
            if not ptype:
                continue
            mastery = float(p.get("mastery", 1))
            await power_set(db, pid, ptype, mastery_mult=mastery)

    return {"pid": pid, "is_npc": is_npc}



# --- Atomic item transfer (single transaction, race-safe) -------------
async def transfer_item(db: Database, src: str, dst: str, item: str, qty: int) -> bool:
    """
    Moves `qty` of `item` from src -> dst atomically.
    Returns True on success, False otherwise.
    """
    if qty is None or int(qty) <= 0:
        return False
    qty = int(qty)

    try:
        await db.execute("BEGIN IMMEDIATE")

        # Check source balance inside the txn
        row = await db.fetchone(
            "SELECT id, qty FROM inventories WHERE pid=? AND item=?",
            (src, item)
        )
        have = int(row["qty"]) if row else 0
        if have < qty:
            await db.execute("ROLLBACK")
            return False

        # Debit source
        new_src = have - qty
        if row:
            await db.execute("UPDATE inventories SET qty=? WHERE id=?", (new_src, int(row["id"])))
        else:
            # Should not happen due to check above, guard anyway
            await db.execute(
                "INSERT INTO inventories(pid, item, qty) VALUES(?,?,?)",
                (src, item, 0)
            )

        # Credit destination
        row_dst = await db.fetchone(
            "SELECT id, qty FROM inventories WHERE pid=? AND item=?",
            (dst, item)
        )
        if row_dst:
            await db.execute("UPDATE inventories SET qty=? WHERE id=?", (int(row_dst["qty"]) + qty, int(row_dst["id"])))
        else:
            await db.execute(
                "INSERT INTO inventories(pid, item, qty) VALUES(?,?,?)",
                (dst, item, qty)
            )

        await db.commit()
        return True

    except Exception:
        try:
            await db.execute("ROLLBACK")
        except Exception:
            pass
        return False

async def sqlite_bootstrap(db: Database) -> None:
    # Foreign keys only if you later add FKs
    await db.execute("PRAGMA foreign_keys = ON")
    # WAL improves concurrency for async server
    await db.execute("PRAGMA journal_mode = WAL")
    await db.commit()


# ======================================================================
# ========================== HELPERS ===================================
# ======================================================================

def _prices_from_resources(resources: Dict[str, Any]) -> Dict[str, float]:
    return {k: float(v) for k, v in (resources.get("resources") or {}).items()}

def _unit_value(item: str, prices: Dict[str, float], override: Optional[float]) -> float:
    if override is not None:
        try:
            return float(override)
        except Exception:
            pass
    return float(prices.get(item, 1.0))

def _rng_for_pid(pid: str, version: str = "v1") -> random.Random:
    # Deterministic per pid+version, so reconnects don’t reshuffle.
    h = hashlib.sha256((version + "::" + str(pid)).encode("utf-8")).digest()
    return random.Random(h)

def grant_random_starter(resources: Dict[str, Any], pid: str) -> Dict[str, int]:
    """
    Returns a dict of random starter items for a NEW player (empty inventory).
    Uses resources['default_player']['random'] spec. Deterministic per PID.
    """
    dp = resources.get("default_player") or {}
    rnd_spec = dp.get("random") or {}
    pool = list(rnd_spec.get("pool") or [])
    if not pool:
        return {}

    prices = _prices_from_resources(resources)
    rng = _rng_for_pid(pid, version="starter_v1")

    rolls = rnd_spec.get("rolls") or {}
    rmin = int(rolls.get("min", 1))
    rmax = int(rolls.get("max", 1))
    num_rolls = rng.randint(max(0, rmin), max(0, rmax))

    budget = rnd_spec.get("budget") or {}
    bmin = float(budget.get("min", 0.0))
    bmax = float(budget.get("max", float("inf")))
    spent = 0.0

    exclusions = set(rnd_spec.get("exclusions") or [])
    # Build weights excluding excluded items
    candidates = []
    weights = []
    for entry in pool:
        it = str(entry.get("item", ""))
        if not it or it in exclusions:
            continue
        w = float(entry.get("weight", 1.0))
        if w <= 0:
            continue
        candidates.append(entry)
        weights.append(w)

    grants: Dict[str, int] = {}
    for _ in range(num_rolls):
        if not candidates:
            break
        entry = rng.choices(candidates, weights=weights, k=1)[0]
        it = str(entry["item"])
        qspec = entry.get("qty") or {}
        qmin = int(qspec.get("min", 1))
        qmax = int(qspec.get("max", 1))
        qty = max(0, rng.randint(qmin, qmax))
        if qty == 0:
            continue

        unit_val = _unit_value(it, prices, entry.get("value"))
        add_value = unit_val * qty

        # If adding would exceed hard max budget, try to reduce qty; else skip
        if spent + add_value > bmax:
            # reduce qty greedily
            if unit_val > 0:
                max_qty_allowed = int((bmax - spent) // unit_val)
                qty = min(qty, max_qty_allowed)
            if qty <= 0:
                continue
            add_value = unit_val * qty

        grants[it] = grants.get(it, 0) + qty
        spent += add_value

        if spent >= bmax:
            break

    # Soft-min budget isn’t enforced strictly; this keeps logic simple and predictable.
    return grants


def _recipe_by_id(resources: dict, rid: str) -> Optional[dict]:
    for r in resources.get("recipes") or []:
        if r.get("id") == rid:
            return r
    return None

async def handle_craft(db, pid: str, msg: dict, resources: dict) -> dict:
    rid   = (msg.get("recipe") or "").strip()
    times = msg.get("times", 1)
    if not rid:
        return {"type": "cmd_status", "kind": "craft", "from": pid,
                "status": "rejected", "reason": "missing_recipe"}

    rec = _recipe_by_id(resources, rid)
    if not rec:
        return {"type": "cmd_status", "kind": "craft", "from": pid,
                "status": "rejected", "reason": "unknown_recipe"}

    # Optional: check required power mastery (ptype/mastery vs ActorPower)
    p = rec.get("power") or {}
    ptype = p.get("type"); pmast = int(p.get("mastery", 1))
    if ptype:
        # read ActorPower for pid, verify value_mult >= pmast
        pass  # keep minimal; wire if you want gating

    # Build atomic recipe for db_models.apply_recipe shape
    inputs  = rec.get("inputs") or {}
    outputs = rec.get("outputs") or {}

    n = 0
    if times == "max":
        # Greedy: compute max crafts from current inventory
        # For minimal code, loop until fail
        while True:
            ok, why = await apply_recipe(db, pid, {
                "requires": inputs, "consumes": inputs, "produces": outputs
            })
            if not ok:
                break
            n += 1
    else:
        t = max(1, int(times))
        for _ in range(t):
            ok, why = await apply_recipe(db, pid, {
                "requires": inputs, "consumes": inputs, "produces": outputs
            })
            if not ok:
                return {"type": "cmd_status", "kind": "craft", "from": pid,
                        "status": "rejected", "reason": why, "effects": {"crafted": n}}
            n += 1

    return {"type": "cmd_status", "kind": "craft", "from": pid,
            "status": "matched", "effects": {"crafted": n, "recipe": rid}}
