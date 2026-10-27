"""From a candidate picture to the bytes that will be saved, plus small previews. No Qt: runs on worker threads.

What the window shows is exactly what gets written: the preview of a slot is its processed result (the same bytes that go
into ``build_request``), so there is nothing to recompute on the GUI thread.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image

from ..processing.images import (has_real_transparency, make_boxart_png, make_image_jpg, make_logo_png,
                                 make_text_logo, open_image, remove_background)

PREVIEW_SIDE = 320                 # video thumbnails are shrunk to this before they are shown


@dataclass(frozen=True)
class Processed:
    data: bytes                    # JPEG (image) or PNG (box art, logo) in the theme's size
    note: str                      # what the user should know, e.g. '600×300 PNG · transparent ✓'
    warning: str = ""              # non-empty: the result is usable but probably not what is wanted


def _from(im) -> str:
    return f" (from {im.width}×{im.height})"


def process_image(raw: bytes, remove_bg: bool = False) -> Processed:
    im = open_image(raw)
    return Processed(make_image_jpg(im), "640×360 JPG" + _from(im))


def process_boxart(raw: bytes, remove_bg: bool = False) -> Processed:
    im = open_image(raw)
    data, cropped = make_boxart_png(im)
    return Processed(data, "396×640 PNG" + _from(im) + (" · cropped from a wide picture" if cropped else ""))


def process_logo(raw: bytes, remove_bg: bool = False) -> Processed:
    im = open_image(raw)
    if has_real_transparency(im):
        return Processed(make_logo_png(im), "600×300 PNG · transparent ✓")
    if remove_bg:
        cut = remove_background(im)
        if has_real_transparency(cut):
            try:
                return Processed(make_logo_png(cut), "600×300 PNG · background removed")
            except ValueError:                                  # the whole picture was "background"
                return Processed(make_logo_png(im), "600×300 PNG", "removing the background left nothing; pick another picture "
                                 "or use Make text logo")
        return Processed(make_logo_png(im), "600×300 PNG",
                         "no solid background could be found to remove; pick another picture or use Make text logo")
    return Processed(make_logo_png(im), "600×300 PNG",
                     "this picture has a solid background; tick 'Remove background', pick another or use Make text logo")


PROCESSORS = {"image": process_image, "boxart": process_boxart, "logo": process_logo}


def process(slot: str, raw: bytes, remove_bg: bool = False) -> Processed:
    return PROCESSORS[slot](raw, remove_bg)


def load_candidate(fetch, cand, slot: str, remove_bg: bool = False, local_ok: bool = False, cancelled=None) -> Processed:
    """Download (or read, when `local_ok`) one candidate and convert it. `fetch` is ``Controller.fetch_bytes``."""
    if getattr(cand, "current", False):
        raise ValueError("The Current picture is what the game has now; it is shown for comparison and cannot be processed again.")
    raw = fetch(cand.url, local_ok=local_ok, cancelled=cancelled)
    if cancelled is not None and cancelled():
        raise InterruptedError("cancelled")
    return process(slot, raw, remove_bg)


def text_logo(title: str) -> Processed:
    return Processed(make_text_logo(title.strip() or "Game"), "600×300 PNG · generated text logo")


def make_preview(raw: bytes, side: int = PREVIEW_SIDE) -> bytes:
    """A small PNG of a picture (decoded with the same safe decoder as everything else), for candidate thumbnails."""
    im = open_image(raw)
    im.thumbnail((side, side), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def decode_rgba(data: bytes, max_side: int = 1024) -> tuple:
    """(width, height, raw RGBA bytes) of picture bytes, decoded by Pillow (the same restricted decoders as everything else),
    shrunk so neither side exceeds `max_side`. The window builds its QImage from these raw pixels, so previews never depend
    on Qt's own image plugins (on the Batocera Qt and Pillow ship different libjpeg versions)."""
    im = open_image(data)
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    im = im.convert("RGBA")
    return im.width, im.height, im.tobytes()
