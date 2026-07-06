"""GTM Engine onboarding splash — full-width, thin, centered.

Stretches a cyan->blue->purple gradient across the whole terminal width,
with slim half-height bars top and bottom and the wordmark centered between
blue caps. Pure standard library. Truecolor when supported, 256-color
fallback, plain text when piped or NO_COLOR is set.

Run:  python banner.py
"""
import os
import shutil
import sys

# gradient endpoints: cyan (left) -> blue -> purple (right)
C0 = (90, 220, 255)
C1 = (150, 80, 250)

RAMP_256 = [51, 45, 39, 33, 27, 57, 93, 99]  # cyan->blue->purple-ish fallback

WORDMARK = "G T M   E N G I N E   ·   O S"
TAGLINE = "the GTM stack you actually own  ·  let's get you set up →"

CAP = 6          # width of the blue caps on the mid row
MAX_WIDTH = 120  # don't stretch wider than this even on huge terminals


def _supports_color():
    if os.environ.get("NO_COLOR") is not None:
        return None
    if not sys.stdout.isatty():
        return None
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return "truecolor"
    return "256"


def _lerp(a, b, t):
    return int(round(a + (b - a) * t))


def _rgb(t):
    return (_lerp(C0[0], C1[0], t), _lerp(C0[1], C1[1], t), _lerp(C0[2], C1[2], t))


def build_rows(width):
    """Return 3 rows; each row is a list of (char, rgb_or_None) cells."""
    n = max(width - 1, 1)
    top = [("▄", _rgb(i / n)) for i in range(width)]
    bot = [("▀", _rgb(i / n)) for i in range(width)]

    mid = [(" ", None)] * width
    for i in range(min(CAP, width)):
        mid[i] = ("█", _rgb(i / n))
        mid[width - 1 - i] = ("█", _rgb((width - 1 - i) / n))
    start = (width - len(WORDMARK)) // 2
    for j, ch in enumerate(WORDMARK):
        pos = start + j
        if 0 <= pos < width and ch != " ":
            mid[pos] = (ch, (255, 255, 255))
    return [top, mid, bot]


def _ansi(rgb, mode, bold=False):
    if rgb == (255, 255, 255):
        return "\033[1;97m" if mode != "truecolor" else "\033[1;38;2;255;255;255m"
    if mode == "truecolor":
        return "\033[38;2;{};{};{}m".format(*rgb)
    # nearest-ish 256 by position is good enough; map by blue/red balance
    idx = RAMP_256[min(int((rgb[0] / 160) * len(RAMP_256)), len(RAMP_256) - 1)]
    return "\033[38;5;{}m".format(idx)


def render():
    cols = shutil.get_terminal_size((80, 24)).columns
    width = min(cols, MAX_WIDTH)
    mode = _supports_color()
    rows = build_rows(width)
    out = ["\n"]
    pad = " " * max((cols - width) // 2, 0)  # center the banner in the terminal
    if mode is None:
        for row in rows:
            out.append(pad + "".join(ch for ch, _ in row) + "\n")
    else:
        reset = "\033[0m"
        for row in rows:
            buf = [pad]
            for ch, rgb in row:
                if ch == " " or rgb is None:
                    buf.append(" ")
                else:
                    buf.append(_ansi(rgb, mode) + ch)
            out.append("".join(buf) + reset + "\n")
    out.append(pad + "  " + TAGLINE + "\n\n")
    return "".join(out)


def main():
    sys.stdout.write(render())


if __name__ == "__main__":
    main()
