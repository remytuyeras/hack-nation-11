# Installation

Use one or the other:
```bash
source build_sdk.sh reset && bash install_requirements.sh
```
or
```bash
bash build_sdk.sh reset && bash install_requirements.sh
source venv/bin/activate
```

# How to run

Make sure every terminal is its the `venv` environment

```bash
source venv/bin/activate
```

```bash
# Terminal 1 (server configured for MMO) make sure only 1 server run in local host
python server.py 

# Terminal 2 (game master)
python agents/agent_GameMaster/agent.py

# Terminal 3 (player 1)
python agents/agent_GamePlayer/agent.py --id alice

# Terminal 4 (player 2)
python agents/agent_GamePlayer/agent.py --id bob
```


# 5-minute demo

## 1) Movement + world + chat (both players)

**Both players:** use **WASD/arrow keys** to move.
**Either player:** press **H** → sends “hello” chat; see it appear in the **right CHAT panel** and (on Windows) as a bubble near the speaker.
**Expected visuals:** seeded grass scrolling under camera; your avatar image for yourself; circles for others; top-left HUD line shows `ID`, `players`, `coords`, `seed`.
**Prompt Structure/ Personality:** This prompt ensures greetings are context-aware, lively, and adapt to previous interactions.
{
  "name": "hello",
  "style": "friendly",
  "prompt": "You are a friendly traveler in a mystical RPG world. Someone greets you or starts a conversation. Respond naturally and with personality — be kind, witty, or curious depending on prior encounters.",
  "summary_instruction": "Summarize how greetings and interactions have evolved. Focus on familiarity and tone."
}


## 2) Targeting + HUD base fields

**On Player 1:**

* Press **G** → “target nearest”. HUD (bottom-left card) shows `tgt=<demo2>`.
* Press **M** repeatedly → cycle `mode: combat → trade → social → craft`. Watch HUD `[mode]` update immediately.
* Press **V** → toggle proximity filter on the cycling keys.

**Cycle targets:**

* Press **TAB** (next) and **`** (prev) to rotate targets. HUD `tgt` updates.

## 3) Weapons & defense + HUD + persistence (MemoryKV)

**On Player 1:**

* Press **1** to equip **knife** (if you have it); HUD `wpn=knife`.
* Press **4** to equip **plate_iron** (if owned); HUD `def=plate_iron`.
* Press **M** to set **mode = social** (we’ll need it).
  These choices are saved via `kv_set`.

> **Persistence check (later):** we’ll quit and re-open to see HUD restore.

**Prompt Structure/ Personality:** This prompt ensure adaptive, context-aware defensive behavior.
{
  "name": "defense",
  "style": "defense",
  "prompt": "You brace for defense in a fantasy RPG world. Describe your defensive move — whether it's a spell, shield, or quick reflex. Show awareness of your opponent and previous attacks if known.",
  "summary_instruction": "Summarize the player's defensive approach, their adaptability, and notable moves."
}


## 4) Emotions (async HUD enrichment) + reputation

**On Player 1 (mode=social, target=demo2):**

* Press **'** to bump **anger↑** for your target.
  HUD first shows base values; within a tick the `emo: anger=… fear=…` line fills in (async).
* Press **"** to **anger↓**; press **\** to **fear↑**.
* Press **+** / **-** to adjust **reputation** toward the target (GM logs + local mirror).

**Expected visuals:** bottom-left HUD gains an `emo:` line with live values; CHAT panel shows small toasts like `anger(demo2)=0.3`.

**Prompt Structure/ Personality:** Guarantees emotionally consistent, vivid reactions tied to game events.
{
  "name": "emotion",
  "style": "emotional",
  "prompt": "Express your current emotional state in this RPG world. Mention what caused this feeling — maybe a battle, weather, or another player's words. Be vivid but concise, using emotional realism.",
  "summary_instruction": "Summarize how the player's emotions have changed over time and what influenced them."
}


## 5) Skills carousel (learn/teach) + HUD

**On Player 1 (mode=social):**

* Press **]** / **[** to change **skill**; HUD `skill` and `(mX)` update.
* Press **=** / **;** to adjust **mastery**.
* Press **L** to **learn** from the target (anger gate can block).
* Press **K** to **teach** (requires small payment item; gate can block).
  **Expected:** HUD updates immediately; GM reply in logs and CHAT toasts; your local DB `Reputation` mirrors accepted `rep` results.

**Prompt Structure/ Personality:** Ensures strategic, reflective decision-making.
{
  "name": "thought",
  "style": "thoughtful",
  "prompt": "Think aloud about your next plan or move. Reflect on what worked or failed before. Show personality — cautious, bold, or cunning.",
  "summary_instruction": "Summarize the player's overall thinking style and strategic evolution."
}


## 6) Craft carousel + action + inventory effects

**On Player 1:**

* Press **M** until **mode=craft**.
* Press **.** / **,** to switch **recipes**; HUD shows `craft=<recipe> → <product>`.
* Press **N** to craft one; **/** to craft max.
  **Expected:** GM applies inventory deltas; you’ll see CHAT toasts and (depending on your world state snapshot) inventory reflected next tick.

## 7) Trade proposal + accept/cancel

**Prepare:** ensure **mode=trade**, **target=demo2**, and you’re close (≤ 220 px).

* **P1:** press **P** to propose a bread↔wood trade.
* **P2:** press **O** to accept (or **I** to cancel).
  **Expected:** CHAT shows `trade ✓` or reason; inventories change via GM effects.

## 8) Combat toggles + gating + commit

**Set P1:** **mode=combat**, ensure **weapon** equipped and target in range.

* Press **Q** to arm **attack** (or **E** to arm **counter** with equipped defense).
* Press **Space** to **commit**:

  * If **fear** (on target) ≥ threshold, attack is blocked with a toast “won’t attack (fear gate)”.
  * If allowed and item owned, GM processes `attack/counter` and logs effects.
    **Expected:** quick toasts; possible health/combat effects in logs.

**Prompt Structure/ Personality:** Produces strategic, context-sensitive combat actions reflecting prior interactions.
{
  "key": "a",
  "name": "attack",
  "style": "combat",
  "prompt": "Describe your attack in a fantasy RPG battle. Be strategic — adapt based on your enemy and previous fights. Keep the tone heroic, and avoid repeating the same action twice unless required.",
  "summary_instruction": "Summarize how this player tends to fight — their tactics, patterns, and signature style."
}


## 9) Persistence proof (close & restore)

* Quit both players (window close or Ctrl+C).
* Relaunch **only Player 1** with the same `--id demo1`.
  **Expected on launch:** `_restore_player_state()` reapplies last `mode`, `target` (if still present), current `skill/mastery`, last selected `craft recipe`, and any equipped `weapon/defense` you still own. The HUD shows these immediately, before any keypress.

---

# Key map (cheat sheet)

* **Move:** WASD / arrows
* **Chat hello:** **H**
* **Target:** **G** (nearest), **TAB** next, **`** prev, **V** toggle proximity filter
* **Mode:** **M** cycle (combat/trade/social/craft)
* **Emotions:** **'** anger↑, **"** anger↓, **\** fear↑
* **Reputation:** **+** up, **-** down
* **Skills:** **]** next, **[** prev, **=** mastery↑, **;** mastery↓, **L** learn, **K** teach
* **Craft:** **.** next, **,** prev, **N** craft one, **/** craft max
* **Weapons:** **1** knife, **2** pickaxe, **3** crystal shard
* **Defense:** **4** plate, **5** cloth, **6** amulet
* **Combat:** **Q** toggle attack, **E** toggle counter, **Space** commit

---

## Tips to make it pop

* Use **different avatars** so self/other drawing contrast is obvious.
* Keep the two players **near each other** so targeting, trade, and combat are in range.
* Watch the **bottom-left HUD**: it updates instantly on local changes, then enriches with **emotions** asynchronously when a target is set.
* Watch the **right CHAT panel** for toasts and GM status.

If you want, I can tailor a one-screen “demo script” overlay message sequence so a single operator can follow it line-by-line during a live run.
