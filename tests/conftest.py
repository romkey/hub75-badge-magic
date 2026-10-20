"""Load code.py on CPython by stubbing the CircuitPython hardware modules."""

import pathlib
import sys
import time
import types
from unittest import mock

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))  # appended: code.py must not shadow the stdlib "code" module

NVM_SIZE = 8192


class FakeBitmap:
    def __init__(self, width, height, _colors):
        self.px = [[0] * width for _ in range(height)]


class FakePalette(list):
    def __init__(self, n):
        super().__init__([0] * n)


def _fill_region(bitmap, x1, y1, x2, y2, value):  # noqa: PLR0917 - bitmaptools signature
    for y in range(y1, y2):
        for x in range(x1, x2):
            bitmap.px[y][x] = value


class _ComplexCharacteristic:
    def __init__(self, **_kwargs):
        pass


def _install_stubs():
    microcontroller = types.ModuleType("microcontroller")
    microcontroller.nvm = bytearray(NVM_SIZE)
    microcontroller.reset = mock.Mock()
    sys.modules["microcontroller"] = microcontroller

    displayio = types.ModuleType("displayio")
    displayio.Bitmap = FakeBitmap
    displayio.Palette = FakePalette
    displayio.Group = mock.MagicMock
    displayio.TileGrid = mock.MagicMock()
    displayio.release_displays = lambda: None
    sys.modules["displayio"] = displayio

    bitmaptools = types.ModuleType("bitmaptools")
    bitmaptools.fill_region = _fill_region
    sys.modules["bitmaptools"] = bitmaptools

    for name in (
        "_bleio",
        "board",
        "framebufferio",
        "keypad",
        "rgbmatrix",
        "adafruit_ble",
        "adafruit_ble.advertising",
        "adafruit_ble.advertising.standard",
        "adafruit_ble.attributes",
        "adafruit_ble.services.standard",
        "adafruit_ble.services.standard.device_info",
        "adafruit_ble.uuid",
    ):
        sys.modules[name] = mock.MagicMock()
    sys.modules["adafruit_ble.services"] = types.SimpleNamespace(Service=object)
    sys.modules["adafruit_ble.characteristics"] = types.SimpleNamespace(
        Characteristic=mock.MagicMock(), ComplexCharacteristic=_ComplexCharacteristic
    )


@pytest.fixture(scope="session")
def code():
    """code.py as a namespace, without its trailing main() call."""
    _install_stubs()
    source = (ROOT / "code.py").read_text().rstrip()
    assert source.endswith("main()")
    namespace = {"__name__": "badge_code"}
    exec(compile(source[: -len("main()")], str(ROOT / "code.py"), "exec"), namespace)
    return types.SimpleNamespace(**namespace)


@pytest.fixture
def nvm(code):
    import microcontroller

    microcontroller.nvm[:] = bytes(NVM_SIZE)
    return microcontroller.nvm


@pytest.fixture
def badge(code, nvm):
    return code.Badge()


@pytest.fixture
def clock():
    """A fake monotonic_ns that tests advance by hand."""
    return [time.monotonic_ns()]


def run(badge, clock, steps, dt_ns=30_000_000):
    """Tick the badge ``steps`` times; returns how many times it changed message."""
    switches = 0
    for _ in range(steps):
        clock[0] += dt_ns
        before = badge.current
        badge.tick(clock[0])
        if badge.current != before:
            switches += 1
    return switches


def restart(badge, clock, messages):
    """Restart playback with the fake clock lined up with the first-step delay."""
    badge.restart(messages)
    clock[0] = badge.next_step - 500_000_000
