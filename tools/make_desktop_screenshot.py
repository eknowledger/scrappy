"""One screenshot of the window on a real desktop, with its title bar and icon (the other six are the window's contents only).

    DISPLAY=:0 python tools/make_desktop_screenshot.py [--real] [--out FILE]

Needs a real display with a window manager, so that the frame is drawn: on the Batocera-like test VM (Xvfb + openbox, see the
batocera-sim project) or on any Linux desktop. It refuses to run headless (QT_QPA_PLATFORM=offscreen has no title bar to show).
It opens the real window the way ``scrappy.app`` does (the icon and the desktop file name included), drives it to the search
result of one game as tools/make_screenshots.py does, and saves the window with its frame, cropped to it.

``--real`` uses the real-title library (artwork cache in ``~/.cache/scrappy-demo-art``, see tools/fetch_demo_art.py), the default
is the made-up library. Exit code 0 on success, 1 on a failure, 2 when PyQt5 or a display is missing.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
os.environ.setdefault("QT_QPA_PLATFORM", "xcb")                      # make_screenshots.py would default to offscreen
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from PyQt5 import QtCore, QtWidgets
except ImportError as exc:
    sys.stderr.write(f"make_desktop_screenshot: PyQt5 could not be loaded ({exc}).\n")
    raise SystemExit(2)

from PIL import Image  # noqa: E402

import make_screenshots as ms  # noqa: E402
from scrappy.app import app_icon  # noqa: E402
from scrappy.providers.base import ProviderError  # noqa: E402
from scrappy.ui.demo import build_demo  # noqa: E402
from scrappy.ui.jobs import set_reporter  # noqa: E402
from scrappy.ui.main_window import MainWindow  # noqa: E402
from scrappy.ui.workers import ensure_dispatcher  # noqa: E402

DEFAULT_OUT = REPO / "docs" / "screenshots" / "07-desktop.png"
WINDOW = (1480, 840)                                                 # leaves room for the frame on a 1600x900 screen
GAMES = {"real": ("windows", "Stardew Valley"), "invented": ("megadrive", "Orbit Otters")}
MARGIN = 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Screenshot of the Scrappy window on a real desktop")
    parser.add_argument("--real", action="store_true", help="the real-title library (needs the artwork cache)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"the PNG to write (default: {DEFAULT_OUT.relative_to(REPO)})")
    args = parser.parse_args(argv)
    if args.real:
        try:
            import real_showcase
            env = real_showcase.build_real_demo(delay=False)
        except ProviderError as exc:
            sys.stderr.write(f"make_desktop_screenshot: {exc}\n")
            return 1
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(["scrappy"])
    if app.platformName() in ("offscreen", "minimal"):
        sys.stderr.write("make_desktop_screenshot: this needs a real display and a window manager (the platform is "
                         f"{app.platformName()!r}). Set DISPLAY, or use tools/make_screenshots.py for headless shots.\n")
        return 2
    app.setApplicationName("Scrappy")
    app.setDesktopFileName("scrappy")
    icon = app_icon()
    if icon is not None:
        app.setWindowIcon(icon)
    ensure_dispatcher()
    if not args.real:
        env = build_demo(delay=False)
    win = MainWindow(env.controller)
    win.resize(*WINDOW)
    win.show()
    shot = ms.Shooter(app, win, args.out.parent)
    try:
        system, title = GAMES["real" if args.real else "invented"]
        shot.pump(lambda: win.system_box.count() > 1, "the list of systems")
        shot.choose_system(system, only_missing=False)
        shot.select_game(title, system)
        shot.search()
        for _ in range(60):                                          # let the window manager draw the frame
            app.processEvents()
            time.sleep(0.02)
        frame = win.frameGeometry()
        pixmap = app.primaryScreen().grabWindow(0, frame.x() - MARGIN, frame.y() - MARGIN,
                                                frame.width() + 2 * MARGIN, frame.height() + 2 * MARGIN)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        if not pixmap.save(str(args.out), "PNG"):
            raise ms.Failed(f"could not write {args.out}")
        Image.open(args.out).convert("RGB").save(args.out, optimize=True)
        print(f"wrote {args.out} ({pixmap.width()}x{pixmap.height()})")
    except ms.Failed as exc:
        sys.stderr.write(f"make_desktop_screenshot: {exc}\n")
        return 1
    finally:
        set_reporter(None)
        win.close()
        app.processEvents()
        env.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
