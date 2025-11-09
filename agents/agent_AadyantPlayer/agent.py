# agent.py
import asyncio, threading, json, argparse, time
from typing import Any, Dict, Optional

from summoner.client import SummonerClient
from summoner.protocol.process import Direction

from hackathon_utils import send_on_keypress, H

from dotenv import load_dotenv
from openai import AsyncOpenAI
import os

# ===== Summoner client =====
PID: Optional[str] = None  # set in __main__
client = SummonerClient(name="GamePlayerAgent")

load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")
client_gpt = AsyncOpenAI(api_key=api_key)

# ===== GPT Utility =====
async def gpt_reply(prompt: str, style: str = "neutral") -> str:
    """Helper to get GPT response for any action."""

    response = await client_gpt.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": f"You are an RPG game character responding in {style} tone. Keep it short and lively."},
            {"role": "user", "content": prompt},
        ],
    )
    return response.choices[0].message.content.strip()

async def update_conversation_and_summary(key: str, reply: str):
    """Append reply to conversation history and update summary for this key."""
    CONVERSATIONS_PER_KEY[key].append(reply)
    SUMMARY_PER_KEY[key] = await summarize_conversation(key)

async def summarize_conversation(key: str) -> str:
    """Use GPT to summarize recent history for a given key."""
    history = "\n".join(CONVERSATIONS_PER_KEY[key][-10:])
    if not history:
        return ""

    prompt = (
        f"Summarize the player's recent {key.upper()} interactions briefly. "
        f"Capture mood, key moments, and continuity cues.\n\n"
        f"History:\n{history}\n\n"
        f"Give a short 2-3 sentence summary."
    )
    summary = await gpt_reply(prompt, "summary")
    return summary.strip()

# ===== Hooks =====
@client.hook(Direction.RECEIVE)
async def rx_normalize(payload: Any) -> Optional[dict]:
    if isinstance(payload, dict) and "content" in payload and isinstance(payload["content"], dict):
        return payload["content"]
    return payload

@client.hook(Direction.SEND)
async def tx_stamp_pid(payload: Any) -> Optional[dict]:
    if isinstance(payload, str):
        client.logger.warning("[GM] received str payload; ignoring")
        return None
    if isinstance(payload, dict) and "pid" not in payload:
        payload["pid"] = PID
    return payload


# ===== Default routes =====
@client.receive("world_state")
async def on_world(msg: dict) -> None:
    if not isinstance(msg, dict) or msg.get("type") != "world_state":
        return None

    now = time.time()

    # Copy world snapshot atomically
    with H.LOCK:
        SNAP = H.SNAP
        SNAP["ts"] = msg.get("ts")
        if "bounds" in msg:  SNAP["bounds"] = msg["bounds"]
        if "players" in msg: SNAP["players"] = msg["players"]
        if "overlays" in msg: SNAP["overlays"] = msg["overlays"]

    # Fold overlays into the (read-only) side chat.
    # Strong dedupe: drop any overlay whose seq <= last seen for that pid.
    overlays = msg.get("overlays") or []
    for overlay in overlays:
        if not isinstance(overlay, dict):
            continue
        pid = overlay.get("pid")
        chat_text = overlay.get("chat")
        if not pid or not isinstance(chat_text, str):
            continue
        text = chat_text.strip()
        if not text:
            continue
        
         # Sequencing: accept each seq once per PID (rebroadcasts during TTL are ignored)
        seq = overlay.get("seq")
        if isinstance(seq, int):
            # consumer key for the decorative side-chat fold
            if H.SEQ.seen("chat_fold", pid, seq):
                continue

        # Legacy time-based dedupe as a fallback if seq is missing
        key = (pid, text) if not isinstance(seq, int) else (pid, text, seq)
        last_ts = H.LAST_CHAT.get(key)
        if (last_ts is None) or (now - last_ts > H.CHAT_DEDUPE_SECS):
            H.LAST_CHAT[key] = now
            with H.CHAT_LOCK:
                H.CHAT_LOG.append((now, pid, text))
    return None


@client.send("directions")
async def tick() -> dict:
    await asyncio.sleep(0.2)  # 20 Hz
    with H.LOCK:
        keys = dict(H.INPUT)
    return {"type": "tick", "ts": time.time(), "keys": keys}


# ===== Hacking =====

# ===== Actions =====
# Each key will have its own conversation memory (mini-database)
CONVERSATIONS_PER_KEY = {
    "h": [],  # hello
    "e": [],  # emotion
    "a": [],  # attack
    "d": [],  # defend
    "t": [],  # thought
    "s": [],  # story
    "r": [],  # banter
}

# Latest GPT reply per key
MESSAGE_PER_KEY = {
    "h": None,
    "e": None,
    "a": None,
    "d": None,
    "t": None,
    "s": None,
    "r": None,
}

SUMMARY_PER_KEY = {
    "h": "",  # friendly greeting summary
    "e": "",  # emotional response summary
    "a": "",  # attack response summary
    "d": "",  # defend response summary
    "t": "",  # thought response summary
    "s": "",  # story/narrative summary
    "r": "",  # playful banter summary
}

# --Simple hello
@client.send("chat")
@send_on_keypress("h", overlay_ttl_ms=1200)
async def greet_response() -> dict:
    summary = SUMMARY_PER_KEY["h"]
    prompt = (
        "You are a player in a mystical RPG world. Someone greets you or starts a conversation. "
        "Respond naturally — with friendliness, curiosity, or recognition depending on context."
    )
    if summary:
        prompt += f"\n\nPreviously you greeted others like this: {summary}"

    reply = await gpt_reply(prompt, "friendly")
    MESSAGE_PER_KEY["h"] = reply
    await update_conversation_and_summary("h", reply)
    return {"type": "overlay", "overlay": {"chat": reply}}

switch = 0

@client.send("switch")
@send_on_keypress("l", overlay_ttl_ms=100)
async def send_chat_hello_immediate() -> dict:
    global switch
    switch = int(not(switch))
    return {"type": "overlay", "overlay": {"chat": f"I am in {switch}"}}

@client.receive("act_on_some_key")
async def on_world(msg: dict) -> None:
    #if switch == 0:
        #return
    
    if not isinstance(msg, dict) or msg.get("type") != "world_state":
        return None

    some_key = "chat"
    overlays = msg.get("overlays") or []
    for overlay in overlays:
        if not isinstance(overlay, dict):
            continue

        pid = overlay.get("pid")
        if not pid:
            continue

        seq = overlay.get("seq")
        if isinstance(seq, int):
            # independent consumer key for your custom logic
            if H.SEQ.seen("act_on_some_key", pid, seq):
                continue

        key_info = overlay.get(some_key)

        response = await gpt_reply(f"Player {pid} said: {key_info}. Respond naturally.", "friendly")
        MESSAGE_PER_KEY["j"] = response

        print(pid, key_info)
        print(f"GPT → {pid}: {response}")

    return None

@client.send("gpt_response")
@send_on_keypress("j")
async def send_gpt():
  print (MESSAGE_PER_KEY)
  return {"type": "overlay", "overlay": {"chat": MESSAGE_PER_KEY["j"]}}

# -- Emotional response
@client.send("chat")
@send_on_keypress("e", overlay_ttl_ms=1200)
async def emotional_response() -> dict:
    summary = SUMMARY_PER_KEY["e"]
    prompt = (
        "Express your emotional state as a character living in an RPG world. "
        "Mention why you feel that way — perhaps due to weather, a battle, or an ally's action. "
        "Be vivid but concise, using emotional realism."
    )
    if summary:
        prompt += f"\n\nPreviously you felt: {summary}, so your reply should change based on past actions of user."

    reply = await gpt_reply(prompt, "emotional")
    MESSAGE_PER_KEY["e"] = reply
    await update_conversation_and_summary("e", reply)
    return {"type": "overlay", "overlay": {"chat": reply}}

# -- Attack action
@client.send("chat")
@send_on_keypress("a", overlay_ttl_ms=1200)
async def attack_action() -> dict:
    summary = SUMMARY_PER_KEY["a"]
    prompt = (
        "Describe your attack move in a fantasy RPG battle. "
        "Be strategic — consider past encounters, your current energy, and the enemy type. "
        "Keep the tone heroic, and avoid repeating the same action twice."
    )
    if summary:
        prompt += f"\n\nYou recall your recent combat style: {summary}"

    reply = await gpt_reply(prompt, "combat")
    MESSAGE_PER_KEY["a"] = reply
    await update_conversation_and_summary("a", reply)
    return {"type": "overlay", "overlay": {"chat": reply}}

# -- Defend action
@client.send("chat")
@send_on_keypress("d", overlay_ttl_ms=1200)
async def defense_action() -> dict:
    summary = SUMMARY_PER_KEY["d"]
    prompt = (
        "You brace for defense in a fantasy RPG world. "
        "Describe your defensive move — whether it's a spell, shield, or quick reflex. "
        "Show awareness of your opponent and previous attacks if known."
    )
    if summary:
        prompt += f"\n\nYour earlier defensive strategies were: {summary}"

    reply = await gpt_reply(prompt, "defense")
    MESSAGE_PER_KEY["d"] = reply
    await update_conversation_and_summary("d", reply)
    return {"type": "overlay", "overlay": {"chat": reply}}

# -- Thought / introspection
@client.send("chat")
@send_on_keypress("t", overlay_ttl_ms=1200)
async def thought_action() -> dict:
    summary = SUMMARY_PER_KEY["t"]
    prompt = (
        "Think aloud about your next strategic move or plan. "
        "Reflect briefly on your previous decisions and their outcomes. "
        "Convey personality — whether you're cautious, bold, or cunning."
    )
    if summary:
        prompt += f"\n\nPreviously, your strategy thoughts were summarized as: {summary}"

    reply = await gpt_reply(prompt, "thoughtful")
    MESSAGE_PER_KEY["t"] = reply
    await update_conversation_and_summary("t", reply)
    return {"type": "overlay", "overlay": {"chat": reply}}

# -- World / story expansion
@client.send("chat")
@send_on_keypress("s", overlay_ttl_ms=1200)
async def story_expand() -> dict:
    summary = SUMMARY_PER_KEY["s"]
    prompt = (
        "Narrate a short piece of your ongoing story. "
        "Add flavor, continuity, and imagination — perhaps a discovery, an ally’s reaction, or a plot twist. "
        "Ensure it connects to prior narrative events."
    )
    if summary:
        prompt += f"\n\nYour last story summary: {summary}"

    reply = await gpt_reply(prompt, "story")
    MESSAGE_PER_KEY["s"] = reply
    await update_conversation_and_summary("s", reply)
    return {"type": "overlay", "overlay": {"chat": reply}}

# -- Random playful banter
@client.send("chat")
@send_on_keypress("r", overlay_ttl_ms=1200)
async def random_banter() -> dict:
    summary = SUMMARY_PER_KEY["r"]
    prompt = (
        "Say something spontaneous or witty as a character in an RPG world. "
        "It could be a joke, a small talk remark, or an observation about your surroundings."
    )
    if summary:
        prompt += f"\n\nYour recent banter style: {summary}"

    reply = await gpt_reply(prompt, "casual")
    MESSAGE_PER_KEY["r"] = reply
    await update_conversation_and_summary("r", reply)
    return {"type": "overlay", "overlay": {"chat": reply}}

# ===== Summoner runner (background thread) =====
def run_client(host: Optional[str], port: Optional[int], config_path: Optional[str], config_dict: Dict[str, Any]):
    # Avoid installing signal handlers in a non-main thread
    if hasattr(client, "set_termination_signals"):
        client.set_termination_signals = lambda *a, **k: None
    asyncio.set_event_loop(asyncio.new_event_loop())

    effective_cfg = dict(config_dict)
    hp = dict(effective_cfg.get("hyper_parameters", {}))
    effective_cfg["hyper_parameters"] = hp

    client.run(
        host=host if host is not None else "38.42.214.245",
        port=port if port is not None else 8888,
        config_path=config_path,
        config_dict=effective_cfg if config_path is None else None,
    )


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

    # Seed
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
        H.ui_loop(args.avatar, world_seed)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        H.RUNNING = False
