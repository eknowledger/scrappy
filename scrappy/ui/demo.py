"""A made-up game library and made-up sources, to try the window (or test it) without the network or your real files.

``python -m scrappy.app --demo`` opens the real window on the showcase library (``build_demo``): 15 invented games on five retro
systems, with pixel-art pictures and short real trailers generated on the spot (see ``demo_data``, ``demo_art``,
``demo_video``). ``build_minimal_demo`` is the old three-game fixture (a Switch folder and one SNES game) that many tests of
the window rely on. Nothing is read from or written to ``/userdata``: the library lives in a temporary folder that is removed
when the program ends. No Qt imports, so tests can use it anywhere.
"""
from __future__ import annotations

import functools
import hashlib
import io
import os
import re
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np
from PIL import Image

from ..config import load_config
from ..controller import Controller
from ..media import media_rel_paths
from ..models import AssetCandidate, GameInfo, Match
from ..processing.images import make_boxart_png, make_image_jpg, make_text_logo
from ..processing.video import probe_duration
from ..providers.base import Provider, ProviderError
from ..providers.youtube import InstallResult, YouTubeCancelled
from ..titlematch import title_score
from . import demo_art, demo_video
from .demo_data import GAME_BY_SLUG, GAMES, SYSTEMS, DemoGame

SAMPLE_ROM = "Tidepool Tactics [0100AAAA00000000][v0].nsp"
ES_CFG = ('<?xml version="1.0"?><systemList>'
          "<system><name>switch</name><extension>.nsp .xci</extension></system>"
          "<system><name>snes</name><extension>.sfc .zip</extension></system></systemList>")
MP4_STUB = demo_video.MP4_STUB
HLS_URL = "https://demo.invalid/trailer/master.m3u8"
YT_URL = "https://www.youtube.com/watch?v=abcdefghijk"


def _png(im: Image.Image) -> bytes:
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def _gradient(width: int, height: int, top, bottom) -> Image.Image:
    t = np.linspace(0.0, 1.0, height)[:, None, None]
    rows = (np.array(top, float) * (1 - t) + np.array(bottom, float) * t)
    return Image.fromarray(np.broadcast_to(rows, (height, width, 3)).astype(np.uint8), "RGB")


def demo_picture(url: str) -> bytes:
    """A made-up picture for a made-up address (``demo://g/<game>/<which>`` for the showcase, a few fixed ones for the
    minimal demo)."""
    shown = re.fullmatch(r"demo://g/([a-z0-9-]+)/([a-z0-9-]+)", url)
    if shown and shown.group(1) in GAME_BY_SLUG:
        return _showcase_picture(shown.group(1), shown.group(2))
    if url == "demo://logo-transparent":
        return make_text_logo("Demo Game")
    if url == "demo://logo-solid":
        logo = Image.open(io.BytesIO(make_text_logo("Solid Logo"))).convert("RGBA")
        page = Image.new("RGBA", logo.size, (230, 30, 30, 255))
        page.alpha_composite(logo)
        return _png(page)
    if url == "demo://box":
        return _png(_gradient(600, 900, (20, 40, 120), (200, 60, 60)))
    if url == "demo://shot-1":
        return _png(_gradient(1920, 1080, (10, 80, 60), (240, 220, 120)))
    if url == "demo://shot-2":
        return _png(_gradient(1920, 1080, (90, 10, 90), (250, 140, 40)))
    if url == "demo://thumb":
        return _png(_gradient(320, 180, (0, 0, 0), (120, 120, 120)))
    raise ProviderError("The demo server has no such picture.")


class DemoProvider(Provider):
    name = "Demo"

    def search(self, title):
        time.sleep(0.2)
        return [Match(f"{title} (demo)", self.name, "1", score=0.97), Match("Something Else Entirely", self.name, "2", score=0.4)]

    def fetch(self, match):
        time.sleep(0.2)
        a = AssetCandidate
        return GameInfo(
            name=match.title, source=self.name, source_id=match.source_id, release_date="20260218T000000",
            developer="Demo Studio", publisher="Demo Publisher", genre="Horror, Adventure", players="1",
            description="A made-up description.\nIt only exists so you can try the window safely.",
            assets=[a(slot="logo", url="demo://logo-solid", label="Solid-colour logo", source=self.name, width=600, height=300),
                    a(slot="logo", url="demo://logo-transparent", label="Transparent logo", source=self.name, width=600, height=300,
                      transparent=True),
                    a(slot="boxart", url="demo://box", label="Box art", source=self.name, width=600, height=900),
                    a(slot="image", url="demo://shot-1", label="Screenshot 1", source=self.name, width=1920, height=1080),
                    a(slot="image", url="demo://missing", label="A picture that fails to load", source=self.name),
                    a(slot="image", url="demo://shot-2", label="Screenshot 2", source=self.name, width=1920, height=1080),
                    a(slot="video", url=HLS_URL, label="Demo trailer", source=self.name, kind="hls", thumb_url="demo://thumb")])


DEMO_YTDLP_VERSION = "2026.08.19"        # what the simulated install "installs" ...
DEMO_YTDLP_OLD = "2026.07.31"            # ... and what an "update" replaces
DEMO_YTDLP_BYTES = 3_150_000             # the size the simulated download announces (a real yt-dlp is about 3 MB)


def simulate_install(tool, delay: bool = True, progress=None, cancelled=None, stage=None, total=DEMO_YTDLP_BYTES) -> InstallResult:
    """The demo's stand-in for ``YtDlp.install``: a download that reports progress and can be cancelled, then the quiet steps.
    Nothing is fetched and nothing is written. `tool.installed` tells whether it counts as an update."""
    steps = 24 if delay else 2
    pause = 0.06 if delay else 0.0

    def check() -> None:
        if cancelled is not None and cancelled():
            raise YouTubeCancelled("Cancelled")

    def say(text: str) -> None:
        check()
        if stage is not None:
            stage(text)
        if delay:
            time.sleep(0.25)

    say("Connecting to GitHub…")
    for i in range(steps + 1):
        check()
        if progress is not None:
            progress(total * i // steps, total)
        if delay and i < steps:
            time.sleep(pause)
    say("Checking the downloaded file…")
    say("Testing that the new downloader starts…")
    say("Installing…")
    was = bool(tool.installed)
    tool.installed = True
    return InstallResult(tool.path, DEMO_YTDLP_VERSION, DEMO_YTDLP_OLD if was else "", was)


def demo_es_reload(delay: bool = True):
    """Stands in for the call to EmulationStation's reload address: answers 'reloaded' after a short pause (nothing is contacted)."""
    def reload() -> bool:
        if delay:
            time.sleep(0.8)
        return True
    return reload


class DemoYt:
    """Stands in for the yt-dlp wrapper. `installed=False` pretends the downloader is not there yet (Install then 'installs' it)."""

    def __init__(self, bin_dir, installed: bool = True, delay: bool = True):
        self.path = Path(bin_dir) / "yt-dlp"
        self.installed, self.delay = installed, delay

    def available(self):
        return self.installed

    def search(self, query, count=6, title="", cancelled=None):
        time.sleep(0.3)
        return [AssetCandidate(slot="video", url=YT_URL, kind="youtube", source="YouTube", label=f"{title or query} gameplay [1:30]",
                               thumb_url="demo://thumb", duration=90.0)]

    def download(self, url, dest, max_height=720, cancelled=None, timeout=None):
        p = Path(dest) / "demo.mp4"
        p.write_bytes(MP4_STUB)
        return p

    def install(self, opener=None, progress=None, cancelled=None, stage=None):
        return simulate_install(self, self.delay, progress, cancelled, stage)

    def update(self, opener=None, progress=None, cancelled=None, stage=None):
        return self.install(opener, progress, cancelled, stage).path


def demo_encode(src, dst, start=0.0, end=None, max_seconds=120, bitrate="1500k", cancelled=None, **_kw):
    """Pretends to encode for a second (so Cancel can be tried), then writes a tiny placeholder file."""
    for _ in range(20):
        if cancelled is not None and cancelled():
            raise InterruptedError("cancelled")
        time.sleep(0.05)
    Path(dst).write_bytes(MP4_STUB)


@dataclass
class DemoEnv:
    controller: Controller
    root: Path
    games: tuple = ()                # the DemoGame records of the showcase library (empty for the minimal demo)

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def build_minimal_demo(root=None, delay: bool = True, ytdlp_installed: bool = True) -> DemoEnv:
    """The old small fixture: three Switch games and one SNES game, stub video files, fixed made-up sources. Many tests of
    the window use it. (`delay` is accepted for symmetry with ``build_demo``; this fixture always pretends to be slow.)"""
    root = Path(root) if root is not None else Path(tempfile.mkdtemp(prefix="scrappy-demo-"))
    roms = root / "roms"
    sw = roms / "switch"
    (sw / "images").mkdir(parents=True)
    (sw / "videos").mkdir()
    (roms / "snes").mkdir()
    for name in (SAMPLE_ROM, "Cinder Citadel [0100BBBB00000000][v0].nsp", "Finished Game.nsp"):
        (sw / name).write_text("x")
    (roms / "snes" / "Super Demo World.sfc").write_text("x")
    # the games that already have art get real (made-up) pictures, so the window can show them as "Current"
    old_image = make_image_jpg(_gradient(640, 360, (60, 60, 60), (200, 160, 40)))
    old_box = make_boxart_png(_gradient(396, 640, (30, 90, 30), (10, 30, 10)))[0]
    (sw / "images" / "Finished Game-image.jpg").write_bytes(old_image)
    (sw / "images" / "Finished Game-thumb.png").write_bytes(old_box)
    (sw / "images" / "Finished Game-marquee.png").write_bytes(make_text_logo("Finished Game"))
    (sw / "videos" / "Finished Game-video.mp4").write_bytes(MP4_STUB)
    cinder = "Cinder Citadel [0100BBBB00000000][v0]"          # a partly finished game: text, image and box art, but no logo or video
    (sw / "images" / f"{cinder}-image.jpg").write_bytes(old_image)
    (sw / "images" / f"{cinder}-thumb.png").write_bytes(old_box)
    (sw / "gamelist.xml").write_text(
        '<?xml version="1.0"?><gameList>'
        "<game><path>./Finished Game.nsp</path><name>Finished Game</name><desc>Done.</desc>"
        "<image>./images/Finished Game-image.jpg</image><thumbnail>./images/Finished Game-thumb.png</thumbnail>"
        "<marquee>./images/Finished Game-marquee.png</marquee><video>./videos/Finished Game-video.mp4</video></game>"
        f"<game><path>./{cinder}.nsp</path><name>Cinder Citadel</name><desc>Old text.</desc><developer>Old Studio</developer>"
        f"<genre>Action</genre><image>./images/{cinder}-image.jpg</image><thumbnail>./images/{cinder}-thumb.png</thumbnail></game>"
        "</gameList>")
    es = root / "es_systems.cfg"
    es.write_text(ES_CFG)
    cfg = load_config(overrides={"roms_root": str(roms), "data_dir": str(root / "data")})
    ctl = Controller(cfg, providers=[DemoProvider()], ytdlp=DemoYt(root / "data" / "bin", ytdlp_installed, delay),
                     encode=demo_encode, fetch=demo_picture, es_configs=[str(es)], es_reload=demo_es_reload(delay), resolver=lambda host, port: ["93.184.216.34"],   # no real DNS
                     probe=lambda path: 45.0)                      # the placeholder videos are not real: say they last 45 s
    return DemoEnv(ctl, root)


# ====================================================================================================================
# The showcase library
# ====================================================================================================================
SHOWCASE_SOURCE = "Retro Archive"
CUT_SUFFIX = "~cut"
YT_SOURCE = "YouTube"
HLS_HOST = "https://demo.invalid/trailer/"
VIDEO_SECONDS = demo_video.SECONDS
FIXED_MTIME = 1_700_000_000                      # ROM files get fixed ages so the list order never changes


@functools.lru_cache(maxsize=128)
def _showcase_picture(slug: str, which: str) -> bytes:
    game = GAME_BY_SLUG[slug]
    if which == "logo-transparent":
        return demo_art.logo_png(game, 1)
    if which == "logo-solid":
        return demo_art.logo_png(game, 0, solid=True)
    if which == "box-a":
        return demo_art.box_png(game, 1)
    if which == "box-b":
        return demo_art.box_png(game, 2)
    if which in ("shot-1", "shot-2", "shot-3"):
        return demo_art.image_png(game, int(which[-1]))
    if which in ("thumb-yt", "thumb-hls"):
        return demo_art._png_bytes(demo_art.video_thumb(game, 0 if which == "thumb-yt" else 1))
    raise ProviderError("The demo server has no such picture.")


def find_game(text: str, threshold: float = 0.72, games=GAMES):
    """(DemoGame, score) of the showcase game that `text` names best, or (None, 0.0)."""
    best, score = None, 0.0
    for game in games:
        s = title_score(text, game.title)
        if s > score:
            best, score = game, s
    return (best, score) if best is not None and score >= threshold else (None, 0.0)


def _video_id(slug: str) -> str:
    return hashlib.sha1(slug.encode()).hexdigest()[:11]


class ShowcaseMedia:
    """Makes the trailers on first use and keeps them in the demo's temporary folder. `make(game, dest) -> Path` draws one
    (default: the invented showcase's animated scenes; tools/real_showcase.py passes its own)."""

    def __init__(self, folder, make=demo_video.make_video):
        self.folder = Path(folder)
        self.make = make
        self._lock = threading.Lock()

    def video(self, game: DemoGame) -> Path:
        with self._lock:
            return self.make(game, self.folder / f"{game.slug}.mp4")


class ShowcaseProvider(Provider):
    """The made-up game database: knows exactly the 15 showcase games and answers for each of them."""
    name = SHOWCASE_SOURCE

    def __init__(self, delay: bool = True):
        self.delay = delay

    def _wait(self):
        if self.delay:
            time.sleep(0.2)

    def search(self, title):
        self._wait()
        game, score = find_game(title)
        if game is None:
            return []
        year = (game.found.get("releasedate") or "")[:4]
        return [Match(game.title, self.name, game.slug, score=max(score, 0.9), year=year),
                Match(f"{game.title} (Collector's Cut)", self.name, game.slug + CUT_SUFFIX, score=0.58, year=year)]

    def fetch(self, match):
        self._wait()
        slug = match.source_id.removesuffix(CUT_SUFFIX)
        game = GAME_BY_SLUG.get(slug)
        if game is None:
            raise ProviderError("The demo archive has no such game.")
        f, a = game.found, AssetCandidate
        pic = lambda which: f"demo://g/{slug}/{which}"            # noqa: E731
        sizes = {which: _image_size(demo_picture(pic(which))) for which in ("logo-solid", "logo-transparent")}
        src = self.name
        assets = [a(slot="logo", url=pic("logo-solid"), label="Logo on a solid colour", source=src, **_wh(sizes["logo-solid"])),
                  a(slot="logo", url=pic("logo-transparent"), label="Transparent logo", source=src, transparent=True,
                    **_wh(sizes["logo-transparent"])),
                  a(slot="boxart", url=pic("box-a"), label="Front cover", source=src, width=396, height=640),
                  a(slot="boxart", url=pic("box-b"), label="Front cover, other colours", source=src, width=396, height=640),
                  a(slot="image", url=pic("shot-1"), label="Screenshot 1", source=src, width=1280, height=720),
                  a(slot="image", url=pic("missing"), label="A picture that fails to load", source=src),
                  a(slot="image", url=pic("shot-2"), label="Screenshot 2", source=src, width=1280, height=720),
                  a(slot="image", url=pic("shot-3"), label="Title screen (4:3, other style)", source=src, width=960, height=720),
                  a(slot="video", url=f"{HLS_HOST}{slug}/master.m3u8", label="Launch trailer (stream)", source=src, kind="hls",
                    thumb_url=pic("thumb-hls"), duration=62.0)]
        name = match.title
        return GameInfo(name=name, source=src, source_id=match.source_id, release_date=f.get("releasedate", ""),
                        developer=f.get("developer", ""), publisher=f.get("publisher", ""), genre=f.get("genre", ""),
                        players=f.get("players", ""), description=f.get("desc", ""), assets=assets)


def _image_size(data: bytes) -> tuple:
    return Image.open(io.BytesIO(data)).size


def _wh(size) -> dict:
    return {"width": size[0], "height": size[1]}


class ShowcaseYt:
    """Stands in for the yt-dlp wrapper: a video for every showcase game, downloaded from the demo's trailer cache.
    `games`, `thumb_url(game)` and `label(game)` let another showcase (tools/real_showcase.py) reuse it."""

    def __init__(self, bin_dir, media: ShowcaseMedia, delay: bool = True, games=GAMES, thumb_url=None, label=None,
                 installed: bool = True):
        self.path = Path(bin_dir) / "yt-dlp"
        self.installed = installed
        self.media, self.delay, self.games = media, delay, tuple(games)
        self.thumb_url = thumb_url or (lambda game: f"demo://g/{game.slug}/thumb-yt")
        self.label = label or (lambda game: f"{game.title} - Official Trailer [1:35]")
        self._by_slug = {g.slug: g for g in self.games}
        self._ids = {_video_id(g.slug): g.slug for g in self.games}

    def available(self):
        return self.installed

    def search(self, query, count=6, title="", cancelled=None):
        if self.delay:
            time.sleep(0.3)
        game, _ = find_game(title or query, games=self.games)
        if game is None:
            return []
        url = f"https://www.youtube.com/watch?v={_video_id(game.slug)}"
        return [AssetCandidate(slot="video", url=url, kind="youtube", source=YT_SOURCE, label=self.label(game),
                               thumb_url=self.thumb_url(game), duration=95.0)][:count]

    def download(self, url, dest, max_height=720, cancelled=None, timeout=None):
        m = re.search(r"v=([0-9a-f]{11})", url)
        game = self._by_slug.get(self._ids.get(m.group(1), "")) if m else None
        if game is None:
            raise ProviderError("The demo has no such video.")
        return self.media.video(game)

    def install(self, opener=None, progress=None, cancelled=None, stage=None):
        return simulate_install(self, self.delay, progress, cancelled, stage)

    def update(self, opener=None, progress=None, cancelled=None, stage=None):
        return self.install(opener, progress, cancelled, stage).path


def _make_encode(media: ShowcaseMedia, delay: bool, games=GAMES):
    by_slug = {g.slug: g for g in games}
    def encode(src, dst, start=0.0, end=None, max_seconds=120, bitrate="1500k", cancelled=None, **_kw):
        """Pretends to cut the clip (a short pause that Cancel can stop), then writes the game's trailer or the chosen file."""
        for _ in range(20 if delay else 1):
            if cancelled is not None and cancelled():
                raise InterruptedError("cancelled")
            if delay:
                time.sleep(0.05)
        text = str(src)
        found = re.fullmatch(re.escape(HLS_HOST) + r"([a-z0-9-]+)/master\.m3u8", text)
        if found and found.group(1) in by_slug:
            source = media.video(by_slug[found.group(1)])
        elif os.path.isfile(text):
            source = Path(text)
        else:
            raise ProviderError("The demo cannot cut that video.")
        tmp = Path(str(dst) + ".part")
        shutil.copyfile(source, tmp)
        os.replace(tmp, dst)
    return encode


def showcase_probe(path) -> float:
    """Length of a library video: ffprobe's answer for the real trailers, 45 s for the placeholder file."""
    try:
        seconds = probe_duration(path, timeout=5)
    except Exception:                                  # noqa: BLE001 - no ffprobe: the length is just a guess
        seconds = 0.0
    return seconds if seconds > 0 else 45.0


def _gamelist_entry(game: DemoGame) -> str:
    rel = media_rel_paths(game.rom)
    values = {"path": "./" + game.rom, "name": game.lib.get("name", game.title)}
    for tag in game.text_tags:
        if tag != "name" and game.lib.get(tag):
            values[tag] = game.lib[tag]
    for tag in game.media:
        values[tag] = rel[tag]
    order = ("path", "name", "desc", "image", "thumbnail", "marquee", "video", "releasedate", "developer", "publisher", "genre",
             "players")
    inner = "".join(f"<{tag}>{escape(values[tag])}</{tag}>" for tag in order if tag in values)
    return f"<game>{inner}</game>"


class InventedArt:
    """The pictures the library holds for the invented games (pixel art drawn by ``demo_art``). Another showcase passes an
    object with the same three methods, each ``(game) -> bytes``: ``image`` (JPG), ``thumbnail`` (PNG), ``marquee`` (PNG)."""

    @staticmethod
    def image(game: DemoGame) -> bytes:
        return make_image_jpg(demo_art.screenshot(game, 0, scale=2))

    @staticmethod
    def thumbnail(game: DemoGame) -> bytes:
        return make_boxart_png(demo_art.box_art(game, 0))[0]

    @staticmethod
    def marquee(game: DemoGame) -> bytes:
        return demo_art.library_logo(game)


def _write_library(roms: Path, media: ShowcaseMedia, games=GAMES, systems=SYSTEMS, art=InventedArt) -> None:
    for system in systems:
        folder = roms / system.folder
        (folder / "images").mkdir(parents=True)
        (folder / "videos").mkdir()
    with ThreadPoolExecutor(max_workers=3) as pool:                      # the trailers of the finished games, side by side
        videos = {g.slug: pool.submit(media.video, g) for g in games if "video" in g.media}
    for index, game in enumerate(games):
        folder = roms / game.system
        rom = folder / game.rom
        rom.write_bytes(b"\0")                                           # a 1-byte placeholder
        stamp = FIXED_MTIME - 60 * index
        os.utime(rom, (stamp, stamp))
        paths = {tag: folder / rel[2:] for tag, rel in media_rel_paths(game.rom).items()}
        if "image" in game.media:
            paths["image"].write_bytes(art.image(game))
        if "thumbnail" in game.media:
            paths["thumbnail"].write_bytes(art.thumbnail(game))
        if "marquee" in game.media:
            paths["marquee"].write_bytes(art.marquee(game))
        if "video" in game.media:
            shutil.copyfile(videos[game.slug].result(), paths["video"])
    for system in systems:
        entries = [_gamelist_entry(g) for g in games if g.system == system.folder and (g.media or g.text_tags)]
        if entries:
            (roms / system.folder / "gamelist.xml").write_text('<?xml version="1.0"?><gameList>' + "".join(entries) + "</gameList>",
                                                                encoding="utf-8")


def _es_config(roms: Path, systems=SYSTEMS) -> str:
    systems = "".join(f"<system><name>{s.folder}</name><fullname>{escape(s.fullname)}</fullname><path>{roms / s.folder}</path>"
                      f"<extension>{s.extensions}</extension></system>" for s in systems)
    return f'<?xml version="1.0"?><systemList>{systems}</systemList>'


def assemble_demo(root=None, delay: bool = True, *, games=GAMES, systems=SYSTEMS, providers=None, fetch=None, art=InventedArt,
                  make_video=demo_video.make_video, thumb_url=None, yt_label=None, ytdlp_installed: bool = True) -> DemoEnv:
    """Create a showcase library (`games` on `systems`) in a new temporary folder (or under `root`) and a Controller on it.
    ``build_demo`` is this with the invented games; tools/real_showcase.py calls it with real titles and its own sources.
    `providers`: game sources (default: the invented archive); `fetch(url) -> bytes`: the picture server; `art`: what the
    library's own pictures look like; `make_video(game, dest)`: draws a trailer. `delay=False` removes the artificial pauses.
    `ytdlp_installed=False` starts without the YouTube downloader (the Trailer panel then offers to install it)."""
    root = Path(root) if root is not None else Path(tempfile.mkdtemp(prefix="scrappy-demo-"))
    roms = root / "roms"
    media = ShowcaseMedia(root / "demo-media", make_video)
    _write_library(roms, media, games, systems, art)
    es = root / "es_systems.cfg"
    es.write_text(_es_config(roms, systems), encoding="utf-8")
    cfg = load_config(overrides={"roms_root": str(roms), "data_dir": str(root / "data")})
    ctl = Controller(cfg, providers=providers if providers is not None else [ShowcaseProvider(delay)],
                     ytdlp=ShowcaseYt(root / "data" / "bin", media, delay, games, thumb_url, yt_label, ytdlp_installed),
                     encode=_make_encode(media, delay, games), fetch=fetch or demo_picture, es_configs=[str(es)],
                     es_reload=demo_es_reload(delay),
                     resolver=lambda host, port: ["93.184.216.34"],                    # no real DNS
                     probe=showcase_probe)
    return DemoEnv(ctl, root, tuple(games))


def build_demo(root=None, delay: bool = True, ytdlp_installed: bool = True) -> DemoEnv:
    """Create the showcase library (15 invented games on five retro systems) in a new temporary folder (or under `root`)
    and a Controller on it, with made-up sources that know every game. `delay=False` removes the artificial pauses."""
    return assemble_demo(root, delay, ytdlp_installed=ytdlp_installed)
