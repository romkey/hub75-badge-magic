"""Uploads and next-gen commands, checked against the badgemagic-ble encoders."""

import datetime as dt

import pytest
from badgemagic_ble.protocol import legacy, ng
from badgemagic_ble.render import Bitmap, render_text

import badge_anim as anim


def columns_of(bitmap):
    raw = bitmap.to_columns()
    return [raw[2 * i] | raw[2 * i + 1] << 8 for i in range(bitmap.width)]


@pytest.fixture
def frame():
    messages = [
        legacy.Message(render_text("HELLO"), legacy.Mode.LASER, speed=6, flash=True),
        legacy.Message(render_text("A longer second message"), legacy.Mode.SCROLL_LEFT, speed=2, marquee=True),
    ]
    return legacy.encode_frame(messages, dt.datetime(2026, 10, 19, 21, 30, 0))


def upload(receiver, frame):
    done = [out for packet in legacy.chunk16(frame) if (out := receiver.feed(packet))]
    return done[0] if done else None


def unpadded(frame):
    """The frame without its 16-byte padding, as the badge stores it."""
    chunks = sum(frame[0x10 + 2 * i] << 8 | frame[0x11 + 2 * i] for i in range(8))
    return frame[: 64 + anim.ROWS * chunks]


def test_decode_chunks_matches_reference():
    bitmap = render_text("Hi 42!")
    chunks = bitmap.to_column_chunks()
    cols = anim.decode_chunks(chunks, 0, len(chunks) // anim.ROWS)
    assert cols[: bitmap.width] == columns_of(bitmap)
    assert not any(cols[bitmap.width :])


def test_upload_reassembly(code, frame):
    assert upload(code.LegacyReceiver(), frame) == unpadded(frame)


def test_new_header_restarts_upload(code, frame):
    receiver = code.LegacyReceiver()
    for packet in legacy.chunk16(frame)[:3]:
        receiver.feed(packet)
    assert upload(receiver, frame) == unpadded(frame)


def test_parse_frame(code, frame):
    first, second = code.parse_frame(frame)
    assert (first.mode, first.speed, first.flash, first.marquee) == (anim.LASER, 5, True, False)
    assert (second.mode, second.speed, second.flash, second.marquee) == (anim.SCROLL_LEFT, 1, False, True)
    assert first.buf[: render_text("HELLO").width] == columns_of(render_text("HELLO"))


def test_upload_is_persisted(code, badge, frame, nvm):
    badge.uploaded(unpadded(frame))
    assert code.store.load_frame() == unpadded(frame)
    assert len(code.Badge().messages) == 2


def test_pin(code, frame, monkeypatch):
    monkeypatch.setitem(code.LegacyReceiver.reset.__globals__, "PIN", "6512")
    assert upload(code.LegacyReceiver(), frame) is None
    receiver = code.LegacyReceiver()
    receiver.feed(legacy.encode_pin("6512"))
    assert upload(receiver, frame) == unpadded(frame)


def test_streaming(code, badge):
    assert badge.command(ng.encode_stream_bitmap(b"")) == 0xFF
    assert badge.command(ng.encode_start_streaming()) == 0
    full = Bitmap.from_strings(["#" * 44] * 11).to_columns()
    assert badge.command(ng.encode_stream_bitmap(full)) == 0
    off = (code.VIEW_COLS - anim.FRAME_COLS) // 2
    assert all(code.shown[off : off + 44])
    assert not any(code.shown[:off])
    assert badge.command(ng.encode_stop_streaming()) == 0
    assert not badge.streaming


@pytest.mark.parametrize(("level", "color"), [(0, 0x3F0000), (1, 0x7F0000), (3, 0xFF0000)])
def test_brightness(code, badge, level, color):
    assert badge.command(ng.encode_set_brightness(level)) == 0
    assert code.palette[1] == color


def test_command_errors(badge):
    assert badge.command(b"\x08\x01\x04") == 0xFE  # brightness out of range
    assert badge.command(b"\x08\x07") == 0xFF  # unknown misc sub-command
    assert badge.command(b"\x01") == 0xFE  # power_setting without parameter
    assert badge.command(b"\x04\x09") == 0xFF  # unknown ble_setting
    assert badge.command(b"\x05\x31\x01\x0b") == 0xFF  # splash too wide
    assert badge.command(b"\x05\x08") == 0xFC  # splash header incomplete
    assert badge.command(b"\x09") is None  # unknown command: no notification


def test_config_save_and_load(code, badge):
    assert badge.command(ng.encode_set_name("Desk badge")) == 0
    assert badge.command(ng.encode_set_always_on(True)) == 0
    assert badge.command(ng.encode_set_brightness(0)) == 0
    assert badge.command(ng.encode_set_splash_speed(50)) == 0
    assert badge.command(ng.encode_set_splash(8, 2, 11, b"\xff\x81")) == 0
    assert badge.command(ng.encode_save_config()) == 0

    saved = code.store.Config("unused", 1)
    assert saved.load()
    assert (saved.name, saved.always_on, saved.brightness) == ("Desk badge", True, 0)
    assert (saved.splash_speed, saved.splash_w, saved.splash_h, saved.splash_bits) == (50, 8, 2, b"\xff\x81")

    assert badge.command(ng.encode_load_defaults()) == 0
    assert badge.cfg.name == "LED Badge Magic"


def test_reset_after_upload(code, badge, frame):
    import microcontroller

    assert badge.command(ng.encode_set_reset_after_upload(True)) == 0
    microcontroller.reset.reset_mock()
    badge.uploaded(frame)
    microcontroller.reset.assert_called_once()


def test_power_commands_are_deferred(badge):
    assert badge.command(ng.encode_reboot()) == 0
    assert badge.pending_action == "reboot"
    assert badge.command(ng.encode_power_off()) == 0
    assert badge.pending_action == "poweroff"
