"""Pixel-art pictures for the made-up showcase library, drawn with Pillow only (no files, no fonts shipped, no network).

Everything is deterministic: the same game gives the same bytes on every run (random numbers are seeded from the title).
A screenshot is a small 320x180 scene (banded sky, parallax layers, tiles, sprites built from rectangles, a HUD line)
enlarged with nearest-neighbour; a box is a 99x160 scene enlarged four times with a big hero, a smooth title, a band with the
generic system name and an invented "fun rating" seal; a logo is the title with an outline and a shadow on a transparent
canvas. Fonts are looked up on the machine (several names tried); with none found Pillow's built-in font is used, which looks
plainer but works.
"""
from __future__ import annotations

import functools
import hashlib
import io
import math
import random
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ..processing.images import BOX_SIZE, FONT_CANDIDATES, IMAGE_SIZE, LOGO_SIZE, _png_bytes, _wrap, make_logo_png
from .demo_data import SYSTEM_BY_FOLDER, DemoGame

SCREEN = (320, 180)                       # the size of a scene before it is enlarged
BOX_ART = (BOX_SIZE[0] // 4, BOX_SIZE[1] // 4)       # 99 x 160, enlarged four times
LCD_TONES = ((15, 56, 15), (48, 98, 48), (139, 172, 15), (202, 220, 159))

# ------------------------------------------------------------------------------------------------------------ fonts
_SYS = "/System/Library/Fonts/"
_SUP = "/System/Library/Fonts/Supplemental/"
FONT_FILES = {
    # (path or bare name, collection index); the first one that opens wins, Pillow's own font is the last resort
    "chunky": [(_SUP + "Arial Rounded Bold.ttf", 0), (_SUP + "Arial Black.ttf", 0), (_SUP + "Impact.ttf", 0)]
              + [(p, 0) for p in FONT_CANDIDATES],
    "mono": [(_SYS + "Menlo.ttc", 1), (_SUP + "Courier New Bold.ttf", 0),
             ("/usr/share/fonts/dejavu/DejaVuSansMono-Bold.ttf", 0), ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 0),
             ("/usr/share/fonts/liberation/LiberationMono-Bold.ttf", 0), ("DejaVuSansMono-Bold.ttf", 0)]
            + [(p, 0) for p in FONT_CANDIDATES],
}


@functools.lru_cache(maxsize=64)
def font(size: int, kind: str = "chunky"):
    """A bold font of about `size` pixels; never raises (Pillow's built-in font when nothing else is installed)."""
    for path, index in FONT_FILES[kind]:
        try:
            return ImageFont.truetype(path, size, index=index)
        except (OSError, ValueError):
            continue
    try:
        return ImageFont.load_default(size)
    except Exception:                                  # noqa: BLE001 - old Pillow, or no FreeType: the small bitmap font
        try:
            return ImageFont.load_default()
        except Exception:                              # noqa: BLE001
            return ImageFont.load_default_imagefont()


def stroked_text(draw, xy, text, fnt, fill, width: int = 0, stroke_fill=None) -> None:
    """Text with an outline. The bitmap fallback font cannot stroke, so the outline is drawn as shifted copies."""
    if width and not isinstance(fnt, ImageFont.FreeTypeFont):
        for dx in range(-width, width + 1):
            for dy in range(-width, width + 1):
                if dx * dx + dy * dy <= width * width:
                    draw.text((xy[0] + dx, xy[1] + dy), text, font=fnt, fill=stroke_fill)
        draw.text(xy, text, font=fnt, fill=fill)
    elif width:
        draw.text(xy, text, font=fnt, fill=fill, stroke_width=width, stroke_fill=stroke_fill)
    else:
        draw.text(xy, text, font=fnt, fill=fill)


def text_mid(draw, x, y, text, fnt, fill, centre: bool = False) -> None:
    """Text whose vertical middle is at `y` (left edge at `x`, or centred on it): works with every kind of font."""
    box = draw.textbbox((0, 0), text, font=fnt)
    left = x - (box[2] - box[0]) / 2 if centre else x
    draw.text((left - box[0], y - (box[1] + box[3]) / 2), text, font=fnt, fill=fill)


# ------------------------------------------------------------------------------------------------------------ helpers
def rng(*parts) -> random.Random:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def mix(a, b, t: float) -> tuple:
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def shade(c, f: float) -> tuple:
    """f < 1 darker, f > 1 lighter (towards white)."""
    return mix(c, (0, 0, 0), 1 - f) if f < 1 else mix(c, (255, 255, 255), min(1.0, f - 1))


def rotate_hue(c, turns: float) -> tuple:
    import colorsys
    h, s, v = colorsys.rgb_to_hsv(*(x / 255 for x in c))
    return tuple(int(round(x * 255)) for x in colorsys.hsv_to_rgb((h + turns) % 1.0, s, v))


def new(w: int, h: int, fill=(0, 0, 0, 0)) -> Image.Image:
    return Image.new("RGBA", (w, h), fill)


def outline(im: Image.Image, colour, pad: int = 1) -> Image.Image:
    """The sprite with a one-pixel outline (4-neighbour) around everything that is not transparent."""
    big = new(im.width + 2 * pad, im.height + 2 * pad)
    big.alpha_composite(im, (pad, pad))
    a = np.asarray(big.getchannel("A")) > 0
    grown = a.copy()
    grown[1:] |= a[:-1]
    grown[:-1] |= a[1:]
    grown[:, 1:] |= a[:, :-1]
    grown[:, :-1] |= a[:, 1:]
    ring = grown & ~a
    px = np.asarray(big).copy()
    px[ring] = (*colour, 255)
    return Image.fromarray(px, "RGBA")


def banded(draw, x0, y0, x1, y1, top, bottom, bands: int = 9, dither: bool = True) -> None:
    """A retro 'gradient': flat bands, with a two-row checker between neighbours."""
    height = y1 - y0
    for i in range(bands):
        ya, yb = y0 + height * i // bands, y0 + height * (i + 1) // bands
        draw.rectangle([x0, ya, x1 - 1, yb - 1], fill=mix(top, bottom, i / max(1, bands - 1)))
        if dither and i < bands - 1:
            nxt = mix(top, bottom, (i + 1) / (bands - 1))
            for y in (yb - 2, yb - 1):
                for x in range((x0 + y) % 2 + x0, x1, 2):
                    draw.point((x, y), fill=nxt)


def text_px(draw, xy, text, fnt, fill, shadow=None) -> None:
    """Hard-edged text (no anti-aliasing) with an optional one-pixel drop shadow: the 'pixel font' look at art size."""
    draw.fontmode = "1"
    if shadow is not None:
        draw.text((xy[0] + 1, xy[1] + 1), text, font=fnt, fill=shadow)
    draw.text(xy, text, font=fnt, fill=fill)


def enlarge(im: Image.Image, scale: int, scanlines: float = 0.0) -> Image.Image:
    out = im.convert("RGB").resize((im.width * scale, im.height * scale), Image.NEAREST)
    if scanlines and scale >= 2:
        px = np.asarray(out).astype(np.float32)
        rows = (np.arange(out.height) % scale) == scale - 1
        px[rows] *= 1.0 - scanlines
        out = Image.fromarray(px.astype(np.uint8), "RGB")
    return out


def lcd_skin(im: Image.Image) -> Image.Image:
    """Four greens with an ordered dither: how the handheld's screen would show a picture."""
    rgb = np.asarray(im.convert("RGB")).astype(np.float32)
    lum = rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    bayer = np.array([[0, 2], [3, 1]], dtype=np.float32) / 4.0 - 0.375
    h, w = lum.shape
    lum = lum + np.tile(bayer, (h // 2 + 1, w // 2 + 1))[:h, :w] * 34.0
    level = np.digitize(lum, [58, 112, 170])
    tones = np.array(LCD_TONES, dtype=np.uint8)
    return Image.fromarray(tones[level], "RGB")


# ------------------------------------------------------------------------------------------------------------ sprites
def _critter(body, belly, ears="round", acc=(), frame=0) -> Image.Image:
    """The standard little hero: 16x16, big eyes, optional ears and gear."""
    dark = shade(body, 0.45)
    im = new(16, 16)
    d = ImageDraw.Draw(im)
    up = frame & 1
    if ears == "round":
        d.ellipse([1, 1, 5, 5], fill=body)
        d.ellipse([10, 1, 14, 5], fill=body)
        d.point([(3, 3), (12, 3)], fill=belly)
    elif ears == "pointy":
        d.polygon([(2, 5), (3, 0), (6, 4)], fill=body)
        d.polygon([(9, 4), (12, 0), (13, 5)], fill=body)
    d.rounded_rectangle([2, 3, 13, 13], radius=4, fill=body)
    d.rounded_rectangle([5, 8, 10, 13], radius=2, fill=belly)
    d.rectangle([3, 14 - up, 6, 15 - up], fill=dark)
    d.rectangle([9, 13 + up, 12, 14 + up], fill=dark)
    d.rectangle([4, 5, 6, 8], fill=(255, 255, 255))
    d.rectangle([9, 5, 11, 8], fill=(255, 255, 255))
    d.rectangle([5, 6, 6, 8], fill=(20, 20, 40))
    d.rectangle([10, 6, 11, 8], fill=(20, 20, 40))
    d.line([(7, 10), (8, 11), (9, 10)], fill=dark)
    for item in acc:
        if item == "mask":
            d.rectangle([3, 5, 12, 7], fill=(40, 40, 60))
            d.rectangle([4, 6, 6, 7], fill=(255, 255, 255))
            d.rectangle([9, 6, 11, 7], fill=(255, 255, 255))
        elif item == "antenna":
            d.line([(7, 3), (7, 0)], fill=dark)
            d.rectangle([6, -1, 8, 1], fill=(255, 230, 90))
        elif item == "helmet":
            d.arc([0, -2, 15, 12], 180, 360, fill=(200, 235, 255), width=2)
            d.rectangle([0, 4, 1, 7], fill=(200, 235, 255))
            d.rectangle([14, 4, 15, 7], fill=(200, 235, 255))
            d.point([(3, 1), (4, 0)], fill=(255, 255, 255))
        elif item == "hat":
            d.polygon([(3, 3), (8, -6), (13, 3)], fill=shade(body, 0.7))
            d.rectangle([1, 2, 14, 3], fill=shade(body, 0.55))
            d.point([(8, -2), (7, 0), (10, 1)], fill=(255, 224, 90))
        elif item == "plume":
            d.rounded_rectangle([2, 1, 13, 7], radius=2, fill=(180, 190, 205))
            d.rectangle([3, 5, 12, 6], fill=(40, 44, 60))
            d.polygon([(8, 1), (6, -3), (12, -2), (13, 3)], fill=(220, 60, 60))
        elif item == "teeth":
            d.rectangle([7, 11, 8, 12], fill=(255, 255, 255))
        elif item == "scarf":
            d.rectangle([2, 11, 13, 12], fill=(230, 70, 70))
            d.rectangle([11, 12, 13, 15], fill=(230, 70, 70))
        elif item == "lantern":
            d.line([(13, 8), (15, 8)], fill=dark)
            d.rectangle([14, 9, 16, 12], fill=(255, 225, 100))
        elif item == "beak":
            d.polygon([(12, 7), (16, 9), (12, 10)], fill=(255, 190, 40))
        elif item == "wings":
            d.polygon([(2, 8), (-2, 4 + up * 3), (3, 11)], fill=shade(body, 1.25))
        elif item == "horns":
            d.polygon([(3, 3), (2, -1), (6, 3)], fill=(240, 230, 210))
            d.polygon([(10, 3), (13, -1), (13, 3)], fill=(240, 230, 210))
        elif item == "glow":
            d.rectangle([5, 9, 10, 12], fill=(255, 190, 60))
    return outline(im, shade(body, 0.22))


def _kart(body, driver, kettle=False, frame=0) -> Image.Image:
    im = new(30, 20)
    d = ImageDraw.Draw(im)
    wheel = (36, 36, 48)
    if kettle:                                       # a copper kettle on wheels, steam from the spout
        d.rounded_rectangle([5, 5, 21, 15], radius=5, fill=body)
        d.rectangle([7, 7, 10, 12], fill=shade(body, 1.35))
        d.polygon([(21, 7), (27, 2), (28, 4), (22, 11)], fill=body)               # spout
        d.arc([0, 5, 8, 15], 90, 270, fill=shade(body, 0.6), width=2)               # handle
        d.rectangle([11, 2, 16, 5], fill=shade(body, 0.7))
        d.rectangle([13, 0, 14, 2], fill=(255, 220, 120))
        puff = (240, 244, 255)
        d.rectangle([27 + frame % 2, -1, 29, 1], fill=puff)
    else:
        d.rounded_rectangle([2, 8, 26, 16], radius=3, fill=body)
        d.rectangle([4, 10, 24, 11], fill=shade(body, 1.35))
        d.rectangle([21, 5, 26, 8], fill=shade(body, 0.7))                           # nose
        d.ellipse([9, -2, 20, 9], fill=driver)                                       # the turnip
        d.polygon([(11, 0), (13, -4), (15, 0)], fill=(70, 190, 80))
        d.polygon([(14, 0), (17, -5), (18, 0)], fill=(100, 210, 90))
        d.rectangle([14, 4, 15, 5], fill=(20, 20, 40))
        d.rectangle([17, 4, 18, 5], fill=(20, 20, 40))
        d.ellipse([9, 3, 12, 9], fill=shade(driver, 1.5))
    for cx in (7, 22):
        d.ellipse([cx - 3, 13, cx + 3, 19], fill=wheel)
        d.point([(cx, 16)], fill=(190, 190, 200))
        d.point([(cx - 1 + frame % 2, 15 + frame % 2)], fill=(120, 120, 140))
    return outline(im, shade(body, 0.22))


def _crab(body, frame=0) -> Image.Image:
    im = new(20, 14)
    d = ImageDraw.Draw(im)
    claw = 0 if frame & 1 else 1
    d.ellipse([3, 4, 16, 12], fill=body)
    d.ellipse([5, 6, 14, 12], fill=shade(body, 1.3))
    d.rectangle([4, 0, 5, 4], fill=body)
    d.rectangle([14, 0, 15, 4], fill=body)
    d.rectangle([3, -1, 6, 2], fill=(255, 255, 255))
    d.rectangle([13, -1, 16, 2], fill=(255, 255, 255))
    d.point([(5, 1), (14, 1)], fill=(20, 20, 40))
    d.polygon([(0, 4 - claw), (3, 3), (3, 7), (1, 8 - claw)], fill=body)
    d.polygon([(19, 4 - claw), (16, 3), (16, 7), (18, 8 - claw)], fill=body)
    for x in (4, 7, 12, 15):
        d.point([(x, 13 - (frame + x) % 2)], fill=shade(body, 0.5))
    return outline(im, shade(body, 0.22))


def _bean(body, frame=0) -> Image.Image:
    im = new(14, 20)
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([1, 1, 12, 16], radius=6, fill=body)
    d.rectangle([3, 3, 4, 8], fill=shade(body, 1.55))
    d.rectangle([4, 7, 5, 10], fill=(255, 255, 255))
    d.rectangle([8, 7, 9, 10], fill=(255, 255, 255))
    d.point([(5, 9), (9, 9)], fill=(20, 20, 40))
    d.line([(6, 12), (7, 13), (8, 12)], fill=shade(body, 0.4))
    d.rectangle([2, 17 - (frame & 1), 5, 19 - (frame & 1)], fill=shade(body, 0.5))
    d.rectangle([8, 16 + (frame & 1), 11, 18 + (frame & 1)], fill=shade(body, 0.5))
    return outline(im, shade(body, 0.25))


def hero_sprite(game: DemoGame, frame: int = 0) -> Image.Image:
    a, kind = game.accent, game.hero
    belly = shade(a, 1.55)
    if kind == "blob":
        return _critter(a, belly, "none", ("antenna",), frame)
    if kind == "quokka":
        return _critter((196, 140, 90), (244, 220, 184), "round", ("helmet",), frame)
    if kind == "knight":
        return _critter((236, 196, 130), (252, 232, 190), "none", ("plume",), frame)
    if kind == "gopher":
        return _critter((170, 120, 84), (232, 204, 164), "round", ("teeth", "scarf"), frame)
    if kind == "bandit":
        return _critter((140, 142, 160), (206, 208, 218), "round", ("mask",), frame)
    if kind == "diver":
        return _critter((70, 170, 214), (190, 232, 250), "none", ("helmet", "lantern"), frame)
    if kind == "otter":
        return _critter((130, 90, 66), (220, 190, 150), "round", ("helmet",), frame)
    if kind == "wizard":
        return _critter((150, 100, 210), (214, 190, 246), "none", ("hat",), frame)
    if kind == "drop":
        return _critter((70, 200, 170), (190, 245, 230), "none", ("lantern",), frame)
    if kind == "bird":
        return _critter((70, 140, 230), (210, 232, 255), "none", ("beak", "wings"), frame)
    if kind == "ember":
        return _critter((70, 52, 66), (255, 150, 60), "none", ("horns", "glow"), frame)
    if kind == "kart":
        return _kart((236, 80, 70), (170, 90, 200), False, frame)
    if kind == "kettle":
        return _kart((214, 120, 66), a, True, frame)
    if kind == "crab":
        return _crab(a, frame)
    if kind == "bean":
        return _bean(a, frame)
    return _critter(a, belly, "round", (), frame)


def enemy_sprite(kind: str, frame: int = 0) -> Image.Image:
    im = new(18, 16)
    d = ImageDraw.Draw(im)
    f = frame & 1
    if kind == "slime":
        c = (120, 220, 90)
        d.pieslice([0, 4 + f, 17, 20 + f], 180, 360, fill=c)
        d.rectangle([0, 11, 17, 12 + f], fill=c)
        d.rectangle([4, 7 + f, 6, 9 + f], fill=(255, 255, 255))
        d.rectangle([11, 7 + f, 13, 9 + f], fill=(255, 255, 255))
        d.point([(5, 8 + f), (12, 8 + f)], fill=(20, 20, 40))
        d.rectangle([3, 5, 5, 6], fill=shade(c, 1.5))
        return outline(im.crop((0, 2, 18, 14)), shade(c, 0.3))
    if kind == "bat":
        c = (110, 80, 150)
        d.ellipse([6, 5, 12, 12], fill=c)
        d.polygon([(6, 7), (0, 3 + 4 * f), (1, 10), (6, 10)], fill=shade(c, 0.8))
        d.polygon([(12, 7), (18, 3 + 4 * f), (17, 10), (12, 10)], fill=shade(c, 0.8))
        d.point([(7, 7), (11, 7)], fill=(255, 80, 80))
        d.point([(7, 4), (11, 4)], fill=c)
        return outline(im.crop((0, 1, 18, 14)), shade(c, 0.3))
    if kind == "jelly":
        c = (250, 130, 170)
        d.pieslice([2, 0, 16, 14], 180, 360, fill=c)
        d.rectangle([2, 6, 15, 7], fill=c)
        d.rectangle([5, 3, 6, 4], fill=shade(c, 1.6))
        for i, x in enumerate((4, 8, 12)):
            d.line([(x, 8), (x + ((i + f) % 2) * 2 - 1, 15)], fill=shade(c, 0.85), width=1)
        d.point([(6, 5), (11, 5)], fill=(60, 20, 60))
        return outline(im, shade(c, 0.35))
    if kind == "ufo":
        c = (170, 180, 200)
        d.ellipse([0, 8, 17, 14], fill=c)
        d.pieslice([4, 1, 13, 14], 180, 360, fill=(150, 235, 255))
        d.rectangle([4, 9, 5, 9], fill=(255, 230, 100) if f else (255, 90, 90))
        d.rectangle([12, 9, 13, 9], fill=(255, 90, 90) if f else (255, 230, 100))
        d.rectangle([8, 11, 9, 11], fill=(255, 230, 100))
        d.point([(7, 5)], fill=(255, 255, 255))
        return outline(im, shade(c, 0.3))
    if kind == "ghost":
        c = (236, 240, 255)
        d.rounded_rectangle([3, 0, 14, 14], radius=5, fill=c)
        for x in range(3, 15, 3):
            d.rectangle([x, 12 + ((x // 3 + f) % 2), x + 2, 15], fill=c)
        d.rectangle([5, 4, 7, 7], fill=(40, 40, 70))
        d.rectangle([10, 4, 12, 7], fill=(40, 40, 70))
        d.rectangle([7, 10, 10, 11], fill=(40, 40, 70))
        return outline(im, (90, 90, 140))
    if kind == "beetle":
        c = (220, 70, 60)
        d.ellipse([1, 3, 16, 13], fill=c)
        d.line([(8, 3), (8, 12)], fill=shade(c, 0.5))
        d.point([(5, 6), (11, 6), (5, 9), (11, 9)], fill=(30, 20, 30))
        d.rectangle([13, 5, 16, 8], fill=(40, 30, 40))
        for x in (3, 8, 12):
            d.point([(x + f, 14)], fill=(40, 30, 40))
        return outline(im.crop((0, 1, 18, 15)), shade(c, 0.3))
    if kind == "fish":
        c = (255, 170, 60)
        d.ellipse([3, 3, 15, 12], fill=c)
        d.polygon([(0, 2 + f), (4, 7), (0, 12 - f)], fill=shade(c, 0.8))
        d.rectangle([11, 6, 12, 7], fill=(255, 255, 255))
        d.point([(12, 7)], fill=(20, 20, 40))
        d.rectangle([6, 4, 7, 11], fill=shade(c, 1.35))
        return outline(im, shade(c, 0.3))
    return outline(im, (0, 0, 0))


def pickup_sprite(kind: str, frame: int = 0) -> Image.Image:
    im = new(8, 8)
    d = ImageDraw.Draw(im)
    if kind == "gem":
        d.polygon([(4, 0), (7, 3), (4, 7), (1, 3)], fill=(90, 220, 255))
        d.polygon([(4, 0), (1, 3), (4, 3)], fill=(200, 250, 255))
        d.polygon([(4, 3), (7, 3), (4, 7)], fill=(40, 150, 220))
    elif kind == "coin":
        width = (3, 2, 1, 2)[frame % 4]
        d.ellipse([4 - width, 0, 3 + width, 7], fill=(255, 210, 60))
        d.line([(4, 1), (4, 6)], fill=(255, 245, 160))
    elif kind == "star":
        d.polygon([(4, 0), (5, 3), (8, 3), (5, 5), (6, 8), (4, 6), (2, 8), (3, 5), (0, 3), (3, 3)], fill=(255, 226, 80))
    elif kind == "pearl":
        d.ellipse([1, 1, 6, 6], fill=(255, 238, 244))
        d.point([(2, 2)], fill=(255, 255, 255))
        d.point([(5, 5)], fill=(230, 160, 200))
    elif kind == "bean":
        d.rounded_rectangle([1, 0, 6, 7], radius=3, fill=((255, 120, 170), (120, 220, 120), (255, 220, 80))[frame % 3])
        d.point([(2, 2)], fill=(255, 255, 255))
    return outline(im, (30, 24, 50))


# ------------------------------------------------------------------------------------------------------------ scenes
SCENES = {
    "sky": dict(top=(84, 160, 250), bot=(206, 240, 255), top2=(250, 130, 120), bot2=(255, 226, 160), far=(150, 190, 236),
                mid=(86, 176, 98), mid2=(60, 148, 88), gtop=(112, 202, 82), gbody=(166, 104, 62), gline=(112, 66, 42),
                plat=(232, 196, 124), enemies=("slime", "bat"), pickup="coin"),
    "sea": dict(top=(8, 58, 126), bot=(28, 176, 190), top2=(44, 22, 96), bot2=(30, 130, 170), far=(20, 100, 144),
                mid=(44, 164, 104), mid2=(255, 124, 132), gtop=(238, 216, 142), gbody=(198, 168, 102), gline=(150, 120, 72),
                plat=(124, 96, 168), enemies=("jelly", "fish"), pickup="pearl"),
    "space": dict(top=(6, 6, 30), bot=(66, 32, 108), top2=(18, 2, 44), bot2=(132, 44, 96), far=(76, 52, 126),
                  mid=(124, 112, 156), mid2=(92, 82, 124), gtop=(168, 166, 198), gbody=(104, 102, 134), gline=(62, 60, 92),
                  plat=(90, 204, 232), enemies=("ufo", "bat"), pickup="star"),
    "cave": dict(top=(14, 16, 44), bot=(44, 56, 100), top2=(30, 10, 52), bot2=(76, 44, 104), far=(32, 36, 76),
                 mid=(90, 210, 236), mid2=(236, 116, 206), gtop=(158, 176, 206), gbody=(92, 104, 136), gline=(60, 66, 98),
                 plat=(132, 154, 186), enemies=("bat", "slime"), pickup="gem"),
    "castle": dict(top=(22, 10, 62), bot=(128, 62, 124), top2=(54, 10, 24), bot2=(208, 84, 44), far=(54, 32, 96),
                   mid=(38, 28, 72), mid2=(255, 222, 120), gtop=(126, 114, 138), gbody=(82, 74, 100), gline=(48, 42, 62),
                   plat=(154, 124, 92), enemies=("ghost", "bat"), pickup="coin"),
    "forest": dict(top=(30, 124, 124), bot=(166, 232, 152), top2=(190, 92, 62), bot2=(255, 204, 112), far=(40, 134, 112),
                   mid=(30, 112, 82), mid2=(64, 164, 92), gtop=(124, 204, 72), gbody=(112, 72, 52), gline=(72, 46, 34),
                   plat=(154, 112, 72), enemies=("beetle", "slime"), pickup="bean"),
}


def _hash_float(r: random.Random, lo: float, hi: float) -> float:
    return lo + (hi - lo) * r.random()


class Scene:
    """A little side-scrolling level: background bands, three parallax layers that repeat, entities, a HUD.

    The same level is used for every picture of a game; `cam` (pixels) says where in the level the picture was taken and
    `tone` 1 gives its evening colours. Layers are twice as wide as their period, so scrolling never shows a seam."""

    def __init__(self, game: DemoGame, w: int = SCREEN[0], h: int = SCREEN[1], tone: int = 0, busy: bool = True):
        self.game, self.w, self.h, self.tone, self.busy = game, w, h, tone, busy      # busy=False: no platforms or creatures (box art)
        self.kit = dict(SCENES[game.scene])
        k = self.kit
        a = game.accent
        k["gtop"], k["far"] = mix(k["gtop"], a, 0.22 if game.scene in ("space", "sky", "forest", "castle") else 0.1), mix(k["far"], a, 0.14)
        self.top, self.bot = (k["top2"], k["bot2"]) if tone else (k["top"], k["bot"])
        self.lw = max(2 * w, 320)
        self.gy = h - 40                                   # the ground line
        self.r = rng(game.title, "world")
        self.bg = self._background()
        self.far = self._far()
        self.mid = self._mid()
        self.ground = self._ground()
        self.entities = self._entities()

    # --- layers (each is `lw` wide, stored twice side by side)
    def _tiled(self, im: Image.Image) -> Image.Image:
        out = new(self.lw * 2, self.h)
        out.alpha_composite(im, (0, 0))
        out.alpha_composite(im, (self.lw, 0))
        return out

    def _background(self) -> Image.Image:
        im = Image.new("RGB", (self.w, self.h))
        d = ImageDraw.Draw(im)
        banded(d, 0, 0, self.w, self.gy + 4, self.top, self.bot, bands=10)
        r = rng(self.game.title, "sky")
        scene = self.game.scene
        if scene in ("space", "cave", "castle") or self.tone:
            for _ in range(int(self.w * self.h / 260)):
                x, y = r.randrange(self.w), r.randrange(max(1, self.gy - 20))
                c = r.choice(((255, 255, 255), (190, 200, 255), (255, 236, 180), (150, 160, 220)))
                d.point((x, y), fill=c if r.random() < 0.5 else mix(self.bot, c, 0.55))
        cx, cy = int(self.w * (0.72 if self.game.hero in ("kettle", "bean", "bird") else 0.78)), int(self.h * 0.2)
        if scene in ("sky", "forest"):                                          # a sun with a ring
            rad = max(8, self.h // 12)
            c = (255, 240, 150) if not self.tone else (255, 190, 90)
            d.ellipse([cx - rad - 3, cy - rad - 3, cx + rad + 3, cy + rad + 3], fill=mix(self.bot, c, 0.3))
            d.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=c)
            d.ellipse([cx - rad + 3, cy - rad + 3, cx - 1, cy - 1], fill=shade(c, 1.3))
        elif scene == "space":                                                   # a ringed planet
            rad = max(10, self.h // 7)
            px, py = int(self.w * 0.7), int(self.h * 0.3)
            base = self.game.accent
            d.ellipse([px - rad, py - rad, px + rad, py + rad], fill=shade(base, 0.8))
            d.pieslice([px - rad, py - rad, px + rad, py + rad], 200, 340, fill=shade(base, 0.55))
            d.ellipse([px - rad + 3, py - rad + 3, px - rad // 3, py - rad // 3], fill=shade(base, 1.2))
            d.arc([px - rad - 8, py - 5, px + rad + 8, py + 5], 0, 360, fill=mix(base, (255, 255, 255), 0.55), width=2)
            d.pieslice([px - rad, py - rad, px + rad, py + rad], 20, 160, fill=shade(base, 0.8))
            d.ellipse([px - rad + 2, py + 1, px - rad + 6, py + 4], fill=shade(base, 0.6))
        elif scene == "castle":                                                  # a big moon
            rad = max(9, self.h // 10)
            d.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=(250, 240, 200))
            d.ellipse([cx - rad // 2, cy - rad // 3, cx - rad // 4, cy], fill=(226, 214, 170))
            d.ellipse([cx + 2, cy + 2, cx + 6, cy + 6], fill=(226, 214, 170))
        elif scene == "sea":                                                     # light shafts from the surface
            overlay = new(self.w, self.h)
            od = ImageDraw.Draw(overlay)
            for i in range(5):
                x = int(self.w * (0.1 + 0.2 * i))
                od.polygon([(x, 0), (x + 14, 0), (x + 44, self.gy), (x + 8, self.gy)], fill=(255, 255, 255, 26))
            im = Image.alpha_composite(im.convert("RGBA"), overlay).convert("RGB")
        return im

    def _ridge(self, kind: str, base: int, amp: int, colour, jag: bool = False) -> Image.Image:
        im = new(self.lw, self.h)
        d = ImageDraw.Draw(im)
        r = rng(self.game.title, "ridge", kind)
        ks = [(1, r.random() * 6.28, 1.0), (2, r.random() * 6.28, 0.5), (3 + r.randrange(3), r.random() * 6.28, 0.28)]
        for x in range(self.lw):
            v = sum(m * math.sin(2 * math.pi * k * x / self.lw + p) for k, p, m in ks) / 1.78
            if jag:
                v = (abs(((x / 17.0 + r.random() * 0.0) % 2) - 1) * 1.4 - 0.6) * 0.6 + v * 0.55
            top = int(base - amp * (v + 1) / 2)
            d.line([(x, top), (x, self.gy + 8)], fill=colour)
        return im

    def _far(self) -> Image.Image:
        k, s = self.kit, self.game.scene
        if s == "cave":
            im = self._ridge("far", self.gy - 14, 46, k["far"], jag=True)
            ceiling = new(self.lw, self.h)
            d = ImageDraw.Draw(ceiling)
            r = rng(self.game.title, "ceil")
            x = 0
            while x < self.lw:
                wd, ln = r.randrange(10, 22), r.randrange(14, 52)
                d.polygon([(x, 0), (x + wd, 0), (x + wd // 2, ln)], fill=shade(k["far"], 0.8))
                x += wd - 2
            im.alpha_composite(ceiling)
            return self._tiled(im)
        if s == "castle":
            return self._tiled(self._ridge("far", self.gy - 20, 44, k["far"]))
        if s == "space":
            return self._tiled(self._ridge("far", self.gy - 10, 34, mix(k["far"], self.top, 0.25)))
        if s == "sea":
            return self._tiled(self._ridge("far", self.gy - 12, 40, k["far"], jag=True))
        im = self._ridge("far", self.gy - 18, 46, k["far"])
        if s == "sky":
            d = ImageDraw.Draw(im)
            r = rng(self.game.title, "clouds")
            cloud = mix((255, 255, 255), self.bot, 0.15)
            for _ in range(6):
                x, y = r.randrange(self.lw), r.randrange(24, 70)
                for dx in (0, self.lw):
                    for ox, oy, rad in ((0, 0, 9), (12, -4, 12), (26, 0, 9), (13, 3, 9)):
                        d.ellipse([x + dx + ox - rad, y + oy - rad, x + dx + ox + rad, y + oy + rad], fill=cloud)
                    d.rectangle([x + dx - 8, y + 6, x + dx + 34, y + 12], fill=cloud)
                    d.line([(x + dx - 8, y + 12), (x + dx + 34, y + 12)], fill=mix(cloud, self.bot, 0.4))
        return self._tiled(im)

    def _mid(self) -> Image.Image:
        k, s, lw = self.kit, self.game.scene, self.lw
        im = new(lw, self.h)
        d = ImageDraw.Draw(im)
        r = rng(self.game.title, "mid")
        if s in ("sky", "forest") and s == "sky":
            for cx in range(0, lw, 96):
                rad = r.randrange(30, 52)
                x = cx + r.randrange(-8, 8)
                for dx in (0, lw):
                    d.ellipse([x - rad + dx, self.gy - rad // 2 - 8, x + rad + dx, self.gy + rad * 2], fill=k["mid"])
                    d.ellipse([x - rad + dx + 6, self.gy - rad // 2 - 5, x - rad // 3 + dx, self.gy - rad // 2 + 3], fill=shade(k["mid"], 1.25))
            for _ in range(10):                                                # bushes
                x = r.randrange(lw)
                for dx in (0, lw):
                    d.ellipse([x + dx - 7, self.gy - 11, x + dx + 7, self.gy + 3], fill=k["mid2"])
        elif s == "forest":
            for x in range(8, lw, 34):
                x += r.randrange(-6, 6)
                tall = r.randrange(70, 110)
                for dx in (0, lw):
                    d.rectangle([x + dx - 3, self.gy - tall, x + dx + 3, self.gy + 6], fill=shade(k["mid"], 0.55))
                    d.rectangle([x + dx - 1, self.gy - tall, x + dx, self.gy + 6], fill=shade(k["mid"], 0.8))
                    for cx, cy, cr in ((x, self.gy - tall, 17), (x - 12, self.gy - tall + 8, 12), (x + 12, self.gy - tall + 9, 13)):
                        d.ellipse([cx + dx - cr, cy - cr, cx + dx + cr, cy + cr], fill=k["mid"])
                        d.ellipse([cx + dx - cr + 3, cy - cr + 3, cx + dx - 1, cy - 2], fill=k["mid2"])
            for _ in range(7):                                                  # hanging vines
                x, ln = r.randrange(lw), r.randrange(24, 60)
                for dx in (0, lw):
                    d.line([(x + dx, 0), (x + dx, ln)], fill=k["mid2"], width=2)
                    d.rectangle([x + dx - 2, ln, x + dx + 2, ln + 3], fill=k["mid2"])
        elif s == "sea":
            for _ in range(16):
                x, hgt = r.randrange(lw), r.randrange(26, 76)
                for dx in (0, lw):
                    for yy in range(0, hgt, 4):
                        off = int(math.sin(yy / 7.0 + x) * 3)
                        d.rectangle([x + dx + off, self.gy - yy - 4, x + dx + off + 2, self.gy - yy], fill=k["mid"])
                        if yy % 12 == 8:
                            d.rectangle([x + dx + off + 3, self.gy - yy - 6, x + dx + off + 6, self.gy - yy - 3], fill=shade(k["mid"], 1.2))
            for _ in range(7):                                                  # coral fans
                x = r.randrange(lw)
                for dx in (0, lw):
                    for i in range(-3, 4):
                        d.line([(x + dx, self.gy + 4), (x + dx + i * 4, self.gy - 20 + abs(i) * 4)], fill=k["mid2"], width=2)
        elif s == "space":
            for _ in range(7):                                                  # drifting rocks
                x, y, rad = r.randrange(lw), r.randrange(24, self.gy - 40), r.randrange(6, 15)
                for dx in (0, lw):
                    d.ellipse([x + dx - rad, y - rad + 4, x + dx + rad, y + rad], fill=k["mid2"])
                    d.ellipse([x + dx - rad + 2, y - rad // 2, x + dx - 1, y - 1], fill=k["mid"])
                    d.ellipse([x + dx + 1, y + 1, x + dx + 4, y + 3], fill=shade(k["mid2"], 0.6))
        elif s == "cave":
            for _ in range(11):                                                 # crystal clusters
                x = r.randrange(lw)
                colour = k["mid"] if r.random() < 0.6 else k["mid2"]
                for dx in (0, lw):
                    for i, (ox, hh) in enumerate(((-6, 18), (0, 30), (6, 14))):
                        d.polygon([(x + dx + ox - 3, self.gy + 4), (x + dx + ox, self.gy + 4 - hh), (x + dx + ox + 3, self.gy + 4)], fill=colour)
                        d.polygon([(x + dx + ox, self.gy + 4 - hh), (x + dx + ox + 3, self.gy + 4), (x + dx + ox, self.gy + 4)], fill=shade(colour, 0.7))
                        d.point([(x + dx + ox - 1, self.gy + 4 - hh + 4)], fill=(255, 255, 255))
        elif s == "castle":
            for x in range(20, lw, 150):
                x += r.randrange(-10, 10)
                for dx in (0, lw):
                    top = self.gy - r.randrange(70, 96)
                    d.rectangle([x + dx - 30, top + 20, x + dx + 30, self.gy + 6], fill=k["mid"])
                    for t in (-34, 22):
                        d.rectangle([x + dx + t, top - 4, x + dx + t + 12, self.gy + 6], fill=k["mid"])
                        d.polygon([(x + dx + t - 2, top - 4), (x + dx + t + 6, top - 24), (x + dx + t + 14, top - 4)], fill=shade(k["mid"], 0.8))
                    d.rectangle([x + dx - 8, top - 10, x + dx + 8, top + 20], fill=k["mid"])
                    d.polygon([(x + dx - 10, top - 10), (x + dx, top - 32), (x + dx + 10, top - 10)], fill=shade(k["mid"], 0.8))
                    for bx in range(-30, 30, 8):
                        d.rectangle([x + dx + bx, top + 14, x + dx + bx + 4, top + 20], fill=k["mid"])
                    for wx, wy in ((-18, top + 40), (14, top + 40), (-2, top + 8), (-26, top + 6 - 20), (26, top + 6 - 20)):
                        d.rectangle([x + dx + wx - 2, wy, x + dx + wx + 2, wy + 7], fill=k["mid2"] if r.random() < 0.8 else shade(k["mid"], 1.15))
                    d.line([(x + dx, top - 32), (x + dx, top - 42)], fill=(30, 22, 50))
                    d.polygon([(x + dx, top - 42), (x + dx + 9, top - 39), (x + dx, top - 36)], fill=self.game.accent)
        return self._tiled(im)

    def _tile(self, d, x: int, y: int, top: bool, rr: random.Random) -> None:
        k, s = self.kit, self.game.scene
        d.rectangle([x, y, x + 15, y + 15], fill=k["gbody"])
        if s in ("castle", "cave"):
            d.line([(x, y + 7), (x + 15, y + 7)], fill=k["gline"])
            d.line([(x + (0 if (y // 16) % 2 else 8), y), (x + (0 if (y // 16) % 2 else 8), y + 7)], fill=k["gline"])
            d.line([(x + (8 if (y // 16) % 2 else 0), y + 8), (x + (8 if (y // 16) % 2 else 0), y + 15)], fill=k["gline"])
            d.line([(x, y + 15), (x + 15, y + 15)], fill=k["gline"])
            d.point([(x + 2, y + 2), (x + 11, y + 10)], fill=shade(k["gbody"], 1.25))
        else:
            for _ in range(3):
                d.point([(x + rr.randrange(1, 15), y + rr.randrange(4, 15))], fill=k["gline"])
            d.point([(x + rr.randrange(1, 15), y + rr.randrange(6, 15))], fill=shade(k["gbody"], 1.25))
        if top:
            d.rectangle([x, y, x + 15, y + 4], fill=k["gtop"])
            d.line([(x, y), (x + 15, y)], fill=shade(k["gtop"], 1.3))
            d.line([(x, y + 5), (x + 15, y + 5)], fill=shade(k["gtop"], 0.62))
            if s in ("sky", "forest"):
                for bx in range(1, 15, 3):
                    if rr.random() < 0.6:
                        d.point([(x + bx, y + 5), (x + bx, y + 6)], fill=shade(k["gtop"], 0.85))

    def _platform(self, d, x: int, y: int, n: int, rr) -> None:
        k = self.kit
        for i in range(n):
            tx = x + 16 * i
            d.rectangle([tx, y, tx + 15, y + 9], fill=k["plat"])
            d.line([(tx, y), (tx + 15, y)], fill=shade(k["plat"], 1.35))
            d.line([(tx, y + 9), (tx + 15, y + 9)], fill=shade(k["plat"], 0.55))
            d.line([(tx + 15, y), (tx + 15, y + 9)], fill=shade(k["plat"], 0.7))
            d.point([(tx + 4, y + 4), (tx + 10, y + 5)], fill=shade(k["plat"], 0.8))

    def _ground(self) -> Image.Image:
        im = new(self.lw, self.h)
        d = ImageDraw.Draw(im)
        rr = rng(self.game.title, "tiles")
        rows = (self.h - self.gy + 15) // 16
        for row in range(rows):
            for tx in range(0, self.lw, 16):
                self._tile(d, tx, self.gy + 16 * row, row == 0, rr)
        # decorations on the ground line
        s = self.game.scene
        for _ in range(9):
            x = rr.randrange(self.lw)
            if s in ("sky", "forest"):
                c = rr.choice(((255, 90, 120), (255, 220, 80), (255, 255, 255)))
                d.line([(x, self.gy - 1), (x, self.gy - 6)], fill=(60, 140, 60))
                d.rectangle([x - 1, self.gy - 9, x + 1, self.gy - 6], fill=c)
            elif s == "castle":
                d.rectangle([x, self.gy - 10, x + 2, self.gy - 1], fill=(90, 70, 60))
                d.polygon([(x - 1, self.gy - 10), (x + 1, self.gy - 17), (x + 3, self.gy - 10)], fill=(255, 170, 40))
                d.polygon([(x, self.gy - 10), (x + 1, self.gy - 14), (x + 2, self.gy - 10)], fill=(255, 240, 150))
            elif s == "sea":
                d.ellipse([x - 4, self.gy - 6, x + 4, self.gy + 2], fill=shade(self.kit["gbody"], 0.8))
                d.ellipse([x - 3, self.gy - 6, x, self.gy - 3], fill=shade(self.kit["gbody"], 1.1))
            elif s == "space":
                d.ellipse([x - 5, self.gy - 3, x + 5, self.gy + 3], fill=shade(self.kit["gtop"], 0.7))
            else:
                d.polygon([(x - 3, self.gy), (x, self.gy - 9), (x + 3, self.gy)], fill=self.kit["mid"] if rr.random() < 0.7 else self.kit["mid2"])
        for _ in range(5 if self.busy else 0):                                  # floating platforms
            n = rr.randrange(2, 5)
            self._platform(d, rr.randrange(0, self.lw - 64), self.gy - rr.randrange(44, 80), n, rr)
        return self._tiled(im)

    # --- things in the level
    def _entities(self) -> list:
        r = rng(self.game.title, "entities")
        ents = []
        if not self.busy:
            return ents
        for i in range(7):
            ents.append(("enemy", self.kit["enemies"][i % 2], r.randrange(self.lw), i))
        for _ in range(5):                                         # collectables in little arcs
            x0, base = r.randrange(self.lw), r.randrange(self.gy - 70, self.gy - 30)
            for i in range(5):
                ents.append(("pickup", self.kit["pickup"], x0 + i * 13, base - int(math.sin(math.pi * i / 4) * 16)))
        if self.game.scene == "sea":
            for _ in range(18):
                ents.append(("bubble", "", r.randrange(self.lw), r.randrange(8, self.gy)))
        return ents

    def _paste_layer(self, base: Image.Image, layer: Image.Image, x0: int) -> None:
        x0 %= self.lw
        base.alpha_composite(layer.crop((x0, 0, x0 + self.w, self.h)))

    def render(self, cam: float = 0.0, t: float = 0.0, hud: bool = True, hero_x: Optional[int] = None, jump: float = 0.3,
               score: Optional[int] = None) -> Image.Image:
        """One frame (RGB, art size). `t` (seconds) animates sprites; `cam` is the scroll position in pixels."""
        im = self.bg.convert("RGBA")
        for factor, layer in ((0.18, self.far), (0.5, self.mid), (1.0, self.ground)):
            self._paste_layer(im, layer, int(cam * factor))
        sx = lambda wx: ((wx - int(cam) + 40) % self.lw) - 40            # noqa: E731 - world x -> screen x
        frame = int(t * 6)
        enemy_y = self.gy
        for kind, name, wx, extra in self.entities:
            x = sx(wx)
            if not -24 < x < self.w + 8:
                continue
            if kind == "pickup":
                spr = pickup_sprite(name, frame + wx)
                bob = int(math.sin(t * 4 + wx) * 2)
                im.alpha_composite(spr, (x, extra + bob))
            elif kind == "enemy":
                spr = enemy_sprite(name, frame + extra)
                y = enemy_y - spr.height + 2
                if name in ("bat", "ufo", "fish", "jelly", "ghost"):
                    y = self.gy - 56 - (extra % 3) * 12 + int(math.sin(t * 2.5 + extra) * 5)
                    x += int(math.cos(t * 2 + extra) * 8)
                im.alpha_composite(spr, (x, y))
            elif kind == "bubble":
                y = int((extra - t * 14) % self.gy)
                px = x + int(math.sin(t * 2 + wx) * 2)
                ImageDraw.Draw(im).ellipse([px, y, px + 2, y + 2], outline=(220, 245, 255, 255))
        hx = self.w // 4 if hero_x is None else hero_x
        hero = hero_sprite(self.game, frame)
        floor = self.gy - hero.height + 2
        hy = floor - int(abs(math.sin(math.pi * jump)) * 34)
        if self.game.hero in ("kart", "kettle"):
            hy = floor - int(abs(math.sin(t * 6)) * 1.5)
        if self.game.scene == "sea" and self.game.hero not in ("crab",):
            hy = self.gy - 78 + int(math.sin(t * 2.2) * 6)
        im.alpha_composite(hero, (hx, hy))
        if self.game.hero == "diver":                                  # the lantern's glow
            glow = new(self.w, self.h)
            gd = ImageDraw.Draw(glow)
            gd.ellipse([hx + 4, hy - 6, hx + 44, hy + 34], fill=(255, 240, 140, 40))
            gd.ellipse([hx + 14, hy + 2, hx + 34, hy + 22], fill=(255, 240, 140, 50))
            im.alpha_composite(glow)
        out = im.convert("RGB")
        if hud:
            self._hud(out, score if score is not None else 4200 + int(cam) * 10 // 1)
        return out

    def _hud(self, im: Image.Image, score: int) -> None:
        d = ImageDraw.Draw(im)
        f = font(10, "mono")
        ink, shadow = (255, 255, 255), (20, 16, 40)
        d.rectangle([0, 0, self.w, 13], fill=(18, 14, 38))
        d.line([(0, 14), (self.w, 14)], fill=(70, 60, 120))
        text_px(d, (6, 2), f"SCORE {score % 1000000:06d}", f, ink, shadow)
        if self.w >= 200:
            text_px(d, (self.w - 66, 2), f"STAGE {1 + score % 7}-{1 + (score // 7) % 4}", f, (255, 224, 110), shadow)
            for i in range(3):                                                   # hearts
                hx = self.w // 2 - 22 + i * 14
                d.polygon([(hx, 5), (hx + 2, 3), (hx + 4, 5), (hx + 6, 3), (hx + 8, 5), (hx + 4, 11)], fill=(255, 70, 100) if i < 2 else (90, 60, 90))


# ------------------------------------------------------------------------------------------------------------ screenshots
def _finish(im: Image.Image, game: DemoGame, scale: int, scan: float = 0.16) -> Image.Image:
    if game.skin == "lcd":
        im = lcd_skin(im)
    return enlarge(im, scale, scan)


def screenshot(game: DemoGame, variant: int = 0, scale: int = 4) -> Image.Image:
    """A made-up gameplay picture. variant 0: the picture the library holds (saved at 2x, used as 'Current'), 1 and 2 are
    other places of the level (1 with the evening colours at 4x) and 3 is a title screen in a different style."""
    if variant == 3:
        return title_screen(game, scale)
    tone = 1 if variant == 2 else 0
    cam = (40, 300, 520)[min(variant, 2)]
    scene = Scene(game, tone=tone)
    frame = scene.render(cam=cam, t=0.5 + variant * 0.37, jump=(0.35, 0.5, 0.2)[min(variant, 2)], score=rng(game.title, 'score', variant).randrange(240, 1990) * 50)
    return _finish(frame, game, scale)


def title_screen(game: DemoGame, scale: int = 4) -> Image.Image:
    """4:3 title screen in the opposite palette style (four greens, or full colour for the handheld's game)."""
    scene = Scene(game, tone=1)
    frame = scene.render(cam=120, t=0.0, hud=False, hero_x=100, jump=0.0).crop((40, 0, 280, 180))
    frame = frame.convert("RGBA")
    shadow = new(frame.width, frame.height)
    ImageDraw.Draw(shadow).rectangle([0, 0, frame.width, frame.height], fill=(0, 0, 20, 70))
    frame.alpha_composite(shadow)
    logo = logo_image(game, 0, 1000).convert("RGBA")
    ratio = min(204 / logo.width, 86 / logo.height)
    logo = logo.resize((max(1, round(logo.width * ratio)), max(1, round(logo.height * ratio))), Image.LANCZOS)
    logo.putalpha(logo.getchannel("A").point(lambda v: 255 if v > 90 else 0))          # hard edges: it is a pixel picture
    frame.alpha_composite(logo, ((frame.width - logo.width) // 2, 18))
    d = ImageDraw.Draw(frame)
    f = font(10, "mono")
    msg = "PRESS START"
    w = d.textlength(msg, font=f)
    text_px(d, ((frame.width - w) // 2, 134), msg, f, (255, 255, 255), (20, 10, 40))
    small = font(9, "mono")
    info = {**game.found, **game.lib}
    foot = f"(C) {info.get('releasedate', '19900101')[:4]} {info.get('developer', 'Demo')}".upper()
    text_px(d, ((frame.width - d.textlength(foot, font=small)) // 2, 164), foot, small, (230, 230, 255), (20, 10, 40))
    out = frame.convert("RGB")
    if game.skin != "lcd":
        out = lcd_skin(out)
    return enlarge(out, scale, 0.2)


def video_thumb(game: DemoGame, kind: int = 0) -> Image.Image:
    """The still of a trailer: a gameplay frame with a play button and a length chip (320x180). `kind` picks another place."""
    frame = Scene(game, tone=1 - kind).render(cam=380 - 230 * kind, t=1.1, hud=False, jump=0.4)
    if game.skin == "lcd":
        frame = lcd_skin(frame)
    im = frame.convert("RGBA")
    ov = new(*im.size)
    d = ImageDraw.Draw(ov)
    d.rectangle([0, 0, im.width, im.height], fill=(0, 0, 0, 40))
    cx, cy = im.width // 2, im.height // 2
    d.ellipse([cx - 22, cy - 22, cx + 22, cy + 22], fill=(20, 16, 36, 210), outline=(255, 255, 255, 255), width=2)
    d.polygon([(cx - 7, cy - 12), (cx - 7, cy + 12), (cx + 13, cy)], fill=(255, 255, 255, 255))
    d.rectangle([im.width - 44, im.height - 18, im.width - 4, im.height - 4], fill=(0, 0, 0, 200))
    text_px(d, (im.width - 40, im.height - 17), "TRAILER", font(8, "mono"), (255, 255, 255))
    im.alpha_composite(ov)
    return im.convert("RGB")


# ------------------------------------------------------------------------------------------------------------ logo
def _fit_lines(title: str, max_w: int, max_h: int):
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    for size in range(220, 17, -6):
        fnt = font(size)
        lines = _wrap(probe, title, fnt, max_w)
        pitch = int(fnt.getbbox("Ag")[3] * 1.0)
        whole_words = all(probe.textlength(word, font=fnt) <= max_w for word in title.split())
        if whole_words and len(lines) <= 3 and pitch * len(lines) <= max_h and (len(lines) < 3 or size < 150):
            return fnt, lines, size, pitch, probe
    return fnt, lines, size, pitch, probe


def logo_image(game: DemoGame, variant: int = 0, width: int = 1000, solid: bool = False) -> Image.Image:
    """The title as a logo: gradient letters, dark outline, light outer rim, drop shadow, on a transparent canvas (or, with
    `solid`, on one flat colour). `variant` 1 uses another colour scheme."""
    a = game.accent
    comp = rotate_hue(a, 0.5)
    if variant == 0:
        top, bottom, rim, dark = shade(a, 1.75), shade(a, 0.9), (255, 250, 232), shade(a, 0.16)
    else:
        top, bottom, rim, dark = shade(comp, 1.7), shade(comp, 0.85), mix(a, (255, 255, 255), 0.65), shade(comp, 0.14)
    title = game.title.upper()
    fnt, lines, size, pitch, probe = _fit_lines(title, width, int(width * 0.5))
    ring, edge, drop = max(3, size // 9), max(2, size // 16), max(3, size // 12)
    pad = ring + drop + 6
    text_w = int(max(probe.textlength(line, font=fnt) for line in lines))
    w, h = text_w + 2 * pad, pitch * len(lines) + 2 * pad + size // 6
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    layers = []
    for i, line in enumerate(lines):
        xy = ((w - probe.textlength(line, font=fnt)) / 2, pad + i * pitch)
        layers.append((xy, line))
        md.text(xy, line, font=fnt, fill=255)
    canvas = new(w, h)
    d = ImageDraw.Draw(canvas)
    for xy, line in layers:                                        # shadow, rim, outline
        stroked_text(d, (xy[0] + drop, xy[1] + drop), line, fnt, (*shade(dark, 0.6), 200), ring, (*shade(dark, 0.6), 200))
    for xy, line in layers:
        stroked_text(d, xy, line, fnt, rim, ring, rim)
    for xy, line in layers:
        stroked_text(d, xy, line, fnt, dark, edge, dark)
    gradient = np.zeros((h, w, 3), dtype=np.uint8)
    t = np.linspace(0, 1, h)[:, None]
    for c in range(3):
        col = np.where(t < 0.5, top[c] + (mix(top, a if variant == 0 else comp, 1)[c] - top[c]) * (t / 0.5),
                       (a if variant == 0 else comp)[c] + (bottom[c] - (a if variant == 0 else comp)[c]) * ((t - 0.5) / 0.5))
        gradient[:, :, c] = np.broadcast_to(col, (h, w)).astype(np.uint8)
    canvas.paste(Image.fromarray(gradient, "RGB"), (0, 0), mask)
    shine = Image.new("L", (w, h), 0)                               # a light stripe across the upper third of the letters
    ImageDraw.Draw(shine).rectangle([0, int(pad + pitch * 0.12), w, int(pad + pitch * 0.2)], fill=90)
    canvas.paste((255, 255, 255), (0, 0), Image.fromarray(np.minimum(np.asarray(mask), np.asarray(shine)), "L"))
    canvas = canvas.crop(canvas.getchannel("A").getbbox())
    if solid:
        bg = mix(dark, comp if variant == 0 else a, 0.5) if variant == 0 else mix(dark, a, 0.55)
        bg = shade(bg, 0.8)
        page = new(canvas.width + 80, canvas.height + 60, (*bg, 255))
        page.alpha_composite(canvas, (40, 30))
        return page
    return canvas


def logo_png(game: DemoGame, variant: int = 0, solid: bool = False) -> bytes:
    """The logo as a candidate: a transparent PNG or (solid) one on a flat background, in its own size."""
    return _png_bytes(logo_image(game, variant, solid=solid))


def library_logo(game: DemoGame) -> bytes:
    """The logo file in the library: the project's own 600x300 transparent PNG."""
    return make_logo_png(logo_image(game, 0))


# ------------------------------------------------------------------------------------------------------------ box art
def _seal(size: int, colour) -> Image.Image:
    """A round, spiky, invented rating seal ("FUN A+"), drawn big and shrunk for smooth edges."""
    ss = 4
    s = size * ss
    im = new(s, s)
    d = ImageDraw.Draw(im)
    pts = []
    for i in range(32):
        ang = math.pi * 2 * i / 32 - math.pi / 2
        rad = s / 2 * (1.0 if i % 2 == 0 else 0.88)
        pts.append((s / 2 + rad * math.cos(ang), s / 2 + rad * math.sin(ang)))
    d.polygon(pts, fill=(40, 28, 60, 255))
    inner = []
    for i in range(32):
        ang = math.pi * 2 * i / 32 - math.pi / 2
        rad = s / 2 * (0.92 if i % 2 == 0 else 0.8)
        inner.append((s / 2 + rad * math.cos(ang), s / 2 + rad * math.sin(ang)))
    d.polygon(inner, fill=colour)
    d.ellipse([s * 0.14, s * 0.14, s * 0.86, s * 0.86], outline=(40, 28, 60, 255), width=ss * 2)
    small, big = font(int(s * 0.14), "mono"), font(int(s * 0.3))
    for text, fnt, cy in (("RATED", small, 0.27), ("FUN", big, 0.5), ("A+", small, 0.76)):
        text_mid(d, s / 2, s * cy, text, fnt, (40, 28, 60, 255), centre=True)
    return im.resize((size, size), Image.LANCZOS)


def box_art(game: DemoGame, variant: int = 0) -> Image.Image:
    """A portrait cover in the project's box size (396x640). variant 0 is the library's, 1 and 2 the candidates."""
    w, h = BOX_ART
    a = game.accent if variant != 2 else rotate_hue(game.accent, 0.45)
    system = SYSTEM_BY_FOLDER[game.system]
    scene = Scene(game, w, h, tone=1 if variant else 0, busy=False)
    base = scene.render(cam=60 + 90 * variant, t=0.0, hud=False, hero_x=-40)             # the hero is added big, below
    big = base.resize((w * 4, h * 4), Image.NEAREST).convert("RGBA")
    # sunburst rays behind the title and the hero
    rays = new(*big.size)
    rd = ImageDraw.Draw(rays)
    cx, cy = big.width // 2, int(big.height * 0.5)
    for i in range(0, 360, 20):
        p = [(cx, cy)] + [(cx + 900 * math.cos(math.radians(i + j)), cy + 900 * math.sin(math.radians(i + j))) for j in (0, 10)]
        rd.polygon(p, fill=(*mix(a, (255, 255, 255), 0.6), 44))
    big.alpha_composite(rays)
    # hero, big
    hero = hero_sprite(game, 0)
    k = 8 if hero.width < 24 else 6
    hero = hero.resize((hero.width * k, hero.height * k), Image.NEAREST)
    shadow = new(hero.width + 40, 36)
    ImageDraw.Draw(shadow).ellipse([0, 0, shadow.width - 1, 35], fill=(0, 0, 0, 90))
    ground_y = scene.gy * 4
    hx = (big.width - hero.width) // 2 + (28 if variant == 2 else -10)
    hy = ground_y - hero.height + 18
    if variant == 2:
        hero = hero.transpose(Image.FLIP_LEFT_RIGHT)
    big.alpha_composite(shadow, (hx - 20, ground_y - 14))
    big.alpha_composite(hero, (hx, hy))
    if game.skin == "lcd":
        pass                                                                               # box art is always in colour
    d = ImageDraw.Draw(big)
    # the band with the generic system name
    band_h = 50
    d.rectangle([0, 0, big.width, band_h], fill=system.colour)
    d.rectangle([0, band_h, big.width, band_h + 5], fill=a)
    d.rectangle([0, band_h + 5, big.width, band_h + 7], fill=(255, 255, 255))
    f = font(24, "mono")
    text_mid(d, big.width / 2, band_h / 2, system.band, f, (255, 255, 255), centre=True)
    for x in (14, big.width - 24):                                                       # tiny stripes either side
        for i in range(3):
            d.rectangle([x, 14 + i * 9, x + 9, 19 + i * 9], fill=mix(system.colour, (255, 255, 255), 0.45))
    # the title
    title = logo_image(game, 0 if variant != 2 else 1, 600)
    ratio = min(340 / title.width, 190 / title.height)
    title = title.resize((max(1, round(title.width * ratio)), max(1, round(title.height * ratio))), Image.LANCZOS)
    big.alpha_composite(title, ((big.width - title.width) // 2, 80 + (190 - title.height) // 2))
    # the plate at the bottom: studio, genre, players
    plate_y = big.height - 78
    d.rectangle([0, plate_y, big.width, big.height], fill=(26, 20, 44))
    d.rectangle([0, plate_y, big.width, plate_y + 4], fill=a)
    dev = (game.lib.get("developer") or game.found.get("developer") or "").upper()
    meta = f"{(game.found.get('genre') or game.lib.get('genre') or '').split(',')[0].upper()}  |  {game.found.get('players', '1')} PLAYER"
    size = 19
    while size > 11 and d.textlength(dev, font=font(size, "mono")) > big.width - 160:
        size -= 1
    text_mid(d, 16, plate_y + 24, dev, font(size, "mono"), (255, 255, 255))
    text_mid(d, 16, plate_y + 54, meta, font(16, "mono"), mix(a, (255, 255, 255), 0.55))
    big.alpha_composite(_seal(104, a if variant != 2 else (255, 214, 70)), (big.width - 122, plate_y - 62))
    return big.convert("RGB")


def box_png(game: DemoGame, variant: int = 0) -> bytes:
    """A box picture as PNG, already in the box size."""
    im = box_art(game, variant)
    assert im.size == BOX_SIZE, im.size
    return _png_bytes(im)


def image_png(game: DemoGame, variant: int) -> bytes:
    """A screenshot candidate as PNG (1280x720 for variants 1 and 2, 960x720 for the title screen)."""
    return _png_bytes(screenshot(game, variant, scale=4))
