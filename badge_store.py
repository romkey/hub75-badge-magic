# SPDX-License-Identifier: MIT
"""Badge configuration and the last upload, kept in microcontroller.nvm.

Like the firmware, configuration changes live in RAM until save_cfg, while
an uploaded frame is persisted as soon as it arrives. Frames too big for
NVM still play; they just do not survive a reset.

NVM layout::

    0     4    b"BMH1"
    4     1    ble_always_on
    5     1    led_brightness (0..3)
    6     1    reset_rx
    7     1    name length
    8     20   name (UTF-8)
    28    2    splash speed, ms (little-endian)
    30    3    splash width, height, frame height
    33    2    splash bitmap length (little-endian)
    35    255  splash XBM bits
    320   2    frame length (little-endian), 0 if none
    322   ...  the legacy "wang" frame
"""

import microcontroller

_MAGIC = b"BMH1"
_CFG_SIZE = 290
_FRAME_OFF = 320
MAX_NAME = 20
SPLASH_MAX_BITS = 255

_nvm = microcontroller.nvm


class Config:
    def __init__(self, name, brightness):
        self.default_name = name
        self.default_brightness = brightness
        self.load_defaults()

    def load_defaults(self):
        """cfg_fallback()."""
        self.always_on = False
        self.brightness = self.default_brightness
        self.reset_rx = False
        self.name = self.default_name
        self.splash_speed = 30
        self.splash_w = 0
        self.splash_h = 0
        self.splash_fh = 11
        self.splash_bits = b""

    def load(self):
        """Read saved config; returns False (keeping defaults) if none is stored."""
        if _nvm is None or len(_nvm) < _CFG_SIZE or _nvm[0:4] != _MAGIC:
            return False
        raw = _nvm[0:_CFG_SIZE]
        self.always_on = bool(raw[4])
        self.brightness = raw[5] & 3
        self.reset_rx = bool(raw[6])
        n = min(raw[7], MAX_NAME)
        try:
            self.name = raw[8 : 8 + n].decode("utf-8") or self.default_name
        except UnicodeError:
            self.name = self.default_name
        self.splash_speed = raw[28] | raw[29] << 8
        self.splash_w, self.splash_h, self.splash_fh = raw[30], raw[31], raw[32]
        n = min(raw[33] | raw[34] << 8, SPLASH_MAX_BITS)
        self.splash_bits = bytes(raw[35 : 35 + n])
        return True

    def save(self):
        """save_cfg(); returns the firmware-style result code."""
        if _nvm is None or len(_nvm) < _CFG_SIZE:
            return 1
        raw = bytearray(_CFG_SIZE)
        raw[0:4] = _MAGIC
        raw[4] = int(self.always_on)
        raw[5] = self.brightness & 3
        raw[6] = int(self.reset_rx)
        name = self.name.encode("utf-8")[:MAX_NAME]
        raw[7] = len(name)
        raw[8 : 8 + len(name)] = name
        raw[28] = self.splash_speed & 0xFF
        raw[29] = self.splash_speed >> 8
        raw[30], raw[31], raw[32] = self.splash_w, self.splash_h, self.splash_fh
        bits = self.splash_bits[:SPLASH_MAX_BITS]
        raw[33] = len(bits) & 0xFF
        raw[34] = len(bits) >> 8
        raw[35 : 35 + len(bits)] = bits
        try:
            _nvm[0:_CFG_SIZE] = raw
        except Exception as err:  # report any flash failure as a result code
            print("config save failed:", err)
            return 1
        return 0


def load_frame():
    if _nvm is None or len(_nvm) < _FRAME_OFF + 2:
        return None
    n = _nvm[_FRAME_OFF] | _nvm[_FRAME_OFF + 1] << 8
    if n == 0 or _FRAME_OFF + 2 + n > len(_nvm):
        return None
    return bytes(_nvm[_FRAME_OFF + 2 : _FRAME_OFF + 2 + n])


def save_frame(frame):
    """Persist an upload; returns False if it does not fit in NVM."""
    if _nvm is None or _FRAME_OFF + 2 + len(frame) > len(_nvm):
        # Clear the old frame so a reset does not bring back stale messages.
        if _nvm is not None and len(_nvm) >= _FRAME_OFF + 2:
            _nvm[_FRAME_OFF : _FRAME_OFF + 2] = b"\x00\x00"
        return False
    n = len(frame)
    _nvm[_FRAME_OFF : _FRAME_OFF + 2 + n] = bytes((n & 0xFF, n >> 8)) + frame
    return True
