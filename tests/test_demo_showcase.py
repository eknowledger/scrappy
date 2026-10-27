"""The showcase library behind ``python -m scrappy.app --demo``: 15 invented games on 5 retro systems, procedural pixel art,
short real trailers and made-up sources that know every game. No network, no real files."""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import pytest
from PIL import Image

from scrappy.models import SLOTS
from scrappy.processing.images import BOX_SIZE, IMAGE_SIZE, LOGO_SIZE
from scrappy.processing.video import probe_duration
from scrappy.providers.base import ProviderError
from scrappy.titles import clean_title
from scrappy.ui import demo, demo_art, demo_video, pipeline
from scrappy.ui.demo import build_demo, build_minimal_demo, demo_picture
from scrappy.ui.demo_data import COMPLETE, DESC_ONLY, GAMES, MEDIA, PARTIAL, ROM_ONLY, STATE_COUNTS, SYSTEMS, all_texts
from scrappy.roms import rom_extensions

ROOT = Path(__file__).resolve().parent.parent
FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
SYSTEM_FOLDERS = ["nes", "snes", "megadrive", "gb", "psx"]

# Words of real consoles, companies and famous games: none may appear in anything the demo shows.
DENY_LIST = """
nintendo sega sony playstation capcom konami atari namco square enix activision ubisoft bandai taito hudson irem jaleco
tecmo snk midway acclaim codemasters eidos psygnosis infogrames natsume sunsoft koei ea microsoft xbox famicom genesis
mario luigi wario yoshi zelda ganon sonic tails pokemon pikachu kirby metroid samus kong crash bandicoot spyro tekken
megaman castlevania contra tetris pacman lemmings earthworm bomberman rayman gex doom
""".split()
DENY_PHRASES = ["electronic arts", "street fighter", "final fantasy", "dragon quest", "metal gear", "resident evil",
                "gran turismo", "tomb raider", "mega man", "pac-man", "donkey kong", "data east", "game boy", "mega drive",
                "bubble bobble", "super nintendo", "master system"]


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    a, b = (build_demo(tmp_path_factory.mktemp(f"showcase{i}"), delay=False) for i in (1, 2))
    yield a, b
    a.cleanup()
    b.cleanup()


@pytest.fixture(scope="module")
def env(built):
    return built[0]


def gamelist(env, folder):
    path = env.root / "roms" / folder / "gamelist.xml"
    if not path.exists():
        return {}
    return {g.findtext("path"): {c.tag: (c.text or "") for c in g} for g in ET.parse(path).getroot().iter("game")}


# ---------------------------------------------------------------- the library

def test_fifteen_games_on_exactly_five_systems(env):
    ctl = env.controller
    assert sorted(ctl.list_systems()) == sorted(SYSTEM_FOLDERS)
    assert len(GAMES) == 15 and Counter(g.system for g in GAMES) == {s: 3 for s in SYSTEM_FOLDERS}
    assert [s.folder for s in SYSTEMS] == SYSTEM_FOLDERS
    for folder in SYSTEM_FOLDERS:
        assert len(ctl.rows(folder)) == 3, folder


def test_roms_are_one_byte_files_with_extensions_the_system_list_accepts(env):
    ctl = env.controller
    for game in GAMES:
        rom = env.root / "roms" / game.system / game.rom
        assert rom.stat().st_size == 1, game.rom
        assert rom.suffix.lower() in rom_extensions(game.system, ctl.es_configs), game.rom
        assert game.rom.startswith(game.title) and re.search(r"\((USA|Europe|World|USA, Europe)\)", game.rom)
        assert clean_title(game.rom) == game.title
    cfg = ET.parse(env.root / "es_systems.cfg").getroot()
    assert [s.findtext("name") for s in cfg.iter("system")] == SYSTEM_FOLDERS
    assert all(s.findtext("fullname") and s.findtext("extension") for s in cfg.iter("system"))
    assert Counter(Path(g.rom).suffix for g in GAMES if g.system == "psx") == {".chd": 2, ".cue": 1}


def test_state_mix_counts_from_the_files_and_the_gamelist(env):
    states = {}
    for folder in SYSTEM_FOLDERS:
        entries = gamelist(env, folder)
        sdir = env.root / "roms" / folder
        for row in env.controller.rows(folder):
            entry = entries.get(row.rom_path, {})
            files = [t for t in MEDIA if row.status.get(t)]
            if row.complete:
                state = COMPLETE
            elif not entry and not files:
                state = ROM_ONLY
            elif set(entry) <= {"path", "name", "desc"} and entry.get("desc") and not files:
                state = DESC_ONLY
            else:
                state = PARTIAL
            states[row.title] = state
            for tag in files:
                assert (sdir / entry[tag][2:]).stat().st_size > 0
    assert len(states) == 15
    assert Counter(states.values()) == STATE_COUNTS == {COMPLETE: 5, PARTIAL: 4, ROM_ONLY: 4, DESC_ONLY: 2}
    assert states == {g.title: g.state for g in GAMES}
    for folder in SYSTEM_FOLDERS:                                    # every system looks different
        assert len({g.state for g in GAMES if g.system == folder}) >= 2
    partial = {g.title: g for g in GAMES if g.state == PARTIAL}
    assert any(not g.have for g in partial.values()) and any("video" not in g.have for g in partial.values())
    assert any("marquee" not in g.have and g.have for g in partial.values())


def test_every_picture_decodes_with_the_projects_sizes(env):
    seen = Counter()
    for game in GAMES:
        sdir = env.root / "roms" / game.system
        for tag, path in ((t, sdir / p[2:]) for t, p in demo.media_rel_paths(game.rom).items()):
            if tag not in game.media or tag == "video":
                continue
            im = Image.open(path)
            im.load()
            seen[tag] += 1
            if tag == "image":
                assert im.format == "JPEG" and im.size == IMAGE_SIZE and im.mode == "RGB"
            elif tag == "thumbnail":
                assert im.format == "PNG" and im.size == BOX_SIZE and im.mode == "RGBA"
            else:
                assert im.format == "PNG" and im.size == LOGO_SIZE and im.mode == "RGBA"
                assert im.getchannel("A").getextrema()[0] == 0, "the logo needs real transparency"
    assert seen["image"] == sum("image" in g.media for g in GAMES) == 8
    assert seen["thumbnail"] == 7 and seen["marquee"] == 7


@pytest.mark.skipif(not FFMPEG, reason="needs ffmpeg and ffprobe")
def test_the_trailers_are_real_small_mp4_files(env):
    n = 0
    for game in GAMES:
        if "video" not in game.media:
            continue
        path = env.root / "roms" / game.system / "videos" / f"{game.stem}-video.mp4"
        info = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout)
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        assert (video["codec_name"], video["pix_fmt"], video["width"], video["height"], video["r_frame_rate"]) == \
            ("h264", "yuv420p", 640, 360, "24/1")
        assert 5.5 <= probe_duration(str(path)) <= 6.5 and path.stat().st_size < 600_000
        n += 1
    assert n == 6
    assert env.controller._probe(str(path)) == pytest.approx(6.0, abs=0.5)         # the demo's probe works on the real files


def test_two_builds_make_identical_pictures(built):
    a, b = built
    names = sorted(p.relative_to(a.root / "roms").as_posix() for p in (a.root / "roms").rglob("*") if p.suffix in (".jpg", ".png"))
    assert len(names) == 8 + 7 + 7 and names == sorted(
        p.relative_to(b.root / "roms").as_posix() for p in (b.root / "roms").rglob("*") if p.suffix in (".jpg", ".png"))
    for name in names:
        assert (a.root / "roms" / name).read_bytes() == (b.root / "roms" / name).read_bytes(), name
    for url in ("demo://g/orbit-otters/shot-3", "demo://g/orbit-otters/box-b", "demo://g/orbit-otters/logo-solid",
                "demo://g/orbit-otters/thumb-hls"):
        demo._showcase_picture.cache_clear()
        first = demo_picture(url)
        demo._showcase_picture.cache_clear()
        assert demo_picture(url) == first


# ---------------------------------------------------------------- nothing real in the texts

def test_no_real_brand_or_company_word_anywhere():
    texts = []
    for game in GAMES:
        texts += all_texts(game)
    for system in SYSTEMS:
        texts += [system.fullname, system.band]
    texts += [demo.SHOWCASE_SOURCE]
    assert len(texts) > 100
    for text in texts:
        words = re.findall(r"[a-z0-9]+", text.lower().replace("'", ""))
        hit = [w for w in words if w in DENY_LIST]
        assert not hit, f"{hit} in {text!r}"
        for phrase in DENY_PHRASES:
            assert phrase not in text.lower(), f"{phrase!r} in {text!r}"


def test_titles_are_unique_and_every_game_is_fully_described():
    assert len({g.title for g in GAMES}) == 15 and len({g.slug for g in GAMES}) == 15
    for g in GAMES:
        info = {**g.lib, **g.found}
        assert re.fullmatch(r"19(89|9\d)\d{4}T000000", info["releasedate"]), g.title
        assert 1989 <= int(info["releasedate"][:4]) <= 1999
        assert g.found["desc"] and 2 <= len(re.findall(r"[.!?](?:\s|$)", g.found["desc"])) <= 4, g.title
        if g.lib.get("desc"):
            assert 2 <= len(re.findall(r"[.!?](?:\s|$)", g.lib["desc"])) <= 4, g.title
            assert g.lib["desc"] != g.found["desc"]
        for key in ("developer", "publisher", "genre", "players"):
            assert g.found.get(key), (g.title, key)
        for key in ("releasedate", "developer", "genre", "players"):      # the made-up source differs from the library
            if g.lib.get(key):
                assert g.lib[key] != g.found[key], (g.title, key)


# ---------------------------------------------------------------- the made-up sources

@pytest.mark.parametrize("game", GAMES, ids=lambda g: g.slug)
def test_every_game_can_be_searched_and_fetched(env, game):
    ctl = env.controller
    res = ctl.search(game.title, game.system)
    assert not res.errors and res.matches[0].title == game.title and res.matches[0].score >= 0.9
    assert [v.kind for v in res.videos] == ["youtube"] and res.videos[0].thumb_url
    info = ctl.load_info(res.matches[0])
    assert info.description and info.developer and info.publisher and info.release_date and info.genre and info.players
    slots = Counter(a.slot for a in info.assets) + Counter(a.slot for a in res.videos)
    assert all(slots[s] >= 1 for s in SLOTS)
    assert Counter(a.slot for a in info.assets if a.slot != "video") == {"logo": 2, "boxart": 2, "image": 4}
    assert sorted(a.transparent for a in info.assets_for("logo")) == [False, True]
    assert info.assets_for("video")[0].kind == "hls" and info.assets_for("video")[0].thumb_url
    broken = 0
    for asset in info.assets:
        if asset.slot == "video":
            data = ctl.fetch_bytes(asset.thumb_url)
            assert Image.open(io.BytesIO(data)).size == (320, 180)
            continue
        try:
            data = ctl.fetch_bytes(asset.url)
        except ProviderError:
            broken += 1
            continue
        processed = pipeline.process(asset.slot, data, remove_bg=False)
        im = Image.open(io.BytesIO(processed.data))
        assert im.size == {"logo": LOGO_SIZE, "boxart": BOX_SIZE, "image": IMAGE_SIZE}[asset.slot]
    assert broken == 1                                                       # exactly one picture fails to load
    assert pipeline.process("logo", ctl.fetch_bytes(info.assets_for("logo")[0].url), remove_bg=True).warning == ""
    assert "transparent" in pipeline.process("logo", ctl.fetch_bytes(info.assets_for("logo")[1].url)).note
    sizes = {Image.open(io.BytesIO(ctl.fetch_bytes(a.url))).size for a in info.assets_for("image") if "missing" not in a.url}
    assert (960, 720) in sizes and (1280, 720) in sizes                      # one screenshot is in a different style


def test_found_text_differs_from_the_library_so_review_has_something_to_compare(env):
    ctl = env.controller
    differing = 0
    for game in GAMES:
        if game.state == ROM_ONLY:
            continue
        info = ctl.load_info(ctl.search(game.title).matches[0])
        for key, value in (("desc", info.description), ("releasedate", info.release_date), ("developer", info.developer)):
            if game.lib.get(key) and game.lib[key] != value:
                differing += 1
    assert differing >= 25


@pytest.mark.skipif(not FFMPEG, reason="needs ffmpeg and ffprobe")
def test_both_trailer_kinds_can_be_cut_into_clips(env, tmp_path):
    ctl = env.controller
    game = GAMES[3]
    res = ctl.search(game.title)
    info = ctl.load_info(res.matches[0])
    for cand in (info.assets_for("video")[0], res.videos[0]):
        clip = ctl.make_video(cand, 2.0, 20.0)
        assert clip.stat().st_size > 10_000 and 5.5 < probe_duration(str(clip)) < 6.5


def test_encode_stand_in_stops_when_cancelled(tmp_path):
    env = build_demo(tmp_path / "c", delay=True)
    try:
        cand = env.controller.load_info(env.controller.search("Orbit Otters").matches[0]).assets_for("video")[0]
        with pytest.raises(InterruptedError):
            env.controller.make_video(cand, 0.0, 10.0, cancelled=lambda: True)
        assert not list(env.controller.tmp_dir.glob("clip-*.mp4"))
    finally:
        env.cleanup()


def test_a_title_the_demo_does_not_know_finds_nothing(env):
    ctl = env.controller
    res = ctl.search("Completely Unknown Adventure Show", "nes")
    assert res.matches == [] and res.videos == [] and res.errors == {}
    assert demo.ShowcaseProvider(delay=False).search("") == []
    with pytest.raises(ProviderError):
        demo_picture("demo://g/orbit-otters/nope")
    with pytest.raises(ProviderError):
        demo_picture("demo://nothing")


# ---------------------------------------------------------------- fallbacks

@pytest.mark.parametrize("bitmap_only", [False, True], ids=["pillow-default-font", "bitmap-font"])
def test_without_any_font_the_pictures_still_work(monkeypatch, bitmap_only):
    real_truetype = demo_art.ImageFont.truetype

    def no_font_files(font, *args, **kwargs):
        if isinstance(font, str) or bitmap_only:
            raise OSError("no such font")
        return real_truetype(font, *args, **kwargs)          # Pillow's own built-in font is loaded from memory

    monkeypatch.setattr(demo_art.ImageFont, "truetype", no_font_files)
    demo_art.font.cache_clear()
    try:
        assert isinstance(demo_art.font(20), demo_art.ImageFont.ImageFont if bitmap_only else demo_art.ImageFont.FreeTypeFont)
        game = GAMES[5]
        assert demo_art.screenshot(game, 1).size == (1280, 720)
        assert demo_art.screenshot(game, 3).size == (960, 720)
        assert demo_art.box_art(game, 0).size == BOX_SIZE
        assert demo_art.box_art(game, 2).size == BOX_SIZE
        assert Image.open(io.BytesIO(demo_art.library_logo(game))).size == LOGO_SIZE
        assert Image.open(io.BytesIO(demo_art.logo_png(game, 0, solid=True))).mode == "RGBA"
        assert demo_art.video_thumb(game).size == (320, 180)
    finally:
        monkeypatch.undo()
        demo_art.font.cache_clear()


def test_without_ffmpeg_the_library_gets_the_placeholder_and_nothing_crashes(monkeypatch, tmp_path):
    monkeypatch.setattr(demo_video.shutil, "which", lambda name: None)
    monkeypatch.setattr(demo, "probe_duration", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("ffprobe")))
    env = build_demo(tmp_path / "nofmpeg", delay=False)
    try:
        ctl = env.controller
        videos = list((env.root / "roms").rglob("*-video.mp4"))
        assert len(videos) == 6 and all(v.read_bytes() == demo_video.MP4_STUB for v in videos)
        assert sum(r.complete for s in ctl.list_systems() for r in ctl.rows(s)) == 5
        cand = ctl.load_info(ctl.search("Orbit Otters").matches[0]).assets_for("video")[0]
        assert ctl.make_video(cand, 0.0, 10.0).read_bytes() == demo_video.MP4_STUB
        current = ctl.load_current("snes", "./Quasar Quokka (USA).sfc")
        assert current.video is not None and current.video.duration == 45.0
    finally:
        env.cleanup()


# ---------------------------------------------------------------- the old fixture and the screenshot tool

def test_the_minimal_demo_is_still_there(tmp_path):
    env = build_minimal_demo(tmp_path / "m")
    try:
        assert env.controller.list_systems() == ["snes", "switch"]
        assert {r.title for r in env.controller.rows("switch")} == {"Cinder Citadel", "Tidepool Tactics", "Finished Game"}
    finally:
        env.cleanup()
    assert not env.root.exists()


def test_cleanup_removes_everything(tmp_path):
    env = build_demo(tmp_path / "gone", delay=False)
    assert (env.root / "demo-media").is_dir()
    env.cleanup()
    assert not env.root.exists()


def test_screenshot_tool_says_so_when_pyqt5_is_missing(tmp_path):
    code = ("import sys, runpy; sys.modules['PyQt5'] = None; "
            f"runpy.run_path({str(ROOT / 'tools' / 'make_screenshots.py')!r}, run_name='__main__')")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp_path)
    assert done.returncode == 2 and "PyQt5" in done.stderr and "Traceback" not in done.stderr


def test_screenshot_tool_makes_the_six_pictures(tmp_path):
    pytest.importorskip("PyQt5.QtWidgets")
    out = tmp_path / "shots"
    done = subprocess.run([sys.executable, str(ROOT / "tools" / "make_screenshots.py"), "--out", str(out)],
                          capture_output=True, text=True, env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, timeout=240)
    assert done.returncode == 0, done.stderr[-2000:]
    names = ["01-library.png", "02-current.png", "03-search-results.png", "04-review-dialog.png", "05-saved.png", "06-trailer.png"]
    assert sorted(p.name for p in out.iterdir()) == sorted(names)
    for name in names:
        im = Image.open(out / name)
        im.load()
        assert im.format == "PNG" and im.width >= 400 and (out / name).stat().st_size < 400_000, name
    assert Image.open(out / "01-library.png").size == (1600, 900)


# ---- the demo's stand-ins for the two Tools actions (no network, nothing written)

def test_the_simulated_install_reports_growing_progress_then_the_quiet_steps_and_writes_nothing(tmp_path):
    tool = demo.DemoYt(tmp_path / "bin", installed=False, delay=False)
    seen, steps = [], []
    result = tool.install(progress=lambda done, total: seen.append((done, total)), stage=steps.append)
    assert seen[0] == (0, demo.DEMO_YTDLP_BYTES) and seen[-1] == (demo.DEMO_YTDLP_BYTES, demo.DEMO_YTDLP_BYTES)
    assert [d for d, _ in seen] == sorted(d for d, _ in seen)
    assert steps == ["Connecting to GitHub…", "Checking the downloaded file…", "Testing that the new downloader starts…", "Installing…"]
    assert (result.version, result.previous_version, result.was_installed) == (demo.DEMO_YTDLP_VERSION, "", False)
    assert tool.available() and not (tmp_path / "bin").exists()
    again = tool.install()
    assert again.was_installed and again.previous_version == demo.DEMO_YTDLP_OLD and tool.update() == tool.path


def test_the_simulated_install_can_be_cancelled(tmp_path):
    from scrappy.providers.youtube import YouTubeCancelled
    tool = demo.DemoYt(tmp_path / "bin", installed=False, delay=False)
    with pytest.raises(YouTubeCancelled):
        tool.install(cancelled=lambda: True)
    assert not tool.available()


def test_the_demo_can_start_without_the_downloader_and_its_reload_answers_without_contacting_anything(tmp_path):
    env = build_demo(tmp_path / "demo", delay=False, ytdlp_installed=False)
    try:
        assert not env.controller.ytdlp.available()
        assert env.controller.ask_es_to_reload() is True
        assert env.controller.ytdlp.install().version == demo.DEMO_YTDLP_VERSION and env.controller.ytdlp.available()
    finally:
        env.cleanup()
    other = build_minimal_demo(tmp_path / "min", ytdlp_installed=False)
    try:
        assert not other.controller.ytdlp.available() and other.controller.ask_es_to_reload() is True
    finally:
        other.cleanup()
