"""The face, drawn by us.

Why this file exists: the widget is supposed to be text on the desktop with no box at all,
and a browser page cannot do that on this machine.  Colour-keying the window was the plan --
one exact colour in the page made transparent by Windows, so the desktop shows through the
gaps -- and it is measured dead: WebView2 composites its own output, so every pixel of the
window stays painted whatever key is set, on the frame or on any of its child windows.  A
transparent page does not help either; a pywebview window has no alpha of its own.

So the face is drawn here instead, into a 32-bit image, and the host hands that image to
Windows as a *per-pixel alpha* layered window (UpdateLayeredWindow).  That is exactly what a
Rainmeter skin is: the window is only the pixels we paint, at the alpha we paint them, and
the desktop shows through everywhere else -- including between the glyphs, because the glyph
edges are antialiased against nothing.

Three consequences worth knowing:

* Only painted pixels are hit-tested, so nothing is clickable except the text.  To keep the
  text draggable, the block it occupies carries an alpha of 1 (0.4% -- Rainmeter's
  SolidColor=0,0,0,1 trick), which no one can see but the mouse can feel.
* There is no CSS, no page and no bridge: the family's look is reproduced here with a font
  file and a shadow, and the host re-renders on the minute.
* The ink follows the wallpaper, as before, and the halo flips with it, or dark ink on a dark
  wallpaper would sit in a black smudge.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

#: Fonts to try, best first.  Bahnschrift is the family's display face and ships with Windows;
#: the fallbacks are only here so that a machine without it still draws something sane.
DISPLAY_FONTS = ("bahnschrift.ttf", "seguisb.ttf", "segoeui.ttf")
TEXT_FONTS = ("segoeui.ttf", "segoeuib.ttf", "arial.ttf")
ICON_FONTS = ("segmdl2.ttf", "seguisym.ttf")

#: The gear, from Segoe MDL2 Assets.
GEAR = "\ue713"

#: Layout, in CSS pixels; the host converts by the monitor's scale factor.
PAD_X = 15.0
PAD_Y = 12.0
GAP = 1.0
GEAR_SIZE = 13.0
GEAR_INSET = 5.0
#: The draggable block extends this far past the text on every side.
HIT_PAD = 6.0
#: The resize handle: a square in the bottom-right corner of the window, sized in CSS pixels.
#: It is always present, at the same place, because a handle that only exists while the pointer
#: is over the widget is a handle that is not there when you reach for it -- measured, and the
#: resize then does nothing at all.
CORNER_CSS = 20.0
#: Alpha of the draggable block: one step out of 255.  Rainmeter's SolidColor trick, and the
#: reason the widget can be moved without a visible surface.
HIT_ALPHA = 1

#: Time: 16% of the width, clamped like the CSS did; date: 6.2%.
TIME_FRACTION = 0.16
TIME_MIN, TIME_MAX = 24.0, 42.0
DATE_FRACTION = 0.062
DATE_MIN, DATE_MAX = 11.0, 14.0

INK = {
    #: ink colour, second line, halo colour, second-line alpha, hover alpha
    "light": ((255, 255, 255, 255), (255, 255, 255, 224), (0, 0, 0), 214),
    "dark": ((15, 21, 28, 255), (15, 21, 28, 230), (255, 255, 255), 235),
}


@dataclass(frozen=True)
class Render:
    """One finished face: the pixels, and where the mouse-sensitive parts of it are."""

    #: Premultiplied BGRA, ready for UpdateLayeredWindow.
    pixels: bytes
    width: int
    height: int
    #: Bounding boxes in image pixels: the block that drags, and the gear that clicks.
    hit: tuple[int, int, int, int]
    corner: tuple[int, int, int, int]
    gear: tuple[int, int, int, int] | None

    def in_hit(self, x: int, y: int) -> bool:
        return _inside(self.hit, x, y)

    def in_corner(self, x: int, y: int) -> bool:
        return _inside(self.corner, x, y)

    def in_gear(self, x: int, y: int) -> bool:
        return self.gear is not None and _inside(self.gear, x, y)


def _inside(box: tuple[int, int, int, int], x: int, y: int) -> bool:
    left, top, right, bottom = box
    return left <= x < right and top <= y < bottom


def _clamp(low: float, value: float, high: float) -> float:
    return max(low, min(high, value))


def _font(candidates: tuple[str, ...], size: float) -> ImageFont.FreeTypeFont:
    """The first installed font from the list, or PIL's built-in face as a last resort."""
    for name in candidates:
        path = Path(r"C:\Windows\Fonts") / name
        if path.exists():
            try:
                return ImageFont.truetype(str(path), int(round(size)))
            except OSError:
                continue
    return ImageFont.load_default(int(round(size)))


def measure(
    time_text: str, date_text: str, width: int, height: int, scale: float
) -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont, tuple[float, float, float, float]]:
    """Work out the two sizes and the text block's box, without drawing anything.

    Split out from render() because the host needs the same numbers to size the window to the
    text when it is asked to fit, and two copies of this arithmetic would drift apart.
    """
    width_css = width / scale
    time_size = _clamp(TIME_MIN, width_css * TIME_FRACTION, TIME_MAX)
    date_size = _clamp(DATE_MIN, width_css * DATE_FRACTION, DATE_MAX)
    time_font = _font(DISPLAY_FONTS, time_size * scale)
    date_font = _font(TEXT_FONTS, date_size * scale)

    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    time_box = probe.textbbox((0, 0), time_text, font=time_font)
    date_box = probe.textbbox((0, 0), date_text, font=date_font)
    time_h = time_box[3] - time_box[1]
    date_h = date_box[3] - date_box[1]
    block_h = time_h + GAP * scale + date_h
    left = PAD_X * scale
    top = max(PAD_Y * scale, (height - block_h) / 2)
    width_needed = max(
        time_box[2] - time_box[0], date_box[2] - date_box[0]
    ) + 2 * left
    height_needed = block_h + 2 * PAD_Y * scale
    return time_font, date_font, (left, top, width_needed, height_needed)


def fit_size(
    time_text: str, date_text: str, width: int, scale: float
) -> tuple[int, int]:
    """The window size that exactly holds the two lines at the given width."""
    _, _, (_, _, needed_w, needed_h) = measure(time_text, date_text, width, 2000, scale)
    return int(round(needed_w)), int(round(needed_h))


def render(
    time_text: str,
    date_text: str,
    ink: str,
    width: int,
    height: int,
    scale: float = 1.0,
    hover: bool = False,
) -> Render:
    """Draw one face.

    The image is transparent except for the glyphs, the gear and the one-step draggable
    block, so whatever the host puts on screen is the widget: no background, no rim, no edge.
    """
    colours = INK.get(ink, INK["light"])
    (ink_rgba, soft_rgba, halo_rgb, soft_alpha) = colours
    time_font, date_font, (left, top, _, _) = measure(
        time_text, date_text, width, height, scale
    )

    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    time_xy = (left, top)
    time_box = draw.textbbox(time_xy, time_text, font=time_font)
    date_xy = (left, time_box[3] + GAP * scale)
    date_box = draw.textbbox(date_xy, date_text, font=date_font)

    # The halo first, on its own layer, so blurring it does not soften the glyphs.  It is the
    # only thing standing between the text and a busy patch of wallpaper.
    halo = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    halo_draw = ImageDraw.Draw(halo)
    halo_draw.text(time_xy, time_text, font=time_font, fill=(*halo_rgb, 150))
    halo_draw.text(date_xy, date_text, font=date_font, fill=(*halo_rgb, 110))
    image = Image.alpha_composite(
        image, halo.filter(ImageFilter.GaussianBlur(max(1.0, 2.0 * scale)))
    )
    draw = ImageDraw.Draw(image)
    draw.text(time_xy, time_text, font=time_font, fill=(*ink_rgba[:3], 255))
    if hover:
        soft_alpha = min(255, soft_alpha + 26)
    draw.text(date_xy, date_text, font=date_font, fill=(*soft_rgba[:3], soft_alpha))

    # The gear, only while the pointer is over the widget: at rest the widget is two lines of
    # text and nothing else.
    gear_box: tuple[int, int, int, int] | None = None
    if hover:
        gear_font = _font(ICON_FONTS, GEAR_SIZE * scale)
        inset = GEAR_INSET * scale
        half = GEAR_SIZE * scale / 2
        centre = (width - inset - half, inset + half)
        gear_draw = ImageDraw.Draw(image)
        gear_box_text = gear_draw.textbbox(
            centre, GEAR, font=gear_font, anchor="mm"
        )
        gear_draw.text(
            centre,
            GEAR,
            font=gear_font,
            fill=(*ink_rgba[:3], 210),
            anchor="mm",
            stroke_width=max(0, int(round(0.6 * scale))),
            stroke_fill=(*halo_rgb, 130),
        )
        gear_box = tuple(int(round(v)) for v in gear_box_text)  # type: ignore[assignment]

    # The draggable block: the text with a little room to breathe, at an alpha of one step.
    hit = (
        max(0, int(time_box[0] - HIT_PAD * scale)),
        max(0, int(time_box[1] - HIT_PAD * scale)),
        min(width, int(max(time_box[2], date_box[2]) + HIT_PAD * scale)),
        min(height, int(date_box[3] + HIT_PAD * scale)),
    )
    if gear_box is not None:
        hit = (
            min(hit[0], gear_box[0]),
            min(hit[1], gear_box[1]),
            max(hit[2], gear_box[2]),
            max(hit[3], gear_box[3]),
        )
    # Raised to one step inside that block and left at zero outside it: the pixels really do
    # belong to the desktop there, so a click on them has to reach it.  A one-step block over
    # the whole window would be invisible but would swallow every click in the rectangle -- an
    # invisible box, which is the thing this design exists to avoid.
    corner_size = max(8, int(round(CORNER_CSS * scale)))
    corner = (
        max(0, width - corner_size),
        max(0, height - corner_size),
        width,
        height,
    )
    block = Image.new("L", (width, height), 0)
    block_draw = ImageDraw.Draw(block)
    block_draw.rectangle((hit[0], hit[1], hit[2] - 1, hit[3] - 1), fill=HIT_ALPHA)
    block_draw.rectangle(
        (corner[0], corner[1], corner[2] - 1, corner[3] - 1), fill=HIT_ALPHA
    )
    image.putalpha(ImageChops.lighter(image.getchannel("A"), block))

    premultiplied = image.convert("RGBa")
    red, green, blue, premultiplied_alpha = premultiplied.split()
    # UpdateLayeredWindow wants BGRA, premultiplied, top-down: PIL's "RGBa" is the premultiplied
    # one, so only the channel order is left to fix.
    bgra = Image.merge("RGBa", (blue, green, red, premultiplied_alpha))
    return Render(
        pixels=bgra.tobytes(),
        width=width,
        height=height,
        hit=hit,
        corner=corner,
        gear=gear_box,
    )
