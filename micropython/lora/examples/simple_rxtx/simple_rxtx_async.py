# MicroPython lora simple_rxtx example - asynchronous API version
# MIT license; Copyright (c) 2023 Angus Gratton
import uasyncio
from umachine import Pin, SPI
from lora import SX1262, AsyncModem


import micropython

micropython.alloc_emergency_exception_buf(256)


async def main_task():
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
        1,
        baudrate=2000_000,
        polarity=0,
        phase=0,
        sck=Pin(10),
        mosi=Pin(11),
        miso=Pin(12),
    )

    modem = SX1262(
        spi,
        cs=Pin(3, mode=Pin.OUT, value=1),
        busy=Pin(2, mode=Pin.IN),
        dio1=Pin(20, mode=Pin.IN),
        reset=Pin(15, mode=Pin.OUT),
        dio2_rf_sw=True,
        dio3_tcxo_millivolts=3300,
        lora_cfg=lora_cfg,
    )

    modem = AsyncModem(modem)

    await uasyncio.gather(
        uasyncio.create_task(send_coro(modem)),
        uasyncio.create_task(recv_coro(modem)),
    )


async def recv_coro(modem):
    while True:
        print("Receiving...")
        rx = await modem.recv(2000)
        if rx:
            print(f"Received: {repr(rx)}")
        else:
            print("Receive timeout!")


async def send_coro(modem):
    counter = 0
    while True:
        print("Sending...")
        await modem.send(f"Hello world from async MicroPython #{counter}".encode())
        print("Sent!")
        await uasyncio.sleep(5)
        counter += 1


if __name__ == "__main__":
    uasyncio.run(main_task())
