"""Discover ROMs of a system and report which assets they lack."""
from __future__ import annotations

import glob
import os
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path

# media folders that never contain ROMs
IGNORED_DIRS = {"images", "videos", "manuals", "media", "downloaded_images", "downloaded_videos",
                "boxart", "snaps", "titles", "wheel", "screenshots", "thumbs"}

DEFAULT_ES_CONFIGS = ["/usr/share/emulationstation/es_systems.cfg"] + \
    sorted(glob.glob("/userdata/system/configs/emulationstation/es_systems*.cfg"))

ASSET_TAGS = ("image", "thumbnail", "marquee", "video")


def rom_extensions(system: str, cfg_paths: Iterable[str] | None = None) -> set[str]:
    """Lowercase ROM extensions (with dot) EmulationStation accepts for ``system``, merged over all configs."""
    exts: set[str] = set()
    for path in (cfg_paths if cfg_paths is not None else DEFAULT_ES_CONFIGS):
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError):
            continue                                 # missing or broken config: use the others
        for s in root.iter("system"):
            if (s.findtext("name") or "").strip() == system:
                exts |= {e.lower() for e in (s.findtext("extension") or "").split()}
    return exts


def _is_dir(path: Path) -> bool:
    """Like Path.is_dir() (follows symlinks) but treats permission errors as 'no'."""
    try:
        return path.is_dir()
    except OSError:
        return False


def _exists(path: Path) -> bool:
    """Like Path.exists() but a broken symlink or permission error means 'no'."""
    try:
        return path.exists()
    except OSError:
        return False


def scan_roms(system_dir: str | os.PathLike, exts: Iterable[str], max_depth: int = 3) -> list[str]:
    """Relative ROM paths like './Sub/B.nsp'. A folder named like a ROM ('Game.wine') is one ROM.

    Default depth 3 so that ./switch/<symlinked share>/<game folder>/<rom> is found on the user's box.
    Symlinked folders are followed, but one that leads back to a folder we are already inside is skipped.
    Hidden entries, media folders and unreadable folders are ignored. Order is deterministic:
    case-insensitive by name within each folder, depth first.
    """
    system_dir = Path(system_dir)
    exts = {e.lower() for e in exts}
    out: list[str] = []

    def walk(directory: Path, depth: int, ancestors: frozenset):
        try:
            entries = sorted(directory.iterdir(), key=lambda p: (p.name.lower(), p.name))
        except OSError:
            return                                   # missing or unreadable folder
        for e in entries:
            if e.name.startswith("."):
                continue
            if e.suffix.lower() in exts:
                if _exists(e):                       # drops dangling symlinks
                    out.append("./" + e.relative_to(system_dir).as_posix())
            elif e.name.lower() not in IGNORED_DIRS and depth < max_depth and _is_dir(e):
                real = os.path.realpath(e)
                if real not in ancestors:            # symlink cycle guard
                    walk(e, depth + 1, ancestors | {real})

    walk(system_dir, 1, frozenset({os.path.realpath(system_dir)}))
    return out


def _has_content(path: str) -> bool:
    """A regular file with at least one byte; an empty file (a failed write) is not an asset."""
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def asset_status(system_dir: str | os.PathLike, gamelist, rom_path: str) -> dict[str, bool]:
    """True per field only if the tag has content AND (for media) the file exists and is not empty."""
    got = gamelist.get(rom_path)
    st = {"desc": bool((got.get("desc") or "").strip())}
    for tag in ASSET_TAGS:
        rel = (got.get(tag) or "").strip()
        st[tag] = bool(rel) and _has_content(os.path.join(str(system_dir), rel[2:] if rel.startswith("./") else rel))
    st["complete"] = all(st[k] for k in ("desc",) + ASSET_TAGS)
    return st
