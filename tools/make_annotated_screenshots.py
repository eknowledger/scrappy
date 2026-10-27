"""Makes the annotated screenshots of docs/USAGE.md: the real window on the invented demo library, with numbered badges drawn on it.

    python tools/make_annotated_screenshots.py [--out DIR]       (default: docs/screenshots/annotated)

It runs headless (QT_QPA_PLATFORM=offscreen), builds the made-up library (``scrappy.ui.demo.build_demo``; nothing real is read, nothing
touches the network), drives the real window with the same ``Shooter`` as tools/make_screenshots.py, grabs it, and draws a numbered
round badge (and a short leader line) next to each control named in ``tools/annotated_spec.py``. Where a badge goes is worked out from
the real geometry of the widget (``mapTo``, ``geometry``, the rows of the list, the actions of the menu), never from fixed pixel
positions, so the script keeps working when the layout changes. The pictures are the same on every run.

Writes one PNG per entry of ``annotated_spec.SHOTS`` plus ``spec.json`` (the same callouts as data) into DIR.
Exit code 0 when everything was made, 1 on a failure (the message says which step), 2 when PyQt5 is not installed.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from PyQt5 import QtCore, QtGui, QtWidgets
except ImportError as exc:                                           # a plain message instead of a traceback
    sys.stderr.write(f"make_annotated_screenshots: PyQt5 could not be loaded ({exc}). Install it (pip install PyQt5) and try again.\n")
    raise SystemExit(2)

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

QtCore.qInstallMessageHandler(lambda kind, context, message: None if "propagateSizeHints" in message else sys.stderr.write(message + "\n"))

from annotated_spec import BY_NAME, export_spec  # noqa: E402
from make_screenshots import Failed, Shooter  # noqa: E402

from scrappy.providers.youtube import InstallResult, YouTubeCancelled, YouTubeError  # noqa: E402
from scrappy.ui.demo import build_demo  # noqa: E402
from scrappy.ui.jobs import set_reporter  # noqa: E402
from scrappy.ui.main_window import MainWindow  # noqa: E402
from scrappy.ui.review import ReviewDialog  # noqa: E402
from scrappy.ui.workers import ensure_dispatcher  # noqa: E402

WIDE = (1600, 900)                       # the window for close-ups
TOUR = (1360, 820)                       # the window for the tour, shown at TOUR_SCALE so that the whole picture stays under 1200 px
TOUR_SCALE = 0.82
MAX_PNG_BYTES = 350_000
SUPERSAMPLE = 4                          # the badges are drawn four times as large and scaled down (smooth edges)

# the look of a badge: brand yellow with dark ink, a dark outline and a white ring, so it reads on light and on dark areas
FILL, INK, RING = (255, 200, 61), (26, 28, 34), (255, 255, 255)
BADGE_RADIUS = 14
GAP = 7                                  # between a badge and its control, where there is a leader line
MARGIN = 38                              # free border around the frames: the badges sit there when the control is at the window's edge
PAPER = (232, 235, 240)                  # colour of that border
FRAME_LINE = (150, 156, 168)
CAPTION_INK = (58, 63, 75)
FRAME_GAP = (30, 26)                     # between frames in a row, and between rows

# how the frames of an image are arranged: rows of frame numbers (default: one frame)
ROWS = {"current-vs-new": ((0, 1),), "after-save": ((0, 1), (2,))}


# ---------------------------------------------------------------- finding a control and its rectangle

def _walk(obj, path: str):
    """``logo_panel.preview`` and ``hints[releasedate].label``: attributes and dictionary keys, from the window."""
    for name, key in re.findall(r"([A-Za-z_]\w*)|\[([^\]]+)\]", path):
        obj = obj[key] if key else getattr(obj, name)
    return obj


def _rect_in(root, widget) -> QtCore.QRect:
    if widget is root:
        return QtCore.QRect(0, 0, root.width(), root.height())
    return QtCore.QRect(widget.mapTo(root, QtCore.QPoint(0, 0)), widget.size())


def _plain(text: str) -> str:
    return text.replace("&", "").strip()


def _find_text(scope, text: str):
    for widget in scope.findChildren(QtWidgets.QWidget):
        if isinstance(widget, (QtWidgets.QAbstractButton, QtWidgets.QLabel)) and widget.isVisible() and _plain(widget.text()) == text:
            return widget
    raise Failed(f"no control with the text {text!r}")


def _find_accessible(scope, name: str):
    for widget in scope.findChildren(QtWidgets.QWidget):
        if widget.accessibleName() == name and widget.isVisible():
            return widget
    raise Failed(f"no control with the accessible name {name!r}")


def _group_title_rect(root, box) -> QtCore.QRect:
    """The label of a group box (its title text) in the coordinates of `root`."""
    opt = QtWidgets.QStyleOptionGroupBox()
    box.initStyleOption(opt)                                          # (protected in C++, allowed on the program's own subclasses)
    label = box.style().subControlRect(QtWidgets.QStyle.CC_GroupBox, opt, QtWidgets.QStyle.SC_GroupBoxLabel, box)
    return label.translated(box.mapTo(root, QtCore.QPoint(0, 0))).adjusted(0, 0, 5, 0)


def _list_row(root, list_widget, title: str):
    for i in range(list_widget.count()):
        item = list_widget.item(i)
        if title in item.text():
            top_left = list_widget.viewport().mapTo(root, QtCore.QPoint(0, 0))
            return item, list_widget.visualItemRect(item).translated(top_left)
    raise Failed(f"{title} is not in the game list")


def _section(root, dialog, heading: str) -> QtCore.QRect:
    """A section of the review dialog: from its divider line to the next one."""
    body = dialog.scroll.widget()
    head = _find_text(body, heading)
    lines = sorted(_rect_in(root, f).top() for f in body.findChildren(QtWidgets.QFrame) if f.frameShape() == QtWidgets.QFrame.HLine)
    top = max([y for y in lines if y < head.mapTo(root, QtCore.QPoint(0, 0)).y()] or [head.mapTo(root, QtCore.QPoint(0, 0)).y()])
    below = [y for y in lines if y > top + 2]
    body_rect = _rect_in(root, body)
    bottom = min(below) if below else body_rect.bottom()
    return QtCore.QRect(body_rect.left(), top + 4, body_rect.width(), bottom - top - 8)


def resolve(root, expr: str, extras: dict) -> QtCore.QRect:
    """The rectangle (in the coordinates of `root`) of what the callout `expr` points at. See tools/annotated_spec.py."""
    if expr.startswith("union:"):
        parts = [resolve(root, p, extras) for p in expr[len("union:"):].split("|")]
        total = parts[0]
        for p in parts[1:]:
            total = total.united(p)
        return total
    scope = root
    if ">" in expr:
        scope_expr, expr = expr.split(">", 1)
        scope = _walk(root, scope_expr)
    if expr.startswith("text:"):
        return _rect_in(root, _find_text(scope, expr[len("text:"):]))
    if expr.startswith("acc:"):
        return _rect_in(root, _find_accessible(scope, expr[len("acc:"):]))
    if expr.startswith("title:"):
        return _group_title_rect(root, _walk(root, expr[len("title:"):]))
    if expr.startswith("row:"):
        return _list_row(root, root.list, expr[len("row:"):])[1]
    if expr.startswith("rowpart:"):
        title, part = expr[len("rowpart:"):].rsplit("|", 1)
        item, row = _list_row(root, root.list, title)
        fm = QtGui.QFontMetrics(root.list.font())
        text = item.text()
        pad = 6
        if part == "mark":
            return QtCore.QRect(row.left() + pad, row.top() + 2, fm.horizontalAdvance(text[0]), fm.height())
        cut = text.index("(missing")
        x = row.left() + pad + fm.horizontalAdvance(text[:cut])
        return QtCore.QRect(x, row.top() + 2, row.right() - x - pad, fm.height())
    if expr.startswith("section:"):
        return _section(root, root, expr[len("section:"):])
    if expr.startswith("action:"):
        bar = root.menuBar()
        for action in bar.actions():
            if _plain(action.text()) == expr[len("action:"):]:
                return bar.actionGeometry(action).translated(bar.mapTo(root, QtCore.QPoint(0, 0)))
        raise Failed(f"no menu {expr!r}")
    if expr == "menu":
        menu, origin = extras["menu"]
        return QtCore.QRect(origin, menu.size())
    if expr.startswith("menuitem:"):
        menu, origin = extras["menu"]
        for action in menu.actions():
            if _plain(action.text()) == expr[len("menuitem:"):]:
                return menu.actionGeometry(action).translated(origin)
        raise Failed(f"no menu entry {expr!r}")
    return _rect_in(root, _walk(scope, expr))


# ---------------------------------------------------------------- frames: a grab of a window, a part of it, or a dialog

@dataclass
class Frame:
    image: Image.Image
    rects: dict                                            # expression -> (x, y, w, h) in this frame
    caption: str = ""
    origin: tuple = (0, 0)                                 # where the frame sits on the canvas (set by compose)


def to_pil(pixmap: QtGui.QPixmap) -> Image.Image:
    buf = QtCore.QBuffer()
    buf.open(QtCore.QIODevice.WriteOnly)
    pixmap.toImage().save(buf, "PNG")
    return Image.open(io.BytesIO(bytes(buf.data()))).convert("RGB")


def capture(shot: Shooter, root, exprs, region=None, pad=(12, 12, 12, 12), caption: str = "", menu=None, scale: float = 1.0, width: int = 0) -> Frame:
    """Grab `root` (the window or a dialog), optionally with a menu opened on top, and cut out the `region` (an expression) with `pad`
    pixels around it (left, top, right, bottom). `exprs`: every callout target to locate; their rectangles move with the cut."""
    shot.settle(12)
    for edit in root.findChildren(QtWidgets.QLineEdit):             # long texts show their beginning, not their end
        edit.setCursorPosition(0)
    image = to_pil(root.grab())
    extras = {}
    if menu is not None:                                            # a popup cannot be grabbed with the window: paste it where it opens
        bar = root.menuBar()
        action = menu.menuAction()
        origin = bar.actionGeometry(action).bottomLeft() + bar.mapTo(root, QtCore.QPoint(0, 0))
        menu.adjustSize()
        image.paste(to_pil(menu.grab()), (origin.x(), origin.y()))
        extras["menu"] = (menu, origin)
    rects = {e: resolve(root, e, extras) for e in exprs}
    left = top = 0
    if region is not None:
        r = resolve(root, region, extras)
        left, top = max(0, r.left() - pad[0]), max(0, r.top() - pad[1])
        right, bottom = min(image.width, r.right() + 1 + pad[2]), min(image.height, r.bottom() + 1 + pad[3])
        if width:
            right = min(right, left + width)
        image = image.crop((left, top, right, bottom))
    moved = {}
    for e, r in rects.items():                                       # (a control cut by the crop is pointed at where it is visible)
        box = r.translated(-left, -top).intersected(QtCore.QRect(0, 0, image.width, image.height))
        moved[e] = (box.x(), box.y(), box.width(), box.height())
    if scale != 1.0:
        image = image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS)
        moved = {e: tuple(v * scale for v in r) for e, r in moved.items()}
    return Frame(image, moved, caption)


# ---------------------------------------------------------------- badges

def _font(size: int):
    try:
        return ImageFont.load_default(size)                          # Pillow's own font: the same on every machine
    except Exception:                                                # noqa: BLE001 - old Pillow or no FreeType
        return ImageFont.load_default()


_SIDE = re.compile(r"(tl|tr|bl|br|l|r|t|b|c)(?:@([0-9.]+))?(?::(\d+))?")


@dataclass
class Badge:
    number: int
    cx: float
    cy: float
    ax: float                                              # where its leader line meets the control
    ay: float
    rect: tuple                                            # the control, on the canvas
    side: str
    line: bool = True
    shifted: bool = field(default=False, repr=False)


def place(number: int, rect: tuple, side: str) -> Badge:
    m = _SIDE.fullmatch(side)
    if not m:
        raise Failed(f"callout {number}: bad side {side!r}")
    where, frac, extra = m.group(1), float(m.group(2) or 0.5), int(m.group(3) or 0)
    x, y, w, h = rect
    d = BADGE_RADIUS + GAP + extra
    tip = 3                                                          # the leader ends just outside the control, not on its text
    if where == "l":
        return Badge(number, x - d, y + h * frac, x - tip, y + h * frac, rect, where)
    if where == "r":
        return Badge(number, x + w + d, y + h * frac, x + w + tip, y + h * frac, rect, where)
    if where == "t":
        return Badge(number, x + w * frac, y - d, x + w * frac, y - tip, rect, where)
    if where == "b":
        return Badge(number, x + w * frac, y + h + d, x + w * frac, y + h + tip, rect, where)
    diag = d * 0.72
    corners = {"tl": (x, y, -1, -1), "tr": (x + w, y, 1, -1), "bl": (x, y + h, -1, 1), "br": (x + w, y + h, 1, 1)}
    if where in corners:
        px, py, sx, sy = corners[where]
        return Badge(number, px + sx * diag, py + sy * diag, px + sx * tip, py + sy * tip, rect, where)
    return Badge(number, x + w * frac, y + h / 2, x + w * frac, y + h / 2, rect, where, line=False)


def separate(badges: list, width: int, height: int) -> None:
    """Keep every badge inside the picture and push apart badges that would touch (sliding along the edge they sit on)."""
    low, need = BADGE_RADIUS + 3, 2 * BADGE_RADIUS + 5
    for _ in range(40):
        moved = False
        for b in badges:
            cx, cy = min(max(b.cx, low), width - low), min(max(b.cy, low), height - low)
            moved |= (cx, cy) != (b.cx, b.cy)
            b.cx, b.cy = cx, cy
        for i, a in enumerate(badges):
            for b in badges[i + 1:]:
                if ((a.cx - b.cx) ** 2 + (a.cy - b.cy) ** 2) ** 0.5 >= need:
                    continue
                x, y, w, h = b.rect
                if b.side in ("l", "r"):                              # on a vertical edge: slide up or down
                    room = max(need ** 2 - (a.cx - b.cx) ** 2, 0) ** 0.5
                    shift = (1 if b.cy >= a.cy else -1) * (room - abs(b.cy - a.cy) + 0.5)
                    b.cy += shift
                    b.ay = min(max(b.ay + shift, y + 2), y + h - 2)
                else:                                                  # on a horizontal edge: slide left or right
                    room = max(need ** 2 - (a.cy - b.cy) ** 2, 0) ** 0.5
                    shift = (1 if b.cx >= a.cx else -1) * (room - abs(b.cx - a.cx) + 0.5)
                    b.cx += shift
                    b.ax = min(max(b.ax + shift, x + 2), x + w - 2)
                moved = True
        if not moved:
            break


def draw_badges(canvas: Image.Image, badges: list) -> Image.Image:
    s = SUPERSAMPLE
    layer = Image.new("RGBA", (canvas.width * s, canvas.height * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    font = _font(int(BADGE_RADIUS * 1.25 * s))
    for b in badges:                                                 # all lines first, so no line crosses a badge
        if b.line:
            ends = [(b.cx * s, b.cy * s), (b.ax * s, b.ay * s)]
            d.line(ends, fill=RING, width=int(5.5 * s))
            d.line(ends, fill=INK, width=int(3 * s))
            for radius, colour in ((5.2, RING), (3.6, INK)):
                d.ellipse([(b.ax - radius) * s, (b.ay - radius) * s, (b.ax + radius) * s, (b.ay + radius) * s], fill=colour)
    for b in badges:
        for radius, colour in ((BADGE_RADIUS + 3.2, RING), (BADGE_RADIUS + 1.8, INK), (BADGE_RADIUS - 0.2, FILL)):
            d.ellipse([(b.cx - radius) * s, (b.cy - radius) * s, (b.cx + radius) * s, (b.cy + radius) * s], fill=colour)
        text = str(b.number)
        try:
            d.text((b.cx * s, b.cy * s), text, font=font, fill=INK, anchor="mm", stroke_width=int(0.7 * s), stroke_fill=INK)
        except (ValueError, TypeError):                              # the small bitmap font has no anchors
            w, h = d.textbbox((0, 0), text, font=font)[2:]
            d.text((b.cx * s - w / 2, b.cy * s - h / 2), text, font=font, fill=INK)
    layer = layer.resize(canvas.size, Image.LANCZOS)
    return Image.alpha_composite(canvas.convert("RGBA"), layer).convert("RGB")


def compose(shot, frames: list, out: Path) -> dict:
    """Lay the frames out on a canvas, draw the badges of `shot` and write the PNG. Returns where the badges are."""
    rows = ROWS.get(shot.name, ((0,),))
    left, top = MARGIN, MARGIN
    y = top
    placed = []                                                      # (frame, x, y)
    width = 0
    for row in rows:
        x = left
        row_h = 0
        for index in row:
            frame = frames[index]
            lift = 24 if frame.caption else 0
            frame.origin = (x, y + lift)
            placed.append(frame)
            x += frame.image.width + FRAME_GAP[0]
            row_h = max(row_h, frame.image.height + lift)
        width = max(width, x - FRAME_GAP[0])
        y += row_h + FRAME_GAP[1]
    height = y - FRAME_GAP[1] + MARGIN
    canvas = Image.new("RGB", (width + MARGIN, height), PAPER)
    d = ImageDraw.Draw(canvas)
    font = _font(15)
    for frame in placed:
        x, y = frame.origin
        canvas.paste(frame.image, (x, y))
        d.rectangle([x - 1, y - 1, x + frame.image.width, y + frame.image.height], outline=FRAME_LINE)
        if frame.caption:
            d.text((x, y - 22), frame.caption, font=font, fill=CAPTION_INK)
    badges = []
    for callout in shot.callouts:
        index, expr = 0, callout.target
        m = re.match(r"(\d+):(.*)", expr)
        if m:
            index, expr = int(m.group(1)), m.group(2)
        frame = frames[index]
        fx, fy = frame.origin
        x, y, w, h = frame.rects[expr]
        badges.append(place(callout.number, (fx + x, fy + y, w, h), callout.side))
    separate(badges, canvas.width, canvas.height)
    canvas = draw_badges(canvas, badges)
    path = out / f"{shot.name}.png"
    canvas.save(path, "PNG", optimize=True)
    if path.stat().st_size > MAX_PNG_BYTES:                          # pixel-art pictures: 256 colours look the same
        canvas.quantize(256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).save(path, "PNG", optimize=True)
    print(f"{path.name}: {canvas.width}x{canvas.height}, {len(badges)} callouts, {path.stat().st_size // 1024} KB")
    return {"size": [canvas.width, canvas.height], "radius": BADGE_RADIUS,
            "badges": [{"number": b.number, "x": round(b.cx, 1), "y": round(b.cy, 1)} for b in badges]}


# ---------------------------------------------------------------- the shots: what the window shows for each picture

class Ctx:
    def __init__(self, app, win, shooter):
        self.app, self.win, self.sh = app, win, shooter

    def exprs(self, name: str, frame: int = 0) -> list:
        """The targets of image `name` that belong to frame number `frame`."""
        found = []
        for callout in BY_NAME[name].callouts:
            m = re.match(r"(\d+):(.*)", callout.target)
            index, expr = (int(m.group(1)), m.group(2)) if m else (0, callout.target)
            if index == frame and expr not in found:
                found.append(expr)
        return found

    def frame(self, name: str, root=None, frame: int = 0, **kw) -> Frame:
        return capture(self.sh, root or self.win, self.exprs(name, frame), **kw)

    def search(self) -> None:
        """Search and wait for everything to be shown (a game that has a video of its own shows no thumbnail until another is chosen)."""
        w = self.win
        w.do_search()
        self.sh.pump(lambda: w.match_box.count() >= 1 and w.info is not None, "the search results")
        self.sh.pump(lambda: not (w.logo_panel.busy or w.box_panel.busy or w.image_panel.busy), "the pictures")
        chosen = w.video_panel.selected_candidate()
        if chosen is not None and chosen.thumb_url:
            self.sh.pump(lambda: w.video_panel.thumb.has_image(), "the trailer thumbnail")
        self.sh.settle()

    def size(self, size) -> None:
        self.win.resize(*size)
        self.sh.settle(20)


def shot_missing_list(c: Ctx):
    c.size(WIDE)
    c.sh.choose_system("nes", only_missing=True)
    c.sh.select_game("Marmalade Moon")
    return [c.frame("missing-list", region="union:system_box|filter_box|row:Bramble Bandits", pad=(14, 28, 3, 18))]


def shot_slot_panel(c: Ctx):
    c.size(WIDE)
    c.sh.select_game("Bramble Bandits", "nes")
    c.search()
    return [c.frame("slot-panel", region="logo_panel", pad=(6, 6, 6, 6))]


def shot_tour(c: Ctx):
    c.size(TOUR)
    c.sh.choose_system("megadrive", only_missing=True)
    c.sh.select_game("Lantern Lagoon")
    c.search()
    c.sh.step_to_new(c.win.logo_panel)
    return [c.frame("window-tour", scale=TOUR_SCALE)]


def shot_trailer(c: Ctx):
    c.size(WIDE)
    c.sh.select_game("Orbit Otters", "megadrive")
    c.search()
    video = c.win.video_panel
    youtube = next(i for i in range(video.combo.count()) if video.combo.itemData(i).kind == "youtube")
    video.combo.setCurrentIndex(youtube)
    c.sh.pump(lambda: video.thumb.has_image(), "the YouTube thumbnail")
    video.start.setValue(14.0)
    video.end.setValue(44.0)
    return [c.frame("trailer-panel", region="video_panel", pad=(6, 6, 6, 6))]


def shot_details(c: Ctx):
    c.size(WIDE)
    c.sh.choose_system("snes", only_missing=False)
    c.sh.select_game("Quasar Quokka")
    c.search()
    return [c.frame("details-form", region="union:name_edit|undo_btn", pad=(2, 16, 2, 4))]


def shot_current_vs_new(c: Ctx):
    c.size(WIDE)
    c.sh.choose_system("gb", only_missing=False)
    c.sh.select_game("Tidepool Tactics")
    c.search()
    before = c.frame("current-vs-new", frame=0, region="box_panel", pad=(1, 1, 1, 1), caption="The game's own box art")
    c.win.box_panel.step(1)
    c.sh.step_to_new()
    c.sh.pump(lambda: not c.win.box_panel.busy, "the new box art")
    c.sh.settle()
    after = c.frame("current-vs-new", frame=1, region="box_panel", pad=(1, 1, 1, 1), caption="After pressing the right arrow")
    return [before, after]


def _review_dialog(c: Ctx, plan, width=900):
    dialog = ReviewDialog(plan, c.win)
    dialog.resize(width, 1600)
    dialog.show()
    c.sh.settle()
    body = dialog.scroll.widget()
    height = 0
    for _ in range(6):                                               # (wrapped labels settle over a few layout passes)
        wanted = min(1300, body.layout().totalHeightForWidth(body.width()) + 200)
        if wanted == height:
            break
        height = wanted
        dialog.resize(width, height)
        c.sh.settle(20)
    return dialog


def shot_review(c: Ctx):
    c.size(WIDE)
    c.sh.select_game("Starling Skyway", "psx")
    c.search()
    c.sh.step_to_new(c.win.logo_panel, c.win.image_panel)
    video = c.win.video_panel
    stream = next(i for i in range(video.combo.count()) if video.combo.itemData(i).kind == "hls")
    video.combo.setCurrentIndex(stream)
    c.sh.pump(lambda: video.thumb.has_image(), "the stream thumbnail")
    c.win.use_all_btn.click()
    c.win.box_panel.use_box.setChecked(False)                        # so that "Left as they are" has an entry
    c.sh.settle()
    dialog = _review_dialog(c, c.win.make_plan())
    try:
        frame = capture(c.sh, dialog, c.exprs("review-dialog"))
    finally:
        dialog.close()
        dialog.deleteLater()
    return [frame]


def shot_tools_menu(c: Ctx):
    c.size(WIDE)
    menu = c.win.menuBar().actions()[0].menu()
    return [c.frame("tools-menu", region="union:action:Tools|menu", pad=(80, 20, 0, 4),
                    menu=menu)]


def shot_after_save(c: Ctx):
    c.size(WIDE)
    win = c.win
    c.sh.choose_system("psx", only_missing=False)
    c.sh.select_game("Cinder Citadel")
    c.search()
    video = win.video_panel
    stream = next(i for i in range(video.combo.count()) if video.combo.itemData(i).kind == "hls")
    video.combo.setCurrentIndex(stream)
    c.sh.pump(lambda: video.thumb.has_image(), "the stream thumbnail")
    win.use_all_btn.click()
    win.review = lambda plan: not plan.is_empty                       # the review window is shown by its own picture
    win.do_save()
    c.sh.pump(lambda: "Saved" in win.status_label.text() and not win.jobs.running("save"), "the save to finish")
    c.sh.pump(lambda: win.video_panel._current is not None and not win.jobs.running("current"), "the new Current files")
    c.sh.pump(lambda: not win.jobs.running("rows"), "the refreshed list")
    c.sh.settle()
    games = capture(c.sh, win, c.exprs("after-save", 0), region="union:system_box|filter_box|row:Cinder Citadel", pad=(2, 26, 2, 6))
    buttons = capture(c.sh, win, c.exprs("after-save", 1), region="union:save_btn|undo_btn", pad=(1, 1, 1, 1))
    text_width = QtGui.QFontMetrics(win.status_label.font()).horizontalAdvance(win.status_label.text()) + 24
    status = capture(c.sh, win, c.exprs("after-save", 2), region="status_label", pad=(0, 3, 0, 3), width=text_width)
    return [games, buttons, status]


class _ScriptedDownload:
    """Stands in for the downloader of the demo so that a progress window can be photographed in a chosen state (the file it
    'installs' is a Batocera path in the picture; nothing is fetched or written)."""

    def __init__(self, tool, outcome):
        self.tool, self.outcome = tool, outcome
        self.at = (1_800_000, 3_200_000)

    def install(self, opener=None, progress=None, cancelled=None, stage=None):
        if self.outcome == "running":
            progress(*self.at)
            while not cancelled():                                   # waits for the picture, then for the cancel that ends it
                time.sleep(0.005)
            raise YouTubeCancelled("Cancelled")
        if self.outcome == "failed":
            try:
                raise OSError(8, "nodename nor servname provided, or not known")
            except OSError as exc:
                raise YouTubeError("Could not get the YouTube downloader (check the internet connection)") from exc
        progress(3_200_000, 3_200_000)
        return InstallResult(Path("/userdata/system/scrappy/bin/yt-dlp"), "2026.08.19", "", False)


def _task_dialog(c: Ctx, name: str, outcome: str, ready):
    """Open Tools > Install the YouTube downloader (scripted to `outcome`), wait until `ready(dialog)`, photograph the window,
    then close it the way a user would."""
    c.size(WIDE)
    tool = c.win.ctl.ytdlp
    saved = (tool.installed, tool.__dict__.get("install"))
    tool.installed = False                                           # so the window says "Install", not "Update"
    tool.install = _ScriptedDownload(tool, outcome).install
    try:
        c.win.install_ytdlp()
        dialog = c.win._task_dialog
        c.sh.pump(lambda: ready(dialog), f"the {name} window")
        if outcome == "failed":
            dialog.details_btn.click()
        c.sh.settle()
        frame = capture(c.sh, dialog, c.exprs(name))
        if dialog.state == "running":
            dialog.request_cancel()
            c.sh.pump(lambda: dialog.finished_state, "the end of the task")
        dialog.ok_btn.click()
        c.sh.settle()
    finally:
        tool.installed = saved[0]
        if saved[1] is None:
            tool.__dict__.pop("install", None)
        else:
            tool.install = saved[1]
    return [frame]


def shot_task_running(c: Ctx):
    return _task_dialog(c, "task-dialog-running", "running", lambda d: d.size_label.text() == "1.8 of 3.2 MB")


def shot_task_result(c: Ctx):
    return _task_dialog(c, "task-dialog-result", "ok", lambda d: d.state == "success")


def shot_task_failed(c: Ctx):
    return _task_dialog(c, "task-dialog-failed", "failed", lambda d: d.state == "failed")


DRIVERS = (("missing-list", shot_missing_list), ("slot-panel", shot_slot_panel), ("window-tour", shot_tour),
           ("trailer-panel", shot_trailer), ("details-form", shot_details), ("current-vs-new", shot_current_vs_new),
           ("review-dialog", shot_review), ("tools-menu", shot_tools_menu), ("task-dialog-running", shot_task_running),
           ("task-dialog-result", shot_task_result), ("task-dialog-failed", shot_task_failed), ("after-save", shot_after_save))


def run(out: Path) -> dict:
    """Make every picture into `out`. Returns {name: where the badges are}. Raises Failed when a step does not work."""
    out.mkdir(parents=True, exist_ok=True)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(["make_annotated_screenshots"])
    app.setStyle("Fusion")                                           # the same look on every machine
    ensure_dispatcher()
    env = build_demo(delay=False)
    win = MainWindow(env.controller)
    win.resize(*WIDE)
    win.show()
    shooter = Shooter(app, win, out)
    layout = {}
    try:
        shooter.pump(lambda: win.system_box.count() == 5, "the list of systems")
        ctx = Ctx(app, win, shooter)
        assert {name for name, _ in DRIVERS} == set(BY_NAME), "every image of annotated_spec needs a driver"
        for name, driver in DRIVERS:
            layout[name] = compose(BY_NAME[name], driver(ctx), out)
    finally:
        set_reporter(None)
        win.close()
        app.processEvents()
        env.cleanup()
    (out / "spec.json").write_text(json.dumps(export_spec(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return layout


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Make the annotated documentation screenshots from the demo library (headless).")
    parser.add_argument("--out", default=str(REPO / "docs" / "screenshots" / "annotated"), help="folder for the PNG files")
    parser.add_argument("--layout", action="store_true", help="also print where every badge was put (JSON)")
    args = parser.parse_args(argv)
    try:
        layout = run(Path(args.out))
    except Failed as exc:
        sys.stderr.write(f"make_annotated_screenshots: {exc}\n")
        return 1
    if args.layout:
        print(json.dumps(layout, indent=1))
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)                                                   # worker threads are daemons; do not wait for them
