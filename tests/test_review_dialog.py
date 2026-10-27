"""The Save review dialog: what it lists, plain text only, Save / Cancel, and no decoding while it is built."""
import io
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt5.QtWidgets")
from PyQt5 import QtCore, QtGui, QtTest  # noqa: E402
from PIL import Image  # noqa: E402

from scrappy.controller import CurrentAssets  # noqa: E402
from scrappy.models import AssetCandidate, CurrentAsset  # noqa: E402
from scrappy.ui import widgets  # noqa: E402
from scrappy.ui.plan import PicturePick, VideoPick, build_plan  # noqa: E402
from scrappy.ui.review import ReviewDialog  # noqa: E402
from scrappy.ui.workers import ensure_dispatcher  # noqa: E402

SYS = "/roms/switch"


@pytest.fixture(scope="session")
def app():
    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication(["scrappy-tests"])
    ensure_dispatcher()
    return application


def qimage(w=64, h=36):
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), (200, 30, 30, 255)).save(buf, "PNG")
    return widgets.to_qimage(buf.getvalue())


def asset(slot, name=None, standard=True, linked=True):
    std = {"logo": "images/Game-marquee.png", "boxart": "images/Game-thumb.png", "image": "images/Game-image.jpg",
           "video": "videos/Game-video.mp4"}[slot]
    cand = AssetCandidate(slot=slot, url=f"current://{slot}", label="Current", source="current", current=True,
                          width=0 if slot == "video" else 640, height=0 if slot == "video" else 360,
                          duration=83.0 if slot == "video" else 0.0)
    return CurrentAsset(cand, f"{SYS}/images/{name}" if name else f"{SYS}/{std}", f"{SYS}/{std}", standard, 54_000,
                        duration=cand.duration, tag_linked=linked)


def full_plan(**kw):
    new = AssetCandidate(slot="image", url="https://x/a.png", label="Screenshot 2", source="Steam")
    trailer = AssetCandidate(slot="video", url="https://x/t.m3u8", label="Launch trailer", source="Steam", kind="hls", duration=100.0)
    cur = CurrentAssets(
        fields={"name": "Cinder Citadel", "desc": "Old text.", "releasedate": "20200102T000000", "developer": "", "publisher": "",
                "genre": "", "players": ""},
        pictures={"logo": None, "boxart": asset("boxart", "my own art.png", standard=False), "image": asset("image")},
        video=asset("video"))
    picks = [PicturePick("image", True, new, b"\xff\xd8\xffjpg", "640×360 JPG (from 1920×1080)", "", qimage()),
             PicturePick("boxart", True, AssetCandidate(slot="boxart", url="u", label="Box", source="Steam"), b"\x89PNGx",
                         "396×640 PNG", "", qimage(40, 64)),
             PicturePick("logo", False, new, b"x", "", "", qimage())]
    return build_plan("Cinder Citadel II", SYS, cur, picks, VideoPick(True, trailer, 5.0, 65.0, qimage()),
                      {"name": "Cinder Citadel", "desc": "New text.", "releasedate": "20260218T000000", "genre": "Roguelike"},
                      current_images={"image": qimage(), "boxart": qimage(40, 64)}, **kw)


def texts(dialog):
    return [label.text() for label in dialog.findChildren(QtWidgets.QLabel)]


def blob(dialog):
    return "\n".join(texts(dialog))


# ---------------------------------------------------------------- content

def test_dialog_lists_pictures_trailer_text_replaced_and_left_alone(app):
    d = ReviewDialog(full_plan())
    text = blob(d)
    assert "Cinder Citadel II" in text
    for needle in ("Image", "Box art", "Screenshot 2", "Steam", "640×360 JPG (from 1920×1080)",         # picture: slot, label, source, size
                   "Trailer", "Launch trailer", "0:05", "1:05", "1:00 (60 s)", "1:23",                    # trailer: range, length, current length
                   "Description", "Old text.", "New text.", "2020-01-02", "2026-02-18", "Roguelike",      # text old and new
                   "images/Game-image.jpg", "videos/Game-video.mp4",                                       # replaced files
                   "images/my own art.png",                                                                 # no longer used, stays on disk
                   "Logo"):                                                                                 # left alone (Include unticked)
        assert needle in text, needle
    assert "Include is unticked" in text
    assert d.windowTitle() == "Review before saving"
    d.deleteLater()


def test_unchanged_text_fields_are_not_listed(app):
    text = blob(ReviewDialog(full_plan()))
    assert [t.tag for t in full_plan().texts] == ["desc", "releasedate", "genre"]            # the title did not change
    assert "Title" not in text and "Developer" not in text and "Players" not in text


def test_old_and_new_pictures_are_side_by_side_when_replacing(app):
    d = ReviewDialog(full_plan())
    previews = [w for w in d.findChildren(widgets.CheckerLabel) if w.has_image()]
    # image: old + new, box art: old + new, trailer: thumbnail
    assert len(previews) == 5
    d.deleteLater()


def test_a_new_slot_shows_only_the_new_picture(app):
    new = AssetCandidate(slot="logo", url="u", label="Logo A", source="Steam")
    cur = CurrentAssets(fields={t: "" for t in ("name", "desc", "releasedate", "developer", "publisher", "genre", "players")},
                        pictures={"logo": None, "boxart": None, "image": None})
    plan = build_plan("G", SYS, cur, [PicturePick("logo", True, new, b"x", "600×300 PNG", "", qimage())], None, {})
    d = ReviewDialog(plan)
    assert len([w for w in d.findChildren(widgets.CheckerLabel) if w.has_image()]) == 1
    assert "No existing file will be replaced" in blob(d)
    d.deleteLater()


def test_warnings_are_shown(app):
    new = AssetCandidate(slot="logo", url="u", label="Logo A", source="Steam")
    cur = CurrentAssets(fields={t: "" for t in ("name", "desc", "releasedate", "developer", "publisher", "genre", "players")},
                        pictures={"logo": None, "boxart": None, "image": None})
    plan = build_plan("G", SYS, cur, [PicturePick("logo", True, new, b"x", "600×300 PNG", "this picture has a solid background",
                                                  qimage())], None, {})
    assert "this picture has a solid background" in blob(ReviewDialog(plan))


# ---------------------------------------------------------------- plain text only

HOSTILE = "<img src='file:///etc/hosts'><b>bold</b> & <a href='http://evil'>link</a>"


def hostile_plan():
    cand = AssetCandidate(slot="image", url="u", label=HOSTILE, source=HOSTILE)
    cur = CurrentAssets(fields={"name": HOSTILE, "desc": HOSTILE, "releasedate": "", "developer": "", "publisher": "", "genre": "",
                                "players": ""},
                        pictures={"logo": None, "boxart": None, "image": asset("image", HOSTILE + ".jpg", standard=False)},
                        video=None)
    trailer = AssetCandidate(slot="video", url="u", label=HOSTILE, source=HOSTILE, kind="hls")
    return build_plan(HOSTILE, SYS, cur, [PicturePick("image", True, cand, b"x", HOSTILE, HOSTILE, qimage())],
                      VideoPick(True, trailer, 0.0, 30.0), {"name": HOSTILE + "2", "desc": HOSTILE + "<h1>x</h1>"})


def test_hostile_text_stays_plain(app):
    d = ReviewDialog(hostile_plan())
    labels = d.findChildren(QtWidgets.QLabel)
    assert labels
    for label in labels:
        assert label.textFormat() == QtCore.Qt.PlainText, label.text()
        assert label.openExternalLinks() is False
    text = blob(d)
    assert HOSTILE in text and text.count(HOSTILE) >= 5                      # shown literally, many times
    assert not d.findChildren((QtWidgets.QTextBrowser, QtWidgets.QTextEdit))  # no rich text widget at all
    for plain in d.findChildren(QtWidgets.QPlainTextEdit):
        assert plain.isReadOnly()
    for w in d.findChildren(QtWidgets.QWidget):
        assert "<img" not in w.toolTip() and "<a " not in w.toolTip()                      # a tooltip would be rich text
    d.deleteLater()


def test_very_long_text_is_shortened_on_screen_but_not_in_the_plan(app):
    long = "word " * 2000
    cur = CurrentAssets(fields={"name": "", "desc": long, "releasedate": "", "developer": "", "publisher": "", "genre": "",
                                "players": ""}, pictures={"logo": None, "boxart": None, "image": None})
    plan = build_plan("G", SYS, cur, [], None, {"desc": long + "new"})
    d = ReviewDialog(plan)
    assert max(len(t) for t in texts(d)) < 1500
    assert plan.texts[0].new.endswith("new") and len(plan.texts[0].new) > 9000
    d.deleteLater()


# ---------------------------------------------------------------- buttons

EMPTY_FIELDS = {t: "" for t in ("name", "desc", "releasedate", "developer", "publisher", "genre", "players")}


def adding_plan():
    """A plan that only adds a new picture: nothing existing is replaced."""
    new = AssetCandidate(slot="logo", url="u", label="Logo A", source="Steam")
    cur = CurrentAssets(fields=dict(EMPTY_FIELDS), pictures={"logo": None, "boxart": None, "image": None})
    return build_plan("G", SYS, cur, [PicturePick("logo", True, new, b"x", "600×300 PNG", "", qimage())], None, {})


def test_save_is_the_default_when_nothing_is_replaced(app):
    d = ReviewDialog(adding_plan())
    assert d.save_btn.text() == "Save" and d.save_btn.isDefault() and d.cancel_btn.text() == "Cancel"
    assert not d.cancel_btn.isDefault()
    d.show()
    assert d.focusWidget() is d.save_btn
    d.deleteLater()


def test_cancel_is_the_default_when_files_will_be_replaced(app):
    plan = full_plan()
    assert plan.replaced_files
    d = ReviewDialog(plan)
    assert d.cancel_btn.isDefault() and not d.save_btn.isDefault() and not d.save_btn.autoDefault()
    d.show()
    assert d.focusWidget() is d.cancel_btn
    QtTest.QTest.keyClick(d.cancel_btn, QtCore.Qt.Key_Return)                   # a stray Enter cancels, it does not overwrite
    assert not d.isVisible() and d.result() == QtWidgets.QDialog.Rejected
    d = ReviewDialog(plan)
    d.show()
    d.save_btn.click()                                                          # Save is still one click away
    assert d.result() == QtWidgets.QDialog.Accepted


def key_event(key, repeat):
    return QtGui.QKeyEvent(QtCore.QEvent.KeyPress, key, QtCore.Qt.NoModifier, "", repeat, 1)


def test_key_repeat_is_ignored_right_after_the_dialog_opens(app):
    d = ReviewDialog(adding_plan())
    d.show()
    QtWidgets.QApplication.sendEvent(d.save_btn, key_event(QtCore.Qt.Key_Return, True))      # a held-down Enter from the main window
    QtWidgets.QApplication.sendEvent(d, key_event(QtCore.Qt.Key_Return, True))
    assert d.isVisible()
    QtWidgets.QApplication.sendEvent(d.save_btn, key_event(QtCore.Qt.Key_Return, False))     # a fresh press works at once
    assert not d.isVisible() and d.result() == QtWidgets.QDialog.Accepted


def test_key_repeat_works_again_after_the_guard_time(app, monkeypatch):
    monkeypatch.setattr(ReviewDialog, "REPEAT_GUARD_MS", 0)
    d = ReviewDialog(adding_plan())
    d.show()
    QtWidgets.QApplication.sendEvent(d.save_btn, key_event(QtCore.Qt.Key_Return, True))
    assert not d.isVisible()


def test_the_top_line_sums_up_the_plan(app):
    plan = full_plan()
    d = ReviewDialog(plan)
    assert plan.summary() in texts(d) and plan.summary()
    assert any(t.startswith("Check what will be saved") for t in texts(d))


# ---------------------------------------------------------------- existing files that only get linked

def linking_plan():
    cur = CurrentAssets(fields=dict(EMPTY_FIELDS), pictures={"logo": asset("logo", linked=False), "boxart": None, "image": None},
                        video=asset("video", linked=False))
    shown = AssetCandidate(slot="logo", url="current://logo", label="Current", source="current", current=True)
    cand = AssetCandidate(slot="video", url="current://video", label="Current", source="current", current=True)
    return build_plan("G", SYS, cur, [PicturePick("logo", True, shown, None, "", "", qimage())], VideoPick(True, cand), {},
                      current_images={"logo": qimage()})


def test_files_that_only_get_linked_have_their_own_heading(app):
    d = ReviewDialog(linking_plan())
    text = blob(d)
    assert "Will be linked" in text and "images/Game-marquee.png" in text and "videos/Game-video.mp4" in text
    assert "not in the game list" in text and "not changed" in text
    assert d.save_btn is not None and "Nothing would be written" not in text
    assert "No existing file will be replaced" in text                          # linking replaces nothing
    assert len([w for w in d.findChildren(widgets.CheckerLabel) if w.has_image()]) == 1
    d.deleteLater()


def test_save_accepts_cancel_and_escape_reject(app):
    d = ReviewDialog(full_plan())
    d.save_btn.click()
    assert d.result() == QtWidgets.QDialog.Accepted
    d = ReviewDialog(full_plan())
    d.cancel_btn.click()
    assert d.result() == QtWidgets.QDialog.Rejected
    d = ReviewDialog(full_plan())
    d.show()
    QtTest.QTest.keyClick(d, QtCore.Qt.Key_Escape)
    assert d.result() == QtWidgets.QDialog.Rejected
    d = ReviewDialog(adding_plan())
    d.show()
    QtTest.QTest.keyClick(d.save_btn, QtCore.Qt.Key_Return)                    # Enter on the default button saves
    assert d.result() == QtWidgets.QDialog.Accepted


def test_an_empty_plan_says_so_and_offers_no_save(app):
    cur = CurrentAssets(fields={t: "x" for t in ("name", "desc", "releasedate", "developer", "publisher", "genre", "players")},
                        pictures={"logo": asset("logo"), "boxart": None, "image": None})
    shown = AssetCandidate(slot="logo", url="current://logo", label="Current", source="current", current=True)
    plan = build_plan("G", SYS, cur, [PicturePick("logo", True, shown, None, "", "", qimage())], None, {"name": "x"})
    assert plan.is_empty
    d = ReviewDialog(plan)
    assert d.save_btn is None
    assert "Nothing would be written" in blob(d) and "Showing Current" in blob(d)
    assert [b.text() for b in d.findChildren(QtWidgets.QPushButton)] == ["Close"]
    d.show()
    QtTest.QTest.keyClick(d, QtCore.Qt.Key_Escape)
    assert d.result() == QtWidgets.QDialog.Rejected


# ---------------------------------------------------------------- never decodes in the constructor, fits small screens

def test_constructing_the_dialog_decodes_nothing(app, monkeypatch):
    plan = full_plan()                                                          # (the QImages exist already: the loaders made them)

    def boom(*a, **k):
        raise AssertionError("the dialog decoded a picture")

    monkeypatch.setattr(widgets, "to_qimage", boom)
    monkeypatch.setattr("scrappy.ui.pipeline.decode_rgba", boom)
    monkeypatch.setattr(Image, "open", boom)
    monkeypatch.setattr("scrappy.processing.images.open_image", boom)
    d = ReviewDialog(plan)
    d.show()
    app.processEvents()
    d.deleteLater()


def test_the_dialog_is_small_enough_for_a_laptop_screen(app):
    d = ReviewDialog(full_plan())
    hint = d.minimumSizeHint()
    assert hint.height() <= 560 and hint.width() <= 900, hint
    d.show()
    assert d.width() <= 1100 and d.height() <= 800
    d.deleteLater()
