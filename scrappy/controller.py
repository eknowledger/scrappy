"""Application logic with no GUI code, so it can be tested and called from worker threads.

Threading: the window calls these methods from Qt worker threads. Everything a Controller sets in ``__init__`` (settings,
providers, folders) never changes afterwards; what does change lives behind a lock or on disk written atomically: the
Saver serialises saves and undo, the picture cache is filled with random temp files + rename (trimming it is locked),
the set of files the user chose is locked, every video job works in its own private folder, and each search builds its
own result objects. Methods that can take a while accept ``cancelled`` (a no-argument callable returning True to stop)
and pass it on to the downloader, yt-dlp and ffmpeg. ``list_systems`` and ``rows`` read the ROM drive (often NTFS or a
network share: slow, can hang), so the window must call them from a worker, never from the GUI thread.

Errors the caller can fix (a bad address, a file that is not a file, ...) are ``ControllerError``: a ``ProviderError``
(so ``describe_error`` shows the message as a plain sentence) and still a ``ValueError`` for older callers.

Safety rules kept here:
* A local file is read only if this Controller issued it: ``local_candidate`` (called with the path the user picked in a
  dialog) records its address, and ``fetch_bytes`` / ``make_video`` read a ``file://`` address only if it was recorded
  (and, for pictures, ``local_ok`` is set). A ``file://`` address that came from a web source, or a candidate that merely
  claims ``source="your file"``, is refused. Everything else goes through ``scrappy.download`` (public hosts only, size and
  time limits).
* ffmpeg only gets an http(s) address (HLS trailers) whose host is a Steam asset host or resolves to public addresses
  only (ffmpeg looks the name up again itself, so this is a guard against honest mistakes and simple tricks, not a
  complete defence), a file this program downloaded itself, or a file the user chose; never a path or scheme from web data.
* ``build_request`` lets through only the text fields the Saver may write, only real JPEG/PNG bytes, and only
  replace-tags the Saver knows. The Saver's rule stays in force: art that exists is kept unless its tag is in
  ``replace``.
* Only files this program makes are ever deleted from its work folders (see ``_prune``), and never through a symlink.
"""
from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, fields, replace as dc_replace
from pathlib import Path
from urllib.parse import urlsplit

from .atomic import atomic_replace
from PIL import Image

from .config import DEFAULTS, data_dir
from .download import MAX_IMAGE_BYTES, cache_path, check_destination, fetch_cached, system_resolver
from .gamelist import Gamelist, _norm
from .media import media_abs_paths
from .models import SLOT_TAGS, SLOTS, AssetCandidate, CurrentAsset
from .processing.images import open_image
from .processing.video import encode_clip, probe_duration
from .providers.base import ProviderError, describe_error
from .providers.steam import ASSET_HOST_SUFFIXES, SteamProvider
from .providers.youtube import INSTALL_HINT, YtDlp, youtube_query
from .roms import _has_content, asset_status, rom_extensions, scan_roms
from .saver import MEDIA_TAGS, TEXT_FIELDS, SaveError, SaveReport, SaveRequest, Saver, _inside, emulationstation_running
from .search import search_matches
from .titles import clean_title

DEFAULT_EXTS = {".nsp", ".xci", ".zip", ".7z", ".iso", ".chd"}
STATUS_KEYS = ("desc", "image", "thumbnail", "marquee", "video")
ES_RELOAD_URL = "http://127.0.0.1:1234/reloadgames"
ES_BY_HAND = "Start > Game Settings > Update Gamelists"
TMP_MAX_AGE = 24 * 3600              # leftovers older than this are removed when the program starts
CACHE_MAX_BYTES = 200_000_000        # the picture cache is trimmed oldest-first to about this size
MAX_LOCAL_VIDEO_BYTES = 4_000_000_000
TRUSTED_VIDEO_HOST_SUFFIXES = tuple(ASSET_HOST_SUFFIXES) + (".youtube.com", ".googlevideo.com")      # no DNS check needed
# the only names _prune may delete in the temporary folder: our clips (and their encoder temp files), scrappy.atomic temp
# files, and the private download folders
_TMP_FILES = re.compile(r"clip-[0-9a-f]{12}\.mp4(?:\.[0-9a-f]{8}\.tmp)?|\.scrappy-[0-9a-f]{16}\.tmp")
_TMP_DIR_PREFIXES = (".yt-", "dl-")
# in the cache folder: cached pictures (never pruned by age, only trimmed by size) and unfinished downloads / temp files
_CACHE_PICTURE = re.compile(r"[0-9a-f]{40}(?:\.[a-z0-9]{1,5})?")
_CACHE_LEFTOVERS = re.compile(r"[0-9a-f]{40}(?:\.[a-z0-9]{1,5})?\.[A-Za-z0-9_]+\.part|\.scrappy-[0-9a-f]{16}\.tmp")
KINDS = ("file", "hls", "youtube")
JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_NOT_A_PICTURE = "That address gave something that is not a picture (a web page or an error message?), so it was not used."
MEDIA_NAMES = {"image": "image", "thumbnail": "box art", "marquee": "logo", "video": "video"}
PICTURE_LABELS = {"image_jpg": "image", "thumb_png": "box art", "marquee_png": "logo"}
CURRENT_PREVIEW_SIDE = 640           # the picture a game has now is shown from a PNG shrunk to this many pixels
CURRENT_PROBE_SECONDS = 8.0          # how long ffprobe may take for the length of the video a game has now


class ControllerError(ProviderError, ValueError):
    """Something the caller asked for cannot be done; the message is a plain sentence (also a ValueError, for old callers)."""


def looks_like_picture(data) -> bool:
    """A cheap check of the first bytes: JPEG, PNG, GIF, WebP, BMP or TIFF. Not a validation (open_image does that); it
    keeps web pages and error messages out of the picture cache."""
    if not isinstance(data, (bytes, bytearray)):
        return False
    head = bytes(data[:12])
    return (head.startswith((JPEG_MAGIC, PNG_MAGIC, b"GIF87a", b"GIF89a", b"BM", b"II*\x00", b"MM\x00*"))
            or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"))


@dataclass
class RomRow:
    rom_path: str
    title: str
    status: dict
    problem: str = ""                # why the status could not be worked out (e.g. a damaged gamelist.xml)

    @property
    def complete(self) -> bool:
        return bool(self.status.get("complete"))

    @property
    def missing(self) -> list:
        return [k for k in STATUS_KEYS if not self.status.get(k)]


@dataclass
class SearchResults:
    matches: list = field(default_factory=list)
    videos: list = field(default_factory=list)
    errors: dict = field(default_factory=dict)


@dataclass
class CurrentAssets:
    """What a game has now (``Controller.load_current``): the seven text fields as in gamelist.xml ("" = nothing), and per
    slot a ``CurrentAsset`` or None when the game has no such file."""
    fields: dict
    pictures: dict = field(default_factory=dict)       # "logo" | "boxart" | "image" -> CurrentAsset or None
    video: object = None


@dataclass
class SaveOutcome(SaveReport):
    """A SaveReport plus what happened with a running EmulationStation (see ``Controller.save``)."""
    es_reloaded: bool = False        # EmulationStation was asked to reload its game lists and answered
    es_reapplied: bool = False       # it overwrote our edit once and the edit was applied again
    es_warning: str = ""             # non-empty: the edit may be missing from the list; a plain sentence for the user


ES_FLUSH_WAIT = 2.0                  # seconds EmulationStation gets to write its unsaved changes before we edit the list
ES_RELOAD_WAIT = 3.0                 # seconds it gets to read our edit before we check that the list on disk still has it
ES_OVERWROTE_WARNING = ("EmulationStation overwrote the game list again, so the new details may be missing from it. "
                        "Quit EmulationStation completely, then press Save again; the picture and video files "
                        "are already in place.")
ES_UNDO_WARNING = ("EmulationStation wrote its old game list back again, so the undone details may reappear. "
                   "Quit EmulationStation completely and check the list; the files are already removed.")


class _GamelistIndex:
    """gamelist.xml entries by normalised path: one pass instead of a scan per ROM. The first entry for a path wins,
    exactly like ``Gamelist.get``. Has the same ``get`` so ``roms.asset_status`` accepts it."""

    def __init__(self, gamelist=None):
        self._by_path = {}
        for game in (gamelist.games() if gamelist is not None else ()):
            self._by_path.setdefault(_norm(game.findtext("path") or ""), {c.tag: (c.text or "") for c in game if c.tag != "scrap"})

    def get(self, rom_path: str) -> dict:
        return self._by_path.get(_norm(rom_path), {})


def changed_fields(fields, current) -> dict:
    """The text fields that differ from what the game has now: stripped, blanks dropped (a blank never clears a field),
    values equal to the current one dropped (nothing new to write). Non-text values are left to ``build_request``."""
    out = {}
    for tag, value in dict(fields).items():
        if isinstance(value, str) and value.strip() and value.strip() != (current.get(tag) or "").strip():
            out[tag] = value.strip()
    return out


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _clip_time(value, name: str, allow_none: bool):
    if value is None:
        return None if allow_none else 0.0
    try:
        number = float(value) if not isinstance(value, bool) else float("nan")
    except (TypeError, ValueError):
        number = float("nan")
    if number != number or number in (float("inf"), float("-inf")):
        raise ControllerError(f"The video {name} time must be a number of seconds.")
    return number


def _require_http_url(url):
    """The parsed address if ffmpeg may open it: plain http(s) with a host, no credentials, no whitespace or control
    characters."""
    if isinstance(url, str) and url and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        try:
            parts = urlsplit(url)
            if parts.scheme.lower() in ("http", "https") and parts.hostname and not parts.username and not parts.password:
                parts.port                                       # noqa: B018 - raises ValueError for a bad port
                return parts
        except ValueError:
            pass
    raise ControllerError("That video address is not a plain http(s) link, so it was not used.")


class Controller:
    def __init__(self, cfg, providers=None, ytdlp=None, encode=encode_clip, fetch=None, es_configs=None,
                 resolver=None, cache_max_bytes=CACHE_MAX_BYTES, es_running=None, es_reload=None, sleep=time.sleep,
                 es_waits=(ES_FLUSH_WAIT, ES_RELOAD_WAIT), probe=None):
        """`providers`: game sources (default Steam). `ytdlp`: the YtDlp wrapper. `encode`: replaces the ffmpeg encoder
        in tests (same signature as ``encode_clip``). `fetch`: replaces the safe downloader in tests, ``fetch(url) -> bytes``.
        `es_configs`: es_systems.cfg paths (default: the Batocera ones). `resolver(host, port) -> [addresses]` replaces
        DNS for the public-address check of video hosts (tests). `cache_max_bytes`: size the picture cache is trimmed to.
        `es_running() -> bool`, `es_reload() -> bool` and `sleep(seconds)` replace the /proc check, the call to
        EmulationStation's reload address and the waiting (tests); `es_waits` = (flush wait, reload wait) in seconds.
        `probe(path) -> seconds` replaces ffprobe for the length of the video a game has now (0 = unknown)."""
        self.cfg = cfg
        self.roms_root = Path(self._setting("roms_root"))
        self.providers = list(providers) if providers is not None else [SteamProvider()]
        self.encode = encode
        self._fetch = fetch                      # None means the real, safe downloader (scrappy.download)
        self._resolver = resolver
        self.es_configs = es_configs
        self.cache_max_bytes = cache_max_bytes
        self._es_running = es_running if es_running is not None else emulationstation_running
        self._es_reload = es_reload                   # None: the real call (reload_es)
        self._sleep = sleep
        self._es_waits = tuple(es_waits)
        self._probe = probe if probe is not None else (lambda path: probe_duration(path, timeout=CURRENT_PROBE_SECONDS))
        self._extra_entries = 0                       # journal entries on top of the last save (a re-apply), undone with it
        self._local_urls: set = set()            # file:// addresses issued by local_candidate (the only local files we read)
        self._local_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self.backup_dir = data_dir(cfg, "backups")
        self.journal_dir = data_dir(cfg, "journal")
        self.tmp_dir = data_dir(cfg, "tmp")
        self.cache_dir = data_dir(cfg, "cache")
        self.ytdlp = ytdlp if ytdlp is not None else YtDlp(data_dir(cfg, "bin"))
        # one Saver per application: it holds the backup session and the lock that serialises saves and undo
        self.saver = Saver(self.backup_dir, self.journal_dir, dry_run_dir=cfg.get("dry_run_dir"), es_running=self._es_running)
        self._prune(self.tmp_dir, _TMP_FILES, _TMP_DIR_PREFIXES)
        self._prune(self.cache_dir, _CACHE_LEFTOVERS, ())

    def _setting(self, key):
        return self.cfg.get(key, DEFAULTS[key])

    def _prune(self, folder: Path, file_names, dir_prefixes, max_age: float = TMP_MAX_AGE) -> None:
        """Remove old leftovers from a work folder. Only names this program creates are ever touched: file names that fully
        match `file_names`, and folders whose names start with one of `dir_prefixes`. Nothing is done at all when the folder
        or the data folder is a symlink, symbolic links inside are never followed (a matching one is unlinked, its target
        is left alone), and every other name (a user's own file dropped there) is ignored."""
        if os.path.islink(folder) or os.path.islink(self._setting("data_dir")):
            return
        cutoff = time.time() - max_age
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return
        for e in entries:
            try:
                if e.stat(follow_symlinks=False).st_mtime >= cutoff:
                    continue
                if e.is_symlink():
                    if file_names.fullmatch(e.name) or e.name.startswith(dir_prefixes):
                        os.unlink(e.path)
                elif e.is_dir(follow_symlinks=False):
                    if dir_prefixes and e.name.startswith(dir_prefixes):
                        shutil.rmtree(e.path, ignore_errors=True)
                elif file_names.fullmatch(e.name):
                    os.unlink(e.path)
            except OSError:
                pass

    def _evict_cache(self) -> None:
        """Trim the picture cache to `cache_max_bytes`, oldest first (a cache hit refreshes a file's age). Only files named
        like cache entries count and only they are removed."""
        with self._cache_lock:
            files, total = [], 0
            try:
                for e in os.scandir(self.cache_dir):
                    if _CACHE_PICTURE.fullmatch(e.name) and e.is_file(follow_symlinks=False):
                        st = e.stat(follow_symlinks=False)
                        files.append((st.st_mtime, e.path, st.st_size))
                        total += st.st_size
            except OSError:
                return
            for _, path, size in sorted(files):
                if total <= self.cache_max_bytes:
                    break
                try:
                    os.unlink(path)
                    total -= size
                except OSError:
                    pass

    # ----- library
    def system_dir(self, system: str) -> Path:
        if (not isinstance(system, str) or system in ("", ".", "..") or "/" in system or "\\" in system
                or "\x00" in system):
            raise ControllerError(f"{system!r} is not a system folder name.")
        return self.roms_root / system

    def list_systems(self) -> list:
        """Names of the system folders under the ROM folder. Reads the ROM drive (NTFS / network share): call from a worker."""
        try:
            folders = sorted(self.roms_root.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return []
        names = []
        for d in folders:
            if d.name.startswith(".") or not _is_dir(d):
                continue
            if _exists(d / "gamelist.xml") or rom_extensions(d.name, self.es_configs):
                names.append(d.name)
        return names

    def rows(self, system: str, only_missing: bool = False) -> list:
        """The games of one system with what each already has. Incomplete games first, the newest ROM files first among
        them, then by title. Scans the ROM drive and reads gamelist.xml (NTFS / network share): call from a worker."""
        sdir = self.system_dir(system)
        exts = rom_extensions(system, self.es_configs) or DEFAULT_EXTS
        problem = ""
        try:
            index = _GamelistIndex(Gamelist(sdir / "gamelist.xml"))
        except (ET.ParseError, OSError, ValueError) as exc:
            index = _GamelistIndex()
            problem = (f"gamelist.xml in {system} could not be read ({exc}), so what each game already has is unknown. "
                       "Nothing will be saved until it is repaired (older copies are in the backups folder).")
        keyed = []
        for rom in scan_roms(sdir, exts):
            st = asset_status(sdir, index, rom)
            try:
                newest = os.stat(sdir / rom).st_mtime
            except OSError:
                newest = 0.0
            keyed.append((newest, RomRow(rom, index.get(rom).get("name", "").strip() or clean_title(rom), st, problem)))
        keyed.sort(key=lambda item: (item[1].complete, -item[0], item[1].title.lower()))
        out = [row for _, row in keyed]
        return [r for r in out if not r.complete] if only_missing else out

    # ----- what a game has now
    def load_current(self, system: str, rom_path: str, cancelled=None) -> CurrentAssets:
        """Read what the game has now: its text fields and, per slot, the file its gamelist tag points to (else the program's
        standard file name). Reads the ROM drive and decodes pictures: call from a worker. Never writes anything.

        A picture that cannot be read as a picture is still returned (``preview`` empty, ``problem`` says why): the file is
        there, and saving a new one over it needs "replace". The video is only measured (``ffprobe`` if it answers in time)."""
        sdir = self.system_dir(system)
        try:
            got = Gamelist(sdir / "gamelist.xml").get(rom_path)
        except (ET.ParseError, OSError, ValueError) as exc:
            raise ControllerError(f"gamelist.xml in {system} could not be read ({exc}), so what this game has now is unknown. "
                                  "Nothing will be saved until it is repaired (older copies are in the backups folder).") from exc
        standard = media_abs_paths(str(sdir), rom_path)
        out = CurrentAssets(fields={tag: (got.get(tag) or "").strip() for tag in sorted(TEXT_FIELDS)},
                            pictures={slot: None for slot in ("logo", "boxart", "image")})
        for slot in ("logo", "boxart", "image", "video"):
            if cancelled is not None and cancelled():
                raise InterruptedError("cancelled")
            tag = SLOT_TAGS[slot]
            found = self._current_file(sdir, got.get(tag), standard[tag])
            if found is None:
                continue
            make = self._current_video if slot == "video" else self._current_picture
            asset = make(slot, *found, standard[tag])
            if slot == "video":
                out.video = asset
            else:
                out.pictures[slot] = asset
        return out

    @staticmethod
    def _current_file(sdir: Path, referenced, standard: str):
        """(path, standard file exists, gamelist points at it) of the file a slot has now: the one the tag points to if it is a
        real file inside the system folder (same rule as the Saver: no absolute or ``..`` ways out, links included), else the
        standard file; None when there is neither."""
        root = os.path.realpath(sdir)
        standard_ok = _has_content(standard) and _inside(standard, root)
        ref = (referenced or "").strip()
        if ref:
            path = os.path.join(str(sdir), ref[2:] if ref.startswith("./") else ref)
            if not os.path.isabs(ref) and _has_content(path) and _inside(path, root):
                return path, standard_ok, True
        return (standard, True, False) if standard_ok else None

    @staticmethod
    def _current_candidate(slot: str, **kw) -> AssetCandidate:
        return AssetCandidate(slot=slot, url=f"current://{slot}", label="Current", source="current", current=True, **kw)

    def _current_picture(self, slot: str, path: str, standard_exists: bool, linked: bool, standard: str) -> CurrentAsset:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        preview, problem, width, height = b"", "", 0, 0
        try:
            im = open_image(self._read_local(path))
            width, height = im.size
            im.thumbnail((CURRENT_PREVIEW_SIDE, CURRENT_PREVIEW_SIDE), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "PNG")
            preview = buf.getvalue()
        except Exception as exc:                                  # noqa: BLE001 - whatever is wrong with the file, it stays "there"
            problem = (str(exc) if isinstance(exc, ControllerError)
                       else f"This file could not be read as a picture ({describe_error(exc)}).")
        return CurrentAsset(self._current_candidate(slot, width=width, height=height), path, standard, standard_exists, size,
                            preview, problem, tag_linked=linked)

    def _current_video(self, slot: str, path: str, standard_exists: bool, linked: bool, standard: str) -> CurrentAsset:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        try:
            duration = float(self._probe(path))
        except Exception:                                        # noqa: BLE001 - no ffprobe, odd file: the length is just unknown
            duration = 0.0
        if not duration > 0 or duration == float("inf"):
            duration = 0.0
        return CurrentAsset(self._current_candidate(slot, duration=duration), path, standard, standard_exists, size,
                            duration=duration, tag_linked=linked)

    # ----- online lookups
    def _search_videos(self, title: str, system: str, cancelled, deadline: float):
        if not self.ytdlp.available():
            return [], f"The YouTube downloader is not installed yet. {INSTALL_HINT}."
        try:
            return self.ytdlp.search(youtube_query(title, system), title=title, cancelled=cancelled), None
        except Exception as exc:                                   # noqa: BLE001 - one source must never break the others
            if time.monotonic() >= deadline:
                return [], "did not answer in time"
            return [], describe_error(exc)

    def search(self, title: str, system: str = "", cancelled=None) -> SearchResults:
        """Matches from every game source plus YouTube trailers. Problems are reported per source in ``errors``."""
        if not isinstance(title, str) or not title.strip():
            return SearchResults()
        title = title.strip()
        deadline = time.monotonic() + self._setting("youtube_search_timeout")

        def yt_cancelled():
            return time.monotonic() >= deadline or (cancelled is not None and cancelled())

        with ThreadPoolExecutor(max_workers=1) as pool:
            yt_future = pool.submit(self._search_videos, title, system, yt_cancelled, deadline)
            outcome = search_matches(self.providers, title, timeout=self._setting("search_timeout"), cancelled=cancelled)
            videos, yt_error = yt_future.result()
        errors = dict(outcome.errors)
        if yt_error:
            errors["YouTube"] = yt_error
        return SearchResults(list(outcome.matches), list(videos), errors)

    def load_info(self, match):
        for p in self.providers:
            if p.name == match.source:
                return p.fetch(match)
        raise ProviderError(f"No source named {match.source!r} is set up.")

    def fetch_bytes(self, url: str, local_ok: bool = False, cancelled=None) -> bytes:
        """Bytes of a picture.

        ``file://`` is read only when `local_ok` is true AND the address was issued by ``local_candidate`` (the user picked
        that file in a dialog); any other file address, whatever its source claims, is refused. The path is taken as typed
        after ``file://`` (not percent-decoded), must be an absolute path to an ordinary file of at most 25 MB, and is
        checked again when read. Everything else goes through ``scrappy.download`` (public hosts only, size and time limits);
        `local_ok` gives no extra rights for web addresses. Web pictures are cached, but only if they start like a
        picture (an HTML error page is not kept); the cache is trimmed to ``cache_max_bytes``.
        """
        if not isinstance(url, str):
            raise ControllerError("The picture address must be text.")
        url = url.strip()
        if urlsplit(url).scheme.lower() == "current":
            raise ControllerError("That is the picture the game has now. It is shown for comparison and is never fetched again.")
        if urlsplit(url).scheme.lower() == "file":
            if not local_ok or not self._is_issued(url):
                raise ControllerError("Use Choose file to pick a file from this computer.")
            return self._read_local(url[7:])
        if self._fetch is None:
            path = fetch_cached(self.cache_dir, url, cancelled=cancelled)
            data = path.read_bytes()
            if not looks_like_picture(data):
                path.unlink(missing_ok=True)
                raise ControllerError(_NOT_A_PICTURE)
            self._touch(path)
        else:
            data = self._cached_fetch(url)
        self._evict_cache()
        return data

    def local_candidate(self, slot: str, path: str, label=None) -> AssetCandidate:
        """The candidate for a file the user chose in a dialog (``source="your file"``). The file must be an absolute path
        to an ordinary, non-empty file (pictures up to 25 MB, videos up to 4 GB). Its ``file://`` address is recorded here;
        only recorded addresses are ever read by ``fetch_bytes(..., local_ok=True)`` and ``make_video``."""
        if slot not in SLOTS:
            raise ControllerError(f"{slot!r} is not a slot (use one of {', '.join(SLOTS)}).")
        video = slot == "video"
        os.close(self._open_local(path, MAX_LOCAL_VIDEO_BYTES if video else None, "video file" if video else "picture file")[0])
        url = "file://" + path
        with self._local_lock:
            self._local_urls.add(url)
        return AssetCandidate(slot=slot, url=url, label=str(label) if label else os.path.basename(path.rstrip("/")),
                              source="your file", kind="file")

    def _is_issued(self, url: str) -> bool:
        with self._local_lock:
            return url in self._local_urls

    @staticmethod
    def _open_local(path, limit, what):
        """Open a user-chosen file for reading and check it: (fd, size). Never blocks on a pipe; closes the fd on a problem."""
        limit = MAX_IMAGE_BYTES if limit is None else limit
        label = f"Cannot read the {what} {path!r}"
        if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
            raise ControllerError(f"{label}: it is not a full path.")
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))   # a pipe must not block us
        except OSError as exc:
            raise ControllerError(f"{label}: {exc.strerror or exc}.") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ControllerError(f"{label}: it is not an ordinary file.")
            if info.st_size == 0:
                raise ControllerError(f"The {what} {path!r} is empty.")
            if info.st_size > limit:
                raise ControllerError(f"The {what} {path!r} is too big ({info.st_size // 1_000_000} MB, "
                                      f"the limit is {limit // 1_000_000} MB).")
        except BaseException:
            os.close(fd)
            raise
        return fd, info.st_size

    @staticmethod
    def _read_local(path: str) -> bytes:
        fd, _ = Controller._open_local(path, None, "picture file")
        try:
            with os.fdopen(fd, "rb") as f:
                fd = -1                                           # the file object owns it now
                data = f.read(MAX_IMAGE_BYTES + 1)
        except OSError as exc:
            raise ControllerError(f"Cannot read the picture file {path!r}: {exc.strerror or exc}.") from exc
        finally:
            if fd >= 0:
                os.close(fd)
        if len(data) > MAX_IMAGE_BYTES:                           # it grew after the size check
            raise ControllerError(f"The picture file {path!r} is too big (the limit is {MAX_IMAGE_BYTES // 1_000_000} MB).")
        if not data:
            raise ControllerError(f"The picture file {path!r} is empty.")
        return data

    @staticmethod
    def _touch(path) -> None:
        try:
            os.utime(path)                                        # a cache hit makes the file "new" for the trimming
        except OSError:
            pass

    def _cached_fetch(self, url: str) -> bytes:
        """The injected fetch function with the on-disk cache (tests and alternative downloaders)."""
        cp = cache_path(self.cache_dir, url)
        try:
            if cp.is_file() and cp.stat().st_size > 0:
                data = cp.read_bytes()
                if looks_like_picture(data):
                    self._touch(cp)
                    return data
                cp.unlink(missing_ok=True)
        except OSError:
            pass
        data = self._fetch(url)
        if not looks_like_picture(data):
            raise ControllerError(_NOT_A_PICTURE)
        atomic_replace(cp, lambda f: f.write(data))          # random temp name: two threads fetching one URL cannot clash
        return data

    # ----- video
    def _require_public_host(self, parts) -> None:
        """Steam / YouTube hosts are trusted by name; any other host must resolve to public addresses only."""
        host = parts.hostname.lower().rstrip(".")
        if host.endswith(TRUSTED_VIDEO_HOST_SUFFIXES):
            return
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
        try:
            ipaddress.ip_address(host)
            resolve = lambda h, p: [h]                              # noqa: E731 - a literal address needs no lookup
        except ValueError:
            resolve = self._resolver or system_resolver
        try:
            check_destination(host, port, resolver=resolve)
        except OSError as exc:                                      # the name could not be looked up
            raise ControllerError(f"Could not look up the address of {host} ({exc.strerror or exc}).") from exc

    def make_video(self, candidate, start: float = 0.0, end=None, cancelled=None) -> Path:
        """Cut and encode a trailer into the theme's format and return the clip (in the temporary folder).

        Start and end must be finite numbers of seconds; clamping to the real length, the minimum length and
        ``max_video_seconds`` is done by the encoder. Sources: a YouTube video (downloaded into a private folder that is
        removed afterwards); a file the user chose (``local_candidate``); or an http(s) address (Steam trailers are HLS
        streams) with no credentials whose host is a Steam/YouTube host or resolves to public addresses only.
        Anything else, including a ``file://`` address that ``local_candidate`` did not issue, is refused.
        """
        if getattr(candidate, "current", False):
            raise ControllerError("The Current video is already in place; it is shown for comparison and cannot be encoded again.")
        start, end = _clip_time(start, "start", False), _clip_time(end, "end", True)
        url, kind = candidate.url, candidate.kind
        if kind not in KINDS:
            raise ControllerError(f"Unknown kind of video: {kind!r}.")
        local_path = None
        if kind == "youtube":
            if not self.ytdlp.available():
                raise ControllerError(f"The YouTube downloader is not installed yet. {INSTALL_HINT}.")
        elif isinstance(url, str) and url.startswith(("file://", "/")):
            issued = url if url.startswith("file://") else "file://" + url
            if not self._is_issued(issued):
                raise ControllerError("That video file was not chosen by you, so it was not used. "
                                      "Use Choose file to pick a file from this computer.")
            local_path = issued[7:]
            os.close(self._open_local(local_path, MAX_LOCAL_VIDEO_BYTES, "video file")[0])
        else:
            self._require_public_host(_require_http_url(url))
        max_seconds, bitrate = self._setting("max_video_seconds"), self._setting("video_bitrate")
        name = hashlib.sha1(f"{url}|{start}|{end}|{max_seconds}|{bitrate}".encode()).hexdigest()[:12]
        dst = self.tmp_dir / f"clip-{name}.mp4"
        work = Path(tempfile.mkdtemp(prefix="dl-", dir=self.tmp_dir)) if kind == "youtube" else None
        try:
            src = str(self.ytdlp.download(url, work, cancelled=cancelled)) if work else (local_path or url)
            self.encode(src, str(dst), start=start, end=end, max_seconds=max_seconds, bitrate=bitrate, cancelled=cancelled)
        finally:
            if work:
                shutil.rmtree(work, ignore_errors=True)
        try:
            made = dst.stat().st_size > 0
        except OSError:
            made = False
        if not made:
            raise ProviderError("The video could not be made (the encoder produced no file).")
        return dst

    # ----- saving
    def build_request(self, system, rom_path, fields, image_jpg=None, thumb_png=None, marquee_png=None, video_mp4=None, replace=(),
                      current=None, link=()):
        """A SaveRequest for the Saver.

        `fields`: text tags the Saver may write (``saver.TEXT_FIELDS``); blank values are dropped (a blank never clears
        a field). With `current` (what the game has now, tag -> text) values equal to it are dropped as well: only
        changed fields are written. Pictures must be real JPEG (image) or PNG (box art, logo) bytes as made by ``scrappy.processing.images``.
        `replace`: media tags (image, thumbnail, marquee, video) whose existing files may be overwritten; every other
        existing file is kept. The video may be attached to the request later (``req.video_mp4 = ...``), so "video" is
        allowed in `replace` even when `video_mp4` is not given yet.
        `link`: media tags whose existing standard file only has to be (re)linked in gamelist.xml: the request carries the
        file's own path, the Saver keeps the file (it is not in `replace`) and sets the tag. A tag that also has a new
        picture or video is written, not linked.
        """
        system_dir = self.system_dir(system)
        clean = {}
        for tag, value in dict(fields).items():
            if tag not in TEXT_FIELDS:
                raise SaveError(f"Field {tag!r} cannot be saved by this program (allowed: {', '.join(sorted(TEXT_FIELDS))}).")
            if value is None:
                continue
            if not isinstance(value, str):
                raise SaveError(f"Field {tag!r} must be text.")
            if value.strip():
                clean[tag] = value.strip()
        if current is not None:
            clean = changed_fields(clean, current)
        pictures = {"image_jpg": (image_jpg, JPEG_MAGIC, "JPEG"), "thumb_png": (thumb_png, PNG_MAGIC, "PNG"),
                    "marquee_png": (marquee_png, PNG_MAGIC, "PNG")}
        data = {}
        for name, (payload, magic, kind) in pictures.items():
            if payload is not None:
                if not isinstance(payload, (bytes, bytearray, memoryview)) or not bytes(payload[:len(magic)]) == magic:
                    raise SaveError(f"The {PICTURE_LABELS[name]} to save is not a {kind} picture.")
                payload = bytes(payload)
            data[name] = payload
        wanted = [replace] if isinstance(replace, str) else list(replace)
        unknown = [t for t in wanted if t not in MEDIA_TAGS]
        if unknown:
            raise SaveError(f"Cannot replace {unknown[0]!r}: replace may only name {', '.join(MEDIA_TAGS)}.")
        req = SaveRequest(system_dir=system_dir, rom_path=rom_path, fields=clean, thumb_png=data["thumb_png"],
                          image_jpg=data["image_jpg"], marquee_png=data["marquee_png"],
                          video_mp4=Path(video_mp4) if video_mp4 is not None else None, replace=set(wanted))
        slots = {"image": "image_jpg", "thumbnail": "thumb_png", "marquee": "marquee_png", "video": "video_mp4"}
        for tag in ([link] if isinstance(link, str) else list(link)):
            if tag not in slots:
                raise SaveError(f"Cannot link {tag!r}: link may only name {', '.join(MEDIA_TAGS)}.")
            if getattr(req, slots[tag]) is not None or (tag == "video" and "video" in wanted):
                continue                                           # a new file is written for this slot: nothing to link
            standard = media_abs_paths(str(system_dir), rom_path)[tag]
            if not _has_content(standard):
                raise SaveError(f"There is no {MEDIA_NAMES[tag]} file to link: {standard} does not exist.")
            setattr(req, slots[tag], Path(standard))
        return req

    # ----- saving and undoing while EmulationStation runs
    # EmulationStation keeps its own copy of every game list and writes it back later (at exit, "update game lists", after a
    # game was played). A save that ignores it can be silently overwritten. So, when it is running: (1) ask it to reload
    # first, which makes it write out what it has not saved yet, and wait; (2) save; (3) ask it to reload again, wait, and
    # check that the list on disk still has our edit; (4) if it does not, apply the edit once more and check again. Never
    # more than one repeat; if that fails too the caller gets a plain warning (``es_warning``).

    def _es_up(self) -> bool:
        try:
            return bool(self._es_running())
        except Exception:                                          # noqa: BLE001 - a broken check must not stop a save
            return False

    def ask_es_to_reload(self) -> bool:
        """The Tools action: ask EmulationStation to reload its game lists; True if it answered (never raises)."""
        return self._ask_es()

    def _ask_es(self) -> bool:
        """Ask EmulationStation to reload its game lists; True if it answered."""
        try:
            return bool(self._es_reload()) if self._es_reload is not None else self._reload_request(ES_RELOAD_URL, 4.0)
        except Exception:                                          # noqa: BLE001
            return False

    def save(self, req):
        """Thread-safe (the Saver serialises calls). Raises SaveError with a plain message on any failure.

        Returns a ``SaveOutcome`` (a SaveReport plus ``es_reloaded``, ``es_reapplied``, ``es_warning``). With a running
        EmulationStation the list is flushed before, reloaded after and verified (see above)."""
        dry = self.saver.dry_run_dir is not None
        if not dry and self._es_up() and self._ask_es():
            self._sleep(self._es_waits[0])                           # let it write what it has not saved yet
        report = self.saver.save(req)
        out = SaveOutcome(**{f.name: getattr(report, f.name) for f in fields(SaveReport)})
        self._extra_entries = 0
        if dry or not out.es_running:
            return out
        out.es_reloaded = self._ask_es()
        if not out.es_reloaded:
            return out                                               # the window shows the "restart EmulationStation" hint
        self._sleep(self._es_waits[1])
        if self._list_has_edit(req, report):
            return out
        try:                                                         # EmulationStation wrote its older copy back: once more
            again = self.saver.save(dc_replace(req, replace=set()))
        except SaveError:
            out.es_warning = ES_OVERWROTE_WARNING
            return out
        self._extra_entries = 1
        self._ask_es()
        self._sleep(self._es_waits[1])
        if self._list_has_edit(req, again):
            out.es_reapplied = True
        else:
            out.es_warning = ES_OVERWROTE_WARNING
        return out

    def _list_has_edit(self, req, report) -> bool:
        """Does gamelist.xml on disk still carry what this save wrote? True if it is byte-for-byte the file the journal
        recorded, or if the game's entry still has every text field we set and a tag for every picture/video we saved."""
        gl_path = Path(req.system_dir) / "gamelist.xml"
        try:
            now = hashlib.sha256(gl_path.read_bytes()).hexdigest()
            recorded = json.loads((Path(report.journal) / "journal.json").read_text(encoding="utf-8")).get("gamelist_sha256")
        except (OSError, ValueError):
            now = recorded = None
        if now is not None and now == recorded:
            return True
        try:
            got = _GamelistIndex(Gamelist(gl_path)).get(req.rom_path)
        except (ET.ParseError, OSError, ValueError):
            return False
        if any(value.strip() and not (got.get(tag) or "").strip() for tag, value in req.fields.items()):
            return False
        for tag, payload in (("image", req.image_jpg), ("thumbnail", req.thumb_png), ("marquee", req.marquee_png),
                             ("video", req.video_mp4)):
            if payload is not None and not (got.get(tag) or "").strip():
                return False
        return True

    def undo(self, force: bool = False):
        """Undo the last save. Raises UndoRefused if gamelist.xml changed since (retry with force=True after asking).

        With a running EmulationStation it is flushed first and reloaded afterwards, and the restored list is checked
        (rewritten once if EmulationStation wrote its older copy back). The returned entry gets ``es_reloaded``,
        ``es_reapplied`` and ``es_warning``. A save that had to be applied twice is undone with both of its entries."""
        dry = self.saver.dry_run_dir is not None
        if self.saver._latest() is None:
            return None
        es = not dry and self._es_up()
        if es and self._ask_es():
            self._sleep(self._es_waits[0])
        entry = self.saver.undo_last(force=force)
        if entry is None:
            return None
        left = list(entry.get("left_alone") or [])
        extra, self._extra_entries = self._extra_entries, 0
        for _ in range(extra):
            more = self.saver.undo_last(force=True)                 # the entry below was made by our own re-apply
            if more is not None:
                left += list(more.get("left_alone") or [])
                entry = dict(more, forced=entry.get("forced") or more.get("forced"))
        entry = dict(entry, left_alone=left, es_reloaded=False, es_reapplied=False, es_warning="")
        if dry or not self._es_up():
            return entry
        gl = Path(entry["gamelist"])
        try:
            restored = gl.read_bytes()
        except FileNotFoundError:
            restored = None
        except OSError:
            return entry
        entry["es_reloaded"] = self._ask_es()
        if not entry["es_reloaded"]:
            return entry
        self._sleep(self._es_waits[1])
        if self._undo_holds(entry, restored):
            return entry
        try:                                                         # it wrote its older list back: restore ours once more
            if restored is None:
                gl.unlink(missing_ok=True)
            else:
                real = Path(os.path.realpath(gl))
                atomic_replace(real, lambda f: f.write(restored))
        except OSError:
            entry["es_warning"] = ES_UNDO_WARNING
            return entry
        self._ask_es()
        self._sleep(self._es_waits[1])
        if self._undo_holds(entry, restored):
            entry["es_reapplied"] = True
        else:
            entry["es_warning"] = ES_UNDO_WARNING
        return entry

    @staticmethod
    def _undo_holds(entry, restored) -> bool:
        """Is the list on disk still the undone one? Same bytes as right after the undo, or the game no longer points to any
        file the undone save created."""
        gl = Path(entry["gamelist"])
        try:
            now = gl.read_bytes()
        except FileNotFoundError:
            now = None
        except OSError:
            return False
        if now == restored:
            return True
        if now is None:
            return restored is None
        created = {os.path.basename(f) for f in entry.get("created", [])}
        try:
            got = _GamelistIndex(Gamelist(gl)).get(entry.get("rom_path", ""))
        except (ET.ParseError, OSError, ValueError):
            return False
        return not any(os.path.basename((got.get(t) or "").strip()) in created for t in MEDIA_TAGS if (got.get(t) or "").strip())

    def _reload_request(self, url: str, timeout: float) -> bool:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(url, timeout=timeout) as response:
                return 200 <= response.status < 300
        except (OSError, ValueError):                      # URLError and HTTPError are OSErrors; http.client errors too
            return False

    def reload_es(self, url: str = ES_RELOAD_URL, timeout: float = 4.0) -> str:
        """Optional: ask EmulationStation to reload game lists. Returns a short status text. Never uses a proxy."""
        failed = f"Could not reach EmulationStation. Use {ES_BY_HAND}."
        return "EmulationStation was asked to reload its game lists." if self._reload_request(url, timeout) else failed
