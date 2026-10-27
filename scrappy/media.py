"""Where Batocera expects a game's media files."""
import os
import re

_EXT = re.compile(r"\.[A-Za-z0-9]{1,6}")

# gamelist tag -> (folder, file suffix)
SUFFIX = {
    "image": ("images", "-image.jpg"),
    "thumbnail": ("images", "-thumb.png"),
    "marquee": ("images", "-marquee.png"),
    "video": ("videos", "-video.mp4"),
}


def rom_stem(rom_path: str) -> str:
    name = os.path.basename(str(rom_path).rstrip("/"))
    root, ext = os.path.splitext(name)
    return root if ext and _EXT.fullmatch(ext) else name


def media_rel_paths(rom_path: str) -> dict:
    """Values to put into gamelist.xml, e.g. ./images/<stem>-image.jpg"""
    stem = rom_stem(rom_path)
    return {tag: f"./{folder}/{stem}{suffix}" for tag, (folder, suffix) in SUFFIX.items()}


def media_abs_paths(system_dir: str, rom_path: str) -> dict:
    return {tag: os.path.join(str(system_dir), rel[2:]) for tag, rel in media_rel_paths(rom_path).items()}
