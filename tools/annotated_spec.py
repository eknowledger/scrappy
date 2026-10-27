"""The numbered callouts of the annotated screenshots in docs/USAGE.md (plain data, no Qt, no Pillow).

``tools/make_annotated_screenshots.py`` draws these callouts on the real window; ``docs/USAGE.md`` explains them in a legend table
under each picture; ``tests/test_annotated_screenshots.py`` checks that the two agree, so a picture and its legend cannot drift apart.

Per callout:
* ``number``  the number on the badge (1, 2, 3 ... in the order the picture is read).
* ``target``  where the control is, as an expression the tool resolves against the live widgets. ``system_box`` is an attribute of
  the main window (``logo_panel.preview`` and ``hints[releasedate]`` work too); ``text:Search`` a button or label by its text;
  ``acc:Summary`` a widget by its accessible name; ``title:logo_panel`` the title of a group box; ``row:Title`` a row of the game list;
  ``union:a|b`` the box around several; ``scope>expr`` looks for ``expr`` inside ``scope``; a leading ``1:`` picks the second picture
  of a composed image. Positions always come from the widget geometry, never from pixel numbers.
* ``label``   the "Control" cell of the legend table in docs/USAGE.md, as plain text (bold marks removed).
* ``side``    where the badge sits relative to the control: ``l r t b`` (outside, with a short leader line), ``tl tr bl br`` (outside
  corner), ``c`` (on top of it); ``@0.3`` moves it to 30 % along that edge, ``:12`` pushes it 12 pixels further out.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class Callout:
    number: int
    target: str
    label: str
    side: str = "t"


@dataclass(frozen=True)
class Shot:
    name: str                            # file name without ".png"
    alt: str                             # alt text of the picture in docs/USAGE.md
    callouts: Tuple[Callout, ...]


def _c(number, target, label, side="t") -> Callout:
    return Callout(number, target, label, side)


SHOTS = (
    Shot("window-tour",
         "The Scrappy window with fourteen numbered callouts: the system box, the filter and the game list on the left; the name box, "
         "the Search button and the match list above four panels (logo, box art, image and trailer) in the middle; the details form "
         "and the Save, Skip and Undo buttons on the right; the status line at the bottom and the Tools menu at the top.",
         (_c(1, "system_box", "System box", "l"),
          _c(2, "filter_box", "Filter box", "l"),
          _c(3, "list", "Game list", "l@0.4"),
          _c(4, "search_edit", "Name box", "t"),
          _c(5, "search_btn", "Search button", "t"),
          _c(6, "match_box", "Match list", "t"),
          _c(7, "title:logo_panel", "Logo (transparent) panel", "r"),
          _c(8, "title:box_panel", "Box art panel", "r"),
          _c(9, "title:image_panel", "Image panel", "r"),
          _c(10, "title:video_panel", "Trailer / video panel", "r"),
          _c(11, "union:name_edit|desc_edit", "Details form", "r@0.03"),
          _c(12, "union:save_btn|skip_btn|undo_btn", "Save, Skip and Undo last save", "r"),
          _c(13, "status_label", "Status line", "b@0.15"),
          _c(14, "action:Tools", "Tools menu", "t"))),
    Shot("slot-panel",
         "A close-up of the Logo (transparent) panel with ten numbered callouts: the title, the picture, the caption with the source "
         "and size, the Remove background box, the Make text logo button, the two arrow buttons, the Include box, and the Paste link, "
         "Choose file and Web search buttons.",
         (_c(1, "title:logo_panel", "Panel title", "t"),
          _c(2, "logo_panel.preview", "Picture", "t@0.5"),
          _c(3, "logo_panel.info", "Caption", "r"),
          _c(4, "logo_panel.remove_bg", "Remove background box", "l:5"),
          _c(5, "logo_panel>text:Make text logo", "Make text logo button", "r"),
          _c(6, "union:logo_panel.prev_btn|logo_panel.next_btn", "Arrow buttons", "l"),
          _c(7, "logo_panel.use_box", "Include box", "r"),
          _c(8, "logo_panel>text:Paste link…", "Paste link button", "b"),
          _c(9, "logo_panel>text:Choose file…", "Choose file button", "b"),
          _c(10, "logo_panel>text:Web search", "Web search button", "b"))),
    Shot("trailer-panel",
         "A close-up of the Trailer / video panel with ten numbered callouts: the video list, the thumbnail, the Start and End boxes, "
         "the Whole trailer box, the clip summary, the Preview and Choose file buttons, the Include video box and the hint line.",
         (_c(1, "video_panel.combo", "Video list", "t"),
          _c(2, "video_panel.thumb", "Thumbnail", "l"),
          _c(3, "video_panel.start", "Start box", "r"),
          _c(4, "video_panel.end", "End box", "r"),
          _c(5, "video_panel.whole", "Whole trailer box", "r"),
          _c(6, "video_panel.summary", "Clip summary", "l"),
          _c(7, "video_panel>text:Preview…", "Preview button", "l"),
          _c(8, "video_panel>text:Choose file…", "Choose file button", "r"),
          _c(9, "video_panel.use_box", "Include video box", "r"),
          _c(10, "video_panel.note", "Hint line", "b"))),
    Shot("details-form",
         "The details form on the right of the window with nine numbered callouts: the Title box, the Release date box, a found line "
         "with its Use button, the Description box, the Use all found details button and the Save, Skip and Undo last save buttons.",
         (_c(1, "name_edit", "Title box", "r"),
          _c(2, "date_edit", "Release date box", "r"),
          _c(3, "hints[releasedate].label", "Found line", "l"),
          _c(4, "hints[releasedate]>text:Use", "Use button", "r"),
          _c(5, "desc_edit", "Description box", "r"),
          _c(6, "use_all_btn", "Use all found details button", "r"),
          _c(7, "save_btn", "Save button", "r"),
          _c(8, "skip_btn", "Skip button", "r"),
          _c(9, "undo_btn", "Undo last save button", "r"))),
    Shot("missing-list",
         "The game list with the Missing something filter: three games, each marked with a cross and a note saying what it lacks, "
         "and five numbered callouts: the System box, the Filter box, the selected game, the cross mark and the missing note.",
         (_c(1, "system_box", "System box", "r"),
          _c(2, "filter_box", "Filter box", "r"),
          _c(3, "row:Marmalade Moon", "Selected game", "l:4"),
          _c(4, "rowpart:Turnip Turbo|mark", "Cross mark", "l"),
          _c(5, "rowpart:Bramble Bandits|note", "Missing note", "r"))),
    Shot("current-vs-new",
         "Two copies of the Box art panel. On the left the panel shows the picture the game has now, with the caption Current and "
         "stays as it is. On the right, after pressing the arrow, it shows a new candidate with its source and size. Six numbered callouts.",
         (_c(1, "0:box_panel.preview", "Current picture", "t"),
          _c(2, "0:box_panel.info", "Current caption", "b"),
          _c(3, "0:union:box_panel.prev_btn|box_panel.next_btn", "Arrow buttons", "l"),
          _c(4, "1:box_panel.preview", "New candidate", "t"),
          _c(5, "1:box_panel.info", "New caption", "b"),
          _c(6, "1:box_panel.use_box", "Include box", "b"))),
    Shot("review-dialog",
         "The Review before saving window with eight numbered callouts: the summary line, the Pictures section with the current and "
         "the new picture, the Trailer section, the Details table, the list of files that will be replaced, the list of items left "
         "as they are, and the Save and Cancel buttons.",
         (_c(1, "acc:Summary", "Summary line", "l"),
          _c(2, "section:Pictures", "Pictures section", "l"),
          _c(3, "section:Trailer", "Trailer section", "l"),
          _c(4, "section:Details", "Details table", "l"),
          _c(5, "section:Files that will be replaced", "Files that will be replaced", "l"),
          _c(6, "section:Left as they are", "Left as they are", "l"),
          _c(7, "save_btn", "Save button", "b"),
          _c(8, "cancel_btn", "Cancel button", "b"))),
    Shot("tools-menu",
         "The Tools menu opened: Install / update YouTube downloader and Reload game lists in EmulationStation, with three numbered "
         "callouts.",
         (_c(1, "action:Tools", "Tools menu", "t"),
          _c(2, "menuitem:Install / update YouTube downloader", "Install / update YouTube downloader", "r"),
          _c(3, "menuitem:Reload game lists in EmulationStation", "Reload game lists in EmulationStation", "r"))),
    Shot("task-dialog-running",
         "The progress window of the Install / update YouTube downloader entry of the Tools menu, in the middle of a download: a blue mark, the title, a line "
         "saying what is going on, a progress bar at 56 percent, the size line and the Cancel button. Six numbered callouts.",
         (_c(1, "acc:State", "State mark", "l"),
          _c(2, "acc:Heading", "Title", "r"),
          _c(3, "acc:Message", "Message", "r"),
          _c(4, "acc:Progress", "Progress bar", "r"),
          _c(5, "acc:Size", "Size line", "r"),
          _c(6, "cancel_btn", "Cancel button", "b"))),
    Shot("task-dialog-result",
         "The same window when the download has finished: a green check mark, the sentence YouTube downloader installed, the version and "
         "the file it was saved to, and the OK button. Four numbered callouts.",
         (_c(1, "acc:State", "Check mark", "l"),
          _c(2, "acc:Heading", "Result", "r"),
          _c(3, "acc:Result", "What was done, and where", "r"),
          _c(4, "ok_btn", "OK button", "b"))),
    Shot("task-dialog-failed",
         "The same window when the download failed: a red cross, the plain reason, what to try, the Details button opened to show the "
         "technical text, and the Close button. Seven numbered callouts.",
         (_c(1, "acc:State", "Cross", "l"),
          _c(2, "acc:Heading", "What failed", "r"),
          _c(3, "acc:Message", "The reason", "r"),
          _c(4, "acc:What to try", "What to try", "r"),
          _c(5, "details_btn", "Details button", "l"),
          _c(6, "acc:Technical details", "Technical details", "r"),
          _c(7, "ok_btn", "Close button", "b"))),
    Shot("after-save",
         "The window after a save: the game list shows the game with a check mark, the status line says what was written, and the "
         "Undo last save button is next to it. Three numbered callouts.",
         (_c(1, "0:row:Cinder Citadel", "Game row with a check mark", "r"),
          _c(2, "1:undo_btn", "Undo last save button", "l"),
          _c(3, "2:status_label", "Status line", "t"))),
)

BY_NAME = {s.name: s for s in SHOTS}


def export_spec() -> dict:
    """The callouts as plain data (written to docs/screenshots/annotated/spec.json by the tool)."""
    return {s.name: {"alt": s.alt, "callouts": [{"number": c.number, "target": c.target, "label": c.label, "side": c.side}
                                                for c in s.callouts]} for s in SHOTS}
