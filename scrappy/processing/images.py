"""Convert any picture into the exact formats the Batocera theme uses."""
from __future__ import annotations

import io
import os
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from ..atomic import atomic_replace

IMAGE_SIZE = (640, 360)     # <image>     JPG
BOX_SIZE = (396, 640)       # <thumbnail> PNG RGBA (box art)
LOGO_SIZE = (600, 300)      # <marquee>   PNG RGBA (logo, transparent)

# Searched in order; a bare name is also looked up by Pillow in the system font folders.
# If nothing is found Pillow's built-in font is used, so the text logo never fails for lack of a font.
FONT_CANDIDATES = [
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "DejaVuSans-Bold.ttf",
    "LiberationSans-Bold.ttf",
]

FLATTEN_COLOR = (16, 16, 16, 255)    # what transparent areas become in a JPG
MAX_BG_REMOVAL_SIDE = 1200           # background removal works on at most this many pixels per side
FLOOD_PASS_FACTOR = 1                # background removal gives up after this many passes per pixel of side length

# Decoders used on downloaded bytes. Everything else (EPS can start Ghostscript, PSD, ICO, DDS, ...) is refused.
ALLOWED_FORMATS = ["JPEG", "PNG", "WEBP", "GIF", "BMP", "TIFF"]


# ---------------------------------------------------------------- loading

def _to_8bit(im: Image.Image) -> Image.Image:
    """Grey images with 16/32-bit or float samples -> 8-bit "L", scaled (not clipped)."""
    arr = np.asarray(im)
    if arr.dtype.kind == "f":
        arr = arr * 255 if arr.size and arr.max() <= 1 else arr
        arr = np.clip(arr, 0, 255)
    else:
        arr = np.clip(arr.astype(np.int64), 0, 65535)
        arr = (arr * 255 + 32767) // 65535
    return Image.fromarray(arr.astype(np.uint8), "L")


def _normalize(im: Image.Image) -> Image.Image:
    """RGB, or RGBA when the picture carries transparency (alpha channel, palette/colour-key transparency)."""
    if im.mode in ("I", "F") or im.mode.startswith("I;"):
        im = _to_8bit(im)
    has_alpha = any(b in ("A", "a") for b in im.getbands()) or "transparency" in im.info
    return im.convert("RGBA" if has_alpha else "RGB")


def open_image(data: bytes) -> Image.Image:
    """Decode picture bytes into an upright, fully loaded RGB or RGBA image.

    Only the formats in ``ALLOWED_FORMATS`` are decoded; anything else raises ``Image.UnidentifiedImageError``.
    EXIF orientation is applied, animated pictures yield their first frame, and palette, CMYK, 16-bit and
    greyscale inputs are converted correctly. Pillow's decompression-bomb protection stays on: pictures
    above ``Image.MAX_IMAGE_PIXELS`` (half of Pillow's hard limit) are refused with
    ``Image.DecompressionBombError``. Pillow itself raises that above twice the limit and merely warns
    in between; the check is done here, before any pixel is decoded, rather than by turning the warning
    into an error, because warning filters are process-global and this runs on worker threads.
    """
    try:
        im = Image.open(io.BytesIO(data), formats=ALLOWED_FORMATS)
    except Image.UnidentifiedImageError:
        raise Image.UnidentifiedImageError(
            "unsupported or unrecognised picture format (allowed: " + ", ".join(ALLOWED_FORMATS) + ")") from None
    limit = Image.MAX_IMAGE_PIXELS
    if limit is not None and im.width * im.height > limit:
        raise Image.DecompressionBombError(
            f"picture has {im.width * im.height} pixels, more than the {limit} allowed")
    im.load()
    return _normalize(ImageOps.exif_transpose(im))


# ---------------------------------------------------------------- image + box art

def cover_box(width: int, height: int, size) -> tuple:
    """The centred part (left, top, right, bottom) of a `width` x `height` picture that has the shape of `size`, in whole
    pixels and always inside the picture (0 <= left < right <= width, 0 <= top < bottom <= height). Floating-point
    results such as -2e-13 used to reach Pillow and be refused ("box offset can't be negative")."""
    tw, th = size
    scale = max(tw / width, th / height)
    cw = min(width, max(1, round(tw / scale)))
    ch = min(height, max(1, round(th / scale)))
    left, top = (width - cw) // 2, (height - ch) // 2
    return left, top, left + cw, top + ch


def cover(im: Image.Image, size) -> Image.Image:
    """Scale to fill `size` completely, cropping the centre of the source (one resampling step)."""
    return im.resize(size, Image.LANCZOS, box=cover_box(im.width, im.height, size))


def _flatten(im: Image.Image) -> Image.Image:
    """RGB version; transparent areas become dark grey."""
    im = _normalize(im)
    if im.mode == "RGBA":
        bg = Image.new("RGBA", im.size, FLATTEN_COLOR)
        bg.alpha_composite(im)
        return bg.convert("RGB")
    return im


def _png_bytes(im: Image.Image) -> bytes:
    out = io.BytesIO()
    im.save(out, "PNG", optimize=True)
    return out.getvalue()


def make_image_jpg(im: Image.Image) -> bytes:
    out = io.BytesIO()
    cover(_flatten(im), IMAGE_SIZE).save(out, "JPEG", quality=88, optimize=True)
    return out.getvalue()


def make_boxart_png(im: Image.Image):
    """Returns (png_bytes, was_cropped). was_cropped is True when the source was far from portrait."""
    w, h = im.size
    target = BOX_SIZE[0] / BOX_SIZE[1]
    cropped = abs((w / h) / target - 1) > 0.25
    return _png_bytes(cover(im.convert("RGBA"), BOX_SIZE)), cropped


# ---------------------------------------------------------------- logo

def has_real_transparency(im: Image.Image, min_fraction: float = 0.02) -> bool:
    if im.mode == "P" or "transparency" in im.info:
        im = im.convert("RGBA")
    if not any(b in ("A", "a") for b in im.getbands()):
        return False
    alpha = np.asarray(im.convert("RGBA").getchannel("A"))
    return float((alpha < 250).mean()) >= min_fraction


def _spread_along_rows(mask: np.ndarray, reach: np.ndarray) -> np.ndarray:
    """Grow `reach` (a subset of `mask`) to every mask pixel in the same horizontal run."""
    before = np.zeros_like(mask)
    before[:, 1:] = mask[:, :-1]
    run_id = np.cumsum((mask & ~before).ravel()).reshape(mask.shape)    # one id per horizontal run
    hit = np.zeros(int(run_id.max()) + 1, dtype=bool)
    hit[run_id[reach]] = True
    return mask & hit[run_id]


def _flood(mask: np.ndarray, seeds, max_passes: int):
    """Mask pixels 4-connected to a seed (alternating row/column run propagation, no Python pixel loops).

    Returns None when the fill has not converged within ``max_passes`` passes (a maze-like picture).
    """
    reach = np.zeros_like(mask)
    for y, x in seeds:
        reach[y, x] = mask[y, x]
    count = int(reach.sum())
    for _ in range(max_passes):
        reach = _spread_along_rows(mask, reach)
        reach = _spread_along_rows(mask.T, reach.T).T
        new_count = int(reach.sum())
        if new_count == count:
            return reach
        count = new_count
    return None


def remove_background(im: Image.Image, tolerance: int = 32) -> Image.Image:
    """Make a solid background transparent by flood-filling inwards from the corners.

    Only background connected to the border is removed, so the same colour inside the logo (the hole of
    an "O", a white highlight) is left alone. Corners that do not share the dominant corner colour are not
    used as seeds; when fewer than two corners agree there is no uniform background, and when the fill does
    not converge within ``FLOOD_PASS_FACTOR * max(width, height)`` passes (a maze; even a 1px serpentine
    needs only half that) it is abandoned. In both cases the picture is returned unchanged (check the result
    with ``has_real_transparency``). If corner colours tie, the colour covering most of the whole border wins.
    """
    im = _normalize(im).convert("RGBA")
    if max(im.size) > MAX_BG_REMOVAL_SIDE:
        im.thumbnail((MAX_BG_REMOVAL_SIDE, MAX_BG_REMOVAL_SIDE), Image.LANCZOS)
    px = np.asarray(im).astype(np.int16)
    h, w = px.shape[:2]
    corners = [(0, 0), (0, w - 1), (h - 1, 0), (h - 1, w - 1)]
    limit = tolerance * 3

    def near(i, j):
        return int(np.abs(px[corners[i]] - px[corners[j]]).sum()) <= limit

    groups = [[j for j in range(4) if near(i, j)] for i in range(4)]
    border = np.concatenate([px[0], px[-1], px[1:-1, 0], px[1:-1, -1]])
    on_border = [int((np.abs(border - px[c]).sum(axis=1) <= limit).sum()) for c in corners]
    best = max(range(4), key=lambda i: (len(groups[i]) >= 2, on_border[i], -i))
    if len(groups[best]) < 2:
        return im
    mask = np.abs(px - px[corners[best]]).sum(axis=2) <= limit
    background = _flood(mask, [corners[j] for j in groups[best]], max(1, int(FLOOD_PASS_FACTOR * max(h, w))))
    if background is None:
        return im
    alpha = np.where(background, 0, px[:, :, 3]).astype(np.uint8)
    alpha_im = Image.fromarray(alpha, "L").filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(0.6))
    im.putalpha(alpha_im)
    return im


def make_logo_png(im: Image.Image) -> bytes:
    """Trim transparent borders, fit into 600x300 on a transparent canvas."""
    rgba = _normalize(im).convert("RGBA")
    bbox = rgba.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox()
    if bbox is None:
        raise ValueError("logo has no visible pixels")
    rgba = rgba.crop(bbox)
    max_w, max_h = int(LOGO_SIZE[0] * 0.94), int(LOGO_SIZE[1] * 0.88)
    scale = min(max_w / rgba.width, max_h / rgba.height, 4.0)
    nw, nh = max(1, round(rgba.width * scale)), max(1, round(rgba.height * scale))
    rgba = rgba.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGBA", LOGO_SIZE, (0, 0, 0, 0))
    canvas.alpha_composite(rgba, dest=((LOGO_SIZE[0] - nw) // 2, (LOGO_SIZE[1] - nh) // 2))
    return _png_bytes(canvas)


# ---------------------------------------------------------------- generated text logo

def _font(size: int):
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:                     # old Pillow
        return ImageFont.load_default()


def _wrap(draw, text, font, max_w):
    """Greedy word wrap; a single word wider than max_w is split into pieces."""
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}" if cur else word
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
            continue
        if cur:
            lines.append(cur)
        while len(word) > 1 and draw.textlength(word, font=font) > max_w:
            n = len(word) - 1
            while n > 1 and draw.textlength(word[:n], font=font) > max_w:
                n -= 1
            lines.append(word[:n])
            word = word[n:]
        cur = word
    if cur:
        lines.append(cur)
    return lines


def make_text_logo(title: str) -> bytes:
    """Fallback logo: the title in white with a black outline on a transparent canvas."""
    if not title.strip():
        raise ValueError("title is empty")
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    max_w, max_h = LOGO_SIZE[0] * 0.92, LOGO_SIZE[1] * 0.88
    for size in range(120, 11, -4):
        font = _font(size)
        pitch = font.getbbox("Ag")[3] * 1.1
        lines = _wrap(probe, title, font, max_w)
        if len(lines) <= 3 and pitch * len(lines) <= max_h:
            break
    vector = isinstance(font, ImageFont.FreeTypeFont)    # the bitmap fallback font has no stroke support
    stroke = max(2, size // 14) if vector else 0
    pad = stroke + 4
    width = int(max(probe.textlength(line, font=font) for line in lines)) + 2 * pad
    work = Image.new("RGBA", (width, int(pitch * len(lines)) + 2 * pad), (0, 0, 0, 0))
    draw = ImageDraw.Draw(work)
    for i, line in enumerate(lines):
        xy = ((width - probe.textlength(line, font=font)) / 2, pad + i * pitch)    # each line centred
        if not vector:
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-1, 1), (1, -1)):
                draw.text((xy[0] + dx, xy[1] + dy), line, font=font, fill=(0, 0, 0, 255))
        draw.text(xy, line, font=font, fill=(255, 255, 255, 255), stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
    work = work.crop(work.getchannel("A").getbbox())     # exact optical centring, whatever the font metrics
    scale = min(max_w / work.width, max_h / work.height)
    if scale < 1 or not vector:                          # shrink if too big; enlarge the tiny bitmap font
        scale = min(scale, 6.0)
        work = work.resize((max(1, round(work.width * scale)), max(1, round(work.height * scale))),
                           Image.LANCZOS if scale < 1 else Image.NEAREST)
    canvas = Image.new("RGBA", LOGO_SIZE, (0, 0, 0, 0))
    canvas.alpha_composite(work, dest=((LOGO_SIZE[0] - work.width) // 2, (LOGO_SIZE[1] - work.height) // 2))
    return _png_bytes(canvas)


# ---------------------------------------------------------------- saving

def write_atomic(path, data: bytes):
    """Write `data` to `path` so that a crash leaves either the old file or the new one, never half of it.
    The temp file has a random name and is created exclusively, so a planted symlink is never followed.

    ``scrappy.saver`` reuses this for every picture and for gamelist restores.
    """
    atomic_replace(Path(os.path.realpath(path)), lambda f: f.write(data))
