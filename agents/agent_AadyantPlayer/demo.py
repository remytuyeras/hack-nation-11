# agent.py
import asyncio, threading, json, argparse, time, os
from typing import Any, Dict, Optional

from summoner.client import SummonerClient
from summoner.protocol.process import Direction

from hackathon_utils import send_on_keypress, H
from dotenv import load_dotenv
from openai import AsyncOpenAI

# ===== Summoner client =====
PID: Optional[str] = None  # set in __main__
client = SummonerClient(name="GamePlayerAgent")

load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")
client_gpt = AsyncOpenAI(api_key=api_key)

# ===== Load agent config =====
CONFIG_PATH = r"agents/agent_AadyantPlayer/config_agent_AadyantPlayer.json"  # raw string
AGENT_CONFIG = {}

if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        AGENT_CONFIG = json.load(f)
    print("[INFO] Loaded config successfully")
else:
    print(f"[WARN] Config file not found at {CONFIG_PATH}, using default")
    AGENT_CONFIG = H.DEFAULT_PLAYER_CONFIG  # fallback

# ===== GPT Utility =====
async def gpt_reply(prompt: str, style: str = "neutral") -> str:
    """Helper to get GPT response for any action."""
    response = await client_gpt.chat.completions.create(
        model=AGENT_CONFIG.get("settings", {}).get("model", "gpt-4o-mini"),
        messages=[
            {"role": "system", "content": f"You are an RPG game character responding in {style} tone. Keep it short and lively."},
            {"role": "user", "content": prompt},
        ],
    )
    return response.choices[0].message.content.strip()

# ===== Conversation memory =====
CONVERSATIONS_PER_KEY = {k: [] for k in AGENT_CONFIG["keys"].keys()}
MESSAGE_PER_KEY = {k: None for k in AGENT_CONFIG["keys"].keys()}
SUMMARY_PER_KEY = {k: "" for k in AGENT_CONFIG["keys"].keys()}

# ===== Helper functions =====
async def summarize_conversation(key: str) -> str:
    """Summarize past conversations for this key using GPT, per config instruction."""
    if not AGENT_CONFIG.get("misc", {}).get("auto_summarize", True):
        return SUMMARY_PER_KEY.get(key, "")
    
    history = "\n".join(CONVERSATIONS_PER_KEY[key][-50:])  # consider last 50 messages
    if not history:
        return ""

    instruction = AGENT_CONFIG["keys"][key].get("summary_instruction", "Summarize briefly.")
    prompt = (
        f"{instruction}\n\n"
        f"Conversation history:\n{history}\n\n"
        f"Provide a short summary in 2-3 sentences."
    )
    summary = await gpt_reply(prompt, "summary")
    return summary.strip()

async def generate_response(key: str) -> str:
    """Generate response for any key using config-driven prompt & style."""
    cfg = AGENT_CONFIG["keys"][key]
    template = cfg["prompt"]
    style = cfg["style"]
    summary = SUMMARY_PER_KEY.get(key, "")

    # Construct dynamic prompt
    prompt = template
    if summary:
        prompt += f"\n\nPreviously, your {cfg['name']} behavior was: {summary}"

    # Optionally add recent messages for context
    recent = "\n".join(CONVERSATIONS_PER_KEY[key][-5:])
    if recent:
        prompt += f"\n\nRecent conversation:\n{recent}"

    response = await gpt_reply(prompt, style)

    # Update memory
    CONVERSATIONS_PER_KEY[key].append(response)
    MESSAGE_PER_KEY[key] = response
    SUMMARY_PER_KEY[key] = await summarize_conversation(key)

    return response

# ===== Summoner Hooks =====
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

# ===== World state updates =====
@client.receive("world_state")
async def on_world(msg: dict) -> None:
    if not isinstance(msg, dict) or msg.get("type") != "world_state":
        return None

    now = time.time()

    # Copy world snapshot
    with H.LOCK:
        SNAP = H.SNAP
        SNAP["ts"] = msg.get("ts")
        if "bounds" in msg:  SNAP["bounds"] = msg["bounds"]
        if "players" in msg: SNAP["players"] = msg["players"]
        if "overlays" in msg: SNAP["overlays"] = msg["overlays"]

    # Fold overlays into side chat
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
    return None

@client.send("directions")
async def tick() -> dict:
    await asyncio.sleep(0.2)
    with H.LOCK:
        keys = dict(H.INPUT)
    return {"type": "tick", "ts": time.time(), "keys": keys}

# ===== Key action hooks =====
for key in AGENT_CONFIG["keys"]:
    if key == AGENT_CONFIG.get("misc", {}).get("speak_key", "j"):
        continue  # skip the speak key

    async def action(k=key) -> dict:
        reply = await generate_response(k)
        return {"type": "overlay", "overlay": {"chat": reply}}

    client.send("chat")(send_on_keypress(key, overlay_ttl_ms=1200)(action))


# ===== Speak key (j) =====
@client.send("gpt_response")
@send_on_keypress(AGENT_CONFIG.get("misc", {}).get("speak_key", "j"))
async def speak_gpt() -> dict:
    """Speak the last GPT-generated message stored in MESSAGE_PER_KEY."""
    key = AGENT_CONFIG.get("misc", {}).get("speak_key", "j")
    reply = MESSAGE_PER_KEY.get(key, "")
    print(MESSAGE_PER_KEY)
    return {"type": "overlay", "overlay": {"chat": reply}}

# ===== Summoner runner =====
def run_client(host: Optional[str], port: Optional[int], config_path: Optional[str], config_dict: Dict[str, Any]):
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

# ===== Main =====
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run a Summoner Player Agent with dynamic GPT summaries.")
    parser.add_argument("--config", dest="config_path", required=False, help="Path to JSON config for the client.")
    parser.add_argument("--host", type=str, default=None, help="Server host (overrides config).")
    parser.add_argument("--port", type=int, default=None, help="Server port (overrides config).")
    parser.add_argument("--avatar", type=str, default="wizard.png", help="Avatar image path.")
    parser.add_argument("--id", type=str, default=None, help="Persistent ID alias.")
    parser.add_argument("--seed", type=str, default="lava", help="Deterministic world seed.")
    args = parser.parse_args()

    PID = H.load_or_create_identity(args.id)
    client.logger.info(f"[Player] Using persistent ID: {PID}")
    client.name = f"Player_{PID}"
    world_seed = H.load_or_create_world_seed(args.seed)
    H.PID = PID

    # Load config if provided
    if args.config_path:
        try:
            with open(args.config, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            print(f"[Player] Failed to load config {args.config_path}: {e}")
            cfg = H.DEFAULT_PLAYER_CONFIG
    else:
        cfg = H.DEFAULT_PLAYER_CONFIG

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
