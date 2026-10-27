"""Short real trailers for the showcase library: animated versions of the pixel-art scenes, encoded by ffmpeg.

Six seconds, 640x360, 24 fps, H.264 yuv420p with a silent AAC track. Frames are drawn here (scrolling parallax layers,
bobbing hero, a title card at the end) and piped to ffmpeg as raw pictures. If ffmpeg is missing or fails, the old tiny
placeholder file is returned, so nothing in the demo crashes.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

from . import demo_art
from .demo_data import DemoGame

SECONDS, FPS, SIZE = 6, 24, (640, 360)
CARD_AT = 4.4                                    # the title card starts here (seconds)
SCROLL = 70.0                                    # pixels of the 320 px wide scene per second
MP4_STUB = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64
ENCODE_TIMEOUT = 120


def _frames(game: DemoGame):
    scene = demo_art.Scene(game)
    logo = demo_art.logo_image(game, 0, 1000).convert("RGBA")
    ratio = min(250 / logo.width, 110 / logo.height)
    logo = logo.resize((max(1, round(logo.width * ratio)), max(1, round(logo.height * ratio))), Image.LANCZOS)
    score0 = demo_art.rng(game.title, "video-score").randrange(100, 900) * 50
    for n in range(SECONDS * FPS):
        t = n / FPS
        frame = scene.render(cam=t * SCROLL, t=t, jump=(t / 1.1) % 1.0, score=score0 + int(t * 6) * 50)
        if t >= CARD_AT:
            k = min(1.0, (t - CARD_AT) / 0.6)                                     # fade the picture down, the logo up
            card = frame.convert("RGBA")
            card.alpha_composite(Image.new("RGBA", frame.size, (10, 6, 28, int(170 * k))))
            faded = logo.copy()
            faded.putalpha(faded.getchannel("A").point(lambda v, k=k: int(v * k)))
            card.alpha_composite(faded, ((frame.width - faded.width) // 2, (frame.height - faded.height) // 2 - 6))
            frame = card.convert("RGB")
        if game.skin == "lcd":
            frame = demo_art.lcd_skin(frame)
        yield np.asarray(frame.resize(SIZE, Image.NEAREST), dtype=np.uint8).tobytes()


def encode_frames(frames, dest) -> bool:
    """Pipe raw RGB frames (``SIZE``, ``FPS``, ``SECONDS`` long) to ffmpeg and write the clip to `dest`. False (and `dest`
    untouched) when ffmpeg is not there or fails. Shared with tools/real_showcase.py, so both showcases use one encoder."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    dest = Path(dest)
    tmp = dest.with_name(dest.name + ".part")
    cmd = [ffmpeg, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{SIZE[0]}x{SIZE[1]}", "-r", str(FPS), "-i", "-",
           "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t", str(SECONDS),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "27", "-pix_fmt", "yuv420p", "-profile:v", "main",
           "-c:a", "aac", "-b:a", "24k", "-shortest", "-map_metadata", "-1", "-movflags", "+faststart", "-f", "mp4", str(tmp)]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except OSError:
        return False
    try:
        for raw in frames:
            proc.stdin.write(raw)
        proc.stdin.close()
        proc.wait(timeout=ENCODE_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        proc.kill()
        proc.wait()
        tmp.unlink(missing_ok=True)
        return False
    finally:
        if proc.stderr:
            proc.stderr.close()
    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(dest)
    return True


def render_video(game: DemoGame, dest) -> bool:
    """Write the trailer of `game` to `dest`. False (and `dest` untouched) when ffmpeg is not there or fails."""
    return encode_frames(_frames(game), dest)


def make_video(game: DemoGame, dest) -> Path:
    """The trailer at `dest` (made now if it does not exist yet); the placeholder file when ffmpeg is not usable."""
    dest = Path(dest)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not render_video(game, dest):
        dest.write_bytes(MP4_STUB)
    return dest
