# SPDX-License-Identifier: MIT
"""Badge Magic animations, ported from badgemagic-firmware src/animation.c.

A frame buffer is a list of columns. Each column is an int whose bit y is
row y (bit 0 = top row), exactly like the firmware's uint16_t fb[]. Values
may carry stray bits above row 10; the renderer masks them off.

Frame-based modes (fixed, animation, snowflake, picture, laser, scroll
up/down) split a message into FRAME_COLS-wide frames, because that is how
apps lay out multi-frame content for a 44-column badge. The horizontal
scroll modes scroll across however many columns the display shows.
"""

ROWS = 11
FRAME_COLS = 44
MASK = (1 << ROWS) - 1

SCROLL_LEFT = 0
SCROLL_RIGHT = 1
SCROLL_UP = 2
SCROLL_DOWN = 3
FIXED = 4
ANIMATION = 5
SNOWFLAKE = 6
PICTURE = 7
LASER = 8

_ANIMATION_STEPS = 5
_FIXED_STEPS = FRAME_COLS
_MARQUEE_TPL = 0b000100010001


class Message:
    """One stored message: its columns plus mode, speed, flash and marquee."""

    def __init__(self, columns, mode=SCROLL_LEFT, speed=4, flash=False, marquee=False):
        self.buf = columns
        self.width = len(columns)
        self.mode = mode if 0 <= mode <= LASER else SCROLL_LEFT
        self.speed = speed  # firmware level 0 (slowest) .. 7 (fastest)
        self.flash = flash
        self.marquee = marquee
        self.anim_step = 0
        self.frames = max(1, (self.width + FRAME_COLS - 1) // FRAME_COLS)
        self.x_offset = self._x_offset()

    def _x_offset(self):
        # get_x_offset(): center a single frame that is narrower than the badge.
        if self.frames > 1:
            return 0
        content = 0
        for k in range(min(FRAME_COLS, self.width)):
            if self.buf[k]:
                content = k + 1
        return (FRAME_COLS - content) // 2 if content < FRAME_COLS else 0


def _shift(reg, n):
    # SIGNED_SHIFT: right on positive n, left on negative n.
    return reg >> n if n >= 0 else reg << -n


# horizontal scroll -----------------------------------------------------------


def _scroll_x(bm, fb, ncols, right):
    total = bm.width + ncols
    x = bm.anim_step % total - ncols
    bm.anim_step += -1 if right else 1
    buf, width = bm.buf, bm.width
    for i in range(ncols):
        j = i + x
        fb[i] = buf[j] if 0 <= j < width else 0
    return bm.anim_step % total


# vertical scroll -------------------------------------------------------------


def _shift_y(bm, fb, y, frame):
    start = frame * FRAME_COLS
    xo = bm.x_offset
    buf = bm.buf
    for j in range(xo):
        fb[j] = 0
    size = min(FRAME_COLS - xo, bm.width - start)
    j = xo
    for i in range(size):
        fb[j] = _shift(buf[start + i], y)
        j += 1
    for k in range(j, FRAME_COLS):
        fb[k] = 0


def _still(bm, fb, frame):
    _shift_y(bm, fb, 0, frame)


def _scroll_y(bm, fb, down):
    frame_steps = ROWS * 3  # in, still, out
    total = frame_steps * bm.frames
    frame = (bm.anim_step % total) // frame_steps
    y = bm.anim_step % frame_steps
    if y < ROWS:
        _shift_y(bm, fb, y - ROWS, frame)
    elif y < ROWS * 2:
        _shift_y(bm, fb, 0, frame)
    else:
        _shift_y(bm, fb, y - ROWS * 2, frame)
    bm.anim_step += -1 if down else 1
    return bm.anim_step % total


# laser -----------------------------------------------------------------------


def _laser_in(bm, fb, step, frame, xo):
    content = FRAME_COLS - xo
    c = step % content
    start = frame * FRAME_COLS
    buf, width = bm.buf, bm.width
    for i in range(xo):
        fb[i] = 0
    for i in range(xo, xo + c):
        src = start + i - xo
        fb[i] = buf[src] if src < width else 0
    limit = xo + min(content, width - start)
    beam = buf[start + c] if start + c < width else 0
    for i in range(xo + c, limit):
        fb[i] = beam
    for i in range(max(limit, xo + c), FRAME_COLS):
        fb[i] = 0


def _laser_out(bm, fb, step, frame, xo):
    content = FRAME_COLS - xo
    c = step % content
    start = frame * FRAME_COLS
    buf, width = bm.buf, bm.width
    for i in range(xo + c):
        fb[i] = 0
    limit = xo + min(content, width - start)
    for i in range(xo + c, limit):
        src = start + i - xo
        fb[i] = buf[src] if src < width else 0
    for i in range(max(limit, xo + c), FRAME_COLS):
        fb[i] = 0


def _laser(bm, fb):
    frame_steps = FRAME_COLS * 3
    total = frame_steps * bm.frames
    frame = (bm.anim_step % total) // frame_steps
    xo = bm.x_offset
    c = bm.anim_step % frame_steps
    bm.anim_step += 1
    if c < FRAME_COLS:
        _laser_in(bm, fb, c - FRAME_COLS, frame, xo)
    elif c < FRAME_COLS * 2:
        _still(bm, fb, frame)
    else:
        _laser_out(bm, fb, c - FRAME_COLS * 2, frame, xo)
    return bm.anim_step % total


# snowflake -------------------------------------------------------------------


def _dilate(w, frm, to):
    # b16dialate(): spread bits frm..to-1 two rows apart.
    ret = 0
    for i in range(frm):
        ret |= w & (1 << i)
    j = frm
    for i in range(frm, to):
        ret |= ((w >> i) & 1) << j
        j += 2
    for i in range(to, 16):
        ret |= ((w >> i) & 1) << j
        j += 1
    return ret


def _snowflake_in(bm, fb, step, frame, xo):
    y = step % (ROWS * 2) - ROWS
    start = frame * FRAME_COLS
    buf = bm.buf
    for i in range(xo):
        fb[i] = 0
    size = min(FRAME_COLS - xo, bm.width - start)
    i = xo
    for k in range(size):
        v = buf[start + k]
        if y < 0:
            fb[i] = _shift(_dilate(v, 0, ROWS), ROWS - y)
        else:
            fb[i] = _shift(_dilate(v, 0, ROWS - y), ROWS - y)
        i += 1
    for k in range(i, FRAME_COLS):
        fb[k] = 0


def _snowflake_out(bm, fb, step, frame, xo):
    y = step % (ROWS * 2) - ROWS
    start = frame * FRAME_COLS
    buf = bm.buf
    for i in range(xo):
        fb[i] = 0
    size = min(FRAME_COLS - xo, bm.width - start)
    i = xo
    for k in range(size):
        v = buf[start + k]
        if y <= 0:
            fb[i] = _shift(_dilate(v, 0, ROWS), y)
        else:
            fb[i] = _dilate(v, y, ROWS)
        i += 1
    for k in range(i, FRAME_COLS):
        fb[k] = 0


def _snowflake(bm, fb):
    frame_steps = ROWS * 6
    total = frame_steps * bm.frames
    frame = (bm.anim_step % total) // frame_steps
    xo = bm.x_offset
    c = bm.anim_step % frame_steps
    bm.anim_step += 1
    if c < ROWS * 2:
        _snowflake_in(bm, fb, c - ROWS * 2, frame, xo)
    elif c <= ROWS * 4:
        _still(bm, fb, frame)
    else:
        _snowflake_out(bm, fb, -(c - ROWS * 4), frame, xo)
    return bm.anim_step % total


# fixed / animation -----------------------------------------------------------


def _stepped(bm, fb, frame_steps):
    total = frame_steps * bm.frames
    frame = (bm.anim_step % total) // frame_steps
    bm.anim_step += 1
    _still(bm, fb, frame)
    return bm.anim_step % total


# picture ---------------------------------------------------------------------


def _picture(bm, fb, step, frame):
    hc = FRAME_COLS // 2
    rng = (step - 1) % FRAME_COLS
    if rng > hc:
        _still(bm, fb, frame)
        return
    off = frame * FRAME_COLS
    buf, width = bm.buf, bm.width
    i = 0
    while i <= rng:
        a = hc + i - 1
        b = hc - i
        fb[a] = buf[a + off] if a + off < width else 0
        fb[b] = buf[b + off] if b + off < width else 0
        i += 1
    if i >= hc:
        return
    fb[hc + i - 1] = MASK
    fb[hc - i] = MASK


def _picture_out(fb, step):
    hc = FRAME_COLS // 2
    if step > hc:
        return
    i = 0
    while i <= step:
        fb[hc + i - 1] = 0
        fb[hc - i] = 0
        i += 1
    if i >= hc:
        return
    fb[hc + i - 1] = MASK
    fb[hc - i] = MASK


def _picture_mode(bm, fb):
    last_steps = FRAME_COLS // 2
    frame_steps = FRAME_COLS
    frames = bm.frames + 1
    total = frame_steps * frames - last_steps
    frame = (bm.anim_step % (frame_steps * frames)) // frame_steps
    bm.anim_step += 1
    if frame == frames - 1:
        _picture_out(fb, bm.anim_step % FRAME_COLS)
        if bm.anim_step % FRAME_COLS >= last_steps:
            bm.anim_step = 0
            return 0
        return bm.anim_step % total
    _picture(bm, fb, bm.anim_step, frame)
    return bm.anim_step % total


# public ----------------------------------------------------------------------


def step(bm, frame_fb, view_fb):
    """Advance ``bm`` one step and draw it into ``view_fb``.

    ``frame_fb`` is a persistent FRAME_COLS-wide scratch buffer (picture mode
    relies on it keeping its contents between steps). Returns the number of
    steps until the animation restarts; 0 means it just finished a cycle.
    """
    mode = bm.mode
    if mode in (SCROLL_LEFT, SCROLL_RIGHT):
        return _scroll_x(bm, view_fb, len(view_fb), mode == SCROLL_RIGHT)
    if mode in (SCROLL_UP, SCROLL_DOWN):
        left = _scroll_y(bm, frame_fb, mode == SCROLL_DOWN)
    elif mode == FIXED:
        left = _stepped(bm, frame_fb, _FIXED_STEPS)
    elif mode == ANIMATION:
        left = _stepped(bm, frame_fb, _ANIMATION_STEPS)
    elif mode == SNOWFLAKE:
        left = _snowflake(bm, frame_fb)
    elif mode == PICTURE:
        left = _picture_mode(bm, frame_fb)
    else:
        left = _laser(bm, frame_fb)
    place(frame_fb, view_fb)
    return left


def place(frame_fb, view_fb):
    """Copy a FRAME_COLS-wide frame into the view, centered (cropped if narrower)."""
    n = len(view_fb)
    off = (n - FRAME_COLS) // 2
    for x in range(n):
        src = x - off
        view_fb[x] = frame_fb[src] if 0 <= src < FRAME_COLS else 0


def marquee(fb, step):
    """ani_marque(): a dotted border running around the frame."""
    n = len(fb)
    top_bottom = ~((1 << (ROWS - 1)) | 1)
    for i in range(n - 1):
        v = fb[i] & top_bottom
        if _MARQUEE_TPL & (1 << ((step + i) % 4)):
            v |= 1
        if _MARQUEE_TPL & (1 << ((-step + i - 2) % 4)):
            v |= 1 << (ROWS - 1)
        fb[i] = v
    fb[0] = _MARQUEE_TPL << (step % 4)
    fb[n - 1] = _MARQUEE_TPL >> ((step + 3) % 4)


def flash(fb, step):
    """ani_flash(): blank every other flash step."""
    if not step % 2:
        for i in range(len(fb)):
            fb[i] = 0


def decode_chunks(data, start, chunks):
    """Legacy bitmap (11-byte chunks of 8 columns, MSB leftmost) to columns."""
    cols = [0] * (chunks * 8)
    for c in range(chunks):
        base = start + c * ROWS
        x0 = c * 8
        for row in range(ROWS):
            b = data[base + row]
            if not b:
                continue
            bit = 1 << row
            for i in range(8):
                if b & (0x80 >> i):
                    cols[x0 + i] |= bit
    return cols


def xbm_window(bits, width, height, top, fb):
    """Draw rows top..top+10 of an XBM (LSB leftmost) centered into a FRAME_COLS fb."""
    stride = (width + 7) // 8
    xo = (FRAME_COLS - width) // 2
    for x in range(FRAME_COLS):
        fb[x] = 0
    for r in range(ROWS):
        y = top + r
        if not 0 <= y < height:
            continue
        row = y * stride
        for x in range(width):
            if bits[row + (x >> 3)] & (1 << (x & 7)):
                col = x + xo
                if 0 <= col < FRAME_COLS:
                    fb[col] |= 1 << r
