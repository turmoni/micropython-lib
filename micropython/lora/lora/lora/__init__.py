# MicroPython lora module
# MIT license; Copyright (c) 2023 Angus Gratton

from .modem import RxPacket  # noqa: F401

ok = False  # Flag if at least one modem driver package is installed

# Optional lora "sub-packages"

try:
    from .async_modem import AsyncModem  # noqa: F401
except ImportError as e:
    print(str(e))


try:
    from .sx126x import SX1261, SX1262  # noqa: F401

    ok = True
except ImportError as e:
    # This is a bit ugly and couples to the ImportError string, but it gives better errors if there
    # is something else wrong in the module (for example, a missing Python feature on this board.)
    if "no module named 'lora." not in str(e):
        raise

try:
    from .sx127x import SX127x

    SX1276 = SX1277 = SX1278 = SX1279 = SX127x

    # Discourage instantiating SX127x directly, in case some day these are different classes
    # (see comment under 'class SX127x' in sx127x.py)
    del SX127x
    ok = True
except ImportError as e:
    # See comment above
    if "no module named 'lora." not in str(e):
        raise


if not ok:
    # The base 'lora' package has been installed but no modem driver. i.e. need
    # to install a package like lora-sx127x or lora-sx126x, as per
    # documentation.
    raise ImportError("No LoRa modem driver installed")

del ok
