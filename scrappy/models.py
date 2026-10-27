"""Plain data classes shared by providers, the UI and the saver."""
from __future__ import annotations

from dataclasses import dataclass, field

SLOTS = ("logo", "boxart", "image", "video")
SLOT_TAGS = {"logo": "marquee", "boxart": "thumbnail", "image": "image", "video": "video"}     # slot -> gamelist tag


@dataclass
class Match:
    """One search hit from a source: 'is this the game you mean?'"""
    title: str
    source: str
    source_id: str
    score: float = 0.0
    year: str = ""


@dataclass
class AssetCandidate:
    """One possible picture or video for a slot."""
    slot: str                      # one of SLOTS
    url: str
    label: str = ""
    source: str = ""
    width: int = 0
    height: int = 0
    transparent: bool = False      # the source is known to deliver real transparency
    kind: str = "file"             # "file" | "hls" | "youtube"
    thumb_url: str = ""            # small preview picture (videos)
    duration: float = 0.0          # seconds, if known
    current: bool = False          # what the game has now (read from disk): shown for comparison, never processed or saved


@dataclass
class CurrentAsset:
    """A file the game has now, as read by ``Controller.load_current``. ``candidate`` is the marker shown in the panel."""
    candidate: AssetCandidate      # ``current=True``; ``url`` is ``current://<slot>``, which nothing can fetch
    path: str                      # the file that is shown (the one the gamelist tag points to, else the standard name)
    standard_path: str             # where this program writes the slot
    standard_exists: bool          # the standard file exists and is not empty (saving over it needs "replace")
    size_bytes: int = 0
    preview: bytes = b""           # small PNG of a picture; empty when the file could not be read as a picture
    problem: str = ""              # why there is no preview (plain sentence)
    duration: float = 0.0          # seconds, videos only, 0 when unknown
    tag_linked: bool = True        # gamelist.xml points at this file; False: found by its standard name only (Save can link it)


@dataclass
class GameInfo:
    name: str
    description: str = ""
    release_date: str = ""         # gamelist format: YYYYMMDDT000000
    developer: str = ""
    publisher: str = ""
    genre: str = ""
    players: str = ""
    source: str = ""
    source_id: str = ""
    assets: list = field(default_factory=list)

    def assets_for(self, slot: str) -> list:
        return [a for a in self.assets if a.slot == slot]
