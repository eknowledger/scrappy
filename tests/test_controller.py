import http.server
import os
import re
import socket
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

import scrappy.controller as controller_mod
from scrappy.config import load_config
from scrappy.controller import Controller, ControllerError, RomRow, SearchResults, looks_like_picture
from scrappy.models import AssetCandidate, GameInfo, Match
from scrappy.processing.images import make_boxart_png, make_image_jpg, make_logo_png
from scrappy.providers.base import Provider, ProviderError, describe_error
from scrappy.saver import SaveError, SaveRequest, UndoRefused

from .test_video import make_source, needs_ffmpeg

ES = """<?xml version="1.0"?><systemList>
<system><name>switch</name><extension>.nsp .xci</extension></system></systemList>"""
SAMPLE_ROM = "Tidepool Tactics [0100AAAA00000000][v0].nsp"
YT_URL = "https://www.youtube.com/watch?v=abcdefghijk"

JPG = make_image_jpg(Image.new("RGB", (320, 180), "red"))
THUMB = make_boxart_png(Image.new("RGB", (100, 160), "blue"))[0]
LOGO = make_logo_png(Image.new("RGBA", (60, 20), (255, 255, 255, 255)))
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def public_resolver(host, port):
    return ["93.184.216.34"]                       # no DNS in tests: every name is a public address


def aged(path, days=3):
    t = time.time() - days * 86400
    os.utime(path, (t, t), follow_symlinks=False)


def plain(exc) -> str:
    """What the user sees; it must be a sentence, not 'ValueError: ...'."""
    text = describe_error(exc)
    assert not re.match(r"\w+Error: ", text), text
    return text


class FakeProvider(Provider):
    name = "Fake"

    def search(self, title):
        return [Match("Tidepool Tactics: Remastered", "Fake", "42"), Match("Something Else", "Fake", "43")]

    def fetch(self, match):
        return GameInfo(name="Tidepool Tactics: Remastered", description="Scary.", source="Fake", source_id=match.source_id,
                        assets=[AssetCandidate(slot="image", url="fake://shot.png")])


class FakeYt:
    path = "/nowhere/yt-dlp"

    def __init__(self, available=True, error=None):
        self._a, self.error = available, error
        self.calls, self.cancelled_seen, self.downloads = [], [], []

    def available(self):
        return self._a

    def search(self, query, count=6, title="", cancelled=None):
        self.query, self.title = query, title
        self.cancelled_seen.append(cancelled)
        if self.error:
            raise self.error
        return [AssetCandidate(slot="video", url=YT_URL, kind="youtube", label=query)]

    def download(self, url, dest, max_height=720, cancelled=None, timeout=None):
        self.cancelled_seen.append(cancelled)
        p = Path(dest) / "dl.mp4"
        p.write_bytes(b"x")
        self.downloads.append(p)
        return p


@pytest.fixture
def env(tmp_path):
    roms = tmp_path / "roms"
    sw = roms / "switch"
    (sw / "images").mkdir(parents=True)
    (sw / SAMPLE_ROM).write_text("x")
    (sw / "Done Game.nsp").write_text("x")
    (sw / "images" / "Done Game-image.jpg").write_bytes(b"j")
    (sw / "images" / "Done Game-thumb.png").write_bytes(b"j")
    (sw / "images" / "Done Game-marquee.png").write_bytes(b"j")
    (sw / "videos").mkdir()
    (sw / "videos" / "Done Game-video.mp4").write_bytes(b"j")
    (sw / "gamelist.xml").write_text(
        '<?xml version="1.0"?><gameList><game><path>./Done Game.nsp</path><name>Done Game</name><desc>d</desc>'
        "<image>./images/Done Game-image.jpg</image><thumbnail>./images/Done Game-thumb.png</thumbnail>"
        "<marquee>./images/Done Game-marquee.png</marquee><video>./videos/Done Game-video.mp4</video></game></gameList>")
    (roms / "empty").mkdir()
    es = tmp_path / "es.cfg"
    es.write_text(ES)
    cfg = load_config(overrides={"roms_root": str(roms), "data_dir": str(tmp_path / "data")})
    return cfg, str(es)


def make(env, **kw):
    cfg, es = env
    kw.setdefault("providers", [])
    kw.setdefault("ytdlp", FakeYt())
    kw.setdefault("resolver", public_resolver)
    return Controller(cfg, es_configs=[es], **kw)


# ---------------------------------------------------------------- construction

def test_controller_has_no_gui_imports():
    code = "import sys, scrappy.controller; assert not [m for m in sys.modules if m.startswith(('PyQt', 'PySide'))]"
    root = Path(__file__).resolve().parents[1]
    subprocess.run([sys.executable, "-c", code], cwd=root, check=True)


def test_default_providers_and_ytdlp_location(env):
    cfg, es = env
    ctl = Controller(cfg, es_configs=[es])
    assert [p.name for p in ctl.providers] == ["Steam"]
    assert ctl.ytdlp.path == Path(cfg["data_dir"]) / "bin" / "yt-dlp"
    for sub in ("backups", "journal", "tmp", "cache", "bin"):
        assert (Path(cfg["data_dir"]) / sub).is_dir()


def test_unwritable_data_dir_is_a_plain_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    cfg = load_config(overrides={"roms_root": str(tmp_path), "data_dir": str(blocker / "d")})
    with pytest.raises(ProviderError, match="work folder"):
        Controller(cfg, providers=[], ytdlp=FakeYt())



def tmp_folder(env):
    tmp = Path(env[0]["data_dir"]) / "tmp"
    tmp.mkdir(parents=True)
    return tmp


def test_prune_removes_only_the_apps_own_old_leftovers(env):
    tmp = tmp_folder(env)
    own_files = ["clip-0123456789ab.mp4", "clip-0123456789ab.mp4.deadbeef.tmp", ".scrappy-0123456789abcdef.tmp"]
    own_dirs = [".yt-crashed", "dl-abc123"]
    foreign = ["my-notes.txt", "report.docx", "clip-0123456789ab.mp4.bak", "clip-xyz.mp4", ".scrappy-notes.tmp", "x.tmp",
               "dl-notes.txt", ".yt-file", "clip-0123456789ABCD.mp4"]
    for name in own_files + foreign:
        (tmp / name).write_bytes(b"x")
        aged(tmp / name)
    for name in own_dirs + ["keepdir"]:
        (tmp / name).mkdir()
        (tmp / name / "inner.bin").write_bytes(b"x")
        aged(tmp / name)
    (tmp / "clip-aaaaaaaaaaaa.mp4").write_bytes(b"x")          # fresh: stays
    (tmp / "dl-new").mkdir()
    make(env)
    assert [n for n in own_files + own_dirs if (tmp / n).exists()] == []
    assert all((tmp / n).exists() for n in foreign + ["keepdir", "clip-aaaaaaaaaaaa.mp4", "dl-new"])
    assert (tmp / "keepdir" / "inner.bin").exists()


def test_prune_never_follows_symlinks_inside_the_tmp_folder(env, tmp_path):
    tmp = tmp_folder(env)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "old.docx").write_bytes(b"x")
    aged(outside / "old.docx")
    aged(outside)
    (tmp / "clip-0123456789ab.mp4").symlink_to(outside / "old.docx")     # matches a pattern: only the link goes
    (tmp / ".yt-evil").symlink_to(outside)
    (tmp / "diary").symlink_to(outside / "old.docx")                      # no pattern: untouched
    for name in ("clip-0123456789ab.mp4", ".yt-evil", "diary"):
        aged(tmp / name)
    make(env)
    assert not os.path.lexists(tmp / "clip-0123456789ab.mp4") and not os.path.lexists(tmp / ".yt-evil")
    assert os.path.islink(tmp / "diary")
    assert (outside / "old.docx").read_bytes() == b"x"


def test_prune_refuses_when_the_tmp_folder_is_a_symlink(env, tmp_path):
    cfg, _ = env
    outside = tmp_path / "outside"
    outside.mkdir()
    for name in ("old.docx", "clip-0123456789ab.mp4", ".scrappy-0123456789abcdef.tmp"):
        (outside / name).write_bytes(b"x")
        aged(outside / name)
    data = Path(cfg["data_dir"])
    data.mkdir()
    (data / "tmp").symlink_to(outside)
    make(env)
    assert sorted(p.name for p in outside.iterdir()) == [".scrappy-0123456789abcdef.tmp", "clip-0123456789ab.mp4", "old.docx"]


def test_prune_refuses_when_the_data_folder_is_a_symlink(env, tmp_path):
    cfg, es = env
    real = tmp_path / "real"
    (real / "tmp").mkdir(parents=True)
    (real / "tmp" / "clip-0123456789ab.mp4").write_bytes(b"x")
    aged(real / "tmp" / "clip-0123456789ab.mp4")
    link = tmp_path / "link"
    link.symlink_to(real)
    cfg["data_dir"] = str(link)
    Controller(cfg, providers=[], ytdlp=FakeYt(), es_configs=[es])
    assert (real / "tmp" / "clip-0123456789ab.mp4").exists()


def test_old_cache_leftovers_are_pruned_but_pictures_and_foreign_files_stay(env):
    cfg, _ = env
    cache = Path(cfg["data_dir"]) / "cache"
    cache.mkdir(parents=True)
    names = [".scrappy-0123456789abcdef.tmp", "0123456789abcdef0123456789abcdef01234567.png.ab12cd.part", "notes.txt",
             "0123456789abcdef0123456789abcdef01234567.png"]
    for n in names:
        (cache / n).write_bytes(b"x")
        aged(cache / n)
    make(env)
    assert sorted(p.name for p in cache.iterdir()) == sorted(names[2:])


# ---------------------------------------------------------------- library

def test_list_systems_only_real_systems(env):
    assert make(env).list_systems() == ["switch"]


def test_list_systems_survives_a_missing_roms_folder(env, tmp_path):
    cfg, es = env
    cfg["roms_root"] = str(tmp_path / "does-not-exist")
    assert Controller(cfg, providers=[], ytdlp=FakeYt(), es_configs=[es]).list_systems() == []


def test_list_systems_skips_hidden_folders_and_includes_gamelist_only_systems(env):
    cfg, _ = env
    roms = Path(cfg["roms_root"])
    (roms / ".trash").mkdir()
    (roms / ".trash" / "gamelist.xml").write_text("<gameList/>")
    (roms / "mystery").mkdir()
    (roms / "mystery" / "gamelist.xml").write_text("<gameList/>")
    (roms / "afile.txt").write_text("x")
    assert make(env).list_systems() == ["mystery", "switch"]



@pytest.mark.parametrize("system", ["", ".", "..", "../etc", "a/b", "a\\b", "x\x00y", None, 5])
def test_system_names_cannot_leave_the_roms_folder(env, system):
    ctl = make(env)
    for call in (lambda: ctl.rows(system), lambda: ctl.build_request(system, "./a.nsp", {})):
        with pytest.raises(ControllerError) as caught:
            call()
        assert "system folder" in plain(caught.value) and isinstance(caught.value, ValueError)      # still a ValueError for old callers



def test_rows_sorted_incomplete_first_and_filter(env):
    ctl = make(env)
    rows = ctl.rows("switch")
    assert [r.title for r in rows][0].startswith("Tidepool Tactics")      # incomplete first
    assert rows[0].complete is False and rows[1].complete is True and rows[1].title == "Done Game"
    assert set(rows[0].missing) == {"desc", "image", "thumbnail", "marquee", "video"} and rows[1].missing == []
    assert rows[0].rom_path == "./" + SAMPLE_ROM and rows[0].problem == ""
    assert [r.title for r in ctl.rows("switch", only_missing=True)] == [rows[0].title]
    assert isinstance(rows[0], RomRow)


def test_newest_roms_come_first_among_the_incomplete(env):
    cfg, _ = env
    sw = Path(cfg["roms_root"]) / "switch"
    same = time.time() - 5 * 86400                                  # Beta and Gamma tie: title order decides
    for name, when in (("Zed", 10), ("Alpha", 20), ("Mid", 1), ("Beta", None), ("Gamma", None)):
        (sw / f"{name}.nsp").write_text("x")
        if when is None:
            os.utime(sw / f"{name}.nsp", (same, same))
        else:
            aged(sw / f"{name}.nsp", when)
    aged(sw / SAMPLE_ROM, 100)
    (sw / "Done Game.nsp").write_text("brand new")                 # complete games stay last however new they are
    rows = make(env).rows("switch")
    assert [r.title for r in rows] == ["Mid", "Beta", "Gamma", "Zed", "Alpha", "Tidepool Tactics", "Done Game"]


def test_rows_use_the_first_gamelist_entry_and_tolerate_paths_without_dot_slash(env):
    cfg, _ = env
    gl = Path(cfg["roms_root"]) / "switch" / "gamelist.xml"
    gl.write_text('<gameList><game><path>Done Game.nsp</path><name>First</name></game>'
                  '<game><path>./Done Game.nsp</path><name>Second</name></game></gameList>')
    titles = {r.rom_path: r.title for r in make(env).rows("switch")}
    assert titles["./Done Game.nsp"] == "First"


def test_rows_with_a_damaged_gamelist_still_list_the_games_and_say_why(env):
    cfg, _ = env
    (Path(cfg["roms_root"]) / "switch" / "gamelist.xml").write_text("<gameList><game>")
    rows = make(env).rows("switch")
    assert len(rows) == 2 and all(not r.complete and r.problem for r in rows)
    assert "gamelist.xml" in rows[0].problem
    assert {r.title for r in rows} == {"Tidepool Tactics", "Done Game"}


# ---------------------------------------------------------------- online lookups

def test_search_combines_providers_and_youtube(env):
    yt = FakeYt()
    ctl = make(env, providers=[FakeProvider()], ytdlp=yt)
    res = ctl.search("Tidepool Tactics", "switch")
    assert isinstance(res, SearchResults)
    assert res.matches[0].source_id == "42" and res.videos[0].kind == "youtube" and res.errors == {}
    assert yt.query == "Tidepool Tactics Nintendo Switch official trailer"
    assert yt.title == "Tidepool Tactics"                    # the bare title ranks the videos


def test_search_reports_missing_ytdlp_without_failing(env):
    ctl = make(env, providers=[FakeProvider()], ytdlp=FakeYt(available=False))
    res = ctl.search("x", "switch")
    assert res.matches and res.videos == [] and "YouTube" in res.errors
    assert "Install" in res.errors["YouTube"]


@pytest.mark.parametrize("error, text", [
    (ProviderError("YouTube said no"), "YouTube said no"),
    (RuntimeError("boom"), "RuntimeError: boom"),
])
def test_search_describes_a_youtube_failure_like_every_other_source(env, error, text):
    res = make(env, providers=[FakeProvider()], ytdlp=FakeYt(error=error)).search("x", "switch")
    assert res.matches and res.videos == [] and res.errors == {"YouTube": text}


def test_search_reports_provider_errors_and_keeps_going(env):
    class Broken(Provider):
        name = "Broken"

        def search(self, title):
            raise ProviderError("Broken is down")

    res = make(env, providers=[Broken(), FakeProvider()]).search("x", "switch")
    assert res.errors == {"Broken": "Broken is down"} and res.matches and res.videos


def test_search_with_a_blank_title_asks_nobody(env):
    yt = FakeYt()
    res = make(env, providers=[FakeProvider()], ytdlp=yt).search("   ", "switch")
    assert (res.matches, res.videos, res.errors) == ([], [], {}) and yt.cancelled_seen == []


def test_search_passes_cancellation_to_every_source(env):
    seen = {}

    class Slow(Provider):
        name = "Slow"

        def search(self, title):
            time.sleep(5)

    yt = FakeYt()
    start = time.monotonic()
    res = make(env, providers=[Slow()], ytdlp=yt).search("x", "switch", cancelled=lambda: True)
    assert time.monotonic() - start < 3
    assert res.errors["Slow"] == "search cancelled"
    assert yt.cancelled_seen[0]() is True                      # yt-dlp's search gets a cancel check that follows ours


def test_youtube_search_has_its_own_deadline(env):
    class Stuck(FakeYt):
        def search(self, query, count=6, title="", cancelled=None):
            while not cancelled():
                time.sleep(0.01)
            raise ProviderError("search cancelled")

    cfg, es = env
    cfg["youtube_search_timeout"] = 0.3
    ctl = Controller(cfg, providers=[FakeProvider()], ytdlp=Stuck(), es_configs=[es])
    start = time.monotonic()
    res = ctl.search("x", "switch")
    assert time.monotonic() - start < 3 and res.matches
    assert res.errors == {"YouTube": "did not answer in time"}


def test_concurrent_searches_do_not_mix_results(env):
    ctl = make(env, providers=[FakeProvider()])
    out, errors = {}, []

    def work(i):
        try:
            res = ctl.search(f"Game {i}", "switch")
            out[i] = res.videos[0].label
        except Exception as exc:                            # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and out == {i: f"Game {i} Nintendo Switch official trailer" for i in range(8)}


def test_load_info_uses_matching_provider(env):
    ctl = make(env, providers=[FakeProvider()])
    assert ctl.load_info(Match("t", "Fake", "42")).description == "Scary."


def test_load_info_for_an_unknown_source_is_a_plain_error(env):
    with pytest.raises(ProviderError, match="Nowhere"):
        make(env, providers=[FakeProvider()]).load_info(Match("t", "Nowhere", "1"))


# ---------------------------------------------------------------- picture bytes


def test_fetch_bytes_file_url_and_cache(env, tmp_path):
    calls = []
    ctl = make(env, fetch=lambda u: calls.append(u) or THUMB)
    f = tmp_path / "pic.png"
    f.write_bytes(THUMB + b"LOCAL")
    cand = ctl.local_candidate("boxart", str(f))
    assert (cand.slot, cand.url, cand.label, cand.source, cand.kind) == ("boxart", "file://" + str(f), "pic.png", "your file", "file")
    assert ctl.local_candidate("image", str(f), label="My shot").label == "My shot"
    assert ctl.fetch_bytes(cand.url, local_ok=True) == THUMB + b"LOCAL"
    with pytest.raises(ControllerError, match="Choose file"):
        ctl.fetch_bytes(cand.url)                              # not marked local_ok: refused
    assert ctl.fetch_bytes("http://x/y.png") == THUMB and ctl.fetch_bytes("http://x/y.png") == THUMB
    assert calls == ["http://x/y.png"]                         # second call served from cache


def test_a_spoofed_your_file_candidate_is_never_read(env, tmp_path):
    secret = tmp_path / "secret.png"
    secret.write_bytes(THUMB)
    ctl = make(env, fetch=lambda u: pytest.fail("must not be fetched"), encode=lambda *a, **k: pytest.fail("must not encode"))
    fake = AssetCandidate(slot="boxart", url="file://" + str(secret), source="your file")        # what a provider could return
    with pytest.raises(ControllerError, match="Choose file"):
        ctl.fetch_bytes(fake.url, local_ok=fake.source == "your file")
    for url in ("file://" + str(secret), str(secret)):
        with pytest.raises(ControllerError, match="not chosen"):
            ctl.make_video(AssetCandidate(slot="video", url=url, kind="file", source="your file"), 0, 5)
    ctl.local_candidate("boxart", str(tmp_path / "secret.png"))                                    # issuing one file ...
    other = tmp_path / "other.png"
    other.write_bytes(THUMB)
    with pytest.raises(ControllerError):
        ctl.fetch_bytes("file://" + str(other), local_ok=True)                                     # ... does not open the others


def test_issued_local_files_are_remembered_across_threads(env, tmp_path):
    ctl = make(env)
    files = []
    for i in range(20):
        f = tmp_path / f"p{i}.png"
        f.write_bytes(THUMB)
        files.append(f)
    urls, errors = [], []

    def work(f):
        try:
            urls.append(ctl.local_candidate("image", str(f)).url)
        except Exception as exc:                                 # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(f,)) for f in files]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(urls) == 20 and all(ctl.fetch_bytes(u, local_ok=True) == THUMB for u in urls)



@pytest.mark.parametrize("url", ["FILE:///etc/passwd", "File:///etc/passwd", " file:///etc/passwd", "file:/etc/passwd"])
@pytest.mark.parametrize("local_ok", [False, True])
def test_file_scheme_is_refused_whatever_its_spelling(env, url, local_ok):
    ctl = make(env, fetch=lambda u: pytest.fail("must not be fetched"))
    with pytest.raises(ControllerError, match="Choose file"):
        ctl.fetch_bytes(url, local_ok=local_ok)



def test_local_files_must_be_ordinary_files_of_sane_size(env, tmp_path, monkeypatch):
    ctl = make(env)
    monkeypatch.setattr(controller_mod, "MAX_IMAGE_BYTES", 10)
    big = tmp_path / "big.png"
    big.write_bytes(b"x" * 11)
    with pytest.raises(ControllerError, match="too big"):
        ctl.local_candidate("image", str(big))
    (tmp_path / "dir").mkdir()
    with pytest.raises(ControllerError, match="not an ordinary file"):
        ctl.local_candidate("image", str(tmp_path / "dir"))
    with pytest.raises(ControllerError, match="No such file"):
        ctl.local_candidate("image", str(tmp_path / "missing.png"))
    with pytest.raises(ControllerError, match="full path"):
        ctl.local_candidate("image", "relative/path.png")
    with pytest.raises(ControllerError, match="full path"):
        ctl.local_candidate("image", "~/pic.png")
    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")
    with pytest.raises(ControllerError, match="empty"):
        ctl.local_candidate("image", str(empty))
    with pytest.raises(ControllerError, match="slot"):
        ctl.local_candidate("wallpaper", str(big))


def test_a_file_that_changed_after_it_was_chosen_is_checked_again(env, tmp_path, monkeypatch):
    ctl = make(env)
    monkeypatch.setattr(controller_mod, "MAX_IMAGE_BYTES", 10)
    f = tmp_path / "pic.png"
    f.write_bytes(b"12345")
    url = ctl.local_candidate("image", str(f)).url
    f.unlink()
    f.mkdir()
    with pytest.raises(ControllerError, match="not an ordinary file"):                    # swapped for a folder
        ctl.fetch_bytes(url, local_ok=True)
    f.rmdir()
    f.write_bytes(b"x" * 20)                                                              # grew while we were reading it
    real = os.fstat
    with monkeypatch.context() as m:
        m.setattr(controller_mod.os, "fstat", lambda fd: type("S", (), {"st_mode": real(fd).st_mode, "st_size": 5})())
        with pytest.raises(ControllerError, match="too big"):
            ctl.fetch_bytes(url, local_ok=True)



@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_local_named_pipe_is_not_read(env, tmp_path):
    fifo = tmp_path / "pipe.png"
    os.mkfifo(fifo)
    start = time.monotonic()
    with pytest.raises(ControllerError, match="not an ordinary file"):
        make(env).local_candidate("image", str(fifo))
    assert time.monotonic() - start < 2                       # it did not block waiting for a writer



def test_web_pictures_use_the_safe_downloader_and_pass_cancellation(env, monkeypatch):
    seen = {}

    def fake_fetch_cached(cache_dir, url, **options):
        seen.update(cache_dir=Path(cache_dir), url=url, **options)
        p = Path(cache_dir) / "got.png"
        p.write_bytes(THUMB)
        return p

    monkeypatch.setattr(controller_mod, "fetch_cached", fake_fetch_cached)
    ctl = make(env)
    cancel = lambda: False                                    # noqa: E731
    assert ctl.fetch_bytes("https://example.com/a.png", cancelled=cancel) == THUMB
    assert seen["url"] == "https://example.com/a.png" and seen["cancelled"] is cancel
    assert seen["cache_dir"] == ctl.cache_dir
    seen.clear()
    ctl.fetch_bytes("https://example.com/b.png", local_ok=True)    # local_ok only matters for file:// addresses
    assert seen["url"] == "https://example.com/b.png"


@pytest.mark.parametrize("data, expected", [
    (JPG, True), (THUMB, True), (b"GIF89a....", True), (b"GIF87a", True), (b"RIFF\x10\x00\x00\x00WEBPVP8 ", True),
    (b"BM\x00\x00", True), (b"II*\x00....", True), (b"MM\x00*....", True),
    (b"", False), (b"<html>404</html>", False), (b'{"error": 1}', False), (b"GIF", False), (b"RIFF\x10\x00\x00\x00WAVEfmt ", False),
    (None, False), ("text", False),
])
def test_looks_like_picture(data, expected):
    assert looks_like_picture(data) is expected


def test_an_error_page_is_not_cached_as_a_picture(env, monkeypatch):
    html = b"<html><body>429 Too Many Requests</body></html>"
    ctl = make(env, fetch=lambda u: html)
    with pytest.raises(ControllerError, match="not a picture"):
        ctl.fetch_bytes("http://x/y.png")
    assert list(ctl.cache_dir.iterdir()) == []

    def fake_fetch_cached(cache_dir, url, **options):          # the real downloader has already written the file
        p = Path(cache_dir) / "page.png"
        p.write_bytes(html)
        return p

    monkeypatch.setattr(controller_mod, "fetch_cached", fake_fetch_cached)
    with pytest.raises(ControllerError, match="not a picture"):
        make(env).fetch_bytes("http://x/y.png")
    assert list(ctl.cache_dir.iterdir()) == []                  # and it is removed again


def test_cache_is_trimmed_oldest_first_and_only_its_own_files(env):
    def pic(n):
        return PNG_MAGIC + b"\0" * n

    ctl = make(env, fetch=lambda u: pic(1000), cache_max_bytes=2500)
    foreign = ctl.cache_dir / "notes.txt"
    foreign.write_bytes(b"n" * 9000)
    aged(foreign, 30)
    from scrappy.download import cache_path
    urls = [f"http://x/{i}.png" for i in range(3)]
    for i, u in enumerate(urls):
        ctl.fetch_bytes(u)
        aged(cache_path(ctl.cache_dir, u), 3 - i)              # u0 is the oldest
    assert [cache_path(ctl.cache_dir, u).exists() for u in urls] == [False, True, True]     # 3 x 1008 > 2500: the oldest went
    ctl.fetch_bytes("http://x/3.png")
    left = [cache_path(ctl.cache_dir, u).exists() for u in urls + ["http://x/3.png"]]
    assert left == [False, False, True, True] and foreign.exists()


def test_cache_limit_has_a_sane_default(env):
    assert 50_000_000 <= make(env).cache_max_bytes <= 500_000_000


@pytest.mark.parametrize("url", ["http://127.0.0.1:9/x.png", "http://localhost/x.png", "http://192.168.1.1/x.png",
                                 "ftp://example.com/x.png", "gopher://example.com/", "data:image/png;base64,AAAA"])
def test_real_downloader_refuses_private_hosts_and_odd_schemes(env, url):
    with pytest.raises(ProviderError):
        make(env).fetch_bytes(url, local_ok=True)             # even a user-picked file privilege never widens web access



def test_concurrent_cache_fills_leave_one_complete_file(env):
    gate = threading.Barrier(6)
    big = THUMB + b"\0" * 4000

    def slow_fetch(url):
        gate.wait(timeout=5)
        return big

    ctl = make(env, fetch=slow_fetch)
    results = []
    threads = [threading.Thread(target=lambda: results.append(ctl.fetch_bytes("http://x/y.png"))) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [big] * 6
    assert len(list(ctl.cache_dir.iterdir())) == 1            # no half-written or temporary files left



def test_empty_download_is_not_cached(env):
    ctl = make(env, fetch=lambda u: b"")
    with pytest.raises(ProviderError):
        ctl.fetch_bytes("http://x/y.png")
    assert list(ctl.cache_dir.iterdir()) == []


def test_picture_address_must_be_text(env):
    with pytest.raises(ControllerError, match="text") as caught:
        make(env).fetch_bytes(5)
    plain(caught.value)


# ---------------------------------------------------------------- video

def recording_encode(seen):
    def encode(src, dst, start=0, end=None, max_seconds=120, bitrate="1500k", cancelled=None):
        seen.update(src=src, dst=dst, start=start, end=end, max_seconds=max_seconds, bitrate=bitrate, cancelled=cancelled)
        with open(dst, "wb") as f:
            f.write(b"MP4")
    return encode


def test_make_video_downloads_youtube_then_encodes(env):
    seen, yt = {}, FakeYt()
    ctl = make(env, ytdlp=yt, encode=recording_encode(seen))
    out = ctl.make_video(AssetCandidate(slot="video", url=YT_URL, kind="youtube"), 5, 65)
    assert out.read_bytes() == b"MP4" and seen["src"].endswith("dl.mp4") and seen["start"] == 5 and seen["end"] == 65
    assert not yt.downloads[0].exists()                       # the big source download is not kept
    out2 = ctl.make_video(AssetCandidate(slot="video", url="https://cdn/x.m3u8", kind="hls"), 0, None)
    assert seen["src"] == "https://cdn/x.m3u8" and out2.exists() and out2 != out
    assert out.parent == ctl.tmp_dir


def test_make_video_uses_the_configured_limits_and_passes_cancellation(env):
    seen, yt = {}, FakeYt()
    cfg, es = env
    cfg["max_video_seconds"], cfg["video_bitrate"] = 45, "900k"
    ctl = Controller(cfg, providers=[], ytdlp=yt, es_configs=[es], encode=recording_encode(seen))
    cancel = lambda: False                                    # noqa: E731
    ctl.make_video(AssetCandidate(slot="video", url=YT_URL, kind="youtube"), 0, 500, cancelled=cancel)
    assert seen["max_seconds"] == 45 and seen["bitrate"] == "900k" and seen["end"] == 500
    assert seen["cancelled"] is cancel and yt.cancelled_seen == [cancel]


def test_clips_for_different_ranges_do_not_overwrite_each_other(env):
    ctl = make(env, encode=recording_encode({}))
    c = AssetCandidate(slot="video", url="https://cdn/x.mp4", kind="file")
    assert ctl.make_video(c, 0, 10) != ctl.make_video(c, 5, 10)
    assert ctl.make_video(c, 0, 10) == ctl.make_video(c, 0, 10)



@pytest.mark.parametrize("url", ["/etc/passwd", "file:///etc/passwd", "concat:a|b", "ftp://x/y.mp4", "", "-i evil",
                                 "rtmp://host/app", "data:video/mp4;base64,AAAA", "https:///nohost.mp4",
                                 "https://user:pw@cdn.steamstatic.com/x.m3u8", "https://cdn.steamstatic.com:99999/x.m3u8",
                                 "https://cdn.steamstatic.com/a b.m3u8", "https://cdn.steamstatic.com/a\nb.m3u8", None])
@pytest.mark.parametrize("kind", ["file", "hls"])
def test_make_video_only_hands_http_addresses_to_ffmpeg(env, url, kind):
    ctl = make(env, encode=lambda *a, **k: pytest.fail("must not encode"))
    with pytest.raises(ControllerError) as caught:
        ctl.make_video(AssetCandidate(slot="video", url=url, kind=kind), 0, 10)
    plain(caught.value)


def test_make_video_checks_that_other_hosts_are_public(env):
    seen = []

    def resolver(host, port):
        seen.append(host)
        return {"cdn.example.com": ["93.184.216.34"], "nas.local": ["192.168.1.5"], "mixed.example": ["93.184.216.34", "10.0.0.1"]}[host]

    made = {}
    ctl = make(env, encode=recording_encode(made), resolver=resolver)
    ctl.make_video(AssetCandidate(slot="video", url="https://cdn.example.com/x.m3u8", kind="hls"), 0, 5)
    assert made["src"] == "https://cdn.example.com/x.m3u8"
    for url in ("http://nas.local/x.m3u8", "https://mixed.example/x.m3u8", "http://192.168.1.5/x.m3u8", "http://127.0.0.1:8080/x",
                "http://[::1]/x", "http://169.254.169.254/latest"):
        with pytest.raises(ProviderError, match="own network") as caught:
            ctl.make_video(AssetCandidate(slot="video", url=url, kind="hls"), 0, 5)
        plain(caught.value)
    assert seen == ["cdn.example.com", "nas.local", "mixed.example"]       # literal addresses never reach the resolver


def test_steam_trailer_hosts_need_no_dns(env):
    def no_dns(host, port):
        raise AssertionError("steam hosts are trusted by name")

    made = {}
    ctl = make(env, encode=recording_encode(made), resolver=no_dns)
    for url in ("https://video.fastly.steamstatic.com/store_trailers/1/hls_264_master.m3u8", "https://cdn.akamaihd.net/a.m3u8"):
        ctl.make_video(AssetCandidate(slot="video", url=url, kind="hls"), 0, 5)
        assert made["src"] == url
    for lookalike in ("https://steamstatic.com.evil.example/a.m3u8", "https://evilsteamstatic.com/a.m3u8"):
        with pytest.raises(AssertionError):
            ctl.make_video(AssetCandidate(slot="video", url=lookalike, kind="hls"), 0, 5)


def test_a_failed_name_lookup_is_a_plain_error(env):
    def broken(host, port):
        raise socket.gaierror(-2, "Name or service not known")

    with pytest.raises(ControllerError, match="look up") as caught:
        make(env, encode=recording_encode({}), resolver=broken).make_video(
            AssetCandidate(slot="video", url="https://nowhere.example/x.m3u8", kind="hls"), 0, 5)
    plain(caught.value)


def test_a_video_file_the_user_chose_is_encoded_from_that_file(env, tmp_path):
    video = tmp_path / "my clip.mp4"
    video.write_bytes(b"x" * 10)
    seen = {}
    ctl = make(env, encode=recording_encode(seen), resolver=lambda h, p: pytest.fail("no DNS for a local file"))
    cand = ctl.local_candidate("video", str(video))
    assert cand.kind == "file" and cand.slot == "video"
    ctl.make_video(cand, 0, 5)
    assert seen["src"] == str(video)
    with pytest.raises(ControllerError, match="not chosen"):
        ctl.make_video(AssetCandidate(slot="video", url="file://" + str(tmp_path / "other.mp4"), kind="file"), 0, 5)



def test_make_video_rejects_unknown_kinds_and_bad_times(env):
    ctl = make(env, encode=recording_encode({}))
    with pytest.raises(ControllerError) as caught:
        ctl.make_video(AssetCandidate(slot="video", url="https://x/y.mp4", kind="torrent"), 0, 10)
    plain(caught.value)
    for start, end in ((float("nan"), 10), (0, float("inf")), ("a", 5), (True, 5), (0, "x"), (0, [1])):
        with pytest.raises(ControllerError, match="number of seconds") as caught:
            ctl.make_video(AssetCandidate(slot="video", url="https://x/y.mp4", kind="file"), start, end)
        plain(caught.value)


def test_bad_times_are_refused_before_anything_is_downloaded(env):
    yt = FakeYt()
    with pytest.raises(ValueError):
        make(env, ytdlp=yt, encode=recording_encode({})).make_video(
            AssetCandidate(slot="video", url=YT_URL, kind="youtube"), float("nan"), 10)
    assert yt.downloads == []


def test_clamping_of_odd_ranges_is_left_to_the_encoder(env):
    seen = {}
    ctl = make(env, encode=recording_encode(seen))
    ctl.make_video(AssetCandidate(slot="video", url="https://x/y.mp4", kind="file"), -3, 2)
    assert (seen["start"], seen["end"]) == (-3, 2)
    ctl.make_video(AssetCandidate(slot="video", url="https://x/y.mp4", kind="file"), None, None)
    assert seen["start"] == 0 and seen["end"] is None


def test_failed_encode_still_removes_the_download(env):
    yt = FakeYt()

    def boom(*a, **k):
        raise RuntimeError("encoder died")

    ctl = make(env, ytdlp=yt, encode=boom)
    with pytest.raises(RuntimeError):
        ctl.make_video(AssetCandidate(slot="video", url=YT_URL, kind="youtube"), 0, 10)
    assert not yt.downloads[0].exists()


def test_make_video_without_ytdlp_says_how_to_install_it(env):
    ctl = make(env, ytdlp=FakeYt(available=False), encode=recording_encode({}))
    with pytest.raises(ProviderError, match="Install"):
        ctl.make_video(AssetCandidate(slot="video", url=YT_URL, kind="youtube"), 0, 10)


@needs_ffmpeg
def test_make_video_with_real_ffmpeg_applies_the_clip_limits(env, tmp_path):
    source = make_source(tmp_path / "source.mp4", seconds=4)

    class RealFileYt(FakeYt):
        def download(self, url, dest, max_height=720, cancelled=None, timeout=None):
            return Path(shutil.copy(source, Path(dest) / "real.mp4"))

    cfg, es = env
    cfg["max_video_seconds"] = 2
    ctl = Controller(cfg, providers=[], ytdlp=RealFileYt(), es_configs=[es])
    out = ctl.make_video(AssetCandidate(slot="video", url=YT_URL, kind="youtube"), 1, 500)
    length = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                                  capture_output=True, text=True, check=True).stdout)
    assert 1.5 <= length <= 2.3                              # asked for 1..500 s, limited to 2 s


# ---------------------------------------------------------------- building a save request

def test_build_request_keeps_only_whitelisted_non_blank_text(env):
    ctl = make(env)
    req = ctl.build_request("switch", "./a.nsp", {"name": "  Nice Name ", "desc": "", "genre": "   ", "players": None,
                                                  "releasedate": "20260218T000000"}, image_jpg=JPG)
    assert isinstance(req, SaveRequest) and req.system_dir == Path(ctl.roms_root) / "switch"
    assert req.fields == {"name": "Nice Name", "releasedate": "20260218T000000"}
    assert req.image_jpg == JPG and req.replace == set()


@pytest.mark.parametrize("fields", [{"playcount": "99"}, {"path": "./x"}, {"image": "./images/x.jpg"}, {"favorite": "true"}])
def test_build_request_rejects_fields_the_saver_may_not_write(env, fields):
    with pytest.raises(SaveError, match="cannot be saved"):
        make(env).build_request("switch", "./a.nsp", fields)


def test_build_request_rejects_non_text_values(env):
    with pytest.raises(SaveError, match="players"):
        make(env).build_request("switch", "./a.nsp", {"players": 4})


def test_build_request_checks_picture_types(env):
    ctl = make(env)
    for kw in ({"image_jpg": THUMB}, {"image_jpg": b"<html>error</html>"}, {"image_jpg": b""},
               {"thumb_png": JPG}, {"marquee_png": b"junk"}, {"image_jpg": "text"}):
        with pytest.raises(SaveError):
            ctl.build_request("switch", "./a.nsp", {}, **kw)
    ctl.build_request("switch", "./a.nsp", {}, image_jpg=JPG, thumb_png=THUMB, marquee_png=LOGO)


def test_build_request_replace_must_name_media_tags_and_may_name_the_video_attached_later(env):
    ctl = make(env)
    with pytest.raises(SaveError, match="replace"):
        ctl.build_request("switch", "./a.nsp", {}, replace={"name"})
    req = ctl.build_request("switch", "./a.nsp", {}, image_jpg=JPG, replace=["image", "video"])
    assert req.replace == {"image", "video"}                   # the video is attached to the request after this call
    req.video_mp4 = Path("/tmp/clip.mp4")


# ---------------------------------------------------------------- saving and undoing

def test_save_and_undo_roundtrip(env):
    ctl = make(env)
    req = ctl.build_request("switch", "./" + SAMPLE_ROM, {"name": "Tidepool Tactics: Remastered"}, image_jpg=JPG)
    report = ctl.save(req)
    assert len(report.written) == 1
    rows = ctl.rows("switch")
    done = [r for r in rows if r.title == "Tidepool Tactics: Remastered"][0]
    assert done.status["image"] is True and done.status["video"] is False
    entry = ctl.undo()
    assert entry["rom_path"] == "./" + SAMPLE_ROM
    assert [r.title for r in ctl.rows("switch")][0].startswith("Tidepool Tactics")
    assert ctl.undo() is None                                  # nothing left to undo


def test_existing_art_is_kept_unless_the_caller_says_replace(env):
    cfg, _ = env
    image = Path(cfg["roms_root"]) / "switch" / "images" / "Done Game-image.jpg"
    ctl = make(env)
    kept = ctl.save(ctl.build_request("switch", "./Done Game.nsp", {}, image_jpg=JPG))
    assert image.read_bytes() == b"j" and str(image) in kept.kept and kept.replaced == []
    replaced = ctl.save(ctl.build_request("switch", "./Done Game.nsp", {}, image_jpg=JPG, replace={"image"}))
    assert image.read_bytes() == JPG and replaced.replaced == [str(image)]
    ctl.undo()
    assert image.read_bytes() == b"j"                          # undoing the replacement brings the old picture back


def test_undo_refuses_when_gamelist_changed_then_force_works(env):
    cfg, _ = env
    ctl = make(env)
    ctl.save(ctl.build_request("switch", "./" + SAMPLE_ROM, {"name": "N"}, image_jpg=JPG))
    gl = Path(cfg["roms_root"]) / "switch" / "gamelist.xml"
    gl.write_text(gl.read_text().replace("</gameList>", "<game><path>./Other.nsp</path></game></gameList>"))
    with pytest.raises(UndoRefused):
        ctl.undo()
    assert ctl.undo(force=True)["forced"] is True


def test_dry_run_leaves_the_real_library_alone(env, tmp_path):
    cfg, es = env
    cfg["dry_run_dir"] = str(tmp_path / "sandbox")
    ctl = Controller(cfg, providers=[], ytdlp=FakeYt(), es_configs=[es])
    gl = Path(cfg["roms_root"]) / "switch" / "gamelist.xml"
    before = gl.read_bytes()
    report = ctl.save(ctl.build_request("switch", "./" + SAMPLE_ROM, {"name": "N"}, image_jpg=JPG))
    assert report.dry_run and gl.read_bytes() == before
    assert not (Path(cfg["roms_root"]) / "switch" / "images" / (SAMPLE_ROM[:-4] + "-image.jpg")).exists()


def test_concurrent_saves_from_worker_threads_all_land(env):
    cfg, _ = env
    sw = Path(cfg["roms_root"]) / "switch"
    for i in range(6):
        (sw / f"Game {i}.nsp").write_text("x")
    ctl = make(env)
    gate, errors = threading.Barrier(6), []

    def work(i):
        try:
            req = ctl.build_request("switch", f"./Game {i}.nsp", {"name": f"Name {i}"}, image_jpg=JPG)
            gate.wait(timeout=5)
            ctl.save(req)
        except Exception as exc:                             # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    titles = {r.title for r in ctl.rows("switch")}
    assert {f"Name {i}" for i in range(6)} <= titles


def test_save_errors_are_plain_messages(env):
    ctl = make(env)
    req = ctl.build_request("switch", "../escape.nsp", {"name": "x"})
    with pytest.raises(SaveError, match="not valid"):
        ctl.save(req)


# ---------------------------------------------------------------- EmulationStation reload

class _ES(http.server.BaseHTTPRequestHandler):
    code = 200
    hits = []

    def do_GET(self):
        _ES.hits.append(self.path)
        self.send_response(_ES.code)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def es_server():
    _ES.hits = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _ES)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/reloadgames"
    server.shutdown()
    server.server_close()


def test_reload_es_success_ignores_proxy_settings(env, es_server, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    _ES.code = 200
    assert "asked to reload" in make(env).reload_es(url=es_server)
    assert _ES.hits == ["/reloadgames"]


def test_reload_es_reports_http_errors_and_unreachable_es(env, es_server):
    _ES.code = 500
    msg = make(env).reload_es(url=es_server)
    assert "Could not reach EmulationStation" in msg and "Update Gamelists" in msg
    assert "Could not reach EmulationStation" in make(env).reload_es(url="http://127.0.0.1:9/reloadgames", timeout=1)


def test_ask_es_to_reload_is_the_tools_action_and_never_raises(env):
    assert make(env, es_reload=lambda: True).ask_es_to_reload() is True
    assert make(env, es_reload=lambda: False).ask_es_to_reload() is False

    def broken():
        raise RuntimeError("socket exploded")

    assert make(env, es_reload=broken).ask_es_to_reload() is False


def test_ask_es_to_reload_without_a_stand_in_makes_the_real_request(env, es_server, monkeypatch):
    _ES.code = 200
    monkeypatch.setattr("scrappy.controller.ES_RELOAD_URL", es_server)
    assert make(env).ask_es_to_reload() is True and _ES.hits == ["/reloadgames"]
