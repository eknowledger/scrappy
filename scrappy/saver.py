"""Write approved assets and metadata into a system folder, with backup, journal and undo.

What the user needs to know (the UI shows ``ES_RESTART_HINT`` when ``SaveReport.es_running`` is true and the controller
could not ask EmulationStation to reload)
----------------------------------------------------------------------------------------------
EmulationStation keeps its own in-memory copy of every ``gamelist.xml`` and rewrites it when it exits
(or after a game changes the play count). The Saver itself does not talk to it: ``Controller.save`` asks it to flush
before and reload after, checks the result and applies the edit once more if needed (see docs/ARCHITECTURE.md, 7.5). When
EmulationStation cannot be reached:

* the new art and text only appear after EmulationStation has re-read the game lists: restart it from
  its menu (Start > Quit > Restart EmulationStation) or use Start > Game Settings > Update Gamelists;
* quit/restart EmulationStation *before* editing more, or an exit-time rewrite from its older in-memory
  copy can overwrite what we saved. Our own files are safe either way: every save keeps a copy of the
  previous ``gamelist.xml`` (backup folder, plus a per-save copy in the journal for exact undo).

How a save works
----------------
``Saver.save`` is one transaction, serialised by a lock (the UI calls it from a worker thread):

1. Validate everything before touching the disk: the ROM path, field names (``TEXT_FIELDS`` only) and
   values (characters XML cannot hold are dropped), the pictures/video, and that every media path
   resolves *inside* the system folder (``..``, absolute paths, symlinked ``images``/``videos`` folders
   or files pointing elsewhere are refused).
2. Load ``gamelist.xml`` (a missing or empty one is started fresh; a damaged one is never overwritten),
   remember its mtime+size, take the backup (once per session, again whenever the file changed behind our
   back), and keep an exact "before" copy in this save's journal folder.
3. Write the media atomically (tmp file, fsync, rename). Existing non-empty files are kept unless the
   caller put the tag in ``SaveRequest.replace``; a replaced file is first copied into the journal folder.
   A 0-byte file counts as missing.
4. Stale-tree guard: right before writing ``gamelist.xml`` the mtime+size are compared with those seen
   at load. If something (usually EmulationStation) changed the file meanwhile, it is reloaded and our
   edit is applied again on the fresh copy. That is always safe because the edit only sets the tags we
   were asked to set and is idempotent. If the file keeps changing (3 tries) ``StaleGamelist`` is raised
   and nothing is left behind. The remaining window (between that check and the rename) is a few
   microseconds; only stopping EmulationStation closes it completely.
5. Re-read what was written to prove it is well-formed XML, then write ``journal.json``.

The slot rule: art the user already has is theirs. If the gamelist tag of a slot points to a non-empty file other than
our canonical name, that slot is kept as it is (nothing written, tag unchanged) unless the tag is in ``replace``. A kept
canonical file is linked only if the tag is empty or leads to a missing/empty file.

Any failure (disk full, permissions, a changed mind) rolls back: files created in this save are removed,
replaced files and ``gamelist.xml`` are restored, folders we created are removed. The user gets a
``SaveError`` with a plain-language message (it is a ``ProviderError`` so ``describe_error`` shows it as is).

``Saver.undo_last`` restores ``gamelist.xml`` from the journal's "before" copy and removes/restores that
save's media. It refuses (``UndoRefused``) when ``gamelist.xml`` is no longer what we wrote, unless
``force=True`` (the file being discarded is then kept in the journal folder). Media that changed since the
save is never touched; it is listed in ``left_alone``.

``dry_run_dir`` makes the whole pipeline run against a sandbox copy (gamelist and the existing media of the
game are copied in on first use; backups and journal live there too), leaving the real folders untouched.

A crash or power cut in the middle of a save cannot corrupt a file (every write is tmp + fsync + rename). What it leaves behind is
described by the journal entry (below): the entry exists before the first media write and before ``gamelist.xml`` is replaced.

Journal folders: the newest ``JOURNAL_KEEP`` (20) saves and the newest 20 undone saves are kept, older ones are
deleted after a successful save (including the old copies of media they replaced). A folder without a
``journal.json`` is neither counted nor deleted. Temp files have fixed-length random names and are created
exclusively (``scrappy.atomic``), so a symlink planted at a guessable name is never followed and 255-byte media names
work; media paths are re-checked against the system folder just before each write. Replaced media keep their file
mode, and undo puts the old mode back. Mode copying and fsync are best effort, so NTFS/FUSE drives
(``/media/Windows``) work; real I/O errors still fail the save and roll it back.

A skeleton ``journal.json`` (state ``media-in-progress``, no gamelist checksum) is written before the first media
write, after the old copies of replaced files were saved. If the process dies during the media writes, Undo removes
the files created and restores the files replaced, and leaves ``gamelist.xml`` alone (it was never touched).

Known limitations: ElementTree drops XML comments in gamelist.xml when it is rewritten (EmulationStation writes
none). Media names come from the ROM's base name only, so two ROMs with the same name in different sub-folders
share one set of files (the same rule Batocera's scraper follows).

The gamelist itself may be a symlink to a file elsewhere (``Gamelist.save`` keeps it a symlink); only media
paths are confined to the system folder.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace as dc_replace
from pathlib import Path
from typing import Callable, Optional

from .atomic import atomic_replace, fsync_dir
from .gamelist import Gamelist, _norm, backup_file
from .media import media_abs_paths, media_rel_paths
from .processing.images import write_atomic
from .providers.base import ProviderError
from .roms import _has_content

TEXT_FIELDS = frozenset({"name", "desc", "releasedate", "developer", "publisher", "genre", "players"})
MEDIA_TAGS = ("image", "thumbnail", "marquee", "video")
LABELS = {"image": "image", "thumbnail": "box art (thumbnail)", "marquee": "logo (marquee)", "video": "video"}
MAX_ATTEMPTS = 3
JOURNAL_KEEP = 20          # saves (and, separately, undone saves) whose journal folders are kept

ES_RESTART_HINT = ("EmulationStation is running. Restart it from its menu (Start > Quit > Restart EmulationStation, "
                   "or Start > Game Settings > Update Gamelists) to see the changes, and do it before making more "
                   "edits: when EmulationStation exits it may rewrite gamelist.xml from its older copy and "
                   "overwrite them. Use Undo last save if that happens.")

# characters XML 1.0 cannot represent (control characters, lone surrogates, U+FFFE/U+FFFF)
_BAD_XML_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


class SaveError(ProviderError):
    """A save or undo could not be done; the message is plain language and nothing was left half-done."""


class StaleGamelist(SaveError):
    """gamelist.xml kept changing while we tried to save (EmulationStation or another program is writing it)."""


class UndoRefused(SaveError):
    """gamelist.xml is not what the save wrote, so undoing would throw away someone else's changes."""


@dataclass
class SaveRequest:
    system_dir: Path
    rom_path: str                                     # './Sub/Game.nsp' relative to system_dir
    fields: dict = field(default_factory=dict)        # text tags, see TEXT_FIELDS
    image_jpg: Optional[bytes] = None
    thumb_png: Optional[bytes] = None
    marquee_png: Optional[bytes] = None
    video_mp4: Optional[Path] = None                  # already-encoded file; it is copied, not moved
    replace: set = field(default_factory=set)         # media tags allowed to overwrite existing files


@dataclass
class SaveReport:
    written: list                                     # str paths created or replaced
    kept: list                                        # str paths that existed and were left alone
    replaced: list                                    # str paths overwritten (old copy is in the journal folder)
    backup: Optional[Path]                            # this session's backup of gamelist.xml (None: there was none)
    journal: Path
    backup_taken: bool = False                        # True if this very save made a new backup
    linked: list = field(default_factory=list)        # kept files that gamelist.xml did not mention and now does
    es_running: bool = False                          # show ES_RESTART_HINT when true
    dry_run: bool = False


# ---------------------------------------------------------------- helpers

def emulationstation_running(proc_root="/proc") -> bool:
    """True if an EmulationStation process exists (looks in /proc; False where there is no /proc, e.g. macOS)."""
    try:
        entries = list(Path(proc_root).iterdir())
    except OSError:
        return False
    for p in entries:
        if not p.name.isdigit():
            continue
        try:
            if (p / "comm").read_text(errors="replace").strip().startswith("emulationstatio"):   # comm is cut at 15 chars
                return True
        except OSError:
            pass
        try:
            argv0 = (p / "cmdline").read_bytes().split(b"\0")[0].decode(errors="replace")
            if os.path.basename(argv0) == "emulationstation":
                return True
        except OSError:
            pass
    return False


def _stamp(path) -> Optional[tuple]:
    """(mtime_ns, size) of a file, or None when it does not exist."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _sha256(path) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except FileNotFoundError:
        return None


def _sha_bytes(data: Optional[bytes]) -> Optional[str]:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _atomic_copy(src, dst, mode: Optional[int] = None) -> str:
    """Copy src to dst (random temp file, fsync, rename; a symlink at dst is followed) and return the sha256 of the data.

    The copy keeps the mode of the file it replaces, or `mode` when given.
    """
    h = hashlib.sha256()

    def fill(fout):
        with open(src, "rb") as fin:
            for chunk in iter(lambda: fin.read(1 << 20), b""):
                h.update(chunk)
                fout.write(chunk)

    atomic_replace(os.path.realpath(dst), fill, mode=mode)
    return h.hexdigest()


def _mode_of(path) -> Optional[int]:
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None


def _explain(exc: OSError, doing: str = "save the files") -> str:
    where = getattr(exc, "filename", None)
    at = f" ({where})" if where else ""
    code = getattr(exc, "errno", None)
    if code in (errno.ENOSPC, getattr(errno, "EDQUOT", None)):
        return f"The disk is full{at}. Free some space and try again."
    if code in (errno.EACCES, errno.EPERM):
        return f"No permission to write{at}. Check that the folder is writable by this user."
    if code == errno.EROFS:
        return f"The drive is read-only{at}. Remount it read-write and try again."
    if code == errno.ENAMETOOLONG:
        return f"A file name is too long for this drive{at}."
    return f"Could not {doing}{at}: {exc.strerror or exc}."


def _inside(path, root_real: str) -> bool:
    real = os.path.realpath(path)
    return real == root_real or real.startswith(root_real + os.sep)


def _clean_rom_path(rom_path) -> str:
    if not isinstance(rom_path, str) or not rom_path.strip():
        raise SaveError("The game path is empty.")
    p = rom_path.replace("\\", "/")
    parts = [c for c in p.split("/") if c not in ("", ".")]
    if "\x00" in p or p.startswith("/") or re.match(r"[A-Za-z]:", p) or not parts or ".." in parts:
        raise SaveError(f"The game path {rom_path!r} is not valid (it must be a plain path inside the system folder).")
    return p


def _clean_fields(fields: dict) -> dict:
    out = {}
    for tag, value in fields.items():
        if tag not in TEXT_FIELDS:
            raise SaveError(f"Field {tag!r} cannot be saved by this program (allowed: {', '.join(sorted(TEXT_FIELDS))}).")
        if value is not None and not isinstance(value, str):
            raise TypeError(f"field {tag!r} must be str, got {type(value).__name__}")
        out[tag] = None if value is None else _BAD_XML_CHARS.sub("", value)
    return out


def _check_payload(tag: str, payload):
    """Return ('bytes', data) or ('file', Path) for a usable payload; SaveError for an empty/unreadable one."""
    label = LABELS[tag]
    if isinstance(payload, (bytes, bytearray, memoryview)):
        data = bytes(payload)
        if not data:
            raise SaveError(f"The {label} to save is empty.")
        return "bytes", data
    path = Path(payload)
    try:
        st = path.stat()
    except OSError as e:
        raise SaveError(f"Cannot read the {label} file {path}: {e.strerror or e}.") from e
    if not stat.S_ISREG(st.st_mode) or st.st_size == 0:
        raise SaveError(f"The {label} file {path} is empty or not a regular file.")
    return "file", path


@dataclass
class _Item:
    tag: str
    target: Path
    rel: str
    kind: str            # 'bytes' | 'file'
    payload: object
    state: str           # 'create' | 'replace' | 'keep'
    explicit: bool = False                 # state 'replace' because the caller asked for it (not an empty leftover)
    tag_untouched: bool = False            # keep: gamelist.xml already points this slot at a real file of the user's
    kept_path: Optional[Path] = None       # the file reported as kept (the one gamelist.xml points to)
    sha: Optional[str] = None              # sha256 of what will be written (known before the first write)
    rec: Optional[dict] = None             # journal record of the file this item replaces


@dataclass
class _Txn:
    jdir: Path
    created_files: list = field(default_factory=list)       # [{"file", "sha256"}]
    created_dirs: list = field(default_factory=list)
    replaced: list = field(default_factory=list)            # [{"file", "backup", "sha256_before", "sha256_after"}]
    before: Optional[bytes] = None                          # gamelist.xml as we loaded it (None: did not exist)
    gl_path: Optional[Path] = None
    system_dir: Optional[Path] = None
    root: str = ""                                          # realpath of the system folder
    decided: bool = False
    gl_written: bool = False
    backup: Optional[Path] = None
    backup_taken: bool = False


class Saver:
    """Saves and undoes game assets. One instance per application; all state lives here (no globals).

    ``es_running`` is a callable returning True when EmulationStation is running (inject a fake in tests).
    """

    def __init__(self, backup_dir, journal_dir, *, dry_run_dir=None, journal_keep: int = JOURNAL_KEEP,
                 es_running: Callable[[], bool] = emulationstation_running):
        if journal_keep < 1:
            raise ValueError(f"journal_keep must be >= 1, got {journal_keep}")
        self.journal_keep = journal_keep
        self.dry_run_dir = Path(dry_run_dir) if dry_run_dir is not None else None
        if self.dry_run_dir is not None:
            backup_dir, journal_dir = self.dry_run_dir / "backups", self.dry_run_dir / "journal"
        self.backup_dir = Path(backup_dir) if backup_dir is not None else None
        self.journal_dir = Path(journal_dir)
        self._es_running = es_running
        self._lock = threading.Lock()
        self._known: dict[str, tuple] = {}          # gamelist realpath -> stamp we last saw/wrote (and have a backup of)
        self._backups: dict[str, Path] = {}         # gamelist realpath -> this session's backup file

    def new_session(self):
        """Forget what was backed up: the next save takes a fresh backup."""
        with self._lock:
            self._known.clear()
            self._backups.clear()

    # ------------------------------------------------------------ saving

    def save(self, req: SaveRequest) -> SaveReport:
        with self._lock:
            return self._save(req)

    def _resolve_targets(self, system_dir: Path, rom: str) -> dict:
        """Absolute media paths, proven to lie inside the system folder."""
        root = os.path.realpath(system_dir)
        targets = {}
        for tag, path in media_abs_paths(str(system_dir), rom).items():
            target = Path(path)
            folder = target.parent
            if os.path.lexists(folder) and not os.path.isdir(folder):
                raise SaveError(f"'{folder.name}' in {system_dir} is not a folder (a file or a broken link is in the way).")
            if not _inside(folder, root) or not _inside(target, root):
                raise SaveError(f"Refusing to write {target}: it leads outside the system folder {system_dir} "
                                "(a symbolic link?).")
            targets[tag] = target
        return targets

    def _sandbox(self, system_dir: Path, rom: str, targets: dict) -> Path:
        real = os.path.realpath(system_dir)
        dry = os.path.realpath(self.dry_run_dir)
        if dry == real or dry.startswith(real + os.sep):
            raise SaveError("The dry-run folder must be outside the system folder it imitates.")
        box = self.dry_run_dir / system_dir.name
        box.mkdir(parents=True, exist_ok=True)
        seeds = [(system_dir / "gamelist.xml", box / "gamelist.xml")]
        seeds += [(t, box / t.relative_to(system_dir)) for t in targets.values()]
        try:                                                  # also the art the game's tags point to, like the real run sees it
            current = Gamelist(system_dir / "gamelist.xml").get(rom)
        except (ET.ParseError, ValueError, OSError):
            current = {}
        for tag in MEDIA_TAGS:
            value = (current.get(tag) or "").strip()
            if not value:
                continue
            path = os.path.normpath(os.path.join(str(system_dir), value[2:] if value.startswith("./") else value))
            rel = os.path.relpath(path, system_dir)
            if _inside(path, real) and not rel.startswith(".." + os.sep) and rel != ".." and not os.path.isabs(rel):
                seeds.append((Path(path), box / rel))
        for src, dst in seeds:
            if os.path.isfile(src) and not os.path.lexists(dst):
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        return box

    def _plan(self, req: SaveRequest, system_dir: Path, targets: dict) -> list:
        rel = media_rel_paths(req.rom_path)
        payloads = {"image": req.image_jpg, "thumbnail": req.thumb_png, "marquee": req.marquee_png, "video": req.video_mp4}
        items = []
        for tag in MEDIA_TAGS:
            if payloads[tag] is None:
                continue
            kind, payload = _check_payload(tag, payloads[tag])
            target = targets[tag]
            try:
                st = os.lstat(target)
            except FileNotFoundError:
                state = "create"
            else:
                if not os.path.isfile(target):
                    raise SaveError(f"Cannot save the {LABELS[tag]}: {target} exists but is not a regular file.")
                if os.path.getsize(target) == 0:
                    state = "replace"                         # a failed earlier write; nothing of value is lost
                else:
                    state = "replace" if tag in req.replace else "keep"
            items.append(_Item(tag, target, rel[tag], kind, payload, state, explicit=tag in req.replace))
        return items

    def _save(self, req: SaveRequest) -> SaveReport:
        if self.backup_dir is None:
            raise ValueError("this Saver was created without a backup folder; it can only undo")
        rom = _clean_rom_path(req.rom_path)
        fields = _clean_fields(req.fields)
        system_dir = Path(req.system_dir)
        if not system_dir.is_dir():
            raise SaveError(f"The system folder {system_dir} does not exist.")
        targets = self._resolve_targets(system_dir, rom)
        dry = self.dry_run_dir is not None
        if dry:
            self._sandbox(system_dir, rom, targets)
            system_dir = self.dry_run_dir / system_dir.name
            targets = self._resolve_targets(system_dir, rom)
        req = dc_replace(req, system_dir=system_dir, rom_path=rom)
        items = self._plan(req, system_dir, targets)

        gl_path = system_dir / "gamelist.xml"
        txn = _Txn(jdir=Path(), gl_path=gl_path, system_dir=system_dir, root=os.path.realpath(system_dir))
        try:
            txn.jdir = self._new_journal_dir()
            gl, seen, linked = self._prepare_gamelist(txn, rom, fields, items)
            if any(i.state != "keep" for i in items):
                self._stage_media(txn, items)
                # skeleton entry BEFORE the first media write: a crash mid-media (big videos) still leaves a usable undo
                self._write_journal(txn.jdir, self._entry(txn, rom, items, "media-in-progress", None, planned=True))
            self._write_media(txn, items)
            attempts = 1
            while _stamp(gl_path) != seen:               # stale-tree guard: changed since we loaded it
                if attempts >= MAX_ATTEMPTS:
                    raise StaleGamelist(
                        "gamelist.xml keeps changing while saving (EmulationStation or another program is writing it). "
                        "Quit or restart EmulationStation and try again; nothing was changed.")
                gl, seen, linked = self._prepare_gamelist(txn, rom, fields, items)    # reload, apply our edit again
                attempts += 1
            new_bytes = gl.to_bytes()
            entry = self._entry(txn, rom, items, "in-progress", _sha_bytes(new_bytes))
            self._write_journal(txn.jdir, entry)             # a power cut from here on still leaves a usable entry
            gl.save()
            txn.gl_written = True
            written = gl_path.read_bytes()
            self._verify(written, rom)
            self._known[os.path.realpath(gl_path)] = _stamp(gl_path)
            entry["gamelist_sha256"] = _sha_bytes(written)
            entry["state"] = "done"
            self._write_journal(txn.jdir, entry)
        except BaseException as exc:
            problems = self._rollback(txn)
            left = (f" Some changes could not be reverted automatically ({'; '.join(problems)}); "
                    f"the saved copies are in {txn.jdir}.") if problems else ""
            if isinstance(exc, OSError):
                raise SaveError(_explain(exc) + (left or " Nothing was changed.")) from exc
            if left and isinstance(exc, SaveError):
                raise type(exc)(str(exc) + left) from exc
            raise

        replaced = [r["file"] for r in txn.replaced]
        self._prune_journal()
        return SaveReport(
            written=[c["file"] for c in txn.created_files] + replaced,
            kept=self._kept(items), replaced=replaced,
            backup=self._backups.get(os.path.realpath(gl_path)), journal=txn.jdir, backup_taken=txn.backup_taken,
            linked=linked, es_running=False if dry else self._check_es(), dry_run=dry)

    def _entry(self, txn: _Txn, rom: str, items: list, state: str, gamelist_sha: Optional[str], planned: bool = False) -> dict:
        """The journal.json content. `planned` lists what the media writes are about to create (before they happened);
        a None `gamelist_sha` means gamelist.xml has not been touched and undo must leave it alone."""
        if planned:
            created = [(str(i.target), i.sha) for i in items if i.state == "create"]
        else:
            created = [(c["file"], c["sha256"]) for c in txn.created_files]
        return {
            "version": 1, "state": state, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "system_dir": str(txn.system_dir), "rom_path": rom,
            "gamelist": str(txn.gl_path), "gamelist_existed": txn.before is not None,
            "gamelist_sha256": gamelist_sha, "backup": str(txn.backup) if txn.backup else None,
            "created": [f for f, _ in created], "created_sha256": dict(created),
            "created_dirs": [str(d) for d in txn.created_dirs],
            "replaced": txn.replaced, "kept": self._kept(items),
        }

    @staticmethod
    def _kept(items: list) -> list:
        return [str(i.kept_path or i.target) for i in items if i.state == "keep"]

    def _prune_journal(self):
        """Keep the newest `journal_keep` saves and undone saves; never the entry undo would use, nor newer ones.

        Only folders with a journal.json are ever deleted. Never fails the save.
        """
        try:
            latest = self._latest()
            if latest is None:
                return
            for folder in (self.journal_dir, self.journal_dir / "undone"):
                if not folder.is_dir():
                    continue
                # folders without a journal.json (a crash before it was written) are neither counted nor deleted
                dirs = sorted((p for p in folder.iterdir()
                               if p.is_dir() and p.name.isdigit() and (p / "journal.json").exists()), key=lambda p: int(p.name))
                for old in dirs[:-self.journal_keep] if len(dirs) > self.journal_keep else []:
                    if int(old.name) < int(latest.name) or folder != self.journal_dir:
                        shutil.rmtree(old)
        except Exception:                                    # noqa: BLE001  (housekeeping must not fail a finished save)
            pass

    def _check_es(self) -> bool:
        try:
            return bool(self._es_running())
        except Exception:                                    # noqa: BLE001  (a broken check must not fail a finished save)
            return False

    def _new_journal_dir(self) -> Path:
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        newest = max((int(p.name) for p in self.journal_dir.iterdir() if p.name.isdigit()), default=0)
        us = max(time.time_ns() // 1000, newest + 1)          # strictly increasing: names sort in save order
        while True:
            jdir = self.journal_dir / f"{us:016d}"
            try:
                jdir.mkdir()
                return jdir
            except FileExistsError:
                us += 1

    def _load_gamelist(self, gl_path: Path):
        """(Gamelist, bytes-or-None, stamp) of a gamelist.xml that did not change while we read it."""
        for _ in range(MAX_ATTEMPTS):
            before = _stamp(gl_path)
            try:
                data = gl_path.read_bytes() if before is not None else None
            except FileNotFoundError:
                continue
            try:
                gl = Gamelist(gl_path)
            except (ET.ParseError, ValueError) as e:
                raise SaveError(f"{gl_path} could not be read ({e}). It was not changed; fix or restore it first.") from e
            if _stamp(gl_path) == before:
                return gl, data, before
        raise StaleGamelist("gamelist.xml keeps changing while it is being read (EmulationStation or another program "
                            "is writing it). Quit or restart EmulationStation and try again.")

    def _prepare_gamelist(self, txn: _Txn, rom: str, fields: dict, items: list):
        """Load gamelist.xml, back it up if needed, apply our edit in memory. Returns (gamelist, stamp seen, linked tags)."""
        gl_path = txn.gl_path
        key = os.path.realpath(gl_path)
        for _ in range(MAX_ATTEMPTS):
            gl, data, stamp = self._load_gamelist(gl_path)
            if data and self._known.get(key) != stamp:       # nothing to lose in a missing or empty file
                self.backup_dir.mkdir(parents=True, exist_ok=True)
                self._backups[key] = backup_file(gl_path, self.backup_dir)
                txn.backup_taken = True
                self._known[key] = stamp
                if _stamp(gl_path) != stamp:                 # changed while being copied: start over with the new one
                    self._known.pop(key, None)
                    continue
            txn.backup = self._backups.get(key)
            if data is not None:
                write_atomic(txn.jdir / "gamelist.before.xml", data)
            txn.before = data
            xml_fields = dict(fields)
            linked = []
            current = gl.get(rom)
            if not txn.decided:
                self._decide(txn, current, items)
                txn.decided = True
            for it in items:
                if it.state != "keep":
                    xml_fields[it.tag] = it.rel
                elif not it.tag_untouched:
                    cur = (current.get(it.tag) or "").strip()
                    # link the kept canonical file only if the tag is empty or leads nowhere useful
                    if not cur or (_norm(cur) != _norm(it.rel) and not _has_content(self._tag_file(txn, cur))):
                        xml_fields[it.tag] = it.rel
                        linked.append(it.tag)
            gl.upsert(rom, xml_fields)
            return gl, stamp, linked
        raise StaleGamelist("gamelist.xml keeps changing (EmulationStation or another program is writing it). "
                            "Quit or restart EmulationStation and try again.")

    @staticmethod
    def _tag_file(txn: _Txn, value: str) -> str:
        """Where a gamelist tag value points (relative to the system folder, like roms.asset_status reads it)."""
        return os.path.join(str(txn.system_dir), value[2:] if value.startswith("./") else value)

    def _decide(self, txn: _Txn, current: dict, items: list):
        """The user's own art wins: a slot whose tag already points to a real file other than our canonical name is
        kept as it is (nothing written, tag unchanged) unless the caller asked to replace it."""
        for it in items:
            cur = (current.get(it.tag) or "").strip()
            if it.explicit or not cur or _norm(cur) == _norm(it.rel):
                continue
            other = self._tag_file(txn, cur)
            if _has_content(other):
                it.state, it.tag_untouched, it.kept_path = "keep", True, Path(other)

    def _stage_media(self, txn: _Txn, items: list):
        """Everything that must exist before the first media write: folders, a saved copy of each file that will be
        replaced, and the checksum of each file that will be written. Nothing in the real media files changes here."""
        for it in items:
            if it.state == "keep":
                continue
            self._make_dirs(it.target.parent, txn)
            self._check_inside(txn, it)
            it.sha = _sha_bytes(it.payload) if it.kind == "bytes" else _sha256(it.payload)
            if it.state == "replace":
                saved = txn.jdir / "replaced" / it.target.name
                saved.parent.mkdir(parents=True, exist_ok=True)
                before = _atomic_copy(it.target, saved)
                it.rec = {"file": str(it.target), "backup": str(saved), "sha256_before": before, "sha256_after": it.sha,
                          "mode": _mode_of(it.target)}
                txn.replaced.append(it.rec)

    @staticmethod
    def _check_inside(txn: _Txn, it: _Item):
        if not _inside(it.target.parent, txn.root) or not _inside(it.target, txn.root):
            raise SaveError(f"Refusing to write {it.target}: it leads outside the system folder {txn.system_dir} "
                            "(a symbolic link?).")

    def _write_media(self, txn: _Txn, items: list):
        for it in items:
            if it.state == "keep":
                continue
            self._check_inside(txn, it)     # again, right before writing: the folder may have been swapped for a link
            if it.kind == "bytes":
                write_atomic(it.target, it.payload)
                digest = _sha_bytes(it.payload)
            else:
                try:
                    digest = _atomic_copy(it.payload, it.target)
                except FileNotFoundError as e:
                    if e.filename == str(it.payload):
                        raise SaveError(f"The video file {it.payload} disappeared before it could be saved.") from e
                    raise
            if it.rec is not None:
                it.rec["sha256_after"] = digest
            else:
                txn.created_files.append({"file": str(it.target), "sha256": digest})

    @staticmethod
    def _make_dirs(folder: Path, txn: _Txn):
        missing = []
        p = folder
        while not p.exists() and p != p.parent:
            missing.append(p)
            p = p.parent
        for d in reversed(missing):
            d.mkdir()
            txn.created_dirs.append(d)

    @staticmethod
    def _verify(written: bytes, rom: str):
        try:
            root = ET.fromstring(written)
        except ET.ParseError as e:
            raise SaveError(f"gamelist.xml did not come out as valid XML ({e}); the previous version was restored.") from e
        if not any(_norm(g.findtext("path") or "") == _norm(rom) for g in root.findall("game")):
            raise SaveError("The game was not found in the new gamelist.xml; the previous version was restored.")

    def _write_journal(self, jdir: Path, entry: dict):
        write_atomic(jdir / "journal.json", json.dumps(entry, indent=1).encode("utf-8"))
        fsync_dir(jdir)                                      # best effort: make the new entry survive a power cut
        fsync_dir(jdir.parent)

    def _rollback(self, txn: _Txn) -> list:
        """Undo this save's changes as far as they happened. Returns descriptions of anything that could not be undone."""
        problems = []

        def attempt(what, fn):
            try:
                fn()
            except Exception as e:                           # noqa: BLE001
                problems.append(f"{what}: {e}")

        if txn.gl_written and txn.gl_path is not None:
            if txn.before is None:
                attempt("gamelist.xml", lambda: txn.gl_path.unlink(missing_ok=True))
            else:
                attempt("gamelist.xml", lambda: write_atomic(txn.gl_path, txn.before))
            self._known[os.path.realpath(txn.gl_path)] = _stamp(txn.gl_path)
        for c in reversed(txn.created_files):
            attempt(c["file"], lambda c=c: Path(c["file"]).unlink(missing_ok=True))
        for r in txn.replaced:
            attempt(r["file"], lambda r=r: _atomic_copy(r["backup"], r["file"], mode=r.get("mode")))
        for d in reversed(txn.created_dirs):
            try:
                d.rmdir()
            except OSError:
                pass
        if not problems and txn.jdir != Path():
            shutil.rmtree(txn.jdir, ignore_errors=True)
        return problems

    # ------------------------------------------------------------ undoing

    def undo_last(self, force: bool = False) -> Optional[dict]:
        """Undo the most recent save. Returns its journal entry (plus ``left_alone``), or None if nothing is left."""
        with self._lock:
            return self._undo_last(force)

    def _latest(self) -> Optional[Path]:
        if not self.journal_dir.is_dir():
            return None
        entries = sorted((p for p in self.journal_dir.iterdir()
                          if p.is_dir() and p.name.isdigit() and (p / "journal.json").exists()), key=lambda p: int(p.name))
        return entries[-1] if entries else None

    def _undo_last(self, force: bool) -> Optional[dict]:
        last = self._latest()
        if last is None:
            return None
        try:
            entry = json.loads((last / "journal.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise SaveError(f"Cannot undo: the record of the last save ({last / 'journal.json'}) is unreadable ({e}).") from e
        gl = Path(entry["gamelist"])
        before_file = last / "gamelist.before.xml"
        before = None
        # a skeleton entry (crash during the media writes) has no checksum: gamelist.xml was never touched, leave it alone
        touch_gl = entry["gamelist_sha256"] is not None
        if touch_gl and entry["gamelist_existed"]:
            try:
                before = before_file.read_bytes()
            except OSError as e:
                raise SaveError(f"Cannot undo safely: the saved copy of gamelist.xml for this save is gone ({before_file}).") from e
        copies = {}
        for r in entry["replaced"]:
            copies[r["file"]] = _saved_copy(last, r)
            if not os.path.isfile(copies[r["file"]]):
                raise SaveError(f"Cannot undo safely: the saved copy of the old file {r['file']} is gone ({r['backup']}).")
        try:
            current = gl.read_bytes()
        except FileNotFoundError:
            current = None
        cur_sha, before_sha = _sha_bytes(current), _sha_bytes(before)
        forced = False
        if not touch_gl:
            restore = False
        elif cur_sha == entry["gamelist_sha256"]:
            restore = True
        elif cur_sha == before_sha:
            restore = False                                  # an earlier, interrupted undo already did this part
        elif force:
            restore, forced = True, True
        else:
            raise UndoRefused(
                f"{gl} was changed after the last save (probably by EmulationStation or another program), so undoing "
                "would throw those changes away. Restart EmulationStation, check the game list, and use force to undo anyway.")
        left_alone = []
        try:
            if restore:
                if forced and current is not None:
                    write_atomic(last / "gamelist.discarded-by-undo.xml", current)
                if before is None:
                    gl.unlink(missing_ok=True)
                else:
                    write_atomic(gl, before)
                key = os.path.realpath(gl)
                stamp = _stamp(gl)
                if stamp is None:
                    self._known.pop(key, None)
                else:
                    self._known[key] = stamp
            for f in entry["created"]:
                p = Path(f)
                if not os.path.lexists(p):
                    continue
                if _sha256(p) == entry["created_sha256"].get(f):
                    p.unlink(missing_ok=True)
                else:
                    left_alone.append(f)
            for r in entry["replaced"]:
                p = Path(r["file"])
                now = _sha256(p)
                if now == r["sha256_before"]:
                    continue                                 # already back to the old file
                if now is None or now == r["sha256_after"]:
                    _atomic_copy(copies[r["file"]], p, mode=r.get("mode"))
                else:
                    left_alone.append(r["file"])
            for d in reversed(entry.get("created_dirs", [])):
                try:
                    Path(d).rmdir()
                except OSError:
                    pass
        except OSError as e:
            raise SaveError(_explain(e, "undo the last save") + " Fix that and use Undo again; it can be repeated safely.") from e
        try:
            done = self.journal_dir / "undone"
            done.mkdir(exist_ok=True)
            shutil.move(str(last), str(done / last.name))
        except OSError as e:
            raise SaveError(_explain(e, "file the record of the undone save away") +
                            " Everything was already restored; use Undo again to finish tidying up.") from e
        return dict(entry, left_alone=left_alone, forced=forced)


def _saved_copy(journal_folder: Path, rec: dict) -> str:
    """Where the saved copy of a replaced file is now.

    journal.json records its absolute path, but the journal can have moved since that save was made (the installer moves
    the journal into a renamed data folder), which leaves the recorded path stale. The copy is always kept in
    ``<journal folder of that save>/replaced/<name>``, so look there when the recorded path no longer exists. A copy
    found there is used only if it is byte for byte the file that was saved (its checksum is in the record); otherwise
    the recorded path is returned and undo reports the copy as gone, rather than restore the wrong content."""
    recorded = rec["backup"]
    if os.path.isfile(recorded):
        return recorded
    beside = journal_folder / "replaced" / Path(recorded).name
    if beside.is_file() and _sha256(beside) == rec.get("sha256_before"):
        return str(beside)
    return recorded


# ---------------------------------------------------------------- one-shot functions

def save_game(req: SaveRequest, backup_dir, journal_dir, **kw) -> SaveReport:
    """Save with a throw-away Saver (so it always takes its own backup). Applications should keep one Saver."""
    return Saver(backup_dir, journal_dir, **kw).save(req)


def undo_last(journal_dir, force: bool = False) -> Optional[dict]:
    return Saver(None, journal_dir).undo_last(force=force)
