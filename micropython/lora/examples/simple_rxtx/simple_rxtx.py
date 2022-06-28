# MicroPython lora simple_rxtx example - synchronous API version
# MIT license; Copyright (c) 2023 Angus Gratton
import utime
from umachine import Pin, SPI
from lora import SX1276


def main():
    # Initializing the modem.
    #
    # TODO: Currently these are some settings I was using for testing, probably
    # to replace with a comment and an exception saying "put modem
    # init code here!"

    lora_cfg = {
        "freq_khz": 916000,
        "sf": 8,
        "bw": "500",  # kHz
        "coding_rate": 8,
        "preamble_len": 12,
        "output_power": 0,  # dBm
    }

    spi = SPI(
        0,
        baudrate=2_000_000,
        polarity=0,
        phase=0,
        sck=Pin(6),
        mosi=Pin(7),
        miso=Pin(4),
    )
    cs = Pin(9, mode=Pin.OUT, value=1)

    modem = SX1276(
        spi,
        cs,
        dio0=Pin(10, mode=Pin.PULL_UP),
        dio1=Pin(11, mode=Pin.PULL_UP),
        reset=Pin(13, mode=Pin.OPEN_DRAIN),
        lora_cfg=lora_cfg,
    )

    counter = 0
    while True:
        print("Transmitting...")
        modem.transmit(f"Hello world from MicroPython #{counter}".encode())

        print("Receiving...")
        rx = modem.receive(timeout_ms=5000)
        if rx:
            print(f"Received: {repr(rx)}")
        else:
            print("Timeout!")
        utime.sleep(2)
        counter += 1


if __name__ == "__main__":
    main()
