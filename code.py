# SPDX-License-Identifier: MIT
"""Badge Magic LED badge emulator for an Adafruit Matrix Portal S3 + 64x32 HUB75.

Speaks the same Bluetooth LE protocol as an 11x44 Badge Magic badge running
the FOSSASIA open firmware, so the Badge Magic apps, the `badgemagic` CLI and
the Home Assistant integration (github.com/romkey/hass-badgemagic) can drive it:

- legacy upload, service 0xFEE0 / characteristic 0xFEE1: up to 8 messages
  with the 9 animation modes, speed, flash and marquee, plus the badge clock
- next-gen control, service 0xF055 (write 0xF057, results notified on 0xF056):
  streaming, brightness, name, splash screen, config save/load, reboot, power
- Battery (0x180F) and Device Information (0x180A)

Each badge LED is drawn as a PIXEL_W x PIXEL_H block of matrix pixels.
Settings come from settings.toml (see settings.toml.example).
"""

import os
import struct
import time

import _bleio
import bitmaptools
import board
import displayio
import framebufferio
import keypad
import microcontroller
import rgbmatrix
from adafruit_ble import BLERadio
from adafruit_ble.advertising.standard import ProvideServicesAdvertisement
from adafruit_ble.attributes import Attribute
from adafruit_ble.characteristics import Characteristic, ComplexCharacteristic
from adafruit_ble.services import Service
from adafruit_ble.services.standard import BatteryService
from adafruit_ble.services.standard.device_info import DeviceInfoService
from adafruit_ble.uuid import StandardUUID

import badge_anim as anim
import badge_store as store


def _setting(name, default):
    value = os.getenv(name)
    if value is None or value == "":
        return default
    if isinstance(default, int) and isinstance(value, str):
        return int(value, 0)
    return value


MATRIX_WIDTH = _setting("BADGE_MATRIX_WIDTH", 64)
MATRIX_HEIGHT = _setting("BADGE_MATRIX_HEIGHT", 32)
PIXEL_W = _setting("BADGE_PIXEL_W", 1)
PIXEL_H = _setting("BADGE_PIXEL_H", 2)
COLOR = _setting("BADGE_COLOR", 0xFF0000)
DEFAULT_NAME = _setting("BADGE_NAME", "LED Badge Magic")
DEFAULT_BRIGHTNESS = _setting("BADGE_BRIGHTNESS", 1)
PIN = str(_setting("BADGE_PIN", ""))  # 4 digits enables SECURITY

ROWS = anim.ROWS
VIEW_COLS = MATRIX_WIDTH // PIXEL_W
X0 = (MATRIX_WIDTH - VIEW_COLS * PIXEL_W) // 2
Y0 = max(0, (MATRIX_HEIGHT - ROWS * PIXEL_H) // 2)

# Firmware timings (src/main.c).
BASE_STEP_NS = 200_000_000
MARQUEE_NS = 100_000_000
FLASH_NS = 500_000_000
FIRST_STEP_NS = 500_000_000

LEGACY_PACKET = 16
LEGACY_HEADER = 64
MAX_FRAME = 8192
STREAM_BYTES = anim.FRAME_COLS * 2

# int8 result codes as the firmware notifies them.
OK = 0x00
ERR_1 = 0xFF
ERR_2 = 0xFE
ERR_3 = 0xFD
ERR_4 = 0xFC


# BLE services ----------------------------------------------------------------


class PacketIn(ComplexCharacteristic):
    """A writable characteristic whose writes are queued whole, one per packet."""

    def __init__(self, *, uuid, max_length, buffer_size):
        self._buffer_size = buffer_size
        super().__init__(
            uuid=uuid,
            properties=Characteristic.WRITE | Characteristic.WRITE_NO_RESPONSE,
            read_perm=Attribute.NO_ACCESS,
            write_perm=Attribute.OPEN,
            max_length=max_length,
        )

    def bind(self, service):
        bound = super().bind(service)
        return _bleio.PacketBuffer(bound, buffer_size=self._buffer_size)


class LegacyService(Service):
    uuid = StandardUUID(0xFEE0)
    rx = PacketIn(uuid=StandardUUID(0xFEE1), max_length=LEGACY_PACKET, buffer_size=256)


class NextGenService(Service):
    uuid = StandardUUID(0xF055)
    result = Characteristic(
        uuid=StandardUUID(0xF056),
        properties=Characteristic.NOTIFY | Characteristic.READ,
        write_perm=Attribute.NO_ACCESS,
        max_length=1,
        fixed_length=True,
        initial_value=b"\x00",
    )
    rx = PacketIn(uuid=StandardUUID(0xF057), max_length=320, buffer_size=8)


# display ---------------------------------------------------------------------

ADDR_PINS = (board.MTX_ADDRA, board.MTX_ADDRB, board.MTX_ADDRC, board.MTX_ADDRD, board.MTX_ADDRE)

displayio.release_displays()
matrix = rgbmatrix.RGBMatrix(
    width=MATRIX_WIDTH,
    height=MATRIX_HEIGHT,
    bit_depth=4,
    rgb_pins=[board.MTX_R1, board.MTX_G1, board.MTX_B1, board.MTX_R2, board.MTX_G2, board.MTX_B2],
    addr_pins=list(ADDR_PINS[: (MATRIX_HEIGHT // 2).bit_length() - 1]),  # 4 for 32 rows, 5 for 64
    clock_pin=board.MTX_CLK,
    latch_pin=board.MTX_LAT,
    output_enable_pin=board.MTX_OE,
)
display = framebufferio.FramebufferDisplay(matrix, auto_refresh=False)
bitmap = displayio.Bitmap(MATRIX_WIDTH, MATRIX_HEIGHT, 2)
palette = displayio.Palette(2)
palette[0] = 0x000000
root = displayio.Group()
root.append(displayio.TileGrid(bitmap, pixel_shader=palette))
display.root_group = root

shown = [-1] * VIEW_COLS


def set_brightness(level):
    """The firmware's 4 PWM duty levels, 0 = 25% .. 3 = 100%, applied to the color."""
    scale = ((level & 3) + 1) / 4
    r = int(((COLOR >> 16) & 0xFF) * scale)
    g = int(((COLOR >> 8) & 0xFF) * scale)
    b = int((COLOR & 0xFF) * scale)
    palette[1] = (r << 16) | (g << 8) | b


def show(fb):
    """Draw a VIEW_COLS-wide column buffer, touching only columns that changed."""
    changed = False
    for x in range(VIEW_COLS):
        v = fb[x] & anim.MASK
        if v == shown[x]:
            continue
        shown[x] = v
        changed = True
        px = X0 + x * PIXEL_W
        bitmaptools.fill_region(bitmap, px, Y0, px + PIXEL_W, Y0 + ROWS * PIXEL_H, 0)
        py = Y0
        while v:
            if v & 1:
                bitmaptools.fill_region(bitmap, px, py, px + PIXEL_W, py + PIXEL_H, 1)
            v >>= 1
            py += PIXEL_H
    if changed:
        display.refresh(minimum_frames_per_second=0)


def blank():
    show([0] * VIEW_COLS)


# default content -------------------------------------------------------------

_GLYPHS = {
    "B": ("####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."),
    "A": (".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "D": ("####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."),
    "G": (".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".###."),
    "E": ("#####", "#....", "#....", "####.", "#....", "#....", "#####"),
    "M": ("#...#", "##.##", "#.#.#", "#.#.#", "#...#", "#...#", "#...#"),
    "I": ("###", ".#.", ".#.", ".#.", ".#.", ".#.", "###"),
    "C": (".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."),
    " ": ("..", "..", "..", "..", "..", "..", ".."),
}


def default_messages():
    cols = []
    for ch in "BADGE MAGIC":
        rows = _GLYPHS[ch]
        for x in range(len(rows[0])):
            v = 0
            for y, row in enumerate(rows):
                if row[x] == "#":
                    v |= 1 << (y + 2)
            cols.append(v)
        cols.append(0)
    return [anim.Message(cols, anim.SCROLL_LEFT, 4)]


# legacy upload ---------------------------------------------------------------


def parse_frame(frame):
    """Decode a "wang" frame into Messages, setting the clock from its timestamp."""
    flash_bits = frame[6]
    marquee_bits = frame[7]
    offset = LEGACY_HEADER
    messages = []
    for i in range(8):
        chunks = frame[0x10 + 2 * i] << 8 | frame[0x11 + 2 * i]
        if not chunks:
            continue
        if offset + chunks * ROWS > len(frame):
            break
        mode_byte = frame[8 + i]
        cols = anim.decode_chunks(frame, offset, chunks)
        offset += chunks * ROWS
        messages.append(
            anim.Message(
                cols,
                mode=mode_byte & 0x0F,
                speed=(mode_byte >> 4) & 0x07,
                flash=bool(flash_bits & (1 << i)),
                marquee=bool(marquee_bits & (1 << i)),
            )
        )
    return messages


def set_clock(frame):
    ts = frame[0x26:0x2C]
    if not 1 <= ts[1] <= 12 or not 1 <= ts[2] <= 31:
        return
    try:
        import rtc  # noqa: PLC0415

        year = 2000 + (ts[0] - 208) % 256
        rtc.RTC().datetime = time.struct_time((year, ts[1], ts[2], ts[3], ts[4], ts[5], 0, -1, -1))
    except (ImportError, ValueError, OverflowError) as err:
        print("clock not set:", err)


class LegacyReceiver:
    """legacy_ble_rx(): reassembles 16-byte packets into a frame."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.count = 0
        self.data = None
        self.data_len = 0
        self.authorized = not PIN
        self.warned = False

    def feed(self, pkt):
        """Returns the complete frame once the last packet has arrived."""
        if len(pkt) != LEGACY_PACKET:
            return None
        if not self.authorized:
            # SECURITY: a 4-digit PIN packet must come first. The firmware rejects
            # bad packets with an ATT error; CircuitPython cannot, so we drop them.
            if pkt[:4] == PIN.encode():
                self.authorized = True
            elif not self.warned:
                self.warned = True
                print("upload rejected: PIN required")
            return None
        if pkt[:5] == b"wang\x00":
            self.count = 0
            self.data = bytearray()
        elif self.count == 0 or self.data is None:
            return None
        self.data += pkt
        if self.count == 1:
            n = 0
            for i in range(8):
                n += self.data[0x10 + 2 * i] << 8 | self.data[0x11 + 2 * i]
            self.data_len = LEGACY_HEADER + ROWS * n
            if self.data_len > MAX_FRAME:
                print("upload too large:", self.data_len)
                self.data = None
                self.count = 0
                return None
        if self.count > 2 and (self.count + 1) * LEGACY_PACKET >= self.data_len:
            frame = bytes(self.data[: self.data_len])
            self.data = None
            self.count = 0
            return frame
        self.count += 1
        return None


# the badge -------------------------------------------------------------------


class Badge:
    def __init__(self):
        self.cfg = store.Config(DEFAULT_NAME, DEFAULT_BRIGHTNESS)
        if self.cfg.load():
            print("config loaded from NVM")
        set_brightness(self.cfg.brightness)

        self.messages = None
        frame = store.load_frame()
        if frame:
            self.messages = parse_frame(frame) or None
        if not self.messages:
            self.messages = default_messages()

        self.current = 0
        self.sequential = True
        self.frame_fb = [0] * anim.FRAME_COLS
        self.base_fb = [0] * VIEW_COLS
        self.stream_fb = [0] * anim.FRAME_COLS
        self.streaming = False
        self.marquee_step = 0
        self.flash_step = 0
        self.dirty = True
        self.pending_action = None
        now = time.monotonic_ns()
        self.next_step = now + FIRST_STEP_NS
        self.next_marquee = now + MARQUEE_NS
        self.next_flash = now + FLASH_NS

    @property
    def bm(self):
        return self.messages[self.current]

    # playback ----------------------------------------------------------------

    def restart(self, messages=None):
        if messages:
            self.messages = messages
        for m in self.messages:
            m.anim_step = 0
        self.current = 0
        self.sequential = True
        for i in range(anim.FRAME_COLS):
            self.frame_fb[i] = 0
        self.next_step = time.monotonic_ns() + FIRST_STEP_NS
        self.dirty = True

    def transition(self):
        """bm_transition(): KEY1 toggles between looping one message and playing all."""
        if self.sequential:
            self.sequential = False
            self.current = 0
            return
        self.bm.anim_step = 0
        self.current = (self.current + 1) % len(self.messages)
        if self.current == 0:
            self.sequential = True

    def tick(self, now):
        if self.streaming:
            return
        if now >= self.next_step:
            bm = self.bm
            if anim.step(bm, self.frame_fb, self.base_fb) == 0 and self.sequential:
                bm.anim_step = 0
                self.current = (self.current + 1) % len(self.messages)
            self.next_step = now + BASE_STEP_NS - bm.speed * BASE_STEP_NS // 8
            self.dirty = True
        if now >= self.next_marquee:
            self.marquee_step += 1
            self.next_marquee = now + MARQUEE_NS
            self.dirty = self.dirty or self.bm.marquee
        if now >= self.next_flash:
            self.flash_step += 1
            self.next_flash = now + FLASH_NS
            self.dirty = self.dirty or self.bm.flash or self.bm.marquee
        if self.dirty:
            self.dirty = False
            fb = list(self.base_fb)
            if self.bm.flash:
                anim.flash(fb, self.flash_step)
            if self.bm.marquee:
                anim.marquee(fb, self.marquee_step)
            show(fb)

    def play_splash(self):
        c = self.cfg
        if not c.splash_bits or not c.splash_w or not c.splash_h:
            return
        fb = [0] * anim.FRAME_COLS
        view = [0] * VIEW_COLS
        # ani_xbm_scrollup_pad(xbm, 11, 11, 11): scroll in from below and out the top.
        for top in range(-ROWS, c.splash_h + 1):
            anim.xbm_window(c.splash_bits, c.splash_w, c.splash_h, top, fb)
            anim.place(fb, view)
            show(view)
            time.sleep(c.splash_speed / 1000)

    # legacy ------------------------------------------------------------------

    def uploaded(self, frame):
        print("upload complete:", len(frame), "bytes")
        set_clock(frame)
        if not store.save_frame(frame):
            print("frame too large for NVM; it will not survive a reset")
        messages = parse_frame(frame)
        if self.cfg.reset_rx:
            microcontroller.reset()
        if messages:
            self.streaming = False
            self.restart(messages)

    # next-gen ----------------------------------------------------------------

    def command(self, pkt):
        """ng_parse(): returns the result byte, or None for an unknown command."""
        if not pkt:
            return None
        cmd, p = pkt[0], pkt[1:]
        if cmd == 0x00:  # next_packet (a stub in the firmware)
            return OK
        if cmd == 0x01:  # power_setting
            if not p:
                return ERR_2
            if p[0] == 0x00:
                self.pending_action = "poweroff"
            elif p[0] == 0x01:
                self.cfg.reset_rx = len(p) > 1 and p[1] != 0
            elif p[0] == 0x02:
                self.pending_action = "reboot"
            else:
                return ERR_1
            return OK
        if cmd == 0x02:  # streaming_setting
            if p and p[0] == 0x00:
                self.streaming = True
                for i in range(anim.FRAME_COLS):
                    self.stream_fb[i] = 0
                blank()
            elif p and p[0] == 0x01:
                self.streaming = False
                self.dirty = True
            return OK
        if cmd == 0x03:  # stream_bitmap
            if not self.streaming:
                return ERR_1
            n = min(STREAM_BYTES, len(p)) // 2
            for i in range(n):
                self.stream_fb[i] = p[2 * i] | p[2 * i + 1] << 8
            view = [0] * VIEW_COLS
            anim.place(self.stream_fb, view)
            show(view)
            return OK
        if cmd == 0x04:  # ble_setting
            if not p:
                return ERR_2
            if p[0] == 0x00:
                self.cfg.always_on = len(p) > 1 and p[1] != 0
            elif p[0] == 0x01:
                raw = bytes(p[1 : 1 + store.MAX_NAME]).split(b"\x00", 1)[0]
                try:
                    name = raw.decode("utf-8")
                except UnicodeError:
                    name = ""
                if name:
                    self.cfg.name = name
                    self.pending_action = "rename"
            else:
                return ERR_1
            return OK
        if cmd == 0x05:  # flash_splash_screen
            if len(p) < 3:
                return ERR_4
            w, h, fh = p[0], p[1], p[2]
            bits = bytes(p[3:])
            if w > 48:
                return ERR_1
            if h > 44:
                return ERR_2
            if len(bits) > store.SPLASH_MAX_BITS:
                return ERR_3
            self.cfg.splash_w, self.cfg.splash_h, self.cfg.splash_fh = w, h, fh
            self.cfg.splash_bits = bits
            return OK
        if cmd == 0x06:  # save_cfg
            return self.cfg.save()
        if cmd == 0x07:  # load_fallback_cfg
            self.cfg.load_defaults()
            set_brightness(self.cfg.brightness)
            return OK
        if cmd == 0x08:  # misc
            if not p:
                return ERR_2
            if p[0] == 0x00:  # splash speed
                if len(p) < 3:
                    return ERR_2
                self.cfg.splash_speed = p[1] | p[2] << 8
                return OK
            if p[0] == 0x01:  # brightness
                if len(p) < 2 or p[1] > 3:
                    return ERR_2
                self.cfg.brightness = p[1]
                set_brightness(p[1])
                return OK
            return ERR_1
        return None


# main ------------------------------------------------------------------------


def power_off(ble):
    """No power switch to cut, so sleep until a button press wakes (resets) the board."""
    ble.stop_advertising()
    blank()
    try:
        import alarm  # noqa: PLC0415

        displayio.release_displays()
        wake = alarm.pin.PinAlarm(pin=board.BUTTON_UP, value=False, pull=True)
        alarm.exit_and_deep_sleep_until_alarms(wake)
    except (ImportError, ValueError, NotImplementedError) as err:
        print("deep sleep unavailable:", err)
        while True:
            time.sleep(1)


def main():
    badge = Badge()
    badge.play_splash()

    ble = BLERadio()
    legacy = LegacyService()
    nextgen = NextGenService()
    battery = BatteryService()
    battery.level = 100
    _info = DeviceInfoService(  # registered on construction
        manufacturer="Adafruit",
        model_number="Matrix Portal S3 HUB75",
        firmware_revision="hub75-badge-magic 1.0",
        software_revision=os.uname().version[:32],
    )

    def advertise():
        ble.name = badge.cfg.name
        adv = ProvideServicesAdvertisement(legacy, nextgen)
        adv.complete_name = badge.cfg.name
        ble.start_advertising(adv, interval=0.1)
        print("advertising as", repr(badge.cfg.name))

    receiver = LegacyReceiver()
    buttons = keypad.Keys((board.BUTTON_UP, board.BUTTON_DOWN), value_when_pressed=False, pull=True)
    pkt = bytearray(512)
    was_connected = False
    advertise()

    while True:
        connected = ble.connected
        if connected != was_connected:
            was_connected = connected
            print("connected" if connected else "disconnected")
            receiver.reset()
            if not connected:
                advertise()
        elif not connected and not ble.advertising:
            advertise()

        while True:
            n = legacy.rx.readinto(pkt)
            if n <= 0:
                break
            frame = receiver.feed(bytes(pkt[:n]))
            if frame:
                badge.uploaded(frame)

        while True:
            n = nextgen.rx.readinto(pkt)
            if n <= 0:
                break
            result = badge.command(bytes(pkt[:n]))
            if result is not None:
                nextgen.result = struct.pack("B", result)
            if badge.pending_action == "rename":
                badge.pending_action = None
                if not connected:
                    ble.stop_advertising()
                    advertise()
            elif badge.pending_action:
                time.sleep(0.2)  # let the result notification go out first
                if badge.pending_action == "reboot":
                    microcontroller.reset()
                power_off(ble)

        event = buttons.events.get()
        if event and event.pressed:
            if event.key_number == 0:
                badge.transition()
            else:
                badge.cfg.brightness = (badge.cfg.brightness + 1) % 4
                set_brightness(badge.cfg.brightness)

        badge.tick(time.monotonic_ns())


main()
