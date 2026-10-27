#!/usr/bin/env python3
"""Compares the media naming of Scrappy (scrappy.media) with what Batocera's own scraper wrote into gamelist.xml.

Run on the box:   PYTHONPATH=/userdata/system/scrappy python3 tools/verify_media_naming.py
                  ... verify_media_naming.py /userdata/roms/psx          (another system folder)
                  ... verify_media_naming.py --all                       (every system under /userdata/roms that has a gamelist.xml)
                  ... verify_media_naming.py --json                      (machine-readable)

STRICTLY READ-ONLY. Files are only opened for reading and stat()ed; nothing is created, changed, renamed or deleted
(not even Python's bytecode cache: ``sys.dont_write_bytecode`` is set before ``scrappy`` is imported).

For every <game> of the gamelist and each media tag (image, thumbnail, marquee, video) that has a value it checks

* that the file the tag points to exists (and is not empty), and
* how the tag value compares with ``scrappy.media.media_rel_paths(<rom path>)``, in this order:
    exact      same value (a leading ./ and \\ versus / do not count)
    extension  same folder and name, only the file extension differs (Batocera's -image.png versus our -image.jpg)
    suffix     same folder and ROM name, but a different "-role.ext" ending
    folder     same file name (any extension), other folder (for example a mirrored sub-folder images/Sub/...)
    other      anything else (the name is not derived from the ROM name, or the value is not a plain relative path)

"exact + extension + suffix" is called *compatible*: those entries live in the folder where we would put our file and carry
the ROM's name, so the app finds and respects them (a slot that already points to a real file is kept as it is).

It also lists the exact patterns seen, per kind of ROM name (all / in a sub-folder / brackets / dots / non-ASCII), with an
example each. Exit code: 0 analysis done, 1 --fail-under not reached, 2 nothing could be analysed (gamelist missing,
damaged or unreadable).
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import stat
import sys
import xml.etree.ElementTree as ET

sys.dont_write_bytecode = True               # a read-only tool must not even write __pycache__ into the install folder

try:
    from scrappy.media import SUFFIX, media_rel_paths, rom_stem
except ImportError:                          # started as `python3 tools/verify_media_naming.py` without PYTHONPATH
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from scrappy.media import SUFFIX, media_rel_paths, rom_stem

TAGS = tuple(SUFFIX)                         # image, thumbnail, marquee, video
DEFAULT_SYSTEM = "/userdata/roms/switch"
DEFAULT_ROOT = "/userdata/roms"
MAX_GAMELIST_BYTES = 200_000_000
CLASSES = ("exact", "extension", "suffix", "folder", "other")
COMPATIBLE = ("exact", "extension", "suffix")
CATEGORIES = ("all", "sub-folder", "brackets", "dots", "non-ascii")
_EXT = re.compile(r"\.[A-Za-z0-9]{1,5}$")


# ---------------------------------------------------------------- comparing names

def norm(value: str) -> str:
    """A gamelist path without a leading ./ and with / separators."""
    text = str(value).strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def classify(have: str, want: str, stem: str) -> str:
    """How the tag value `have` relates to our value `want` for a ROM whose name stem is `stem`."""
    h, w = norm(have), norm(want)
    if h == w:
        return "exact"
    if os.path.splitext(h)[0] == os.path.splitext(w)[0]:
        return "extension"
    h_dir, h_base = os.path.split(h)
    w_dir, w_base = os.path.split(w)
    if h_dir == w_dir and h_base.startswith(stem):
        return "suffix"
    if os.path.splitext(h_base)[0] == os.path.splitext(w_base)[0]:
        return "folder"
    return "other"


def pattern_of(have: str, rom_path: str) -> str:
    """The value with the ROM's name replaced by {stem} and its sub-folders by {sub}, e.g. ./images/{stem}-image.png"""
    rom = norm(rom_path)
    parts = rom.split("/")
    subdirs, filename = parts[:-1], parts[-1]
    stem = rom_stem(rom_path)
    pieces = norm(have).split("/")
    dirs, base = pieces[:-1], pieces[-1]
    if subdirs and len(dirs) >= len(subdirs) and dirs[-len(subdirs):] == subdirs:
        dirs = dirs[:-len(subdirs)] + ["{sub}"]
    if filename != stem and base.startswith(filename):             # the whole file name, extension included: B.nsp-image.png
        base = "{romfile}" + base[len(filename):]
    elif stem and base.startswith(stem):
        base = "{stem}" + base[len(stem):]
    else:
        base = "{other}"
    return "./" + "/".join(dirs + [base])


def categories_of(rom_path: str) -> list:
    """Which kinds of ROM name this one counts as (always 'all')."""
    stem = rom_stem(rom_path)
    found = ["all"]
    if "/" in norm(rom_path):
        found.append("sub-folder")
    if any(c in stem for c in "[]()"):
        found.append("brackets")
    if "." in stem:
        found.append("dots")
    if not stem.isascii():
        found.append("non-ascii")
    return found


# ---------------------------------------------------------------- reading

def read_gamelist(path: str):
    """-> (root element or None, problem text or ''). Never raises for a bad file."""
    try:
        size = os.stat(path).st_size
    except FileNotFoundError:
        return None, "gamelist.xml not found"
    except OSError as exc:
        return None, f"cannot read gamelist.xml ({exc.strerror or exc})"
    if size == 0:
        return None, "gamelist.xml is empty"
    if size > MAX_GAMELIST_BYTES:
        return None, f"gamelist.xml is unreasonably large ({size} bytes); not read"
    try:
        with open(path, "rb") as f:
            root = ET.parse(f).getroot()
    except ET.ParseError as exc:
        return None, f"gamelist.xml is damaged (not valid XML: {exc})"
    except (OSError, ValueError) as exc:
        return None, f"cannot read gamelist.xml ({getattr(exc, 'strerror', None) or exc})"
    if root.tag != "gameList":
        return None, f"gamelist.xml is not a game list (root element <{root.tag}>)"
    return root, ""


def file_state(system_dir: str, value: str) -> str:
    """'ok', 'missing' or 'empty' for the file a tag value points to (relative to the system folder)."""
    text = value.strip()
    path = text if os.path.isabs(text) else os.path.join(system_dir, norm(text))
    try:
        st = os.stat(path)
    except OSError:
        return "missing"
    if not stat.S_ISREG(st.st_mode):
        return "missing"
    return "ok" if st.st_size > 0 else "empty"


def analyse(system_dir: str, limit: int = 20) -> dict:
    """The report of one system folder as a dict (see the keys at the end)."""
    system_dir = os.path.abspath(system_dir)
    report = {"system": os.path.basename(system_dir.rstrip("/")), "dir": system_dir, "status": "ok", "problem": ""}
    if not os.path.isdir(system_dir):
        report.update(status="not-a-folder", problem=f"{system_dir} is not a folder")
        return report
    root, problem = read_gamelist(os.path.join(system_dir, "gamelist.xml"))
    if root is None:
        report.update(status="missing" if "not found" in problem else "unusable", problem=problem)
        return report

    classes = collections.Counter()
    by_tag = {tag: collections.Counter() for tag in TAGS}
    files = collections.Counter()
    patterns = {cat: collections.defaultdict(lambda: {"count": 0, "example": ""}) for cat in CATEGORIES}
    compat_by_cat = {cat: [0, 0] for cat in CATEGORIES}            # category -> [compatible, total]
    mismatches, missing_files = [], []
    games = games_with_media = no_path = untagged_ours = 0

    for game in root.findall("game"):
        games += 1
        path = (game.findtext("path") or "").strip()
        if not path:
            no_path += 1
            continue
        want = media_rel_paths(path)
        stem = rom_stem(path)
        cats = categories_of(path)
        any_media = False
        for tag in TAGS:
            have = (game.findtext(tag) or "").strip()
            if not have:
                if file_state(system_dir, want[tag]) == "ok":
                    untagged_ours += 1                              # our file exists but the game does not list it
                continue
            any_media = True
            kind = classify(have, want[tag], stem)
            classes[kind] += 1
            by_tag[tag][kind] += 1
            state = file_state(system_dir, have)
            files[state] += 1
            if state != "ok" and len(missing_files) < limit:
                missing_files.append({"rom": path, "tag": tag, "value": have, "file": state})
            pat = pattern_of(have, path)
            for cat in cats:
                slot = patterns[cat][pat]
                slot["count"] += 1
                slot["example"] = slot["example"] or f"{path}  ->  {have}"
                compat_by_cat[cat][1] += 1
                compat_by_cat[cat][0] += kind in COMPATIBLE
            if kind != "exact" and len(mismatches) < limit:
                mismatches.append({"rom": path, "tag": tag, "have": have, "ours": want[tag], "class": kind})
        games_with_media += any_media

    total = sum(classes.values())

    def pct(n, d=None):
        d = total if d is None else d
        return round(100.0 * n / d, 1) if d else None

    report.update({
        "games": games, "games_without_path": no_path, "games_with_media": games_with_media, "entries": total,
        "classes": {c: classes.get(c, 0) for c in CLASSES},
        "exact_pct": pct(classes.get("exact", 0)),
        "compatible_pct": pct(sum(classes.get(c, 0) for c in COMPATIBLE)),
        "by_tag": {tag: {c: by_tag[tag].get(c, 0) for c in CLASSES} for tag in TAGS},
        "files": {"ok": files.get("ok", 0), "missing": files.get("missing", 0), "empty": files.get("empty", 0)},
        "missing_files": missing_files,
        "untagged_files_we_would_use": untagged_ours,
        "patterns": {cat: [{"pattern": p, **v} for p, v in sorted(patterns[cat].items(), key=lambda kv: -kv[1]["count"])]
                     for cat in CATEGORIES},
        "category_compatible": {cat: {"compatible": c, "entries": t, "pct": pct(c, t)} for cat, (c, t) in compat_by_cat.items()},
        "mismatches": mismatches,
    })
    report["conclusions"] = conclusions(report)
    return report


def conclusions(r: dict) -> list:
    """Plain-language verdicts derived from the numbers."""
    out = []
    if not r["entries"]:
        return ["No game of this gamelist has an image/thumbnail/marquee/video tag yet, so there is nothing to compare."]
    if r["classes"]["exact"] == r["entries"]:
        out.append("Our naming is IDENTICAL to what the scraper used for every entry.")
    elif r["compatible_pct"] == 100:
        out.append("Same folders and ROM names everywhere; only extensions or the -role suffix differ. That is harmless: the "
                   "app keeps a slot that already points to a real file and only writes its own name where the slot is empty.")
    else:
        other = r["classes"]["folder"] + r["classes"]["other"]
        out.append(f"{other} of {r['entries']} entries use a different folder or name than ours (see the patterns below). Old entries are "
                   "harmless (the tag is what the theme follows). Change scrappy/media.py only if the theme fails to show a NEW entry "
                   "for such a ROM; then copy the pattern observed for it.")
    for cat, label in (("sub-folder", "ROMs in sub-folders"), ("brackets", "names with brackets"), ("dots", "names with dots"),
                       ("non-ascii", "non-ASCII names")):
        info = r["category_compatible"][cat]
        if not info["entries"]:
            out.append(f"No {label} have media yet: nothing observed for them.")
        elif info["compatible"] == info["entries"]:
            out.append(f"{label[0].upper() + label[1:]}: all {info['entries']} entries follow our folder and name rule.")
        else:
            top = r["patterns"][cat][0]["pattern"]
            out.append(f"{label[0].upper() + label[1:]}: {info['compatible']} of {info['entries']} follow our rule; most common observed pattern: {top}")
    if r["files"]["missing"] or r["files"]["empty"]:
        out.append(f"{r['files']['missing']} tag(s) point to a file that does not exist and {r['files']['empty']} to an empty file "
                   "(EmulationStation shows nothing there; the app treats an empty file as missing).")
    return out


# ---------------------------------------------------------------- output

def render(r: dict, limit: int = 20, pattern_limit: int = 6) -> str:
    lines = [f"== {r['system']}  ({r['dir']})"]
    if r["status"] != "ok":
        lines.append(f"   cannot check: {r['problem']}")
        return "\n".join(lines)
    n = r["entries"]
    lines.append(f"   {r['games']} games ({r['games_with_media']} with media), {n} media entries (image/thumbnail/marquee/video)")
    if r["games_without_path"]:
        lines.append(f"   {r['games_without_path']} <game> element(s) without a <path> were skipped")
    if n:
        c = r["classes"]
        lines.append(f"   exact match with scrappy.media ...... {c['exact']:>6}  ({r['exact_pct']}%)")
        lines.append(f"   only the extension differs ...... {c['extension']:>6}")
        lines.append(f"   same name, other -role suffix ... {c['suffix']:>6}")
        lines.append(f"   same file name, other folder .... {c['folder']:>6}")
        lines.append(f"   other ........................... {c['other']:>6}")
        lines.append(f"   compatible (exact+ext+suffix) ... {c['exact'] + c['extension'] + c['suffix']:>6}  ({r['compatible_pct']}%)")
        f = r["files"]
        lines.append(f"   files: {f['ok']} present, {f['missing']} missing, {f['empty']} empty; "
                     f"{r['untagged_files_we_would_use']} file(s) with our name exist but are not listed in the gamelist")
        lines.append("   per tag (exact/extension/suffix/folder/other): " + "; ".join(
            f"{t} {'/'.join(str(r['by_tag'][t][k]) for k in CLASSES)}" for t in TAGS))
        for cat in CATEGORIES:
            pats = r["patterns"][cat]
            if not pats:
                continue
            info = r["category_compatible"][cat]
            lines.append(f"   patterns, {cat} ({info['entries']} entries, {info['pct']}% compatible):")
            for p in pats[:pattern_limit]:
                lines.append(f"      {p['count']:>5}  {p['pattern']}")
                lines.append(f"             e.g. {p['example']}")
            if len(pats) > pattern_limit:
                lines.append(f"      ... and {len(pats) - pattern_limit} more pattern(s) (see --json)")
    lines.append("   conclusion:")
    lines += [f"     - {text}" for text in r["conclusions"]]
    if r["mismatches"]:
        lines.append(f"   differences from our naming (first {min(limit, len(r['mismatches']))}):")
        for m in r["mismatches"][:limit]:
            lines.append(f"     [{m['class']}] {m['rom']} {m['tag']}: has {m['have']}  ours {m['ours']}")
    if r["missing_files"]:
        lines.append(f"   tags pointing to missing/empty files (first {min(limit, len(r['missing_files']))}):")
        for m in r["missing_files"][:limit]:
            lines.append(f"     [{m['file']}] {m['rom']} {m['tag']}: {m['value']}")
    return "\n".join(lines)


def system_folders(args) -> list:
    if args.all:
        found = sorted(os.path.dirname(p) for p in glob.glob(os.path.join(args.roms_root, "*", "gamelist.xml")))
        return found + list(args.systems)
    return list(args.systems) or [DEFAULT_SYSTEM]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only check of scrappy.media naming against the real gamelist.xml.")
    parser.add_argument("systems", nargs="*", help=f"system folder(s) (default {DEFAULT_SYSTEM})")
    parser.add_argument("--all", action="store_true", help="check every folder under --roms-root that has a gamelist.xml")
    parser.add_argument("--roms-root", default=DEFAULT_ROOT)
    parser.add_argument("--json", action="store_true", help="print one JSON document instead of text")
    parser.add_argument("--limit", type=int, default=20, help="how many differences and missing files to list (default 20)")
    parser.add_argument("--fail-under", type=float, default=None, metavar="PCT",
                        help="exit 1 when the exact-match percentage of any checked system is below PCT")
    args = parser.parse_args(argv)

    reports = [analyse(folder, args.limit) for folder in system_folders(args)]
    analysed = [r for r in reports if r["status"] == "ok"]
    code = 0 if analysed else 2
    if args.fail_under is not None and any((r["exact_pct"] or 0.0) < args.fail_under for r in analysed if r["entries"]):
        code = max(code, 1)
    if args.json:
        json.dump({"read_only": True, "systems": reports, "exit_code": code}, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        print("\n\n".join(render(r, args.limit) for r in reports))
    return code


if __name__ == "__main__":
    sys.exit(main())
