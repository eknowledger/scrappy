"""The developer-only "real titles" screenshot mode (tools/real_showcase.py, tools/fetch_demo_art.py, make_screenshots --real).

Offline only: the list of titles, the libretro file-name rules, the provider served from a tiny SYNTHETIC cache that these tests
generate in tmp_path (never real artwork), the failure on an empty cache, and a guard that no picture or video is tracked in git
outside the folders meant for them. What needs the network is tried by hand."""
import io
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
from PIL import Image

from scrappy.models import Match
from scrappy.providers.base import ProviderError
from scrappy.ui.demo_data import COMPLETE, DESC_ONLY, PARTIAL, ROM_ONLY

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import fetch_demo_art as fetch  # noqa: E402
import real_showcase as rs  # noqa: E402

FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
EXPECTED = {
    "nes": ["The Legend of Zelda", "Mega Man 2"],
    "snes": ["Super Metroid", "Chrono Trigger"],
    "megadrive": ["Sonic the Hedgehog 2", "Streets of Rage 2"],
    "gb": ["Tetris", "The Legend of Zelda: Link's Awakening"],
    "psx": ["Final Fantasy VII", "Castlevania: Symphony of the Night"],
    "switch": ["Hades", "Celeste"],
    "windows": ["Stardew Valley", "Cuphead"],
    "ps3": ["Demon's Souls", "Heavy Rain"],
}
EXTENSIONS = {"nes": {".nes"}, "snes": {".sfc"}, "megadrive": {".md"}, "gb": {".gb"}, "psx": {".cue"}, "switch": {".nsp"},
              "windows": {".pc", ".wine"}, "ps3": {".iso"}}
IMAGE_VIDEO = re.compile(r"\.(png|jpe?g|gif|webp|bmp|tiff?|ico|icns|svg|mp4|m4v|webm|mkv|avi|mov|m3u8|ts)$", re.I)
NOTICE = ("Trademarks and artwork. The screenshots in this folder show the names, box art and in-game pictures of commercial "
          "video games and consoles. All of them belong to their respective owners and are shown only to illustrate how this "
          "tool works. This project is not affiliated with, endorsed by or sponsored by any of them. The artwork is not part "
          "of this project's source code, is not covered by its MIT licence, and is not needed to run the program. If you are "
          "a rights holder and want an image removed, open an issue at https://github.com/eknowledger/scrappy/issues "
          "and it will be removed promptly.")


# ---------------------------------------------------------------- the list of titles
def test_sixteen_games_on_eight_systems_two_each():
    assert len(rs.GAMES) == 16 and len({g.slug for g in rs.GAMES}) == 16
    assert [s.folder for s in rs.REAL_SYSTEMS] == list(EXPECTED)
    assert Counter(g.system for g in rs.GAMES) == {s: 2 for s in EXPECTED}
    for system, titles in EXPECTED.items():
        assert [g.title for g in rs.GAMES if g.system == system] == titles


def test_rom_names_have_the_extensions_of_their_system():
    exts = {s.folder: {e.lower() for e in s.extensions.split()} for s in rs.REAL_SYSTEMS}
    for g in rs.GAMES:
        suffix = Path(g.rom).suffix
        assert suffix in EXTENSIONS[g.system], g.rom
        assert suffix.lower() in exts[g.system], g.rom
        assert g.rom == g.rom.strip() and "/" not in g.rom


def test_rom_names_look_like_real_dumps():
    by = {g.title: g.rom for g in rs.GAMES}
    assert by["Super Metroid"] == "Super Metroid (Japan, USA) (En,Ja).sfc"
    for g in rs.GAMES:
        if g.system in ("nes", "snes", "megadrive", "gb", "psx", "ps3"):
            assert re.search(r"\((USA|World|Japan|Europe)[^)]*\)", g.stem), g.rom        # a No-Intro / Redump region tag
        if g.system == "switch":
            assert g.rom.endswith("[v0].nsp") and not re.search(r"\[[0-9A-F]{16}\]", g.rom)   # no title id we could not confirm
        if g.system == "windows":
            assert g.stem == g.title


def test_state_mix_and_sources():
    assert Counter(g.state for g in rs.GAMES) == {COMPLETE: 5, PARTIAL: 4, ROM_ONLY: 5, DESC_ONLY: 2}
    for g in rs.GAMES:
        steam = g.system in ("switch", "windows")
        assert g.source == (rs.STEAM if steam else rs.LIBRETRO), g.title
        assert bool(g.steam_id) == steam
        assert g.found.get("desc") and 1 <= g.found["desc"].count(". ") + 1 <= 3, g.title        # 1 to 3 sentences
        assert g.found.get("developer") and g.found.get("releasedate", "").endswith("T000000")


def test_the_cache_lives_outside_the_repository(monkeypatch, tmp_path):
    monkeypatch.delenv(rs.CACHE_ENV, raising=False)
    assert rs.cache_dir() == Path.home() / ".cache" / "scrappy-demo-art"
    assert ROOT not in rs.cache_dir().parents
    monkeypatch.setenv(rs.CACHE_ENV, str(tmp_path / "elsewhere"))
    assert rs.cache_dir() == tmp_path / "elsewhere"


# ---------------------------------------------------------------- libretro file names
@pytest.mark.parametrize("name, expected", [
    ("Super Metroid (Japan, USA) (En,Ja)", "Super Metroid (Japan, USA) (En,Ja).png"),
    ("Sonic & Knuckles", "Sonic _ Knuckles.png"),
    ("Ratchet & Clank", "Ratchet _ Clank.png"),
    ("Ninokuni: Shiroki Seihai", "Ninokuni_ Shiroki Seihai.png"),
    ("What? <1> * 2 | 3 \\ 4 / 5 `6`", "What_ _1_ _ 2 _ 3 _ 4 _ 5 _6_.png"),
    ("Demon's Souls (USA)", "Demon's Souls (USA).png"),                      # the apostrophe stays
])
def test_libretro_file_name_replaces_the_forbidden_characters(name, expected):
    assert rs.libretro_file_name(name) == expected
    assert rs.libretro_file_name(name, "") == expected[:-4]


def test_libretro_urls_are_escaped_and_point_at_the_right_folder():
    url = rs.libretro_url("Sony_-_PlayStation", "boxart", "Final Fantasy VII (USA) (Disc 1)")
    assert url == ("https://raw.githubusercontent.com/libretro-thumbnails/Sony_-_PlayStation/master/Named_Boxarts/"
                   "Final%20Fantasy%20VII%20%28USA%29%20%28Disc%201%29.png")
    assert rs.libretro_url("Sega_-_Mega_Drive_-_Genesis", "snap", "Sonic & Knuckles").endswith(
        "/Named_Snaps/Sonic%20_%20Knuckles.png")
    assert rs.libretro_url("Nintendo_-_Game_Boy", "title", "Tetris (World) (Rev 1)").endswith("/Named_Titles/Tetris%20%28World%29%20%28Rev%201%29.png")
    assert "%27" in rs.libretro_url("Sony_-_PlayStation_3", "snap", "Demon's Souls (USA)")
    with pytest.raises(ValueError):
        rs.libretro_url("Sony_-_PlayStation", "logo", "x")


def test_libretro_names_of_the_games():
    repos = set(rs.LIBRETRO_REPOS.values())
    assert repos == {"Nintendo_-_Nintendo_Entertainment_System", "Nintendo_-_Super_Nintendo_Entertainment_System",
                     "Sega_-_Mega_Drive_-_Genesis", "Nintendo_-_Game_Boy", "Sony_-_PlayStation", "Sony_-_PlayStation_3"}
    for g in rs.GAMES:
        if g.source != rs.LIBRETRO:
            continue
        assert g.system in rs.LIBRETRO_REPOS
        for kind in rs.LIBRETRO_FOLDERS:
            assert g.libretro_name(kind) in (g.stem, "Heavy Rain (USA)")
            url = rs.libretro_url(rs.LIBRETRO_REPOS[g.system], kind, g.libretro_name(kind))
            assert url.startswith("https://raw.githubusercontent.com/") and " " not in url
    heavy = next(g for g in rs.GAMES if g.title == "Heavy Rain")
    assert heavy.libretro_name("boxart") == "Heavy Rain (USA)" and heavy.libretro_name("snap") == heavy.stem


# ---------------------------------------------------------------- the fetch tool (no network)
class FakeHttp:
    def __init__(self, answers):
        self.answers, self.calls, self.errors = dict(answers), [], 0

    def get(self, url, accept=""):
        self.calls.append(url)
        return self.answers.get(url)


def png(size=(40, 30), colour=(200, 40, 40)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, "PNG")
    return out.getvalue()


def test_fetch_is_idempotent_and_remembers_what_the_source_does_not_have(tmp_path):
    game = rs.GAME_BY_SLUG["super-metroid"]
    url = rs.libretro_url(rs.LIBRETRO_REPOS["snes"], "boxart", game.stem)
    http = FakeHttp({url: png()})
    f = fetch.Fetcher(tmp_path, http)
    assert f.picture(game, "boxart", "boxart", "boxart", url) is True
    assert f.picture(game, "boxart", "boxart", "boxart", url) is True
    assert http.calls == [url]                                                  # the second call used the cache
    snap = rs.libretro_url(rs.LIBRETRO_REPOS["snes"], "snap", game.stem)
    assert f.picture(game, "image", "snap", "snap", snap) is False             # 404
    assert f.picture(game, "image", "snap", "snap", snap) is False
    assert http.calls == [url, snap]                                            # asked only once
    again = rs.ArtCache(tmp_path)                                               # the index on disk tells the same
    entry = again.entries(game.slug, "boxart")[0]
    assert entry["url"] == url and again.path(entry).read_bytes()[:4] == b"\x89PNG"
    assert rs.ArtCache(tmp_path).by_url() == {url: again.path(entry)}


def test_a_libretro_symlink_file_is_followed_to_its_target(tmp_path):
    game = rs.GAME_BY_SLUG["final-fantasy-vii"]
    base = rs.libretro_url(rs.LIBRETRO_REPOS["psx"], "boxart", game.stem)
    target = base.rsplit("/", 1)[0] + "/" + "Final%20Fantasy%20VII%20%28USA%29.png"
    http = FakeHttp({base: b"Final Fantasy VII (USA).png", target: png()})
    assert fetch.Fetcher(tmp_path, http).picture(game, "boxart", "boxart", "boxart", base) is True
    assert http.calls == [base, target]
    assert fetch.symlink_target(b"<html>no</html>", base) == "" and fetch.symlink_target(b"a/b.png", base) == ""
    assert fetch.symlink_target(png(), base) == ""


def test_html_is_never_kept_as_a_picture(tmp_path):
    game = rs.GAME_BY_SLUG["tetris"]
    http = FakeHttp({"https://x/y": b"<html>blocked</html>"})
    assert fetch.Fetcher(tmp_path, http).picture(game, "boxart", "boxart", "boxart", "https://x/y") is False
    assert not (tmp_path / "files").exists()
    assert fetch.image_extension(png()) == ".png" and fetch.image_extension(b"\xff\xd8\xff\xe0") == ".jpg"


def test_http_only_talks_to_the_listed_hosts_and_stops_politely(tmp_path):
    http = fetch.Http(tmp_path, pause=0)
    http.session = None                                       # any real request would blow up
    for bad in ("https://example.com/a.png", "http://raw.githubusercontent.com/a.png", "https://store.steampowered.com.evil.org/x"):
        with pytest.raises(fetch.Stop):
            http.get(bad)
    assert http.counts["total"] == 0 and not (tmp_path / "requests.log").exists()
    assert set(fetch.ALLOWED_HOSTS) == {"store.steampowered.com", "shared.akamai.steamstatic.com", "cdn.cloudflare.steamstatic.com",
                                        "cdn.akamai.steamstatic.com", "api.github.com", "raw.githubusercontent.com"}
    assert "scrappy-demo-tool/1.0" in fetch.USER_AGENT and "github.com/eknowledger" in fetch.USER_AGENT


class FakeSession:
    def __init__(self, statuses):
        self.statuses, self.headers, self.urls = list(statuses), {}, []

    def get(self, url, timeout=None, headers=None):
        self.urls.append(url)
        status = self.statuses.pop(0)
        return type("R", (), {"status_code": status, "content": b"data"})()


def test_http_counts_requests_stops_after_repeated_errors_and_at_the_limit(tmp_path):
    http = fetch.Http(tmp_path, pause=0, limit=10)
    http.session = FakeSession([200, 404, 500, 500, 500, 200])
    url = "https://raw.githubusercontent.com/x/y.png"
    assert http.get(url) == b"data" and http.get(url) is None
    assert http.get(url) is None and http.get(url) is None
    with pytest.raises(fetch.Stop, match="errors in a row"):
        http.get(url)
    assert json.loads((tmp_path / "requests.json").read_text())["hosts"] == {"raw.githubusercontent.com": 5}
    http.session = FakeSession([429])
    with pytest.raises(fetch.Stop, match="slow down"):
        http.get(url)
    capped = fetch.Http(tmp_path, pause=0, limit=6)                  # the count survives between runs
    capped.session = FakeSession([200])
    with pytest.raises(fetch.Stop, match="limit"):
        capped.get(url)
    assert capped.session.urls == []


# ---------------------------------------------------------------- a synthetic cache
def synthetic_cache(folder: Path) -> Path:
    """The cache layout of fetch_demo_art.py, filled with generated pictures (flat colours with a number on them)."""
    games = []
    for n, g in enumerate(rs.GAMES):
        entry = fetch.new_entry(g)
        colour = (30 + 12 * n, 90, 200 - 8 * n)
        host = f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{g.steam_id}" if g.steam_id \
            else f"https://synthetic.example/{g.slug}"

        def add(slot, role, name, size, ext=".png"):
            rel = f"files/{g.slug}/{name}{ext}"
            (folder / rel).parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGBA" if slot == "logo" else "RGB", size, colour + ((255,) if slot == "logo" else ())).save(folder / rel)
            entry["slots"][slot].append({"file": rel, "url": f"{host}/{name}{ext}", "role": role,
                                         "name": name})

        add("boxart", "library" if g.source == rs.STEAM else "boxart", "box", (300, 450))
        if g.source == rs.STEAM:
            add("logo", "logo", "logo", (300, 120))
            for i in (1, 2, 3):
                add("image", "screenshot", f"screenshot-{i}", (480, 270))
            add("image", "header", "header", (460, 215))
            add("image", "hero", "hero", (620, 200))
            add("video", "movie-thumb", "movie-thumb", (293, 165))
        else:
            add("image", "snap", "snap", (256, 224))
            if n % 2 == 0:
                add("image", "title", "title", (256, 224))
        games.append(entry)
        if g.source == rs.STEAM:                                   # what the Steam API would have said (synthetic text)
            (folder / "steam").mkdir(exist_ok=True)
            (folder / "steam" / f"{g.steam_id}-search.json").write_text(json.dumps({"total": 2, "items": [
                {"type": "app", "id": g.steam_id, "name": g.title},
                {"type": "app", "id": 1, "name": g.title + " Soundtrack"},
                {"type": "app", "id": 2, "name": "Something Unrelated Entirely"}]}))
            (folder / "steam" / f"{g.steam_id}-details.json").write_text(json.dumps({str(g.steam_id): {"success": True, "data": {
                "name": g.title, "steam_appid": g.steam_id, "type": "game", "short_description": "STORE BLURB MUST NOT APPEAR",
                "developers": ["Synthetic Dev"], "publishers": ["Synthetic Pub"], "genres": [{"description": "Action"}],
                "release_date": {"coming_soon": False, "date": "Feb 3, 2020"}, "categories": [{"description": "Single-player"}],
                "screenshots": [{"path_full": f"{host}/screenshot-{i}.png"} for i in (1, 2, 3)],
                "header_image": f"{host}/header.png",
                "movies": [{"name": "Official trailer", "thumbnail": f"{host}/movie-thumb.png", "hls_h264": f"{host}/hls.m3u8"}]}}}))
    (folder / "index.json").write_text(json.dumps({"version": 1, "games": games}))
    return folder


@pytest.fixture(scope="module")
def cache(tmp_path_factory):
    return rs.ArtCache(synthetic_cache(tmp_path_factory.mktemp("synthetic-art")))


@pytest.fixture(scope="module")
def env(cache, tmp_path_factory):
    e = rs.build_real_demo(tmp_path_factory.mktemp("real-demo"), delay=False, cache=cache)
    yield e
    e.cleanup()


def test_the_synthetic_cache_satisfies_the_minimum(cache):
    assert cache.missing() == []
    assert len(cache.by_url()) == sum(len(cache.entries(g.slug, s)) for g in rs.GAMES for s in ("logo", "boxart", "image", "video"))


def test_the_library_has_eight_systems_and_the_state_mix(env):
    ctl = env.controller
    assert sorted(ctl.list_systems()) == sorted(EXPECTED)
    complete = missing = 0
    for system in EXPECTED:
        rows = ctl.rows(system)
        assert len(rows) == 2
        complete += sum(1 for r in rows if r.complete)
        missing += sum(1 for r in ctl.rows(system, only_missing=True))
    assert (complete, missing) == (5, 11)
    assert len(env.games) == 16
    roms = list((env.root / "roms").glob("*/*"))
    assert all(p.stat().st_size == 1 for p in roms if p.suffix in set().union(*EXTENSIONS.values()))
    assert (env.root / "roms" / "snes" / "Super Metroid (Japan, USA) (En,Ja).sfc").is_file()


def test_complete_and_partial_games_get_their_pictures_from_the_cache(env):
    sdir = env.root / "roms" / "switch" / "images"
    assert {p.name for p in sdir.iterdir()} == {"Hades [v0]-image.jpg", "Hades [v0]-thumb.png", "Hades [v0]-marquee.png"}
    assert Image.open(sdir / "Hades [v0]-image.jpg").size == (640, 360)
    assert Image.open(sdir / "Hades [v0]-thumb.png").size == (396, 640)
    assert (env.root / "roms" / "windows" / "videos" / "Cuphead-video.mp4").stat().st_size > 0


def test_steam_titles_are_answered_by_the_real_steam_code_from_the_cache(env):
    ctl = env.controller
    found = ctl.search("Stardew Valley", "windows")
    steam = [m for m in found.matches if m.source == "Steam"]
    assert steam and steam[0].title == "Stardew Valley" and steam[0].source_id == "413150"
    assert all(m.score >= rs.MIN_SEARCH_SCORE for m in steam)                       # no loosely related entries
    assert not [m for m in found.matches if m.source == rs.LIBRETRO]
    info = ctl.load_info(steam[0])
    assert info.source == "Steam" and info.release_date == "20200203T000000" and info.developer == "Synthetic Dev"
    assert "STORE BLURB" not in info.description and info.description == rs.GAME_BY_SLUG["stardew-valley"].found["desc"]
    slots = Counter(a.slot for a in info.assets)
    assert slots["logo"] == 1 and slots["boxart"] == 1 and slots["image"] >= 3 and slots["video"] == 1
    assert {a.source for a in info.assets} == {"Steam"}
    labels = {a.label for a in info.assets}
    assert {"Steam screenshot", "Steam header", "Steam library box art", "Steam library hero", "Steam logo", "Trailer"} <= labels
    assert next(a for a in info.assets if a.slot == "logo").transparent
    for a in info.assets:                                                           # every picture is served from the cache
        if a.slot != "video":
            assert Image.open(io.BytesIO(ctl.fetch_bytes(a.url))).size[0] > 0
        else:
            assert Image.open(io.BytesIO(ctl.fetch_bytes(a.thumb_url))).size == (293, 165)
    with pytest.raises(ProviderError):
        ctl.load_info(Match("Stardew Valley Soundtrack", "Steam", "1"))             # a Steam entry that is not a game of the demo


def test_retro_titles_are_answered_under_the_libretro_name(env):
    ctl = env.controller
    found = ctl.search("Chrono Trigger", "snes")
    assert [m.source for m in found.matches] == [rs.LIBRETRO] and found.matches[0].title == "Chrono Trigger"
    info = ctl.load_info(found.matches[0])
    assert info.developer == "Square" and info.release_date == "19950822T000000"
    assert {a.source for a in info.assets} == {rs.LIBRETRO}
    labels = [(a.slot, a.label) for a in info.assets]
    assert ("boxart", "Box art") in labels and ("image", "In-game screenshot") in labels
    assert any(slot == "logo" and "Text logo" in label for slot, label in labels)
    logo = next(a for a in info.assets if a.slot == "logo")
    assert Image.open(io.BytesIO(ctl.fetch_bytes(logo.url))).size == (600, 300)    # the program's own generated text logo
    assert all(a.url.startswith("https://synthetic.example/") for a in info.assets if a.slot in ("boxart", "image"))
    assert ctl.search("Something That Is Not There", "snes").matches == []


def test_the_trailer_candidate_can_be_cut_and_youtube_has_a_stand_in(env):
    ctl = env.controller
    found = ctl.search("Hades", "switch")
    assert [v.label for v in found.videos] == ["Hades gameplay [1:30]"]
    info = ctl.load_info(next(m for m in found.matches if m.source == "Steam"))
    trailer = next(a for a in info.assets if a.slot == "video")
    assert trailer.label == "Trailer" and trailer.kind == "hls"
    clip = ctl.make_video(trailer, 0.0, 5.0)
    assert clip.stat().st_size > 0
    got = ctl.make_video(found.videos[0], 0.0, 5.0)
    assert got.stat().st_size > 0


def test_nothing_in_the_demo_touches_the_network(cache, tmp_path, monkeypatch):
    import socket
    import requests

    def refuse(*a, **k):
        raise AssertionError("network used")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(requests.Session, "request", refuse)
    e = rs.build_real_demo(tmp_path / "offline", delay=False, cache=cache)
    try:
        for title, system in (("Celeste", "switch"), ("Tetris", "gb")):
            found = e.controller.search(title, system)
            e.controller.load_info(found.matches[0])
    finally:
        e.cleanup()


def test_an_incomplete_cache_is_refused_with_a_hint(tmp_path):
    with pytest.raises(ProviderError, match=r"fetch_demo_art\.py"):
        rs.build_real_demo(tmp_path / "x", cache=rs.ArtCache(tmp_path / "empty"))
    partial = tmp_path / "partial"
    synthetic_cache(partial)
    entry = json.loads((partial / "index.json").read_text())
    entry["games"][0]["slots"]["boxart"] = []                                       # one game without its cover
    (partial / "index.json").write_text(json.dumps(entry))
    missing = rs.ArtCache(partial).missing()
    assert [(g.title, w) for g, w in missing] == [("The Legend of Zelda", ["boxart"])]
    assert not (tmp_path / "x").exists()


def test_the_slideshow_is_a_real_clip_made_from_the_stills(cache, tmp_path):
    game = rs.GAME_BY_SLUG["hades"]
    frames = rs.slideshow_frames(cache.stills(game), seconds=1, fps=4)
    data = list(frames)
    assert len(data) == 4 and all(len(f) == 640 * 360 * 3 for f in data)
    with pytest.raises(ProviderError):
        list(rs.slideshow_frames([]))
    if not FFMPEG:
        pytest.skip("ffmpeg not installed")
    out = rs.make_slideshow(game, cache, tmp_path / "hades.mp4")
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height,duration", "-of", "csv",
                            str(out)], capture_output=True, text=True).stdout
    assert "h264,640,360,6.0" in probe.replace("\n", "")
    assert rs.make_slideshow(game, cache, out) == out                               # made once


# ---------------------------------------------------------------- the tools as programs
def run_tool(args, env_extra, **kw):
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", **env_extra}
    return subprocess.run([sys.executable, *args], capture_output=True, text=True, env=env, timeout=120, **kw)


def test_real_screenshots_fail_cleanly_on_an_empty_cache(tmp_path):
    pytest.importorskip("PyQt5.QtWidgets")
    out = tmp_path / "shots"
    done = run_tool([str(ROOT / "tools" / "make_screenshots.py"), "--real", "--out", str(out)],
                    {rs.CACHE_ENV: str(tmp_path / "no-cache-here")})
    assert done.returncode == 1
    assert "fetch_demo_art.py" in done.stderr and "Traceback" not in done.stderr
    assert not out.exists() or not list(out.iterdir())
    assert not (tmp_path / "no-cache-here").exists()                                # nothing was created either


def test_fetch_tool_lists_the_cache_and_reports_missing_art_by_exit_code(tmp_path):
    empty = run_tool([str(ROOT / "tools" / "fetch_demo_art.py"), "--list"], {rs.CACHE_ENV: str(tmp_path / "empty")})
    assert empty.returncode == 1 and empty.stdout.count("MISSING") == 16
    assert not (tmp_path / "empty").exists()                                        # --list never creates or fetches anything
    full = synthetic_cache(tmp_path / "full")
    ok = run_tool([str(ROOT / "tools" / "fetch_demo_art.py"), "--list"], {rs.CACHE_ENV: str(full)})
    assert ok.returncode == 0 and ok.stdout.count("OK") == 16 and "MISSING" not in ok.stdout
    assert not (full / "requests.json").exists()
    one = run_tool([str(ROOT / "tools" / "fetch_demo_art.py"), "--list", "--only", "hades"], {rs.CACHE_ENV: str(full)})
    assert one.returncode == 0 and one.stdout.count("OK") == 1


def test_real_screenshots_run_on_a_synthetic_cache(tmp_path):
    pytest.importorskip("PyQt5.QtWidgets")
    out = tmp_path / "shots"
    done = run_tool([str(ROOT / "tools" / "make_screenshots.py"), "--real", "--out", str(out)],
                    {rs.CACHE_ENV: str(synthetic_cache(tmp_path / "art"))})
    assert done.returncode == 0, done.stderr[-2000:]
    names = ["01-library.png", "02-current.png", "03-search-results.png", "04-review-dialog.png", "05-saved.png", "06-trailer.png"]
    assert sorted(p.name for p in out.iterdir()) == sorted(names)
    assert Image.open(out / "01-library.png").size == (1600, 900) and Image.open(out / "06-trailer.png").width >= 400


# ---------------------------------------------------------------- nothing third-party in git
def tracked_files():
    try:
        done = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("git is not available")
    if done.returncode != 0:
        pytest.skip("not a git checkout")
    return [f for f in done.stdout.split("\0") if f]


ALLOWED_PICTURE_PLACES = ("docs/screenshots/", "assets/")
ALLOWED_PICTURE_FILES = {"deploy/icon.png"}                          # the program's own icon


def test_no_picture_or_video_is_tracked_outside_the_allowed_places():
    stray = [f for f in tracked_files() if IMAGE_VIDEO.search(f) and not f.startswith(ALLOWED_PICTURE_PLACES)
             and f not in ALLOWED_PICTURE_FILES]
    assert stray == [], f"pictures or videos in git outside docs/screenshots/ and assets/: {stray}"


ANNOTATED = ["after-save", "current-vs-new", "details-form", "missing-list", "review-dialog", "slot-panel", "tools-menu",
             "trailer-panel", "window-tour", "task-dialog-running", "task-dialog-result",
             "task-dialog-failed"]                                   # the invented-library pictures of docs/USAGE.md


def test_only_the_screenshots_and_their_notice_are_tracked_in_the_screenshot_folder():
    names = sorted(f for f in tracked_files() if f.startswith("docs/screenshots/"))
    assert names == sorted(["docs/screenshots/01-library.png", "docs/screenshots/02-current.png", "docs/screenshots/03-search-results.png",
                            "docs/screenshots/04-review-dialog.png", "docs/screenshots/05-saved.png", "docs/screenshots/06-trailer.png",
                            "docs/screenshots/07-desktop.png", "docs/screenshots/README.md", "docs/screenshots/annotated/spec.json"]
                           + [f"docs/screenshots/annotated/{name}.png" for name in ANNOTATED])


def test_no_cache_files_or_art_folders_are_tracked():
    bad = [f for f in tracked_files() if re.search(r"scrappy-demo-art|index\.json$|requests\.(json|log)$|(^|/)listings/|(^|/)files/", f)]
    assert bad == []


def test_the_screenshot_folder_carries_the_legal_notice_and_a_table():
    text = (ROOT / "docs" / "screenshots" / "README.md").read_text(encoding="utf-8")
    assert " ".join(text.split()).count(NOTICE) == 1
    shown = ["The Legend of Zelda", "Mega Man 2", "Hades", "Celeste", "Stardew Valley", "Cuphead"]    # the titles visible in the pictures
    assert {g.title for g in rs.GAMES} >= set(shown)
    for title in shown:
        assert f"| {title} |" in text, title
    assert "libretro" in text and "Steam" in text and "never committed" in text
