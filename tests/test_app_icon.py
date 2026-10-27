"""The program's icon: one PNG file that the window, the desktop shortcut and the Applications menu all use."""
import os
import shutil
from pathlib import Path

import pytest
from PIL import Image

from scrappy import app

REPO = Path(__file__).resolve().parents[1]


def test_the_icon_is_a_square_png_with_a_transparent_background():
    im = Image.open(REPO / "deploy" / "icon.png")
    assert im.format == "PNG" and im.size == (256, 256) and im.mode == "RGBA"
    assert im.getpixel((0, 0))[3] == 0                      # the corner is transparent: the mascot is not on a tile
    assert im.getpixel((128, 100))[3] == 255                # and the middle is solid


def test_the_logo_files_exist_in_both_themes():
    for name in ("logo.svg", "logo-dark.svg"):
        text = (REPO / "assets" / name).read_text(encoding="utf-8")
        assert text.startswith("<svg") and "Scrappy" in text


def test_a_checkout_finds_the_icon_in_deploy():
    assert app.app_icon_path() == REPO / "deploy" / "icon.png"


def test_an_installed_layout_finds_the_icon_next_to_the_program_folder(tmp_path, monkeypatch):
    (tmp_path / "scrappy").mkdir()
    shutil.copy(REPO / "deploy" / "icon.png", tmp_path / "icon.png")
    monkeypatch.setattr(app, "__file__", str(tmp_path / "scrappy" / "app.py"))
    assert app.app_icon_path() == tmp_path / "icon.png"


def test_a_missing_icon_file_is_not_an_error(tmp_path, monkeypatch):
    (tmp_path / "scrappy").mkdir()
    monkeypatch.setattr(app, "__file__", str(tmp_path / "scrappy" / "app.py"))
    assert app.app_icon_path() is None and app.app_icon() is None


def test_the_icon_loads_as_a_qt_icon():
    pytest.importorskip("PyQt5")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5 import QtGui, QtWidgets
    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    icon = app.app_icon()
    assert icon is not None and not icon.isNull()
    pixmap = icon.pixmap(64, 64)
    assert isinstance(pixmap, QtGui.QPixmap) and pixmap.width() > 0
    assert qapp is not None


def test_the_desktop_screenshot_tool_refuses_to_run_headless():
    pytest.importorskip("PyQt5")
    import subprocess
    import sys
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    done = subprocess.run([sys.executable, str(REPO / "tools" / "make_desktop_screenshot.py")], env=env, capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 2 and "real display" in done.stderr
