"""Cut and encode a trailer into the format the theme plays: MP4, H.264 1280x720, 30 fps, about 1.5 Mbps, faststart."""
from __future__ import annotations

import functools
import json
import math
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

# Non-square pixels are made square first (sar), then the picture is fitted inside 1280x720 and padded with
# black bars, never stretched. Even dimensions keep yuv420p happy.
VIDEO_FILTER = ("scale=iw*sar:ih,setsar=1,"
                "scale=1280:720:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                "pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30,format=yuv420p")

# The URL comes from web content (an HLS playlist can name any other URL, including file:// and
# concat:), so ffmpeg may only speak the protocols a plain HTTP(S) stream needs. A local file gets "file" only.
# "httpproxy" is what ffmpeg opens internally when http_proxy/https_proxy is set; "data" is base64 inline data
# (HLS AES keys) and cannot reach anything.
URL_PROTOCOLS = "http,https,tcp,tls,crypto,httpproxy,data"
FILE_PROTOCOLS = "file"

MIN_SECONDS = 1.0            # shortest clip we ask for (unless the whole video is shorter)
DEFAULT_TIMEOUT = 1800.0     # seconds for one encode, download included
PROBE_TIMEOUT = 60.0
NETWORK_IO_TIMEOUT_US = "30000000"     # ffmpeg gives up on a stalled connection after 30 s
POLL_SECONDS = 0.1           # how often a running encode checks for cancellation and timeout
STDERR_TAIL = 400

_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")


class VideoError(RuntimeError):
    """Encoding failed."""


class EncodeTimeout(VideoError):
    """ffmpeg ran longer than allowed and was killed."""


class EncodeCancelled(VideoError):
    """The caller cancelled; ffmpeg was killed."""


# ---------------------------------------------------------------- argument building

def _classify(src) -> tuple[str, bool]:
    """(safe argument for -i, is_url). Only http(s) URLs and plain file paths are accepted."""
    text = str(src)
    if not text:
        raise ValueError("empty video source")
    if _SCHEME.match(text):
        parts = urlsplit(text)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            raise ValueError(f"unsupported video source: {text[:80]!r}")
        return text, True
    return _dash_safe(text), False


def _dash_safe(path) -> str:
    """A path that begins with '-' would be read as an option; './' makes it a path."""
    text = str(path)
    return "./" + text if text.startswith("-") else text


def _finite(value, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def _window(start, end, max_seconds, duration) -> tuple[float, float]:
    """(start, length) in seconds, limited by max_seconds and, when known, by the real duration."""
    start = max(0.0, _finite(start or 0, "start"))
    max_seconds = _finite(max_seconds, "max_seconds")
    if end is None:
        length = max_seconds
    else:
        length = _finite(end, "end") - start
    length = max(MIN_SECONDS, min(length, max_seconds))
    if duration is not None and math.isfinite(duration) and duration > 0:
        start = min(start, max(0.0, duration - MIN_SECONDS))
        length = min(length, duration - start)
    return start, length


def build_encode_cmd(src, dst, start=0.0, end=None, max_seconds=120, bitrate="1500k", ffmpeg="ffmpeg",
                     duration=None) -> list:
    """The ffmpeg argument list (never a shell string) that encodes src[start:end] into dst.

    `src` is a local path or an http(s) URL (e.g. an HLS playlist); anything else raises ValueError.
    `duration` is the probed length of the source in seconds; start and end are clamped to it.
    """
    source, is_url = _classify(src)
    start, length = _window(start, end, max_seconds, duration)
    cmd = [ffmpeg, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
           "-protocol_whitelist", URL_PROTOCOLS if is_url else FILE_PROTOCOLS]
    if is_url:
        cmd += ["-rw_timeout", NETWORK_IO_TIMEOUT_US]
    if start >= 0.005:
        cmd += ["-ss", f"{start:.2f}"]
    cmd += ["-i", source, "-t", f"{length:.2f}",
            "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1",
            "-vf", VIDEO_FILTER,
            "-c:v", "libx264", "-profile:v", "high", "-preset", "medium",
            "-b:v", bitrate, "-maxrate", "2200k", "-bufsize", "4400k",
            "-c:a", "aac", "-b:a", "128k", "-ac", "2",
            "-movflags", "+faststart", "-f", "mp4", _dash_safe(dst)]
    return cmd


# ---------------------------------------------------------------- probing

def probe_duration(src, ffprobe="ffprobe", runner=subprocess.run, timeout=PROBE_TIMEOUT) -> float:
    """Length of `src` in seconds, or 0.0 when it cannot be determined (unreadable, no duration, timed out)."""
    source, is_url = _classify(src)
    cmd = [ffprobe, "-v", "error", "-protocol_whitelist", URL_PROTOCOLS if is_url else FILE_PROTOCOLS]
    if is_url:
        cmd += ["-rw_timeout", NETWORK_IO_TIMEOUT_US]
    cmd += ["-show_entries", "format=duration", "-of", "json", "-i", source]
    try:
        result = runner(cmd, capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL,
                        timeout=timeout)
    except subprocess.TimeoutExpired:
        return 0.0
    if result.returncode != 0:
        return 0.0
    try:
        value = float(json.loads(result.stdout)["format"]["duration"])
    except (ValueError, KeyError, TypeError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


# ---------------------------------------------------------------- running

def _kill(proc) -> None:
    """Kill and reap the process so it never lingers as a zombie."""
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.communicate(timeout=10)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass


def _run(cmd, timeout, cancelled, popen) -> tuple[int, str]:
    """Run ffmpeg to completion, killing it on timeout, cancellation or any exception. -> (returncode, stderr)."""
    proc = popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                 text=True, errors="replace")
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancelled is not None and cancelled():
                raise EncodeCancelled("encoding cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EncodeTimeout(f"ffmpeg did not finish within {timeout:g} seconds")
            try:
                _, stderr = proc.communicate(timeout=min(POLL_SECONDS, remaining))
            except subprocess.TimeoutExpired:
                continue
            return proc.returncode, stderr or ""
    except BaseException:
        _kill(proc)
        raise


def encode_clip(src, dst, start=0.0, end=None, max_seconds=120, bitrate="1500k", ffmpeg="ffmpeg",
                ffprobe="ffprobe", duration: Optional[float] = None, timeout: float = DEFAULT_TIMEOUT,
                cancelled: Optional[Callable[[], bool]] = None, probe: Optional[Callable] = None,
                popen=subprocess.Popen):
    """Encode `src` (file path or http(s) URL, e.g. an HLS playlist) into `dst`.

    The output is written to a temporary file next to `dst` and moved into place only when ffmpeg succeeded, so a
    failure, timeout or cancellation leaves any existing `dst` untouched and no leftovers. `cancelled` is polled
    about ten times a second; when it returns True ffmpeg is killed and EncodeCancelled is raised.
    Raises VideoError (RuntimeError) on failure, EncodeTimeout after `timeout` seconds, ValueError for a source
    that is neither a file path nor an http(s) URL.
    """
    _, is_url = _classify(src)
    target = Path(os.path.realpath(dst))                          # write through a symlinked destination
    if not is_url and os.path.realpath(str(src)) == str(target):
        raise ValueError("source and destination are the same file")
    if cancelled is not None and cancelled():
        raise EncodeCancelled("encoding cancelled")
    if duration is None:
        probe = probe or functools.partial(probe_duration, ffprobe=ffprobe)
        try:
            duration = probe(src)
        except OSError:
            duration = 0.0
    tmp = target.with_name(f"{target.name}.{uuid.uuid4().hex[:8]}.tmp")
    cmd = build_encode_cmd(src, tmp, start, end, max_seconds, bitrate, ffmpeg, duration)
    try:
        try:
            returncode, stderr = _run(cmd, timeout, cancelled, popen)
        except OSError as exc:
            raise VideoError(f"cannot run {ffmpeg}: {exc}") from exc
        if returncode != 0:
            raise VideoError("ffmpeg failed: " + stderr.strip()[-STDERR_TAIL:])
        try:
            size = os.stat(tmp).st_size
        except OSError:
            size = 0
        if size == 0:
            raise VideoError("ffmpeg produced no output")
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dst
