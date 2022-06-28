# LoRa Simple RX/TX Example

## Source Files

* `simple_rxtx.py` is a very simple implementation of a program to alternately
  receive and transmit LoRa packets.
* `simple_rxtx_async.py` is the same program implemented using async Python.

## Running the examples

One way to run this example interactively:

1. Install or "freeze in" the necessary lora modem driver package (`lora-sx127x`
   or `lora-sx126x`) and optionally the `lora-async` package if using the async
   examples (see main lora `README.md` in the above directory for details).
3. Edit the program you plan to run to set the modem constructors, pin
   assignments, etc. to match your board.
4. Change to this directory in a terminal.
5. Run `mpremote mount . run simple_rxtx.py` or `mpremote mount . run
   simple_rxtx_async.py` as applicable.

Consult the [mpremote
documentation](https://docs.micropython.org/en/latest/reference/mpremote.html)
for an explanation of these commands and the options needed to run two copies of
`mpremote` on different serial ports at the same time.
