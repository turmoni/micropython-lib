# MicroPython LoRa modem driver base class
# MIT license; Copyright (c) 2023 Angus Gratton
#
# LoRa is a registered trademark or service mark of Semtech Corporation or its affiliates.
import umachine
import utime
from micropython import const, schedule

_DEBUG = const(False)


def _clamp(v, vmin, vmax):
    # Small utility function to clamp a value 'v' between 'vmin' and 'vmax', inclusive.
    return min(max(vmin, v), vmax)


def _flag(value, condition):
    # Small utility function for returning a bit 'value' or not, based on a
    # boolean condition. Can help make expressions to build register values more
    # readable.
    #
    # Note that for value==1, can also rely on int(bool(x)) with one or both
    # conversions being implicit, as int(True)==1 and int(False)==0
    #
    # There is also (condition and value) but this is (IMO) confusing to read.
    return value if condition else 0


class ConfigError(ValueError):
    # Raise if there is an error in lora_cfg, saves some duplicated strings
    def __init__(self, field):
        super().__init__("Invalid lora_cfg {}".format(field))


class BaseModem:
    def __init__(self, ant_sw):
        self._ant_sw = ant_sw
        self._irq_callback = None

        # Common configuration settings that need to be tracked by all modem drivers
        # (Note that subclasses may set these to other values in their constructors, to match
        # the power-on-reset configuration of a particular modem.)
        #
        self._rf_freq_hz = 0  # Needs to be set via configure()
        self._sf = 7  # Spreading factor
        self._bw_hz = 125000  # Reset value
        self._coding_rate = 5
        self._crc_en = True  # use packet CRCs
        self._implicit_header = False  # implict vs explicit header mode
        self._preamble_len = 12
        self._coding_rate = 5

        # CRC error counter
        self.crc_errors = 0
        self.rx_crc_error = False

        # Current state of the modem

        # _rx holds radio recv state:
        #
        # - False if the radio is not receiving
        # - True if the radio is continuously receiving, or performing a single receive with
        #   no timeout.
        # - An int if there is a timeout set, in which case it is the is the receive deadline
        #   (as a utime.ticks_ms() timestamp).
        #
        # Note that self._rx can be not-False even when the radio hardware is not actually
        # receiving, if self._tx is True (send always pauses recv.)
        self._rx = False

        # _rx_continuous is True if the modem is in continuous receive mode
        # (this value is only valid when self._rx is also True).
        self._rx_continuous = False

        # This argument is stored from the parameter of the same name, as set in
        # the last call to start_recv()
        self._rx_length = None

        # _tx holds radio send state and is simpler, True means sending and
        # False means not sending.
        self._tx = False

        # timestamp (as utime.ticks_ms() result) of last IRQ event
        self._last_irq = None

        # values are:
        # - lora_cfg["invert_iq_rx"]
        # - lora_cfg["invert_iq_tx"]
        # - Current modem Invert setting
        self._invert_iq = [False, False, False]

    def standby(self):
        # Put the modem into standby. Can be used to cancel a continuous recv,
        # or cancel a send before it completes.
        #
        # Calls the private function which actually sets the mode to standby, and then
        # clears all the driver's state flags.
        #
        # Note this is also called before going to sleep(), to save on duplicated code.
        self._standby()
        self._rx = False
        self._tx = False
        self._last_irq = None
        if self._ant_sw:
            self._ant_sw.idle()
        self._radio_isr(None)  # "soft ISR"

    def _get_t_sym_us(self):
        # Return length of a symbol in microseconds
        return 1000_000 * (1 << self._sf) // self._bw_hz

    def _get_ldr_en(self):
        # Return true if Low Data Rate should be enabled
        #
        # The calculation in get_n_symbols_x4() relies on this being the same logic applied
        # in the modem configuration routines.
        return self._get_t_sym_us() >= 16000

    def _get_pa_ramp_val(self, lora_cfg, supported):
        # Return the PA ramp register index from the list of supported PA ramp
        # values. If the requested ramp time is supported by the modem, round up
        # to the next supported value.
        #
        # 'supported' is the list of supported ramp times, must be sorted
        # already.
        us = int(lora_cfg["pa_ramp_us"])

        # Find the index of the lowest supported ramp time that is longer or the
        # same value as 'us'
        for i, v in enumerate(supported):
            if v >= us:
                return i
        # The request ramp time is longer than all this modem's supported ramp times
        raise ConfigError("pa_ramp_us")

    def _symbol_offsets(self):
        # Called from get_time_on_air_us().
        #
        # This function provides a way to implement the different SF5 and SF6 in SX126x,
        # by returning two offsets: one for the overall number of symbols, and one for the
        # number of bits used to calculate the symbol length of the payload.
        return (0, 0)

    def get_n_symbols_x4(self, payload_len):
        # Get the number of symbols in a packet (Time-on-Air) for the current
        # configured modem settings and the provided payload length in bytes.
        #
        # Result is in units of "symbols times 4" as there is a fractional term
        # in the equation, and we want to limit ourselves to integer arithmetic.
        #
        # References are:
        # - SX1261/2 DS 6.1.4 "LoRa Time-on-Air"
        # - SX1276 DS 4.1.1 "Time on air"
        #
        # Note the two datasheets give the same information in different
        # ways. SX1261/62 DS is (IMO) clearer, so this function is based on that
        # formula. The result is equivalent to the datasheet value "Nsymbol",
        # times 4.
        #
        # Note also there are unit tests for this function in tests/test_time_on_air.py,
        # and that it's been optimised a bit for code size (with impact on readability)

        # Account for a minor difference between SX126x and SX127x: they have
        # incompatible SF 5 & 6 modes.
        #
        # In SX126x when using SF5 or SF6, we apply an offset of +2 symbols to
        # the overall preamble symbol count (s_o), and an offset of -8 to the
        # payload bit length (b_o).
        s_o, b_o = self._symbol_offsets()

        # calculate the bit length of the payload
        #
        # This is the part inside the max(...,0) in the datasheet
        bits = (
            # payload_bytes
            8 * payload_len
            # N_bit_crc
            + (16 if self._crc_en else 0)
            # (4 * SF)
            - (4 * self._sf)
            # +8 for most modes, except SF5/6 on SX126x where b_o == -8 so these two cancel out
            + 8
            + b_o
            # N_symbol_header
            + (0 if self._implicit_header else 20)
        )
        bits = max(bits, 0)

        # "Bits per symbol" denominator is either (4 * SF) or (4 * (SF -2))
        # depending on Low Data Rate Optimization
        bps = (self._sf - (2 * self._get_ldr_en())) * 4

        return (
            # Fixed preamble portion (4.25), times 4
            17
            # Remainder of equation is an integer number of symbols, times 4
            + 4
            * (
                # configured preamble length
                self._preamble_len
                +
                # optional extra preamble symbols (4.25+2=6.25 for SX1262 SF5,SF6)
                s_o
                +
                # 8 symbol constant overhead
                8
                +
                # Payload symbol length
                # (this is the term "ceil(bits / 4 * SF) * (CR + 4)" in the datasheet
                ((bits + bps - 1) // bps) * self._coding_rate
            )
        )

    def get_time_on_air_us(self, payload_len):
        # Return the "Time on Air" in microseconds for a particular
        # payload length and the current configured modem settings.
        return self._get_t_sym_us() * self.get_n_symbols_x4(payload_len) // 4

    # Modem ISR routines
    #
    # ISR implementation is relatively simple, just exists to signal an optional
    # callback, record a timestamp, and wake up the hardware if
    # needed. ppplication code is expected to call poll_send() or
    # poll_recv() as applicable in order to confirm the modem state.
    #
    # This is a MP hard irq in some configurations, meaning no memory allocation is possible.
    #
    # 'pin' may also be None if this is a "soft" IRQ triggered after a receive
    # timed out during a send (meaning no receive IRQ will fire, but the
    # receiver should wake up and move on anyhow.)
    def _radio_isr(self, pin):
        self._last_irq = utime.ticks_ms()
        if self._irq_callback:
            self._irq_callback(pin)
        if _DEBUG:
            # Note: this may cause a MemoryError and fail if _DEBUG is enabled in this base class
            # but disabled in the subclass, meaning this is a hard irq handler
            try:
                print("_radio_isr pin={}".format(pin))
            except MemoryError:
                pass

    def irq_triggered(self):
        # Returns True if the ISR has executed since the last time a send or a receive
        # started
        return self._last_irq is not None

    def set_irq_callback(self, callback):
        # Set a function to be called from the radio ISR
        #
        # This is used by the AsyncModem implementation, but can be called in
        # other circumstances to implement custom ISR logic.
        #
        # Note that callback may be called in hard ISR context, meaning no
        # memory allocation is possible.
        self._irq_callback = callback

    def _get_last_irq(self):
        # Return the _last_irq timestamp if set by an ISR, or the
        # current utime.time_ms() timestamp otherwise.
        if self._last_irq is None:
            return utime.ticks_ms()
        return self._last_irq

    # Common parts of receive API

    def start_recv(self, timeout_ms=None, continuous=False, rx_length=0xFF):
        # Start receiving.
        #
        # Part of common low-level modem API, see README.md for usage.
        if continuous and timeout_ms is not None:
            raise ValueError()  # these two options are mutually exclusive

        if timeout_ms is not None:
            self._rx = utime.ticks_add(utime.ticks_ms(), timeout_ms)
        else:
            self._rx = True

        self._rx_continuous = continuous
        self._rx_length = rx_length

        if self._ant_sw and not self._tx:
            # this is guarded on 'not self._tx' as the subclass will not immediately
            # start receiving if a send is in progress.
            self._ant_sw.rx()

    def poll_recv(self, rx_packet=None):
        # Should be called while a receive is in progress:
        #
        # Part of common low-level modem API, see README.md for usage.
        #
        # This function may alter the state of the modem - it will clear
        # RX interrupts, it may read out a packet from the FIFO, and it
        # may resume receiving if the modem has gone to standby but receive
        # should resume.

        if self._rx is False:
            # Not actually receiving...
            return False

        if self._tx:
            # Actually sending, this has to complete before we
            # resume receiving, but we'll indicate that we are still receiving.
            #
            # (It's not harmful to fall through here and check flags anyhow, but
            # it is a little wasteful if an interrupt has just triggered
            # poll_send() as well.)
            return True

        packet = None

        flags = self._get_irq()

        final_flags = flags & self._IRQ_RX_COMPLETE  # has RX actually completed?
        if _DEBUG and flags:
            print("RX flags {:#x} masked {:#x}".format(flags, final_flags))
        if final_flags:
            # clear final_flags IRQs only. Keep flags for valid header, etc. as
            # they may actually relate to a new packet that the modem is
            # currently receiving
            #
            # This is safe to do because the individual modem classes ensure that
            # DIO1/DIO0/etc only go high on 'final_flags' interrupts and not other
            # interrrupts that may get "stuck" if not cleared.
            self._clear_irq(final_flags)
            ok = self._rx_flags_success(flags)
            if not ok:
                # If a non-valid receive happened, increment the CRC error counter
                self.crc_errors += 1
            if ok or self.rx_crc_error:
                # Successfully received a valid packet (or configured to return all packets)
                packet = self._read_packet(rx_packet, flags)
                if not self._rx_continuous:
                    # Done receiving now
                    self._end_recv()

        # _check_recv() will return True if a receive is ongoing and hasn't timed out,
        # and also manages resuming any modem receive if needed
        #
        # We need to always call check_recv(), but if we received a packet then this is what
        # we should return to the caller.
        res = self._check_recv()
        return packet or res

    def _end_recv(self):
        # Utility function to clear the receive state
        self._rx = False
        if self._ant_sw:
            self._ant_sw.idle()

    def _check_recv(self):
        # Internal function to automatically call start_recv()
        # again if a receive has been interrupted and the host
        # needs to start it again.
        #
        # Return True if modem is still receiving (or sending, but will
        # resume receiving after send finishes).

        if not self._rx:
            return False  # Not receiving, nothing to do

        if not self.is_idle():
            return True  # Radio is already sending or receiving

        rx = self._rx

        timeout_ms = None
        if isinstance(rx, int):  # timeout is set
            timeout_ms = utime.ticks_diff(rx, utime.ticks_ms())
            if timeout_ms <= 0:
                # Timed out in software, nothing to resume
                self._end_recv()
                if _DEBUG:
                    print("Timed out in software timeout_ms={}".format(timeout_ms))
                schedule(
                    self._radio_isr, None
                )  # "soft irq" to unblock anything waiting on the interrupt event
                return False

        if _DEBUG:
            print(
                "Resuming receive timeout_ms={} continuous={} rx_length={}".format(
                    timeout_ms, self._rx_continuous, self._rx_length
                )
            )

        self.start_recv(timeout_ms, self._rx_continuous, self._rx_length)

        # restore the previous version of _rx so ticks_ms deadline can't
        # slowly creep forward each time this happens
        self._rx = rx

        return True

    # Common parts of send API

    def poll_send(self):
        # Check the ongoing send state.
        #
        # Returns one of:
        #
        # - True if a send is ongoing and the caller
        #   should call again.
        # - False if no send is ongoing.
        # - An int value exactly one time per transmission, the first time
        #   poll_send() is called after a send ends. In this case it
        #   is the utime.ticks_ms() timestamp of the time that the send completed.
        #
        # Note this function only returns an int value one time (the first time it
        # is called after send completes).
        #
        # Part of common low-level modem API, see README.md for usage.
        if not self._tx:
            return False

        ticks_ms = self._get_last_irq()

        if not (self._get_irq() & self._IRQ_TX_COMPLETE):
            # Not done. If the host and modem get out
            # of sync here, or the caller doesn't follow the sequence of
            # send operations exactly, then can end up in a situation here
            # where the modem has stopped sending and has gone to Standby,
            # so _IRQ_TX_DONE is never set.
            #
            # For now, leaving this for the caller to do correctly. But if it becomes an issue then
            # we can call _get_mode() here as well and check the modem is still in a TX mode.
            return True

        self._clear_irq()

        self._tx = False

        if self._ant_sw:
            self._ant_sw.idle()

        # The modem just finished sending, so start receiving again if needed
        self._check_recv()

        return ticks_ms

    # Simple synchronous modem API functions
    #
    # These are intended for simple applications. They block the caller until
    # the modem operation is complete, and don't support interleaving send
    # and receive.

    def send(self, packet, tx_at_ms=None):
        # Send the given packet (byte sequence),
        # and return once transmission of the packet is complete.
        #
        # Returns a timestamp (result of utime.ticks_ms()) when the packet
        # finished sending.
        self.prepare_send(packet)

        # If the caller specified a timestamp to start transmission at, wait until
        # that time before triggering the send
        if tx_at_ms is not None:
            utime.sleep_ms(max(0, utime.ticks_diff(tx_at_ms, utime.ticks_ms())))

        will_irq = self.start_send()  # ... and go!

        # sleep for the expected send time before checking if send has ended
        utime.sleep_ms(self.get_time_on_air_us(len(packet)) // 1000)

        tx = True
        while tx is True:
            tx = self.poll_send()
            self._sync_wait(will_irq)
        return tx

    def recv(self, timeout_ms=None, rx_length=0xFF, rx_packet=None):
        # Attempt to a receive a single LoRa packet, timeout after timeout_ms milliseconds
        # or wait indefinitely if no timeout is supplied (default).
        #
        # Returns an instance of RxPacket or None if the radio timed out while receiving.
        #
        # Optional rx_length argument is only used if lora_cfg["implict_header"] == True
        # (not the default) and holds the length of the payload to receive.
        #
        # Optional rx_packet argument can be an existing instance of RxPacket
        # which will be reused to save allocations, but only if the received packet
        # is the same length as the rx_packet packet. If the length is different, a
        # new RxPacket instance is allocated and returned.
        will_irq = self.start_recv(timeout_ms, False, rx_length)
        rx = True
        while rx is True:
            self._sync_wait(will_irq)
            rx = self.poll_recv(rx_packet)
        return rx or None

    def _sync_wait(self, will_irq):
        # For synchronous usage, block until an interrupt occurs or we time out
        if will_irq:
            for n in range(100):
                umachine.idle()
                # machine.idle() wakes up very often, so don't actually return
                # unless _radio_isr ran already. The outer for loop is so the
                # modem is still polled occasionally to
                # avoid the possibility an IRQ was lost somewhere.
                #
                # None of this is very efficient, power users should either use
                # async or call the low-level API manually with better
                # port-specific sleep configurations, in order to get the best
                # efficiency.
                if self.irq_triggered():
                    break
        else:
            utime.sleep_ms(1)


class RxPacket(bytearray):
    # A class to hold a packet received from a LoRa modem.
    #
    # The base class is bytearray, which represents the packet payload,
    # allowing RxPacket objects to be passed anywhere that bytearrays are
    # accepted.
    #
    # Some additional properties are set on the object to store metadata about
    # the received packet.
    def __init__(self, payload, ticks_ms=None, snr=None, rssi=None, valid_crc=True):
        super().__init__(payload)
        self.ticks_ms = ticks_ms
        self.snr = snr
        self.rssi = rssi
        self.valid_crc = valid_crc

    def __repr__(self):
        return "{}({}, {}, {}, {}, {})".format(
            "RxPacket",
            repr(
                bytes(self)
            ),  # This is a bit wasteful, but gets us b'XYZ' rather than "bytearray(b'XYZ')"
            self.ticks_ms,
            self.snr,
            self.rssi,
            self.valid_crc,
        )
