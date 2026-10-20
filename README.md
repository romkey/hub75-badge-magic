# hub75-badge-magic

[![CI](https://github.com/romkey/hub75-badge-magic/actions/workflows/ci.yml/badge.svg)](https://github.com/romkey/hub75-badge-magic/actions/workflows/ci.yml)
[![Lint](https://github.com/romkey/hub75-badge-magic/actions/workflows/lint.yml/badge.svg)](https://github.com/romkey/hub75-badge-magic/actions/workflows/lint.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CircuitPython](https://img.shields.io/badge/CircuitPython-9.x%20%7C%2010.x-blueviolet.svg?logo=python&logoColor=white)](https://circuitpython.org/board/adafruit_matrixportal_s3/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

Turns an [Adafruit Matrix Portal S3](https://www.adafruit.com/product/5778) with a
64x32 HUB75 panel into a Bluetooth LE **Badge Magic** LED name badge. It speaks the
same protocol as an 11x44 badge running the FOSSASIA
[open firmware](https://github.com/fossasia/badgemagic-firmware), so these all work
with it:

- the Badge Magic phone apps
- the `badgemagic` command-line tool and the
  [Home Assistant integration](https://github.com/romkey/hass-badgemagic)

## What it does

| Protocol | Supported |
| --- | --- |
| Legacy upload (service `FEE0`, char `FEE1`) | Up to 8 messages, all 9 modes (scroll left/right/up/down, fixed, animation, snowflake, picture, laser), speed 1–8, flash, marquee, and the clock timestamp |
| Next-gen control (service `F055`, write `F057`, notify `F056`) | Streaming, brightness 0–3, always-on flag, rename, splash screen and splash speed, save/load config, reset-after-upload, reboot, power off |
| Battery (`180F`) / Device Information (`180A`) | Battery always reads 100% |

The animations are a line-by-line port of the firmware's `animation.c`, including
its timings: each step takes 200 ms minus 25 ms per speed level, the marquee moves
every 100 ms, and flash toggles every 500 ms. Messages play one after another,
just like the badge does.

Each badge LED is drawn as a block of `BADGE_PIXEL_W` x `BADGE_PIXEL_H` matrix
pixels (1x2 by default). That makes the panel show 64 badge columns in a 64x22
band. The scroll-left and scroll-right modes use the full width. The frame-based
modes (fixed, animation, picture, and the rest) and streamed frames use the
badge's native 44 columns, centered, because apps lay out their frames 44
columns wide.

The last upload is stored in `microcontroller.nvm`, so it survives a reset, just
as a badge keeps its messages in flash. Uploads too big to fit in NVM (about 7.8 KB)
still play but are lost on reset. As on the badge, configuration changes stay in
RAM until a `save_cfg` command.

## Install

1. Install CircuitPython 9.x or 10.x for the Matrix Portal S3.
2. Install the libraries in [requirements.txt](requirements.txt) with
   [circup](https://github.com/adafruit/circup), with the board plugged in:

   ```bash
   circup install -r requirements.txt
   ```

   Or copy `adafruit_ble/` from the
   [library bundle](https://circuitpython.org/libraries) to `CIRCUITPY/lib/` by hand.
3. Copy `code.py`, `badge_anim.py` and `badge_store.py` to `CIRCUITPY/`.
4. Optionally copy `settings.toml.example` to `CIRCUITPY/settings.toml` and edit it
   to set the color, pixel scale, name, starting brightness or PIN.

On boot it shows any saved splash screen. Then it plays the stored messages, or
a scrolling "BADGE MAGIC" if none are stored, and advertises as
`LED Badge Magic`.

## Try it

```bash
uv tool install "git+https://github.com/romkey/hass-badgemagic#subdirectory=badgemagic-ble"
```

```bash
badgemagic send --text HELLO --mode laser --speed 6 --marquee
```

```bash
badgemagic stream-text "Streaming works" --fps 10
```

```bash
badgemagic set-brightness 3 --save
```

For Home Assistant, the integration discovers it like a badge (it matches
service `FEE0`). With no pairing mode to enter, the board is always reachable.

## Buttons

- **UP**: the badge's KEY1. It switches between looping the current message and
  playing all of them; each further press moves to the next message.
- **DOWN**: steps through the brightness levels. This isn't saved; send
  `save_cfg` to keep a level.

## How it differs from a real badge

- **Always advertising.** There is no BT-PAIRING menu, and the board never turns
  Bluetooth off after an upload. New messages play right away, with no
  "BLUETOOTH OFF" screen. The always-on setting is stored and saved, but it has no
  effect.
- **No ATT errors.** CircuitPython can't fail a write with a custom ATT error.
  So an unknown next-gen command gets no result notification, and the client
  times out instead of reporting `UnknownCommandError`. A missing or wrong PIN
  drops the upload silently instead of raising `PinRequired`/`PinRejected`.
- **Fixed PIN.** The PIN comes from `settings.toml`; a badge makes up a new one
  each time it enters pairing mode.
- **Power off** stops advertising, blanks the panel and deep-sleeps the board.
  Press UP (or reset) to wake it.
- **Brightness** scales the color: levels 0–3 give 25/50/75/100%, matching the
  badge's PWM duty cycles.
- Battery is always 100%, and the splash frame height is stored but unused,
  just as the firmware ignores it.

## Files

- `code.py`: display setup, the BLE services, upload reassembly, next-gen
  commands and the main loop
- `badge_anim.py`: the animation port, plus the bitmap decoders
- `badge_store.py`: config and the last upload in NVM

## Development

The tests run on desktop Python 3.13 with the CircuitPython hardware modules
stubbed out. They check uploads and next-gen commands against the encoders in
[badgemagic-ble](https://github.com/romkey/hass-badgemagic/tree/main/badgemagic-ble).

```bash
uv sync
```

```bash
uv run pytest
```

```bash
uv run ruff check . && uv run ruff format --check .
```

CI runs the tests and ruff. It also compiles the three program files with
CircuitPython's own `mpy-cross` for 9.x and 10.x, which catches syntax that
CPython accepts but CircuitPython does not.

## License

MIT; see [LICENSE](LICENSE).

## Status

Not yet tested on hardware. If writes longer than 20 bytes, such as stream
frames, fail, the ATT MTU negotiation is the first thing to check.
