"""Tests of the two on-box tools: tools/verify_media_naming.py (read-only naming check) and tools/box_selftest.py (sandboxed
end-to-end self-test). Both are run as the user would run them: as scripts in a child process."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VERIFY = REPO / "tools" / "verify_media_naming.py"
SELFTEST = REPO / "tools" / "box_selftest.py"
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
HAVE_QT = importlib.util.find_spec("PyQt5") is not None


def load_tool(path: Path, name: str):
    """Import a tool script as a module without leaving its global side effects (bytecode switch) behind."""
    old = sys.dont_write_bytecode
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.dont_write_bytecode = old


def snapshot(root: Path) -> dict:
    """Every entry below root with content, size and mtime, to prove a tool changed nothing."""
    out = {}
    for dirpath, dirs, files in os.walk(root):
        for name in dirs + files:
            full = os.path.join(dirpath, name)
            st = os.lstat(full)
            body = None if stat.S_ISDIR(st.st_mode) else Path(full).read_bytes()
            out[os.path.relpath(full, root)] = (st.st_mode, st.st_size, st.st_mtime_ns, body)
    return out


def tree(root: Path) -> dict:
    """Relative path -> content (None for folders); folder mtimes are not compared (a created-and-removed file moves them)."""
    return {rel: v[3] for rel, v in snapshot(root).items()}


# ============================================================ verify_media_naming.py

GAMELIST = """<?xml version="1.0"?>
<gameList>
\t<game><path>./Plain Game.nsp</path>
\t\t<image>./images/Plain Game-image.png</image><thumbnail>./images/Plain Game-thumb.png</thumbnail>
\t\t<marquee>./images/Plain Game-marquee.png</marquee><video>./videos/Plain Game-video.mp4</video></game>
\t<game><path>./Our Game.nsp</path><image>./images/Our Game-image.jpg</image><thumbnail>./images/Our Game-thumb.png</thumbnail></game>
\t<game><path>./Sub Folder/Nested [01].nsp</path><image>./images/Sub Folder/Nested [01]-image.png</image></game>
\t<game><path>./Dotted.Name.v1.2.nsp</path><image>./images/Dotted.Name.v1.2-image.png</image></game>
\t<game><path>./Ünïcøde.nsp</path><image>./images/Ünïcøde-image.png</image></game>
\t<game><path>./Missing File.nsp</path><image>./images/Missing File-image.png</image></game>
\t<game><path>./Empty File.nsp</path><image>./images/Empty File-image.png</image></game>
\t<game><path>./Weird.nsp</path><image>./images/something-else.png</image></game>
\t<game><path>./Other Role.nsp</path><thumbnail>./images/Other Role-boxart.png</thumbnail></game>
\t<game><path>./No Media.nsp</path><name>No Media</name></game>
\t<game><name>No path at all</name></game>
</gameList>
"""
PRESENT = ["images/Plain Game-image.png", "images/Plain Game-thumb.png", "images/Plain Game-marquee.png", "videos/Plain Game-video.mp4",
           "images/Our Game-image.jpg", "images/Our Game-thumb.png", "images/Sub Folder/Nested [01]-image.png",
           "images/Dotted.Name.v1.2-image.png", "images/Ünïcøde-image.png", "images/something-else.png",
           "images/Other Role-boxart.png",
           "images/No Media-image.jpg"]           # exists with OUR name but the gamelist does not mention it


@pytest.fixture
def system(tmp_path):
    folder = tmp_path / "roms" / "switch"
    folder.mkdir(parents=True)
    (folder / "gamelist.xml").write_text(GAMELIST, encoding="utf-8")
    for rel in PRESENT:
        target = folder / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"media")
    (folder / "images" / "Empty File-image.png").write_bytes(b"")
    return folder


@pytest.fixture(scope="module")
def verifier():
    return load_tool(VERIFY, "verify_media_naming_tool")


def run_verify(*args, cwd=None):
    return subprocess.run([sys.executable, str(VERIFY), *map(str, args)], capture_output=True, text=True, cwd=cwd, timeout=60)


def test_classification_of_the_observed_values(system, verifier):
    r = verifier.analyse(str(system))
    assert r["status"] == "ok" and r["system"] == "switch"
    assert r["games"] == 11 and r["games_without_path"] == 1 and r["games_with_media"] == 9
    # entries: Plain 4 + Our 2 + Nested 1 + Dotted 1 + Unicode 1 + Missing 1 + Empty 1 + Weird 1 + Other Role 1
    assert r["entries"] == 13
    assert r["classes"] == {"exact": 5, "extension": 5, "suffix": 1, "folder": 1, "other": 1}
    assert r["by_tag"]["image"] == {"exact": 1, "extension": 5, "suffix": 0, "folder": 1, "other": 1}
    assert r["by_tag"]["thumbnail"]["exact"] == 2 and r["by_tag"]["thumbnail"]["suffix"] == 1
    assert r["by_tag"]["marquee"]["exact"] == 1 and r["by_tag"]["video"]["exact"] == 1
    assert r["exact_pct"] == round(100 * 5 / 13, 1) and r["compatible_pct"] == round(100 * 11 / 13, 1)
    assert r["files"] == {"ok": 11, "missing": 1, "empty": 1}
    assert [m["rom"] for m in r["missing_files"] if m["file"] == "missing"] == ["./Missing File.nsp"]
    assert r["untagged_files_we_would_use"] == 1, "the file with our name that the gamelist does not list must be counted"


def test_patterns_per_kind_of_rom_name(system, verifier):
    r = verifier.analyse(str(system))
    patterns = {cat: {p["pattern"] for p in r["patterns"][cat]} for cat in r["patterns"]}
    assert "./images/{stem}-image.png" in patterns["all"] and "./images/{stem}-image.jpg" in patterns["all"]
    assert patterns["sub-folder"] == {"./images/{sub}/{stem}-image.png"}, "a mirrored sub-folder must be named as such"
    assert "./images/{stem}-image.png" in patterns["dots"]
    assert patterns["brackets"] == {"./images/{sub}/{stem}-image.png"}
    assert patterns["non-ascii"] == {"./images/{stem}-image.png"}
    assert r["category_compatible"]["sub-folder"] == {"compatible": 0, "entries": 1, "pct": 0.0}
    assert r["category_compatible"]["non-ascii"]["pct"] == 100.0
    top = r["patterns"]["all"][0]
    assert top["example"] and "  ->  " in top["example"]
    text = " ".join(r["conclusions"])
    assert "sub-folders: 0 of 1" in text and "./images/{sub}/{stem}-image.png" in text


def test_pure_functions(verifier):
    c = verifier.classify
    assert c("./images/A-image.jpg", "./images/A-image.jpg", "A") == "exact"
    assert c("images\\A-image.jpg", "./images/A-image.jpg", "A") == "exact"
    assert c("./images/A-image.png", "./images/A-image.jpg", "A") == "extension"
    assert c("./images/A-boxart.png", "./images/A-image.jpg", "A") == "suffix"
    assert c("./images/Sub/A-image.png", "./images/A-image.jpg", "A") == "folder"
    assert c("./pics/zzz.png", "./images/A-image.jpg", "A") == "other"
    assert verifier.pattern_of("./images/Sub/B-image.png", "./Sub/B.nsp") == "./images/{sub}/{stem}-image.png"
    assert verifier.pattern_of("./images/B.nsp-image.png", "./B.nsp") == "./images/{romfile}-image.png"
    assert verifier.categories_of("./Sub/Game [1].v2.nsp") == ["all", "sub-folder", "brackets", "dots"]
    assert verifier.categories_of("./Ü.nsp") == ["all", "non-ascii"]


def test_cli_text_and_json_agree(system):
    text = run_verify(system)
    assert text.returncode == 0, text.stderr
    assert "13 media entries" in text.stdout and "patterns, sub-folder" in text.stdout and "conclusion:" in text.stdout
    as_json = run_verify(system, "--json")
    assert as_json.returncode == 0
    data = json.loads(as_json.stdout)
    assert data["read_only"] is True and data["exit_code"] == 0
    assert data["systems"][0]["entries"] == 13 and data["systems"][0]["classes"]["exact"] == 5
    assert data["systems"][0]["patterns"]["non-ascii"][0]["example"].startswith("./Ünïcøde.nsp")


def test_it_never_writes_anything(system, tmp_path):
    before = snapshot(tmp_path)
    modes = []
    for p in [tmp_path / "roms", *sorted((tmp_path / "roms").rglob("*"))]:       # also make everything read-only
        modes.append((p, p.stat().st_mode))
    try:
        for p, mode in reversed(modes):
            p.chmod(mode & ~0o222)
        assert run_verify(system).returncode == 0
        assert run_verify(system, "--json").returncode == 0
        assert run_verify("--all", "--roms-root", tmp_path / "roms").returncode == 0
    finally:
        for p, mode in modes:
            p.chmod(mode)
    assert snapshot(tmp_path) == before, "the verifier changed something (content, size, mtime, or created/removed a file)"


def test_all_flag_finds_every_system_with_a_gamelist(system, tmp_path):
    other = tmp_path / "roms" / "snes"
    other.mkdir()
    (other / "gamelist.xml").write_text("<gameList><game><path>./A.sfc</path><image>./images/A-image.png</image></game></gameList>")
    (tmp_path / "roms" / "no-gamelist-here").mkdir()
    out = run_verify("--all", "--roms-root", tmp_path / "roms", "--json")
    data = json.loads(out.stdout)
    assert sorted(s["system"] for s in data["systems"]) == ["snes", "switch"]
    assert out.returncode == 0


@pytest.mark.parametrize("content,word", [(None, "not found"), (b"", "empty"), (b"<gameList><game>", "damaged"),
                                          (b"\xff\xfe garbage", "damaged"), (b"<other/>", "not a game list")])
def test_missing_or_damaged_gamelist_is_reported_not_raised(tmp_path, content, word):
    folder = tmp_path / "switch"
    folder.mkdir()
    if content is not None:
        (folder / "gamelist.xml").write_bytes(content)
    for extra in ([], ["--json"]):
        out = run_verify(folder, *extra)
        assert out.returncode == 2, out.stdout + out.stderr
        assert "Traceback" not in out.stderr and "Traceback" not in out.stdout
        assert word in out.stdout
    assert sorted(p.name for p in folder.iterdir()) == ([] if content is None else ["gamelist.xml"])


def test_not_a_folder_and_default_path(tmp_path):
    out = run_verify(tmp_path / "nothing-here")
    assert out.returncode == 2 and "is not a folder" in out.stdout
    default = run_verify()                                      # /userdata/roms/switch: not on this machine
    assert default.returncode == 2 and "/userdata/roms/switch" in default.stdout


def test_fail_under(system):
    assert run_verify(system, "--fail-under", "10").returncode == 0
    assert run_verify(system, "--fail-under", "90").returncode == 1


def test_a_clean_library_says_identical(tmp_path):
    folder = tmp_path / "switch"
    (folder / "images").mkdir(parents=True)
    (folder / "images" / "G-image.jpg").write_bytes(b"x")
    (folder / "gamelist.xml").write_text("<gameList><game><path>./G.nsp</path><image>./images/G-image.jpg</image></game></gameList>")
    data = json.loads(run_verify(folder, "--json").stdout)["systems"][0]
    assert data["exact_pct"] == 100.0 and "IDENTICAL" in data["conclusions"][0]


def test_works_from_any_folder_without_pythonpath(system, tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    out = subprocess.run([sys.executable, str(VERIFY), str(system)], capture_output=True, text=True, cwd=tmp_path, env=env, timeout=60)
    assert out.returncode == 0, out.stderr


# ============================================================ box_selftest.py

@pytest.fixture(scope="module")
def selftest():
    return load_tool(SELFTEST, "box_selftest_tool")


def run_selftest(tmp_path, *args, path=None, name="parent"):
    parent = tmp_path / name
    parent.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["TMPDIR"] = str(tmp_path / "systmp")
    env["HOME"] = str(tmp_path / "home")
    (tmp_path / "systmp").mkdir(exist_ok=True)
    (tmp_path / "home").mkdir(exist_ok=True)
    if path is not None:
        env["PATH"] = path
    started = time.monotonic()
    out = subprocess.run([sys.executable, str(SELFTEST), "--json", "--sandbox-parent", str(parent), *args],
                         capture_output=True, text=True, env=env, timeout=300)
    return out, time.monotonic() - started, parent


def statuses(out):
    return {c["name"]: c["status"] for c in json.loads(out.stdout)["checks"]}


def test_selftest_full_run_is_quick_and_leaves_nothing_behind(tmp_path):
    (tmp_path / "canary").mkdir()
    (tmp_path / "canary" / "precious.txt").write_text("keep me")
    for name in ("parent", "systmp", "home"):
        (tmp_path / name).mkdir()
    before = tree(tmp_path)
    out, seconds, parent = run_selftest(tmp_path, "--skip-missing")
    assert out.returncode == 0, out.stderr[-3000:]
    data = json.loads(out.stdout)
    assert data["ok"] is True and data["failed"] == 0 and data["sandbox"] is None
    assert "BOX SELFTEST OK" in out.stderr
    assert seconds < 90, f"the self-test took {seconds:.0f} s"
    # nothing outside the sandbox, and the sandbox itself is gone: parent, system temp folder, home and canary are as before
    assert tree(tmp_path) == before, "the self-test left files behind or changed something outside its sandbox"
    assert list(parent.iterdir()) == []
    assert list((tmp_path / "systmp").iterdir()) == []

    st = statuses(out)
    for name, status in st.items():
        assert status in ("PASS", "SKIP"), (name, status)
        if name.startswith(("env.", "images.", "saver.", "download.")) and name != "env.ffmpeg":
            assert status == "PASS", (name, status)
    assert st["env.ffmpeg"] == st["video.encode"] == ("PASS" if HAVE_FFMPEG else "SKIP")
    assert st["qt.offscreen_app"] == st["qt.app_modules_import"] == ("PASS" if HAVE_QT else "SKIP")
    assert st["network.steam"] == st["network.https_download"] == st["youtube.search"] == "SKIP"
    skipped = {c["name"]: c["detail"] for c in data["checks"] if c["status"] == "SKIP"}
    assert "--network" in skipped["network.steam"] and "--youtube" in skipped["youtube.search"]
    for c in data["checks"]:
        assert c["seconds"] >= 0 and c["detail"], c
    assert data["environment"]["pillow"]["file"] and data["environment"]["pillow"]["version"]


def test_selftest_keep_leaves_the_sandbox_only_inside_its_parent(tmp_path):
    out, _, parent = run_selftest(tmp_path, "--keep", "--only", "saver.create,images.jpg,download.fetch")
    assert out.returncode == 0, out.stderr[-3000:]
    kept = Path(json.loads(out.stdout)["sandbox"])
    try:
        assert kept.parent == parent.resolve() and kept.is_dir() and kept.name.startswith("scrappy-selftest-")
        assert (kept / "saver-create" / "switch" / "images" / "Tidepool Tactics-image.jpg").is_file()
        assert (kept / "saver-create" / "switch" / "gamelist.xml").is_file()
        assert (kept / "tmp").is_dir()
        assert list((tmp_path / "systmp").iterdir()) == []
    finally:
        shutil.rmtree(kept, ignore_errors=True)
    assert sorted(statuses(out)) == ["download.fetch_and_cache", "images.jpg", "saver.create"]


def test_selftest_without_ffmpeg_fails_loudly_unless_told_to_skip(tmp_path):
    empty_path = str(tmp_path / "emptybin")
    os.makedirs(empty_path)
    only = ("--only", "env.ffmpeg,video,saver.create")
    bad, _, _ = run_selftest(tmp_path, *only, path=empty_path, name="p1")
    assert bad.returncode == 1
    failed = {c["name"]: c["detail"] for c in json.loads(bad.stdout)["checks"] if c["status"] == "FAIL"}
    assert set(failed) >= {"env.ffmpeg", "video.encode", "video.clamp"} and "not found" in failed["env.ffmpeg"]
    assert "BOX SELFTEST FAILED" in bad.stderr
    good, _, _ = run_selftest(tmp_path, "--skip-missing", *only, path=empty_path, name="p2")
    assert good.returncode == 0, good.stderr[-2000:]
    st = statuses(good)
    assert st["video.encode"] == st["env.ffmpeg"] == "SKIP" and st["saver.create"] == "PASS", "the saver must work with a placeholder video"


def test_list_and_only(tmp_path):
    listed = subprocess.run([sys.executable, str(SELFTEST), "--list"], capture_output=True, text=True, timeout=60)
    assert listed.returncode == 0
    names = [line.split()[0] for line in listed.stdout.splitlines()]
    assert {"env.jpeg_roundtrip", "images.remove_background", "video.encode", "saver.stale_gamelist", "saver.symlink_refused",
            "download.redirects", "qt.offscreen_app", "network.steam", "youtube.search"} <= set(names)
    assert "(opt-in: --network)" in listed.stdout


def test_sandbox_inside_roms_is_refused(selftest, tmp_path):
    with pytest.raises(SystemExit):
        selftest.refuse_if_protected(Path("/userdata/roms/switch/tmp"))
    with pytest.raises(SystemExit):
        selftest.refuse_if_protected(Path("/userdata/roms"))
    selftest.refuse_if_protected(tmp_path)
    out = subprocess.run([sys.executable, str(SELFTEST), "--sandbox-parent", "/userdata/roms"], capture_output=True, text=True, timeout=60)
    assert out.returncode != 0 and not os.path.exists("/userdata/roms")


def make_ctx(selftest, tmp_path, **flags):
    args = types.SimpleNamespace(skip_missing=False, network=False, youtube=False, verbose=False, ytdlp_dir=None, **flags)
    (tmp_path / "tmp").mkdir(exist_ok=True)
    return selftest.Ctx(tmp_path, args)


def test_the_jpeg_check_fails_loudly_with_a_hint_when_pillow_cannot_do_jpeg(selftest, tmp_path, monkeypatch):
    from PIL import Image
    real_save = Image.Image.save

    def broken(self, fp, format=None, **params):
        if format == "JPEG":
            raise OSError("Wrong JPEG library version: library is 80, caller expects 90")
        return real_save(self, fp, format, **params)

    monkeypatch.setattr(Image.Image, "save", broken)
    with pytest.raises(AssertionError) as caught:
        selftest.t_env_jpeg(make_ctx(selftest, tmp_path))
    message = str(caught.value)
    assert "Wrong JPEG library version" in message and "vendor" in message and "PYTHONPATH" in message


def test_the_checks_are_not_vacuous(selftest, tmp_path, monkeypatch):
    import scrappy.download
    monkeypatch.setattr(scrappy.download, "is_public_address", lambda ip: True)
    with pytest.raises(AssertionError):
        selftest.t_download_policy(make_ctx(selftest, tmp_path))
    import scrappy.saver
    monkeypatch.setattr(scrappy.saver, "_inside", lambda path, root_real: True)       # the confinement to the system folder, removed
    with pytest.raises(AssertionError):
        selftest.t_saver_symlink(make_ctx(selftest, tmp_path))


def test_environment_report_names_the_pillow_that_was_imported(selftest, tmp_path):
    ctx = make_ctx(selftest, tmp_path)
    detail = selftest.t_env_pillow(ctx)
    import PIL
    assert PIL.__version__ in detail and os.path.dirname(PIL.__file__) in detail
    assert any(PIL.__file__ in n for n in ctx.notes)
