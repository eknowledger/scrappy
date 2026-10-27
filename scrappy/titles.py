"""Turn ROM file names into clean game titles."""
import os
import re

# words that are never part of a title when they trail the name
TAG_WORDS = {"base", "update", "upd", "dlc", "patched", "repack", "proper", "eshop", "multi", "nsp", "xci", "nsz", "xcz"}

# file extensions that are stripped from the end of a name (compared lowercase, without the dot)
ROM_EXTENSIONS = frozenset(
    "nsp xci nsz xcz zip 7z rar iso bin cue chd cso pbp rom sfc smc nes fds gba gbc gb n64 z64 v64 "
    "md gen smd sms gg pce a26 a52 a78 lnx wad wbfs rvz gcz wua wux wup exe lnk sh pc wine wsquashfs "
    "squashfs m3u elf pkg vpk d64 t64 adf st dsk img mdf nrg gdi cdi 3ds cia nds dsi nro".split()
)


def clean_title(filename: str) -> str:
    """'Tidepool Tactics [0100...][v0] [example.com].nsp' -> 'Tidepool Tactics'."""
    name = os.path.basename(str(filename).rstrip("/"))
    root, ext = os.path.splitext(name)
    if ext[1:].lower() in ROM_EXTENSIONS:
        name = root
    stem = name.strip()
    name = name.replace("꞉", ":").replace("：", ":")
    name = re.sub(r"\[[^\]]*\]", " ", name)          # [0100...] [v0] [Base] [example.com]
    name = re.sub(r"\([^)]*\)", " ", name)           # (example.com) (US) (v1.0 UPD)
    name = name.replace("_", " ").replace("+", " ")
    # a trailing bare 'vN' (e.g. 'Game v2') is stripped on purpose
    name = re.sub(r"(?<![\w.])v\d+(?:\.\d+)*(?![\w])", " ", name, flags=re.I)   # v1.3.0 v65536
    words = name.split()
    while words and words[-1].lower().strip(".-") in TAG_WORDS:
        words.pop()
    name = " ".join(words)
    name = re.sub(r"\s+[-–—:]\s*$", "", name)  # dangling separator
    return name.strip(" -_") or stem  # tag-only names such as '[Base].nsp' fall back to the stem


def title_id(filename: str):
    """Switch title id in the file name, e.g. 0100AAAA00000000, or None."""
    m = re.search(r"\[([0-9A-Fa-f]{16})\]", filename)
    return m.group(1).upper() if m else None
