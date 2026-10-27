"""A developer-only showcase library of REAL, famous games with their real artwork, for the README screenshots.

    python tools/fetch_demo_art.py          # once: downloads the artwork into ~/.cache/scrappy-demo-art (outside the repository)
    python tools/make_screenshots.py --real # uses it

Rules (see docs/screenshots/README.md):
* No third-party artwork or trailer is ever stored in this repository. The pictures live in the cache folder
  ``~/.cache/scrappy-demo-art/`` (override with the environment variable ``SCRAPPY_DEMO_ART``). This file holds only code and the
  list of game TITLES (names are facts), plus short texts written for this project.
* Nothing here is reachable from ``python -m scrappy.app --demo`` (that stays the invented, offline showcase).
* Once the cache is filled, everything works offline. The "trailers" are short slideshows that ffmpeg makes from the cached
  stills; no real trailer is ever downloaded.

``build_real_demo`` returns the same ``DemoEnv`` as ``scrappy.ui.demo.build_demo`` and reuses its machinery (temporary library,
stand-ins for yt-dlp / the encoder / ffprobe, the ``delay`` flag); only the games, the pictures and the provider differ.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from PIL import Image, ImageFilter  # noqa: E402

from scrappy.models import AssetCandidate, GameInfo, Match  # noqa: E402
from scrappy.processing.images import make_boxart_png, make_image_jpg, make_logo_png, make_text_logo, open_image  # noqa: E402
from scrappy.providers.base import Provider, ProviderError  # noqa: E402
from scrappy.providers.steam import DETAILS, SEARCH, SteamProvider  # noqa: E402
from scrappy.titlematch import title_score  # noqa: E402
from scrappy.ui import demo, demo_video  # noqa: E402
from scrappy.ui.demo_data import COMPLETE, DESC_ONLY, PARTIAL, ROM_ONLY, SOME_TEXT, DemoGame, DemoSystem  # noqa: E402

CACHE_ENV = "SCRAPPY_DEMO_ART"
STEAM, LIBRETRO = "Steam", "libretro thumbnails"
LIBRETRO_RAW = "https://raw.githubusercontent.com/libretro-thumbnails/"
LIBRETRO_FOLDERS = {"boxart": "Named_Boxarts", "snap": "Named_Snaps", "title": "Named_Titles"}
STEAM_ASSETS = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/"
MIN_SEARCH_SCORE = 0.5                           # Steam search hits below this score are left out of the demo
MIN_SLOTS = ("boxart", "image")                  # a game needs at least a cover and one screenshot to be shown


def cache_dir() -> Path:
    return Path(os.environ.get(CACHE_ENV) or Path.home() / ".cache" / "scrappy-demo-art")


# --------------------------------------------------------------------------------------------------------- the systems
def _sys(folder, fullname, extensions):
    return DemoSystem(folder, fullname, extensions, folder.upper(), (60, 60, 60))


REAL_SYSTEMS = (
    _sys("nes", "Nintendo Entertainment System", ".nes .NES .zip .ZIP .7z .7Z"),
    _sys("snes", "Super Nintendo Entertainment System", ".sfc .SFC .smc .SMC .zip .ZIP .7z .7Z"),
    _sys("megadrive", "Sega Mega Drive / Genesis", ".md .MD .gen .GEN .bin .BIN .zip .ZIP .7z .7Z"),
    _sys("gb", "Nintendo Game Boy", ".gb .GB .zip .ZIP .7z .7Z"),
    _sys("psx", "Sony PlayStation", ".cue .CUE .chd .CHD .m3u .M3U .pbp .PBP .iso .ISO"),
    _sys("switch", "Nintendo Switch", ".nsp .NSP .xci .XCI"),
    _sys("windows", "Windows", ".pc .PC .wine .WINE .wsquashfs .squashfs .exe"),
    _sys("ps3", "Sony PlayStation 3", ".ps3 .PS3 .psn .PSN .squashfs .iso .ISO"),
)
SYSTEM_ORDER = tuple(s.folder for s in REAL_SYSTEMS)
LIBRETRO_REPOS = {
    "nes": "Nintendo_-_Nintendo_Entertainment_System",
    "snes": "Nintendo_-_Super_Nintendo_Entertainment_System",
    "megadrive": "Sega_-_Mega_Drive_-_Genesis",
    "gb": "Nintendo_-_Game_Boy",
    "psx": "Sony_-_PlayStation",
    "ps3": "Sony_-_PlayStation_3",
}


# --------------------------------------------------------------------------------------------------- the 16 games
@dataclass(frozen=True)
class RealGame(DemoGame):
    source: str = LIBRETRO                       # STEAM or LIBRETRO: where the pictures come from
    steam_id: int = 0                            # Steam app id (STEAM games)
    thumb_names: tuple = ()                      # (("boxart", "name"), ...): libretro picture names that differ from the ROM name

    def libretro_name(self, kind: str) -> str:
        """The libretro-thumbnails name (without extension) of the ``boxart``, ``snap`` or ``title`` picture."""
        return dict(self.thumb_names).get(kind, self.stem)


def _g(title, system, rom, state, lib, found, source=LIBRETRO, steam_id=0, have=(), text=(), thumb_names=()):
    return RealGame(title, system, rom, state, "sky", "blob", (60, 60, 60), lib, found, have=have, text=text, source=source,
                    steam_id=steam_id, thumb_names=thumb_names)


GAMES = (
    # ---- nes
    _g("The Legend of Zelda", "nes", "Legend of Zelda, The (USA).nes", PARTIAL,
       lib=dict(name="The Legend of Zelda", releasedate="19870822T000000", developer="Nintendo", genre="Action-adventure",
                desc="A hero explores an overworld and nine dungeons."),
       found=dict(releasedate="19870822T000000", developer="Nintendo R&D4", publisher="Nintendo", genre="Action-adventure",
                  players="1",
                  desc="A young hero crosses an overworld and a string of dungeons to gather the pieces of a broken Triforce and "
                       "free a princess from the villain Ganon. Items found along the way open new routes, and a battery in "
                       "the cartridge keeps the progress."),
       have=("image", "thumbnail"), text=SOME_TEXT),
    _g("Mega Man 2", "nes", "Mega Man 2 (USA).nes", ROM_ONLY, lib={},
       found=dict(releasedate="19890601T000000", developer="Capcom", publisher="Capcom", genre="Platformer, Action", players="1",
                  desc="The blue robot hero can tackle eight robot masters in any order and borrows the weapon of each one he "
                       "beats, before storming the fortress of Dr. Wily. Energy tanks and a password system make the run "
                       "kinder.")),
    # ---- snes
    _g("Super Metroid", "snes", "Super Metroid (Japan, USA) (En,Ja).sfc", COMPLETE,
       lib=dict(releasedate="19940319T000000", developer="Nintendo R&D1, Intelligent Systems", publisher="Nintendo",
                genre="Action-adventure", players="1",
                desc="Bounty hunter Samus Aran returns to planet Zebes to recover a stolen creature. The big connected map "
                     "opens up piece by piece as she finds new suits and weapons."),
       found=dict(releasedate="19940319T000000", developer="Nintendo R&D1, Intelligent Systems", publisher="Nintendo",
                  genre="Action-adventure", players="1",
                  desc="Samus Aran flies back to the planet Zebes to get a stolen baby creature back from space pirates. A "
                       "single big map is unlocked piece by piece with every suit upgrade and weapon she finds.")),
    _g("Chrono Trigger", "snes", "Chrono Trigger (USA).sfc", DESC_ONLY,
       lib=dict(name="Chrono Trigger", desc="Time travellers hop between eras to stop a disaster."),
       found=dict(releasedate="19950822T000000", developer="Square", publisher="Square", genre="Role-playing", players="1",
                  desc="A band of time travellers hops between the distant past and a ruined future to stop a catastrophe. "
                       "Battles take place on the map with no random encounters, and different choices lead to different "
                       "endings.")),
    # ---- megadrive
    _g("Sonic the Hedgehog 2", "megadrive", "Sonic The Hedgehog 2 (World).md", COMPLETE,
       lib=dict(releasedate="19921124T000000", developer="Sega Technical Institute, Sonic Team", publisher="Sega",
                genre="Platformer", players="1-2",
                desc="Sonic and his friend Tails race through loops and slopes to stop Dr. Robotnik's plan for a giant space "
                     "station. A second player can take over Tails."),
       found=dict(releasedate="19921124T000000", developer="Sega Technical Institute, Sonic Team", publisher="Sega",
                  genre="Platformer", players="1-2",
                  desc="Sonic and sidekick Tails speed through loops and slopes to stop Dr. Robotnik's plan for a giant space "
                       "station. A second player can take control of Tails, and a split-screen mode allows races.")),
    _g("Streets of Rage 2", "megadrive", "Streets of Rage 2 (USA).md", ROM_ONLY, lib={},
       found=dict(releasedate="19921220T000000", developer="Sega", publisher="Sega", genre="Beat 'em up", players="1-2",
                  desc="Four fighters battle through eight stages of a side-scrolling brawler to take back their city from a "
                       "crime boss, and a second player can join at any time. Its electronic soundtrack is as famous as the "
                       "game itself.")),
    # ---- gb
    _g("Tetris", "gb", "Tetris (World) (Rev 1).gb", COMPLETE,
       lib=dict(releasedate="19890731T000000", developer="Nintendo R&D1", publisher="Nintendo", genre="Puzzle", players="1-2",
                desc="Falling blocks must be arranged into complete rows before the pile reaches the top. Two players can "
                     "compete with a link cable."),
       found=dict(releasedate="19890731T000000", developer="Nintendo R&D1", publisher="Nintendo", genre="Puzzle", players="1-2",
                  desc="Falling blocks must be fitted into complete rows before the pile reaches the top. It came bundled with "
                       "the Game Boy, and a link cable lets two players compete.")),
    _g("The Legend of Zelda: Link's Awakening", "gb", "Legend of Zelda, The - Link's Awakening (USA, Europe).gb", ROM_ONLY,
       lib={},
       found=dict(releasedate="19930806T000000", developer="Nintendo EAD", publisher="Nintendo", genre="Action-adventure",
                  players="1",
                  desc="Link is shipwrecked on Koholint Island and must wake the Wind Fish by collecting eight instruments "
                       "hidden in dungeons. It was the first Zelda adventure made for a handheld console.")),
    # ---- psx
    _g("Final Fantasy VII", "psx", "Final Fantasy VII (USA) (Disc 1).cue", COMPLETE,
       lib=dict(releasedate="19970907T000000", developer="Square", publisher="Sony Computer Entertainment",
                genre="Role-playing", players="1",
                desc="Former soldier Cloud joins a group of rebels fighting a powerful energy company, and a personal quest "
                     "grows into a struggle to save the planet. The story spans three discs."),
       found=dict(releasedate="19970907T000000", developer="Square", publisher="Sony Computer Entertainment",
                  genre="Role-playing", players="1",
                  desc="Cloud, a former soldier, takes a job with a group of rebels fighting a giant energy company, and the "
                       "job turns into a fight for the fate of the planet. The story is told over three discs.")),
    _g("Castlevania: Symphony of the Night", "psx", "Castlevania - Symphony of the Night (USA).cue", PARTIAL,
       lib=dict(name="Castlevania: Symphony of the Night", releasedate="19971002T000000", developer="Konami", genre="Action",
                desc="Alucard explores a huge castle."),
       found=dict(releasedate="19971002T000000", developer="Konami Computer Entertainment Tokyo", publisher="Konami",
                  genre="Action-adventure", players="1",
                  desc="Alucard, the son of Dracula, explores a vast castle that keeps growing as he learns new abilities. "
                       "Levels, equipment and spells brought role-playing ideas to the series' action."),
       have=("image", "marquee"), text=SOME_TEXT),
    # ---- switch (Steam art)
    _g("Hades", "switch", "Hades [v0].nsp", PARTIAL,
       lib=dict(name="Hades", releasedate="20200917T000000", developer="Supergiant Games", genre="Action",
                desc="Fight out of the Underworld."),
       found=dict(releasedate="20200917T000000", developer="Supergiant Games", publisher="Supergiant Games",
                  genre="Action / RPG", players="1",
                  desc="Zagreus, son of the god of the dead, fights his way out of the Underworld through rooms that change "
                       "on every run. Each failed escape sends him home, where he grows stronger and the story moves on."),
       source=STEAM, steam_id=1145360, have=("image", "thumbnail", "marquee"), text=SOME_TEXT),
    _g("Celeste", "switch", "Celeste [v0].nsp", ROM_ONLY, lib={},
       found=dict(releasedate="20180125T000000", developer="Maddy Makes Games", publisher="Maddy Makes Games",
                  genre="Platformer", players="1",
                  desc="Madeline climbs a mountain by running, jumping and dashing through screens that call for precise "
                       "timing. Optional assist settings let players tune the difficulty."),
       source=STEAM, steam_id=504230),
    # ---- windows (Steam art)
    _g("Stardew Valley", "windows", "Stardew Valley.pc", DESC_ONLY,
       lib=dict(name="Stardew Valley", desc="Inherit a farm and build a life in the valley."),
       found=dict(releasedate="20160226T000000", developer="ConcernedApe", publisher="ConcernedApe", genre="Simulation / RPG",
                  players="1-4",
                  desc="An inherited farm slowly becomes a home as the player plants crops, tends animals, mines caves and "
                       "makes friends in the nearby village. Seasons and festivals keep the years moving."),
       source=STEAM, steam_id=413150),
    _g("Cuphead", "windows", "Cuphead.wine", COMPLETE,
       lib=dict(releasedate="20170929T000000", developer="Studio MDHR", publisher="Studio MDHR", genre="Run and gun",
                players="1-2",
                desc="Two cup-headed brothers fight boss after boss in levels drawn like 1930s cartoons. It can be played "
                     "alone or in co-op."),
       found=dict(releasedate="20170929T000000", developer="Studio MDHR", publisher="Studio MDHR",
                  genre="Action / Platformer", players="1-2",
                  desc="Two cup-headed brothers fight their way through boss battles and run-and-gun levels drawn in the style "
                       "of 1930s cartoons. It can be played alone or in co-op, and is known for its difficulty."),
       source=STEAM, steam_id=268910),
    # ---- ps3 (libretro has few PS3 pictures: the first choices, The Last of Us and Journey, have no box art there)
    _g("Demon's Souls", "ps3", "Demon's Souls (USA).iso", PARTIAL,
       lib=dict(name="Demon's Souls", releasedate="20091006T000000", developer="FromSoftware", genre="Action role-playing",
                desc="A knight hunts demons in a fog-bound kingdom."),
       found=dict(releasedate="20091006T000000", developer="FromSoftware, SCE Japan Studio", publisher="Atlus USA",
                  genre="Action role-playing", players="1",
                  desc="A lone knight enters the fog-bound kingdom of Boletaria to hunt the demons that have taken over the "
                       "land. Combat is slow and unforgiving, and messages and ghosts of other players can warn or help you."),
       have=("image", "thumbnail", "video"), text=SOME_TEXT),
    _g("Heavy Rain", "ps3", "Heavy Rain (USA) (En,Fr,Es,Pt).iso", ROM_ONLY, lib={},
       found=dict(releasedate="20100223T000000", developer="Quantic Dream", publisher="Sony Computer Entertainment",
                  genre="Interactive drama", players="1",
                  desc="An interactive thriller in which four characters search for a serial killer, and every choice, success "
                       "or mistake changes how the story continues. Action scenes are played with timed button prompts."),
       thumb_names=(("boxart", "Heavy Rain (USA)"),)),
)
GAME_BY_SLUG = {g.slug: g for g in GAMES}
EXTRA_STATE = ROM_ONLY                           # the sixteenth game's state beyond the 5 / 4 / 4 / 2 mix of the invented library


# ------------------------------------------------------------------------------------------ libretro file-name rules
_LIBRETRO_BAD = re.compile(r'[&*/:`<>?\\|]')


def libretro_file_name(name: str, ext: str = ".png") -> str:
    """libretro-thumbnails file name of a No-Intro / Redump name: ``& * / : ` < > ? \\ |`` become ``_``."""
    return _LIBRETRO_BAD.sub("_", name) + ext


def libretro_url(repo: str, kind: str, name: str) -> str:
    """Address of one picture in a libretro-thumbnails system repository. kind: ``boxart``, ``snap`` or ``title``."""
    if kind not in LIBRETRO_FOLDERS:
        raise ValueError(f"unknown libretro picture kind {kind!r}")
    return f"{LIBRETRO_RAW}{repo}/master/{LIBRETRO_FOLDERS[kind]}/{quote(libretro_file_name(name), safe='')}"


# -------------------------------------------------------------------------------------------------------- the cache
class ArtCache:
    """Reads the art cache folder written by ``tools/fetch_demo_art.py`` (``index.json`` and the files it lists)."""

    def __init__(self, folder=None):
        self.folder = Path(folder) if folder is not None else cache_dir()
        self.index = self._load()

    def _load(self) -> dict:
        try:
            data = json.loads((self.folder / "index.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        games = data.get("games") if isinstance(data, dict) else None
        if not isinstance(games, list):
            return {}
        return {g["slug"]: g for g in games if isinstance(g, dict) and isinstance(g.get("slug"), str)}

    def entries(self, slug: str, slot: str, role: str = "") -> list:
        """The entries ``{"file", "url", "role", "name"}`` of one slot whose file exists and is not empty, in index order."""
        slots = (self.index.get(slug) or {}).get("slots")
        out = []
        for entry in (slots.get(slot) if isinstance(slots, dict) else None) or []:
            try:
                if (not role or entry.get("role") == role) and (self.folder / entry["file"]).stat().st_size > 0:
                    out.append(entry)
            except (OSError, KeyError, TypeError, AttributeError):
                continue
        return out

    def path(self, entry) -> Path:
        return self.folder / entry["file"]

    def by_url(self) -> dict:
        """{source address: cached file} for every cached picture of every game."""
        out = {}
        for slug in self.index:
            for slot in ("logo", "boxart", "image", "video"):
                for entry in self.entries(slug, slot):
                    if isinstance(entry.get("url"), str):
                        out[entry["url"]] = self.path(entry)
        return out

    def json_file(self, rel: str):
        try:
            return json.loads((self.folder / rel).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def missing(self, games=GAMES) -> list:
        """[(game, [what is missing])] for the games that lack the cover or a screenshot."""
        out = []
        for game in games:
            lacks = [s for s in MIN_SLOTS if not self.entries(game.slug, s)]
            if lacks:
                out.append((game, lacks))
        return out

    def stills(self, game: RealGame) -> list:
        """Files to build the slideshow from: screenshots, then title screens, then the cover."""
        rank = {"snap": 0, "screenshot": 0, "title": 1, "header": 2, "hero": 3}
        images = sorted(self.entries(game.slug, "image"), key=lambda e: rank.get(e.get("role"), 9))
        return [self.path(e) for e in images + self.entries(game.slug, "boxart")]


# ----------------------------------------------------------------------------------------------------- pictures
def text_logo_for(game: RealGame) -> bytes:
    """The project's own generated text logo (what "Make text logo" makes) for a game."""
    return make_text_logo(game.lib.get("name", game.title))


def library_logo(game: RealGame, cache: ArtCache) -> bytes:
    """The logo file in the library: the Steam logo for Steam games, the project's own text logo otherwise."""
    logos = cache.entries(game.slug, "logo") if game.source == STEAM else []
    if logos:
        try:
            return make_logo_png(open_image(cache.path(logos[0]).read_bytes()))
        except (OSError, ValueError):
            pass
    return text_logo_for(game)


class RealArt:
    """What the library holds for the finished and partly finished games (``scrappy.ui.demo`` calls these)."""

    def __init__(self, cache: ArtCache):
        self.cache = cache

    def _first(self, game: RealGame, slot: str, roles=()) -> bytes:
        found = [e for e in self.cache.entries(game.slug, slot) if not roles or e.get("role") in roles]
        if not found:
            raise ProviderError(f"No cached {slot} picture for {game.title}. Run tools/fetch_demo_art.py.")
        return self.cache.path(found[0]).read_bytes()

    def image(self, game: RealGame) -> bytes:
        # the library's own picture is one the source also offers, but a later one (so a "new" picture differs from it)
        pics = [e for e in self.cache.entries(game.slug, "image") if e.get("role") in ("snap", "screenshot", "title")]
        if not pics:
            raise ProviderError(f"No cached screenshot for {game.title}. Run tools/fetch_demo_art.py.")
        pick = pics[-1] if game.source == STEAM or game.state == PARTIAL else pics[0]
        return make_image_jpg(open_image(self.cache.path(pick).read_bytes()))

    def thumbnail(self, game: RealGame) -> bytes:
        return make_boxart_png(open_image(self._first(game, "boxart")))[0]

    def marquee(self, game: RealGame) -> bytes:
        if game.state == PARTIAL:             # an earlier run of "Make text logo": the source may offer a better one
            return text_logo_for(game)
        return library_logo(game, self.cache)


# ---- the slideshow "trailer" (made by ffmpeg from the cached stills; no real trailer is ever downloaded)
ZOOM = 1.25                                  # the stills are prepared this much larger than the frame, to zoom and pan over


def _backdrop(still: Image.Image, size) -> Image.Image:
    """The picture fitted into a canvas of `size` on top of a blurred, darkened copy of itself."""
    w, h = size
    picture = still.convert("RGB")
    back = picture.resize((w, h), Image.BILINEAR).filter(ImageFilter.GaussianBlur(max(6, w // 40)))
    back = back.point(lambda v: int(v * 0.45))
    ratio = min(w / picture.width, h / picture.height)
    fit = picture.resize((max(1, round(picture.width * ratio)), max(1, round(picture.height * ratio))),
                         Image.LANCZOS if ratio < 1 else Image.BICUBIC)
    back.paste(fit, ((w - fit.width) // 2, (h - fit.height) // 2))
    return back


def slideshow_frames(files, seconds: int = demo_video.SECONDS, fps: int = demo_video.FPS, size=demo_video.SIZE):
    """Raw RGB frames: a slow push-in and pan over up to three stills, one after the other with short cross-fades."""
    import numpy as np
    canvas = (round(size[0] * ZOOM), round(size[1] * ZOOM))
    stills = []
    for path in list(files)[:3]:
        try:
            stills.append(_backdrop(open_image(Path(path).read_bytes()), canvas))
        except (OSError, ValueError):
            continue
    if not stills:
        raise ProviderError("There is no picture to make the clip from.")
    total = seconds * fps
    per, fade = total / len(stills), max(1, int(0.4 * fps))

    def view(index: int, progress: float) -> Image.Image:
        zoom = 1.0 + 0.2 * progress                                            # slow push-in
        vw, vh = canvas[0] / zoom, canvas[1] / zoom
        drift = (progress - 0.5) * 0.05 * canvas[0] * (1 if index % 2 == 0 else -1)
        x0 = min(max((canvas[0] - vw) / 2 + drift, 0), canvas[0] - vw)
        y0 = (canvas[1] - vh) / 2
        return stills[index].resize(size, Image.BILINEAR, box=(x0, y0, x0 + vw, y0 + vh))

    for n in range(total):
        index = min(len(stills) - 1, int(n // per))
        local = n - index * per
        frame = view(index, min(1.0, local / per))
        if index > 0 and local < fade:                                         # cross-fade from the end of the last still
            frame = Image.blend(view(index - 1, 1.0), frame, (local + 1) / (fade + 1))
        yield np.asarray(frame, dtype=np.uint8).tobytes()


def make_slideshow(game: RealGame, cache: ArtCache, dest) -> Path:
    """The clip at `dest` (made now if it does not exist yet); the tiny placeholder file when ffmpeg is not usable."""
    dest = Path(dest)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        made = demo_video.encode_frames(slideshow_frames(cache.stills(game)), dest)
    except ProviderError:
        made = False
    if not made:
        dest.write_bytes(demo_video.MP4_STUB)
    return dest


# ------------------------------------------------------------------------------------------------ the picture server
TEXT_LOGO_URL = "demo://text-logo/"


class RealPictures:
    """``fetch(url) -> bytes`` for the demo controller. Candidates carry the pictures' real source addresses (the ones the
    index lists); this reads the cached file instead of the network. ``demo://text-logo/<game>`` is the generated logo."""

    def __init__(self, cache: ArtCache):
        self.files = cache.by_url()

    def __call__(self, url: str) -> bytes:
        if url.startswith(TEXT_LOGO_URL) and url[len(TEXT_LOGO_URL):] in GAME_BY_SLUG:
            return text_logo_for(GAME_BY_SLUG[url[len(TEXT_LOGO_URL):]])
        path = self.files.get(url)
        if path is None:
            raise ProviderError("The demo server has no such picture.")
        return path.read_bytes()


# ------------------------------------------------------------------------------------------------------- providers
class _Offline:
    """A requests-style session that refuses to touch the network (the Steam provider is only used for its logic)."""
    headers = {}

    def __init__(self):
        self.headers = {}

    def get(self, *args, **kwargs):
        raise ProviderError("The real-titles demo never uses the network.")

    head = get


class CachedSteam(SteamProvider):
    """The real Steam provider (``scrappy.providers.steam``: search scoring, appdetails parsing, candidate order and labels), fed
    from the art cache instead of the network. Candidates keep their real Steam addresses; only pictures that are in the cache
    are offered, and the trailer is the generated slideshow."""

    def __init__(self, cache: ArtCache, delay: bool = True):
        super().__init__(session=_Offline())
        self.cache, self.delay = cache, delay
        self.games = tuple(g for g in GAMES if g.source == STEAM)
        self.files = cache.by_url()

    def _wait(self):
        if self.delay:
            time.sleep(0.2)

    def _game(self, appid) -> "RealGame | None":
        return next((g for g in self.games if str(g.steam_id) == str(appid)), None)

    def _get_json(self, url, params=None) -> dict:
        params = params or {}
        if url == SEARCH:                                   # Steam's own answer, saved by fetch_demo_art.py
            game, _ = find_game(str(params.get("term", "")), self.games)
            data = self.cache.json_file(f"steam/{game.steam_id}-search.json") if game else None
            if not isinstance(data, dict):
                return {"items": []}
            # Steam's list for a short title also holds loosely related entries (soundtracks, fan games, adult titles): the
            # showcase keeps only the ones whose name is close to what was searched for
            items = [i for i in data.get("items", []) if isinstance(i, dict)
                     and title_score(str(params.get("term", "")), str(i.get("name", ""))) >= MIN_SEARCH_SCORE]
            return {**data, "items": items}
        if url == DETAILS:
            game = self._game(params.get("appids"))
            data = self.cache.json_file(f"steam/{game.steam_id}-details.json") if game else None
            if isinstance(data, dict):
                return data
        raise ProviderError("Steam has no data for that in the demo cache")

    def _cached(self, game, slot: str, role: str, make):
        found = self.cache.entries(game.slug, slot, role)
        return [make(found[0]["url"])] if found else []

    def _browse_assets(self, appid: str) -> list:
        game = self._game(appid)
        if game is None:
            return []
        a = AssetCandidate
        return (self._cached(game, "boxart", "library", lambda u: a(
                    slot="boxart", url=u, label="Steam library box art", source="Steam", width=600, height=900))
                + self._cached(game, "image", "hero", lambda u: a(
                    slot="image", url=u, label="Steam library hero", source="Steam", width=1920, height=620)))

    def _logo_png(self, appid: str, label: str) -> list:
        game = self._game(appid)
        return self._cached(game, "logo", "logo", lambda u: AssetCandidate(
            slot="logo", url=u, label=label, source="Steam", transparent=True)) if game else []

    def search(self, title):
        self._wait()
        return super().search(title)

    def fetch(self, match):
        self._wait()
        game = self._game(match.source_id)
        if game is None:
            raise ProviderError("That Steam entry is not in the demo cache.")
        info = super().fetch(match)
        kept = []
        for asset in info.assets:
            if asset.slot == "video":
                if asset.thumb_url in self.files and not any(k.slot == "video" for k in kept):
                    kept.append(AssetCandidate(slot="video", url=clip_url(game), label="Trailer", source="Steam", kind="hls",
                                               thumb_url=asset.thumb_url, duration=62.0))
            elif asset.url in self.files:
                kept.append(asset)
        info.assets = kept
        info.description = game.found.get("desc", "")                  # always the text written for this project
        info.players = info.players or game.found.get("players", "")
        return info


class LibretroProvider(Provider):
    """A stand-in source for the retro and PS3 titles, named after where their pictures come from (the program itself has no
    such source). Serves the cached libretro-thumbnails pictures under their real addresses."""
    name = LIBRETRO
    labels = (("boxart", "boxart", "Box art"), ("image", "snap", "In-game screenshot"), ("image", "title", "Title screen"))

    def __init__(self, cache: ArtCache, delay: bool = True):
        self.cache, self.delay = cache, delay
        self.games = tuple(g for g in GAMES if g.source == LIBRETRO)

    def _wait(self):
        if self.delay:
            time.sleep(0.2)

    def search(self, title):
        self._wait()
        game, score = find_game(title, self.games)
        if game is None:
            return []
        return [Match(game.title, self.name, game.slug, score=max(score, 0.9), year=(game.found.get("releasedate") or "")[:4])]

    def fetch(self, match):
        self._wait()
        game = next((g for g in self.games if g.slug == match.source_id), None)
        if game is None:
            raise ProviderError("The demo archive has no such game.")
        f, src, a = game.found, self.name, AssetCandidate
        assets = [a(slot="logo", url=TEXT_LOGO_URL + game.slug, label="Text logo (generated by this program)", source=src,
                    transparent=True, width=600, height=300)]
        for slot, role, label in self.labels:
            for entry in self.cache.entries(game.slug, slot, role):
                assets.append(a(slot=slot, url=entry["url"], label=label, source=src))
        stills = self.cache.entries(game.slug, "image")
        if stills:
            assets.append(a(slot="video", url=clip_url(game), label="Trailer", source=src, kind="hls", thumb_url=stills[0]["url"],
                            duration=62.0))
        return GameInfo(name=game.title, source=src, source_id=game.slug, release_date=f.get("releasedate", ""),
                        developer=f.get("developer", ""), publisher=f.get("publisher", ""), genre=f.get("genre", ""),
                        players=f.get("players", ""), description=f.get("desc", ""), assets=assets)


def clip_url(game: RealGame) -> str:
    return f"{demo.HLS_HOST}{game.slug}/master.m3u8"


def find_game(text: str, games=GAMES, threshold: float = 0.72):
    return demo.find_game(text, threshold, games)


def yt_label(game: RealGame) -> str:
    return f"{game.title} gameplay [1:30]"


# ------------------------------------------------------------------------------------------------------ the builder
def build_real_demo(root=None, delay: bool = True, cache=None) -> demo.DemoEnv:
    """The real-titles showcase library (16 games on 8 systems) in a new temporary folder, with a Controller whose sources
    answer from the art cache. Raises ``ProviderError`` when the cache lacks the cover or a screenshot of any game."""
    cache = cache if isinstance(cache, ArtCache) else ArtCache(cache)
    lacking = cache.missing()
    if lacking:
        names = ", ".join(f"{g.title} ({'/'.join(w)})" for g, w in lacking[:4]) + (" ..." if len(lacking) > 4 else "")
        raise ProviderError(f"The art cache {cache.folder} is incomplete: {names}. Run tools/fetch_demo_art.py first.")
    return demo.assemble_demo(
        root, delay, games=GAMES, systems=REAL_SYSTEMS, providers=[CachedSteam(cache, delay), LibretroProvider(cache, delay)],
        fetch=RealPictures(cache), art=RealArt(cache), make_video=lambda game, dest: make_slideshow(game, cache, dest),
        thumb_url=lambda game: (cache.entries(game.slug, "image") or [{"url": ""}])[0]["url"], yt_label=yt_label)
