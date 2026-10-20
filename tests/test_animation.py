"""Animation modes, flash, marquee and the matrix renderer."""

import pytest
from badgemagic_ble.render import render_text
from conftest import restart, run

import badge_anim as anim


def text_columns(text):
    bitmap = render_text(text)
    chunks = bitmap.to_column_chunks()
    return anim.decode_chunks(chunks, 0, len(chunks) // anim.ROWS)


def rows_on_matrix(code):
    """The badge rows as ASCII, read back from the matrix bitmap."""
    return [
        "".join("#" if code.bitmap.px[code.Y0 + r * code.PIXEL_H][x] else "." for x in range(code.MATRIX_WIDTH))
        for r in range(anim.ROWS)
    ]


@pytest.mark.parametrize("mode", range(9))
def test_every_mode_completes_a_cycle(badge, clock, mode):
    cols = text_columns("Hi 42!")
    restart(badge, clock, [anim.Message(list(cols), mode, 7), anim.Message(list(cols), anim.FIXED, 7)])
    assert run(badge, clock, 1500) > 0


@pytest.mark.parametrize("mode", range(9))
def test_modes_with_long_messages(badge, clock, mode):
    cols = text_columns("This message spans several badge frames")
    assert len(cols) > 2 * anim.FRAME_COLS
    restart(badge, clock, [anim.Message(list(cols), mode, 7, flash=True, marquee=True)])
    run(badge, clock, 400)


def test_fixed_is_centered(code, badge, clock):
    cols = text_columns("Hi")
    restart(badge, clock, [anim.Message(list(cols), anim.FIXED, 4)])
    run(badge, clock, 3, dt_ns=300_000_000)
    rows = rows_on_matrix(code)
    lit = [x for x in range(code.MATRIX_WIDTH) if any(row[x] == "#" for row in rows)]
    left, right = lit[0], code.MATRIX_WIDTH - 1 - lit[-1]
    assert abs(left - right) <= 1


def test_scroll_left_enters_from_the_right(code, badge, clock):
    restart(badge, clock, [anim.Message(text_columns("I"), anim.SCROLL_LEFT, 7)])
    run(badge, clock, 10, dt_ns=100_000_000)
    rows = rows_on_matrix(code)
    lit = [x for x in range(code.MATRIX_WIDTH) if any(row[x] == "#" for row in rows)]
    assert lit
    assert min(lit) > code.MATRIX_WIDTH // 2


def test_step_timing_follows_speed(badge, clock):
    cols = text_columns("Hi")
    slow = anim.Message(list(cols), anim.SCROLL_LEFT, 0)
    restart(badge, clock, [slow])
    run(badge, clock, 100, dt_ns=10_000_000)  # 1 s at 200 ms/step, after the 500 ms start delay
    assert 2 <= slow.anim_step <= 4
    fast = anim.Message(list(cols), anim.SCROLL_LEFT, 7)
    restart(badge, clock, [fast])
    run(badge, clock, 200, dt_ns=5_000_000)  # 25 ms/step
    assert 18 <= fast.anim_step <= 22


def test_flash_blanks_alternate_steps():
    fb = [0x7FF] * 4
    anim.flash(fb, 1)
    assert fb == [0x7FF] * 4
    anim.flash(fb, 2)
    assert fb == [0] * 4


def test_marquee_draws_a_border():
    fb = [0] * 44
    anim.marquee(fb, 0)
    assert any(v & 1 for v in fb)
    assert any(v & (1 << 10) for v in fb)
    assert fb[0] and fb[-1]


def test_sequential_playback_and_transition(badge, clock):
    cols = text_columns("Hi")
    restart(badge, clock, [anim.Message(list(cols), anim.ANIMATION, 7), anim.Message(list(cols), anim.ANIMATION, 7)])
    assert run(badge, clock, 50) > 0
    badge.transition()  # loop the first message only
    assert not badge.sequential and badge.current == 0
    assert run(badge, clock, 50) == 0
    badge.transition()
    assert badge.current == 1
    badge.transition()
    assert badge.current == 0 and badge.sequential


def test_splash_plays(code, badge):
    badge.cfg.splash_w, badge.cfg.splash_h, badge.cfg.splash_speed = 8, 2, 0
    badge.cfg.splash_bits = b"\xff\x81"
    badge.play_splash()
    assert not any(code.shown)  # scrolled out the top


def test_default_message_without_upload(badge):
    assert len(badge.messages) == 1
    assert badge.messages[0].mode == anim.SCROLL_LEFT
