"""The made-up games of the showcase library (``python -m scrappy.app --demo``) and the five retro systems they live on.

Everything here is invented: titles, studios, publishers, descriptions. Nothing is taken from a real game, a real company
or a real console brand (the system folders use the standard EmulationStation folder names, the full names are generic).
Plain data only: no Pillow, no Qt, so tests and tools can import it anywhere.

States of a game (what the library holds):
* ``complete``   all text, image, box art, logo and video
* ``partial``    some of it (``have`` lists the media files it has; ``text`` says how much text)
* ``rom-only``   only the ROM file; it is not even in gamelist.xml
* ``desc-only``  a gamelist entry with just the name and the description
"""
from __future__ import annotations

import re
from dataclasses import dataclass

COMPLETE, PARTIAL, ROM_ONLY, DESC_ONLY = "complete", "partial", "rom-only", "desc-only"
MEDIA = ("image", "thumbnail", "marquee", "video")             # the gamelist tags of the four media slots
FULL_TEXT = ("name", "desc", "releasedate", "developer", "publisher", "genre", "players")
SOME_TEXT = ("name", "desc", "releasedate", "developer", "genre")          # a partly filled game: no publisher, no player count
TEXT_BY_STATE = {COMPLETE: FULL_TEXT, ROM_ONLY: (), DESC_ONLY: ("name", "desc")}


@dataclass(frozen=True)
class DemoSystem:
    folder: str                    # the standard EmulationStation folder name
    fullname: str                  # generic, brand-free
    extensions: str                # as written in es_systems.cfg
    band: str                      # the plain-text system name on a box
    colour: tuple                  # colour of the band at the top of a box


SYSTEMS = (
    DemoSystem("nes", "Classic 8-bit Console", ".nes .NES .zip .ZIP .7z .7Z", "8-BIT CONSOLE", (196, 52, 48)),
    DemoSystem("snes", "Classic 16-bit Console", ".sfc .SFC .smc .SMC .zip .ZIP .7z .7Z", "16-BIT CONSOLE", (96, 84, 168)),
    DemoSystem("megadrive", "16-bit Cartridge Console", ".md .MD .gen .GEN .bin .BIN .zip .ZIP .7z .7Z", "16-BIT SYSTEM", (36, 40, 56)),
    DemoSystem("gb", "Monochrome Handheld Console", ".gb .GB .zip .ZIP .7z .7Z", "HANDHELD", (112, 120, 104)),
    DemoSystem("psx", "32-bit Disc Console", ".cue .CUE .chd .CHD .m3u .M3U .pbp .PBP .iso .ISO", "32-BIT DISC", (52, 96, 156)),
)
SYSTEM_BY_FOLDER = {s.folder: s for s in SYSTEMS}


def slug(title: str) -> str:
    """'Sir Biscuit's Quest' -> 'sir-biscuits-quest' (used in picture and trailer addresses and in file names)."""
    return re.sub(r"[^a-z0-9]+", "-", title.lower().replace("'", "")).strip("-")


@dataclass(frozen=True)
class DemoGame:
    title: str
    system: str
    rom: str                       # ROM file name inside the system folder
    state: str
    scene: str                     # backdrop of the pictures: sky sea space cave castle forest
    hero: str                      # sprite of the pictures (see demo_art.HEROES)
    accent: tuple                  # main colour of its box and logo
    lib: dict                      # text the library holds (only the tags the state allows are written)
    found: dict                    # what the made-up source answers: differs a little from ``lib``
    have: tuple = ()               # media tags with a file (partial games); a complete game has all four
    text: tuple = ()               # text tags written for a partial game
    skin: str = "color"            # 'lcd' paints the screenshots in four greens (the handheld)

    @property
    def slug(self) -> str:
        return slug(self.title)

    @property
    def media(self) -> tuple:
        return MEDIA if self.state == COMPLETE else (self.have if self.state == PARTIAL else ())

    @property
    def text_tags(self) -> tuple:
        return self.text if self.state == PARTIAL else TEXT_BY_STATE[self.state]

    @property
    def stem(self) -> str:
        return self.rom.rsplit(".", 1)[0]


def _g(title, system, rom, state, scene, hero, accent, lib, found, **kw):
    return DemoGame(title, system, rom, state, scene, hero, accent, lib, found, **kw)


# Order matters: within a system the first game is the newest file, so it is the first one in the incomplete list.
GAMES = (
    # ---- nes: a partly finished game (pictures, no logo, no video), a bare ROM and a description-only entry
    _g("Marmalade Moon", "nes", "Marmalade Moon (USA).nes", PARTIAL, "space", "blob", (255, 150, 40),
       lib=dict(releasedate="19901116T000000", developer="Brightbyte Studios", publisher="Cloudberry Cartridge Co.",
                genre="Platformer", players="1",
                desc="Hop across a moon made entirely of marmalade! Guide Dollop, a jam-loving moonling, through sticky craters, "
                     "wobbly toast bridges and the dreaded Crumb Caverns to refill the sleepy sky lantern before dawn."),
       found=dict(releasedate="19901202T000000", developer="Brightbyte Studios Ltd.", publisher="Cloudberry Cartridge Company",
                  genre="Platformer, Action", players="1-2",
                  desc="A sweet-toothed platform adventure on a moon made of fruit preserve. Bounce, stick and slide through twelve "
                       "sugary stages, collect the glowing spoons and outwit the Toast Brigade. A password system saves your "
                       "progress, and the chiptune score is a treat."),
       have=("image", "thumbnail", "video"), text=SOME_TEXT),
    _g("Turnip Turbo", "nes", "Turnip Turbo (USA).nes", ROM_ONLY, "sky", "kart", (150, 90, 200),
       lib={},
       found=dict(releasedate="19910621T000000", developer="Copperkettle Interactive", publisher="Hummingbird House Games",
                  genre="Racing", players="1-2",
                  desc="A top-down farm-road race against the clock, with a turnip behind the wheel. Dodge scarecrows, hop haystacks "
                       "and grab speed-boost radishes across eight muddy tracks. Two friends can race side by side.")),
    _g("Bramble Bandits", "nes", "Bramble Bandits (Europe).nes", DESC_ONLY, "forest", "bandit", (96, 150, 70),
       lib=dict(desc="Three masked voles creep through the Bramble Wood, swiping shiny trinkets from sleeping giants. Sneak past "
                     "owls, swing from thorny vines and share out the loot before sunrise."),
       found=dict(releasedate="19890908T000000", developer="Mossy Rock Software", publisher="Fiddlehead Press",
                  genre="Action, Platformer", players="1-2",
                  desc="Sneak, swing and swipe your way through the Bramble Wood as one of three light-fingered voles. Every level "
                       "hides a secret stash, and the owl guards never blink twice. Pass the controller to a friend for "
                       "two-player heists.")),
    # ---- snes: two finished games and a bare ROM
    _g("Quasar Quokka", "snes", "Quasar Quokka (USA).sfc", COMPLETE, "space", "quokka", (60, 190, 210),
       lib=dict(releasedate="19930312T000000", developer="Pixelpine Works", publisher="Marzipan Media", genre="Shooter, Platformer",
                players="1",
                desc="A cheerful quokka in a patched-up rocket suit sets out to rescue the Quasar Choir, kidnapped by the Static "
                     "Syndicate. Blast through asteroid fields, ride comet trails and dance past boss bots in this speedy "
                     "side-scroller."),
       found=dict(releasedate="19930401T000000", developer="Pixelpine Works Inc.", publisher="Marzipan Media Group",
                  genre="Action, Shooter", players="1-2",
                  desc="Strap on the rocket suit and save the galaxy's favourite singers! Quasar Quokka blends fast shooting stages "
                       "with bouncy platforming, ten bosses and a soundtrack that refuses to sit still. A friend can join in as "
                       "the co-pilot.")),
    _g("Sir Biscuit's Quest", "snes", "Sir Biscuit's Quest (USA).sfc", COMPLETE, "castle", "knight", (230, 170, 60),
       lib=dict(releasedate="19941028T000000", developer="Lantern Lane Games", publisher="Marzipan Media", genre="Role-playing",
                players="1",
                desc="Sir Biscuit, a very small and very brave knight, sets out to recover the stolen Crown of Crumbs. Explore "
                     "twelve castles, trade with talkative toads and win turn-based duels with sword, shield and well-timed jokes."),
       found=dict(releasedate="19941115T000000", developer="Lantern Lane Games Ltd.", publisher="Marzipan Media Group",
                  genre="Adventure, Role-playing", players="1-2",
                  desc="The tiniest knight in the realm faces the biggest quest of all. Sir Biscuit's Quest mixes cosy exploration "
                       "with clever battles, a deep inventory of silly hats, and three different endings to chase.")),
    _g("Glacier Gophers", "snes", "Glacier Gophers (USA).sfc", ROM_ONLY, "cave", "gopher", (110, 200, 240),
       lib={},
       found=dict(releasedate="19950217T000000", developer="Tumbledown Labs", publisher="Kitebrook Interactive", genre="Puzzle",
                  players="1-2",
                  desc="Two gophers tunnel through frozen caverns, pushing ice blocks to melt a path to the warm burrow below. "
                       "Sixty puzzle levels and a head-to-head dig race keep the whole family busy.")),
    # ---- megadrive: a finished game, a game with pictures but no video, a bare ROM
    _g("Kettle Kingdom Racers", "megadrive", "Kettle Kingdom Racers (USA, Europe).md", COMPLETE, "sky", "kettle", (220, 120, 60),
       lib=dict(releasedate="19931203T000000", developer="Saffron Sparrow Studio", publisher="Hummingbird House Games",
                genre="Racing", players="1-2",
                desc="Rev up a teapot-powered kart and race through the Kettle Kingdom's pantry, sink and sugar-bowl canyons. "
                     "Boost on steam vents, dodge rolling lemons and fight for the Grand Whisk Cup."),
       found=dict(releasedate="19940112T000000", developer="Saffron Sparrow Studio Ltd.", publisher="Hummingbird House",
                  genre="Racing, Arcade", players="1-4",
                  desc="Sixteen tracks, twelve kitchen-table racers and one very hot kettle. Kettle Kingdom Racers is an arcade "
                       "racer with split-screen races, secret shortcuts through the cupboards and a time-trial mode.")),
    _g("Lantern Lagoon", "megadrive", "Lantern Lagoon (USA).md", PARTIAL, "sea", "diver", (40, 200, 170),
       lib=dict(releasedate="19920724T000000", developer="Nimbus Nook Games", genre="Adventure",
                desc="A tiny diver with a glowing lantern explores the sunken lagoon, lighting the way for shy sea creatures. "
                     "Solve tide-powered puzzles, befriend a grumpy crab and find the lost Pearl Lamp."),
       found=dict(releasedate="19920810T000000", developer="Nimbus Nook Games Inc.", publisher="Fiddlehead Press",
                  genre="Adventure, Puzzle", players="1",
                  desc="Dive into a gentle underwater adventure where light is the only weapon. Carry your lantern through coral "
                       "mazes, wake the sleepy whales and uncover the secret of the Pearl Lamp."),
       have=("image", "thumbnail", "marquee"), text=SOME_TEXT),
    _g("Orbit Otters", "megadrive", "Orbit Otters (USA).md", ROM_ONLY, "space", "otter", (255, 190, 70),
       lib={},
       found=dict(releasedate="19940520T000000", developer="Brightbyte Studios", publisher="Cloudberry Cartridge Co.",
                  genre="Platformer, Action", players="1-2",
                  desc="Otters in space helmets bounce between tiny planets, collecting stardust clams for the Great Orbit "
                       "Feast. Each planet has its own gravity, and two players can juggle the clams together.")),
    # ---- gb: a finished game, a text-only entry (no pictures at all), a description-only entry
    _g("Tidepool Tactics", "gb", "Tidepool Tactics (World).gb", COMPLETE, "sea", "crab", (240, 100, 90),
       lib=dict(releasedate="19930421T000000", developer="Pixelpine Works", publisher="Marzipan Media", genre="Strategy, Puzzle",
                players="1-2",
                desc="Command a squad of crabs, starfish and snails in turn-based skirmishes on the rocky shore. Position your "
                     "team before the tide comes in, and link up with a friend for a battle on the beach."),
       found=dict(releasedate="19930514T000000", developer="Pixelpine Works Inc.", publisher="Marzipan Media Group",
                  genre="Strategy", players="2",
                  desc="A pocket-sized tactics game set in the rock pools. Twenty maps, nine shellfish heroes and a tide that "
                       "changes the rules every few turns."), skin="lcd"),
    _g("Wobbly Wizards", "gb", "Wobbly Wizards (World).gb", PARTIAL, "castle", "wizard", (170, 110, 220),
       lib=dict(releasedate="19950915T000000", developer="Saffron Sparrow Studio", genre="Puzzle",
                desc="Stack and swap wobbling spellbooks to cast chain reactions before the tower topples. The higher the pile, "
                     "the wilder the magic."),
       found=dict(releasedate="19951001T000000", developer="Saffron Sparrow Studio Ltd.", publisher="Kitebrook Interactive",
                  genre="Puzzle, Action", players="1-2",
                  desc="A falling-block puzzler with a magical twist: every spell you cast makes the tower sway. Clear the board "
                       "to graduate from apprentice to archmage, or duel a friend with spells that spill over."),
       have=(), text=SOME_TEXT, skin="lcd"),
    _g("Dewdrop Dungeon", "gb", "Dewdrop Dungeon (World).gb", DESC_ONLY, "cave", "drop", (90, 190, 120),
       lib=dict(desc="Descend forty floors of a mossy dungeon where every corridor drips with magic dew. Collect droplets to power "
                     "your lantern, avoid sleepy slimes and find the exit before your light runs dry."),
       found=dict(releasedate="19911206T000000", developer="Mossy Rock Software", publisher="Dapple & Dot Entertainment",
                  genre="Adventure, Role-playing", players="1",
                  desc="A tiny dungeon crawler where the rooms rearrange themselves every time you blink. Gather dew to keep your "
                       "lantern lit, learn the slimes' sleepy habits and reach the fortieth floor."), skin="lcd"),
    # ---- psx: a finished game, a game with a picture and a logo but no box art and no video, a bare ROM
    _g("Jellybean Jungle Run", "psx", "Jellybean Jungle Run (USA).chd", COMPLETE, "forest", "bean", (240, 90, 150),
       lib=dict(releasedate="19981112T000000", developer="Lantern Lane Games", publisher="Hummingbird House Games",
                genre="Platformer", players="1-2",
                desc="Sprint, bounce and roll through a candy-coloured jungle as Bean, a spirited jellybean delivering the last sweet "
                     "seed to the Sugar Tree. Twenty-four stages, hidden shortcut vines and a split-screen race mode."),
       found=dict(releasedate="19981204T000000", developer="Lantern Lane Games Ltd.", publisher="Hummingbird House",
                  genre="Platformer, Racing", players="1-4",
                  desc="A bouncy 3D adventure through a jungle that is mostly sugar. Roll downhill, swing from licorice vines and "
                       "race up to three friends through the Sprinkle Canyon.")),
    _g("Starling Skyway", "psx", "Starling Skyway (USA).cue", PARTIAL, "sky", "bird", (80, 160, 240),
       lib=dict(releasedate="19970627T000000", developer="Tumbledown Labs", genre="Flying, Action",
                desc="Swoop through towering cloud cities as a courier starling, delivering parcels at breakneck speed. Loop, roll "
                     "and slipstream past rival birds on sixteen skyways."),
       found=dict(releasedate="19970715T000000", developer="Tumbledown Labs Inc.", publisher="Marzipan Media",
                  genre="Flying, Racing", players="1-2",
                  desc="You are the fastest starling in the sky, and the post must go through. Dive through cloud tunnels, catch "
                       "thermals for extra lift and challenge a friend in the two-player parcel derby."),
       have=("image", "marquee"), text=SOME_TEXT),
    _g("Cinder Citadel", "psx", "Cinder Citadel (USA).chd", ROM_ONLY, "castle", "ember", (255, 100, 40),
       lib={},
       found=dict(releasedate="19990319T000000", developer="Copperkettle Interactive", publisher="Cloudberry Cartridge Co.",
                  genre="Action, Role-playing", players="1",
                  desc="Climb a floating fortress of molten glass as a young ember-smith. Forge new tools between floors, "
                       "outsmart the clockwork guards and learn why the citadel's furnaces were lit.")),
)
GAME_BY_SLUG = {g.slug: g for g in GAMES}

# A 'cleaned' title never carries the region tag; used to look a game up from whatever the window searches for.
STATE_COUNTS = {COMPLETE: 5, PARTIAL: 4, ROM_ONLY: 4, DESC_ONLY: 2}


def games_of(system: str) -> list:
    return [g for g in GAMES if g.system == system]


def all_texts(game: DemoGame) -> list:
    """Every piece of text a game contributes to the demo (for tests that check them against a list of real brands)."""
    out = [game.title, game.rom]
    out += [str(v) for v in game.lib.values()] + [str(v) for v in game.found.values()]
    return out
