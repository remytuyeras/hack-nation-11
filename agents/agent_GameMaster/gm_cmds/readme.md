# Game Master Commands README

This document explains how to use the structured command API handled by `gm_cmds.py`. It covers the envelope, each command type, expected responses, effects, range and timing rules, and integration notes.

The Game Master does not parse text. Players send a JSON dictionary that represents a command. The Game Master validates, applies rules, and returns a `cmd_status` message with the result.

---

## Overview

### Message envelope sent by a Player

```
{
  "type": "overlay",
  "seq": 123,                     // per-player monotonic sequence for replay protection
  "overlay": {
    "cmd": { ... }                // a single structured command
  }
}
```

The Game Master should register its receive handler with replay protection:

```python
@agent.keyed_receive("overlay", key_by="pid", seq_by="seq")
async def on_overlay(msg): ...
```

### Typical response from Game Master

```
{
  "type": "cmd_status",
  "status": "accepted | matched | rejected | error",
  "kind":   "<command-name>",
  "from":   "<pid>",
  "txid":   "t-abc",              // only for accepted offers
  "reason": "<short-code>",       // present when status is rejected or error
  "effects": { ... }              // present when status is matched
}
```

Status meanings:

* `accepted`: the request was queued and carries a `txid` that a counterparty can accept later.
* `matched`: the request was validated and applied. `effects` describe state deltas.
* `rejected`: the request was well-formed but failed rule checks.
* `error`: the request had a bad shape or triggered an exception.

Common effect keys:

* `effects.inventory` updates per pid
* `effects.health` deltas per pid
* `effects.skills` learned powers
* `effects.combat` diagnostic details for damage calculation

### Proximity, timing and identity

* Range: many bilateral actions require proximity. The GM checks `distance(a, b) ≤ PROXIMITY_R`.
* TTL: pending offers expire after `OFFER_TTL_MS`. Expired offers are removed and reservations are released.
* Sequence: use the outer `seq` field to drop replays on the GM `@keyed_receive(..., seq_by="seq")`.

### Integration

At GM startup:

```python
from gm_cmds import load_combat_rules, set_players_reference
load_combat_rules("resources.json")              # reads combat section
set_players_reference(players)                   # give gm_cmds access to positions
```

In the overlay handler:

```python
from gm_cmds import process_structured_cmd

@agent.keyed_receive("overlay", key_by="pid", seq_by="seq")
async def on_overlay(msg: dict):
    pid = msg.get("pid")
    ov  = msg.get("overlay")
    if not pid or not isinstance(ov, dict): return
    cmd = ov.get("cmd")
    if not isinstance(cmd, dict): return
    status = process_structured_cmd(pid, cmd)
    await agent.send_now("gm/reply", status)
```

---

## Command: make

### Purpose

Craft items using recipes. This is where you map to `resources.json.recipes` and actor powers. The provided `gm_cmds.py` contains a placeholder implementation for demonstration.

### Payload

```
{ "kind": "make", "items": { "<output_item>": <qty>, ... } }
```

Example:

```
{ "kind": "make", "items": { "bread": 1 } }
```

### Response

* `matched` with `effects.inventory` for successful crafts
* `rejected` with `reason` when inputs are missing or recipe is unknown
* `error` when the payload is malformed

Placeholder behavior:

* Producing `bread` consumes `dough` 1 to 1
* Any other item is rejected as `unknown_recipe`

### Notes

To wire full crafting, resolve:

* Recipe existence and required inputs
* Actor power type and mastery threshold
* Time to craft if you simulate jobs
* Inventory updates and result emissions

---

## Command: rep

### Purpose

Adjust local reputation from the issuing Player toward a target.

### Payload

```
{ "kind": "rep", "target": "<pid or name>", "delta": <int> }
```

Example:

```
{ "kind": "rep", "target": "npc_smith_01", "delta": 1 }
```

### Response

* `matched` with echo of fields
* `error` if `target` is not a string or `delta` is not an integer

### Notes

Persist to your DB for real use. Consider clamping ranges and rate limiting.

---

## Command: trade

### Purpose

Create a bilateral trade offer between two parties with inventory reservation on the proposer side. The counterparty must later accept the offer using `accept`.

### Payload

```
{
  "kind": "trade",
  "to": "<counterparty_pid>",
  "give": { "<item>": <qty>, ... },      // proposer offers these items
  "want": { "<item>": <qty>, ... }       // proposer expects these items
}
```

Example:

```
{ "kind": "trade", "to": "alice", "give": { "wood": 2 }, "want": { "rock": 1 } }
```

### Response

* `accepted` with a `txid` when the offer is posted and inventory reserved
* `rejected` if the proposer lacks inventory or reservation fails
* `error` on malformed payload

### Semantics

* The proposer’s `give` is reserved immediately
* Offer expires after TTL
* Trade commits only when the counterparty calls `accept` with the `txid`

---

## Command: accept

### Purpose

Accept an offer created by `trade`, `learn`, or `teach`.

### Payload

```
{ "kind": "accept", "txid": "<txid>" }
```

### Response

* For trade:

  * `matched` transfers items in both directions and releases the reservation
  * `rejected` if not the counterparty, out of range, expired, or insufficient inventory on the accepting side
* For learn or teach:

  * `matched` transfers payment and applies the skill
* `error` for unknown or malformed `txid`

---

## Command: cancel

### Purpose

Cancel a posted offer. Only the proposer can cancel.

### Payload

```
{ "kind": "cancel", "txid": "<txid>" }
```

### Response

* `matched` on successful cancel, reservation released
* `rejected` if requester is not the proposer
* `error` if `txid` is unknown

---

## Command: attack

### Purpose

Apply very simple combat using opposition tags defined in `resources.json.combat`. Damage is a scalar product of a weapon base value and a multiplier determined by the attacker’s tag against the defender’s active defense tag.

### Inputs from configuration

`resources.json` must contain a `combat` section with:

* `requires.attack_power` and `requires.defense_power` booleans
* `base_damage` default
* `items` dictionary with per-item `attack` or `defense` tags and optional `damage`
* `opposition` matrix of multipliers by attack tag against defense tag

Example fragment:

```json
"combat": {
  "requires": { "attack_power": true, "defense_power": true },
  "base_damage": 1,
  "items": {
    "knife":        { "attack": "slash",  "damage": 4 },
    "pickaxe":      { "attack": "blunt",  "damage": 3 },
    "crystal_shard":{ "attack": "arcane", "damage": 2 },
    "plate_iron":   { "defense": "tough" },
    "cloth":        { "defense": "soft" },
    "amulet_minor": { "defense": "ward_arcane" }
  },
  "opposition": {
    "slash":  { "vs": { "tough": 1.10, "soft": 1.30, "ward_arcane": 1.00, "none": 1.00 } },
    "blunt":  { "vs": { "tough": 0.90, "soft": 1.20, "ward_arcane": 1.00, "none": 1.00 } },
    "arcane": { "vs": { "ward_arcane": 0.50, "tough": 1.00, "soft": 1.00, "none": 1.00 } }
  }
}
```

### Payload

```
{ "kind": "attack", "target": "<pid>", "with": "<item>" }
```

Example:

```
{ "kind": "attack", "target": "bandit_3", "with": "knife" }
```

### Response

* `matched` with `effects.health[target] = -damage` and `effects.combat` diagnostic
* `rejected` if not in range or weapon not allowed by configuration
* `error` for malformed payload

### Semantics

Steps:

1. Validate range.
2. If `requires.attack_power` is true then the weapon item must have an `attack` tag.
3. Defender may have set a defense item via `counter`. Otherwise defense tag is `none`.
4. Compute `damage = base_damage(weapon) × opposition[attack_tag].vs[defense_tag]`.
5. Round to integer and apply a negative delta to the defender’s health.

---

## Command: counter

### Purpose

Arm a short lived defense window for the caller using a chosen item. During this window the defense tag of that item is used for opposition in damage calculation.

### Payload

```
{ "kind": "counter", "target": "<pid>", "with": "<item>" }
```

Example:

```
{ "kind": "counter", "target": "bandit_3", "with": "plate_iron" }
```

### Response

* `accepted` if the item is valid for defense under configuration
* `rejected` if `requires.defense_power` is true and the item does not declare a defense tag
* `error` on malformed payload

### Semantics

* Defense window duration is 1000 ms by default
* Only affects the opposition tag used by `attack`
* There is no durability or blocking math in the simple model

---

## Command: learn

### Purpose

Propose to learn a power from a teacher in exchange for payment. Payment is reserved on the learner until acceptance.

### Payload

```
{
  "kind": "learn",
  "to": "<teacher_pid>",
  "power": { "type": "<power_name>", "mastery": <int> },
  "pay": { "<item>": <qty>, ... }
}
```

Example:

```
{
  "kind": "learn",
  "to": "npc_smith_01",
  "power": { "type": "hammer", "mastery": 2 },
  "pay": { "ingot_iron": 1 }
}
```

### Response

* `accepted` with `txid` if payment was reserved
* `rejected` if payment is insufficient
* `error` on malformed payload

### Acceptance

The teacher or learner calls `accept` with `txid`. On success:

* Payment transfers to the counterparty
* `effects.skills[learner][power.type] = mastery`

### Notes

Add teacher mastery checks and persistence when you integrate with your DB.

---

## Command: teach

### Purpose

Propose to teach a power to a learner in exchange for payment. Payment is reserved on the learner.

### Payload

```
{
  "kind": "teach",
  "to": "<learner_pid>",
  "power": { "type": "<power_name>", "mastery": <int> },
  "pay": { "<item>": <qty>, ... }
}
```

Example:

```
{
  "kind": "teach",
  "to": "alice",
  "power": { "type": "brew", "mastery": 1 },
  "pay": { "berries": 2 }
}
```

### Response and acceptance

Same as `learn`, but roles are swapped internally. On `accept`, payment moves to the teacher and the learner receives the skill.

---

## Error and rejection codes

Common `reason` strings:

* `unknown_kind` command not recognized
* `exception` internal exception
* `bad_make`, `bad_rep`, `bad_trade`, `bad_learn_teach`, `bad_attack`, `bad_counter` malformed payload
* `unknown_recipe` no recipe or disabled placeholder
* `insufficient_inputs` missing crafting inputs
* `insufficient_inventory` not enough items to reserve or commit
* `reserve_failed` inventory changed during reservation
* `expired` offer expired
* `not_owner` only proposer can cancel
* `not_counterparty` acceptor is not part of the offer
* `not_in_range` parties are too far apart
* `invalid_weapon` weapon fails attack requirement
* `invalid_defense` item fails defense requirement
* `bad_offer_type` `accept` on non supported type

---

## Player side quick examples

Send a trade:

```
{
  "type": "overlay",
  "seq": 41,
  "overlay": {
    "cmd": { "kind": "trade", "to": "bob", "give": { "wood": 2 }, "want": { "rock": 1 } }
  }
}
```

Counter then attack:

```
{ "type":"overlay", "seq": 50, "overlay": { "cmd": { "kind":"counter", "target":"bandit_3", "with":"plate_iron" } } }
{ "type":"overlay", "seq": 51, "overlay": { "cmd": { "kind":"attack",  "target":"bandit_3", "with":"knife" } } }
```

Learn a power:

```
{
  "type": "overlay",
  "seq": 60,
  "overlay": {
    "cmd": { "kind":"learn", "to":"npc_smith_01", "power": { "type":"hammer", "mastery": 2 }, "pay": { "ingot_iron": 1 } }
  }
}
```

Accept by counterparty:

```
{ "type":"overlay", "seq": 61, "overlay": { "cmd": { "kind":"accept", "txid":"t-1a" } } }
```

---

## Implementation notes

* `gm_cmds.py` keeps state in memory for clarity. Replace with your DB models later.
* Inventory reservations allow safe two-step commits for trades and skill exchanges.
* Combat is intentionally minimal. It only checks tags and uses a scalar opposition multiplier.
* Use `set_players_reference(players)` so range checks read positions from your live Player objects.
