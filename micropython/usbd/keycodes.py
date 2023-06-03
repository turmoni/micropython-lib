_KEYPAD_KEYS = [
    "<NumLock>", "/", "*", "-", "+", "<Enter>", "1", "2", "3", "4", "5", "6",
    "7", "8", "9", "0", "."
]

KEYPAD_KEYCODES_TO_KEYS = {k + 0x53: v for k, v in enumerate(_KEYPAD_KEYS)}
KEYPAD_KEYS_TO_KEYCODES = {v: k for k, v in KEYPAD_KEYCODES_TO_KEYS.items()}
