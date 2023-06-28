from .device import USBInterface

from .utils import (
    endpoint_descriptor,
    split_bmRequestType,
    STAGE_SETUP,
    REQ_TYPE_CLASS,
)
from micropython import const
import micropython
import ustruct
import time
from machine import Timer

_INTERFACE_CLASS_MSC = const(0x08)
_INTERFACE_SUBCLASS_SCSI = const(0x06)
# Bulk-only transport
_PROTOCOL_BBB = const(0x50)

_MAX_PACKET_SIZE = const(64)
_MASS_STORAGE_RESET_REQUEST = const(0xFF)
_GET_MAX_LUN_REQUEST = const(0xFE)

EP_IN_FLAG = const(1 << 7)
EP_OUT_FLAG = const(0x7F)


class CBW:
    """Command Block Wrapper"""

    DIR_OUT = const(0)
    DIR_IN = const(1)

    def __init__(
        self,
        binary=None,
        dCBWSignature=0,
        dCBWTag=0,
        dCBWDataTransferLength=0,
        bmCBWFlags=0,
        bCBWLUN=0,
        bCBWCBLength=0,
        CBWCB=bytearray(16),
    ):
        if binary is not None:
            self.from_binary(binary)
        else:
            self.dCBWSignature = dCBWSignature
            self.dCBWTag = dCBWTag
            self.dCBWDataTransferLength = dCBWDataTransferLength
            self.bmCBWFlags = bmCBWFlags
            self.bCBWLUN = bCBWLUN
            self.bCBWCBLength = bCBWCBLength
            self.CBWCB = CBWCB

    def get_direction(self):
        """Get the direction of the CBW transfer, 0 for host to device, 1 for device to host"""
        if self.dCBWDataTransferLength == 0:
            return None

        return self.bmCBWFlags >= 0x80

    def __bytes__(self):
        return ustruct.pack(
            "<LLLBBB16s",
            self.dCBWSignature,
            self.dCBWTag,
            self.dCBWDataTransferLength,
            self.bmCBWFlags,
            self.bCBWLUN,
            self.bCBWCBLength,
            self.CBWCB,
        )

    def from_binary(self, binary):
        """Take a binary representation of a CBW and parse it into this object"""
        (
            self.dCBWSignature,
            self.dCBWTag,
            self.dCBWDataTransferLength,
            self.bmCBWFlags,
            self.bCBWLUN,
            self.bCBWCBLength,
            self.CBWCB,
        ) = ustruct.unpack("<LLLBBB16s", binary)


class CSW:
    """Command Status Wrapper"""

    STATUS_PASSED = const(0)
    STATUS_FAILED = const(1)
    STATUS_PHASE_ERROR = const(2)

    def __init__(
        self, dCSWSignature=0x53425355, dCSWTag=None, dCSWDataResidue=0, bCSWStatus=0
    ):
        self.dCSWSignature = dCSWSignature
        self.dCSWTag = dCSWTag
        self.dCSWDataResidue = dCSWDataResidue
        self.bCSWStatus = bCSWStatus

    def __bytes__(self):
        return ustruct.pack(
            "<LLLB",
            self.dCSWSignature,
            self.dCSWTag,
            self.dCSWDataResidue,
            self.bCSWStatus,
        )


class MSCInterface(USBInterface):
    """Mass storage interface - contains the USB parts"""

    MSC_STAGE_CMD = const(0)
    MSC_STAGE_DATA = const(1)
    MSC_STAGE_STATUS = const(2)
    MSC_STAGE_STATUS_SENT = const(3)
    MSC_STAGE_NEED_RESET = const(4)

    CBW_SIGNATURE = const(0x43425355)

    def __init__(
        self,
        subclass=_INTERFACE_SUBCLASS_SCSI,
        protocol=_PROTOCOL_BBB,
        filesystem=None,
        lcd=None,
        uart=None,
        print_logs=False,
    ):
        super().__init__(_INTERFACE_CLASS_MSC, subclass, protocol)
        self.lcd = lcd
        self.uart = uart
        self.print_logs = print_logs

        try:
            self.storage_device = StorageDevice(filesystem)
            # Command Block Wrapper, for incoming commands
            self.cbw = CBW()
            # Command Status Wrapper, for outgoing statuses
            self.csw = CSW()
            self.lun = 0
            self.stage = None
            self.timer = Timer()
        except Exception as exc:
            self.log(str(exc))

    def log(self, message):
        """Log to UART, stdout, and/or an LCD depending on whether they have been configured"""
        if self.print_logs:
            print(message)

        if self.uart is not None:
            self.uart.write(bytes(f"{message}\n", "ASCII"))

        if self.lcd is not None:
            self.lcd.putstr(message)

    def get_endpoint_descriptors(self, ep_addr, str_idx):
        """Get the IN and OUT endpoint descriptors"""
        self.log(f"MSC: get_endpoint_descriptors, {ep_addr}, {str_idx}")
        # The OUT endpoint is from host to device, and has the top bit set to 0
        # The IN endpoint is from device to host, and has the top bit set to 1
        self.ep_out = ep_addr & EP_OUT_FLAG
        self.ep_in = (ep_addr + 1) | EP_IN_FLAG
        e_out = endpoint_descriptor(self.ep_out, "bulk", _MAX_PACKET_SIZE)
        e_in = endpoint_descriptor(self.ep_in, "bulk", _MAX_PACKET_SIZE)
        desc = e_out + e_in
        micropython.schedule(self.try_to_prepare_cbw, None)

        return (desc, [], (self.ep_out, self.ep_in))

    def try_to_prepare_cbw(self, args=None):
        try:
            self.prepare_cbw()
        except KeyError:
            self.timer.init(
                mode=Timer.ONE_SHOT, period=2000, callback=self.try_to_prepare_cbw
            )

    def handle_interface_control_xfer(self, stage, request):
        """Handle the interface control transfers; reset and get max lun"""
        self.log("handle_interface_control_xfer()")
        bmRequestType, bRequest, wValue, wIndex, _ = request
        recipient, req_type, _ = split_bmRequestType(bmRequestType)

        if stage != STAGE_SETUP:
            return True

        if req_type == REQ_TYPE_CLASS:
            if bRequest == _MASS_STORAGE_RESET_REQUEST:
                return self.reset()

            if bRequest == _GET_MAX_LUN_REQUEST:
                # This will need updating if more LUNs are added
                retval = int(self.lun).to_bytes(1, "little")
                # Kick off the CBW->CSW->CBW chain here
                if self.stage is None:
                    self.prepare_cbw()
                return retval

        return False

    def reset(self):
        """Theoretically reset, in reality just break things a bit"""
        self.log("reset()")
        # This doesn't work properly at the moment, needs additional
        # functionality in the C side
        self.stage = type(self).MSC_STAGE_CMD
        self.transferred_length = 0
        self.storage_device.reset()
        self.prepare_cbw()
        return True

    def prepare_for_csw(self, status=CSW.STATUS_PASSED):
        """Set up the variables for a CSW"""
        self.log("prepare_for_csw()")
        self.csw.bCSWStatus = int(status)
        self.stage = type(self).MSC_STAGE_STATUS
        return True

    def handle_endpoint_control_xfer(self, stage, request):
        # This isn't currently being invoked at all
        self.log("handle_endpoint_control_xfer")
        if stage != STAGE_SETUP:
            self.log(f"Got {stage}, only dealing with setup")
            return True

        bmRequestType, bRequest, wValue, wIndex, _ = request
        recipient, req_type, _ = split_bmRequestType(bmRequestType)

        ep_addr = wIndex & 0xFFFF

        if self.stage == type(self).MSC_STAGE_NEED_RESET:
            # TODO: stall endpoint?
            self.log("Needs reset")
            return True

        if ep_addr == self.ep_in and self.stage == type(self).MSC_STAGE_STATUS:
            return self.send_csw()

        if ep_addr == self.ep_out and self.stage == type(self).MSC_STAGE_CMD:
            self.log("Preparing CBW")
            self.prepare_cbw()

        return True

    def prepare_cbw(self, args=None):
        """Prepare to have an incoming CBW"""
        self.log("prepare_cbw()")
        try:
            self.stage = type(self).MSC_STAGE_CMD
            self.transferred_length = 0
            self.rx_data = bytearray(31)
            self.log("About to submit xfer for CBW")
            self.submit_xfer(self.ep_out, self.rx_data, self.receive_cbw_callback)
        except Exception as exc:
            self.log(str(exc))
            raise

    def receive_cbw_callback(self, ep_addr, result, xferred_bytes):
        """Callback stub to schedule actual CBW processing"""
        self.log("receive_cbw_callback")
        micropython.schedule(
            self.proc_receive_cbw_callback, (ep_addr, result, xferred_bytes)
        )

    def proc_receive_cbw_callback(self, args):
        """Invoke CBW processing"""
        (ep_addr, result, xferred_bytes) = args
        if self.stage == type(self).MSC_STAGE_CMD:
            self.cbw.from_binary(self.rx_data)
            return self.handle_cbw()

    def handle_cbw(self):
        """Deal with an incoming CBW"""
        self.log("handle_cbw")
        self.csw.dCSWTag = self.cbw.dCBWTag
        self.csw.dCSWDataResidue = 0
        self.csw.bCSWStatus = CSW.STATUS_PASSED

        status = int(self.validate_cbw())
        if status != CSW.STATUS_PASSED:
            self.log(f"Didn't pass: {status}")
            self.prepare_for_csw(status=status)
            return micropython.schedule(self.send_csw, None)

        self.stage = type(self).MSC_STAGE_DATA

        cmd = self.cbw.CBWCB[0 : self.cbw.bCBWCBLength]

        try:
            response = self.storage_device.handle_cmd(cmd)
        except StorageDevice.StorageError as exc:
            self.log(f"Error: {exc}")
            self.prepare_for_csw(status=exc.status)
            return micropython.schedule(self.send_csw, None)
            return self.send_csw()

        if response is None:
            self.log("None response")
            self.prepare_for_csw()
            return micropython.schedule(self.send_csw, None)
            return self.send_csw()

        if len(response) > self.cbw.dCBWDataTransferLength:
            self.log("Wrong size")
            self.prepare_for_csw(status=CSW.STATUS_FAILED)
            return micropython.schedule(self.send_csw, None)
            return self.send_csw()

        if len(response) == 0:
            self.log("Empty response")
            self.prepare_for_csw()
            return micropython.schedule(self.send_csw, None)
            return self.send_csw()

        try:
            self.data = bytearray(response)
            self.proc_transfer_data((self.ep_in, None, 0))
        except Exception as exc:
            self.log(str(exc))

        self.log("Exiting handle_cbw")
        return True

    def transfer_data(self, ep_addr, result, xferred_bytes):
        """Callback function for scheduling transferring data function"""
        self.log("transfer_data")
        micropython.schedule(self.proc_transfer_data, (ep_addr, result, xferred_bytes))

    def proc_transfer_data(self, args):
        """Actual handler for transferring non-CSW data"""
        (ep_addr, result, xferred_bytes) = args
        self.log("proc_transfer_data")

        if self.stage != type(self).MSC_STAGE_DATA:
            self.log("Wrong stage")
            return False

        self.data = self.data[xferred_bytes:]
        self.transferred_length += xferred_bytes
        if not self.data:
            self.log("We're done")
            self.prepare_for_csw()
            return micropython.schedule(self.send_csw, None)
            return self.send_csw()

        residue = self.cbw.dCBWDataTransferLength - len(self.data)
        if residue:
            self.csw.dCSWDataResidue = len(self.data)
            self.data.extend("\0" * residue)

        self.log(f"Preparing to submit data transfer, {len(self.data)} bytes")
        self.submit_xfer(
            ep_addr, self.data[: self.cbw.dCBWDataTransferLength], self.transfer_data
        )

    def validate_cbw(self) -> bool:
        """Perform Valid and Meaningful checks on a CBW"""
        self.log("validate_cbw")
        # Valid checks (6.2.1)
        if self.stage != type(self).MSC_STAGE_CMD:
            self.log("Wrong stage")
            return CSW.STATUS_PHASE_ERROR

        if len(self.rx_data) != 31:
            self.log("Wrong length")
            return CSW.STATUS_FAILED

        if self.cbw.dCBWSignature != type(self).CBW_SIGNATURE:
            self.log("Wrong sig")
            self.log(str(self.cbw.dCBWSignature))
            return CSW.STATUS_FAILED

        # Meaningful checks (6.2.2)
        if self.cbw.bCBWLUN > 15 or not 0 < self.cbw.bCBWCBLength < 17:
            self.log("Wrong length")
            return CSW.STATUS_FAILED

        if self.cbw.bCBWLUN != self.lun:
            self.log("Wrong LUN")
            return CSW.STATUS_FAILED

        # Check if this is a valid SCSI command
        try:
            # The storage layer doesn't know about USB, it'll return True for valid and False for invalid
            return not self.storage_device.validate_cmd(
                self.cbw.CBWCB[0 : self.cbw.bCBWCBLength]
            )
        except Exception as exc:
            self.log(str(exc))
            raise

    def padding_sent(self, ep_addr, result, xferred_bytes):
        """Reschedule send_csw having sent some padding"""
        micropython.schedule(self.send_csw, None)

    def send_csw(self, args):
        """Send a CSW to the host"""
        self.log("send_csw")
        if self.stage == type(self).MSC_STAGE_STATUS_SENT:
            self.log("Wrong status here")

        if self.csw.dCSWDataResidue == 0:
            self.csw.dCSWDataResidue = int(self.cbw.dCBWDataTransferLength) - int(
                self.transferred_length
            )

        # If the host sent a command that was expecting more than just a CSW, we may have to send them some nothing in the absence of being able to STALL
        if self.transferred_length == 0 and self.csw.dCSWDataResidue != 0:
            self.log(
                f"Sending {self.csw.dCSWDataResidue} bytes of nothing to pad it out"
            )
            self.transferred_length = self.csw.dCSWDataResidue
            self.submit_xfer(
                self.ep_in, bytearray(self.csw.dCSWDataResidue), self.padding_sent
            )
            # The flow from sending the CSW happens in the callback, not in whatever called us, so we can just return and re-call from the padding callback
            return

        self.log(
            f"Sending CSW for {hex(self.csw.dCSWTag)}, data residue {self.csw.dCSWDataResidue}, status {self.csw.bCSWStatus}"
        )

        self.stage = type(self).MSC_STAGE_STATUS_SENT

        self.submit_xfer(self.ep_in, self.csw.__bytes__(), self.send_csw_callback)
        return True

    def send_csw_callback(self, ep_addr, result, xferred_bytes):
        """Schedule the preparation for the next CBW on having sent a CSW"""
        self.log("send_csw_callback")
        micropython.schedule(self.prepare_cbw, None)


class StorageDevice:
    """Storage Device - holds the SCSI parts"""

    class StorageError(OSError):
        def __init__(self, message, status):
            super().__init__(message)
            self.status = status

    NO_SENSE = const(0x00)
    MEDIUM_NOT_PRESENT = const(0x01)
    INVALID_COMMAND = const(0x02)

    def __init__(self, filesystem):
        self.filesystem = filesystem
        self.block_size = 512
        self.sense = None
        self.additional_sense_code = None

        # A dict of SCSI commands and their handlers; the key is the opcode for the command
        self.scsi_commands = {
            0x00: {"name": "TEST_UNIT_READY", "handler": self.handle_test_unit_ready},
            0x03: {"name": "REQUEST_SENSE", "handler": self.handle_request_sense},
            0x12: {"name": "INQUIRY", "handler": self.handle_inquiry},
            0x15: {"name": "MODE_SELECT_6"},
            0x1A: {"name": "MODE_SENSE_6", "handler": self.handle_mode_sense6},
            0x1B: {"name": "START_STOP_UNIT"},
            0x1E: {"name": "PREVENT_ALLOW_MEDIUM_REMOVAL"},
            0x25: {"name": "READ_CAPACITY_10", "handler": self.handle_read_capacity_10},
            0x23: {
                "name": "READ_FORMAT_CAPCITY",
                "handler": self.handle_read_format_capacity,
            },
            0x28: {"name": "READ_10", "handler": self.handle_read10},
            0x2A: {"name": "WRITE_10"},
            0x5A: {"name": "MODE_SENSE_10", "handler": self.handle_mode_sense10},
        }

        # KCQ values for different sense states
        self.sense_values = {
            # Key, code, qualifier
            type(self).NO_SENSE: [0x00, 0x00, 0x00],
            type(self).MEDIUM_NOT_PRESENT: [0x02, 0x3A, 0x00],
            type(self).INVALID_COMMAND: [0x05, 0x20, 0x00],
        }

    def reset(self):
        self.sense_key = None

    def validate_cmd(self, cmd):
        """Ensure that this is a command we can handle"""
        if cmd[0] not in self.scsi_commands:
            # We don't know about the command at all
            self.sense = type(self).INVALID_COMMAND
            return False

        if "handler" not in self.scsi_commands[cmd[0]]:
            # We do know about the command, but not what to do with it
            self.sense = type(self).INVALID_COMMAND
            return False

        if self.scsi_commands[cmd[0]]["name"] != "REQUEST_SENSE":
            self.sense = type(self).NO_SENSE

        return True

        # 0x00 to 0x1F should have 6-byte CBDs
        if cmd[0] < 0x20:
            return len(cmd) == 6

        # 0x20 to 0x5F should have 10-byte CBDs
        if cmd[0] < 0x60:
            return len(cmd) == 10

        # Other lengths exist, but aren't supported by us

    def fail_scsi(self, status):
        """If we need to report a failure"""
        raise StorageDevice.StorageError("Failing SCSI", CSW.STATUS_FAILED)

    def handle_cmd(self, cmd):
        try:
            return self.scsi_commands[cmd[0]]["handler"](cmd)
        except Exception as exc:
            raise StorageDevice.StorageError(
                f"Error handling command: {str(exc)}", CSW.STATUS_FAILED
            )

    # Below here are the SCSI command handlers

    def handle_mode_sense6(self, cmd):
        return ustruct.pack(
            ">BBBB",
            3,  # Data length
            0x00,  # What medium?
            0x80,  # Write protected
            0x00,  # Nope
        )

    def handle_mode_sense10(self, cmd):
        return ustruct.pack(
            ">HBBBBH",
            6,  # Data length
            0x00,  # What medium?
            0x80,  # Write protected
            0x00,  # Nope
            0x00,
            0x00,
        )

    def handle_test_unit_ready(self, cmd):
        if self.filesystem is not None:
            self.sense = type(self).NO_SENSE
            return None

        self.sense = type(self).MEDIUM_NOT_PRESENT
        raise StorageDevice.StorageError("No filesystem", status=CSW.STATUS_FAILED)

    def handle_read_capacity_10(self, cmd):
        if self.filesystem is None:
            self.sense = type(self).MEDIUM_NOT_PRESENT
            raise StorageDevice.StorageError("No filesystem", status=CSW.STATUS_FAILED)
        else:
            max_lba = int(len(bytes(self.filesystem)) / self.block_size) - 1

        return ustruct.pack(">LL", max_lba, self.block_size)

    def handle_read_format_capacity(self, cmd):
        block_num = 0
        list_length = 8
        descriptor_type = 3  # 3 = no media present
        if self.filesystem is not None:
            descriptor_type = 2  # 2 = formatted media
            block_num = int(len(bytes(self.filesystem)) / self.block_size)

        return ustruct.pack(
            ">BBBBLBBH",
            0x00,  # Reserved
            0x00,  # Reserved
            0x00,  # Reserved
            list_length,
            block_num,
            descriptor_type,
            0x00,  # Reserved
            self.block_size,
        )

    def handle_read10(self, cmd):
        (read10, flags, lba, group, length, control) = ustruct.unpack(">BBLBHB", cmd)
        return self.filesystem[
            lba * self.block_size : lba * self.block_size + length * self.block_size
        ]

    def handle_request_sense(self, cmd):
        return ustruct.pack(
            ">BBBLBLBBB3s",
            0x70,  # Response code (+invalid INFORMATION)
            0,  # Obsolete
            self.sense_values[self.sense][0],  # Sense key
            0,  # Information
            9,  # Additional sense length
            0,  # Command specific information
            self.sense_values[self.sense][1],  # Additional sense code
            self.sense_values[self.sense][2],  # Additional sense code qualifier
            0,
        )

    def handle_inquiry(self, cmd):
        (_, evpd, page_code, allocation_length, control) = ustruct.unpack(">BBBBB", cmd)
        if evpd == 0:
            return ustruct.pack(
                ">BBBBBBBB8s16s4s",
                0x00,  # SBC-4 device type, Windows may not like RBC?
                #                                0x0E, # RBC device type
                0x80,  # set the top-most bit to say it's removable
                0x00,  # Definitely not claiming to conform to any SCSI standard
                0x02,  # Response data format of 2, other bits set to 0
                32,  # Extra length
                0x00,  # Don't support any of this
                0x00,  # Likewise
                0x00,  # And again
                "MPython",  # Vendor
                "MicroPython MSC",  # Procut
                "0000",  # Revision level
            )

        if page_code == 0x80:
            return ustruct.pack(
                ">BBBB10s",
                0x00,  # SBC-4 device type, Windows may not like RBC?
                0x80,  # Page code
                0x00,  # Reserved
                0x0A,  # Randomly choose ten characters for a serial
                "\0",
            )

        self.sense = type(self).INVALID_COMMAND
        raise StorageDevice.StorageError(
            "EVPD not implemented", status=CSW.STATUS_FAILED
        )
