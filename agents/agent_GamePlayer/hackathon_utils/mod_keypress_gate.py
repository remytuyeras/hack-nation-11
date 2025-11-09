from __future__ import annotations
from typing import Iterable, Set
from collections import defaultdict
import threading


# Thread-safe edge detector shared by UI thread (pygame) and Summoner client thread.
class _EdgeState:
    __slots__ = ("_now", "_last", "_lock", "_keymap")
    def __init__(self) -> None:
        self._now: Set[str]  = set()
        self._last: Set[str] = set()
        self._lock = threading.Lock()
        # Map human-friendly names → pygame key constants (ints). UI will fill it.
        self._keymap: dict[str, int] = {}

    def update_pressed(self, pressed_names: Iterable[str]) -> None:
        with self._lock:
            self._last, self._now = self._now, set(pressed_names)

    def edge_down(self, name: str) -> bool:
        with self._lock:
            return (name in self._now) and (name not in self._last)

    def set_keymap(self, mapping: dict[str, int]) -> None:
        # Optional: expose to users if they want to redefine names
        with self._lock:
            self._keymap = dict(mapping)

EDGE = _EdgeState()

_KEY_LATCH = defaultdict(bool)
_KEY_LATCH_LOCK = threading.Lock()

def latch_keypress(name: str) -> None:
    with _KEY_LATCH_LOCK:
        _KEY_LATCH[name] = True

def consume_latch(name: str) -> bool:
    with _KEY_LATCH_LOCK:
        v = _KEY_LATCH.get(name, False)
        if v:
            _KEY_LATCH[name] = False
        return v

def send_on_keypress(key_name: str, overlay_ttl_ms=1500):
    """
    Decorate an @client.send coroutine so it fires once per keypress edge.
    Assumes the UI loop latches edges via latch_keypress(name).
    """
    def outer(fn):
        async def wrapped():
            # Only the first poll after the edge fires; subsequent polls see False
            if not consume_latch(key_name):
                return None

            output = await fn()
            if isinstance(output, dict) and isinstance(output.get("overlay"), dict):
                output["overlay"].setdefault("ttl_ms", overlay_ttl_ms)
            return output
        wrapped.__name__ = fn.__name__
        wrapped.__doc__  = fn.__doc__
        return wrapped
    return outer

