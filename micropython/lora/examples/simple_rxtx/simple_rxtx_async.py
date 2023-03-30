# MicroPython lora simple_rxtx example - asynchronous API version
# MIT license; Copyright (c) 2023 Angus Gratton
import uasyncio
from umachine import Pin, SPI
from lora import AsyncModem


import micropython

micropython.alloc_emergency_exception_buf(256)


def get_modem():
    # from lora import SX1276
    #
    # lora_cfg = {
    #    "freq_khz": 916000,
    #    "sf": 8,
    #    "bw": "500",  # kHz
    #    "coding_rate": 8,
    #    "preamble_len": 12,
    #    "output_power": 0,  # dBm
    # }
    #
    # return SX1276(
    #     spi=SPI(1, baudrate=2000_000, polarity=0, phase=0,
    #             miso=Pin(19), mosi=Pin(27), sck=Pin(5)),
    #     cs=Pin(18, mode=Pin.OUT, value=1),
    #     dio0=Pin(26, mode=Pin.IN),
    #     dio1=Pin(35, mode=Pin.IN),
    #     reset=Pin(14, mode=Pin.OPEN_DRAIN),
    #     lora_cfg=lora_cfg,
    # )
    raise NotImplementedError("Replace this function with one that returns a lora modem instance")


async def main_task():
    modem = AsyncModem(get_modem())
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
