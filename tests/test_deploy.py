"""Deployment tests: deploy/install.sh against a fake ssh, the launcher, the desktop entry and the environment probe.

The fake ``ssh`` on PATH never connects anywhere. It records every command it is given, rewrites the Batocera paths
(/userdata, /usr/share/applications) into a sandbox folder and runs the command locally with ``sh``. So the real remote
scripts are executed end to end, on a copy of the Batocera layout that holds seeded "user data".
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash") or "/bin/bash"

SEPARATOR = "=====STUB-COMMAND"
PROTECTED_NAMES = ("bin", "cache", "backups", "journal", "logs", "config.json")

STUB_SSH = r'''#!/bin/bash
# Fake ssh for tests: records the command, runs it in a sandbox. Never reaches a network.
: "${STUB_SANDBOX:?}" "${STUB_LOG:?}"
while [ $# -gt 0 ]; do
    case $1 in
        -o|-p|-i|-l) shift 2 ;;
        -*) shift ;;
        *) break ;;
    esac
done
host=$1; shift
cmd="$*"
upload=""
if [ -n "${STUB_SSH_FAIL:-}" ]; then
    { echo "$STUB_SEPARATOR host=$host"; printf '%s\n' "$cmd"; } >> "$STUB_LOG"
    echo "ssh: connect to host $host port 22: No route to host" >&2
    exit 255
fi
if [ "$cmd" = "sh -s" ]; then
    cmd=$(cat)
else
    case $cmd in *"cat >"*) upload=1 ;; esac
fi
{ echo "$STUB_SEPARATOR host=$host"; printf '%s\n' "$cmd"; } >> "$STUB_LOG"
rewritten=$(printf '%s' "$cmd" | sed -e "s#/userdata#$STUB_SANDBOX/userdata#g" -e "s#/usr/share/applications#$STUB_SANDBOX/usr/share/applications#g" -e "s#/proc/#$STUB_SANDBOX/proc/#g")
header='uname() { if [ -n "${STUB_UNAME:-}" ]; then echo "$STUB_UNAME"; else command uname "$@"; fi; }
df() { if [ -n "${STUB_FREE_MB:-}" ]; then printf "Filesystem 1M-blocks Used Available Capacity Mounted\nx 9 9 %s 1%% /\n" "$STUB_FREE_MB"; else command df "$@"; fi; }
sha256sum() { shasum -a 256 "$@"; }
'
if [ -n "$upload" ]; then
    cat > "$STUB_SANDBOX/.upload.tgz"
    cp "$STUB_SANDBOX/.upload.tgz" "$STUB_SANDBOX/captured.tar.gz"
    if [ -n "${STUB_CORRUPT_UPLOAD:-}" ]; then printf 'garbage' >> "$STUB_SANDBOX/.upload.tgz"; fi
    sh -c "$header$rewritten" < "$STUB_SANDBOX/.upload.tgz"
else
    sh -c "$header$rewritten" < /dev/null
fi
'''

STUB_CURL = r"""#!/bin/bash
# Fake curl: records the URL, serves $STUB_CURL_FILE (if set) to the -o target, otherwise fails like a dead network.
out=""; url=""
while [ $# -gt 0 ]; do
    case $1 in
        -o) out=$2; shift 2 ;;
        --retry|--connect-timeout|--max-time) shift 2 ;;
        -*) shift ;;
        *) url=$1; shift ;;
    esac
done
echo "$url" >> "${STUB_CURL_LOG:-/dev/null}"
[ -n "${STUB_CURL_FILE:-}" ] || exit 22
cp "$STUB_CURL_FILE" "$out"
"""

STUB_SCP = '#!/bin/bash\necho "scp must not be used" >&2\nexit 97\n'


# ---------------------------------------------------------------- fixtures and helpers

WHEEL_NAME = "pillow-99.0-cp311-cp311-manylinux_2_17_x86_64.whl"
WHEEL_NAME_ARM = "pillow-99.0-cp311-cp311-manylinux_2_17_aarch64.whl"
ARM_MARKER = "pillow.libs/libmarker-aarch64.so"       # only the aarch64 test wheel has this member
_WHEELS = {}


def build_wheel(path, members=None, real_pil="light"):
    """A wheel shaped like Pillow's: a PIL package, a pillow.libs file, a dist-info, and one foreign package.

    real_pil="full"   the venv's complete PIL (about 14 MB, slow: used by one test)
    real_pil="light"  a PIL whose __init__ borrows the venv's modules through __path__ (works, but is tiny)
    real_pil=False    a hollow PIL without an Image module (cannot do JPEG)
    """
    import PIL
    pil_dir = Path(PIL.__file__).parent
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        if real_pil == "full":
            for f in sorted(pil_dir.rglob("*")):
                if f.is_file() and "__pycache__" not in f.parts:
                    z.write(f, "PIL/" + f.relative_to(pil_dir).as_posix())
            libs = pil_dir.parent / "pillow.libs"                    # Linux wheels keep their libraries here, next to PIL
            if libs.is_dir():
                for f in sorted(libs.rglob("*")):
                    if f.is_file():
                        z.write(f, "pillow.libs/" + f.relative_to(libs).as_posix())
        elif real_pil == "light":
            z.writestr("PIL/__init__.py", f"__path__.append({str(pil_dir)!r})\n"
                       f"exec(compile(open({str(pil_dir / '__init__.py')!r}).read(), 'PIL/__init__.py', 'exec'))\n")
        else:
            z.writestr("PIL/__init__.py", "__version__ = '0.0'\n")
        z.writestr("pillow.libs/libfake-1.so.0", "not a real library")
        z.writestr("pillow-99.0.dist-info/METADATA", "Name: pillow\nVersion: 99.0\n")
        z.writestr("some_other_package/__init__.py", "raise SystemExit('must not be extracted')\n")
        for name, data in (members or {}).items():
            z.writestr(name, data)
    return Path(path)


def copy_real_pil(vendor):
    """Copy the venv's own Pillow into `vendor` the way an unpacked wheel looks: PIL, and (Linux wheels) the sibling pillow.libs."""
    import PIL
    pil_dir = Path(PIL.__file__).parent
    shutil.copytree(pil_dir, Path(vendor) / "PIL", ignore=shutil.ignore_patterns("__pycache__"))
    libs = pil_dir.parent / "pillow.libs"
    if libs.is_dir():
        shutil.copytree(libs, Path(vendor) / "pillow.libs")


def good_wheel_path():
    """Built once per test run (about 10 MB)."""
    if "good" not in _WHEELS:
        import tempfile
        _WHEELS["good"] = build_wheel(Path(tempfile.mkdtemp(prefix="scrappy-test-wheel-")) / WHEEL_NAME)
    return _WHEELS["good"]


def arm_wheel_path():
    """Like good_wheel_path(), but the aarch64 pin's wheel: it carries a marker library so a test can tell which one was shipped."""
    if "arm" not in _WHEELS:
        import tempfile
        _WHEELS["arm"] = build_wheel(Path(tempfile.mkdtemp(prefix="scrappy-test-wheel-")) / WHEEL_NAME_ARM, {ARM_MARKER: "arm"})
    return _WHEELS["arm"]


class Env:
    def __init__(self, tmp: Path, repo: Path):
        self.tmp = tmp
        self.repo = repo
        self.sandbox = tmp / "box"
        self.bin = tmp / "stubbin"
        self.log = tmp / "ssh.log"
        self.userdata = self.sandbox / "userdata"
        self.dest = self.userdata / "system" / "scrappy"
        self.old_dest = self.userdata / "system" / "game-asset-fixer"      # the folder of the install before the rename
        (self.userdata / "system").mkdir(parents=True)
        (self.sandbox / "usr" / "share" / "applications").mkdir(parents=True)
        self.bin.mkdir()
        for name, body in (("ssh", STUB_SSH), ("scp", STUB_SCP), ("curl", STUB_CURL)):
            p = self.bin / name
            p.write_text(body)
            p.chmod(0o755)
        (self.bin / "python3").symlink_to(sys.executable)
        self.log.write_text("")
        self.curl_log = tmp / "curl.log"
        self.curl_log.write_text("")
        self.cache = tmp / "wheelcache"
        self.vendor_file = tmp / "vendor.txt"
        self.use_wheel(good_wheel_path())

    def use_wheel(self, wheel, pin_sha=None, seed_cache=True):
        """Pins `wheel` in a private vendor.txt and (by default) puts it into the wheel cache, like a finished download."""
        sha = pin_sha or hashlib.sha256(Path(wheel).read_bytes()).hexdigest()
        self.vendor_file.write_text(f"# test pin\n{sha}  {WHEEL_NAME}  https://files.pythonhosted.org/packages/test/{WHEEL_NAME}\n")
        shutil.rmtree(self.cache, ignore_errors=True)
        self.cache.mkdir(parents=True)
        if seed_cache:
            shutil.copy(wheel, self.cache / WHEEL_NAME)
        return sha

    def pin_line(self, name, wheel, url_name=None, sha=None):
        sha = sha or hashlib.sha256(Path(wheel).read_bytes()).hexdigest()
        return f"{sha}  {name}  https://files.pythonhosted.org/packages/test/{url_name or name}\n"

    def use_both_wheels(self):
        """vendor.txt with one pin per architecture (like the real one); both wheels are in the cache."""
        self.vendor_file.write_text("# test pins\n" + self.pin_line(WHEEL_NAME, good_wheel_path())
                                    + self.pin_line(WHEEL_NAME_ARM, arm_wheel_path()))
        shutil.rmtree(self.cache, ignore_errors=True)
        self.cache.mkdir(parents=True)
        shutil.copy(good_wheel_path(), self.cache / WHEEL_NAME)
        shutil.copy(arm_wheel_path(), self.cache / WHEEL_NAME_ARM)

    @property
    def install_sh(self) -> Path:
        return self.repo / "deploy" / "install.sh"

    def env(self, **extra):
        e = dict(os.environ)
        e.update(PATH=f"{self.bin}:{e['PATH']}", STUB_SANDBOX=str(self.sandbox), STUB_LOG=str(self.log),
                 STUB_SEPARATOR=SEPARATOR, TMPDIR=str(self.tmp), SCRAPPY_VENDOR_FILE=str(self.vendor_file),
                 SCRAPPY_WHEEL_CACHE=str(self.cache), STUB_CURL_LOG=str(self.curl_log))
        e.pop("SCRAPPY_HOST", None)
        e.pop("SCRAPPY_ARCH", None)
        e["STUB_UNAME"] = "x86_64"                         # what the fake box answers to `uname -m`
        e.update(extra)
        return e

    def run(self, *args, **extra):
        return subprocess.run([BASH, str(self.install_sh), *args], capture_output=True, text=True, timeout=180,
                              env=self.env(**extra), cwd=str(self.tmp))

    def commands(self):
        """[(host, script text)] of every ssh call the stub saw."""
        out = []
        text = self.log.read_text()
        for chunk in text.split(SEPARATOR)[1:]:
            head, _, body = chunk.partition("\n")
            out.append((head.strip().replace("host=", "", 1), body))
        return out

    def seed_user_data(self):
        """Files the installer must never touch. Returns the snapshot to compare against later."""
        d = self.dest
        for rel, content in (("bin/yt-dlp", "#!/bin/sh\necho 2026.01.01\n"), ("cache/steam/1.json", "{}"),
                             ("backups/switch/gamelist-20261001.xml", "<gameList/>"), ("journal/save-1.json", "{}"),
                             ("config.json", '{"max_video_seconds": 60}'), ("logs/scrappy.log", "old log\n")):
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        return self.snapshot()

    # ---- the install this project had before it was renamed (game-asset-fixer, package gaf)
    OLD_USER_FILES = {"config.json": '{"max_video_seconds": 45}',
                      "backups/switch/gamelist-20261001.xml": "<gameList/>",
                      "journal/0000000000000001/journal.json": "{}",
                      "cache/steam/1.json": "{}",
                      "bin/yt-dlp": "#!/bin/sh\necho 2026.01.01\n"}
    OLD_APP_FILES = {"gaf/__init__.py": "# old\n", "gaf/app.py": "# old\n", "gaf.previous/__init__.py": "# older\n",
                     "tools/box_selftest.py": "# old tool\n", "vendor/PIL/__init__.py": "# old pillow\n",
                     "game-asset-fixer.sh": "#!/bin/sh\n", "game-asset-fixer.desktop": "[Desktop Entry]\n", "icon.png": "png",
                     "VERSION": "abc built long ago\n", "MANIFEST.sha256": "00  VERSION\n", "VENDOR.sha256": "00  vendor\n",
                     "logs/gaf.log": "program log\n", "tmp/clip-0123456789ab.mp4": "x", "notes-by-the-user.txt": "mine\n"}
    OLD_SHORTCUT = "[Desktop Entry]\nName=Game Asset Fixer\nExec=/userdata/system/game-asset-fixer/game-asset-fixer.sh\n"

    def old_shortcuts(self):
        """The four places the old installer put a shortcut (the Desktop one under the name it really wrote)."""
        return [self.userdata / "system/.local/share/applications/game-asset-fixer.desktop",
                self.userdata / "system/Desktop/Game-Asset-Fixer.desktop",
                self.sandbox / "usr/share/applications/game-asset-fixer.desktop"]

    def seed_old_install(self, user_data=True, app=True, shortcuts=True, config=None):
        """An install made under the old name: the app, the user's data, the shortcuts, the launcher log, bystanders."""
        files = {}
        if app:
            files.update(self.OLD_APP_FILES)
        if user_data:
            files.update(self.OLD_USER_FILES)
        if config is not None:
            files["config.json"] = config
        if files:
            self.old_dest.mkdir(parents=True, exist_ok=True)
        for rel, text in files.items():
            path = self.old_dest / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        if shortcuts:
            for path in self.old_shortcuts():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(self.OLD_SHORTCUT)
        logs = self.userdata / "system" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "game-asset-fixer.log").write_text("launcher log\n")
        (logs / "game-asset-fixer.log.1").write_text("older launcher log\n")
        # files that merely look like ours must never be touched
        (self.userdata / "system" / "game-asset-fixer-notes.txt").write_text("bystander\n")
        (self.userdata / "system" / "Desktop").mkdir(parents=True, exist_ok=True)
        (self.userdata / "system" / "Desktop" / "other-app.desktop").write_text("[Desktop Entry]\n")

    def seed_process(self, pid, *argv):
        """A running process as /proc shows it (the fake ssh points /proc at the sandbox): NUL-separated arguments."""
        d = self.sandbox / "proc" / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")

    def old_backups(self):
        return sorted((self.userdata / "system").glob("game-asset-fixer.old-*"))

    def tree(self, root):
        """{relative path: sha256, or None for a folder} of everything below root."""
        return {p.relative_to(root).as_posix(): None if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(Path(root).rglob("*"))}

    def snapshot(self):
        snap = {}
        for name in PROTECTED_NAMES:
            base = self.dest / name
            if base.is_file():
                snap[name] = hashlib.sha256(base.read_bytes()).hexdigest()
            elif base.is_dir():
                for p in sorted(base.rglob("*")):
                    snap[str(p.relative_to(self.dest))] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir"
        return snap


def _copy_repo(dst: Path) -> Path:
    """A private copy of the parts install.sh packages, so tests can add junk without touching the real tree."""
    for part in ("scrappy", "tools", "deploy"):
        shutil.copytree(REPO / part, dst / part, ignore=shutil.ignore_patterns(".DS_Store"), symlinks=False)
    return dst


@pytest.fixture
def box(tmp_path):
    repo = _copy_repo(tmp_path / "repo")
    return Env(tmp_path, repo)


def installed(box):
    r = box.run("--no-probe", "alice@box.local")
    assert r.returncode == 0, r.stdout + r.stderr
    return r


# ---------------------------------------------------------------- syntax and file content

def test_scripts_pass_bash_syntax_check():
    for rel in ("deploy/install.sh", "deploy/scrappy.sh"):
        r = subprocess.run([BASH, "-n", str(REPO / rel)], capture_output=True, text=True)
        assert r.returncode == 0, f"{rel}: {r.stderr}"
        assert (REPO / rel).stat().st_mode & stat.S_IXUSR, f"{rel} must be executable"


def test_install_script_avoids_bash4_features():
    text = (REPO / "deploy/install.sh").read_text()
    code = "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))
    for bad in ("declare -A", "mapfile", "readarray", "coproc", "${!", ",,}", "^^}", "&>>", "|&"):
        assert bad not in code, f"macOS bash 3.2 does not support {bad!r}"


def test_remote_scripts_have_valid_posix_sh_syntax(box):
    r = box.run("--dry-run", "--no-probe")
    assert r.returncode == 0, r.stderr
    scripts = []
    cur = None
    for line in r.stdout.splitlines():
        if line.startswith("[dry-run] would run this"):
            cur = []
            scripts.append(cur)
        elif line.startswith("==>"):
            cur = None
        elif cur is not None and line.startswith("    | "):
            cur.append(line[6:])
    assert len(scripts) >= 5
    prelude = scripts[0][:scripts[0].index("set -eu")]
    for body in scripts:
        text = "\n".join((prelude if body is not scripts[0] else []) + body) + "\n"
        r = subprocess.run(["sh", "-n"], input=text, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr + text


def parse_desktop(path: Path):
    lines = path.read_text().splitlines()
    assert lines[0] == "[Desktop Entry]"
    kv = {}
    for l in lines[1:]:
        if not l.strip() or l.startswith("#"):
            continue
        assert "=" in l and not l.startswith("["), f"bad line {l!r}"
        k, v = l.split("=", 1)
        assert k not in kv, f"duplicate key {k}"
        kv[k] = v
    return kv


def test_desktop_entry_is_valid():
    kv = parse_desktop(REPO / "deploy/scrappy.desktop")
    assert kv["Type"] == "Application"
    assert kv["Name"] == "Scrappy"
    assert kv["Terminal"] == "false"
    assert kv["Exec"] == "/userdata/system/scrappy/scrappy.sh"
    assert kv["Icon"] == "/userdata/system/scrappy/icon.png"
    assert kv["Categories"].endswith(";") and "Game" in kv["Categories"].split(";")
    assert kv["Categories"] == "Game;batocera.linux;"          # the same categories as the proven Eden shortcut
    assert kv["Version"] == "1.0"
    assert (REPO / "deploy/icon.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_launcher_does_not_use_isolated_mode():
    """-I would ignore PYTHONPATH and the current folder, so `-m scrappy.app` could not find the package."""
    code = "\n".join(l for l in (REPO / "deploy/scrappy.sh").read_text().splitlines() if not l.lstrip().startswith("#"))
    assert " -I" not in code
    assert "-m scrappy.app" in code and "PYTHONPATH" in code and "QT_QPA_PLATFORM=xcb" in code and ":0.0" in code


# ---------------------------------------------------------------- launcher

def _fake_python(tmp: Path) -> Path:
    p = tmp / "fakepython"
    p.write_text('#!/bin/bash\necho "ARGS: $*"\necho "DISPLAY=$DISPLAY"\necho "QT=$QT_QPA_PLATFORM"\n'
                 'echo "PYTHONPATH=$PYTHONPATH"\necho "CWD=$(pwd)"\n')
    p.chmod(0o755)
    return p


def _run_launcher(tmp, env_extra=None, args=(), unset=()):
    home = tmp / "app"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in unset}
    env.update(SCRAPPY_HOME=str(home), SCRAPPY_LOG=str(tmp / "logs" / "scrappy.log"), SCRAPPY_PYTHON=str(_fake_python(tmp)))
    env.update(env_extra or {})
    return subprocess.run([BASH, str(REPO / "deploy/scrappy.sh"), *args], capture_output=True, text=True, env=env,
                          timeout=30), home


def test_launcher_sets_environment_and_logs(tmp_path):
    r, home = _run_launcher(tmp_path, args=("--demo",), unset=("DISPLAY",))
    assert r.returncode == 0, r.stderr
    log = (tmp_path / "logs" / "scrappy.log").read_text()
    assert "ARGS: -m scrappy.app --demo" in log
    assert "DISPLAY=:0.0" in log and "QT=xcb" in log
    assert f"PYTHONPATH={home}/vendor:{home}\n" in log          # the vendored Pillow must win over the system one
    assert "CWD=" in log and str(home.name) in log
    assert "starting" in log


def test_launcher_keeps_an_existing_display(tmp_path):
    r, _ = _run_launcher(tmp_path, env_extra={"DISPLAY": ":1.0"})
    assert "DISPLAY=:1.0" in (tmp_path / "logs" / "scrappy.log").read_text()


def test_launcher_rotates_big_logs(tmp_path):
    logdir = tmp_path / "logs"
    logdir.mkdir()
    log = logdir / "scrappy.log"
    log.write_text("x" * 1_200_000)
    (logdir / "scrappy.log.1").write_text("one")
    (logdir / "scrappy.log.2").write_text("two")
    (logdir / "scrappy.log.3").write_text("three-is-dropped")
    r, _ = _run_launcher(tmp_path)
    assert r.returncode == 0, r.stderr
    assert (logdir / "scrappy.log.1").read_text() == "x" * 1_200_000
    assert (logdir / "scrappy.log.2").read_text() == "one"
    assert (logdir / "scrappy.log.3").read_text() == "two"
    assert not (logdir / "scrappy.log.4").exists()
    assert log.stat().st_size < 10_000


def test_launcher_does_not_rotate_small_logs(tmp_path):
    logdir = tmp_path / "logs"
    logdir.mkdir()
    (logdir / "scrappy.log").write_text("keep me\n")
    _run_launcher(tmp_path)
    text = (logdir / "scrappy.log").read_text()
    assert text.startswith("keep me") and not (logdir / "scrappy.log.1").exists()


def test_launcher_reports_missing_install(tmp_path):
    env = dict(os.environ, SCRAPPY_HOME=str(tmp_path / "nope"), SCRAPPY_LOG=str(tmp_path / "l.log"))
    r = subprocess.run([BASH, str(REPO / "deploy/scrappy.sh")], capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "not installed" in r.stderr


# ---------------------------------------------------------------- install.sh: dry run

def test_dry_run_install_sends_nothing(box):
    r = box.run("--dry-run", "alice@box.local")
    assert r.returncode == 0, r.stderr
    assert box.commands() == []                                  # the fake ssh was never called
    assert "DRY RUN" in r.stdout and "nothing was changed" in r.stdout
    assert "alice@box.local" in r.stdout
    assert "scrappy/__init__.py" in r.stdout and "sha256:" in r.stdout
    assert not box.dest.exists()
    assert list(box.sandbox.rglob("*.desktop")) == []


def test_dry_run_uninstall_sends_nothing(box):
    installed(box)
    before = box.log.read_text()
    snap = box.seed_user_data()
    r = box.run("--uninstall", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert box.log.read_text() == before
    assert (box.dest / "scrappy").is_dir() and box.snapshot() == snap


def test_printed_install_scripts_never_modify_protected_dirs(box):
    r = box.run("--dry-run")
    in_migration = False
    for line in r.stdout.splitlines():
        if line.startswith("==>"):
            in_migration = "old name" in line         # the migration moves the user data on purpose: tests below cover it
        if in_migration or not line.startswith("    | "):
            continue
        code = line[6:].strip()
        if code.startswith("#") or code.startswith("mkdir -p") or code.startswith("echo"):
            continue
        for name in PROTECTED_NAMES:
            assert not re.search(rf"(^|[\s/\"']){re.escape(name)}($|[\s/\"'])", code), f"{name} in: {code}"


# ---------------------------------------------------------------- install.sh: the real flow against the fake box

def test_install_end_to_end(box):
    snap = box.seed_user_data()
    r = installed(box)
    out = r.stdout
    assert "PILLOW-OK" in out and "/vendor/PIL/__init__.py" in out            # printed: version + the file that was used
    assert "free space on " in out and "tarball sha256 matches" in out
    assert "VERIFY-OK" in out and "installed to /userdata/system/scrappy" in out
    assert box.commands()[0][0] == "alice@box.local"
    d = box.dest
    assert (d / "scrappy" / "__init__.py").is_file() and (d / "tools" / "probe_environment.py").is_file()
    launcher = d / "scrappy.sh"
    assert launcher.is_file() and os.access(launcher, os.X_OK)
    assert (d / "icon.png").is_file() and (d / "VERSION").read_text().strip()
    # shortcuts: same method as Eden (persistent copy, Desktop copy, /usr/share copy), all executable and identical
    packaged = (REPO / "deploy/scrappy.desktop").read_bytes()
    for p in (box.userdata / "system/.local/share/applications/scrappy.desktop",
              box.userdata / "system/Desktop/scrappy.desktop",
              box.sandbox / "usr/share/applications/scrappy.desktop"):
        assert p.read_bytes() == packaged, p
        assert os.access(p, os.X_OK), p
    # user data untouched, nothing temporary left behind, the standard folders exist
    assert box.snapshot() == snap
    assert [p.name for p in d.iterdir() if p.name.startswith(".")] == []
    for sub in ("bin", "cache", "backups", "journal"):
        assert (d / sub).is_dir()
    assert not (d / "scrappy.previous").exists()
    # the vendored Pillow: only PIL, pillow.libs and the dist-info, nothing else, and the wheel itself is not left behind
    assert (d / "vendor" / "PIL" / "__init__.py").is_file() and (d / "vendor" / "pillow.libs" / "libfake-1.so.0").is_file()
    assert (d / "vendor" / "pillow-99.0.dist-info" / "METADATA").is_file()
    assert sorted(p.name for p in (d / "vendor").iterdir()) == ["PIL", "pillow-99.0.dist-info", "pillow.libs"]
    assert not (d / "wheels").exists() and not list(d.rglob("*.whl"))
    assert (d / "VENDOR.sha256").read_text().count("\n") == sum(1 for p in (d / "vendor").rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    assert box.curl_log.read_text() == ""                                    # the cached wheel was used, no download


def test_every_remote_command_stays_inside_allowed_folders(box):
    installed(box)
    allowed = ("/userdata/", "/usr/share/applications", "/dev/null")
    seen = 0
    for host, script in box.commands():
        migration = "# MIGRATE:" in script                 # the one script that moves user data (it also reads /proc)
        for line in script.splitlines():
            code = line.strip()
            if code.startswith("#"):
                continue
            for m in re.finditer(r"""(?:(?<=\s)|(?<=\s")|(?<=\s')|(?<==)|(?<==")|(?<==')|(?<=\())(/[A-Za-z0-9_.\-/]*)""", code):
                path = m.group(1)
                if path == "/":
                    continue
                seen += 1
                assert path.startswith(allowed) or path in ("/userdata",) or (migration and path == "/proc/"), f"{path} in: {code}"
        for name in PROTECTED_NAMES:
            if migration:
                break
            for line in script.splitlines():
                code = line.strip()
                if code.startswith(("#", "mkdir -p", "echo")):
                    continue
                assert not re.search(rf"(^|[\s/\"']){re.escape(name)}($|[\s/\"'])", code), f"{name} in: {code}"
    assert seen > 10


def test_update_keeps_one_previous_version(box):
    installed(box)
    (box.dest / "scrappy" / "marker_v1.txt").write_text("v1")
    (box.dest / "tools" / "marker_v1.txt").write_text("v1")
    (box.dest / "vendor" / "marker_v1.txt").write_text("v1")
    snap = box.seed_user_data()
    installed(box)
    assert (box.dest / "scrappy.previous" / "marker_v1.txt").read_text() == "v1"
    assert (box.dest / "tools.previous" / "marker_v1.txt").exists()
    assert (box.dest / "vendor.previous" / "marker_v1.txt").read_text() == "v1"
    assert not (box.dest / "vendor" / "marker_v1.txt").exists() and (box.dest / "vendor" / "PIL" / "__init__.py").is_file()
    assert not (box.dest / "scrappy" / "marker_v1.txt").exists()
    assert (box.dest / "scrappy" / "__init__.py").is_file()
    assert box.snapshot() == snap
    installed(box)                                               # a third install drops the first generation
    assert not (box.dest / "scrappy.previous" / "marker_v1.txt").exists()
    assert not (box.dest / "vendor.previous" / "marker_v1.txt").exists()
    assert (box.dest / "scrappy.previous" / "__init__.py").is_file()


def test_refuses_when_less_than_200_mb_free(box):
    r = box.run("--no-probe", STUB_FREE_MB="199")
    assert r.returncode != 0
    assert "only 199 MB free" in r.stdout + r.stderr
    assert not box.dest.exists()                                 # nothing was created, nothing was uploaded
    assert len(box.commands()) == 2                              # the architecture question (uname -m) and the one preflight
    assert not (box.sandbox / "captured.tar.gz").exists()


def test_exactly_200_mb_free_is_enough(box):
    r = box.run("--no-probe", STUB_FREE_MB="200")
    assert r.returncode == 0, r.stdout + r.stderr


def test_min_free_cannot_be_lowered_but_can_be_raised(box):
    assert box.run("--min-free-mb", "50", "--dry-run").returncode != 0
    r = box.run("--no-probe", "--min-free-mb", "1000", STUB_FREE_MB="500")
    assert r.returncode != 0 and "only 500 MB free" in r.stdout + r.stderr


def test_damaged_upload_is_detected_and_old_install_survives(box):
    installed(box)
    (box.dest / "scrappy" / "still_here.txt").write_text("old")
    snap = box.seed_user_data()
    r = box.run("--no-probe", STUB_CORRUPT_UPLOAD="1")
    assert r.returncode != 0
    assert "damaged" in r.stdout + r.stderr
    assert (box.dest / "scrappy" / "still_here.txt").read_text() == "old"
    assert not (box.dest / "scrappy.previous").exists()
    assert box.snapshot() == snap
    assert [p.name for p in box.dest.iterdir() if p.name.startswith(".staging")] == []     # the failed upload was cleaned up


def test_broken_python_file_is_caught_before_the_swap(box):
    installed(box)
    (box.dest / "scrappy" / "still_here.txt").write_text("old")
    (box.repo / "scrappy" / "broken.py").write_text("def x(:\n")
    r = box.run("--no-probe")
    assert r.returncode != 0 and "do not compile" in r.stdout + r.stderr
    assert (box.dest / "scrappy" / "still_here.txt").exists()


def test_install_with_probe_runs_the_read_only_probe(box):
    r = box.run("alice@box.local")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "environment probe" in r.stdout and "Overall:" in r.stdout


def test_scp_is_never_needed(box):
    installed(box)                                               # the fake scp exits 97 if it is ever called


def test_host_argument_and_validation(box):
    installed(box)
    assert {h for h, _ in box.commands()} == {"alice@box.local"}
    r = box.run("--dry-run", "--host", "pi@10.0.0.7")
    assert "pi@10.0.0.7" in r.stdout
    r = box.run("--dry-run", SCRAPPY_HOST="bob@elsewhere")
    assert "bob@elsewhere" in r.stdout
    for bad in ("-oProxyCommand=evil", "a b", "host;rm", "$(x)"):
        r = box.run("--dry-run", bad)
        assert r.returncode != 0, bad
    assert box.run("--bogus").returncode != 0


# ---------------------------------------------------------------- the tarball

def test_tarball_content(box):
    repo = box.repo
    (repo / "scrappy" / "__pycache__").mkdir(exist_ok=True)
    (repo / "scrappy" / "__pycache__" / "x.cpython-311.pyc").write_bytes(b"\0")
    (repo / "scrappy" / "stale.pyc").write_bytes(b"\0")
    (repo / "scrappy" / ".DS_Store").write_bytes(b"\0")
    (repo / "scrappy" / "._resource_fork").write_bytes(b"\0")
    (repo / "scrappy" / "old.py.orig").write_text("x")
    (repo / "tools" / "run.log").write_text("x")
    (repo / "scrappy" / "link.py").symlink_to("/etc/passwd")
    for junk_dir in ("tests", "docs", ".venv", ".git"):
        (repo / junk_dir).mkdir()
        (repo / junk_dir / "junk.py").write_text("x")
    installed(box)
    with tarfile.open(box.sandbox / "captured.tar.gz") as tf:
        members = tf.getmembers()
        names = [m.name for m in members]
        bad = re.compile(r"__pycache__|\.pyc$|\.DS_Store|(^|/)\._|\.orig$|\.log$|(^|/)(tests|docs|\.venv|\.git)(/|$)|link\.py|junk")
        assert [n for n in names if bad.search(n)] == []
        for needed in ("scrappy/__init__.py", "scrappy/app.py", "tools/probe_environment.py", "scrappy.sh",
                       "scrappy.desktop", "icon.png", "MANIFEST.sha256", "VERSION"):
            assert needed in names, needed
        assert all(m.isfile() or m.isdir() for m in members)           # no symlinks, devices ...
        assert all(m.uid == 0 and m.gid == 0 for m in members)
        assert all(not n.startswith("/") and ".." not in n.split("/") for n in names)
        assert set(n.split("/")[0] for n in names) == {"scrappy", "tools", "wheels", "scrappy.sh",
                                                          "scrappy.desktop", "icon.png", "MANIFEST.sha256", "VERSION"}
        assert {n for n in names if n.startswith("wheels/")} == {"wheels/" + WHEEL_NAME, "wheels/extract_wheel.py"}
        manifest = tf.extractfile("MANIFEST.sha256").read().decode().splitlines()
        listed = {}
        for line in manifest:
            digest, _, path = line.partition("  ")
            listed[path] = digest
            assert hashlib.sha256(tf.extractfile(path).read()).hexdigest() == digest, path
        files = {n for n, m in zip(names, members) if m.isfile() and n != "MANIFEST.sha256" and not n.startswith("wheels/")}
        assert set(listed) == files                                    # the manifest covers exactly the packaged files
        launcher = next(m for m in members if m.name == "scrappy.sh")
        assert launcher.mode & 0o111


def test_real_repo_tarball_has_no_junk_in_dry_run_listing(box):
    """The listing printed by --dry-run against the real working tree (with whatever caches it has)."""
    r = subprocess.run([BASH, str(REPO / "deploy/install.sh"), "--dry-run"], capture_output=True, text=True, timeout=120,
                       env=box.env())
    assert r.returncode == 0, r.stderr
    listing = r.stdout.split("contents (")[1].split("==>")[0]
    for junk in ("__pycache__", ".pyc", ".DS_Store", "tests/", "docs/", ".venv", "config.json"):
        assert junk not in listing, junk
    assert "scrappy/__init__.py" in listing and "tools/probe_environment.py" in listing


# ---------------------------------------------------------------- migration from the old name (game-asset-fixer, package gaf)

USER_ITEMS = ("config.json", "backups", "journal", "cache", "bin")


def _old_name_leftovers(box):
    """Everything under the sandbox whose name still says game-asset-fixer (relative path)."""
    return sorted(p.relative_to(box.sandbox).as_posix() for p in box.sandbox.rglob("*") if "game-asset-fixer" in p.name.lower())


def _outside(box):
    """The bystanders seed_old_install() leaves around (launcher logs, a look-alike file, another app's shortcut):
    the migration must never change them."""
    keep = ("userdata/system/logs/", "userdata/system/game-asset-fixer-notes.txt", "userdata/system/Desktop/other-app.desktop")
    return {k: v for k, v in box.tree(box.sandbox).items() if k.startswith(keep)}


def test_old_install_is_migrated_to_the_new_name(box):
    box.seed_old_install()
    old_tree = box.tree(box.old_dest)
    bystanders = _outside(box)
    r = installed(box)
    out = r.stdout
    assert "found an install under the old name" in out and "MIGRATE-OK" in out
    # the user's data reached the new folder, byte for byte
    new_tree = box.tree(box.dest)
    for rel, digest in old_tree.items():
        if rel.split("/")[0] in USER_ITEMS:
            assert new_tree.get(rel) == digest, rel
    assert (box.dest / "config.json").read_text() == '{"max_video_seconds": 45}'
    assert (box.dest / "scrappy" / "__init__.py").is_file()
    # the old folder is gone from its place; what is left of it (the app) is kept whole as a backup next to it
    assert not box.old_dest.exists()
    backups = box.old_backups()
    assert len(backups) == 1 and re.fullmatch(r"game-asset-fixer\.old-\d{8}-\d{6}-\d+", backups[0].name)
    kept = box.tree(backups[0])
    assert kept == {rel: d for rel, d in old_tree.items() if rel.split("/")[0] not in USER_ITEMS}
    assert (backups[0] / "gaf" / "app.py").is_file() and (backups[0] / "notes-by-the-user.txt").is_file()
    assert str(backups[0]).replace(str(box.sandbox), "") in out
    # old shortcuts removed, new ones in place
    for p in box.old_shortcuts():
        assert not p.exists() and not p.is_symlink(), p
    assert (box.userdata / "system/Desktop/scrappy.desktop").is_file()
    assert (box.userdata / "system/.local/share/applications/scrappy.desktop").is_file()
    # nothing else was touched: the old launcher logs, a file that merely looks like ours, another app's shortcut
    assert (box.userdata / "system/logs/game-asset-fixer.log").read_text() == "launcher log\n"
    assert (box.userdata / "system/logs/game-asset-fixer.log.1").read_text() == "older launcher log\n"
    assert "left the old launcher log files" in out
    assert (box.userdata / "system/game-asset-fixer-notes.txt").read_text() == "bystander\n"
    assert (box.userdata / "system/Desktop/other-app.desktop").is_file()
    assert _outside(box) == bystanders


def test_no_old_install_means_nothing_to_migrate(box):
    r = installed(box)
    assert "nothing to migrate" in r.stdout and "MIGRATE-OK" not in r.stdout
    assert "old name" not in r.stdout.split("Migrate an install")[0]            # the check did not announce one
    assert _old_name_leftovers(box) == [] and box.old_backups() == []
    assert not (box.dest / "config.json").exists() and list((box.dest / "journal").iterdir()) == []


def test_migration_twice_changes_nothing_the_second_time(box):
    box.seed_old_install()
    installed(box)
    backup = box.old_backups()[0]
    backup_tree, user_tree = box.tree(backup), {k: v for k, v in box.tree(box.dest).items() if k.split("/")[0] in USER_ITEMS}
    system_entries = sorted(p.name for p in (box.userdata / "system").iterdir())
    outside = _outside(box)
    r = installed(box)
    assert "nothing to migrate" in r.stdout and "MIGRATE-OK" not in r.stdout
    assert box.old_backups() == [backup] and box.tree(backup) == backup_tree
    assert {k: v for k, v in box.tree(box.dest).items() if k.split("/")[0] in USER_ITEMS} == user_tree
    assert sorted(p.name for p in (box.userdata / "system").iterdir()) == system_entries
    assert _outside(box) == outside


def test_dry_run_with_an_old_install_changes_nothing(box):
    box.seed_old_install()
    snapshot = box.tree(box.sandbox)
    r = box.run("--dry-run", "alice@box.local")
    assert r.returncode == 0, r.stderr
    assert box.commands() == []                                          # not a single ssh call
    assert box.tree(box.sandbox) == snapshot                             # not a single file or folder changed
    assert "DRY RUN" in r.stdout and "script_migrate" not in r.stdout
    assert "Migrate an install under the old name" in r.stdout and "MIGRATE-OK" in r.stdout      # the plan is shown
    assert "$OLD_DEST.old-$TS" in r.stdout and "game-asset-fixer" in r.stdout


def test_migration_refuses_while_the_old_program_is_running(box):
    box.seed_old_install()
    box.seed_process(4242, "python3", "-m", "gaf.app")
    box.seed_process(4243, "sh", "-s")                                   # unrelated processes do not matter
    old_tree = box.tree(box.old_dest)
    outside = _outside(box)
    r = box.run("--no-probe", "alice@box.local")
    assert r.returncode != 0
    assert "still running" in r.stdout + r.stderr and "4242" in r.stdout + r.stderr
    assert box.tree(box.old_dest) == old_tree and box.old_backups() == []     # the old install was not touched at all
    assert all(p.exists() for p in box.old_shortcuts())
    assert not (box.dest / "config.json").exists() and (box.dest / "scrappy" / "__init__.py").is_file()   # the new one is installed
    assert _outside(box) == outside
    # once the program is closed, the same command finishes the job
    shutil.rmtree(box.sandbox / "proc" / "4242")
    r = installed(box)
    assert "MIGRATE-OK" in r.stdout and not box.old_dest.exists() and len(box.old_backups()) == 1


def test_the_new_program_running_does_not_block_the_migration(box):
    box.seed_old_install()
    box.seed_process(4242, "python3", "-m", "scrappy.app")
    r = installed(box)
    assert "MIGRATE-OK" in r.stdout and not box.old_dest.exists()


def test_migration_with_only_old_shortcuts_left(box):
    box.seed_old_install(user_data=False, app=False, shortcuts=True)
    assert not box.old_dest.exists()
    r = installed(box)
    assert "MIGRATE-OK" in r.stdout and "only old shortcuts were left" in r.stdout
    assert all(not p.exists() for p in box.old_shortcuts()) and box.old_backups() == []
    assert (box.userdata / "system/Desktop/other-app.desktop").is_file() and not (box.dest / "config.json").exists()


def test_migration_with_only_the_old_app_folder(box):
    box.seed_old_install(user_data=False, app=True, shortcuts=False)
    old_tree = box.tree(box.old_dest)
    r = installed(box)
    assert "MIGRATE-OK" in r.stdout and not box.old_dest.exists()
    assert box.tree(box.old_backups()[0]) == old_tree
    assert not (box.dest / "config.json").exists() and list((box.dest / "backups").iterdir()) == []


def test_migration_with_only_a_config_in_the_old_folder(box):
    box.seed_old_install(user_data=False, app=False, shortcuts=False, config='{"max_video_seconds": 33}')
    r = installed(box)
    assert "MIGRATE-OK" in r.stdout and not box.old_dest.exists()
    assert (box.dest / "config.json").read_text() == '{"max_video_seconds": 33}'
    assert box.tree(box.old_backups()[0]) == {}                          # an empty folder is still kept, nothing is deleted
    assert list((box.dest / "journal").iterdir()) == []                  # the installer's empty folders stay


def test_migration_never_overwrites_data_the_new_folder_already_has(box):
    installed(box)
    (box.dest / "config.json").write_text('{"max_video_seconds": 7}')
    (box.dest / "backups" / "switch").mkdir(parents=True)
    (box.dest / "backups" / "switch" / "gamelist-new.xml").write_text("<gameList>new</gameList>")
    box.seed_old_install()
    old_tree = box.tree(box.old_dest)
    r = installed(box)
    assert "MIGRATE-OK" in r.stdout and r.stdout.count("kept the existing") == 2
    assert (box.dest / "config.json").read_text() == '{"max_video_seconds": 7}'
    assert sorted(box.tree(box.dest / "backups")) == ["switch", "switch/gamelist-new.xml"]          # nothing merged in
    assert (box.dest / "backups" / "switch" / "gamelist-new.xml").read_text() == "<gameList>new</gameList>"
    # the items that did not clash were moved; the clashing old ones are in the backup folder, not lost
    assert (box.dest / "journal" / "0000000000000001" / "journal.json").is_file()
    assert (box.dest / "bin" / "yt-dlp").is_file() and (box.dest / "cache" / "steam" / "1.json").is_file()
    kept = box.old_backups()[0]
    assert (kept / "config.json").read_text() == '{"max_video_seconds": 45}'
    assert (kept / "backups" / "switch" / "gamelist-20261001.xml").read_text() == "<gameList/>"
    assert box.tree(kept / "backups") == {k[len("backups/"):]: v for k, v in old_tree.items() if k.startswith("backups/")}


def test_a_config_that_names_the_old_data_folder_is_pointed_at_the_new_one(box):
    old = str(box.old_dest)                     # what the sandbox calls /userdata/system/game-asset-fixer
    box.seed_old_install(config=json.dumps({"data_dir": old, "roms_root": "/userdata/roms", "note": old + "-not-this"}, indent=2))
    original = (box.old_dest / "config.json").read_text()
    r = installed(box)
    cfg = json.loads((box.dest / "config.json").read_text())
    assert cfg["data_dir"] == str(box.dest) and cfg["roms_root"] == "/userdata/roms"
    assert cfg["note"] == old + "-not-this"                              # only a path that IS the old folder (or inside it)
    assert (box.dest / "config.json.before-migration").read_text() == original
    assert "config.json named the old data folder" in r.stdout


def test_uninstall_leaves_an_old_install_alone_and_says_so(box):
    box.seed_old_install()
    snapshot = box.tree(box.old_dest)
    r = box.run("--uninstall", "alice@box.local")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "NOTE: an install under the old name" in r.stdout
    assert box.tree(box.old_dest) == snapshot and all(p.exists() for p in box.old_shortcuts())
    assert box.run("--uninstall", "--dry-run").returncode == 0


def test_a_backup_of_the_old_folder_survives_uninstall_and_purge(box):
    box.seed_old_install()
    installed(box)
    backup = box.old_backups()[0]
    snapshot = box.tree(backup)
    r = box.run("--uninstall", "--purge", "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "NOTE: kept the backup of the old install" in r.stdout and str(backup.name) in r.stdout
    assert box.tree(backup) == snapshot


def test_undo_of_a_save_made_by_the_old_version_still_works_after_the_migration(box, tmp_path):
    """The old version's journal records ABSOLUTE paths into its own folder (journal/<id>/replaced/<file>, and the
    gamelist backup). After the migration that folder is renamed, so the recorded copies are not where journal.json says;
    Saver.undo_last must find them next to journal.json, in the new journal folder."""
    from scrappy.saver import SaveRequest, Saver
    roms = tmp_path / "roms" / "switch"                                  # the games live outside the install, as on the box
    (roms / "images").mkdir(parents=True)
    gamelist = '<?xml version="1.0"?>\n<gameList>\n\t<game>\n\t\t<path>./Lantern Lagoon.nsp</path>\n\t\t<name>old name</name>\n\t</game>\n</gameList>\n'
    (roms / "gamelist.xml").write_text(gamelist, encoding="utf-8")
    (roms / "images" / "Lantern Lagoon-image.jpg").write_bytes(b"THE OLD PICTURE")
    box.seed_old_install(user_data=False)                                # the app and shortcuts; the data comes from a real save
    old_saver = Saver(box.old_dest / "backups", box.old_dest / "journal", es_running=lambda: False)
    old_saver.save(SaveRequest(system_dir=roms, rom_path="./Lantern Lagoon.nsp", fields={"name": "Lantern Lagoon"},
                               image_jpg=b"NEW PICTURE", thumb_png=b"NEW THUMB", replace={"image"}))
    assert (roms / "images" / "Lantern Lagoon-image.jpg").read_bytes() == b"NEW PICTURE"
    saved_gamelist = (roms / "gamelist.xml").read_bytes()
    assert saved_gamelist != gamelist.encode()
    # what the old journal looks like: absolute paths into the old folder
    journal_json = next((box.old_dest / "journal").glob("*/journal.json"))
    record = json.loads(journal_json.read_text())
    assert record["replaced"][0]["backup"] == str(box.old_dest / "journal" / journal_json.parent.name / "replaced" / "Lantern Lagoon-image.jpg")
    assert record["backup"].startswith(str(box.old_dest / "backups"))
    assert record["gamelist"] == str(roms / "gamelist.xml")             # the games' own files are not in the install folder
    # the real migration, in the sandbox
    r = installed(box)
    assert "MIGRATE-OK" in r.stdout and not box.old_dest.exists()
    new_journal = box.dest / "journal"
    assert json.loads(next(new_journal.glob("*/journal.json")).read_text()) == record      # the record itself is unchanged
    assert next((box.dest / "backups").rglob("*.xml"), None) is not None                    # the gamelist backups moved along
    # Undo from the new place
    undone = Saver(None, new_journal).undo_last()
    assert undone is not None and undone["rom_path"] == "./Lantern Lagoon.nsp" and undone["left_alone"] == []
    assert (roms / "gamelist.xml").read_text(encoding="utf-8") == gamelist
    assert (roms / "images" / "Lantern Lagoon-image.jpg").read_bytes() == b"THE OLD PICTURE"
    assert not (roms / "images" / "Lantern Lagoon-thumb.png").exists()
    assert Saver(None, new_journal).undo_last() is None and any((new_journal / "undone").iterdir())


# ---------------------------------------------------------------- uninstall

def test_uninstall_removes_app_and_shortcuts_but_keeps_user_data(box):
    installed(box)
    snap = box.seed_user_data()
    (box.dest / "my-notes.txt").write_text("not ours")
    r = box.run("--uninstall", "alice@box.local")
    assert r.returncode == 0, r.stdout + r.stderr
    d = box.dest
    for gone in ("scrappy", "tools", "scrappy.previous", "tools.previous", "vendor", "vendor.previous", "scrappy.sh",
                 "icon.png", "scrappy.desktop", "MANIFEST.sha256", "VENDOR.sha256", "VERSION"):
        assert not (d / gone).exists(), gone
    assert list(box.sandbox.rglob("*.desktop")) == []
    assert box.snapshot() == snap
    assert (d / "my-notes.txt").read_text() == "not ours"
    assert "kept your data" in r.stdout


def test_uninstall_of_a_clean_install_removes_the_empty_folder(box):
    installed(box)
    r = box.run("--uninstall")
    assert r.returncode == 0, r.stdout + r.stderr
    leftovers = sorted(p.name for p in box.dest.iterdir()) if box.dest.exists() else []
    assert leftovers == ["backups", "bin", "cache", "journal"] or not box.dest.exists()   # install creates the empty data folders
    assert list(box.sandbox.rglob("*.desktop")) == []


def test_uninstall_when_nothing_is_installed_is_harmless(box):
    r = box.run("--uninstall")
    assert r.returncode == 0, r.stdout + r.stderr


def test_purge_needs_yes_and_then_deletes_everything(box):
    installed(box)
    box.seed_user_data()
    r = box.run("--uninstall", "--purge")
    assert r.returncode != 0 and "--yes" in r.stderr
    assert (box.dest / "backups").is_dir()
    assert box.run("--purge").returncode != 0                          # --purge alone makes no sense
    r = box.run("--uninstall", "--purge", "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not box.dest.exists()
    assert list(box.sandbox.rglob("*.desktop")) == []


def test_printed_uninstall_script_protects_user_data_without_purge(box):
    r = box.run("--uninstall", "--dry-run")
    in_purge_branch = False
    for line in r.stdout.splitlines():
        code = line[6:].strip() if line.startswith("    | ") else ""
        if code == 'if [ "$PURGE" = 1 ]; then':
            in_purge_branch = True
        elif code == "else":
            in_purge_branch = False
        if code.startswith(("rm ", "rm -")) and not in_purge_branch:
            for name in PROTECTED_NAMES:
                assert not re.search(rf"(^|[\s/\"']){re.escape(name)}($|[\s/\"'])", code), code
    assert re.search(r'\[ "\$PURGE" = 1 \]', r.stdout)


# ---------------------------------------------------------------- the environment probe

def load_probe():
    spec = importlib.util.spec_from_file_location("probe_environment", REPO / "tools" / "probe_environment.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def probe():
    return load_probe()


def good_facts(probe):
    mods = {name: {"ok": True, "version": "2.2.0"} for name, _l, _r in probe.MODULES}
    return {
        "python": {"version": [3, 11, 9]},
        "modules": mods,
        "ffmpeg": {"ffmpeg": {"path": "/usr/bin/ffmpeg", "version": "ffmpeg version 6.1"},
                   "ffprobe": {"path": "/usr/bin/ffprobe", "version": "ffprobe version 6.1"},
                   "libx264": True, "aac_encoder": True, "av1_decoders": ["libdav1d"]},
        "pillow": {"vendor_dir": "/userdata/system/scrappy/vendor", "system": {"version": "11.0.0", "file": "/s/PIL/__init__.py", "jpeg": True,
                   "png": True, "webp": True},
                   "vendored": {"version": "11.3.0", "file": "/v/vendor/PIL/__init__.py", "jpeg": True, "png": True, "webp": True,
                                "under_vendor": True}},
        "es_api": {"root": {"status": 200, "body": "EmulationStation"}, "running_game": {"status": 200, "body": ""}},
        "disk": {"userdata": {"path": "/userdata", "free_mb": 12000, "total_mb": 50000}},
        "es_config": {"configs": [{"path": "/usr/share/emulationstation/es_systems.cfg", "exists": True}],
                      "switch_system": {"path": "/userdata/roms/switch", "extensions": [".nsp", ".xci"]}},
        "switch_roms": {"path": "/userdata/roms/switch", "exists": True, "realpath": "/media/Windows/roms/switch",
                        "is_symlink": True, "gamelist": True, "images": 328, "videos": 98, "writable": True},
        "display": {"DISPLAY": ":0.0", "assumed": ":0.0", "socket": "/tmp/.X11-unix/X0", "socket_exists": True, "connectable": True},
        "ytdlp": {"local_path": "/userdata/system/scrappy/bin/yt-dlp", "local_exists": True, "on_path": None,
                  "version": "2026.08.19"},
        "data_dir": {"path": "/userdata/system/scrappy", "exists": True, "scrappy": True, "bin": True, "cache": True,
                     "backups": True, "journal": True, "config_json": "valid"},
    }


def statuses(probe, facts):
    return {r["name"]: r["status"] for r in probe.evaluate(facts)}


def test_probe_all_good(probe):
    results = probe.evaluate(good_facts(probe))
    assert {r["status"] for r in results} == {"OK"}, [r for r in results if r["status"] != "OK"]
    assert probe.overall(results) == "OK"
    text = probe.format_text(results)
    assert "[OK  ]" in text and "Overall: OK" in text


def test_probe_labels_each_problem(probe):
    f = good_facts(probe)
    f["python"]["version"] = [3, 8, 0]
    f["modules"]["PyQt5.QtMultimedia"] = {"ok": False, "error": "ImportError: no"}
    f["modules"]["PIL"] = {"ok": False, "error": "ImportError: no"}
    f["modules"]["urllib3"] = {"ok": True, "version": "1.26.18"}
    f["ffmpeg"]["libx264"] = False
    f["ffmpeg"]["av1_decoders"] = []
    f["es_api"]["root"] = {"error": "URLError: refused"}
    f["disk"]["userdata"]["free_mb"] = 150
    f["ytdlp"] = {"local_path": "/x/bin/yt-dlp", "local_exists": False, "on_path": None, "version": None}
    f["display"] = {"DISPLAY": None, "assumed": ":0.0", "socket": "/tmp/.X11-unix/X0", "socket_exists": False, "connectable": False}
    f["switch_roms"] = {"path": "/userdata/roms/switch", "exists": False}
    s = statuses(probe, f)
    assert s["python"] == "FAIL"
    assert s["PyQt5 QtMultimedia"] == "WARN"
    assert s["Pillow"] == "FAIL"
    assert s["urllib3 version"] == "WARN"
    assert s["ffmpeg libx264 encoder"] == "FAIL"
    assert s["ffmpeg AV1 decoder"] == "WARN"
    assert s["EmulationStation API GET /"] == "WARN"
    assert s["EmulationStation API GET /runningGame"] == "OK"
    assert s["free space /userdata"] == "FAIL"
    assert s["yt-dlp"] == "WARN"
    assert s["display"] == "WARN"
    assert s["switch roms folder"] == "WARN"
    assert probe.overall(probe.evaluate(f)) == "FAIL"


def test_probe_free_space_thresholds(probe):
    f = good_facts(probe)
    for mb, want in ((199, "FAIL"), (200, "WARN"), (999, "WARN"), (1000, "OK")):
        f["disk"]["userdata"]["free_mb"] = mb
        assert statuses(probe, f)["free space /userdata"] == want, mb


def test_probe_tolerates_missing_facts(probe):
    results = probe.evaluate({})
    assert results and all(r["status"] in ("OK", "WARN", "FAIL") for r in results)


def test_probe_main_prints_report_and_always_returns_zero(probe, monkeypatch, capsys):
    monkeypatch.setattr(probe, "gather_facts", lambda *a, **k: dict(good_facts(probe)))
    assert probe.main([]) == 0
    assert "Overall: OK" in capsys.readouterr().out
    assert probe.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["overall"] == "OK" and data["results"] and "facts" in data
    bad = good_facts(probe)
    bad["ffmpeg"]["libx264"] = False
    monkeypatch.setattr(probe, "gather_facts", lambda *a, **k: bad)
    assert probe.main([]) == 0                                          # FAIL in the text, still exit code 0
    assert "[FAIL]" in capsys.readouterr().out


def test_probe_main_survives_a_crash_and_bad_arguments(probe, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(probe, "gather_facts", boom)
    assert probe.main([]) == 0
    assert "probe crashed" in capsys.readouterr().out
    assert probe.main(["--no-such-option"]) == 0


def test_probe_es_api_only_allows_read_only_paths(probe):
    for path in ("/reloadgames", "/quit", "/emuend", "/launch", "/systems", "//reloadgames", "/runningGame?x=1", ""):
        with pytest.raises(probe.EsRefused):
            probe.es_get(path)
    requests_seen = []

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return b'{"name": "x"}'

    def opener(req, timeout):
        requests_seen.append((req.get_method(), req.full_url, req.data))
        return FakeResp()

    assert probe.es_get("/", opener=opener)["status"] == 200
    assert probe.es_get("/runningGame", opener=opener)["body"] == '{"name": "x"}'
    assert requests_seen == [("GET", "http://127.0.0.1:1234/", None), ("GET", "http://127.0.0.1:1234/runningGame", None)]


def test_probe_gather_calls_only_the_two_es_paths(probe, monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(probe, "es_get", lambda path, *a, **k: called.append(path) or {"error": "x"})
    facts = probe.gather_facts(userdata=str(tmp_path), data_dir=str(tmp_path / "scrappy"))
    assert called == ["/", "/runningGame"]
    assert probe.evaluate(facts)                                       # and the whole thing can be judged
    json.dumps(facts)                                                  # and printed as JSON


def test_probe_reads_es_config_and_switch_folder(probe, tmp_path):
    ud = tmp_path / "userdata"
    cfgdir = ud / "system/configs/emulationstation"
    cfgdir.mkdir(parents=True)
    (cfgdir / "es_systems_switch.cfg").write_text(
        "<systemList><system><name>switch</name><path>/userdata/roms/switch</path>"
        "<extension>.nsp .NSP .xci</extension></system></systemList>")
    (cfgdir / "es_systems_broken.cfg").write_text("<not xml")
    base = tmp_path / "es_systems.cfg"
    base.write_text("<systemList><system><name>snes</name><path>/x</path><extension>.sfc</extension></system></systemList>")
    es = probe.es_config_facts(str(ud), str(base))
    assert es["switch_system"]["path"] == "/userdata/roms/switch" and ".nsp" in es["switch_system"]["extensions"]
    assert [c["exists"] for c in es["configs"]] == [True, True, True]
    share = tmp_path / "share"
    (share / "switch").mkdir(parents=True)
    (share / "switch" / "gamelist.xml").write_text("<gameList/>")
    (share / "switch" / "images").mkdir()
    (share / "switch" / "images" / "a.png").write_text("x")
    (ud / "roms").mkdir()
    (ud / "roms" / "switch").symlink_to(share / "switch")
    sw = probe.switch_roms_facts(str(ud))
    assert sw["exists"] and sw["is_symlink"] and sw["gamelist"] and sw["images"] == 1 and sw["videos"] is None and sw["writable"]


def test_probe_disk_facts_measures_the_nearest_existing_folder(probe, tmp_path):
    facts = probe.disk_facts(str(tmp_path), str(tmp_path / "does" / "not" / "exist"))
    assert facts["userdata"]["free_mb"] > 0
    assert facts["data_dir"]["measured_at"] == str(tmp_path)


def test_probe_uses_only_the_standard_library():
    tree = ast.parse((REPO / "tools" / "probe_environment.py").read_text())
    top = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            top |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            top.add(node.module.split(".")[0])
    assert top <= set(sys.stdlib_module_names) | {"__future__"}, top - set(sys.stdlib_module_names)
    src = (REPO / "tools" / "probe_environment.py").read_text()
    # the optional libraries are only probed by name (importlib), never imported at module level
    assert "import PyQt5" not in src and "import numpy" not in src


def test_probe_script_runs_as_a_program(tmp_path):
    ud = tmp_path / "userdata"
    ud.mkdir()
    r = subprocess.run([sys.executable, str(REPO / "tools" / "probe_environment.py"), "--userdata", str(ud)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "Overall:" in r.stdout and re.search(r"^\[(OK  |WARN|FAIL)\]", r.stdout, re.M)



# ---------------------------------------------------------------- the vendored Pillow

def load_extractor():
    spec = importlib.util.spec_from_file_location("extract_wheel", REPO / "deploy" / "extract_wheel.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def real_pins():
    return [l.split() for l in (REPO / "deploy/vendor.txt").read_text().splitlines() if l.strip() and not l.lstrip().startswith("#")]


def test_vendor_pin_file_is_well_formed_and_the_wheel_is_not_in_git():
    pins = real_pins()
    assert len(pins) == 2
    versions = set()
    for sha, name, url in pins:
        assert re.fullmatch(r"[0-9a-f]{64}", sha)
        assert re.fullmatch(r"pillow-(\d+(?:\.\d+)*)-cp311-cp311-manylinux[\w.]*\.whl", name), name
        versions.add(re.match(r"pillow-([^-]+)-", name).group(1))
        assert url.startswith("https://files.pythonhosted.org/") and url.endswith("/" + name)
    plat = lambda p: p[1].removesuffix(".whl").rsplit("-", 1)[1]           # the platform tag of the wheel file name
    by_arch = {a: p for p in pins for a in ("x86_64", "aarch64") if a in plat(p)}
    assert sorted(by_arch) == ["aarch64", "x86_64"]                       # exactly one pin per architecture
    assert all(("x86_64" in plat(p)) != ("aarch64" in plat(p)) for p in pins)
    assert versions == {"11.3.0"}                                         # the same Pillow version on both
    assert by_arch["x86_64"][0] == "91da1d88226663594e3f6b4b8c3c8d85bd504117d043740a8e0ec449087cc494"
    assert by_arch["aarch64"][0] == "7d1aa4de119a0ecac0a34a9c8bde33f34022e2e8f99104e47a3ca392fd60e37d"
    tracked = subprocess.run(["git", "ls-files", "*.whl"], cwd=REPO, capture_output=True, text=True).stdout
    assert tracked.strip() == ""
    assert not list((REPO / "deploy").glob("*.whl"))


def test_pinned_wheels_in_a_local_cache_match_their_pins_if_present():
    checked = 0
    for sha, name, _url in real_pins():
        for folder in (Path.home() / ".cache/scrappy/wheels", Path("/tmp/pillow-wheel")):
            if (folder / name).is_file():
                assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == sha
                checked += 1
                break
    if not checked:
        pytest.skip("the real wheels are not in a local cache")


def test_wheel_is_downloaded_once_into_the_cache_and_checked(box):
    box.use_wheel(good_wheel_path(), seed_cache=False)
    r = box.run("--dry-run", STUB_CURL_FILE=str(good_wheel_path()))
    assert r.returncode == 0, r.stdout + r.stderr
    assert box.curl_log.read_text().strip() == f"https://files.pythonhosted.org/packages/test/{WHEEL_NAME}"
    assert (box.cache / WHEEL_NAME).is_file() and "downloaded, sha256 matches the pin" in r.stdout
    box.curl_log.write_text("")
    r = box.run("--dry-run")                                     # second run: cached, no network at all
    assert r.returncode == 0 and box.curl_log.read_text() == "" and "cached, sha256 matches" in r.stdout


def test_tampered_download_is_refused_before_anything_is_sent(box, tmp_path):
    evil = tmp_path / "evil.whl"
    evil.write_bytes(good_wheel_path().read_bytes() + b"tampered")
    box.use_wheel(good_wheel_path(), seed_cache=False)
    r = box.run(STUB_CURL_FILE=str(evil))
    assert r.returncode != 0
    assert "pin says" in r.stdout + r.stderr and "refusing" in r.stdout + r.stderr
    assert not (box.cache / WHEEL_NAME).exists() and not list(box.cache.glob("*.part*"))
    assert [c for _, c in box.commands()] == ["uname -m\n"] and not box.dest.exists()   # only the read-only architecture question


def test_tampered_cached_wheel_is_replaced_by_a_good_download(box, tmp_path):
    (box.cache / WHEEL_NAME).write_bytes(b"corrupted cache entry")
    r = box.run("--dry-run", STUB_CURL_FILE=str(good_wheel_path()))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "does not match the pin" in r.stdout
    assert hashlib.sha256((box.cache / WHEEL_NAME).read_bytes()).hexdigest() == hashlib.sha256(good_wheel_path().read_bytes()).hexdigest()


def test_tampered_cached_wheel_and_no_network_refuses(box):
    (box.cache / WHEEL_NAME).write_bytes(b"corrupted cache entry")
    r = box.run("--dry-run")                                     # the fake curl fails like a dead network
    assert r.returncode != 0 and "download failed" in r.stdout + r.stderr
    assert not (box.cache / WHEEL_NAME).exists()


def test_wrong_pin_in_vendor_txt_refuses_the_wheel(box):
    box.use_wheel(good_wheel_path(), pin_sha="0" * 64)
    r = box.run("--dry-run", STUB_CURL_FILE=str(good_wheel_path()))
    assert r.returncode != 0 and "pin says" in r.stdout + r.stderr


@pytest.mark.parametrize("line", ["", "xyz  a.whl  https://files.pythonhosted.org/a.whl",
                                  "{sha}  ../a.whl  https://files.pythonhosted.org/a.whl",
                                  "{sha}  a.zip  https://files.pythonhosted.org/a.zip",
                                  "{sha}  a.whl  http://files.pythonhosted.org/a.whl",
                                  "{sha}  a.whl  https://evil.example.com/a.whl"])
def test_malformed_vendor_pin_is_refused(box, line):
    box.vendor_file.write_text(line.format(sha="a" * 64) + "\n")
    r = box.run("--dry-run")
    assert r.returncode != 0 and box.commands() == []


@pytest.mark.parametrize("member", ["../evil.txt", "/tmp/evil-abs.txt", "PIL/../../evil.txt", "PIL\\..\\evil.txt", "C:evil.txt"])
def test_zip_slip_wheel_is_refused_on_the_box_side_and_nothing_escapes(box, tmp_path, member):
    installed(box)
    (box.dest / "scrappy" / "still_here.txt").write_text("old")
    wheel = build_wheel(tmp_path / "slip.whl", {member: "pwned"})
    box.use_wheel(wheel)
    r = box.run("--no-probe")
    assert r.returncode != 0
    assert "refused" in r.stdout + r.stderr
    assert (box.dest / "scrappy" / "still_here.txt").read_text() == "old"          # the old install is untouched
    assert not list(tmp_path.rglob("evil*.txt")) and not Path("/tmp/evil-abs.txt").exists()
    assert [p.name for p in box.dest.iterdir() if p.name.startswith(".staging")] == []


def test_wheel_with_a_symlink_member_is_refused(tmp_path):
    ex = load_extractor()
    wheel = tmp_path / "w.whl"
    with zipfile.ZipFile(wheel, "w") as z:
        z.writestr("PIL/__init__.py", "x")
        info = zipfile.ZipInfo("PIL/link")
        info.external_attr = (0o120777 << 16)
        z.writestr(info, "/etc/passwd")
    with pytest.raises(ex.WheelError, match="symlink"):
        ex.extract(wheel, tmp_path / "out", hashlib.sha256(wheel.read_bytes()).hexdigest(), tmp_path / "m")
    assert not (tmp_path / "out").exists()                                # validated before anything is written


def test_extractor_unpacks_only_pil_libs_and_dist_info(tmp_path):
    ex = load_extractor()
    wheel = build_wheel(tmp_path / "w.whl", {"pillow-99.0.data/scripts/x": "x", "PIL/sub/mod.py": "y=1\n"})
    sha = hashlib.sha256(wheel.read_bytes()).hexdigest()
    written = ex.extract(wheel, tmp_path / "vendor", sha, tmp_path / "VENDOR.sha256")
    assert sorted(written) == ["PIL/__init__.py", "PIL/sub/mod.py", "pillow-99.0.dist-info/METADATA", "pillow.libs/libfake-1.so.0"]
    assert not (tmp_path / "vendor" / "some_other_package").exists()
    assert os.access(tmp_path / "vendor" / "pillow.libs" / "libfake-1.so.0", os.X_OK)
    lines = (tmp_path / "VENDOR.sha256").read_text().splitlines()
    assert len(lines) == 4 and all(re.fullmatch(r"[0-9a-f]{64}  vendor/\S+", l) for l in lines)
    with pytest.raises(ex.WheelError, match="already exists"):
        ex.extract(wheel, tmp_path / "vendor", sha, tmp_path / "m2")
    with pytest.raises(ex.WheelError, match="does not match the pin"):
        ex.extract(wheel, tmp_path / "other", "0" * 64, tmp_path / "m3")
    assert not (tmp_path / "other").exists()
    (tmp_path / "junk.whl").write_bytes(b"not a zip")
    with pytest.raises(ex.WheelError, match="not a readable zip"):
        ex.extract(tmp_path / "junk.whl", tmp_path / "o2", hashlib.sha256(b"not a zip").hexdigest(), tmp_path / "m4")


def test_extractor_main_exit_codes(tmp_path):
    ex = load_extractor()
    wheel = build_wheel(tmp_path / "w.whl")
    sha = hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert ex.main(["x", str(wheel), str(tmp_path / "o"), sha, str(tmp_path / "m")]) == 0
    assert ex.main(["x", str(wheel), str(tmp_path / "o3"), "f" * 64, str(tmp_path / "m")]) == 1
    assert ex.main(["x"]) == 1


def test_a_vendored_pillow_that_cannot_do_jpeg_stops_the_install(box, tmp_path):
    installed(box)
    (box.dest / "scrappy" / "still_here.txt").write_text("old")
    (box.dest / "vendor" / "old_vendor.txt").write_text("old")
    box.use_wheel(build_wheel(tmp_path / "hollow.whl", real_pil=False))           # a PIL that has no Image module
    r = box.run("--no-probe")
    assert r.returncode != 0
    assert "JPEG round trip failed" in r.stdout + r.stderr and "PILLOW-FAIL" in r.stdout
    assert (box.dest / "scrappy" / "still_here.txt").exists() and (box.dest / "vendor" / "old_vendor.txt").exists()
    assert not (box.dest / "vendor.previous").exists()


def test_update_and_uninstall_semantics_for_vendor(box):
    snap = box.seed_user_data()
    installed(box)
    installed(box)                                                       # now vendor.previous exists too
    assert (box.dest / "vendor.previous" / "PIL" / "__init__.py").is_file()
    r = box.run("--uninstall")
    assert r.returncode == 0, r.stdout + r.stderr
    for gone in ("vendor", "vendor.previous", "VENDOR.sha256"):
        assert not (box.dest / gone).exists(), gone
    assert box.snapshot() == snap


def test_install_scripts_never_mention_pip_or_unzip(box):
    r = box.run("--dry-run")
    code = "\n".join(l for l in r.stdout.splitlines() if l.startswith("    | ") and not l[6:].lstrip().startswith("#"))
    assert not re.search(r"\b(pip|unzip)\b", code)
    assert "extract_wheel.py" in code


# ---------------------------------------------------------------- the architecture decides which wheel is shipped

def tar_names(box):
    with tarfile.open(box.sandbox / "captured.tar.gz") as tf:
        return [m.name for m in tf.getmembers()]


def stage_script(box):
    """The prelude+stage script that the fake ssh received (the one that unpacks the wheel)."""
    return next(c for _, c in box.commands() if "extract_wheel.py" in c and "WHEEL_NAME=" in c)


def test_x86_64_target_ships_the_x86_64_wheel(box):
    box.use_both_wheels()
    r = box.run("--no-probe", "alice@box.local", STUB_UNAME="x86_64")
    assert r.returncode == 0, r.stdout + r.stderr
    assert box.commands()[0] == ("alice@box.local", "uname -m\n")          # asked the machine first, read-only
    assert f"pinned wheel for x86_64: {WHEEL_NAME}" in r.stdout and "architecture: x86_64 (read from alice@box.local" in r.stdout
    names = tar_names(box)
    assert "wheels/" + WHEEL_NAME in names and "wheels/" + WHEEL_NAME_ARM not in names
    assert f"WHEEL_NAME='{WHEEL_NAME}'" in stage_script(box)
    assert (box.dest / "vendor" / "PIL" / "__init__.py").is_file() and not (box.dest / "vendor" / ARM_MARKER).exists()


@pytest.mark.parametrize("machine", ["aarch64", "arm64"])
def test_aarch64_target_ships_the_aarch64_wheel(box, machine):
    box.use_both_wheels()
    r = box.run("--no-probe", "alice@box.local", STUB_UNAME=machine)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"pinned wheel for aarch64: {WHEEL_NAME_ARM}" in r.stdout
    names = tar_names(box)
    assert "wheels/" + WHEEL_NAME_ARM in names and "wheels/" + WHEEL_NAME not in names
    assert f"WHEEL_NAME='{WHEEL_NAME_ARM}'" in stage_script(box)
    assert (box.dest / "vendor" / ARM_MARKER).is_file()                    # the aarch64 wheel is the one that was unpacked
    assert not list(box.dest.rglob("*.whl")) and box.curl_log.read_text() == ""


def test_a_vendor_file_with_one_pin_works_for_the_matching_architecture(box):
    box.vendor_file.write_text(box.pin_line(WHEEL_NAME_ARM, arm_wheel_path()))
    shutil.copy(arm_wheel_path(), box.cache / WHEEL_NAME_ARM)
    r = box.run("--no-probe", "alice@box.local", STUB_UNAME="aarch64")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (box.dest / "vendor" / ARM_MARKER).is_file()


@pytest.mark.parametrize("machine", ["armv7l", "i686", "riscv64", ""])
def test_unsupported_architecture_fails_before_anything_is_built_or_uploaded(box, machine):
    box.use_both_wheels()
    r = box.run("--no-probe", "alice@box.local", STUB_UNAME=machine or " ")
    out = r.stdout + r.stderr
    assert r.returncode != 0
    assert "no Pillow wheel is pinned for it in deploy/vendor.txt" in out and "nothing was changed" in out
    if machine:
        assert f"'{machine}'" in out
    assert [c for _, c in box.commands()] == ["uname -m\n"]                # nothing but the read-only question
    assert not (box.sandbox / "captured.tar.gz").exists() and not box.dest.exists()
    assert not list(box.tmp.glob("scrappy-pkg.*"))                              # no package was even started
    assert box.curl_log.read_text() == ""


def test_architecture_from_the_machine_is_printed_safely(box):
    r = box.run("--no-probe", "alice@box.local", STUB_UNAME="evil\x1b[31mred")
    assert r.returncode != 0 and "\x1b" not in r.stdout + r.stderr


def test_ssh_failure_while_asking_the_architecture_stops_everything(box):
    r = box.run("--no-probe", "alice@box.local", STUB_SSH_FAIL="1")
    assert r.returncode != 0
    assert "step failed on alice@box.local" in r.stdout + r.stderr and "No route to host" in r.stderr
    assert [c for _, c in box.commands()] == ["uname -m\n"] and not box.dest.exists()
    assert not list(box.tmp.glob("scrappy-pkg.*")) and box.curl_log.read_text() == ""


def test_dry_run_uses_scrappy_arch_says_so_and_makes_no_ssh_call(box):
    box.use_both_wheels()
    r = box.run("--dry-run", "alice@box.local")                              # default: x86_64
    assert r.returncode == 0, r.stdout + r.stderr
    assert "architecture: x86_64 (taken from SCRAPPY_ARCH, default x86_64; NOT read from alice@box.local)" in r.stdout
    assert f"pinned wheel for x86_64: {WHEEL_NAME}" in r.stdout and f"WHEEL_NAME='{WHEEL_NAME}'" in r.stdout
    for value in ("aarch64", "arm64"):
        r = box.run("--dry-run", "alice@box.local", SCRAPPY_ARCH=value, STUB_UNAME="x86_64")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "architecture: aarch64 (taken from SCRAPPY_ARCH" in r.stdout and "NOT read from alice@box.local" in r.stdout
        assert f"pinned wheel for aarch64: {WHEEL_NAME_ARM}" in r.stdout and f"WHEEL_NAME='{WHEEL_NAME_ARM}'" in r.stdout
    assert box.commands() == []                                              # the fake ssh was never called, in any of them


def test_dry_run_with_an_unsupported_scrappy_arch_fails(box):
    r = box.run("--dry-run", SCRAPPY_ARCH="armv7l")
    assert r.returncode != 0 and "SCRAPPY_ARCH='armv7l'" in r.stdout + r.stderr and box.commands() == []


def test_a_real_run_ignores_scrappy_arch(box):
    box.use_both_wheels()
    r = box.run("--no-probe", "alice@box.local", STUB_UNAME="x86_64", SCRAPPY_ARCH="aarch64")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SCRAPPY_ARCH is set but is ignored in a real run" in r.stdout
    names = tar_names(box)
    assert "wheels/" + WHEEL_NAME in names and "wheels/" + WHEEL_NAME_ARM not in names
    assert not (box.dest / "vendor" / ARM_MARKER).exists()
    # and the other way round: a machine that says aarch64 gets the aarch64 wheel whatever SCRAPPY_ARCH says
    r = box.run("--no-probe", "alice@box.local", STUB_UNAME="aarch64", SCRAPPY_ARCH="x86_64")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "wheels/" + WHEEL_NAME_ARM in tar_names(box) and (box.dest / "vendor" / ARM_MARKER).is_file()


def test_uninstall_does_not_ask_for_the_architecture(box):
    installed(box)
    n = len(box.commands())
    r = box.run("--uninstall", "alice@box.local", STUB_UNAME="armv7l")
    assert r.returncode == 0, r.stdout + r.stderr
    assert all(c != "uname -m\n" for _, c in box.commands()[n:])


@pytest.mark.parametrize("bad_name", ["pillow-99.0-cp311-cp311-win_amd64.whl", "pillow-99.0-cp311-cp311-macosx_11_0_arm64.whl",
                                      "pillow-99.0-cp311-cp311-manylinux_2_17_i686.whl", "pillow-99.0-cp311-cp311-any.whl",
                                      "pillow-99.0-cp311-cp311-manylinux_x86_64.manylinux_aarch64.whl"])
def test_a_pin_with_an_unknown_platform_tag_is_rejected_even_next_to_a_good_one(box, bad_name):
    box.vendor_file.write_text(box.pin_line(WHEEL_NAME, good_wheel_path()) + box.pin_line(bad_name, good_wheel_path()))
    shutil.copy(good_wheel_path(), box.cache / bad_name)
    r = box.run("--dry-run")
    assert r.returncode != 0 and "cannot tell the architecture" in r.stdout + r.stderr and bad_name in r.stdout + r.stderr
    r = box.run("--no-probe", "alice@box.local")
    assert r.returncode != 0 and not box.dest.exists() and [c for _, c in box.commands()] == ["uname -m\n"]


def test_two_pins_for_one_architecture_are_rejected(box):
    box.vendor_file.write_text(box.pin_line(WHEEL_NAME, good_wheel_path())
                               + box.pin_line("pillow-98.0-cp311-cp311-manylinux_2_17_x86_64.whl", good_wheel_path()))
    r = box.run("--dry-run")
    assert r.returncode != 0 and "two wheels are pinned for x86_64" in r.stdout + r.stderr


def test_missing_pin_for_the_machines_architecture_is_refused_before_the_upload(box):
    r = box.run("--no-probe", "alice@box.local", STUB_UNAME="aarch64")        # the default test vendor.txt only pins x86_64
    out = r.stdout + r.stderr
    assert r.returncode != 0 and "no Pillow wheel is pinned for aarch64" in out and "nothing was changed" in out
    assert [c for _, c in box.commands()] == ["uname -m\n"] and not (box.sandbox / "captured.tar.gz").exists()


# ---------------------------------------------------------------- the probe's Pillow checks

def pillow_facts_ok(version="11.3.0", file="/x/vendor/PIL/__init__.py"):
    return {"version": version, "file": file, "jpeg": True, "png": True, "webp": True, "under_vendor": True}


JPEG_BROKEN = {"version": "10.3.0", "file": "/usr/lib/python3.11/site-packages/PIL/__init__.py", "jpeg": False, "png": True,
               "webp": True, "jpeg_error": "OSError: Wrong JPEG library version: library is 80, caller expects 90"}


def test_probe_pillow_system_broken_but_vendored_works(probe):
    f = good_facts(probe)
    f["pillow"] = {"vendor_dir": "/x/vendor", "system": JPEG_BROKEN, "vendored": pillow_facts_ok()}
    res = {r["name"]: r for r in probe.evaluate(f)}
    system = res["Pillow JPEG round trip (system)"]
    assert system["status"] == "WARN" and "library is 80" in system["detail"] and "80 vs 90" in system["detail"]
    assert res["Pillow JPEG round trip (vendored)"]["status"] == "OK" and "11.3.0" in res["Pillow JPEG round trip (vendored)"]["detail"]
    assert res["Pillow PNG/WEBP support"]["status"] == "OK" and "WEBP supported" in res["Pillow PNG/WEBP support"]["detail"]


def test_probe_pillow_both_broken_or_vendor_missing_is_a_fail(probe):
    f = good_facts(probe)
    f["pillow"] = {"vendor_dir": "/x/vendor", "system": JPEG_BROKEN, "vendored": dict(pillow_facts_ok(), jpeg=False, jpeg_error="boom")}
    res = {r["name"]: r["status"] for r in probe.evaluate(f)}
    assert res["Pillow JPEG round trip (system)"] == "FAIL" and res["Pillow JPEG round trip (vendored)"] == "FAIL"
    assert res["Pillow PNG/WEBP support"] == "FAIL"
    f["pillow"] = {"vendor_dir": "/x/vendor", "system": JPEG_BROKEN, "vendored": None}
    res = {r["name"]: r for r in probe.evaluate(f)}
    assert res["Pillow JPEG round trip (system)"]["status"] == "FAIL"
    assert res["Pillow JPEG round trip (vendored)"]["status"] == "FAIL" and "install.sh" in res["Pillow JPEG round trip (vendored)"]["detail"]


def test_probe_pillow_vendored_copy_must_be_the_one_that_loads(probe):
    f = good_facts(probe)
    f["pillow"] = {"vendor_dir": "/x/vendor", "system": pillow_facts_ok("10.3.0", "/usr/lib/PIL/__init__.py"),
                   "vendored": dict(pillow_facts_ok(), under_vendor=False, file="/usr/lib/PIL/__init__.py")}
    res = {r["name"]: r for r in probe.evaluate(f)}
    assert res["Pillow JPEG round trip (vendored)"]["status"] == "FAIL" and "not the vendored one" in res["Pillow JPEG round trip (vendored)"]["detail"]


def test_probe_pillow_system_works_and_no_vendor_is_fine(probe):
    f = good_facts(probe)
    f["pillow"] = {"vendor_dir": "/x/vendor", "system": pillow_facts_ok("12.0", "/s/PIL/__init__.py"), "vendored": None}
    res = {r["name"]: r["status"] for r in probe.evaluate(f)}
    assert res["Pillow JPEG round trip (system)"] == "OK" and res["Pillow JPEG round trip (vendored)"] == "OK"


def test_probe_pillow_png_failure_and_no_webp(probe):
    f = good_facts(probe)
    f["pillow"] = {"vendor_dir": "/v", "system": pillow_facts_ok(), "vendored": dict(pillow_facts_ok(), png=False, png_error="bad", webp=False)}
    res = {r["name"]: r for r in probe.evaluate(f)}
    assert res["Pillow PNG/WEBP support"]["status"] == "FAIL" and "bad" in res["Pillow PNG/WEBP support"]["detail"]
    f["pillow"]["vendored"] = dict(pillow_facts_ok(), webp=False)
    res = {r["name"]: r for r in probe.evaluate(f)}
    assert res["Pillow PNG/WEBP support"]["status"] == "OK" and "not supported" in res["Pillow PNG/WEBP support"]["detail"]


def test_pillow_check_reads_the_child_answer_and_survives_trouble(probe):
    class P:
        def __init__(self, out, err="", rc=0):
            self.stdout, self.stderr, self.returncode = out, err, rc

    ok = probe.pillow_check(run=lambda *a, **k: P('noise\n{"version": "1", "jpeg": true, "png": true, "webp": false}\n'))
    assert ok["jpeg"] is True and ok["version"] == "1"
    crashed = probe.pillow_check(run=lambda *a, **k: P("", "Segmentation fault\n", -11))
    assert crashed["jpeg"] is False and "crashed" in crashed["error"] and "Segmentation" in crashed["error"]
    assert probe.pillow_check(run=lambda *a, **k: P("{not json"))["jpeg"] is False

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("x", 1)
    assert "timed out" in probe.pillow_check(run=timeout)["error"]
    seen = {}

    def spy(cmd, **kw):
        seen.update(cmd=cmd, env=kw["env"])
        return P('{"jpeg": true, "png": true}')
    probe.pillow_check(pythonpath="/v", run=spy)
    assert seen["env"]["PYTHONPATH"] == "/v" and "-I" not in seen["cmd"]
    probe.pillow_check(isolated=True, run=spy)
    assert "-I" in seen["cmd"] and "PYTHONPATH" not in seen["env"]


def test_pillow_check_really_round_trips_a_jpeg_here(probe):
    r = probe.pillow_check(isolated=True)
    assert r["jpeg"] is True and r["png"] is True and r["version"] and r["file"].endswith("__init__.py")


def test_pillow_check_command_strict_mode(probe, tmp_path, capsys):
    empty = tmp_path / "empty_vendor"
    empty.mkdir()
    assert probe.main(["--pillow-check", "--vendor-dir", str(empty), "--strict"]) == 1          # falls back to the system PIL
    out = capsys.readouterr().out
    assert "NOT the vendored copy" in out and "PILLOW-FAIL" in out
    assert probe.main(["--pillow-check", "--vendor-dir", str(empty)]) == 0                       # without --strict: report only
    vendor = tmp_path / "vendor"
    copy_real_pil(vendor)
    capsys.readouterr()
    assert probe.main(["--pillow-check", "--vendor-dir", str(vendor), "--strict"]) == 0
    out = capsys.readouterr().out
    assert "PILLOW-OK" in out and str(vendor) in out and "JPEG save+open+pixel check: ok" in out


def test_probe_gather_includes_pillow_facts_for_a_vendor_folder(probe, monkeypatch, tmp_path):
    data = tmp_path / "scrappy"
    copy_real_pil(data / "vendor")
    monkeypatch.setattr(probe, "es_get", lambda *a, **k: {"error": "x"})
    facts = probe.gather_facts(userdata=str(tmp_path), data_dir=str(data))
    assert facts["pillow"]["vendored"]["jpeg"] is True and facts["pillow"]["vendored"]["under_vendor"] is True
    assert facts["pillow"]["system"]["jpeg"] is True
    res = {r["name"]: r["status"] for r in probe.evaluate(facts)}
    assert res["Pillow JPEG round trip (vendored)"] == "OK"


def test_install_with_a_full_pil_tree_including_native_libraries(box, tmp_path):
    """One slow test with a complete PIL copy (the real wheel unpacks to ~25 MB of .so files and bundled libraries)."""
    full = build_wheel(tmp_path / "full.whl", real_pil="full")
    box.use_wheel(full)
    r = box.run("--no-probe")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PILLOW-OK" in r.stdout
    so = [p for p in (box.dest / "vendor" / "PIL").rglob("*") if ".so" in p.name or p.suffix == ".dylib"]
    assert so and all(os.access(p, os.X_OK) for p in so)
