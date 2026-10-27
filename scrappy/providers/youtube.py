"""YouTube trailers through yt-dlp: a single-file download (a Python zipapp) run with the system Python.

yt-dlp's output and the web are untrusted. A video is only ever identified by an 11-character id that passed
validation; every URL handed to yt-dlp is rebuilt from that id, text is cleaned before it reaches the UI, thumbnail
URLs must be https on *.ytimg.com, and yt-dlp's own messages never reach the user except as one short sanitised line.
Programs are started with argument lists (never a shell), and the user's text always comes after `--`.
"""
from __future__ import annotations

import http.client
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit

from ..models import AssetCandidate
from ..titlematch import normalize
from .base import ProviderError

YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"
INSTALL_HINT = "Choose Tools > Install / update YouTube downloader"

SYSTEM_HINTS = {"switch": "Nintendo Switch", "windows": "PC", "ps2": "PS2", "ps3": "PS3", "psp": "PSP",
                "wiiu": "Wii U", "wii": "Wii", "gamecube": "GameCube", "xbox360": "Xbox 360", "n64": "Nintendo 64",
                "snes": "SNES", "nes": "NES", "psx": "PlayStation"}

# limits
MAX_QUERY = 200
MAX_COUNT = 20                  # most candidates one search returns
MAX_SEARCH_FETCH = 20           # most results asked from YouTube (extra ones make up for filtered-out videos)
MAX_RESULTS_PARSED = 50
MAX_LINE = 200_000              # a longer line of yt-dlp output is ignored
MAX_LABEL_TITLE = 120
MIN_SECONDS = 10                # shorter videos are Shorts or teasers
MAX_SECONDS = 20 * 60           # longer videos are full games, streams, compilations
MAX_HEIGHT = 720
MIN_HEIGHT = 144
MAX_DOWNLOAD_SIZE = "300M"      # yt-dlp skips a bigger file (a 720p trailer is far below this)
SEARCH_TIMEOUT = 90.0
DOWNLOAD_TIMEOUT = 1200.0
POLL_SECONDS = 0.1              # how often a running process checks for cancellation and timeout
MAX_STDOUT = 2_000_000          # more output than this kills the process
STDERR_KEEP = 8192              # only the tail of stderr is kept
MAX_BINARY_BYTES = 25_000_000   # yt-dlp is about 3 MB
BINARY_TIMEOUT = 60
SMOKE_TIMEOUT = 30.0            # `yt-dlp --version` of a freshly downloaded copy
MESSAGE_TAIL = 160              # characters of yt-dlp's last error line shown to the user
OUTPUT_EXTENSIONS = (".mp4", ".webm", ".mkv")

_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_YOUTUBE_HOSTS = ("www.youtube.com", "youtube.com", "m.youtube.com")
_THUMB_SUFFIX = ".ytimg.com"
_GITHUB_SUFFIXES = (".github.com", ".githubusercontent.com")
# invalid in XML / UTF-8, plus the invisible bidi controls that can make text read differently from how it is stored
_CONTROL_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200e\u200f\u202a-\u202e\u2066-\u2069"
                            "\ud800-\udfff\ufffe\uffff]")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


class YouTubeError(ProviderError):
    """YouTube access failed; the message is plain text for the user."""


class YouTubeTimeout(YouTubeError):
    """yt-dlp ran longer than allowed and was killed."""


class YouTubeCancelled(YouTubeError):
    """The caller cancelled; yt-dlp was killed."""


@dataclass
class InstallResult:
    """What ``YtDlp.install`` did: the file, the version that is installed now and what it replaced."""
    path: Path
    version: str = ""                  # "" when the new copy did not print a version
    previous_version: str = ""         # "" when there was no copy before, or the old copy could not tell
    was_installed: bool = False        # a copy was there already: this was an update

    @property
    def unchanged(self) -> bool:
        """An update that found the same version again."""
        return bool(self.was_installed and self.version and self.version == self.previous_version)


@dataclass
class YouTubeCandidate(AssetCandidate):
    """A video candidate that also remembers what ranking needs."""
    title: str = ""
    channel: str = ""
    verified: bool = False         # YouTube's own "verified channel" badge


# ---------------------------------------------------------------- small helpers

def youtube_query(title: str, system: str = "") -> str:
    hint = SYSTEM_HINTS.get(system, "") if isinstance(system, str) else ""
    return " ".join(x for x in (title, hint, "official trailer") if x)


def _text(value) -> str:
    """A plain one-line string from a str or number; anything else is ''."""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return " ".join(_CONTROL_CHARS.sub("", _ANSI.sub("", value)).split())
    return ""


def _version_text(stdout) -> str:
    """The first line of `yt-dlp --version` as short plain text (e.g. 2026.08.19), '' if there is none."""
    lines = [ln for ln in (_text(raw) for raw in str(stdout or "").splitlines()) if ln]
    return _shorten(lines[0], 40) if lines else ""


def _content_length(response):
    """The size the server announced for a download, or None when it did not (or sent something odd)."""
    headers = getattr(response, "headers", None)
    value = headers.get("Content-Length", "") if headers is not None and hasattr(headers, "get") else ""
    return int(value) if isinstance(value, str) and value.isdigit() and int(value) > 0 else None


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def _duration(value) -> float:
    """Seconds as a finite non-negative number, else 0.0 (unknown)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if math.isfinite(value) and value > 0 else 0.0


def _fmt_duration(seconds: float) -> str:
    if not seconds:
        return "?"
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def video_id_from_url(url) -> str:
    """The 11-character video id of a https YouTube watch / youtu.be link (or a bare id); '' if it is anything else."""
    if not isinstance(url, str):
        return ""
    text = url.strip()
    if _VIDEO_ID.fullmatch(text):
        return text
    if not text or len(text) > 2048 or _CONTROL_CHARS.search(text) or any(c.isspace() for c in text):
        return ""
    try:
        parts = urlsplit(text)
        host, port = parts.hostname, parts.port
    except ValueError:
        return ""
    if parts.scheme.lower() != "https" or parts.username or parts.password or port not in (None, 443):
        return ""
    if host in _YOUTUBE_HOSTS and parts.path == "/watch":
        value = (parse_qs(parts.query).get("v") or [""])[0]
    elif host == "youtu.be" and parts.path.startswith("/"):
        value = parts.path[1:]
    else:
        return ""
    return value if _VIDEO_ID.fullmatch(value) else ""


def _watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _thumb_url(value) -> str:
    """The URL if it is plain https on *.ytimg.com (YouTube's picture servers), else ''."""
    if not isinstance(value, str):
        return ""
    url = value.strip()
    if not url or len(url) > 2048 or _CONTROL_CHARS.search(url) or any(c.isspace() for c in url):
        return ""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return ""
    if parts.scheme.lower() != "https" or not host or parts.username or parts.password or port not in (None, 443):
        return ""
    return url if host.endswith(_THUMB_SUFFIX) else ""


def _best_thumbnail(thumbnails, video_id: str) -> str:
    best, best_area = "", -1
    if isinstance(thumbnails, list):
        for thumb in thumbnails:
            if not isinstance(thumb, dict):
                continue
            url = _thumb_url(thumb.get("url"))
            width, height = thumb.get("width"), thumb.get("height")
            area = width * height if all(isinstance(n, int) and not isinstance(n, bool) and n > 0
                                         for n in (width, height)) else 0
            if url and area >= best_area:
                best, best_area = url, area
    return best or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


# ---------------------------------------------------------------- parsing and ranking

def parse_search_output(text) -> list:
    """YouTubeCandidates from `yt-dlp --flat-playlist --dump-json` output (one JSON object per line).

    Real shape (yt-dlp 2026.08.19): per line {"_type": "url", "id": "<11 chars>", "title", "duration": seconds (int, or
    null for live streams), "channel", "uploader", "channel_is_verified": bool, "live_status", "availability",
    "thumbnails": [{"url", "width", "height"}, ...]}. Anything malformed is skipped; only the id is trusted to
    build the video URL (the entry's own "url" is ignored).
    """
    if not isinstance(text, str):
        return []
    found, seen = [], set()
    for line in text.splitlines():
        if len(found) >= MAX_RESULTS_PARSED:
            break
        line = line.strip()
        if not line.startswith("{") or len(line) > MAX_LINE:
            continue
        try:
            data = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(data, dict):
            continue
        video_id = data.get("id")
        if not isinstance(video_id, str) or not _VIDEO_ID.fullmatch(video_id) or video_id in seen:
            continue
        if data.get("live_status") in ("is_live", "is_upcoming", "post_live") \
                or data.get("availability") in ("needs_auth", "subscriber_only", "premium_only"):
            continue
        seen.add(video_id)
        title = _shorten(_text(data.get("title")), MAX_LABEL_TITLE)
        channel = _shorten(_text(data.get("channel")) or _text(data.get("uploader")), 60)
        seconds = _duration(data.get("duration"))
        found.append(YouTubeCandidate(
            slot="video", url=_watch_url(video_id), source="YouTube", kind="youtube",
            label=f"{title} [{_fmt_duration(seconds)}] {channel}".strip(),
            thumb_url=_best_thumbnail(data.get("thumbnails"), video_id), duration=seconds,
            title=title, channel=channel, verified=data.get("channel_is_verified") is True))
    return found


_PLUS = ((re.compile(r"\btrailer\b"), 3.0), (re.compile(r"\bofficial\b"), 2.0),
         (re.compile(r"\b(?:launch|announcement|reveal|teaser|cinematic)\b"), 1.0))
# fan content about the game rather than the publisher's own promotion
_MINUS = re.compile(r"\b(?:react(?:s|ion|ions|ing)?|reviews?|reviewed|walkthrough|walk through|let'?s play|playthrough|"
                    r"play through|longplay|no commentary|commentary|full game|tutorial|guide|how to|explained|"
                    r"theory|theories|easter eggs?|tier list|top \d+|parody|fan ?made|part \d+|ending|vs|shorts?)\b")
_GAMEPLAY_ONLY = re.compile(r"\bgameplay\b(?!\s+(?:reveal\s+)?trailer)")
_OFFICIAL_CHANNELS = ("nintendo", "playstation", "xbox", "steam", "bethesda", "ubisoft", "capcom", "sega", "bandai namco",
                      "square enix", "ign", "gamespot", "game trailers", "gametrailers")


def _score(candidate, wanted_words: list, position: int) -> tuple:
    words = normalize(candidate.title).split()
    lowered = " ".join(words)
    # The game's own name may contain a penalty word ('Marvel vs. Capcom'): judge only the rest of the title.
    own = set(wanted_words)
    rest = " ".join(w for w in words if w not in own)
    score = sum(weight for pattern, weight in _PLUS if pattern.search(lowered))
    if _MINUS.search(rest) or _GAMEPLAY_ONLY.search(rest):
        score -= 5.0
    if candidate.verified:
        score += 2.0
    channel = normalize(candidate.channel)
    if any(re.search(rf"\b{re.escape(name)}\b", channel) for name in _OFFICIAL_CHANNELS):
        score += 1.0
    if wanted_words:
        found = set(words)
        score += 3.0 * sum(w in found for w in wanted_words) / len(wanted_words)
    if not candidate.duration:
        score -= 0.5                 # unknown length
    elif 30 <= candidate.duration <= 300:
        score += 1.0                 # the usual trailer length
    return (-score, position)        # best first; YouTube's own order breaks ties


def rank_candidates(candidates, title: str = "", min_seconds: float = MIN_SECONDS,
                    max_seconds: float = MAX_SECONDS) -> list:
    """Drop Shorts and very long videos, then put the likeliest official trailers first.

    A video of unknown length is kept (a flat search sometimes lacks it) but ranks a little lower. Score, highest
    first: +3 'trailer' in the title, +2 'official', +1 launch/announcement/reveal/teaser/cinematic, +2 YouTube
    verified channel, +1 a well-known publisher or platform channel, up to +3 for how many words of `title` (the
    game's name, when given) the video title contains, +1 for the usual 30 s to 5 min length, -5 for fan content
    (reaction, review, walkthrough, 'no commentary', explained, 'gameplay' without 'trailer', 'shorts', ...;
    words that are part of `title` itself, like 'vs' in 'Marvel vs. Capcom', do not count). Ties keep
    YouTube's own order. The user picks from a short list, so this only has to get the obvious ones on top.
    """
    wanted = normalize(title).split()
    kept = [c for c in candidates if not c.duration or min_seconds <= c.duration <= max_seconds]
    scored = sorted(((_score(c, wanted, i), c) for i, c in enumerate(kept)), key=lambda pair: pair[0])
    return [c for _, c in scored]


# ---------------------------------------------------------------- running a process

@dataclass
class ProcessResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _kill(proc) -> None:
    """Kill the process and everything it started (yt-dlp may run ffmpeg), then reap it."""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        pass


def run_process(cmd, timeout: float, cancelled: Optional[Callable[[], bool]] = None, cwd=None,
                max_stdout: int = MAX_STDOUT, popen=subprocess.Popen) -> ProcessResult:
    """Run `cmd` (an argument list) to completion and return its output.

    `cancelled` is polled about ten times a second; when it returns True the process (and its children) is killed and
    YouTubeCancelled raised. After `timeout` seconds YouTubeTimeout. stdout is limited to `max_stdout` bytes (more
    kills the process: YouTubeError); only the last STDERR_KEEP bytes of stderr are kept. OSError (program not
    found) propagates.
    """
    if cancelled is not None and cancelled():
        raise YouTubeCancelled("Cancelled")
    extra = {"start_new_session": True} if os.name == "posix" else {}
    proc = popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                 cwd=None if cwd is None else str(cwd), **extra)
    out, err, overflow = bytearray(), bytearray(), threading.Event()

    def drain_stdout():
        while chunk := proc.stdout.read(65536):
            out.extend(chunk)
            if len(out) > max_stdout:
                overflow.set()
                return

    def drain_stderr():
        while chunk := proc.stderr.read(65536):
            err.extend(chunk)
            if len(err) > 2 * STDERR_KEEP:
                del err[:-STDERR_KEEP]

    threads = [threading.Thread(target=drain_stdout, daemon=True), threading.Thread(target=drain_stderr, daemon=True)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    try:
        while proc.poll() is None:
            if cancelled is not None and cancelled():
                raise YouTubeCancelled("Cancelled")
            if overflow.is_set():
                raise YouTubeError("YouTube answered with far too much data, so the search was stopped")
            if time.monotonic() >= deadline:
                raise YouTubeTimeout("The YouTube downloader did not finish in time and was stopped")
            time.sleep(POLL_SECONDS)
        for thread in threads:
            thread.join(timeout=5)
        if overflow.is_set():
            raise YouTubeError("YouTube answered with far too much data, so the search was stopped")
    except BaseException:
        _kill(proc)
        raise
    return ProcessResult(proc.returncode, out.decode("utf-8", "replace"),
                         bytes(err[-STDERR_KEEP:]).decode("utf-8", "replace"))


def explain_failure(returncode: int, stderr: str) -> str:
    """Plain words for a failed yt-dlp run. Known causes get advice; the rest shows one short cleaned line."""
    low = _ANSI.sub("", stderr or "").lower()
    update = ("YouTube refused or changed something this version of the downloader cannot handle. "
              f"Update the downloader and try again. {INSTALL_HINT}.")
    if "unsupported version of python" in low:
        return "The YouTube downloader needs a newer Python (3.10 or later) than this system has."
    if "http error 429" in low or "too many requests" in low:
        return "YouTube is limiting requests right now, try again in a few minutes."
    if any(k in low for k in ("video unavailable", "this video is unavailable", "private video", "this video is private",
                              "has been removed", "not available in your country", "members-only", "confirm your age",
                              "who has blocked it", "copyright")):
        return "That video is not available (it may be private, removed, age-restricted or blocked in this country)."
    if any(k in low for k in ("http error 403", "unable to extract", "requested format is not available",
                              "sign in to confirm", "nsig", "please report this issue", "player response")):
        return update
    if any(k in low for k in ("unable to download webpage", "name resolution", "network is unreachable", "timed out",
                              "connection refused", "connection reset", "getaddrinfo", "urlopen error",
                              "remote end closed")):
        return "YouTube could not be reached (check the internet connection)."
    lines = [ln for ln in (_text(raw) for raw in (stderr or "").splitlines()) if ln]
    errors = [ln for ln in lines if ln.upper().startswith("ERROR")]
    last = (errors or lines or [""])[-1]
    last = re.sub(r"(?i)^error:\s*", "", last)
    detail = _shorten(last, MESSAGE_TAIL) if last else f"(exit code {returncode})"
    return f"The YouTube downloader failed: {detail}"


# ---------------------------------------------------------------- yt-dlp

def _clean_query(query) -> str:
    return _text(query)[:MAX_QUERY].strip()


def _find_download(workdir: Path) -> Optional[Path]:
    """The finished video yt-dlp left in `workdir`: a real file (no symlink), not a .part, non-empty."""
    best = None
    for entry in os.scandir(workdir):
        suffix = os.path.splitext(entry.name)[1].lower()
        if entry.name.startswith("video.") and suffix in OUTPUT_EXTENSIONS and entry.is_file(follow_symlinks=False):
            size = entry.stat(follow_symlinks=False).st_size
            if size > 0 and (best is None or size > best[0]):
                best = (size, Path(entry.path), suffix)
    return best[1] if best else None


class YtDlp:
    """The yt-dlp zipapp in `bin_dir`: install/update it, search YouTube, download a video.

    `python` is the interpreter that runs it (default: the one running this program); `runner` replaces
    run_process in tests.
    """

    def __init__(self, bin_dir, python: Optional[str] = None, runner: Callable = run_process):
        self.path = Path(bin_dir) / "yt-dlp"
        self.python = python or sys.executable or "python3"
        self.runner = runner

    # ---- installing

    def available(self) -> bool:
        return self.path.is_file()

    def ensure(self, opener=urllib.request.urlopen) -> Path:
        """Install yt-dlp if it is not there yet."""
        return self.path if self.available() else self._install(opener).path

    def update(self, opener=urllib.request.urlopen, progress=None, cancelled=None, stage=None) -> Path:
        """Fetch the latest yt-dlp. The old copy is replaced only after the new one checked out. See ``install``."""
        return self.install(opener, progress, cancelled, stage).path

    def install(self, opener=urllib.request.urlopen, progress=None, cancelled=None, stage=None) -> InstallResult:
        """Install or update yt-dlp and say what happened.

        `progress(done, total)` is called while the file downloads (bytes so far; `total` is the size the server announced,
        or None when it did not say). `stage(text)` announces the quiet steps after the download. `cancelled()` is polled
        between reads and before each step; a cancel raises YouTubeCancelled and leaves nothing behind (no temp file, the
        old copy untouched). Everything else is the same as ``update``: size cap, GitHub-only origin, zipapp check, smoke run.
        """
        return self._install(opener, progress, cancelled, stage)

    def _install(self, opener, progress=None, cancelled=None, stage=None) -> InstallResult:
        """Download to a temp file, check it, smoke-run it, and only then replace the installed copy."""
        folder = self.path.parent
        had_one = self.available()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            fd, part = tempfile.mkstemp(prefix=".yt-dlp-", suffix=".part", dir=folder)
        except OSError as exc:
            raise self._cannot_write(exc) from exc

        def step(text: str) -> None:
            if cancelled is not None and cancelled():
                raise YouTubeCancelled("Cancelled")
            if stage is not None:
                stage(text)

        try:
            step("Connecting to GitHub…")
            with os.fdopen(fd, "wb") as out:
                self._fetch_into(out, opener, progress, cancelled)
                try:
                    out.flush()
                    os.fsync(out.fileno())
                except OSError as exc:
                    raise self._cannot_write(exc) from exc
            step("Checking the downloaded file…")
            self._check_zipapp(part)
            step("Testing that the new downloader starts…")
            version = self._smoke_run(part, cancelled)
            previous = ""
            if had_one:
                step("Comparing with the installed version…")
                previous = self._installed_version(cancelled)
            step("Installing…")
            try:
                os.chmod(part, 0o755)
                os.replace(part, self.path)
            except OSError as exc:
                raise self._cannot_write(exc) from exc
        finally:
            Path(part).unlink(missing_ok=True)
        return InstallResult(self.path, version, previous, had_one)

    def _installed_version(self, cancelled=None) -> str:
        """The version the installed copy reports, or '' (a copy that cannot tell is simply replaced)."""
        try:
            result = self.runner([self.python, "-I", str(self.path), "--version"], timeout=SMOKE_TIMEOUT, cancelled=cancelled, cwd=None)
        except YouTubeCancelled:
            raise
        except (YouTubeError, OSError):
            return ""
        return _version_text(result.stdout) if result.returncode == 0 else ""

    def _fetch_into(self, out, opener, progress=None, cancelled=None) -> None:
        """Stream the release file into `out`. Network trouble and local write trouble get different messages."""
        try:
            with opener(urllib.request.Request(YTDLP_URL, headers={"User-Agent": "Mozilla/5.0"}),
                        timeout=BINARY_TIMEOUT) as response:
                self._check_origin(getattr(response, "geturl", None))
                announced = _content_length(response)
                if announced is not None and announced > MAX_BINARY_BYTES:
                    raise YouTubeError("Could not get the YouTube downloader: the file is too large to be right")
                total = 0
                if progress is not None:
                    progress(0, announced)
                while True:
                    if cancelled is not None and cancelled():
                        raise YouTubeCancelled("Cancelled")
                    try:
                        chunk = response.read(65536)
                    except (OSError, http.client.HTTPException) as exc:
                        raise self._offline(exc) from exc
                    if not chunk:
                        if announced is not None and total != announced:
                            raise self._offline(None)             # the connection ended early
                        return
                    total += len(chunk)
                    if total > MAX_BINARY_BYTES:
                        raise YouTubeError("Could not get the YouTube downloader: the file is too large to be right")
                    try:
                        out.write(chunk)
                    except OSError as exc:
                        raise self._cannot_write(exc) from exc
                    if progress is not None:
                        progress(total, announced)
        except YouTubeError:
            raise
        except (OSError, http.client.HTTPException) as exc:        # includes URLError / HTTPError
            raise self._offline(exc) from exc

    @staticmethod
    def _offline(exc) -> YouTubeError:
        return YouTubeError("Could not get the YouTube downloader (check the internet connection)")

    def _cannot_write(self, exc) -> YouTubeError:
        why = _shorten(_text(getattr(exc, "strerror", None)) or type(exc).__name__, 80)
        return YouTubeError(f"Could not write the YouTube downloader to {self.path.parent}: {why}")

    def _smoke_run(self, candidate: str, cancelled=None) -> str:
        """Run `python -I <candidate> --version`: a download that cannot start must never replace a working copy.
        Returns the version it printed."""
        keep = "The new YouTube downloader would not start, so the old one was kept." if self.available() \
            else "The new YouTube downloader would not start, so nothing was installed."
        try:
            result = self.runner([self.python, "-I", candidate, "--version"], timeout=SMOKE_TIMEOUT, cancelled=cancelled, cwd=None)
        except YouTubeCancelled:
            raise
        except YouTubeError as exc:
            raise YouTubeError(f"{keep} ({exc})") from exc
        except OSError as exc:
            raise YouTubeError(f"{keep} Python could not be started ({type(exc).__name__}).") from exc
        if result.returncode != 0 or not result.stdout.strip():
            raise YouTubeError(f"{keep} {explain_failure(result.returncode, result.stderr)}")
        return _version_text(result.stdout)

    @staticmethod
    def _check_origin(geturl) -> None:
        """The address the download really came from (after redirects) must be GitHub over https; unknown is refused."""
        final_url = geturl() if callable(geturl) else ""
        parts = urlsplit(final_url) if isinstance(final_url, str) else urlsplit("")
        host = parts.hostname or ""
        if parts.scheme.lower() != "https" or not (host == "github.com" or host.endswith(_GITHUB_SUFFIXES)):
            raise YouTubeError("Could not get the YouTube downloader: it was offered from an unexpected address")

    @staticmethod
    def _check_zipapp(path: str) -> None:
        bad = YouTubeError("The file that came back is not the YouTube downloader, nothing was installed")
        with open(path, "rb") as f:
            if f.read(2) != b"#!":
                raise bad
        try:
            if not zipfile.is_zipfile(path):
                raise bad
            with zipfile.ZipFile(path) as archive:
                if "__main__.py" not in archive.namelist():
                    raise bad
        except (zipfile.BadZipFile, OSError) as exc:
            raise bad from exc

    # ---- running

    def _run(self, args: list, timeout: float, cancelled=None, cwd=None) -> ProcessResult:
        if not self.available():
            raise YouTubeError(f"The YouTube downloader is not installed yet. {INSTALL_HINT}.")
        cmd = [self.python, "-I", str(self.path), *args]
        try:
            return self.runner(cmd, timeout=timeout, cancelled=cancelled, cwd=cwd)
        except YouTubeError:
            raise
        except OSError as exc:
            raise YouTubeError(f"Python could not start the YouTube downloader ({type(exc).__name__}). "
                               f"{INSTALL_HINT} or check that Python 3.10+ is installed.") from exc

    def search(self, query: str, count: int = 6, title: str = "", cancelled=None) -> list:
        """Up to `count` ranked video candidates for `query` (see rank_candidates; `title` = the bare game name)."""
        query = _clean_query(query)
        if not query:
            return []
        count = max(1, min(int(count), MAX_COUNT))
        fetch = min(2 * count, MAX_SEARCH_FETCH)
        result = self._run(["--ignore-config", "--no-plugin-dirs", "--flat-playlist", "--dump-json", "--no-warnings", "--no-cache-dir",
                            "--color", "never", "--socket-timeout", "20", "--", f"ytsearch{fetch}:{query}"],
                           SEARCH_TIMEOUT, cancelled)
        found = parse_search_output(result.stdout)
        if result.returncode != 0 and not found:
            raise YouTubeError(explain_failure(result.returncode, result.stderr))
        return rank_candidates(found, title)[:count]

    def download(self, url: str, dest_dir, max_height: int = MAX_HEIGHT, cancelled=None,
                 timeout: float = DOWNLOAD_TIMEOUT) -> Path:
        """Download one video (at most `max_height` p, mp4 or webm, no playlist) into `dest_dir` as <id>.<ext>.

        yt-dlp works inside a private sub-folder of `dest_dir` (its normal .part files make the download itself
        atomic); only a finished, non-empty file is moved to its final name, and the sub-folder is always removed,
        also on failure, timeout or cancellation. No cookies, no config files, no cache.
        """
        if isinstance(max_height, bool) or not isinstance(max_height, int) or not MIN_HEIGHT <= max_height <= MAX_HEIGHT:
            raise ValueError(f"max_height must be a whole number from {MIN_HEIGHT} to {MAX_HEIGHT}")
        video_id = video_id_from_url(url)
        if not video_id:
            raise YouTubeError("That is not a YouTube video link.")
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        h = max_height
        # mp4 first, then webm. AV1 and VP9 are allowed on purpose: the Batocera ffmpeg decodes av1 (libdav1d), vp9 and
        # h264, and the encoder re-encodes everything to H.264. Asking for H.264 only was tried and fails: YouTube
        # answers HTTP 403 for its 720p H.264 stream while the AV1 mp4 downloads fine.
        fmt = (f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/b[height<={h}][ext=mp4]/"
               f"bv*[height<={h}][ext=webm]+ba[ext=webm]/b[height<={h}][ext=webm]")
        workdir = Path(tempfile.mkdtemp(prefix=".yt-", dir=dest))
        try:
            result = self._run(["--ignore-config", "--no-plugin-dirs", "--no-playlist", "--quiet", "--no-warnings", "--no-progress",
                                "--color", "never", "--no-cache-dir", "--no-mtime", "--socket-timeout", "30",
                                "--retries", "3", "--fragment-retries", "3", "--max-filesize", MAX_DOWNLOAD_SIZE,
                                "-f", fmt, "-o", "video.%(ext)s", "--", _watch_url(video_id)],
                               timeout, cancelled, cwd=workdir)
            if result.returncode != 0:
                raise YouTubeError(explain_failure(result.returncode, result.stderr))
            made = _find_download(workdir)
            if made is None:
                raise YouTubeError(f"YouTube gave no video file (it may be over {MAX_DOWNLOAD_SIZE.rstrip('M')} MB or "
                                   f"have no {h}p-or-smaller version). Try another video.")
            final = dest / f"{video_id}{made.suffix.lower()}"
            try:
                os.replace(made, final)
            except OSError as exc:
                raise YouTubeError(f"Could not save the downloaded video to {dest}: "
                                   f"{_shorten(_text(exc.strerror) or type(exc).__name__, 80)}") from exc
            return final
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
