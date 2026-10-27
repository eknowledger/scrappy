"""python -m scrappy.app            the real program
python -m scrappy.app --demo     the same window on a made-up library and made-up sources (nothing real is touched)

This module imports no Qt at the top, so a missing PyQt5 or a missing screen ends in a plain message (stderr, the log, and a
dialog through zenity/xmessage when one exists) instead of a Python traceback.
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

log = logging.getLogger("scrappy")


def setup_logging(log_dir=None) -> None:
    """Log to stderr, and to a rotating file in the data folder when it can be written (the desktop shortcut hides stderr)."""
    handlers = [logging.StreamHandler()]
    if log_dir is not None:
        try:
            Path(log_dir).mkdir(parents=True, exist_ok=True)
            handlers.append(logging.handlers.RotatingFileHandler(Path(log_dir) / "scrappy.log", maxBytes=500_000, backupCount=2,
                                                                 encoding="utf-8"))
        except OSError:
            pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers, force=True)


def install_excepthooks() -> None:
    """PyQt5 aborts the whole process when an exception escapes a slot and sys.excepthook is the default one.
    With our own hook the error is logged and shown in the status line and the program carries on."""
    from .ui.jobs import report_exception

    def hook(exc_type, exc, tb):
        exc.__traceback__ = tb
        report_exception(exc, "the window")

    def thread_hook(args):
        if args.exc_value is not None:
            log.error("Unexpected error in thread %s", getattr(args.thread, "name", "?"),
                      exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = hook
    threading.excepthook = thread_hook


def display_problem(env=None, platform=None) -> str:
    """'' if a window can be opened; otherwise a plain sentence (Linux needs an X or Wayland display or an explicit Qt platform)."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if not platform.startswith("linux") or env.get("DISPLAY") or env.get("WAYLAND_DISPLAY") or env.get("QT_QPA_PLATFORM"):
        return ""
    return ("There is no screen to show the window on (DISPLAY is not set). Start Scrappy from the Batocera desktop "
            "(Applications menu or the desktop icon), not from a text-only session.")


def fatal_dialog(text: str) -> None:
    """Show `text` in a small dialog if zenity or xmessage exists. Never raises."""
    try:
        zenity, xmessage = shutil.which("zenity"), shutil.which("xmessage")
        if zenity:
            subprocess.run([zenity, "--error", "--title=Scrappy", "--no-markup", f"--text={text}"], timeout=120,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        elif xmessage:
            subprocess.run([xmessage, "-center", text], timeout=120, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)
    except Exception:                                          # noqa: BLE001 - this is the last resort; the text is on stderr anyway
        pass


def _fail(text: str) -> int:
    print("Scrappy: " + text, file=sys.stderr)
    log.error(text)
    fatal_dialog(text)
    return 1


def app_icon_path():
    """The program's icon file: icon.png next to the program folder when installed, deploy/icon.png in a checkout."""
    here = Path(__file__).resolve().parent.parent
    for candidate in (here / "icon.png", here / "deploy" / "icon.png"):
        if candidate.is_file():
            return candidate
    return None


def app_icon():
    """The window icon (a QIcon), or None if the file is not there; a missing icon must never stop the program."""
    path = app_icon_path()
    if path is None:
        return None
    from PyQt5 import QtGui
    icon = QtGui.QIcon(str(path))
    return None if icon.isNull() else icon


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="scrappy.app", description="Scrappy")
    parser.add_argument("--demo", action="store_true", help="try the program on a made-up library (nothing real is touched)")
    return parser.parse_known_args(argv)


def main(argv=None) -> int:
    args, qt_args = parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        from PyQt5 import QtCore, QtWidgets
    except ImportError as exc:
        return _fail(f"PyQt5 could not be loaded ({exc}). On the Batocera it comes with the "
                     "system Python, so start the program with its launcher (scrappy.sh) or /usr/bin/python3.")
    problem = display_problem()
    if problem:
        return _fail(problem)
    from .config import load_config
    from .controller import Controller
    from .providers.base import describe_error
    from .ui.main_window import MainWindow
    from .ui.widgets import show_message

    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
    app = QtWidgets.QApplication([sys.argv[0] if sys.argv else "scrappy"] + qt_args)
    app.setApplicationName("Scrappy")
    app.setDesktopFileName("scrappy")                      # ties the window to scrappy.desktop (its icon and name in the shell)
    icon = app_icon()
    if icon is not None:
        app.setWindowIcon(icon)
    install_excepthooks()
    env = None
    if args.demo:
        from .ui.demo import build_demo
        setup_logging()
        env = build_demo()
        controller, notices = env.controller, ["Demo mode: a made-up library and made-up sources. Nothing real is read or changed."]
    else:
        cfg = load_config()                                 # never raises: a bad config.json gives defaults + cfg.warnings
        setup_logging(Path(cfg.get("data_dir", "")) / "logs" if cfg.get("data_dir") else None)
        try:
            controller = Controller(cfg)
        except Exception as exc:                            # noqa: BLE001 - e.g. the data folder cannot be created
            log.exception("could not start")
            show_message(None, "Scrappy", describe_error(exc), error=True)
            return 1
        notices = list(cfg.warnings)
    try:
        win = MainWindow(controller, notices=notices)
        win.show()
        return app.exec_()
    finally:
        if env is not None:
            env.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
