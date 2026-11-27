"""Minimal driver for the Thermalright USB LCD (VID 0x0416 / PID 0x5408, "USBDISPLAY").

Protocol reverse-engineered from TRCC 2.1.8 (USBLCDNEW.dll, "LY" device branch):

  * Handshake: write 2048 bytes  [02 FF 00 00 00 00 00 00 01 ...zeros] to EP 0x09,
    read 512 bytes from EP 0x81. Reply starts 03 FF .. and byte[8] == 1.
    byte[20] is the panel code (<=3 -> TRCC profile 65 = 1920x462 bar screen).
  * Frame: a JPEG split into 496-byte pieces. Each piece is sent as a 512-byte
    chunk with a 16-byte header:
        [0]=0x01 [1]=0xFF [2..5]=total jpeg length (LE) [6..7]=piece length (LE)
        [8]=0x01 [9..10]=chunk count (LE) [11..12]=chunk index (LE) [13..15]=0
    Chunk count is padded (with zero chunks) to a multiple of 4, written in
    4096-byte transfers, then the device acks with a 512-byte read.
"""

from __future__ import annotations

import io
import struct
import sys

import libusb_package
import usb.core
import usb.util
from PIL import Image

VID, PID = 0x0416, 0x5408
EP_OUT, EP_IN = 0x09, 0x81
PIECE = 496
CHUNK = 512

# Panel code from handshake byte[20] -> (width, height), per TRCC FormCZTVInit.
PANEL_SIZES = {
    1: (1920, 462),  # TRCC maps codes 0..3 to 1 -> profile 65
    2: (1920, 462),
    4: (1280, 480),  # profile 68
    5: (1920, 440),  # profile 69
}


_PERMISSION_HELP = ("No permission to use the LCD. On Linux, install the udev rule "
                    "(see packaging/linux/README) and plug the screen in again.")


class LCDError(Exception):
    pass


class ThermalrightLCD:
    def __init__(self):
        self.dev = None
        self.size = (1920, 462)
        self.info = b""

    # -- connection -------------------------------------------------------
    def open(self) -> None:
        try:
            dev = libusb_package.find(idVendor=VID, idProduct=PID)
        except usb.core.NoBackendError as e:
            raise LCDError("libusb isn't available on this computer") from e
        if dev is None:
            raise LCDError("Thermalright LCD (0416:5408) not found. Is it plugged in?")
        if sys.platform.startswith("linux"):
            try:  # let go of any kernel driver bound to it
                if dev.is_kernel_driver_active(0):
                    dev.detach_kernel_driver(0)
            except (usb.core.USBError, NotImplementedError):
                pass
        try:
            dev.set_configuration(1)
        except usb.core.USBError as e:
            if e.errno == 13:  # EACCES
                raise LCDError(_PERMISSION_HELP) from e
            # otherwise: already configured
        try:
            usb.util.claim_interface(dev, 0)
        except usb.core.USBError as e:
            if e.errno == 13:
                raise LCDError(_PERMISSION_HELP) from e
            raise LCDError(
                f"Could not claim the LCD ({e}). Close any other program driving it (e.g. TRCC)."
            ) from e
        self.dev = dev
        self._handshake()

    def close(self) -> None:
        if self.dev is not None:
            try:
                usb.util.release_interface(self.dev, 0)
                usb.util.dispose_resources(self.dev)
            except usb.core.USBError:
                pass
            self.dev = None

    def _handshake(self) -> None:
        hello = bytes([0x02, 0xFF, 0, 0, 0, 0, 0, 0, 0x01]) + bytes(2048 - 9)
        self.dev.write(EP_OUT, hello, timeout=1000)
        resp = bytes(self.dev.read(EP_IN, 512, timeout=1000))
        if len(resp) < 24 or resp[0] != 0x03 or resp[1] != 0xFF or resp[8] != 0x01:
            raise LCDError(f"Unexpected handshake reply: {resp[:24].hex(' ')}")
        self.info = resp
        code = resp[20] if resp[20] > 3 else 1
        self.size = PANEL_SIZES.get(code, (1920, 462))

    # -- drawing ------------------------------------------------------------
    @staticmethod
    def encode(image: Image.Image, quality: int = 92) -> bytes:
        buf = io.BytesIO()
        image.convert("RGB").save(buf, "JPEG", quality=quality)
        data = buf.getvalue()
        while len(data) >= 450_000 and quality > 40:  # TRCC's size limit
            quality -= 5
            buf = io.BytesIO()
            image.convert("RGB").save(buf, "JPEG", quality=quality)
            data = buf.getvalue()
        return data

    def send_jpeg(self, jpeg: bytes) -> None:
        if self.dev is None:
            raise LCDError("Device not open")
        total = len(jpeg)
        count = total // PIECE + 1
        last_len = total % PIECE
        buf = bytearray()
        for i in range(count):
            n = last_len if i == count - 1 else PIECE
            header = struct.pack("<BBIHBHH3x", 0x01, 0xFF, total, n, 0x01, count, i)
            piece = jpeg[i * PIECE : i * PIECE + n]
            buf += header + piece + bytes(PIECE - n)
        # pad to a multiple of 4 chunks (2048 bytes)
        if count % 4:
            buf += bytes(CHUNK * (4 - count % 4))
        try:
            for off in range(0, len(buf), 4096):
                self.dev.write(EP_OUT, buf[off : off + 4096], timeout=1000)
            self.dev.read(EP_IN, 512, timeout=1000)  # ack
        except usb.core.USBError as e:
            raise LCDError(f"USB transfer failed: {e}") from e

    def show(self, image: Image.Image, quality: int = 92) -> bytes:
        jpeg = self.encode(image, quality)
        self.send_jpeg(jpeg)
        return jpeg
