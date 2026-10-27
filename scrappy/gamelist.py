"""Read and modify EmulationStation gamelist.xml without disturbing anything we do not touch."""
from __future__ import annotations

import os
import re
import shutil
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from .atomic import atomic_replace

SCRAPER_NAME = "Scrappy"


def _norm(path: str) -> str:
    p = str(path).replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


class Gamelist:
    def __init__(self, path):
        self.path = Path(path)
        if self.path.exists() and self.path.stat().st_size > 0:
            self.tree = ET.parse(self.path)
            self.root = self.tree.getroot()
        else:
            self.root = ET.Element("gameList")
            self.tree = ET.ElementTree(self.root)

    def games(self):
        return self.root.findall("game")

    def find(self, rom_path: str):
        want = _norm(rom_path)
        for g in self.games():
            if _norm(g.findtext("path") or "") == want:
                return g
        return None

    def get(self, rom_path: str) -> dict:
        g = self.find(rom_path)
        if g is None:
            return {}
        return {c.tag: (c.text or "") for c in g if c.tag != "scrap"}

    def upsert(self, rom_path: str, fields: dict):
        """Set only the given tags on the game (creating the game if needed).

        Values must be ``str`` (anything else raises TypeError naming the tag).
        Empty, whitespace-only and ``None`` values are skipped: the tag is neither
        created nor changed. Fields therefore cannot be cleared through upsert, so a
        provider that finds no description can never blank an existing one. If
        nothing remains to write and the game already exists, it is returned
        untouched (including its <scrap> element).
        """
        for tag, value in fields.items():
            if value is not None and not isinstance(value, str):
                raise TypeError(f"field {tag!r} must be str, got {type(value).__name__}")
        to_write = {tag: value for tag, value in fields.items() if value is not None and value.strip()}
        g = self.find(rom_path)
        if g is not None and not to_write:
            return g
        created = g is None
        if created:
            g = ET.SubElement(self.root, "game")
            ET.SubElement(g, "path").text = "./" + _norm(rom_path)
        for tag, value in to_write.items():
            el = g.find(tag)
            if el is None:
                el = ET.SubElement(g, tag)
            el.text = value
        scrap = g.find("scrap")
        if scrap is None:
            scrap = ET.SubElement(g, "scrap")
        scrap.attrib.clear()
        scrap.set("name", SCRAPER_NAME)
        scrap.set("date", time.strftime("%Y%m%dT%H%M%S"))
        self._reindent(g, created)
        return g

    def _reindent(self, game, created: bool):
        """Tab indentation like EmulationStation writes it; only for the game we touched."""
        children = list(game)
        game.text = "\n\t\t"
        for i, child in enumerate(children):
            child.tail = "\n\t\t" if i < len(children) - 1 else "\n\t"
        if created:
            siblings = list(self.root)
            if not (self.root.text or "").strip():
                self.root.text = "\n\t"
            if len(siblings) >= 2:
                siblings[-2].tail = "\n\t"
            game.tail = "\n"

    def to_bytes(self) -> bytes:
        """The exact file content ``save()`` writes."""
        return b'<?xml version="1.0"?>\n' + ET.tostring(self.root, encoding="utf-8") + b"\n"

    def save(self):
        data = self.to_bytes()
        # Resolve symlinks so a symlinked gamelist.xml stays a symlink; atomic_replace keeps the file's mode.
        atomic_replace(Path(os.path.realpath(self.path)), lambda f: f.write(data))


def _copy_atomic(src: Path, dest: Path):
    """Copy so that `dest` either does not exist or is complete (random temp file, fsync, rename), then copy times."""
    def fill(fout):
        with open(src, "rb") as fin:
            shutil.copyfileobj(fin, fout)

    atomic_replace(dest, fill)
    try:
        shutil.copystat(src, dest)
    except OSError:
        pass                                              # the copy is complete; only its timestamps differ


def backup_file(path, backup_dir, keep: int = 30) -> Path:
    """Copy gamelist.xml into backup_dir; return the path of the copy created.

    The first backup ever made for a system is kept forever as ``<prefix>original``;
    it is never pruned and does not count toward `keep`. Later backups are named with
    a UTC timestamp and only the newest `keep` of those are retained.
    """
    if keep < 1:
        raise ValueError(f"keep must be >= 1, got {keep}")
    path = Path(path)
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{path.parent.name}-{path.stem}-"
    suffix = path.suffix
    # Exact match so another system's files (e.g. "switch" vs "switch-gamelist") and
    # stray user files in the backup dir are never adopted or pruned.
    name_re = re.compile(re.escape(prefix) + r"(original|\d{8}-\d{6}-\d{6})" + re.escape(suffix))

    def mine():
        return [f for f in backup_dir.iterdir() if f.is_file() and name_re.fullmatch(f.name)]

    if any(f.stem.endswith("-original") for f in mine()):
        t = time.time()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(t)) + f"-{int((t % 1) * 1e6):06d}"
        dest = backup_dir / f"{prefix}{stamp}{suffix}"
    else:
        dest = backup_dir / f"{prefix}original{suffix}"
    _copy_atomic(path, dest)
    timestamped = sorted(f for f in mine() if not f.stem.endswith("-original"))
    for old in timestamped[:-keep]:
        if old != dest:
            old.unlink(missing_ok=True)
    return dest
