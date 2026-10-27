#!/usr/bin/env python3
"""End-to-end self-test of Scrappy, meant to run ON the Batocera (also works on a developer machine).

Run on the box (after deploy/install.sh):

    PYTHONPATH=/userdata/system/scrappy/vendor:/userdata/system/scrappy \\
        python3 /userdata/system/scrappy/tools/box_selftest.py
        [--network] [--youtube] [--json] [--keep] [--only PREFIX[,PREFIX]] [--skip-missing] [--verbose]

(without a vendor folder just PYTHONPATH=/userdata/system/scrappy.)

It exercises the REAL code of the program, not copies of it: the picture and video processing, the saver (create, keep,
replace, undo, stale gamelist, refused symbolic links, damaged gamelist, dry run), the safe downloader against a web server
on this machine, the address policy, and PyQt5 (import and an offscreen QApplication, in a child process).

Everything happens in a throw-away sandbox folder (a fresh sub-folder of the temp directory, deleted at the end unless --keep).
It never touches /userdata/roms, a live gamelist.xml or the program's own settings and journals. The sandbox is refused when it
would be inside /userdata/roms. Temporary files made by libraries are redirected into the sandbox too.

Opt-in checks (they need the internet, so they are skipped by default):
    --network   one real Steam search + fetch for 'OpenTTD' (a free, open-source game; a base-game fallback would be printed),
                then one small HTTPS download of a logo/box art through the program's own downloader. No big media.
    --youtube   one yt-dlp search (needs yt-dlp installed by the app; nothing is downloaded).

Every check prints PASS, FAIL or SKIP with the time it took and a reason/detail. The exit code is 1 when any check FAILed
(0 otherwise). A check that needs a tool that is not there (ffmpeg, PyQt5) FAILs, because the box must have it;
--skip-missing turns that into SKIP for machines that are not the box. --json prints a JSON summary on stdout (the human
lines then go to stderr).
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import http.server
import importlib.util
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

sys.dont_write_bytecode = True               # no __pycache__ in the install folder from this run

ROOT = Path(__file__).resolve().parent.parent          # the folder that contains scrappy/ and tools/
try:
    import scrappy  # noqa: F401
except ImportError:                                    # started without PYTHONPATH: use the folder next to tools/
    sys.path.append(str(ROOT))

PROTECTED = ("/userdata/roms",)                        # a sandbox may never live inside these
DEFAULT_HOME = "/userdata/system/scrappy"
NETWORK_TITLE = "OpenTTD"                  # a free, open-source game that Steam sells
JPEG_HINT = ("Pillow cannot use the system JPEG library (typical: 'Wrong JPEG library version: library is 80, caller expects 90'). "
             "The Pillow that was imported does not match the box's libjpeg. Run with the vendored Pillow first on the path: "
             "PYTHONPATH=$DEST/vendor:$DEST python3 tools/box_selftest.py (re-run deploy/install.sh if $DEST/vendor is missing).")


class Skip(Exception):
    """The check does not apply (reason in the message)."""


class Ctx:
    """What a check gets: the sandbox, the options, a place to leave notes and results other checks may reuse."""

    def __init__(self, sandbox: Path, args):
        self.sandbox, self.args = sandbox, args
        self.shared: dict = {}
        self.env: dict = {}
        self.notes: list = []

    def sub(self, name: str) -> Path:
        p = self.sandbox / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    def note(self, text: str) -> None:
        self.notes.append(str(text))

    def need_tool(self, *names: str) -> None:
        missing = [n for n in names if not shutil.which(n)]
        if missing:
            self.missing(", ".join(missing) + " not found on PATH")

    def missing(self, what: str):
        if self.args.skip_missing:
            raise Skip(what + " (--skip-missing)")
        raise AssertionError(what + " (the box must have it; use --skip-missing on a machine that is not the box)")


CHECKS: list = []        # (name, function, opt-in option name or None)


def check(name: str, optin: str | None = None):
    def register(fn):
        CHECKS.append((name, fn, optin))
        return fn
    return register


# ---------------------------------------------------------------- helpers

def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot(root) -> dict:
    """Every file and folder below root: path -> sha256 of the content, 'dir', or 'link:<target>'."""
    out = {}
    for dirpath, dirs, files in os.walk(root):
        for name in dirs:
            full = os.path.join(dirpath, name)
            out[full] = "link:" + os.readlink(full) if os.path.islink(full) else "dir"
        for name in files:
            full = os.path.join(dirpath, name)
            out[full] = "link:" + os.readlink(full) if os.path.islink(full) else sha(Path(full).read_bytes())
    return out


def run(cmd, timeout=120, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, **kw)


def expect_raises(exc_type, fn, what: str):
    try:
        fn()
    except exc_type as exc:
        return exc
    except Exception as exc:
        raise AssertionError(f"{what}: expected {exc_type.__name__}, got {type(exc).__name__}: {exc}") from exc
    raise AssertionError(f"{what}: expected {exc_type.__name__}, but nothing was raised")


def ffprobe(path) -> dict:
    out = run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], timeout=60)
    assert out.returncode == 0, "ffprobe could not read the file: " + out.stderr.strip()[-200:]
    return json.loads(out.stdout)


def make_source(path, lavfi: str, seconds: float, audio: bool = True) -> Path:
    cmd = ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", lavfi]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-shortest"] if audio else []
    cmd.append(str(path))
    out = run(cmd, timeout=120)
    assert out.returncode == 0, "ffmpeg could not make the test video: " + out.stderr.strip()[-300:]
    return Path(path)


def mp4_boxes(path) -> list:
    """Top-level boxes of an MP4 file as [(type, offset)]."""
    size, pos, boxes = os.path.getsize(path), 0, []
    with open(path, "rb") as f:
        while pos + 8 <= size:
            f.seek(pos)
            length, kind = struct.unpack(">I4s", f.read(8))
            if length == 1:
                length = struct.unpack(">Q", f.read(8))[0]
            elif length == 0:
                length = size - pos
            if length < 8:
                break
            boxes.append((kind.decode("latin1"), pos))
            pos += length
    return boxes


def pixel(video, x: int, y: int) -> tuple:
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-frames:v", "1", "-vf", f"crop=2:2:{x}:{y}",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
    assert out.returncode == 0 and len(out.stdout) == 12, "could not read a pixel: " + out.stderr.decode(errors="replace")[-200:]
    return tuple(out.stdout[:3])


def pil_picture(width: int, height: int):
    """A deterministic, colourful test picture."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (width, height), (30, 90, 160))
    draw = ImageDraw.Draw(im)
    for i in range(0, width, max(1, width // 16)):
        draw.rectangle((i, 0, i + max(1, width // 32), height), fill=(220 - i * 200 // width, 60 + i * 150 // width, 40))
    draw.ellipse((width // 4, height // 4, 3 * width // 4, 3 * height // 4), fill=(250, 240, 80))
    return im


# ---------------------------------------------------------------- environment

@check("env.python")
def t_env_python(ctx):
    ctx.env.update(python=sys.version.split()[0], executable=sys.executable, platform=platform.platform(), machine=platform.machine())
    if sys.version_info < (3, 9):
        raise AssertionError(f"Python {sys.version.split()[0]} is too old (3.9+ needed)")
    from scrappy.saver import emulationstation_running
    ctx.note(f"emulationstation_running() -> {emulationstation_running()} (read from /proc only)")
    ctx.note(f"PYTHONPATH={os.environ.get('PYTHONPATH', '')!r}")
    return f"Python {sys.version.split()[0]} on {platform.machine()}"


@check("env.libs")
def t_env_libs(ctx):
    import importlib
    parts = []
    for name in ("PIL", "numpy", "requests", "urllib3", "certifi"):
        try:
            mod = importlib.import_module(name)
        except ImportError as exc:
            if name == "certifi":
                ctx.note("certifi: not importable (requests may use the system certificate store)")
                continue
            raise AssertionError(f"{name} cannot be imported ({exc})")
        version = getattr(mod, "__version__", getattr(mod, "__VERSION__", "?"))
        ctx.env[name] = {"version": str(version), "file": getattr(mod, "__file__", "")}
        ctx.note(f"{name} {version} from {getattr(mod, '__file__', '?')}")
        parts.append(f"{name} {version}")
    import urllib3
    import requests
    major = int(urllib3.__version__.split(".")[0])
    assert major >= 2, f"urllib3 {urllib3.__version__} is too old: scrappy.download needs urllib3 >= 2.0"
    req = tuple(int(x) for x in requests.__version__.split(".")[:2])
    assert req >= (2, 31), f"requests {requests.__version__} is too old: scrappy.download needs requests >= 2.31"
    qt = importlib.util.find_spec("PyQt5")
    ctx.env["PyQt5_found"] = qt is not None
    ctx.note("PyQt5: " + (f"found at {qt.origin or list(qt.submodule_search_locations)[0]}" if qt else "NOT found"))
    return ", ".join(parts)


@check("env.pillow")
def t_env_pillow(ctx):
    import PIL
    from PIL import Image, features
    info = {"version": PIL.__version__, "file": PIL.__file__}
    codecs = {c: bool(features.check_codec(c)) for c in ("jpg", "zlib")}
    info["codecs"] = codecs
    try:
        info["libjpeg"] = features.version("jpg")
    except Exception:
        info["libjpeg"] = None
    ctx.env["pillow"] = info
    ctx.note(f"Pillow {PIL.__version__} imported from {PIL.__file__}")
    ctx.note(f"libjpeg {info['libjpeg']}, codecs {codecs}, formats JPEG={'JPEG' in Image.registered_extensions().values()}")
    missing = [c for c, ok in codecs.items() if not ok]
    assert not missing, f"Pillow lacks the {', '.join(missing)} codec. " + JPEG_HINT
    return f"Pillow {PIL.__version__} ({os.path.dirname(PIL.__file__)})"


@check("env.jpeg_roundtrip")
def t_env_jpeg(ctx):
    import io
    from PIL import Image
    try:
        src = Image.new("RGB", (64, 48), (200, 40, 40))
        buf = io.BytesIO()
        src.save(buf, "JPEG", quality=90)
        data = buf.getvalue()
        assert data[:2] == b"\xff\xd8", "the saved data does not start like a JPEG"
        back = Image.open(io.BytesIO(data))
        back.load()
        assert back.format == "JPEG" and back.size == (64, 48), f"JPEG reads back as {back.format} {back.size}"
        got = back.convert("RGB").getpixel((32, 24))
    except Exception as exc:
        raise AssertionError(f"JPEG save/open round-trip broke: {type(exc).__name__}: {exc}. {JPEG_HINT}") from exc
    assert all(abs(a - b) <= 14 for a, b in zip(got, (200, 40, 40))), f"JPEG pixel came back as {got}, expected about (200, 40, 40)"
    return f"saved {len(data)} bytes, read back, centre pixel {got}"


@check("env.ffmpeg")
def t_env_ffmpeg(ctx):
    ctx.need_tool("ffmpeg", "ffprobe")
    head = run(["ffmpeg", "-hide_banner", "-version"]).stdout.splitlines()[0]
    encoders = run(["ffmpeg", "-hide_banner", "-encoders"]).stdout
    lacking = [e for e in ("libx264", "aac") if not any(line.split()[1:2] == [e] for line in encoders.splitlines() if line.startswith(" "))]
    assert not lacking, f"this ffmpeg has no {', '.join(lacking)} encoder, so trailers cannot be made"
    filters = run(["ffmpeg", "-hide_banner", "-filters"]).stdout
    for needed in ("scale", "pad", "fps"):
        assert any(line.split()[1:2] == [needed] for line in filters.splitlines()), f"ffmpeg lacks the {needed} filter"
    ctx.note(head)
    return " ".join(head.split()[:3])


# ---------------------------------------------------------------- pictures

@check("images.jpg")
def t_images_jpg(ctx):
    import io
    from PIL import Image
    from scrappy.processing.images import FLATTEN_COLOR, IMAGE_SIZE, make_image_jpg, open_image
    assert IMAGE_SIZE == (640, 360)
    data = make_image_jpg(pil_picture(1920, 1080))
    im = Image.open(io.BytesIO(data))
    im.load()
    assert (im.format, im.size, im.mode) == ("JPEG", (640, 360), "RGB"), (im.format, im.size, im.mode)
    assert 1500 < len(data) < 400_000, f"JPEG of {len(data)} bytes looks wrong"
    # a portrait picture is cropped to fill, a transparent one is flattened onto dark grey
    port = Image.open(io.BytesIO(make_image_jpg(pil_picture(600, 900))))
    assert port.size == (640, 360)
    clear = Image.new("RGBA", (800, 800), (0, 0, 0, 0))
    flat = Image.open(io.BytesIO(make_image_jpg(clear))).convert("RGB").getpixel((5, 5))
    assert all(abs(a - b) <= 6 for a, b in zip(flat, FLATTEN_COLOR[:3])), f"transparent area became {flat}"
    # a PNG downloaded from the web goes through open_image first
    png = io.BytesIO()
    pil_picture(300, 200).save(png, "PNG")
    assert Image.open(io.BytesIO(make_image_jpg(open_image(png.getvalue())))).size == (640, 360)
    return f"640x360 JPEG, {len(data)} bytes"


@check("images.boxart")
def t_images_boxart(ctx):
    import io
    from PIL import Image
    from scrappy.processing.images import BOX_SIZE, make_boxart_png
    assert BOX_SIZE == (396, 640)
    data, cropped = make_boxart_png(pil_picture(1920, 1080))
    im = Image.open(io.BytesIO(data))
    assert (im.format, im.size, im.mode) == ("PNG", (396, 640), "RGBA"), (im.format, im.size, im.mode)
    assert cropped is True, "a 16:9 picture must be reported as cropped for box art"
    data2, cropped2 = make_boxart_png(pil_picture(600, 970))
    assert Image.open(io.BytesIO(data2)).size == (396, 640) and cropped2 is False
    return f"396x640 RGBA PNG, {len(data)} bytes (cropped flag: landscape True, portrait False)"


def _alpha_stats(im):
    import numpy as np
    alpha = np.asarray(im.convert("RGBA").getchannel("A"))
    return alpha


@check("images.logo")
def t_images_logo(ctx):
    import io
    from PIL import Image, ImageDraw
    from scrappy.processing.images import LOGO_SIZE, has_real_transparency, make_logo_png
    assert LOGO_SIZE == (600, 300)
    src = Image.new("RGBA", (1000, 400), (0, 0, 0, 0))
    ImageDraw.Draw(src).ellipse((300, 100, 700, 300), fill=(240, 200, 20, 255))
    data = make_logo_png(src)
    im = Image.open(io.BytesIO(data))
    assert (im.format, im.size, im.mode) == ("PNG", (600, 300), "RGBA"), (im.format, im.size, im.mode)
    alpha = _alpha_stats(im)
    assert alpha[0, 0] == 0 and alpha[-1, -1] == 0, "the corners of a logo must stay transparent"
    assert has_real_transparency(im)
    ys, xs = (alpha > 8).nonzero()
    assert xs.min() >= 15 and xs.max() <= 585 and ys.min() >= 17 and ys.max() <= 283, "the logo must have a margin"
    assert abs((xs.min() + xs.max()) / 2 - 300) < 3 and abs((ys.min() + ys.max()) / 2 - 150) < 3, "the logo must be centred"
    expect_raises(ValueError, lambda: make_logo_png(Image.new("RGBA", (50, 50), (0, 0, 0, 0))), "fully transparent logo")
    return f"600x300 RGBA PNG, centred with margin, {len(data)} bytes"


@check("images.remove_background")
def t_images_removebg(ctx):
    import io
    from PIL import Image, ImageDraw
    from scrappy.processing.images import LOGO_SIZE, has_real_transparency, make_logo_png, remove_background
    logo = Image.new("RGB", (400, 200), (255, 255, 255))
    draw = ImageDraw.Draw(logo)
    draw.rectangle((100, 50, 300, 150), fill=(200, 0, 0))
    draw.rectangle((180, 80, 220, 120), fill=(255, 255, 255))          # a hole inside the logo: must stay (like the O of a word)
    cut = remove_background(logo)
    assert has_real_transparency(cut), "remove_background found no background on a solid white logo"
    alpha = _alpha_stats(cut)
    assert alpha[3, 3] == 0 and alpha[196, 396] == 0, "the background must become transparent"
    assert alpha[100, 120] >= 250, "the logo itself must stay opaque"
    assert alpha[100, 200] >= 200, "an enclosed hole of the same colour must not be cut out"
    out = Image.open(io.BytesIO(make_logo_png(cut)))
    assert out.size == LOGO_SIZE and out.mode == "RGBA"
    # no uniform background (four different corners): the picture must come back unchanged
    quad = Image.new("RGB", (200, 200))
    for box, color in (((0, 0, 100, 100), (255, 0, 0)), ((100, 0, 200, 100), (0, 255, 0)),
                       ((0, 100, 100, 200), (0, 0, 255)), ((100, 100, 200, 200), (255, 255, 0))):
        quad.paste(color, box)
    assert not has_real_transparency(remove_background(quad)), "a picture without a uniform background must be left alone"
    return "white background removed, hole kept, 4-colour picture left alone"


@check("images.text_logo")
def t_images_textlogo(ctx):
    import io
    from PIL import Image, ImageFont
    from scrappy.processing import images as I
    shown = {}
    for title in ("Tidepool Tactics: Remastered", "Ünïcødé Spiel ゲーム 世界", "A" * 120, "X"):
        data = I.make_text_logo(title)
        im = Image.open(io.BytesIO(data))
        assert (im.format, im.size, im.mode) == ("PNG", I.LOGO_SIZE, "RGBA"), f"{title[:20]!r}: {im.format} {im.size} {im.mode}"
        assert I.has_real_transparency(im) and _alpha_stats(im).max() == 255, f"{title[:20]!r}: logo is empty or opaque"
        shown[title[:12]] = len(data)
    expect_raises(ValueError, lambda: I.make_text_logo("   "), "empty title")
    font = I._font(40)
    kind = f"TrueType {getattr(font, 'path', '?')}" if isinstance(font, ImageFont.FreeTypeFont) else "built-in bitmap font (no TTF font found)"
    ctx.note("font used: " + kind)
    return f"4 titles incl. non-ASCII and a 120-letter one; font: {kind}"


@check("images.rejects_garbage")
def t_images_garbage(ctx):
    import io
    from PIL import Image, UnidentifiedImageError
    from scrappy.processing.images import open_image
    expect_raises(UnidentifiedImageError, lambda: open_image(b"definitely not a picture"), "garbage bytes")
    eps = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\nshowpage\n"
    expect_raises(UnidentifiedImageError, lambda: open_image(eps), "EPS (would start Ghostscript)")
    gif = io.BytesIO()
    Image.new("P", (20, 20)).save(gif, "GIF", transparency=0)
    assert open_image(gif.getvalue()).mode == "RGBA", "a GIF with transparency must become RGBA"
    return "garbage and EPS refused, transparent GIF understood"


# ---------------------------------------------------------------- video

def _source(ctx, name="src5s.mp4", lavfi="testsrc=duration=5:size=640x360:rate=25", seconds=5, audio=True) -> Path:
    cached = ctx.shared.setdefault("sources", {})
    if name not in cached:
        cached[name] = make_source(ctx.sub("video") / name, lavfi, seconds, audio)
    return cached[name]


@check("video.encode")
def t_video_encode(ctx):
    ctx.need_tool("ffmpeg", "ffprobe")
    from scrappy.processing.video import encode_clip, probe_duration
    out = ctx.sub("video") / "out.mp4"
    src = _source(ctx)
    t0 = time.perf_counter()
    encode_clip(str(src), str(out), start=1, end=4)
    took = time.perf_counter() - t0
    info = ffprobe(out)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    assert (video["codec_name"], video["width"], video["height"]) == ("h264", 1280, 720), video
    assert video["pix_fmt"] == "yuv420p", f"pixel format {video['pix_fmt']}"
    assert video["r_frame_rate"] == "30/1", f"frame rate {video['r_frame_rate']}"
    assert video.get("profile") == "High", f"profile {video.get('profile')}"
    assert audio and audio[0]["codec_name"] == "aac", f"audio streams: {[a['codec_name'] for a in audio]}"
    length = probe_duration(str(out))
    assert 2.4 < length < 3.6, f"clip is {length:.2f} s long, expected about 3"
    kinds = [k for k, _ in mp4_boxes(out)]
    assert "moov" in kinds and "mdat" in kinds and kinds.index("moov") < kinds.index("mdat"), f"not faststart: boxes {kinds}"
    rate = os.path.getsize(out) * 8 / length / 1000
    assert rate < 3000, f"{rate:.0f} kbit/s is far above the 1.5 Mbit/s target"
    leftovers = [p.name for p in out.parent.iterdir() if p.name.endswith(".tmp")]
    assert not leftovers, f"temporary files left behind: {leftovers}"
    ctx.shared["clip"] = out
    ctx.note(json.dumps({k: video.get(k) for k in ("codec_name", "profile", "width", "height", "r_frame_rate", "pix_fmt")}))
    return f"h264 High 1280x720 30fps yuv420p + aac, {length:.1f} s, {rate:.0f} kbit/s, faststart, encoded in {took:.1f} s"


@check("video.aspect_and_no_audio")
def t_video_aspect(ctx):
    ctx.need_tool("ffmpeg", "ffprobe")
    from scrappy.processing.video import encode_clip
    src = make_source(ctx.sub("video") / "red43.mp4", "color=c=red:size=480x360:rate=25:duration=2", 2, audio=False)
    out = ctx.sub("video") / "red43-out.mp4"
    encode_clip(str(src), str(out))
    video = next(s for s in ffprobe(out)["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1280, 720), (video["width"], video["height"])
    bar, mid = pixel(out, 10, 360), pixel(out, 640, 360)
    assert max(bar) < 40, f"the side of a 4:3 picture must be black bars, got {bar}"
    assert mid[0] > 150 and mid[1] < 90 and mid[2] < 90, f"the picture itself must be kept (red), got {mid}"
    return f"4:3 source without audio -> 1280x720 with black bars (bar {bar}, picture {mid})"


@check("video.clamp")
def t_video_clamp(ctx):
    ctx.need_tool("ffmpeg", "ffprobe")
    from scrappy.processing.video import encode_clip, probe_duration
    src = _source(ctx)
    out = ctx.sub("video") / "clamp.mp4"
    results = []
    for label, kw, low, high in (("start/end past the end of a 5 s video", dict(start=100, end=200), 0.7, 1.6),
                                 ("max_seconds=2", dict(start=0, end=None, max_seconds=2), 1.7, 2.6),
                                 ("end before start -> minimum length", dict(start=1, end=1.2), 0.8, 1.7)):
        encode_clip(str(src), str(out), **kw)
        length = probe_duration(str(out))
        assert low < length < high, f"{label}: clip is {length:.2f} s, expected between {low} and {high}"
        results.append(f"{length:.1f}s")
    expect_raises(ValueError, lambda: encode_clip(str(src), str(out), start=float("nan")), "NaN start")
    expect_raises(ValueError, lambda: encode_clip("file:///etc/passwd", str(out)), "file:// source")
    expect_raises(ValueError, lambda: encode_clip(str(src), str(src)), "source == destination")
    return "clamped lengths " + ", ".join(results) + "; NaN, file:// and same-file refused"


@check("video.cancel_and_failure")
def t_video_cancel(ctx):
    ctx.need_tool("ffmpeg", "ffprobe")
    from scrappy.processing.video import EncodeCancelled, VideoError, encode_clip
    src = _source(ctx)
    folder = ctx.sub("video-cancel")
    out = folder / "keep.mp4"
    out.write_bytes(b"old content")
    expect_raises(EncodeCancelled, lambda: encode_clip(str(src), str(out), cancelled=lambda: True), "cancelled encode")
    expect_raises(VideoError, lambda: encode_clip(str(folder / "missing.mp4"), str(out), duration=5.0), "missing source")
    assert out.read_bytes() == b"old content", "a failed encode must leave the existing file untouched"
    assert [p.name for p in folder.iterdir()] == ["keep.mp4"], f"leftovers: {[p.name for p in folder.iterdir()]}"
    return "cancel and failure leave the old file and no temp files"


# ---------------------------------------------------------------- the saver on a realistic system folder

GAMELIST = """<?xml version="1.0"?>
<gameList>
\t<game id="101" source="Batocera">
\t\t<path>./Existing Game [0100AAAA00000000][v0].nsp</path>
\t\t<name>Existing Game</name>
\t\t<desc>Already scraped.</desc>
\t\t<image>./images/Existing Game [0100AAAA00000000][v0]-image.png</image>
\t\t<thumbnail>./images/Existing Game [0100AAAA00000000][v0]-thumb.png</thumbnail>
\t\t<playcount>4</playcount>
\t\t<lastplayed>20250101T101010</lastplayed>
\t\t<favorite>true</favorite>
\t</game>
\t<game id="102" source="Batocera">
\t\t<path>./Sub Folder/Nested Game.xci</path>
\t\t<name>Nested Game</name>
\t\t<playcount>0</playcount>
\t</game>
</gameList>
"""
SYSTEM_ROMS = ("Tidepool Tactics.nsp", "Existing Game [0100AAAA00000000][v0].nsp", "Sub Folder/Nested Game.xci",
               "Ünïcode Spiel.nsz", "Dotted.Name.v1.2.nsp")
SAMPLE_ROM_PATH = "./Tidepool Tactics.nsp"
TAG_ORDER = ("image", "thumbnail", "marquee", "video")


def build_system(root: Path, with_gamelist: bool = True) -> Path:
    """A system folder like /userdata/roms/switch: ROMs (one in a sub-folder), images/, videos/, a gamelist with user art."""
    from PIL import Image
    system = root / "switch"
    for rel in SYSTEM_ROMS:
        rom = system / rel
        rom.parent.mkdir(parents=True, exist_ok=True)
        rom.write_bytes(b"rom " + rel.encode("utf-8"))
    (system / "images").mkdir(exist_ok=True)
    (system / "videos").mkdir(exist_ok=True)
    for suffix in ("-image.png", "-thumb.png"):
        Image.new("RGB", (32, 18), (10, 120, 200)).save(system / "images" / f"Existing Game [0100AAAA00000000][v0]{suffix}", "PNG")
    if with_gamelist:
        (system / "gamelist.xml").write_text(GAMELIST, encoding="utf-8")
    return system


def payloads(ctx, variant: int = 0) -> dict:
    """Real image bytes made by the real converters; variant 1 gives different bytes. The video is a real clip when ffmpeg works."""
    from scrappy.processing.images import make_boxart_png, make_image_jpg, make_text_logo
    from PIL import Image
    key = f"payload{variant}"
    if key not in ctx.shared:
        pic = pil_picture(1920, 1080) if variant == 0 else Image.new("RGB", (1920, 1080), (90, 20, 140))
        folder = ctx.sub("payloads")
        if variant == 0 and ctx.shared.get("clip"):
            video = Path(ctx.shared["clip"])
        else:
            video = folder / f"clip{variant}.mp4"
            if shutil.which("ffmpeg") and shutil.which("ffprobe"):
                from scrappy.processing.video import encode_clip
                encode_clip(str(_source(ctx)), str(video), start=0, end=1)
            else:
                video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200)      # the saver only needs a non-empty file
        ctx.shared[key] = dict(image_jpg=make_image_jpg(pic), thumb_png=make_boxart_png(pic)[0],
                               marquee_png=make_text_logo(f"Tidepool {variant}"), video_mp4=video)
    return ctx.shared[key]


def make_saver(root: Path, **kw):
    from scrappy.saver import Saver
    kw.setdefault("es_running", lambda: False)
    return Saver(root / "backups", root / "journal", **kw)


def req(system, rom=SAMPLE_ROM_PATH, variant=0, ctx=None, **kw):
    from scrappy.saver import SaveRequest
    base = dict(system_dir=system, rom_path=rom, fields={"name": "Tidepool Tactics: Remastered", "desc": "Scary.",
                                                         "releasedate": "20260218T000000", "developer": "Kettle Works"})
    base.update(payloads(ctx, variant))
    base.update(kw)
    return SaveRequest(**base)


def game_tags(system, rom=SAMPLE_ROM_PATH) -> dict:
    from scrappy.gamelist import Gamelist
    game = Gamelist(Path(system) / "gamelist.xml").find(rom)
    return {} if game is None else {c.tag: (c.text or "") for c in game}


def canon(el) -> tuple:
    """An element as comparable data, ignoring whitespace between tags."""
    return (el.tag, tuple(sorted(el.attrib.items())), (el.text or "").strip(), tuple(canon(c) for c in el))


def other_games(system, skip_rom=SAMPLE_ROM_PATH) -> dict:
    from scrappy.gamelist import Gamelist
    gl = Gamelist(Path(system) / "gamelist.xml")
    return {g.findtext("path"): canon(g) for g in gl.games() if g.findtext("path") != skip_rom}


def rom_media(system, rom) -> dict:
    from scrappy.media import media_abs_paths
    return {tag: Path(p) for tag, p in media_abs_paths(str(system), rom).items()}


def es_rewrites(system, game_path="./Written By ES.nsp"):
    """What EmulationStation does behind our back: rewrites gamelist.xml (here: with one more game)."""
    gl = Path(system) / "gamelist.xml"
    text = gl.read_text(encoding="utf-8")
    gl.write_text(text.replace("</gameList>", f"\t<game id=\"900\">\n\t\t<path>{game_path}</path>\n\t\t<name>ES {time.time_ns()}</name>\n\t</game>\n</gameList>"),
                  encoding="utf-8")


@check("saver.create")
def t_saver_create(ctx):
    import io
    from PIL import Image
    from scrappy.gamelist import Gamelist
    from scrappy.roms import asset_status, scan_roms
    root = ctx.sub("saver-create")
    system = build_system(root)
    original = (system / "gamelist.xml").read_bytes()
    others = other_games(system)
    saver = make_saver(root)
    report = saver.save(req(system, ctx=ctx))
    media = rom_media(system, SAMPLE_ROM_PATH)
    assert {os.path.realpath(p) for p in report.written} == {os.path.realpath(p) for p in media.values()}, report.written
    assert report.kept == [] and report.replaced == [] and report.dry_run is False
    for tag, path in media.items():
        assert path.is_file() and path.stat().st_size > 0, f"{tag} file was not written: {path}"
    jpg = Image.open(io.BytesIO(media["image"].read_bytes()))
    assert (jpg.format, jpg.size) == ("JPEG", (640, 360))
    assert Image.open(media["thumbnail"]).size == (396, 640) and Image.open(media["marquee"]).size == (600, 300)
    tags = game_tags(system)
    for tag in TAG_ORDER:
        assert tags.get(tag) == "./" + str(media[tag].relative_to(system)).replace(os.sep, "/"), f"<{tag}> is {tags.get(tag)!r}"
    assert tags["name"] == "Tidepool Tactics: Remastered" and tags["releasedate"] == "20260218T000000" and tags["developer"] == "Kettle Works"
    scrap = Gamelist(system / "gamelist.xml").find(SAMPLE_ROM_PATH).find("scrap")
    assert scrap is not None and scrap.get("name") == "Scrappy", "the <scrap> marker is missing"
    assert other_games(system) == others, "other games in gamelist.xml changed"
    assert report.backup and Path(report.backup).read_bytes() == original, "the backup of gamelist.xml is not the original"
    assert (report.journal / "journal.json").is_file()
    status = asset_status(system, Gamelist(system / "gamelist.xml"), SAMPLE_ROM_PATH)
    assert all(status[t] for t in TAG_ORDER), f"roms.asset_status does not see the new files: {status}"
    assert "./Sub Folder/Nested Game.xci" in scan_roms(system, {".nsp", ".xci", ".nsz"}), "scan_roms misses the sub-folder ROM"
    assert (system / "images").is_dir() and not [p for p in (system / "images").iterdir() if p.name.endswith((".tmp", ".part"))]
    return f"4 files written, gamelist updated, other games intact, backup identical, asset_status complete"


@check("saver.keep_replace_undo")
def t_saver_keep_replace_undo(ctx):
    root = ctx.sub("saver-undo")
    system = build_system(root)
    gl = system / "gamelist.xml"
    original = gl.read_bytes()
    saver = make_saver(root)
    media = rom_media(system, SAMPLE_ROM_PATH)
    first = payloads(ctx, 0)
    saver.save(req(system, ctx=ctx))
    first_bytes = {t: p.read_bytes() for t, p in media.items()}
    # second save with different bytes: existing non-empty files are the user's now and are kept
    second = saver.save(req(system, variant=1, ctx=ctx))
    assert second.written == [] and len(second.kept) == 4, f"expected nothing written and 4 kept, got {second.written} / {second.kept}"
    assert {t: p.read_bytes() for t, p in media.items()} == first_bytes, "kept files were modified"
    # explicit replace of the image only
    third = saver.save(req(system, variant=1, ctx=ctx, replace={"image"}))
    assert [os.path.realpath(p) for p in third.replaced] == [os.path.realpath(media["image"])], third.replaced
    assert media["image"].read_bytes() != first_bytes["image"] and media["thumbnail"].read_bytes() == first_bytes["thumbnail"]
    undone = saver.undo_last()
    assert undone is not None and undone["left_alone"] == []
    assert media["image"].read_bytes() == first_bytes["image"], "undo of a replace must bring the old picture back"
    saver.undo_last()
    assert all(p.exists() for p in media.values()), "undoing the keep-only save must not remove the first save's files"
    saver.undo_last()
    assert not any(p.exists() for p in media.values()), "undoing the first save must remove the files it created"
    assert gl.read_bytes() == original, "gamelist.xml is not byte-identical to the original after undoing everything"
    assert saver.undo_last() is None, "nothing should be left to undo"
    # the user's own art in a slot with another name is respected
    existing = "./Existing Game [0100AAAA00000000][v0].nsp"
    before = game_tags(system, existing)
    rep = saver.save(req(system, rom=existing, ctx=ctx))
    after = game_tags(system, existing)
    assert after["image"] == before["image"] and after["thumbnail"] == before["thumbnail"], "the user's image/thumbnail tags were changed"
    assert os.path.exists(system / "images" / "Existing Game [0100AAAA00000000][v0]-image.png") and \
        not os.path.exists(system / "images" / "Existing Game [0100AAAA00000000][v0]-image.jpg"), "user art was shadowed by a new file"
    assert {Path(p).name for p in rep.written} == {"Existing Game [0100AAAA00000000][v0]-marquee.png",
                                                   "Existing Game [0100AAAA00000000][v0]-video.mp4"}, [Path(p).name for p in rep.written]
    return "keep, replace(image), 3x undo -> gamelist byte-identical; user art with another name kept"


@check("saver.subfolder_and_odd_names")
def t_saver_names(ctx):
    root = ctx.sub("saver-names")
    system = build_system(root)
    saver = make_saver(root)
    roms = ["./Sub Folder/Nested Game.xci", "./Ünïcode Spiel.nsz", "./Dotted.Name.v1.2.nsp"]
    for rom in roms:
        report = saver.save(req(system, rom=rom, ctx=ctx))
        media = rom_media(system, rom)
        for tag, path in media.items():
            assert path.is_file(), f"{rom}: {tag} missing at {path}"
            assert path.parent in (system / "images", system / "videos"), f"{rom}: {tag} not in images/ or videos/: {path}"
        assert game_tags(system, rom)["image"] == "./images/" + media["image"].name
        assert len(report.written) == 4
    assert (system / "images" / "Dotted.Name.v1.2-image.jpg").is_file(), "dots inside a name must be kept"
    assert (system / "images" / "Nested Game-image.jpg").is_file(), "a sub-folder ROM uses its bare name in images/"
    assert game_tags(system, "./Sub Folder/Nested Game.xci")["name"] == "Tidepool Tactics: Remastered"
    for _ in roms:
        assert saver.undo_last() is not None
    assert not [p for p in (system / "images").iterdir() if "Nested" in p.name or "Dotted" in p.name or "Spiel" in p.name]
    return "sub-folder, non-ASCII and dotted names saved and undone"


@check("saver.stale_gamelist")
def t_saver_stale(ctx):
    from scrappy.saver import Saver, StaleGamelist

    class Racing(Saver):
        """EmulationStation rewrites gamelist.xml right after our edit was prepared (`times` times)."""
        times = 1

        def _prepare_gamelist(self, *a, **kw):
            result = super()._prepare_gamelist(*a, **kw)
            if self.times > 0:
                self.times -= 1
                es_rewrites(self.race_system)
            return result

    if not hasattr(Saver, "_prepare_gamelist"):
        raise Skip("Saver._prepare_gamelist hook not found (internals changed); update this check")
    root = ctx.sub("saver-stale")
    system = build_system(root)
    saver = Racing(root / "backups", root / "journal", es_running=lambda: False)
    saver.race_system = system
    saver.times = 1
    report = saver.save(req(system, ctx=ctx))
    assert len(report.written) == 4
    paths = [g.findtext("path") for g in __import__("scrappy.gamelist", fromlist=["Gamelist"]).Gamelist(system / "gamelist.xml").games()]
    assert "./Written By ES.nsp" in paths, "EmulationStation's change was lost: our edit overwrote the file instead of re-applying"
    assert game_tags(system)["image"].startswith("./images/"), "our edit is missing after the retry"
    # a file that never stops changing: give up, leave nothing behind, do not clobber it
    root2 = ctx.sub("saver-stale2")
    system2 = build_system(root2)
    images_before = sorted(p.name for p in (system2 / "images").iterdir())
    videos_before = sorted(p.name for p in (system2 / "videos").iterdir())
    busy = Racing(root2 / "backups", root2 / "journal", es_running=lambda: False)
    busy.race_system, busy.times = system2, 10 ** 6
    expect_raises(StaleGamelist, lambda: busy.save(req(system2, ctx=ctx)), "gamelist.xml that keeps changing")
    assert sorted(p.name for p in (system2 / "images").iterdir()) == images_before, "media left behind after a failed save"
    assert sorted(p.name for p in (system2 / "videos").iterdir()) == videos_before
    assert "./Written By ES.nsp" in (system2 / "gamelist.xml").read_text(encoding="utf-8"), "the other program's changes were lost"
    return "one concurrent rewrite: re-applied on the fresh file; endless rewrites: StaleGamelist, nothing left behind"


@check("saver.undo_refused_then_forced")
def t_saver_undo_refused(ctx):
    from scrappy.saver import UndoRefused
    root = ctx.sub("saver-refuse")
    system = build_system(root)
    gl = system / "gamelist.xml"
    original = gl.read_bytes()
    saver = make_saver(root)
    saver.save(req(system, ctx=ctx))
    es_rewrites(system)
    es_version = gl.read_bytes()
    expect_raises(UndoRefused, lambda: saver.undo_last(), "undo after EmulationStation rewrote gamelist.xml")
    assert gl.read_bytes() == es_version and all(p.exists() for p in rom_media(system, SAMPLE_ROM_PATH).values()), "a refused undo changed things"
    entry = saver.undo_last(force=True)
    assert entry["forced"] is True and gl.read_bytes() == original
    kept = list((root / "journal").glob("undone/*/gamelist.discarded-by-undo.xml"))
    assert kept and kept[0].read_bytes() == es_version, "the discarded gamelist.xml was not kept in the journal"
    assert not any(p.exists() for p in rom_media(system, SAMPLE_ROM_PATH).values())
    return "undo refused after an outside rewrite; forced undo restored the original and kept the discarded file"


@check("saver.symlink_refused")
def t_saver_symlink(ctx):
    from scrappy.saver import SaveError
    root = ctx.sub("saver-link")
    system = build_system(root)
    elsewhere = ctx.sub("saver-link-elsewhere")
    gl = system / "gamelist.xml"
    original = gl.read_bytes()
    shutil.rmtree(system / "images")
    try:
        os.symlink(elsewhere, system / "images")
    except OSError as exc:
        raise Skip(f"cannot create symbolic links here ({exc.strerror})")
    saver = make_saver(root)
    expect_raises(SaveError, lambda: saver.save(req(system, ctx=ctx)), "symlinked images/ folder")
    assert not list(elsewhere.iterdir()), "files were written through the symlink"
    assert gl.read_bytes() == original, "gamelist.xml changed although the save was refused"
    os.unlink(system / "images")
    (system / "images").mkdir()
    victim = elsewhere / "victim.jpg"
    os.symlink(victim, system / "images" / "Tidepool Tactics-image.jpg")
    expect_raises(SaveError, lambda: saver.save(req(system, ctx=ctx)), "media file that is a symlink to elsewhere")
    assert not victim.exists() and not list(elsewhere.iterdir()), "a file was written through a symlinked media file"
    assert gl.read_bytes() == original
    return "symlinked images/ folder and symlinked media file both refused; nothing written outside, gamelist untouched"


@check("saver.damaged_and_missing_gamelist")
def t_saver_damaged(ctx):
    from scrappy.saver import SaveError
    root = ctx.sub("saver-damaged")
    system = build_system(root)
    gl = system / "gamelist.xml"
    gl.write_bytes(b"<gameList><game><path>./x.nsp</path>")           # cut off in the middle: damaged
    damaged = gl.read_bytes()
    saver = make_saver(root)
    expect_raises(SaveError, lambda: saver.save(req(system, ctx=ctx)), "damaged gamelist.xml")
    assert gl.read_bytes() == damaged, "a damaged gamelist.xml must never be overwritten"
    assert not any(p.exists() for p in rom_media(system, SAMPLE_ROM_PATH).values()), "media written although the gamelist was unusable"
    root2 = ctx.sub("saver-nogamelist")
    system2 = build_system(root2, with_gamelist=False)
    make_saver(root2).save(req(system2, ctx=ctx))
    assert game_tags(system2)["image"], "a missing gamelist.xml must be started fresh"
    return "damaged gamelist.xml refused and left as it was; missing one started fresh"


@check("saver.dry_run")
def t_saver_dry(ctx):
    root = ctx.sub("saver-dry")
    system = build_system(root)
    before = snapshot(system)
    dry = root / "dry"
    report = make_saver(root, dry_run_dir=dry).save(req(system, ctx=ctx))
    assert report.dry_run is True
    assert snapshot(system) == before, "a dry run changed the real system folder"
    assert (dry / "switch" / "images" / "Tidepool Tactics-image.jpg").is_file(), "the dry run left no sandbox copy"
    return "dry run changed nothing in the real folder"


# ---------------------------------------------------------------- downloads against a local server

class Site:
    """A tiny local web server; `routes` maps a path to a function(handler)."""

    def __init__(self):
        self.hits = collections.Counter()
        self.stop = threading.Event()
        self.routes = {}
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                site.hits[self.path] += 1
                route = site.routes.get(self.path)
                if route is None:
                    return self.reply(404, b"no such page")
                try:
                    route(self)
                except OSError:
                    pass                                              # the client hung up: part of the tests

            def reply(self, code, body=b"", headers=()):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                for k, v in headers:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

        class Quiet(http.server.ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass

        self.httpd = Quiet(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def close(self):
        self.stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()


def local_only(ip) -> bool:
    return ip == "127.0.0.1"


def with_site(ctx):
    site = Site()
    ctx.shared.setdefault("sites", []).append(site)
    return site


@check("download.address_policy")
def t_download_policy(ctx):
    from scrappy.download import BlockedDestination, check_destination, download_bytes, is_public_address
    private = ["127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.10", "169.254.169.254", "0.0.0.0", "100.64.0.1", "224.0.0.1",
               "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "64:ff9b::7f00:1", "2002:7f00:1::", "not-an-ip"]
    public = ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111", "::ffff:8.8.8.8"]
    wrong = [ip for ip in private if is_public_address(ip)] + [ip for ip in public if not is_public_address(ip)]
    assert not wrong, f"is_public_address gives the wrong answer for: {wrong}"
    try:
        expect_raises(BlockedDestination, lambda: check_destination("localhost", 80), "localhost")
    except AssertionError:
        raise
    except Exception:
        pass
    site = with_site(ctx)
    site.routes["/ok"] = lambda h: h.reply(200, b"x" * 100)
    expect_raises(BlockedDestination, lambda: download_bytes(site.base + "/ok", deadline=5), "default policy against 127.0.0.1")
    assert site.hits["/ok"] == 0, "the downloader contacted a server on this machine although the policy forbids it"
    return f"{len(private)} private/odd and {len(public)} public addresses classified right on Python {sys.version.split()[0]}; loopback server never contacted"


@check("download.fetch_and_cache")
def t_download_fetch(ctx):
    from scrappy.download import DownloadError, download_bytes, download_to_file, fetch_cached
    site = with_site(ctx)
    site.routes["/ok"] = lambda h: h.reply(200, b"a" * 1000, [("Content-Type", "image/png")])
    site.routes["/redir"] = lambda h: h.reply(302, b"", [("Location", "/ok")])
    folder = ctx.sub("download")
    kw = dict(address_policy=local_only, deadline=10, read_timeout=3, connect_timeout=3)
    assert download_bytes(site.base + "/ok", **kw) == b"a" * 1000
    assert download_bytes(site.base + "/redir", **kw) == b"a" * 1000, "a relative redirect was not followed"
    dest = download_to_file(site.base + "/ok", folder / "sub" / "got.bin", **kw)
    assert dest.read_bytes() == b"a" * 1000
    err = expect_raises(DownloadError, lambda: download_to_file(site.base + "/nothing", folder / "bad.bin", **kw), "404 page")
    assert not (folder / "bad.bin").exists() and not [p for p in folder.iterdir() if p.name.endswith(".part")], "a failed download left files"
    assert err.args and "404" in str(err) or "found" in str(err).lower(), f"unhelpful message: {err}"
    site.hits.clear()
    first = fetch_cached(folder / "cache", site.base + "/ok", **kw)
    second = fetch_cached(folder / "cache", site.base + "/ok", **kw)
    assert first == second and site.hits["/ok"] == 1, f"fetch_cached asked the server {site.hits['/ok']} times"
    return "GET, relative redirect, file download (atomic), 404 message, cache hit all fine"


@check("download.size_cap")
def t_download_cap(ctx):
    from scrappy.download import DownloadError, DownloadTooLarge, download_bytes
    site = with_site(ctx)
    site.routes["/declared"] = lambda h: h.reply(200, b"b" * 300_000)

    def stream(h):                                  # no Content-Length: the size is unknown until the connection closes
        h.send_response(200)
        h.send_header("Connection", "close")
        h.end_headers()
        h.close_connection = True
        for _ in range(60):
            h.wfile.write(b"c" * 10_000)
            h.wfile.flush()

    site.routes["/stream"] = stream
    site.routes["/gzip"] = lambda h: h.reply(200, b"zz", [("Content-Encoding", "gzip")])
    kw = dict(address_policy=local_only, deadline=10, read_timeout=3, connect_timeout=3)
    expect_raises(DownloadTooLarge, lambda: download_bytes(site.base + "/declared", 100_000, **kw), "declared size above the cap")
    expect_raises(DownloadTooLarge, lambda: download_bytes(site.base + "/stream", 100_000, **kw), "unannounced size above the cap")
    expect_raises(DownloadError, lambda: download_bytes(site.base + "/gzip", **kw), "compressed answer")
    assert len(download_bytes(site.base + "/declared", 400_000, **kw)) == 300_000
    return "oversize refused both by Content-Length and mid-stream; compressed answers refused; within-cap download works"


@check("download.redirects")
def t_download_redirects(ctx):
    from scrappy.download import BlockedDestination, DownloadError, UnsupportedURL, download_bytes, system_resolver
    site = with_site(ctx)
    site.routes["/secret"] = lambda h: h.reply(200, b"internal")
    site.routes["/ok"] = lambda h: h.reply(200, b"fine")
    site.routes["/to-metadata"] = lambda h: h.reply(302, b"", [("Location", "http://169.254.169.254/latest/meta-data/")])
    site.routes["/to-v6-loopback"] = lambda h: h.reply(302, b"", [("Location", f"http://[::1]:{site.port}/secret")])
    site.routes["/to-private-name"] = lambda h: h.reply(302, b"", [("Location", f"http://intranet.test:{site.port}/secret")])
    site.routes["/to-file"] = lambda h: h.reply(302, b"", [("Location", "file:///etc/passwd")])
    site.routes["/to-ftp"] = lambda h: h.reply(302, b"", [("Location", "ftp://example.com/x")])
    site.routes["/loop"] = lambda h: h.reply(302, b"", [("Location", "/loop")])

    def resolver(host, port):
        return ["10.0.0.5"] if host == "intranet.test" else system_resolver(host, port)

    kw = dict(address_policy=local_only, resolver=resolver, deadline=10, read_timeout=3, connect_timeout=3)
    for path, exc in (("/to-metadata", BlockedDestination), ("/to-v6-loopback", BlockedDestination),
                      ("/to-private-name", BlockedDestination), ("/to-file", UnsupportedURL), ("/to-ftp", UnsupportedURL),
                      ("/loop", DownloadError)):
        expect_raises(exc, lambda p=path: download_bytes(site.base + p, **kw), f"redirect {path}")
    assert site.hits["/secret"] == 0, "a redirect target on a private address was contacted"
    return "redirects to metadata IP, ::1, a name resolving to 10.x, file://, ftp:// and loops all refused; /secret never hit"


@check("download.timeouts")
def t_download_timeouts(ctx):
    from scrappy.download import DownloadCancelled, DownloadTimeout, download_bytes
    site = with_site(ctx)
    site.routes["/silent"] = lambda h: site.stop.wait(15)

    def drip(h):
        h.send_response(200)
        h.send_header("Content-Length", "100000")
        h.end_headers()
        for _ in range(300):
            h.wfile.write(b"d")
            h.wfile.flush()
            if site.stop.wait(0.1):
                return

    site.routes["/drip"] = drip
    kw = dict(address_policy=local_only, connect_timeout=2)
    t0 = time.monotonic()
    expect_raises(DownloadTimeout, lambda: download_bytes(site.base + "/silent", read_timeout=0.5, deadline=3, **kw), "silent server")
    silent = time.monotonic() - t0
    assert silent < 3.6, f"a silent server held the download for {silent:.1f} s"
    t0 = time.monotonic()
    expect_raises(DownloadTimeout, lambda: download_bytes(site.base + "/drip", read_timeout=5, deadline=1.0, **kw), "dripping server")
    drip_time = time.monotonic() - t0
    assert drip_time < 3.0, f"a dripping server held the download for {drip_time:.1f} s"
    expect_raises(DownloadCancelled, lambda: download_bytes(site.base + "/drip", cancelled=lambda: True, deadline=5, **kw), "cancelled")
    return f"silent server gave up after {silent:.1f} s, dripping one after {drip_time:.1f} s, cancel honoured"


# ---------------------------------------------------------------- Qt

QT_PROGRAM = r"""
import os, sys
from PyQt5 import QtCore, QtGui, QtWidgets
app = QtWidgets.QApplication(["scrappy-selftest"])
label = QtWidgets.QLabel("Scrappy")
label.resize(220, 40)
pix = label.grab()
assert not pix.isNull() and pix.width() == 220 and pix.height() == 40, "widget rendering gave an empty picture"
image = QtGui.QImage(8, 8, QtGui.QImage.Format_ARGB32)
image.fill(QtGui.QColor(10, 20, 30))
assert image.pixelColor(3, 3).blue() == 30
print("PyQt5", QtCore.PYQT_VERSION_STR, "Qt", QtCore.qVersion(), "platform", app.platformName())
"""


def child_env(ctx, platform_name: str) -> dict:
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = platform_name
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["TMPDIR"] = str(ctx.sandbox / "tmp")
    parts = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    if str(ROOT) not in parts:
        parts.append(str(ROOT))
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env["HOME"] = str(ctx.sandbox)                 # nothing the child writes into a home folder escapes the sandbox
    return env


def qt_run(ctx, code: str, timeout=90):
    """Run `code` in a child Python with Qt's offscreen platform; falls back to xcb when offscreen is not built in."""
    tried = []
    for plat in ("offscreen", "xcb"):
        if plat == "xcb" and not os.environ.get("DISPLAY"):
            break
        out = run([sys.executable, "-c", code], timeout=timeout, env=child_env(ctx, plat), cwd=str(ctx.sandbox))
        if out.returncode == 0:
            return plat, out.stdout.strip()
        tried.append(f"{plat}: {(out.stderr.strip().splitlines() or ['exit code ' + str(out.returncode)])[-1][:200]}")
        if "platform plugin" not in out.stderr:
            break
    raise AssertionError("Qt failed (" + "; ".join(tried) + ")")


@check("qt.offscreen_app")
def t_qt_app(ctx):
    if importlib.util.find_spec("PyQt5") is None:
        ctx.missing("PyQt5 cannot be imported")
    plat, line = qt_run(ctx, QT_PROGRAM)
    return f"{line} (child process)"


@check("qt.app_modules_import")
def t_qt_app_import(ctx):
    if importlib.util.find_spec("PyQt5") is None:
        ctx.missing("PyQt5 cannot be imported")
    plat, line = qt_run(ctx, "import scrappy.app, scrappy.ui.main_window, scrappy.ui.widgets, scrappy.controller\nprint('imported scrappy.app and scrappy.ui')")
    return f"{line} with Qt platform {plat}"


# ---------------------------------------------------------------- opt-in: the internet

@check("network.steam", optin="network")
def t_network_steam(ctx):
    from scrappy.providers.base import ProviderError, describe_error
    from scrappy.providers.steam import SteamProvider
    steam = SteamProvider()
    try:
        matches = steam.search(NETWORK_TITLE)
        assert matches, f"Steam found nothing for {NETWORK_TITLE!r}"
        for m in matches[:4]:
            ctx.note(f"match: {m.title}  [{m.source_id}]  score {m.score:.0%}")
        info = steam.fetch(matches[0])
    except ProviderError as exc:
        raise AssertionError("Steam: " + describe_error(exc)) from exc
    ctx.shared["steam_info"] = info
    by_slot = collections.Counter(a.slot for a in info.assets)
    borrowed = [a for a in info.assets if "base game" in a.label]
    ctx.note(f"fetched: {info.name!r}, release {info.release_date or '-'}, developer {info.developer or '-'}, genre {info.genre or '-'}")
    ctx.note("description: " + (info.description[:100] + "..." if len(info.description) > 100 else info.description or "-"))
    ctx.note("assets: " + ", ".join(f"{slot} x{n}" for slot, n in sorted(by_slot.items())))
    for a in borrowed:
        ctx.note(f"base-game fallback: [{a.slot}] {a.label}")
    assert info.name and info.assets, "Steam returned no usable data"
    assert by_slot.get("image") and (by_slot.get("boxart") or by_slot.get("logo")), f"no screenshot / box art / logo candidates: {dict(by_slot)}"
    return f"{matches[0].title!r} app {matches[0].source_id}: {sum(by_slot.values())} assets, {len(borrowed)} labelled base-game fallback(s)"


@check("network.https_download", optin="network")
def t_network_download(ctx):
    from scrappy.download import DownloadTooLarge, download_bytes
    from scrappy.processing.images import open_image
    info = ctx.shared.get("steam_info")
    if info is None:
        raise Skip("no Steam result to take a picture from (network.steam did not pass)")
    asset = next((a for slot in ("logo", "boxart", "image") for a in info.assets if a.slot == slot), None)
    assert asset is not None, "Steam offered no picture"
    try:
        data = download_bytes(asset.url, 3_000_000, deadline=30)
    except DownloadTooLarge:
        return f"{asset.url} is above the 3 MB test cap: the cap works (nothing downloaded)"
    im = open_image(data)
    return f"{asset.slot} over HTTPS: {len(data)} bytes, decoded as {im.size[0]}x{im.size[1]} {im.mode}"


@check("youtube.search", optin="youtube")
def t_youtube(ctx):
    from scrappy.providers.youtube import YtDlp, YouTubeError, youtube_query
    if ctx.args.ytdlp_dir:
        bin_dir = Path(ctx.args.ytdlp_dir)
    else:
        try:
            from scrappy.config import DEFAULTS
            home = DEFAULTS["data_dir"]
        except Exception:
            home = DEFAULT_HOME
        bin_dir = Path(os.environ.get("SCRAPPY_HOME", home)) / "bin"
    yt = YtDlp(bin_dir)
    if not yt.available():
        raise Skip(f"yt-dlp is not installed at {yt.path} (in the app: Tools > Install / update YouTube downloader)")
    try:
        found = yt.search(youtube_query(NETWORK_TITLE, "windows"), count=3, title=NETWORK_TITLE)
    except YouTubeError as exc:
        raise AssertionError(f"YouTube search failed: {exc}") from exc
    assert found, "yt-dlp returned no videos"
    for c in found:
        ctx.note(f"video: {c.label[:90]}  {c.duration:.0f}s  {c.url}")
        assert c.url.startswith("https://www.youtube.com/watch?v="), f"unexpected URL {c.url}"
    return f"{len(found)} result(s) via {yt.path}; nothing downloaded"


# ---------------------------------------------------------------- running

def pick(args):
    wanted = [p for p in (args.only or "").split(",") if p]
    return [(n, f, o) for n, f, o in CHECKS if not wanted or any(n.startswith(w) for w in wanted)]


def run_checks(ctx, say) -> list:
    results = []
    for name, fn, optin in pick(ctx.args):
        ctx.notes = []
        t0 = time.perf_counter()
        tb = ""
        if optin and not getattr(ctx.args, optin):
            status, detail = "SKIP", f"not requested (add --{optin})"
        else:
            try:
                status, detail = "PASS", fn(ctx) or ""
            except Skip as exc:
                status, detail = "SKIP", str(exc)
            except AssertionError as exc:
                status, detail, tb = "FAIL", str(exc) or "assertion failed", traceback.format_exc()
            except Exception as exc:
                status, detail, tb = "FAIL", f"{type(exc).__name__}: {exc}", traceback.format_exc()
        seconds = time.perf_counter() - t0
        results.append({"name": name, "status": status, "detail": detail, "seconds": round(seconds, 3),
                        "notes": list(ctx.notes), **({"traceback": tb} if tb else {})})
        say(f"{status:<4}  {name:<34} {seconds:6.2f}s  {detail}")
        if status != "SKIP" or ctx.args.verbose:
            for note in ctx.notes:
                say(f"            {note}")
        if tb and ctx.args.verbose:
            say(tb)
    return results


def refuse_if_protected(path: Path) -> None:
    real = os.path.realpath(path)
    for guard in PROTECTED:
        guard_real = os.path.realpath(guard)
        if real == guard_real or real.startswith(guard_real + os.sep):
            raise SystemExit(f"refusing to use {path} as sandbox parent: it is inside {guard}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="On-box self-test of Scrappy (works in a throw-away sandbox).")
    parser.add_argument("--network", action="store_true", help="also do one real Steam search+fetch and one small HTTPS download")
    parser.add_argument("--youtube", action="store_true", help="also do one yt-dlp search (nothing is downloaded)")
    parser.add_argument("--json", action="store_true", help="print a JSON summary on stdout (human lines go to stderr)")
    parser.add_argument("--keep", action="store_true", help="keep the sandbox folder and print where it is")
    parser.add_argument("--skip-missing", action="store_true", help="SKIP instead of FAIL when ffmpeg or PyQt5 is missing")
    parser.add_argument("--only", metavar="PREFIX[,PREFIX]", help="run only checks whose name starts with one of these (e.g. saver,video.encode)")
    parser.add_argument("--sandbox-parent", metavar="DIR", help="where to create the sandbox (default: the temp folder)")
    parser.add_argument("--ytdlp-dir", metavar="DIR", help="folder that holds the yt-dlp file (default: <data folder>/bin)")
    parser.add_argument("--verbose", action="store_true", help="show notes of skipped checks and tracebacks of failures")
    parser.add_argument("--list", action="store_true", help="list the checks and exit")
    args = parser.parse_args(argv)
    if args.list:
        for name, _fn, optin in CHECKS:
            print(name + (f"   (opt-in: --{optin})" if optin else ""))
        return 0

    parent = Path(args.sandbox_parent or tempfile.gettempdir())
    if not parent.is_dir():
        raise SystemExit(f"{parent} is not a folder")
    refuse_if_protected(parent)
    sandbox = Path(tempfile.mkdtemp(prefix="scrappy-selftest-", dir=parent)).resolve()
    refuse_if_protected(sandbox)
    (sandbox / "tmp").mkdir()
    old_tmp, old_env_tmp = tempfile.tempdir, os.environ.get("TMPDIR")
    tempfile.tempdir = str(sandbox / "tmp")             # stray temp files of libraries land in the sandbox too
    os.environ["TMPDIR"] = str(sandbox / "tmp")

    def say(text):
        print(text, file=sys.stderr if args.json else sys.stdout, flush=True)

    ctx = Ctx(sandbox, args)
    started = time.perf_counter()
    say(f"Scrappy self-test  (sandbox {sandbox})")
    results = []
    try:
        results = run_checks(ctx, say)
    finally:
        for site in ctx.shared.get("sites", []):
            try:
                site.close()
            except Exception:
                pass
        tempfile.tempdir = old_tmp
        if old_env_tmp is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = old_env_tmp
        if args.keep:
            say(f"sandbox kept: {sandbox}")
        else:
            shutil.rmtree(sandbox, ignore_errors=True)
    counts = collections.Counter(r["status"] for r in results)
    total = time.perf_counter() - started
    ok = counts["FAIL"] == 0
    say(f"\n{counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIP']} skipped in {total:.1f} s")
    say("BOX SELFTEST OK" if ok else "BOX SELFTEST FAILED: " + ", ".join(r["name"] for r in results if r["status"] == "FAIL"))
    if args.json:
        json.dump({"ok": ok, "passed": counts["PASS"], "failed": counts["FAIL"], "skipped": counts["SKIP"], "seconds": round(total, 2),
                   "sandbox": str(sandbox) if args.keep else None, "environment": ctx.env, "checks": results},
                  sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
