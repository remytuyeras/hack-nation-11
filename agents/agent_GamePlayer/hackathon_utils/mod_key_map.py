import pygame
from typing import Dict, Optional

def default_keymap() -> Dict[str, int]:
    K = pygame

    def _get(name: str) -> Optional[int]:
        return getattr(K, name, None)

    def _add(d: Dict[str, int], name: str, keyconst: Optional[int]):
        if keyconst is not None:
            d[name] = keyconst

    m: Dict[str, int] = {}

    # Letters (lowercase names; uppercase aliases)
    for c in range(ord('a'), ord('z') + 1):
        ch = chr(c)
        kc = _get(f"K_{ch}")
        _add(m, ch, kc)
        _add(m, ch.upper(), kc)

    # Digits (top row)
    for d in range(0, 10):
        kc = _get(f"K_{d}")
        _add(m, str(d), kc)

    # Arrows & navigation
    _add(m, "left",       _get("K_LEFT"))
    _add(m, "right",      _get("K_RIGHT"))
    _add(m, "up",         _get("K_UP"))
    _add(m, "down",       _get("K_DOWN"))
    _add(m, "home",       _get("K_HOME"))
    _add(m, "end",        _get("K_END"))
    _add(m, "pageup",     _get("K_PAGEUP"))
    _add(m, "pagedown",   _get("K_PAGEDOWN"))
    _add(m, "insert",     _get("K_INSERT"))
    _add(m, "delete",     _get("K_DELETE"))
    _add(m, "backspace",  _get("K_BACKSPACE"))

    # Whitespace & control
    _add(m, "space",      _get("K_SPACE"))
    _add(m, " ",          _get("K_SPACE"))       # literal space alias
    _add(m, "enter",      _get("K_RETURN"))
    _add(m, "return",     _get("K_RETURN"))
    _add(m, "tab",        _get("K_TAB"))
    _add(m, "esc",        _get("K_ESCAPE"))
    _add(m, "escape",     _get("K_ESCAPE"))
    _add(m, "capslock",   _get("K_CAPSLOCK"))
    _add(m, "scrolllock", _get("K_SCROLLLOCK"))
    _add(m, "numlock",    _get("K_NUMLOCK"))
    _add(m, "printscreen",_get("K_PRINTSCREEN"))
    _add(m, "prtsc",      _get("K_PRINTSCREEN"))
    _add(m, "pause",      _get("K_PAUSE"))
    _add(m, "break",      _get("K_PAUSE"))

    # Modifiers (left/right + convenient aliases)
    _add(m, "lshift",     _get("K_LSHIFT"))
    _add(m, "rshift",     _get("K_RSHIFT"))
    _add(m, "shift",      _get("K_LSHIFT"))  # default to left
    _add(m, "lctrl",      _get("K_LCTRL"))
    _add(m, "rctrl",      _get("K_RCTRL"))
    _add(m, "ctrl",       _get("K_LCTRL"))
    _add(m, "lalt",       _get("K_LALT"))
    _add(m, "ralt",       _get("K_RALT"))
    _add(m, "alt",        _get("K_LALT"))
    # Meta/Super/GUI varies by platform; add all aliases to whatever exists
    gui_left  = _get("K_LGUI")  or _get("K_LMETA")  or _get("K_LSUPER")
    gui_right = _get("K_RGUI")  or _get("K_RMETA")  or _get("K_RSUPER")
    _add(m, "lgui",  gui_left);   _add(m, "rgui",  gui_right)
    _add(m, "lmeta", gui_left);   _add(m, "rmeta", gui_right)
    _add(m, "lsuper",gui_left);   _add(m, "rsuper",gui_right)
    _add(m, "meta",  gui_left or gui_right)
    _add(m, "super", gui_left or gui_right)
    _add(m, "gui",   gui_left or gui_right)
    _add(m, "menu",  _get("K_MENU"))  # application/menu key where available

    # Function keys (F1..F24 if present)
    for i in range(1, 25):
        kc = _get(f"K_F{i}")
        if kc is not None:
            _add(m, f"f{i}", kc)

    # Numpad (aliases: "kp#" and "numpad_#")
    for d in range(0, 10):
        kc = _get(f"K_KP{d}")
        if kc is not None:
            _add(m, f"kp{d}", kc)
            _add(m, f"numpad_{d}", kc)
    _add(m, "kp_period",    _get("K_KP_PERIOD"))
    _add(m, "kp_dot",       _get("K_KP_PERIOD"))
    _add(m, "kp_divide",    _get("K_KP_DIVIDE"))
    _add(m, "kp_multiply",  _get("K_KP_MULTIPLY"))
    _add(m, "kp_minus",     _get("K_KP_MINUS"))
    _add(m, "kp_plus",      _get("K_KP_PLUS"))
    _add(m, "kp_enter",     _get("K_KP_ENTER"))
    _add(m, "kp_equals",    _get("K_KP_EQUALS"))

    # Punctuation / symbols (top-row and US-ANSI style; maps both base and shifted forms)
    _add(m, "-",   _get("K_MINUS"));       _add(m, "_",   _get("K_MINUS"))
    _add(m, "=",   _get("K_EQUALS"));      _add(m, "+",   _get("K_EQUALS"))
    _add(m, "[",   _get("K_LEFTBRACKET")); _add(m, "{",   _get("K_LEFTBRACKET"))
    _add(m, "]",   _get("K_RIGHTBRACKET"));_add(m, "}",   _get("K_RIGHTBRACKET"))
    _add(m, "\\",  _get("K_BACKSLASH"))    # backslash
    _add(m, ";",   _get("K_SEMICOLON"));   _add(m, ":",   _get("K_SEMICOLON"))
    _add(m, "'",   _get("K_QUOTE"));       _add(m, '"',   _get("K_QUOTE"))
    _add(m, ",",   _get("K_COMMA"));       _add(m, "<",   _get("K_COMMA"))
    _add(m, ".",   _get("K_PERIOD"));      _add(m, ">",   _get("K_PERIOD"))
    _add(m, "/",   _get("K_SLASH"));       _add(m, "?",   _get("K_SLASH"))
    _add(m, "`",   _get("K_BACKQUOTE"));   _add(m, "~",   _get("K_BACKQUOTE"))

    # Additional symbol constants (present on some builds/layouts)
    # Map literal characters if Pygame exposes dedicated constants.
    for name, ch in [
        ("K_EXCLAIM", "!"), ("K_QUOTEDBL", '"'), ("K_HASH", "#"),
        ("K_DOLLAR", "$"),  ("K_AMPERSAND", "&"), ("K_LEFTPAREN", "("),
        ("K_RIGHTPAREN", ")"), ("K_ASTERISK", "*"), ("K_COLON", ":"),
        ("K_LESS", "<"),   ("K_GREATER", ">"), ("K_QUESTION", "?"),
        ("K_AT", "@"),     ("K_CARET", "^"),   ("K_UNDERSCORE", "_"),
    ]:
        kc = _get(name)
        if kc is not None:
            _add(m, ch, kc)

    # Media keys (only if available on the platform)
    _add(m, "volumeup",     _get("K_VOLUMEUP"))
    _add(m, "volup",        _get("K_VOLUMEUP"))
    _add(m, "volumedown",   _get("K_VOLUMEDOWN"))
    _add(m, "voldown",      _get("K_VOLUMEDOWN"))
    _add(m, "mute",         _get("K_MUTE"))
    _add(m, "audioplay",    _get("K_AUDIOPLAY"))
    _add(m, "audiostop",    _get("K_AUDIOSTOP"))
    _add(m, "audioprev",    _get("K_AUDIOPREV"))
    _add(m, "audionext",    _get("K_AUDIONEXT"))
    _add(m, "mediaselect",  _get("K_MEDIASELECT"))
    _add(m, "brightnessup", _get("K_BRIGHTNESSUP"))
    _add(m, "brightnessdown", _get("K_BRIGHTNESSDOWN"))
    _add(m, "power",        _get("K_POWER"))
    _add(m, "sleep",        _get("K_SLEEP"))
    _add(m, "wake",         _get("K_WAKE"))

    return m

