"""Makes the screenshots for the documentation from the real window on a showcase library.

    python tools/make_screenshots.py [--out DIR]          (default: docs/screenshots)
    python tools/make_screenshots.py --real [--out DIR]   (real, famous games with their real artwork)

By default the library is the made-up one (``scrappy.ui.demo.build_demo``): nothing real is read or written and nothing touches the
network. With ``--real`` it is tools/real_showcase.py's library of 16 real games, whose artwork must have been downloaded
once into ``~/.cache/scrappy-demo-art`` by tools/fetch_demo_art.py (outside the repository; never commit it). From then on it
works offline. The six shots, their names and sizes and the way they are driven are the same in both modes.

It runs headless (QT_QPA_PLATFORM=offscreen, no display needed), builds the demo library (``scrappy.ui.demo.build_demo``) in a
temporary folder, opens the real main window at 1600x900, drives it the way the tests do and saves six PNGs:

    01-library.png        a system with the "Missing something" filter
    02-current.png        a partly finished game with the pictures it already has ("Current")
    03-search-results.png after a search: logo, box art, image, trailer and details found
    04-review-dialog.png  the Save review dialog (pictures, old and new details, replaced and kept files)
    05-saved.png          the window after a successful save
    06-trailer.png        the trailer panel with a clip chosen (start and end)

Exit code 0 when all six files were made, 1 on a failure (the message says which step; with --real also when the art cache is
incomplete), 2 when PyQt5 is not installed.
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(REPO))

try:
    from PyQt5 import QtCore, QtGui, QtWidgets
except ImportError as exc:                                           # a plain message instead of a traceback
    sys.stderr.write(f"make_screenshots: PyQt5 could not be loaded ({exc}). Install it (pip install PyQt5) and try again.\n")
    raise SystemExit(2)

from PIL import Image  # noqa: E402

from scrappy.providers.base import ProviderError  # noqa: E402
from scrappy.ui import main_window as mw  # noqa: E402
from scrappy.ui.demo import build_demo  # noqa: E402
from scrappy.ui.jobs import set_reporter  # noqa: E402
from scrappy.ui.main_window import MainWindow  # noqa: E402
from scrappy.ui.review import ReviewDialog  # noqa: E402
from scrappy.ui.workers import ensure_dispatcher  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))             # tools/real_showcase.py (loaded only for --real)

WINDOW = (1600, 900)
MAX_PNG_BYTES = 400_000
# Which game each shot is about, as (system, title): 1 = filtered list, 2 = "Current", 3 = search, 4 and 5 = review and save.
PLANS = {
    "invented": dict(first=("nes", "Marmalade Moon"), current=("megadrive", "Lantern Lagoon"), search=("megadrive", "Orbit Otters"),
                     review=("megadrive", "Lantern Lagoon"), systems=5, open_systems_in=None),
    # with eight systems, the "Current" shot also shows the open list of systems
    "real": dict(first=("nes", "The Legend of Zelda"), current=("switch", "Hades"), search=("windows", "Stardew Valley"),
                 review=("switch", "Hades"),
                 systems=8, open_systems_in=1),
}
NAMES = ("01-library.png", "02-current.png", "03-search-results.png", "04-review-dialog.png", "05-saved.png", "06-trailer.png")


class Failed(Exception):
    pass


class Shooter:
    def __init__(self, app, win, out: Path):
        self.app, self.win, self.out = app, win, out
        self.saved = []

    # ----- waiting
    def pump(self, until, what: str, seconds: float = 25.0) -> None:
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            if until():
                return
            time.sleep(0.01)
        self.app.processEvents()
        if not until():
            raise Failed(f"timed out waiting for {what}. Status line: {self.win.status_label.text()!r}")

    def settle(self, rounds: int = 8) -> None:
        for _ in range(rounds):
            self.app.processEvents()
            time.sleep(0.01)

    # ----- driving
    def choose_system(self, name: str, only_missing: bool) -> None:
        w = self.win
        w.filter_box.blockSignals(True)
        w.filter_box.setCurrentIndex(1 if only_missing else 0)
        w.filter_box.blockSignals(False)
        if w.system_box.currentText() == name:
            w.load_rows()
        else:
            w.system_box.setCurrentText(name)
        self.pump(lambda: w.system_box.currentText() == name and w.list.count() > 0 and not w.jobs.running("rows"),
                  f"the games of {name}")

    def select_game(self, title: str, system: str = "") -> None:
        w = self.win
        if system and w.system_box.currentText() != system:
            self.choose_system(system, only_missing=False)
        for i in range(w.list.count()):
            if title in w.list.item(i).text():
                w.list.setCurrentRow(i)
                self.pump(lambda: w.current is not None and not w.jobs.running("current"), f"what {title} has now")
                self.settle()
                return
        raise Failed(f"{title} is not in the list")

    def search(self) -> None:
        w = self.win
        w.do_search()
        self.pump(lambda: w.match_box.count() >= 1 and w.info is not None, "the search results")
        self.pump(lambda: not (w.logo_panel.busy or w.box_panel.busy or w.image_panel.busy), "the pictures")
        self.pump(lambda: w.video_panel.thumb.has_image(), "the trailer thumbnail")
        self.settle()

    def step_to_new(self, *panels) -> None:
        for panel in panels:
            if panel.shows_current:
                panel.step(1)
        self.pump(lambda: not any(p.busy for p in panels), "the new pictures")
        self.settle()

    # ----- saving the picture
    def grab(self, widget, name: str, popup_of=None) -> None:
        """Save a picture of `widget`. `popup_of`: a combo box of the window whose drop-down list is shown open on top."""
        self.settle()
        for edit in widget.findChildren(QtWidgets.QLineEdit):             # long texts show their beginning, not their end
            edit.setCursorPosition(0)
        pix = widget.grab()
        if popup_of is not None:
            popup_of.showPopup()
            self.settle(20)
            container = popup_of.view().window()
            at = popup_of.mapTo(widget, QtCore.QPoint(0, popup_of.height()))      # open below the box, as a drop-down list does
            painter = QtGui.QPainter(pix)
            painter.drawPixmap(at, container.grab())
            painter.end()
            popup_of.hidePopup()
            self.settle()
        buf = QtCore.QBuffer()
        buf.open(QtCore.QIODevice.WriteOnly)
        pix.toImage().save(buf, "PNG")
        im = Image.open(io.BytesIO(bytes(buf.data()))).convert("RGB")
        path = self.out / name
        im.save(path, "PNG", optimize=True)
        if path.stat().st_size > MAX_PNG_BYTES:                       # pixel-art pictures: 256 colours look the same
            im.quantize(256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).save(path, "PNG", optimize=True)
        self.saved.append(path)
        print(f"{name}: {im.width}x{im.height}, {path.stat().st_size // 1024} KB")


def run(out: Path, real: bool = False) -> int:
    plan = PLANS["real" if real else "invented"]
    if real:                                                         # fail before anything is made if the art is not there
        try:
            import real_showcase
            env = real_showcase.build_real_demo(delay=False)
        except ProviderError as exc:
            sys.stderr.write(f"make_screenshots: {exc}\n")
            return 1
    out.mkdir(parents=True, exist_ok=True)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(["make_screenshots"])
    app.setStyle("Fusion")                                           # the same look on every machine
    ensure_dispatcher()
    if not real:
        env = build_demo(delay=False)
    win = MainWindow(env.controller)
    win.resize(*WINDOW)
    win.show()
    shot = Shooter(app, win, out)
    try:
        shot.pump(lambda: win.system_box.count() == plan["systems"], "the list of systems")

        # 1. a system, filtered to what is still missing
        shot.choose_system(plan["first"][0], only_missing=True)
        shot.select_game(plan["first"][1])
        shot.grab(win, NAMES[0])

        # 2. a partly finished game shows its own pictures as "Current"
        shot.choose_system(plan["current"][0], only_missing=False)
        shot.select_game(plan["current"][1])
        shot.grab(win, NAMES[1], popup_of=win.system_box if plan["open_systems_in"] == 1 else None)

        # 3. a game with nothing yet: search, and everything found fills the panels
        shot.select_game(plan["search"][1], plan["search"][0])
        shot.search()
        shot.grab(win, NAMES[2])

        # 6. the trailer panel with a clip chosen
        video = win.video_panel
        youtube = next(i for i in range(video.combo.count()) if video.combo.itemData(i).kind == "youtube")
        video.combo.setCurrentIndex(youtube)
        shot.pump(lambda: video.thumb.has_image(), "the YouTube thumbnail")
        video.start.setValue(14.0)
        video.end.setValue(44.0)
        shot.grab(video, NAMES[5])

        # 4. the review before saving: new logo and image replace the old ones, the box art stays, a trailer is added
        shot.select_game(plan["review"][1], plan["review"][0])
        shot.search()
        shot.step_to_new(win.logo_panel, win.image_panel)
        stream = next(i for i in range(video.combo.count()) if video.combo.itemData(i).kind == "hls")
        video.combo.setCurrentIndex(stream)
        shot.pump(lambda: video.thumb.has_image(), "the stream thumbnail")
        win.use_all_btn.click()
        shot.settle()

        def review(plan):
            dialog = ReviewDialog(plan, win)
            dialog.resize(1000, 1600)
            dialog.show()
            shot.settle()
            body = dialog.scroll.widget()                                    # as tall as the dialog needs, so nothing scrolls
            height = 0
            for _ in range(6):                                               # (wrapped labels settle over a few layout passes)
                wanted = min(1300, body.layout().totalHeightForWidth(body.width()) + 200)
                if wanted == height:
                    break
                height = wanted
                dialog.resize(1000, height)
                shot.settle(20)
            shot.grab(dialog, NAMES[3])
            dialog.close()
            dialog.deleteLater()
            return not plan.is_empty

        win.review = review
        win.do_save()
        shot.pump(lambda: "Saved" in win.status_label.text() and not win.jobs.running("save"), "the save to finish")
        shot.pump(lambda: win.video_panel._current is not None and not win.jobs.running("current"), "the new Current files")
        shot.pump(lambda: not win.jobs.running("rows"), "the refreshed list")

        # 5. the status line after the save
        shot.grab(win, NAMES[4])
    except Failed as exc:
        sys.stderr.write(f"make_screenshots: {exc}\n")
        return 1
    finally:
        set_reporter(None)
        win.close()
        app.processEvents()
        env.cleanup()
    missing = [n for n in NAMES if not (out / n).is_file()]
    if missing:
        sys.stderr.write(f"make_screenshots: not made: {', '.join(missing)}\n")
        return 1
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Make the documentation screenshots from the demo library (headless).")
    parser.add_argument("--out", default=str(REPO / "docs" / "screenshots"), help="folder for the PNG files")
    parser.add_argument("--real", action="store_true",
                        help="use the real-titles library and the artwork cached by tools/fetch_demo_art.py (developer tool)")
    args = parser.parse_args(argv)
    return run(Path(args.out), real=args.real)


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)                                                   # worker threads are daemons; do not wait for them
