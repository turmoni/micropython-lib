# Implement a keypad

from .hid import HIDInterface
from .keycodes import KEYPAD_KEYS_TO_KEYCODES
from micropython import const
_INTERFACE_PROTOCOL_KEYBOARD = const(0x01)

_KEYPAD_REPORT_DESC = bytes(
    [
        0x05, 0x01,  # Usage Page (Generic Desktop)
            0x09, 0x07,  # Usage (Keypad)
            0xA1, 0x01,  # Collection (Application)
                0x05, 0x07,  # Usage Page (Keypad)
                0x19, 0x00,  # Usage Minimum (00),
                0x29, 0xff,  # Usage Maximum (ff),
                0x15, 0x00,  # Logical Minimum (0),
                0x25, 0xff,  # Logical Maximum (ff),
                0x95, 0x01,  # Report Count (1),
                0x75, 0x08,  # Report Size (8),
                0x81, 0x00,  # Input (Data, Array, Absolute)
            0xC0,  # End Collection
    ]
)


class KeypadInterface(HIDInterface):
    # Very basic synchronous USB keypad HID interface

    def __init__(self):
        super().__init__(
            _KEYPAD_REPORT_DESC,
            protocol=_INTERFACE_PROTOCOL_KEYBOARD,
            interface_str="MicroPython Keypad!",
        )

    def send_report(self, key):
        super().send_report(KEYPAD_KEYS_TO_KEYCODES[key].to_bytes(1, "big"))
