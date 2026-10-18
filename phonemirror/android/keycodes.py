"""Android KeyEvent codes for the key names used by the viewer."""

KEYCODES = {
    "home": 3,
    "back": 4,
    "up": 19,
    "down": 20,
    "left": 21,
    "right": 22,
    "volume_up": 24,
    "volume_down": 25,
    "power": 26,
    "tab": 61,
    "space": 62,
    "enter": 66,
    "backspace": 67,  # KEYCODE_DEL
    "menu": 82,
    "page_up": 92,
    "page_down": 93,
    "escape": 111,
    "delete": 112,  # KEYCODE_FORWARD_DEL
    "move_home": 122,
    "move_end": 123,
    "insert": 124,
    "recents": 187,  # KEYCODE_APP_SWITCH
}
KEYCODES.update({chr(ord("a") + i): 29 + i for i in range(26)})
KEYCODES.update({str(i): 7 + i for i in range(10)})

META_SHIFT_ON = 0x1 | 0x40   # META_SHIFT_ON | META_SHIFT_LEFT_ON
META_ALT_ON = 0x2 | 0x10     # META_ALT_ON | META_ALT_LEFT_ON
META_CTRL_ON = 0x1000 | 0x2000  # META_CTRL_ON | META_CTRL_LEFT_ON


def metastate(shift=False, alt=False, ctrl=False):
    return (META_SHIFT_ON if shift else 0) | (META_ALT_ON if alt else 0) | (META_CTRL_ON if ctrl else 0)
