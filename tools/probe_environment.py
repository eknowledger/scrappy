#!/usr/bin/env python3
"""Checks, on the Batocera, the facts the Scrappy design assumed. Standard library only.

Run on the box:   python3 tools/probe_environment.py            (readable report)
                  python3 tools/probe_environment.py --json     (the same, as JSON)

Every line is labelled OK, WARN or FAIL:
  OK    as assumed.
  WARN  works, but something is missing or different (the detail says what the program does about it).
  FAIL  the program cannot work until this is fixed.

The exit code is ALWAYS 0 (a probe that fails would hide its own report from `ssh ... && ...` chains); read the labels.

Strictly read-only. The only network traffic is two GET requests to EmulationStation's local web API on
127.0.0.1:1234: ``/`` and ``/runningGame``. ``/reloadgames`` and every other endpoint can change state and are
refused by ``es_get`` below, whatever the caller asks for.
"""
from __future__ import annotations

import argparse
import glob
import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

OK, WARN, FAIL = "OK", "WARN", "FAIL"

MIN_FREE_MB_FAIL = 200            # the installer refuses below this
MIN_FREE_MB_WARN = 1000
ES_URL = "http://127.0.0.1:1234"
ES_ALLOWED_PATHS = ("/", "/runningGame")     # read-only endpoints; nothing else is ever requested
SUBPROCESS_TIMEOUT = 20
YTDLP_TIMEOUT = 40                # a zipapp starts slowly on the box
DEFAULT_USERDATA = "/userdata"
SYSTEM_ES_CONFIG = "/usr/share/emulationstation/es_systems.cfg"

# (module, label, required?)  required -> FAIL when missing, otherwise WARN
MODULES = [
    ("PyQt5.QtCore", "PyQt5 core", True),
    ("PyQt5.QtGui", "PyQt5 gui", True),
    ("PyQt5.QtWidgets", "PyQt5 widgets", True),
    ("PyQt5.QtMultimedia", "PyQt5 QtMultimedia", False),
    ("PIL", "Pillow", True),
    ("numpy", "numpy", True),
    ("requests", "requests", True),
    ("urllib3", "urllib3", True),
    ("cv2", "OpenCV (cv2)", False),
]


class EsRefused(ValueError):
    """Raised when something asks the EmulationStation web API for a path that is not on the read-only list."""


# ---------------------------------------------------------------- gathering facts (all read-only)

def _run(cmd, timeout=SUBPROCESS_TIMEOUT):
    """-> (returncode, stdout, stderr); (None, '', reason) when the program is missing or times out."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
    except FileNotFoundError:
        return None, "", "not found"
    except subprocess.TimeoutExpired:
        return None, "", f"timed out after {timeout} s"
    except OSError as exc:
        return None, "", str(exc)
    return p.returncode, p.stdout, p.stderr


def module_facts(modules=MODULES):
    out = {}
    for name, _label, _required in modules:
        try:
            mod = importlib.import_module(name)
        except Exception as exc:                      # ImportError, but a broken library can raise anything
            out[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
            continue
        version = getattr(mod, "__version__", None) or getattr(mod, "VERSION", None)
        if name == "PyQt5.QtCore":
            version = f"PyQt {getattr(mod, 'PYQT_VERSION_STR', '?')}, Qt {getattr(mod, 'QT_VERSION_STR', '?')}"
        out[name] = {"ok": True, "version": str(version) if version is not None else None}
    return out


# Run in a child process so that "the system Pillow" and "the vendored Pillow" can be judged separately, and so that a
# crash inside a broken libjpeg cannot take the probe down with it.
PILLOW_CHECK_CODE = r"""
import io, json
out = {"jpeg": False, "png": False, "webp": False}
try:
    import PIL
    from PIL import Image, features
    out["version"] = PIL.__version__
    out["file"] = PIL.__file__
except Exception as exc:
    out["error"] = "%s: %s" % (type(exc).__name__, exc)
    print(json.dumps(out))
    raise SystemExit(0)

def picture():
    im = Image.new("RGB", (64, 48), (200, 30, 30))
    im.paste((30, 30, 200), (32, 0, 64, 48))
    return im

def near(a, b, tol):
    return all(abs(x - y) <= tol for x, y in zip(a, b))

try:
    buf = io.BytesIO()
    picture().save(buf, "JPEG", quality=95)
    back = Image.open(io.BytesIO(buf.getvalue()))
    back.load()
    assert back.size == (64, 48), back.size
    assert near(back.getpixel((8, 24)), (200, 30, 30), 40) and near(back.getpixel((56, 24)), (30, 30, 200), 40), "pixels differ"
    out["jpeg"] = True
except Exception as exc:
    out["jpeg_error"] = "%s: %s" % (type(exc).__name__, exc)
try:
    buf = io.BytesIO()
    picture().save(buf, "PNG")
    back = Image.open(io.BytesIO(buf.getvalue()))
    back.load()
    assert back.getpixel((8, 24)) == (200, 30, 30) and back.getpixel((56, 24)) == (30, 30, 200)
    out["png"] = True
except Exception as exc:
    out["png_error"] = "%s: %s" % (type(exc).__name__, exc)
try:
    out["webp"] = bool(features.check("webp"))
except Exception:
    out["webp"] = False
print(json.dumps(out))
"""


def pillow_check(pythonpath=None, isolated=False, run=None, timeout=60):
    """Round-trips a picture through Pillow in a child Python.

    pythonpath=None, isolated=True  -> the system Pillow only (python -I ignores PYTHONPATH and user folders)
    pythonpath='/x/vendor'          -> the Pillow found first in that folder
    -> {'version', 'file', 'jpeg', 'png', 'webp', optional 'error' / 'jpeg_error' / 'png_error'}
    """
    cmd = [sys.executable] + (["-I"] if isolated else []) + ["-c", PILLOW_CHECK_CODE]
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    try:
        p = (run or subprocess.run)(cmd, capture_output=True, text=True, timeout=timeout, errors="replace", env=env, cwd="/")
    except subprocess.TimeoutExpired:
        return {"jpeg": False, "png": False, "webp": False, "error": f"timed out after {timeout} s"}
    except OSError as exc:
        return {"jpeg": False, "png": False, "webp": False, "error": str(exc)}
    lines = [l for l in (p.stdout or "").splitlines() if l.startswith("{")]
    if not lines:
        tail = ((p.stderr or "").strip().splitlines() or ["no output"])[-1][:200]
        return {"jpeg": False, "png": False, "webp": False, "error": f"the check crashed (exit {p.returncode}): {tail}"}
    try:
        return json.loads(lines[-1])
    except ValueError:
        return {"jpeg": False, "png": False, "webp": False, "error": "unreadable answer from the check"}


def pillow_facts(data_dir):
    vendor = os.path.join(data_dir, "vendor") if data_dir else None
    facts = {"vendor_dir": vendor, "system": pillow_check(isolated=True), "vendored": None}
    if vendor and os.path.isdir(os.path.join(vendor, "PIL")):
        facts["vendored"] = pillow_check(pythonpath=vendor)
        file = facts["vendored"].get("file")
        facts["vendored"]["under_vendor"] = bool(
            file and os.path.realpath(file).startswith(os.path.realpath(vendor) + os.sep))
    return facts


def ffmpeg_facts(run=_run):
    facts = {}
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        rc, out, err = run([tool, "-hide_banner", "-version"]) if path else (None, "", "not found")
        first = out.splitlines()[0] if rc == 0 and out else None
        facts[tool] = {"path": path, "version": first, "error": None if first else (err or "no output")}
    rc, out, _ = run(["ffmpeg", "-hide_banner", "-encoders"]) if facts["ffmpeg"]["path"] else (None, "", "")
    enc = out.split() if rc == 0 else []
    facts["libx264"] = "libx264" in enc
    facts["aac_encoder"] = "aac" in enc
    rc, out, _ = run(["ffmpeg", "-hide_banner", "-decoders"]) if facts["ffmpeg"]["path"] else (None, "", "")
    dec = out.split() if rc == 0 else []
    facts["av1_decoders"] = sorted(d for d in ("libdav1d", "libaom-av1", "av1", "av1_cuvid", "av1_qsv") if d in dec)
    facts["h264_decoder"] = "h264" in dec
    return facts


def es_get(path, timeout=3, opener=None):
    """GET one of the read-only EmulationStation endpoints -> {'status': int, 'body': str} or {'error': str}."""
    if path not in ES_ALLOWED_PATHS:
        raise EsRefused(f"refusing to call {path!r}: only {', '.join(ES_ALLOWED_PATHS)} are allowed")
    req = urllib.request.Request(ES_URL + path, method="GET")
    try:
        with (opener or urllib.request.urlopen)(req, timeout=timeout) as resp:
            body = resp.read(2000).decode("utf-8", "replace")
            return {"status": resp.status, "body": body}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "body": ""}
    except Exception as exc:                          # URLError, timeout, reset ...
        return {"error": f"{type(exc).__name__}: {exc}"[:200]}


def disk_facts(userdata=DEFAULT_USERDATA, data_dir=None):
    out = {}
    for key, path in (("userdata", userdata), ("data_dir", data_dir)):
        if path is None:
            continue
        probe = path
        while probe and not os.path.exists(probe) and probe != os.path.dirname(probe):
            probe = os.path.dirname(probe)            # the data folder may not exist yet: measure its parent
        try:
            du = shutil.disk_usage(probe)
            out[key] = {"path": path, "measured_at": probe, "free_mb": du.free // (1024 * 1024),
                        "total_mb": du.total // (1024 * 1024)}
        except OSError as exc:
            out[key] = {"path": path, "error": str(exc)}
    return out


def _system_entry(cfg_path, name):
    # The es_systems files are the box's own trusted configuration, hence the plain standard-library XML parser.
    try:
        root = ET.parse(cfg_path).getroot()
    except (OSError, ET.ParseError):
        return None
    for s in root.iter("system"):
        if (s.findtext("name") or "").strip() == name:
            return {"path": (s.findtext("path") or "").strip(), "extensions": (s.findtext("extension") or "").split()[:12]}
    return None


def es_config_facts(userdata=DEFAULT_USERDATA, system_cfg=SYSTEM_ES_CONFIG):
    cfgs = [system_cfg] + sorted(glob.glob(os.path.join(userdata, "system/configs/emulationstation/es_systems*.cfg")))
    found = []
    switch = None
    for p in cfgs:
        exists = os.path.isfile(p)
        found.append({"path": p, "exists": exists})
        if exists:
            switch = _system_entry(p, "switch") or switch   # later (user) configs override earlier ones
    return {"configs": found, "switch_system": switch}


def _count(path):
    try:
        names = os.listdir(path)
    except OSError:
        return None
    return len(names)


def switch_roms_facts(userdata=DEFAULT_USERDATA):
    folder = os.path.join(userdata, "roms", "switch")
    facts = {"path": folder, "exists": os.path.isdir(folder)}
    if facts["exists"]:
        facts["realpath"] = os.path.realpath(folder)
        facts["is_symlink"] = os.path.islink(folder)
        facts["gamelist"] = os.path.isfile(os.path.join(folder, "gamelist.xml"))
        facts["images"] = _count(os.path.join(folder, "images"))
        facts["videos"] = _count(os.path.join(folder, "videos"))
        facts["writable"] = os.access(folder, os.W_OK)
    return facts


def display_facts(env=None):
    env = os.environ if env is None else env
    display = env.get("DISPLAY")
    target = display or ":0.0"
    num = target.split(":")[-1].split(".")[0] if ":" in target else "0"
    sock_path = f"/tmp/.X11-unix/X{num}" if (target.startswith(":") or target.startswith("unix:")) else None
    facts = {"DISPLAY": display, "assumed": target, "socket": sock_path, "socket_exists": False, "connectable": False}
    if sock_path and os.path.exists(sock_path):
        facts["socket_exists"] = True
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        try:
            s.connect(sock_path)
            facts["connectable"] = True
        except OSError as exc:
            facts["connect_error"] = str(exc)
        finally:
            s.close()
    facts["QT_QPA_PLATFORM"] = env.get("QT_QPA_PLATFORM")
    return facts


def ytdlp_facts(data_dir, run=_run):
    local = os.path.join(data_dir, "bin", "yt-dlp") if data_dir else None
    on_path = shutil.which("yt-dlp")
    facts = {"local_path": local, "local_exists": bool(local and os.path.isfile(local)), "on_path": on_path, "version": None}
    exe = local if facts["local_exists"] else on_path
    if exe:
        rc, out, err = run([sys.executable, exe, "--version"] if exe == local else [exe, "--version"], timeout=YTDLP_TIMEOUT)
        facts["version"] = out.strip().splitlines()[0] if rc == 0 and out.strip() else None
        facts["error"] = None if facts["version"] else (err.strip()[-160:] or "no output")
    return facts


def data_dir_facts(data_dir):
    facts = {"path": data_dir, "exists": os.path.isdir(data_dir)}
    for sub in ("scrappy", "bin", "cache", "backups", "journal"):
        facts[sub] = os.path.isdir(os.path.join(data_dir, sub))
    cfg = os.path.join(data_dir, "config.json")
    facts["config_json"] = "absent"
    if os.path.isfile(cfg):
        try:
            with open(cfg, encoding="utf-8") as f:
                json.load(f)
            facts["config_json"] = "valid"
        except (OSError, ValueError):
            facts["config_json"] = "unreadable"
    return facts


def gather_facts(userdata=DEFAULT_USERDATA, data_dir=None, system_cfg=SYSTEM_ES_CONFIG):
    data_dir = data_dir or os.path.join(userdata, "system", "scrappy")
    return {
        "python": {"version": list(sys.version_info[:3]), "executable": sys.executable, "implementation": sys.implementation.name},
        "modules": module_facts(),
        "ffmpeg": ffmpeg_facts(),
        "pillow": pillow_facts(data_dir),
        "es_api": {"root": es_get("/"), "running_game": es_get("/runningGame")},
        "disk": disk_facts(userdata, data_dir),
        "es_config": es_config_facts(userdata, system_cfg),
        "switch_roms": switch_roms_facts(userdata),
        "display": display_facts(),
        "ytdlp": ytdlp_facts(data_dir),
        "data_dir": data_dir_facts(data_dir),
    }


# ---------------------------------------------------------------- judging the facts (pure: facts in, results out)

def _r(name, status, detail):
    return {"name": name, "status": status, "detail": detail}


def _version_tuple(text):
    parts = []
    for p in str(text or "").replace("-", ".").split("."):
        if not p.isdigit():
            break
        parts.append(int(p))
    return tuple(parts)


def evaluate(facts):
    """Facts -> list of {'name', 'status', 'detail'}. Never raises on missing keys: a missing fact is a WARN."""
    res = []
    g = facts.get

    py = (g("python") or {}).get("version") or []
    if not py:
        res.append(_r("python", WARN, "version unknown"))
    else:
        v = ".".join(str(x) for x in py)
        if tuple(py[:2]) < (3, 9):
            res.append(_r("python", FAIL, f"{v}: the program needs Python 3.9 or newer (the design assumed 3.11)"))
        elif tuple(py[:2]) < (3, 11):
            res.append(_r("python", WARN, f"{v}: newer than the minimum, but the design assumed 3.11"))
        else:
            res.append(_r("python", OK, v))

    mods = g("modules") or {}
    for name, label, required in MODULES:
        m = mods.get(name)
        if m is None:
            res.append(_r(label, WARN, "not checked"))
        elif m.get("ok"):
            res.append(_r(label, OK, m.get("version") or "present"))
        elif required:
            res.append(_r(label, FAIL, f"missing ({m.get('error', '?')})"))
        elif name == "PyQt5.QtMultimedia":
            res.append(_r(label, WARN, "missing: the in-app video preview needs it; the program still runs"))
        else:
            res.append(_r(label, WARN, "missing (optional)"))
    u3 = mods.get("urllib3") or {}
    if u3.get("ok") and u3.get("version") and _version_tuple(u3["version"]) and _version_tuple(u3["version"]) < (2,):
        res.append(_r("urllib3 version", WARN, f"{u3['version']} is older than 2.x: the downloader is written for it, but note it"))

    ff = g("ffmpeg") or {}
    for tool in ("ffmpeg", "ffprobe"):
        t = ff.get(tool) or {}
        if t.get("version"):
            res.append(_r(tool, OK, f"{t['version']} ({t.get('path')})"))
        else:
            res.append(_r(tool, FAIL, f"not usable: {t.get('error', 'unknown')}"))
    if (ff.get("ffmpeg") or {}).get("version"):
        res.append(_r("ffmpeg libx264 encoder", OK if ff.get("libx264") else FAIL,
                      "available" if ff.get("libx264") else "missing: scrappy/processing/video.py must use another H.264 encoder"))
        res.append(_r("ffmpeg aac encoder", OK if ff.get("aac_encoder") else FAIL,
                      "available" if ff.get("aac_encoder") else "missing: trailers would have no sound"))
        av1 = ff.get("av1_decoders") or []
        res.append(_r("ffmpeg AV1 decoder", OK if av1 else WARN,
                      ", ".join(av1) if av1 else "none: YouTube trailers that only come as AV1 cannot be cut (H.264/VP9 are fine)"))

    res.extend(_pillow_results(g("pillow")))

    es = g("es_api") or {}
    for key, path in (("root", "GET /"), ("running_game", "GET /runningGame")):
        e = es.get(key) or {}
        if "status" in e and 200 <= e["status"] < 300:
            body = (e.get("body") or "").strip().replace("\n", " ")[:120]
            res.append(_r(f"EmulationStation API {path}", OK, f"HTTP {e['status']}" + (f", {body}" if body else "")))
        elif "status" in e:
            res.append(_r(f"EmulationStation API {path}", WARN, f"HTTP {e['status']}"))
        else:
            res.append(_r(f"EmulationStation API {path}", WARN,
                          f"not reachable ({e.get('error', 'no answer')}): saving works, only the optional list reload does not"))

    disk = g("disk") or {}
    ud = disk.get("userdata") or {}
    if "free_mb" in ud:
        mb = ud["free_mb"]
        st = FAIL if mb < MIN_FREE_MB_FAIL else WARN if mb < MIN_FREE_MB_WARN else OK
        res.append(_r("free space /userdata", st, f"{mb} MB free of {ud.get('total_mb')} MB"
                      + (f" (need at least {MIN_FREE_MB_FAIL} MB)" if st == FAIL else "")))
    else:
        res.append(_r("free space /userdata", WARN, ud.get("error", "unknown")))

    cfgs = (g("es_config") or {}).get("configs") or []
    existing = [c["path"] for c in cfgs if c.get("exists")]
    if existing:
        res.append(_r("es_systems configs", OK, "; ".join(existing)))
    else:
        res.append(_r("es_systems configs", WARN, "none found (" + ", ".join(c["path"] for c in cfgs) + "): the system list will be empty"))
    sw = (g("es_config") or {}).get("switch_system")
    if sw:
        res.append(_r("switch system in es_systems", OK, f"path {sw.get('path') or '?'}, extensions {' '.join(sw.get('extensions') or [])}"))
    else:
        res.append(_r("switch system in es_systems", WARN, "not defined in any config"))

    r = g("switch_roms") or {}
    if not r.get("exists"):
        res.append(_r("switch roms folder", WARN, f"{r.get('path', '/userdata/roms/switch')} does not exist"))
    else:
        bits = [f"{r['path']}" + (f" -> {r['realpath']}" if r.get("is_symlink") else ""),
                "gamelist.xml " + ("present" if r.get("gamelist") else "ABSENT"),
                f"images {r.get('images')}", f"videos {r.get('videos')}"]
        st = OK if r.get("writable") and r.get("gamelist") else WARN
        res.append(_r("switch roms folder", st, ", ".join(bits) + ("" if r.get("writable") else "; NOT writable")))

    d = g("display") or {}
    if d.get("connectable"):
        note = "" if d.get("DISPLAY") else " (DISPLAY is not set in this shell; the launcher sets :0.0)"
        res.append(_r("display", OK, f"X server answers on {d.get('socket')}{note}"))
    elif d.get("socket_exists"):
        res.append(_r("display", WARN, f"{d.get('socket')} exists but refused the connection: {d.get('connect_error', '?')}"))
    else:
        res.append(_r("display", WARN, f"no X server found for {d.get('assumed', ':0.0')}: the window can only open on the desktop "
                                       f"(tests with QT_QPA_PLATFORM=offscreen still work)"))

    y = g("ytdlp") or {}
    if y.get("version"):
        where = y.get("local_path") if y.get("local_exists") else y.get("on_path")
        res.append(_r("yt-dlp", OK, f"{y['version']} ({where})"))
    elif y.get("local_exists") or y.get("on_path"):
        res.append(_r("yt-dlp", WARN, f"present but did not run: {y.get('error', '?')}"))
    else:
        res.append(_r("yt-dlp", WARN, f"not installed (expected {y.get('local_path')}): YouTube trailers need Tools > Update downloader"))

    dd = g("data_dir") or {}
    if dd.get("exists"):
        have = [k for k in ("scrappy", "bin", "cache", "backups", "journal") if dd.get(k)]
        res.append(_r("app folder", OK, f"{dd['path']} (has: {', '.join(have) or 'nothing yet'}; config.json {dd.get('config_json')})"))
        if dd.get("config_json") == "unreadable":
            res.append(_r("config.json", WARN, "exists but is not valid JSON: defaults are used and the file is left untouched"))
    else:
        res.append(_r("app folder", WARN, f"{dd.get('path', '?')} does not exist yet (deploy/install.sh creates it)"))
    return res


def _jpeg_problem(check):
    """Plain words for a failed JPEG round trip, with the known box problem spelled out."""
    err = check.get("error") or check.get("jpeg_error") or "unknown error"
    text = f"JPEG failed: {err}"
    if "jpeg library version" in err.lower():
        text += " (libjpeg 80 vs 90 mismatch: the system Pillow was built for libjpeg 9 but the system has libjpeg 8)"
    return text


def _pillow_results(p):
    if not p:
        return [_r("Pillow JPEG round trip", WARN, "not checked")]
    out = []
    system, vendored = p.get("system") or {}, p.get("vendored")
    sys_ok = bool(system.get("jpeg"))
    ven_ok = bool(vendored and vendored.get("jpeg") and vendored.get("under_vendor", True))
    where = lambda c: f"Pillow {c.get('version', '?')} at {c.get('file', '?')}"
    if sys_ok:
        out.append(_r("Pillow JPEG round trip (system)", OK, f"save + open + pixel check ok; {where(system)}"))
    elif ven_ok:
        out.append(_r("Pillow JPEG round trip (system)", WARN,
                      f"{_jpeg_problem(system)}; harmless, the launcher uses the vendored Pillow below"))
    else:
        out.append(_r("Pillow JPEG round trip (system)", FAIL, _jpeg_problem(system)))
    if vendored is None:
        if sys_ok:
            out.append(_r("Pillow JPEG round trip (vendored)", OK, f"no vendored copy in {p.get('vendor_dir')} (not needed: the system Pillow works)"))
        else:
            out.append(_r("Pillow JPEG round trip (vendored)", FAIL,
                          f"no vendored copy in {p.get('vendor_dir')} and the system Pillow cannot do JPEG: run deploy/install.sh"))
    elif ven_ok:
        out.append(_r("Pillow JPEG round trip (vendored)", OK, f"save + open + pixel check ok; {where(vendored)}"))
    elif vendored.get("jpeg") and not vendored.get("under_vendor", True):
        out.append(_r("Pillow JPEG round trip (vendored)", FAIL,
                      f"the Pillow that loads is not the vendored one: {vendored.get('file')}"))
    else:
        out.append(_r("Pillow JPEG round trip (vendored)", FAIL, _jpeg_problem(vendored)))
    used = vendored if ven_ok else system if sys_ok else None
    if used is None:
        out.append(_r("Pillow PNG/WEBP support", FAIL, "no working Pillow to ask"))
    else:
        png, webp = bool(used.get("png")), bool(used.get("webp"))
        out.append(_r("Pillow PNG/WEBP support", OK if png else FAIL,
                      f"PNG round trip {'ok' if png else 'FAILED: ' + str(used.get('png_error'))}; "
                      f"WEBP {'supported' if webp else 'not supported (optional)'}"))
    return out


def overall(results):
    if any(r["status"] == FAIL for r in results):
        return FAIL
    return WARN if any(r["status"] == WARN for r in results) else OK


def format_text(results):
    width = max((len(r["name"]) for r in results), default=10)
    lines = [f"[{r['status']:<4}] {r['name']:<{width}}  {r['detail']}" for r in results]
    counts = {s: sum(1 for r in results if r["status"] == s) for s in (OK, WARN, FAIL)}
    lines.append("")
    lines.append(f"Overall: {overall(results)}  ({counts[OK]} OK, {counts[WARN]} WARN, {counts[FAIL]} FAIL)")
    return "\n".join(lines)


def pillow_check_command(vendor_dir, strict):
    """The installer's gate: a real JPEG round trip with the vendored Pillow. Exit code 1 only with --strict."""
    check = pillow_check(pythonpath=vendor_dir) if vendor_dir else pillow_check(isolated=True)
    under = True
    if vendor_dir and check.get("file"):
        under = os.path.realpath(check["file"]).startswith(os.path.realpath(vendor_dir) + os.sep)
    ok = bool(check.get("jpeg") and check.get("png") and under)
    print(f"Pillow {check.get('version', '?')} from {check.get('file', '?')}")
    print(f"  JPEG save+open+pixel check: {'ok' if check.get('jpeg') else _jpeg_problem(check)}")
    print(f"  PNG round trip: {'ok' if check.get('png') else 'FAILED ' + str(check.get('png_error', ''))}; "
          f"WEBP: {'yes' if check.get('webp') else 'no'}")
    if not under:
        print(f"  NOT the vendored copy: expected a file under {vendor_dir}")
    print("PILLOW-OK" if ok else "PILLOW-FAIL")
    return 0 if (ok or not strict) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Report the facts Scrappy assumes about this Batocera (read-only).")
    parser.add_argument("--json", action="store_true", help="print JSON (facts and verdicts) instead of text")
    parser.add_argument("--userdata", default=DEFAULT_USERDATA, help="the /userdata folder (default %(default)s)")
    parser.add_argument("--data-dir", default=None, help="the app's data folder (default <userdata>/system/scrappy)")
    parser.add_argument("--pillow-check", action="store_true",
                        help="only round-trip a JPEG through the Pillow found first in --vendor-dir (used by deploy/install.sh)")
    parser.add_argument("--vendor-dir", default=None, help="folder holding the vendored PIL (with --pillow-check)")
    parser.add_argument("--strict", action="store_true",
                        help="with --pillow-check: exit 1 unless JPEG and PNG work and PIL really comes from --vendor-dir")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 0                                      # even a typo on the command line must not break `&&` chains
    if args.pillow_check:
        return pillow_check_command(args.vendor_dir, args.strict)
    try:
        facts = gather_facts(args.userdata, args.data_dir)
        results = evaluate(facts)
        if args.json:
            print(json.dumps({"overall": overall(results), "results": results, "facts": facts}, indent=2, sort_keys=True))
        else:
            print(format_text(results))
    except Exception as exc:                          # the probe itself must never crash the caller
        print(f"[FAIL] probe crashed: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
