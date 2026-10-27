"""What a Save would do, worked out before anything is written. No Qt: the window collects the state of its panels and
text boxes into small picks, ``build_plan`` turns them into a ``SavePlan``, the review dialog shows it, and (only after
the user pressed Save there) the same plan supplies the replace set and the text fields of the request.

Rules (one place, so the dialog and the save cannot disagree):
* A slot is written only when it is ticked, shows a new candidate (never the Current one) and has a usable result.
* A written slot that has a Current file replaces it: its tag goes into ``replace``. If the standard file is overwritten,
  the old copy goes to the undo journal; if the gamelist pointed at a file with another name, the tag is pointed at the
  standard name and the old file stays on disk untouched.
* A Current file the game list does not point at (found by its standard name, or the tag points at a missing file) is
  linked: gamelist.xml gets the tag, the file is neither re-encoded nor replaced. Only when its slot is ticked.
* Only text fields that differ from what the game has now are written; a blank never clears a field.
Values are passed through untouched (they may come from the web): the dialog shows them as plain text.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

from ..controller import changed_fields
from ..models import SLOT_TAGS
from . import logic

PICTURE_SLOTS = ("logo", "boxart", "image")
SLOT_NAMES = {"logo": "Logo", "boxart": "Box art", "image": "Image", "video": "Trailer"}
TEXT_ORDER = ("name", "desc", "releasedate", "developer", "publisher", "genre", "players")
TEXT_LABELS = {"name": "Title", "desc": "Description", "releasedate": "Release date", "developer": "Developer",
               "publisher": "Publisher", "genre": "Genre", "players": "Players"}


@dataclass
class PicturePick:
    """What one picture panel shows: `candidate` is the displayed one (None: nothing), `data` its processed result (None:
    it could not be loaded), `image` the ready preview (opaque here, a QImage in the window)."""
    slot: str
    included: bool
    candidate: Any = None
    data: Optional[bytes] = None
    note: str = ""
    warning: str = ""
    image: Any = None


@dataclass
class VideoPick:
    """The trailer panel: displayed candidate, chosen range (`end` None = the whole trailer), cached thumbnail if any."""
    included: bool
    candidate: Any = None
    start: float = 0.0
    end: Optional[float] = None
    thumb: Any = None


@dataclass
class PictureItem:
    slot: str
    name: str
    tag: str
    data: bytes
    label: str
    source: str
    note: str
    warning: str
    image: Any
    size_text: str
    current: Any = None                  # CurrentAsset being replaced, if any
    current_image: Any = None
    current_size_text: str = ""
    replaces: str = ""                   # the standard file that is overwritten (copied to the undo journal first)
    repoints: str = ""                   # a differently named file the gamelist pointed at; it stays on disk


@dataclass
class VideoItem:
    candidate: Any
    label: str
    source: str
    start: float
    end: Optional[float]
    length: Optional[float]
    start_text: str
    end_text: str
    length_text: str
    thumb: Any = None
    current: Any = None
    current_text: str = ""
    replaces: str = ""
    repoints: str = ""
    tag: str = "video"


@dataclass
class LinkItem:
    """An existing file that only needs its tag in gamelist.xml."""
    slot: str
    name: str
    tag: str
    path: str                            # short path of the file
    current: Any = None
    current_text: str = ""
    image: Any = None


@dataclass
class TextItem:
    tag: str
    label: str
    old: str                             # as shown (a date as YYYY-MM-DD)
    new: str
    raw: str = ""                        # the value as it goes into gamelist.xml


@dataclass
class LeftAlone:
    name: str
    reason: str


@dataclass
class SavePlan:
    game: str
    pictures: list = field(default_factory=list)
    video: Optional[VideoItem] = None
    texts: list = field(default_factory=list)
    left_alone: list = field(default_factory=list)
    links: list = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.pictures or self.video or self.texts or self.links)

    @property
    def link_tags(self) -> set:
        """Media tags whose existing standard file is only linked into gamelist.xml."""
        return {i.tag for i in self.links}

    def summary(self) -> str:
        """One line for the top of the review: '1 new file, 2 files replaced, 5 details, 1 file linked'."""
        written = list(self.pictures) + ([self.video] if self.video else [])
        replaced = sum(1 for i in written if i.current is not None)
        parts = []
        for count, one, many in ((len(written) - replaced, "new file", "new files"), (replaced, "file replaced", "files replaced"),
                                 (len(self.texts), "detail", "details"), (len(self.links), "file linked", "files linked")):
            if count:
                parts.append(f"{count} {one if count == 1 else many}")
        return ", ".join(parts)

    @property
    def fields(self) -> dict:
        """The text tags to write, in gamelist form."""
        return {t.tag: t.raw for t in self.texts}

    @property
    def replace(self) -> set:
        """Media tags whose existing file may be overwritten (the Saver keeps everything else)."""
        items = list(self.pictures) + ([self.video] if self.video else [])
        return {i.tag for i in items if i.current is not None}

    @property
    def replaced_files(self) -> list:
        items = list(self.pictures) + ([self.video] if self.video else [])
        return [i.replaces for i in items if i.replaces]

    @property
    def repointed_files(self) -> list:
        items = list(self.pictures) + ([self.video] if self.video else [])
        return [i.repoints for i in items if i.repoints]


def _rel(path: str, system_dir: str) -> str:
    base = str(system_dir).rstrip("/") + "/"
    return path[len(base):] if path.startswith(base) else path


def _replacement(asset, system_dir: str) -> tuple:
    """(standard file that gets overwritten, other file the gamelist stops pointing at) as short paths."""
    replaces = _rel(asset.standard_path, system_dir) if asset.standard_exists else ""
    repoints = _rel(asset.path, system_dir) if asset.path != asset.standard_path else ""
    return replaces, repoints


def build_plan(game: str, system_dir: str, current, pictures, video, texts: dict, max_seconds: float = 120.0,
               current_images: Optional[dict] = None) -> SavePlan:
    """`current`: ``CurrentAssets``; `pictures`: PicturePicks; `video`: VideoPick or None; `texts`: tag -> the text as it
    should be saved (the date already in gamelist form); `current_images`: slot -> ready preview of the Current picture."""
    plan = SavePlan(game=game)
    current_images = current_images or {}

    def stays(asset) -> str:
        if asset is None:
            return " The game has none, so nothing is added."
        return " It stays as it is." if asset.tag_linked else " It is not in the game list yet and stays unlinked."

    def link(slot, asset, image=None) -> LinkItem:
        return LinkItem(slot, SLOT_NAMES[slot], SLOT_TAGS[slot], _rel(asset.path, system_dir), asset, logic.current_text(asset), image)

    for pick in sorted(pictures, key=lambda p: PICTURE_SLOTS.index(p.slot)):
        name, asset = SLOT_NAMES[pick.slot], current.pictures.get(pick.slot)
        has = asset is not None
        cand = pick.candidate
        if cand is not None and getattr(cand, "current", False):
            if has and not asset.tag_linked and pick.included:
                plan.links.append(link(pick.slot, asset, current_images.get(pick.slot)))
            elif has and not asset.tag_linked:
                plan.left_alone.append(LeftAlone(name, "Include is unticked." + stays(asset)))
            else:
                plan.left_alone.append(LeftAlone(name, "Showing Current, so there is nothing new to write." + stays(asset)))
        elif not pick.included:
            plan.left_alone.append(LeftAlone(name, "Include is unticked." + stays(asset)))
        elif cand is None:
            plan.left_alone.append(LeftAlone(name, "No picture chosen." + stays(asset)))
        elif pick.data is None:
            plan.left_alone.append(LeftAlone(name, "The picture could not be loaded, so it is left out." + stays(asset)))
        else:
            replaces, repoints = _replacement(asset, system_dir) if has else ("", "")
            plan.pictures.append(PictureItem(
                slot=pick.slot, name=name, tag=SLOT_TAGS[pick.slot], data=pick.data, label=cand.label, source=cand.source,
                note=pick.note, warning=pick.warning, image=pick.image,
                size_text=(pick.note + ", " if pick.note else "") + logic.size_text(len(pick.data)),
                current=asset, current_image=current_images.get(pick.slot) if has else None,
                current_size_text=logic.current_text(asset) if has else "", replaces=replaces, repoints=repoints))
    asset = current.video
    has = asset is not None
    cand = video.candidate if video is not None else None
    if video is None or cand is None:
        plan.left_alone.append(LeftAlone("Trailer", "No trailer chosen." + stays(asset)))
    elif getattr(cand, "current", False):
        if has and not asset.tag_linked and video.included:
            plan.links.append(link("video", asset))
        elif has and not asset.tag_linked:
            plan.left_alone.append(LeftAlone("Trailer", "Include video is unticked." + stays(asset)))
        else:
            plan.left_alone.append(LeftAlone("Trailer", "Showing Current, so there is nothing new to write." + stays(asset)))
    elif not video.included:
        plan.left_alone.append(LeftAlone("Trailer", "Include video is unticked." + stays(asset)))
    else:
        length = logic.clip_length(video.start, video.end, cand.duration, max_seconds)
        replaces, repoints = _replacement(asset, system_dir) if has else ("", "")
        plan.video = VideoItem(
            candidate=cand, label=cand.label, source=cand.source, start=video.start, end=video.end, length=length,
            start_text=logic.seconds_text(video.start),
            end_text=logic.seconds_text(video.end) if video.end is not None else "the end of the trailer",
            length_text=(f"{logic.seconds_text(length)} ({length:.0f} s)" if length is not None
                         else f"up to {logic.seconds_text(max_seconds)}"),
            thumb=video.thumb, current=asset, current_text=logic.current_text(asset) if has else "",
            replaces=replaces, repoints=repoints)
    changed = changed_fields(texts, current.fields)
    for tag in TEXT_ORDER:
        if tag in changed:
            plan.texts.append(TextItem(tag, TEXT_LABELS[tag], logic.display_value(tag, current.fields.get(tag, "")),
                                       logic.display_value(tag, changed[tag]), raw=changed[tag]))
    return plan
