"""Settings with sensible defaults for the Batocera box.

Where the settings live: ``<data folder>/config.json`` (default ``/userdata/system/scrappy/config.json``).
The file is optional and hand-editable. Loading never fails: a missing, unreadable, oversized or corrupt file gives the
defaults, and a single bad value falls back to that key's default; either way the reasons are in ``Config.warnings``
for the UI to show. Keys this version does not know are kept untouched (a newer or older version may use them).
Loading never writes. The program itself writes one setting, ``intro_seen`` (the window has shown its one-time tip about the
Tools menu), through ``save_settings`` and only when the file loaded without a problem. ``save_settings`` merges changes into the file atomically (``scrappy.atomic``) and keeps a corrupt
file as ``config.json.corrupt`` instead of silently overwriting it.

SECRETS DO NOT BELONG IN config.json. Nothing in this program needs a secret today (Steam and YouTube are read without
keys). If a future source needs an API key, store it in a separate ``<data folder>/secrets.json`` written with
``atomic_replace(..., mode=0o600)`` so only that user can read it (chmod 0600), or read it from an environment
variable; never add it to ``DEFAULTS``, to this file, to logs, or to the Config object that is passed around. A key
name in config.json that looks like a secret (key, token, password, ...) is NOT loaded into the Config object (so it
cannot leak through logs or the UI); it stays in the file untouched and is reported in ``warnings`` (without its value).
``save_settings`` refuses to write such a key. ``roms_root``, ``data_dir`` and ``dry_run_dir`` must be absolute paths: a
relative or ``~`` path would depend on where the program was started, so it is reported and the default is used.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
from pathlib import Path

from .atomic import atomic_replace
from .providers.base import ProviderError

log = logging.getLogger(__name__)

DEFAULTS = {
    "roms_root": "/userdata/roms",
    "data_dir": "/userdata/system/scrappy",
    "max_video_seconds": 120,        # longest clip that is ever encoded
    "default_video_seconds": 90,     # clip length the video panel starts with (never above the maximum)
    "video_bitrate": "1500k",        # ffmpeg -b:v
    "search_timeout": 20,            # seconds to wait for the game sources
    "youtube_search_timeout": 45,    # seconds to wait for the YouTube search (yt-dlp starts slowly on the box)
    "dry_run_dir": None,             # set to a folder to make saves go to a sandbox copy instead of the real library
    "intro_seen": False,             # true once the window has shown its one-time tip about the Tools menu (the only setting the program writes)
}

MAX_CONFIG_BYTES = 1_000_000
_SECRET_NAME = re.compile(r"key|token|secret|passw|credential", re.IGNORECASE)
_BITRATE = re.compile(r"[1-9][0-9]{0,4}[kKmM]?", re.ASCII)


class ConfigError(ProviderError, ValueError):
    """A setting or folder name that cannot be used; the message is plain language (a ValueError too, for older callers)."""


class Config(dict):
    """The settings (a plain dict for callers) plus what went wrong while loading them."""

    def __init__(self, *args, path=None, warnings=None, **kw):
        super().__init__(*args, **kw)
        self.path = Path(path) if path is not None else None
        self.warnings = warnings if warnings is not None else []


# ---------------------------------------------------------------- validation

def _number(lo, hi):
    def check(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
            raise ValueError(f"must be a number from {lo:g} to {hi:g}")
        return v
    return check


def _text(v):
    if not isinstance(v, str) or not v.strip() or "\x00" in v:
        raise ValueError("must be a non-empty text without NUL characters")
    return v


def _folder(v):
    _text(v)
    if not os.path.isabs(v):
        raise ValueError("must be a full path starting with /, not a relative or ~ path")
    return v


def _bitrate(v):
    if not isinstance(v, str) or not _BITRATE.fullmatch(v):
        raise ValueError("must look like 1500k or 2M")
    return v


def _flag(v):
    if not isinstance(v, bool):
        raise ValueError("must be true or false")
    return v


def _folder_or_none(v):
    return None if v is None else _folder(v)


VALIDATORS = {
    "roms_root": _folder,
    "data_dir": _folder,
    "max_video_seconds": _number(1, 600),
    "default_video_seconds": _number(1, 600),
    "video_bitrate": _bitrate,
    "search_timeout": _number(0.1, 300),
    "youtube_search_timeout": _number(0.1, 600),
    "dry_run_dir": _folder_or_none,
    "intro_seen": _flag,
}


def _checked(key, value):
    return VALIDATORS[key](value) if key in VALIDATORS else value


# ---------------------------------------------------------------- loading

def config_path(data_folder=None) -> Path:
    return Path(data_folder or DEFAULTS["data_dir"]) / "config.json"


def _read_file(path: Path, warnings: list) -> dict:
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        warnings.append(f"Could not read {path} ({exc.strerror or exc}); using the default settings.")
        return {}
    if len(raw) > MAX_CONFIG_BYTES:
        warnings.append(f"{path} is too large to be a settings file; using the default settings.")
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as exc:                 # JSONDecodeError and UnicodeDecodeError are ValueErrors
        warnings.append(f"{path} is not valid JSON ({type(exc).__name__}); using the default settings.")
        return {}
    if not isinstance(data, dict):
        warnings.append(f"{path} must hold a JSON object; using the default settings.")
        return {}
    return data


def load_config(path=None, overrides=None) -> Config:
    """Defaults, then the file, then `overrides` (code and tests; these must be valid or ValueError is raised).

    Without `path` the file is ``config.json`` in the default data folder, or in ``overrides["data_dir"]`` if given,
    so a test that redirects the data folder never reads the real settings.
    """
    overrides = dict(overrides or {})
    cfg_path = Path(path) if path else config_path(overrides.get("data_dir"))
    warnings: list = []
    cfg = Config(DEFAULTS, path=cfg_path, warnings=warnings)
    for key, value in _read_file(cfg_path, warnings).items():
        if _SECRET_NAME.search(str(key)):
            warnings.append(f"'{key}' in {cfg_path} looks like a secret, so it was not loaded; "
                            "keep secrets out of config.json (see scrappy/config.py).")
            continue
        try:
            cfg[key] = _checked(key, value)
        except ValueError as exc:
            warnings.append(f"Setting '{key}' {exc}; using {DEFAULTS[key]!r}.")
    cfg["default_video_seconds"] = min(cfg["default_video_seconds"], cfg["max_video_seconds"])
    for key, value in overrides.items():
        try:
            cfg[key] = _checked(key, value)
        except ValueError as exc:
            raise ConfigError(f"override '{key}' {exc}") from None
    cfg["default_video_seconds"] = min(cfg["default_video_seconds"], cfg["max_video_seconds"])
    for w in warnings:
        log.warning(w)
    return cfg


# ---------------------------------------------------------------- saving

def save_settings(updates: dict, path=None) -> Path:
    """Merge `updates` into the settings file (a value of None removes the key) and write it atomically.

    Only the given keys change: everything else in the file, including keys this version does not know, stays as it
    is, and defaults / test overrides are never written. All values are validated first; nothing is written if one is
    bad (ConfigError, also a ValueError). A corrupt existing file is copied to ``<name>.corrupt`` before it is replaced.
    """
    target = Path(path) if path else config_path()
    if not isinstance(updates, dict):
        raise ConfigError("The settings to save must be a set of name/value pairs.")
    clean = {}
    for key, value in updates.items():
        if not isinstance(key, str) or not key:
            raise ConfigError(f"A setting name must be text, not {key!r}.")
        if _SECRET_NAME.search(key):
            raise ConfigError(f"'{key}' looks like a secret and is not stored in config.json (see scrappy/config.py).")
        try:
            clean[key] = None if value is None else _checked(key, value)
        except ValueError as exc:
            raise ConfigError(f"setting '{key}' {exc}") from None
    try:
        problems: list = []
        current = _read_file(target, problems)
        for key, value in clean.items():
            if value is None:
                current.pop(key, None)
            else:
                current[key] = value
        try:
            text = (json.dumps(current, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            raise ConfigError("Some setting value cannot be stored in the settings file (only text, numbers, "
                              "true/false, lists and objects can).") from None
        if problems and target.is_file():
            shutil.copyfile(target, target.with_name(target.name + ".corrupt"))
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_replace(target, lambda f: f.write(text))
    except OSError as exc:
        raise ProviderError(f"Could not save the settings to {target}: {exc.strerror or exc}.") from exc
    return target


def data_dir(cfg: dict, name: str) -> Path:
    """The sub-folder `name` (plain name, no separators) of the data folder, created if needed."""
    if not isinstance(name, str) or name in ("", ".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise ConfigError(f"{name!r} is not a plain folder name.")
    p = Path(cfg["data_dir"]) / name
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProviderError(f"Cannot create the work folder {p}: {exc.strerror or exc}. "
                            "Check that the disk is writable and not full.") from exc
    return p
