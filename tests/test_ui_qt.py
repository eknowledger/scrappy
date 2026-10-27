"""The window itself, driven headless (QT_QPA_PLATFORM=offscreen) on the made-up library of scrappy.ui.demo.

Skipped where PyQt5 is not installed. Everything slow in the window runs in worker threads, so the tests pump the Qt
event loop until the expected state shows up.
"""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt5.QtWidgets")
from PyQt5 import QtCore, QtGui  # noqa: E402

from scrappy.models import AssetCandidate  # noqa: E402
from scrappy.saver import ES_RESTART_HINT  # noqa: E402
from scrappy.ui import main_window as mw  # noqa: E402
from scrappy.ui import widgets  # noqa: E402
from scrappy.ui.demo import HLS_URL, SAMPLE_ROM, build_minimal_demo as build_demo, demo_picture  # noqa: E402
from scrappy.ui.jobs import set_reporter  # noqa: E402
from scrappy.ui.main_window import MainWindow  # noqa: E402
from scrappy.ui.pipeline import Processed  # noqa: E402
from scrappy.ui.workers import ensure_dispatcher, new_group  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def app():
    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication(["scrappy-tests"])
    ensure_dispatcher()
    return application


def pump(app, until, seconds=10.0):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        if until():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(until())


@pytest.fixture(autouse=True)
def no_surprise_dialogs(monkeypatch):
    """A dialog nobody expects would block the test forever; make it a failure instead."""
    def refuse(*args, **kwargs):
        raise AssertionError("an unexpected dialog was opened")

    monkeypatch.setattr(mw, "ask_choice", refuse)
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec_", refuse)
    monkeypatch.setattr(QtWidgets.QDialog, "exec_", refuse)                    # the review dialog too: tests must opt in
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText", staticmethod(refuse))
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName", staticmethod(refuse))


@pytest.fixture
def env(tmp_path):
    e = build_demo(tmp_path / "demo")
    yield e
    e.cleanup()


@pytest.fixture
def win(app, env):
    w = MainWindow(env.controller)
    w.show()
    assert pump(app, lambda: w.system_box.count() == 2 and w.list.count() == 1)         # snes comes first
    w.system_box.setCurrentText("switch")
    assert pump(app, lambda: w.list.count() == 3)
    yield w
    w.close()
    app.processEvents()
    set_reporter(None)


def pick(app, w, name):
    for i in range(w.list.count()):
        if name in w.list.item(i).text():
            w.list.setCurrentRow(i)
            return
    raise AssertionError(f"{name} is not in the list")


def search(app, w, name="Tidepool"):
    pick(app, w, name)
    w.do_search()
    assert pump(app, lambda: w.match_box.count() >= 1 and w.info is not None), w.status_label.text()
    assert pump(app, lambda: not (w.logo_panel.busy or w.box_panel.busy or w.image_panel.busy))


def ready(app, w):
    assert pump(app, lambda: w.logo_panel.out and w.box_panel.out and w.image_panel.out)


def current_loaded(app, w):
    assert pump(app, lambda: w.current is not None and not w.jobs.running("current")), w.status_label.text()


def show_new(app, w):
    """Step every picture panel that shows Current to its first new candidate and wait for the pictures."""
    for panel in (w.logo_panel, w.box_panel, w.image_panel):
        if panel.shows_current:
            panel.step(1)
    assert pump(app, lambda: w.logo_panel.out and w.box_panel.out and w.image_panel.out), w.status_label.text()


def confirm(monkeypatch, answer=True):
    """Stands in for the review dialog: records the dialogs that were opened and presses Save (True) or Cancel (False)."""
    shown = []

    def fake(self):
        shown.append(self)
        return QtWidgets.QDialog.Accepted if answer else QtWidgets.QDialog.Rejected

    monkeypatch.setattr(QtWidgets.QDialog, "exec_", fake)
    return shown


# ---------------------------------------------------------------- layout and plain text

def test_window_opens_with_status_and_fits_the_screen(app, win):
    hint = win.minimumSizeHint()                                               # must fit a 1366 x 768 laptop screen
    assert hint.width() <= 1250 and hint.height() <= 730, hint
    assert win.status_label.isVisibleTo(win) and win.status_label.text()
    assert win.search_btn.isEnabled() is False and win.save_btn.isEnabled() is False      # no game chosen yet
    assert win.undo_btn.isEnabled()                                                       # undo is always available


def test_every_label_that_can_show_web_text_is_plain(app, win):
    for label in (win.status_label, win.notice_label, win.logo_panel.info, win.box_panel.info, win.image_panel.info,
                  win.video_panel.note, win.video_panel.summary):
        assert label.textFormat() == QtCore.Qt.PlainText, label.objectName()
    for label in win.findChildren(QtWidgets.QLabel):
        assert label.textFormat() != QtCore.Qt.RichText


def test_html_from_the_web_is_shown_literally(app, win, env):
    ctl = env.controller
    evil = "<img src=x onerror=alert(1)><b>Bold</b>"
    real = ctl.load_info

    def load_info(match):
        info = real(match)
        info.name = evil
        for a in info.assets:
            a.label = evil
        return info

    ctl.load_info = load_info
    search(app, win)
    assert win.name_edit.text() == evil
    assert evil in win.image_panel.info.text() or evil in win.logo_panel.info.text()
    win.status(evil)
    assert win.status_label.text() == evil and win.status_label.textFormat() == QtCore.Qt.PlainText


def test_message_boxes_are_plain_text(app, monkeypatch):
    seen = {}

    def fake_exec(self):
        seen["format"] = self.textFormat()
        seen["text"] = self.text()
        return 0

    monkeypatch.setattr(QtWidgets.QMessageBox, "exec_", fake_exec)
    result = widgets.ask_choice(None, "t", "<b>hi</b>", (("yes", "Yes", "accept"), ("no", "No", "reject")), "no")
    assert seen["format"] == QtCore.Qt.PlainText and result == "no"       # closed without a click: the 'reject' answer


def test_tab_order_follows_the_screen(app, win):
    chain, w = [], win.system_box
    for _ in range(300):
        chain.append(w)
        w = w.nextInFocusChain()
        if w is win.system_box:
            break
    order = [win.system_box, win.filter_box, win.list, win.search_edit, win.search_btn, win.match_box,
             win.logo_panel.use_box, win.video_panel.combo, win.name_edit, win.desc_edit, win.save_btn, win.undo_btn]
    positions = [chain.index(x) for x in order]
    assert positions == sorted(positions)


# ---------------------------------------------------------------- the checkerboard

def test_checkerboard_shows_through_transparent_pixels(app):
    label = widgets.CheckerLabel((100, 60))
    label.resize(240, 160)
    image = QtGui.QImage(120, 60, QtGui.QImage.Format_ARGB32)
    image.fill(QtGui.QColor(0, 0, 0, 0))                                   # fully transparent
    label.set_image(image)
    label.show()
    app.processEvents()
    shot = label.grab().toImage()
    r = label.image_rect()
    assert r.width() > 0
    light = QtGui.QColor(shot.pixel(r.x() + 2, r.y() + 2))
    dark = QtGui.QColor(shot.pixel(r.x() + 2 + 12, r.y() + 2))
    assert (light.red(), light.green()) == (205, 205) and (dark.red(), dark.green()) == (160, 160)
    # an opaque pixel hides it
    image.fill(QtGui.QColor(255, 0, 0, 255))
    label.set_image(image)
    app.processEvents()
    assert QtGui.QColor(label.grab().toImage().pixel(r.x() + 2, r.y() + 2)).red() == 255
    label.set_image(None)
    assert not label.has_image()
    label.close()


# ---------------------------------------------------------------- the review and save flow

def test_review_flow_fills_every_panel_and_saves(app, win, env, monkeypatch):
    sw = env.root / "roms" / "switch"
    shown = confirm(monkeypatch)
    search(app, win)
    assert win.search_edit.text() == "Tidepool Tactics"
    assert win.name_edit.text() == "Tidepool Tactics (demo)" and win.date_edit.text() == "2026-02-18"
    ready(app, win)
    assert win.logo_panel.candidates[0].transparent                            # ranked: the transparent logo first
    assert win.video_panel.combo.count() == 2                                  # Steam trailer + YouTube video
    # memory safety: previews are the processed, theme-sized results, never the 1920x1080 sources
    for panel, limit in ((win.image_panel, (640, 360)), (win.box_panel, (396, 640)), (win.logo_panel, (600, 300))):
        pm = panel.preview._pixmap
        assert pm.width() <= limit[0] and pm.height() <= limit[1]
    plan = win.make_plan()
    req = win.request_for(plan)
    assert req.image_jpg and req.thumb_png and req.marquee_png and req.fields["releasedate"] == "20260218T000000"
    win.do_save()
    assert len(shown) == 1 and not shown[0].plan.is_empty and shown[0].plan.video is not None      # reviewed before saving
    assert not win.save_btn.isEnabled()                                        # busy: no double save
    assert pump(app, lambda: win.save_btn.isEnabled() and "Saved" in win.status_label.text(), 20), win.status_label.text()
    assert (sw / "images" / f"{Path(SAMPLE_ROM).stem}-image.jpg").stat().st_size > 1000
    assert (sw / "videos" / f"{Path(SAMPLE_ROM).stem}-video.mp4").exists()
    assert "<name>Tidepool Tactics (demo)</name>" in (sw / "gamelist.xml").read_text()
    assert pump(app, lambda: "Tidepool" in win.list.item(win.list.currentRow()).text() and "✓" in win.list.item(win.list.currentRow()).text())
    assert win.current_row.complete                                            # the list was refreshed; the review stays on screen
    assert win.name_edit.text()


def test_text_logo_replaces_the_logo(app, win):
    search(app, win)
    ready(app, win)
    before = win.logo_panel.out
    win.name_edit.setText("   ")
    win.make_text_logo()
    assert pump(app, lambda: win.logo_panel.out is not None and win.logo_panel.out != before)
    assert "generated" in win.logo_panel.info.text()
    win.logo_panel.step(1)                                                     # stepping goes back to the candidates
    assert pump(app, lambda: win.logo_panel.out is not None and "Demo" in win.logo_panel.info.text())


def test_remove_background_checkbox_reprocesses_in_a_worker(app, win):
    search(app, win)
    ready(app, win)
    win.logo_panel.step(1)                                                     # the solid-colour logo
    assert pump(app, lambda: "⚠" in win.logo_panel.info.text())
    win.logo_panel.remove_bg.setChecked(True)
    assert pump(app, lambda: "background removed" in win.logo_panel.info.text())


def test_a_failing_picture_is_explained_and_does_not_block(app, win):
    search(app, win)
    ready(app, win)
    win.image_panel.step(-1)                                                    # ranked last: the picture that fails
    assert pump(app, lambda: "Could not use this one" in win.image_panel.info.text())
    assert win.image_panel.current_output() is None


def test_nothing_to_save_says_so_and_offers_no_save(app, win, monkeypatch):
    shown = confirm(monkeypatch)
    pick(app, win, "Tidepool")
    current_loaded(app, win)
    win.do_save()
    assert len(shown) == 1 and shown[0].plan.is_empty and shown[0].save_btn is None
    assert "Nothing to save" in win.status_label.text() and not win.jobs.running("save")


# ---------------------------------------------------------------- the date

@pytest.mark.parametrize("text", ["18.02.2026", "02/08/2026", "Feb 18th, 2026"])
def test_unreadable_date_is_not_dropped_silently(app, win, env, text):
    search(app, win)
    ready(app, win)
    win.date_edit.setText(text)
    assert "border" in win.date_edit.styleSheet()
    win.do_save()
    assert text in win.status_label.text() and "2026-02-18" in win.status_label.text() and "Feb 18, 2026" in win.status_label.text()
    assert not win.jobs.running("save")
    assert not (env.root / "roms" / "switch" / "images" / f"{Path(SAMPLE_ROM).stem}-image.jpg").exists()
    win.date_edit.setText("Feb 18, 2026")
    assert win.date_edit.styleSheet() == ""


# ---------------------------------------------------------------- Current, Save review, Undo

def answers(monkeypatch, *given):
    asked = []
    queue = list(given)

    def fake(parent, title, text, choices, default):
        asked.append((title, text, [c[1] for c in choices], default))
        return queue.pop(0)

    monkeypatch.setattr(mw, "ask_choice", fake)
    return asked


CINDER_IMAGE = "Cinder Citadel [0100BBBB00000000][v0]-image.jpg"


def test_a_partial_game_shows_current_for_what_it_has_and_placeholders_for_the_rest(app, win, env):
    pick(app, win, "Cinder")
    assert pump(app, lambda: win.logo_panel.info.text().startswith("Current: none") and win.box_panel.shows_current)
    assert win.image_panel.shows_current and win.image_panel.preview.has_image() and win.box_panel.preview.has_image()
    assert "Current - 640×360" in win.image_panel.info.text() and "Current - 396×640" in win.box_panel.info.text()
    assert not win.logo_panel.preview.has_image() and "Current: none" in win.video_panel.note.text()
    assert (win.name_edit.text(), win.dev_edit.text(), win.genre_edit.text(), win.desc_edit.toPlainText()) == \
        ("Cinder Citadel", "Old Studio", "Action", "Old text.")
    assert win.date_edit.text() == "" and not win.jobs.running("current")
    assert win.logo_panel.current_output() is None and win.image_panel.current_output() is None
    assert all(h.isHidden() for h in win.hints.values()) and win.use_all_btn.isHidden()


def test_a_complete_game_shows_all_four_as_current(app, win, env):
    pick(app, win, "Finished")
    current_loaded(app, win)
    assert all(p.shows_current for p in (win.logo_panel, win.box_panel, win.image_panel))
    assert win.video_panel.selected_candidate().current and "45 s" not in win.video_panel.summary.text()
    assert "0:45" in win.video_panel.combo.itemText(0)                          # length from the (demo) probe
    assert win.desc_edit.toPlainText() == "Done."


def test_a_game_without_assets_shows_placeholders_everywhere(app, win):
    pick(app, win, "Tidepool")
    current_loaded(app, win)
    for panel in (win.logo_panel, win.box_panel, win.image_panel):
        assert "Current: none" in panel.info.text() and not panel.preview.has_image()
    assert not win.name_edit.text()


def test_search_keeps_current_and_you_can_go_back_to_it(app, win, env):
    pick(app, win, "Cinder")
    current_loaded(app, win)
    win.do_search()
    assert pump(app, lambda: win.info is not None)
    assert pump(app, lambda: win.logo_panel.out)                                # no Current logo: the first new one is shown
    for panel in (win.box_panel, win.image_panel):
        assert panel.shows_current and panel.candidates[0].current and len(panel.candidates) > 1
        assert panel.current_output() is None                                   # Include is ticked, but nothing new to write
    assert win.video_panel.selection() is not None                              # no Current video: the first new one is chosen
    win.image_panel.step(1)
    assert pump(app, lambda: win.image_panel.out is not None)
    assert "New - " in win.image_panel.info.text() and win.image_panel.current_output() == win.image_panel.out
    win.image_panel.step(-1)
    assert win.image_panel.shows_current and win.image_panel.current_output() is None
    assert win.image_panel.info.text().startswith("Current - 640×360")


def test_search_never_replaces_existing_text_but_offers_it(app, win, env):
    pick(app, win, "Cinder")
    current_loaded(app, win)
    win.do_search()
    assert pump(app, lambda: win.info is not None)
    assert (win.name_edit.text(), win.dev_edit.text(), win.genre_edit.text(), win.desc_edit.toPlainText()) == \
        ("Cinder Citadel", "Old Studio", "Action", "Old text.")                         # untouched
    shown = {tag: not hint.isHidden() for tag, hint in win.hints.items()}
    assert shown == {"name": True, "releasedate": False, "developer": True, "publisher": False, "genre": True, "players": False,
                     "desc": True}
    assert win.hints["developer"].label.text() == "found: Demo Studio" and win.hints["developer"].label.textFormat() == QtCore.Qt.PlainText
    assert win.date_edit.text() == "2026-02-18" and win.pub_edit.text() == "Demo Publisher" and win.players_edit.text() == "1"   # blanks are filled
    assert not win.use_all_btn.isHidden()
    win.hints["developer"].button.click()                                       # one click applies one value
    assert win.dev_edit.text() == "Demo Studio" and win.hints["developer"].isHidden() and win.genre_edit.text() == "Action"
    win.use_all_btn.click()
    assert win.genre_edit.text() == "Horror, Adventure" and win.name_edit.text() == "Cinder Citadel (demo)"
    assert win.desc_edit.toPlainText().startswith("A made-up description.")
    assert win.use_all_btn.isHidden() and all(h.isHidden() for h in win.hints.values())


def test_typing_the_found_value_hides_its_hint_and_blank_boxes_follow_the_match(app, win, env):
    pick(app, win, "Tidepool")
    current_loaded(app, win)
    win.do_search()
    assert pump(app, lambda: win.info is not None)
    assert win.name_edit.text() == "Tidepool Tactics (demo)" and win.hints["name"].isHidden()    # blank: filled
    win.match_box.setCurrentIndex(1)                                            # another match: the untouched boxes follow it
    assert pump(app, lambda: win.info is not None and win.info.source_id == "2")
    assert win.name_edit.text() == "Something Else Entirely"
    win.name_edit.setText("My own title")                                       # once edited, the user's text is theirs
    win.match_box.setCurrentIndex(0)
    assert pump(app, lambda: win.info is not None and win.info.source_id == "1")
    assert win.name_edit.text() == "My own title" and not win.hints["name"].isHidden()
    win.name_edit.setText("Tidepool Tactics (demo)")
    assert win.hints["name"].isHidden()


def test_save_shows_the_review_and_replaces_only_what_you_chose(app, win, env, monkeypatch):
    sw = env.root / "roms" / "switch"
    shown = confirm(monkeypatch)
    search(app, win, "Cinder")
    assert pump(app, lambda: win.logo_panel.out)
    old_image = (sw / "images" / CINDER_IMAGE).read_bytes()
    old_box = (sw / "images" / "Cinder Citadel [0100BBBB00000000][v0]-thumb.png").read_bytes()
    win.video_panel.use_box.setChecked(False)
    win.do_save()
    assert pump(app, lambda: "Saved" in win.status_label.text(), 20), win.status_label.text()
    plan = shown[0].plan
    assert [i.slot for i in plan.pictures] == ["logo"] and plan.replace == set() and plan.replaced_files == []
    assert {"Box art", "Image", "Trailer"} == {i.name for i in plan.left_alone}
    assert (sw / "images" / CINDER_IMAGE).read_bytes() == old_image              # Current was shown: untouched
    assert (sw / "images" / "Cinder Citadel [0100BBBB00000000][v0]-thumb.png").read_bytes() == old_box
    assert (sw / "images" / "Cinder Citadel [0100BBBB00000000][v0]-marquee.png").exists()      # the missing logo was added
    gamelist = (sw / "gamelist.xml").read_text()
    assert "<desc>Old text.</desc>" in gamelist and "<developer>Old Studio</developer>" in gamelist
    assert "<players>1</players>" in gamelist and "<releasedate>20260218T000000</releasedate>" in gamelist   # blanks were filled


def test_replace_overwrites_and_undo_brings_the_old_back(app, win, env, monkeypatch):
    sw = env.root / "roms" / "switch"
    shown = confirm(monkeypatch)
    search(app, win, "Cinder")
    show_new(app, win)
    win.use_all_btn.click()
    win.video_panel.use_box.setChecked(False)
    image = sw / "images" / CINDER_IMAGE
    before = image.read_bytes()
    win.do_save()
    assert pump(app, lambda: "Saved" in win.status_label.text(), 20), win.status_label.text()
    plan = shown[0].plan
    assert plan.replace == {"image", "thumbnail"} and sorted(plan.replaced_files) == [
        "images/Cinder Citadel [0100BBBB00000000][v0]-image.jpg", "images/Cinder Citadel [0100BBBB00000000][v0]-thumb.png"]
    assert image.read_bytes() != before and "A made-up description." in (sw / "gamelist.xml").read_text()
    assert pump(app, lambda: win.image_panel.shows_current and win.current.fields["desc"].startswith("A made-up"))   # re-read
    assert win.image_panel.current_output() is None                              # the saved picture is Current now
    asked = answers(monkeypatch, "yes")
    win.do_undo()
    assert pump(app, lambda: "Undone" in win.status_label.text(), 20), win.status_label.text()
    assert image.read_bytes() == before and "<desc>Old text.</desc>" in (sw / "gamelist.xml").read_text()
    assert asked[0][0] == "Undo"


def test_cancel_in_the_review_saves_nothing(app, win, env, monkeypatch):
    sw = env.root / "roms" / "switch"
    before = (sw / "gamelist.xml").read_text()
    files = sorted(p.name for p in (sw / "images").iterdir())
    shown = confirm(monkeypatch, answer=False)
    search(app, win, "Cinder")
    show_new(app, win)
    win.do_save()
    assert len(shown) == 1 and win.status_label.text() == "Not saved." and not win.jobs.running("save")
    pump(app, lambda: False, 0.3)
    assert (sw / "gamelist.xml").read_text() == before and sorted(p.name for p in (sw / "images").iterdir()) == files


def test_the_review_is_built_from_the_screen_not_from_the_disk(app, win, env, monkeypatch):
    shown = confirm(monkeypatch, answer=False)
    search(app, win, "Cinder")
    show_new(app, win)
    win.do_save()
    plan = shown[0].plan
    image = next(i for i in plan.pictures if i.slot == "image")
    assert image.current is not None and image.current_image is not None and image.image is not None
    assert image.source == "Demo" and image.label == "Screenshot 1" and image.size_text.startswith("640×360 JPG")
    assert [t.tag for t in plan.texts] == ["releasedate", "publisher", "players"]    # only the blank boxes a search filled in
    assert plan.fields == {"releasedate": "20260218T000000", "publisher": "Demo Publisher", "players": "1"}


def test_undo_refused_asks_again_and_can_force(app, win, env, monkeypatch):
    sw = env.root / "roms" / "switch"
    confirm(monkeypatch)
    answers(monkeypatch)
    search(app, win)
    ready(app, win)
    win.video_panel.use_box.setChecked(False)
    win.do_save()
    assert pump(app, lambda: "Saved" in win.status_label.text(), 20)
    gl = sw / "gamelist.xml"
    gl.write_text(gl.read_text().replace("</gameList>", "<game><path>./Other.nsp</path></game></gameList>"))   # "EmulationStation" wrote it
    asked = answers(monkeypatch, "yes", "no")
    win.do_undo()
    assert pump(app, lambda: "Undo cancelled" in win.status_label.text(), 20), win.status_label.text()
    assert [a[0] for a in asked] == ["Undo", "Undo anyway?"] and "Other.nsp" in gl.read_text()
    asked = answers(monkeypatch, "yes", "yes")
    win.do_undo()
    assert pump(app, lambda: "Undone" in win.status_label.text(), 20), win.status_label.text()
    assert "Other.nsp" not in gl.read_text()


def test_a_picture_that_cannot_be_loaded_is_listed_as_left_out(app, win, monkeypatch):
    shown = confirm(monkeypatch, answer=False)
    search(app, win)
    ready(app, win)
    win.image_panel.step(-1)                                                    # ranked last: the picture that fails
    assert pump(app, lambda: "Could not use this one" in win.image_panel.info.text())
    win.do_save()
    plan = shown[0].plan
    assert "Image" in {i.name for i in plan.left_alone} and "could not be loaded" in next(
        i.reason for i in plan.left_alone if i.name == "Image")
    assert [i.slot for i in plan.pictures] == ["logo", "boxart"] and not win.jobs.running("save")


def test_current_not_yet_read_blocks_the_save_politely(app, win, env, monkeypatch):
    shown = confirm(monkeypatch)
    gate = threading.Event()
    real = env.controller.load_current
    env.controller.load_current = lambda *a, **k: (gate.wait(5), real(*a, **k))[1]
    pick(app, win, "Cinder")
    assert pump(app, lambda: win.jobs.running("current") is not None)
    win.do_save()
    assert "Still reading" in win.status_label.text() and not shown
    gate.set()
    current_loaded(app, win)


def test_current_of_the_game_you_left_does_not_fill_the_new_game(app, win, env):
    gate, seen = threading.Event(), []
    real = env.controller.load_current

    def slow(system, rom_path, cancelled=None):
        seen.append(rom_path)
        if "Cinder" in rom_path:
            gate.wait(5)
        return real(system, rom_path, cancelled)

    env.controller.load_current = slow
    pick(app, win, "Cinder")
    assert pump(app, lambda: seen)
    pick(app, win, "Tidepool")                                                    # the user moves on before Cinder was read
    assert pump(app, lambda: win.current is not None and not win.jobs.running("current"))
    gate.set()
    pump(app, lambda: False, 0.6)
    assert not win.name_edit.text() and not win.dev_edit.text() and not win.image_panel.shows_current
    assert all("Current: none" in p.info.text() for p in (win.logo_panel, win.box_panel, win.image_panel))
    assert win.current.fields["name"] == "" and win.video_panel.combo.count() == 0


# ---------------------------------------------------------------- files the game list does not point at

def give_sample_files(env):
    """Files saved earlier for the sample game, whose gamelist entry lost the tags."""
    sw = env.root / "roms" / "switch"
    stem = Path(SAMPLE_ROM).stem
    for src, dst in (("images/Finished Game-image.jpg", f"images/{stem}-image.jpg"),
                     ("images/Finished Game-marquee.png", f"images/{stem}-marquee.png"),
                     ("videos/Finished Game-video.mp4", f"videos/{stem}-video.mp4")):
        (sw / dst).write_bytes((sw / src).read_bytes())
    return sw, stem


def test_existing_files_the_gamelist_does_not_mention_are_linked_by_save(app, win, env, monkeypatch):
    sw, stem = give_sample_files(env)
    shown = confirm(monkeypatch)
    pick(app, win, "Tidepool")
    current_loaded(app, win)
    assert win.logo_panel.shows_current and "not in the game list" in win.logo_panel.info.text()
    assert "not in the game list" in win.image_panel.info.text() and "not in the game list" in win.video_panel.combo.itemText(0)
    assert "Current: none" in win.box_panel.info.text()
    before = {n: (sw / "images" / f"{stem}-{n}").read_bytes() for n in ("image.jpg", "marquee.png")}
    stamps = {n: (sw / "images" / f"{stem}-{n}").stat().st_mtime_ns for n in ("image.jpg", "marquee.png")}
    win.do_save()                                                              # no search at all: just link what is there
    assert pump(app, lambda: "Saved" in win.status_label.text(), 20), win.status_label.text()
    plan = shown[0].plan
    assert sorted(i.slot for i in plan.links) == ["image", "logo", "video"] and plan.pictures == [] and plan.video is None
    assert plan.replace == set() and plan.replaced_files == [] and "Linked 3" in win.status_label.text()
    gamelist = (sw / "gamelist.xml").read_text()
    for tag, name in (("image", f"images/{stem}-image.jpg"), ("marquee", f"images/{stem}-marquee.png"),
                      ("video", f"videos/{stem}-video.mp4")):
        assert f"<{tag}>./{name}</{tag}>" in gamelist
    assert {n: (sw / "images" / f"{stem}-{n}").read_bytes() for n in before} == before          # not rewritten
    assert {n: (sw / "images" / f"{stem}-{n}").stat().st_mtime_ns for n in before} == stamps
    assert pump(app, lambda: "not in the game list" not in win.logo_panel.info.text() and win.logo_panel.shows_current)
    row = next(r for r in win.rows if "Tidepool" in r.title)
    assert pump(app, lambda: "logo" not in next(r for r in win.rows if "Tidepool" in r.title).missing)
    assert set(row.missing) <= {"desc", "thumbnail"} or pump(app, lambda: set(next(r for r in win.rows if "Tidepool" in r.title).missing) <= {"desc", "thumbnail"})


def test_unticked_unlinked_files_stay_unlinked(app, win, env, monkeypatch):
    sw, stem = give_sample_files(env)
    shown = confirm(monkeypatch, answer=False)
    pick(app, win, "Tidepool")
    current_loaded(app, win)
    for panel in (win.logo_panel, win.image_panel):
        panel.use_box.setChecked(False)
    win.video_panel.use_box.setChecked(False)
    win.do_save()
    plan = shown[0].plan
    assert plan.is_empty and plan.links == [] and "game list" in {i.name: i.reason for i in plan.left_alone}["Logo"]


# ---------------------------------------------------------------- Undo refreshes the window

def test_undo_leaves_the_window_matching_the_disk(app, win, env, monkeypatch):
    sw = env.root / "roms" / "switch"
    shown = confirm(monkeypatch)
    search(app, win, "Cinder")
    show_new(app, win)
    win.use_all_btn.click()
    win.video_panel.use_box.setChecked(False)
    win.do_save()
    assert pump(app, lambda: "Saved" in win.status_label.text(), 20), win.status_label.text()
    assert pump(app, lambda: win.current.fields["desc"].startswith("A made-up") and not win.jobs.running("current"))
    answers(monkeypatch, "yes")
    win.do_undo()
    assert pump(app, lambda: "Undone" in win.status_label.text(), 20), win.status_label.text()
    assert pump(app, lambda: win.current.fields["desc"] == "Old text." and not win.jobs.running("current")), win.current.fields
    disk = (sw / "images" / CINDER_IMAGE).stat().st_size
    assert win.current.pictures["image"].size_bytes == disk and win.current.pictures["logo"] is None
    assert (win.name_edit.text(), win.dev_edit.text(), win.desc_edit.toPlainText()) == ("Cinder Citadel", "Old Studio", "Old text.")
    assert win.image_panel.shows_current and not win.hints["name"].isHidden()          # the found values are offered again
    shown.clear()
    win.do_save()                                                              # a fresh plan, not "nothing to save"
    plan = shown[0].plan
    assert not plan.is_empty and [i.slot for i in plan.pictures] == ["logo"]
    assert [t.tag for t in plan.texts] == ["releasedate", "publisher", "players"]


# ---------------------------------------------------------------- unchanged text is never rewritten

ODD_GAMELIST_DESC = "Old\u00a0text.\r\nSecond line\u2028third\rfourth"


def odd_gamelist(env, **tags):
    sw = env.root / "roms" / "switch"
    text = (sw / "gamelist.xml").read_text().replace("<desc>Old text.</desc>", "<desc>" + ODD_GAMELIST_DESC + "</desc>")
    for tag, value in tags.items():
        text = text.replace("<genre>Action</genre>", f"<genre>Action</genre><{tag}>{value}</{tag}>")
    (sw / "gamelist.xml").write_text(text)
    return sw


def test_text_the_box_shows_differently_is_not_rewritten(app, win, env, monkeypatch):
    sw = odd_gamelist(env, releasedate="20230101T123456")
    shown = confirm(monkeypatch)
    pick(app, win, "Cinder")
    current_loaded(app, win)
    assert win.date_edit.text() == "2023-01-01" and "\u00a0" not in win.desc_edit.toPlainText()
    win.dev_edit.setText("New Studio")                                         # the only thing the user changes
    win.do_save()
    assert pump(app, lambda: "Saved" in win.status_label.text(), 20), win.status_label.text()
    plan = shown[0].plan
    assert plan.fields == {"developer": "New Studio"}                           # not the date (T123456 -> T000000), not the desc
    gamelist = (sw / "gamelist.xml").read_text()
    assert "<releasedate>20230101T123456</releasedate>" in gamelist
    assert "\u00a0" in gamelist and "<developer>New Studio</developer>" in gamelist


def test_an_edited_box_is_written_even_when_it_looks_like_the_original(app, win, env, monkeypatch):
    odd_gamelist(env, releasedate="20230101T123456")
    shown = confirm(monkeypatch, answer=False)
    pick(app, win, "Cinder")
    current_loaded(app, win)
    win.date_edit.setText("2023-01-02")
    win.do_save()
    assert shown[0].plan.fields == {"releasedate": "20230102T000000"}


def test_use_found_clears_its_hint_even_with_odd_characters(app, win, env):
    ctl = env.controller
    real = ctl.load_info

    def load_info(match):
        info = real(match)
        info.description = "A\u00a0made-up\r\ndescription\u2028with\rodd breaks"
        info.name = "Name\u00a0with nbsp"
        return info

    ctl.load_info = load_info
    odd_gamelist(env)
    pick(app, win, "Cinder")
    current_loaded(app, win)
    win.do_search()
    assert pump(app, lambda: win.info is not None)
    assert not win.hints["desc"].isHidden() and not win.hints["name"].isHidden()
    win.hints["desc"].button.click()
    win.hints["name"].button.click()
    assert win.hints["desc"].isHidden() and win.hints["name"].isHidden()
    win.use_all_btn.click()
    assert all(h.isHidden() for h in win.hints.values())


def test_blank_boxes_follow_the_match_even_with_odd_characters(app, win, env):
    ctl = env.controller
    real = ctl.load_info

    def load_info(match):
        info = real(match)
        info.description = "First\r\nline\u00a0here" if match.source_id == "1" else "Second\u2028one"
        return info

    ctl.load_info = load_info
    pick(app, win, "Tidepool")
    current_loaded(app, win)
    win.do_search()
    assert pump(app, lambda: win.info is not None and win.info.source_id == "1")
    assert win.desc_edit.toPlainText().startswith("First") and win.hints["desc"].isHidden()
    win.match_box.setCurrentIndex(1)
    assert pump(app, lambda: win.info is not None and win.info.source_id == "2")
    assert win.desc_edit.toPlainText().startswith("Second")                     # still following: it was not edited


# ---------------------------------------------------------------- text typed while Current is still loading

def test_text_typed_before_current_arrives_is_kept(app, win, env, monkeypatch):
    shown = confirm(monkeypatch, answer=False)
    gate = threading.Event()
    real = env.controller.load_current
    env.controller.load_current = lambda *a, **k: (gate.wait(5), real(*a, **k))[1]
    pick(app, win, "Cinder")
    assert pump(app, lambda: win.jobs.running("current") is not None)
    win.name_edit.setText("My typed title")
    gate.set()
    current_loaded(app, win)
    assert win.name_edit.text() == "My typed title"                             # not overwritten
    assert win.dev_edit.text() == "Old Studio" and win.desc_edit.toPlainText() == "Old text."    # untouched boxes are filled
    win.do_save()
    assert shown[0].plan.fields == {"name": "My typed title"}


# ---------------------------------------------------------------- a stored date the program cannot read

def test_an_unreadable_stored_date_does_not_block_other_changes(app, win, env, monkeypatch):
    sw = env.root / "roms" / "switch"
    (sw / "gamelist.xml").write_text((sw / "gamelist.xml").read_text().replace(
        "<genre>Action</genre>", "<genre>Action</genre><releasedate>19690101T000000</releasedate>"))
    shown = confirm(monkeypatch, answer=False)
    pick(app, win, "Cinder")
    current_loaded(app, win)
    assert win.date_edit.text() == "19690101T000000" and win.date_edit.styleSheet() == ""      # shown as stored, no red border
    win.dev_edit.setText("New Studio")
    win.do_save()
    assert shown and shown[0].plan.fields == {"developer": "New Studio"} and "release date" not in win.status_label.text().lower()
    shown.clear()
    win.date_edit.setText("18.02.2026")                                         # now the user did edit it: it must be readable
    assert "border" in win.date_edit.styleSheet()
    win.do_save()
    assert not shown and "18.02.2026" in win.status_label.text()
    win.date_edit.setText("2026-02-18")
    win.do_save()
    assert shown[0].plan.fields["releasedate"] == "20260218T000000"


def test_a_current_that_cannot_be_read_says_so_and_blocks_the_save(app, win, env, monkeypatch):
    shown = confirm(monkeypatch)
    (env.root / "roms" / "switch" / "gamelist.xml").write_text("<gameList><game>")
    pick(app, win, "Cinder")
    assert pump(app, lambda: "Could not read what this game has now" in win.status_label.text()), win.status_label.text()
    win.do_save()
    assert "could not be read" in win.status_label.text() and not shown


# ---------------------------------------------------------------- races, cancelling, closing

def test_switching_game_mid_search_drops_the_old_answer(app, win, env):
    ctl, gate = env.controller, threading.Event()
    real = ctl.search

    def slow(title, system="", cancelled=None):
        gate.wait(5)
        return real(title, system, cancelled)

    ctl.search = slow
    pick(app, win, "Tidepool")
    win.do_search()
    assert pump(app, lambda: win.jobs.running("search") is not None)
    pick(app, win, "Cinder")                                                    # the user moves on
    assert win.search_edit.text() == "Cinder Citadel" and win.search_btn.isEnabled()
    gate.set()
    pump(app, lambda: False, 1.0)
    assert win.match_box.count() == 0 and win.info is None and win.name_edit.text() == "Cinder Citadel"   # (its own name, not a found one)
    assert win.results is None and all(h.isHidden() for h in win.hints.values())


def test_changing_the_match_mid_load_keeps_only_the_new_one(app, win, env):
    ctl, gate = env.controller, threading.Event()
    real = ctl.load_info
    calls = []

    def load_info(match):
        calls.append(match.source_id)
        if match.source_id == "1":
            gate.wait(5)
        return real(match)

    ctl.load_info = load_info
    pick(app, win, "Tidepool")
    win.do_search()
    assert pump(app, lambda: win.match_box.count() == 2 and calls == ["1"])
    win.match_box.setCurrentIndex(1)                                           # another match before the first answered
    assert pump(app, lambda: win.info is not None and win.info.source_id == "2")
    gate.set()
    pump(app, lambda: False, 0.5)
    assert win.info.source_id == "2" and win.name_edit.text() == "Something Else Entirely"


def test_panel_ignores_a_slow_answer_for_a_candidate_the_user_left(app):
    jobs = new_group()
    gate = threading.Event()

    def loader(cand, remove_bg, local_ok, cancelled):
        if cand.url == "demo://slow":
            gate.wait(5)
        return Processed(_png_for(cand.url), cand.url)

    panel = widgets.AssetPanel("Image", "image", jobs, loader)
    slow = AssetCandidate(slot="image", url="demo://slow", label="slow")
    fast = AssetCandidate(slot="image", url="demo://shot-2", label="fast")
    panel.set_candidates([slow, fast])
    assert panel.busy
    panel.step(1)
    assert pump(app, lambda: panel.out is not None and not panel.busy)
    assert "demo://shot-2" in panel.note
    gate.set()
    pump(app, lambda: False, 0.5)
    assert "demo://shot-2" in panel.note and panel.candidates[panel.index] is fast
    panel.close()


def _png_for(url):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA", (40, 20), (9, 9, 9, 255)).save(buf, "PNG")
    return buf.getvalue()


def test_cancel_button_stops_the_video_encode_and_saves_nothing(app, win, env, monkeypatch):
    confirm(monkeypatch)
    sw = env.root / "roms" / "switch"
    search(app, win)
    ready(app, win)
    before = (sw / "gamelist.xml").read_text()
    win.do_save()
    assert pump(app, lambda: win.jobs.running("save") is not None and win.cancel_btn.isEnabled())
    win.cancel_btn.click()
    assert win.status_label.text() == "Cancelled."
    assert win.save_btn.isEnabled()                                            # free again at once
    pump(app, lambda: False, 1.5)
    assert (sw / "gamelist.xml").read_text() == before
    assert not list((sw / "videos").glob("Tidepool*"))


def test_window_refuses_to_close_while_a_save_is_writing(app, win):
    entered, release = threading.Event(), threading.Event()

    def writing(job):
        assert job.enter_critical()
        entered.set()
        release.wait(5)
        return "ok"

    win.jobs.start("save", writing, lambda r: None)
    assert pump(app, entered.is_set)
    assert not win.cancel_btn.isEnabled()                                      # past the point of no return
    win.close()
    assert win.isVisible() and "Saving is in progress" in win.status_label.text()
    release.set()
    assert pump(app, lambda: not win.jobs.running("save") and not win.jobs.active())
    win.close()
    assert not win.isVisible()


def test_closing_cancels_searches_and_stops_the_player(app, win, env):
    class Player:
        def __init__(self):
            self.stopped = False

        def poll(self):
            return None

        def terminate(self):
            self.stopped = True

    win._player = player = Player()
    pick(app, win, "Tidepool")
    win.do_search()
    job = win.jobs.running("search")
    win.close()
    assert job.cancelled() and player.stopped and not win.isVisible()


# ---------------------------------------------------------------- slots never raise

def test_exception_in_a_button_slot_is_shown_not_raised(app, env):
    class Boom(MainWindow):
        def do_search(self):
            1 / 0

        def skip(self):
            raise RuntimeError("boom")

    w = Boom(env.controller)
    w.show()
    assert pump(app, lambda: w.system_box.count() == 2)
    w.system_box.setCurrentText("switch")
    assert pump(app, lambda: w.list.count() == 3)
    pick(app, w, "Tidepool")
    w.search_edit.returnPressed.emit()
    assert "ZeroDivisionError" in w.status_label.text()
    w.skip_btn.click()
    assert "Something went wrong inside the program: RuntimeError: boom" in w.status_label.text()
    assert w.search_btn.isEnabled()                                            # still alive
    w.close()
    set_reporter(None)


def test_exception_in_a_worker_callback_is_shown_not_raised(app, win, monkeypatch):
    monkeypatch.setattr(win, "on_info", lambda info: 1 / 0)
    pick(app, win, "Tidepool")
    win.do_search()
    assert pump(app, lambda: "Something went wrong inside the program" in win.status_label.text()), win.status_label.text()
    assert win.centralWidget().isEnabled() and win.search_btn.isEnabled()      # the window is still alive


def test_exception_in_a_worker_becomes_a_plain_message(app, win, env):
    def boom(title, system="", cancelled=None):
        raise RuntimeError("the network exploded")

    env.controller.search = boom
    pick(app, win, "Tidepool")
    win.do_search()
    assert pump(app, lambda: win.status_label.text().startswith("Search failed: RuntimeError: the network exploded"))
    assert win.search_btn.isEnabled()


def test_paint_event_error_does_not_escape(app, monkeypatch):
    shown = []
    set_reporter(shown.append)
    try:
        label = widgets.CheckerLabel()
        monkeypatch.setattr(label, "image_rect", lambda: 1 / 0)
        label._pixmap = QtGui.QPixmap(10, 10)
        label.show()
        app.processEvents()
        label.repaint()
        assert shown and "ZeroDivisionError" in shown[0]
        label.close()
    finally:
        set_reporter(None)


# ---------------------------------------------------------------- files and links

def test_choose_file_goes_through_the_controller_and_only_that_file_is_read(app, win, env, tmp_path):
    f = tmp_path / "my logo.png"
    f.write_bytes(demo_picture("demo://logo-transparent"))
    panel = win.logo_panel
    pick(app, win, "Tidepool")
    panel.add_file_path(str(f))
    assert pump(app, lambda: panel.out is not None and "my logo.png" in panel.info.text()), panel.info.text()
    assert panel.candidates[-1].source == "your file" and panel.candidates[-1].url in panel.local_urls
    # a candidate from the web with a file address is refused, even if its source text says "your file"
    other = tmp_path / "secret.png"
    other.write_bytes(f.read_bytes())
    spoof = AssetCandidate(slot="logo", url="file://" + str(other), source="your file", label="spoof")
    panel.add_candidate(spoof)
    assert pump(app, lambda: "Could not use this one" in panel.info.text())
    assert panel.out is None


def test_local_candidate_comes_from_the_controller_when_it_has_one(app, win, env, tmp_path):
    f = tmp_path / "x.png"
    f.write_bytes(demo_picture("demo://box"))
    calls = []

    real = env.controller.local_candidate

    def local_candidate(slot, path, label=None):
        calls.append((slot, path, threading.current_thread() is threading.main_thread()))
        return real(slot, path, "from controller")

    env.controller.local_candidate = local_candidate
    pick(app, win, "Tidepool")
    win.box_panel.add_file_path(str(f))
    assert pump(app, lambda: win.box_panel.out is not None)
    assert calls == [("boxart", str(f), False)]                                # made in a worker, not on the GUI thread
    assert "from controller" in win.box_panel.info.text()


def test_file_that_cannot_be_used_is_reported(app, win):
    pick(app, win, "Tidepool")
    win.box_panel.add_file_path("/nonexistent/folder/pic.png")
    assert pump(app, lambda: win.box_panel.out is None and "pic.png" in win.box_panel.info.text() + win.status_label.text())
    win.box_panel.add_file_path("relative.png")
    assert "full path" in win.status_label.text()


def test_paste_link_checks_the_address(app, win):
    pick(app, win, "Tidepool")
    win.image_panel.add_link_text("javascript:alert(1)")
    assert "not a web link" in win.status_label.text() and not win.image_panel.candidates
    win.image_panel.add_link_text("https://demo.invalid/x.png")
    assert win.image_panel.candidates[-1].source == "you"


def test_dropping_files_and_links_adds_candidates(app, win, tmp_path):
    f = tmp_path / "drop.png"
    f.write_bytes(demo_picture("demo://shot-1"))
    pick(app, win, "Tidepool")
    mime = QtCore.QMimeData()
    mime.setUrls([QtCore.QUrl.fromLocalFile(str(f)), QtCore.QUrl("https://demo.invalid/a.png")])
    event = QtGui.QDropEvent(QtCore.QPointF(5, 5), QtCore.Qt.CopyAction, mime, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
    win.image_panel.dropEvent(event)
    assert pump(app, lambda: len(win.image_panel.candidates) == 2)
    assert {c.source for c in win.image_panel.candidates} == {"your file", "you"}


# ---------------------------------------------------------------- video

def test_video_panel_clip_and_known_duration(app, win):
    search(app, win)
    panel = win.video_panel
    assert panel.combo.count() == 2
    cand, start, end = panel.selection()
    assert (start, end) == (0.0, 90.0) and cand.url == HLS_URL
    panel.combo.setCurrentIndex(1)                                             # YouTube video of 90 s
    assert pump(app, lambda: panel.thumb.has_image())                          # thumbnail fetched through the controller
    assert panel.selection()[2] == 90.0 and panel.end.maximum() == 90.0
    panel.start.setValue(100)
    assert panel.selection()[1] <= 89.0 and panel.selection()[2] == 90.0
    panel.whole.setChecked(True)
    assert panel.selection()[1:] == (0.0, None) and not panel.start.isEnabled()
    panel.use_box.setChecked(False)
    assert panel.selection() is None


def test_choose_a_video_file(app, win, tmp_path):
    clip = tmp_path / "my clip.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    pick(app, win, "Tidepool")
    panel = win.video_panel
    panel.add_file_path(str(clip))
    assert pump(app, lambda: panel.combo.count() == 1)
    cand = panel.selection()[0]
    assert cand.source == "your file" and cand.url in panel.local_urls and cand.url.endswith("my clip.mp4")
    panel.add_file_path("/nonexistent/clip.mp4")
    assert pump(app, lambda: "Could not use that file" in win.status_label.text())
    panel.add_file_path("clip.mp4")
    assert "full path" in win.status_label.text()


def test_preview_starts_a_player_with_safe_arguments(app, win, monkeypatch):
    started = []

    class P:
        def __init__(self, argv):
            self.argv, self.dead = argv, False
            started.append(argv)

        def poll(self):
            return 0 if self.dead else None

        def terminate(self):
            self.dead = True
            started.append("terminated")

    monkeypatch.setattr(mw, "launch", lambda argv, name: P(argv))
    monkeypatch.setattr(mw.shutil, "which", lambda n: "/usr/bin/mpv" if n == "mpv" else None)
    search(app, win)
    win.video_panel.preview_btn.click()
    argv = started[0]
    assert argv[0] == "/usr/bin/mpv" and argv[-2:] == ["--", HLS_URL] and not any(a.startswith("--ytdl") for a in argv)
    win.video_panel.preview_btn.click()
    assert "terminated" in started                                             # one player at a time
    monkeypatch.setattr(mw.shutil, "which", lambda n: None)
    win.video_panel.preview_btn.click()
    assert "No video player" in win.status_label.text()
    started.clear()
    monkeypatch.setattr(mw.shutil, "which", lambda n: "/usr/bin/cvlc" if n == "cvlc" else None)
    win.video_panel.combo.setCurrentIndex(1)                                   # the YouTube video; vlc cannot use the downloader
    win.video_panel.preview_btn.click()
    assert "needs mpv" in win.status_label.text() and not any(isinstance(x, list) for x in started)


def test_player_that_failed_to_start_is_reported(app, win, monkeypatch):
    def broken(argv, name):
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(mw, "launch", broken)
    monkeypatch.setattr(mw.shutil, "which", lambda n: "/usr/bin/mpv" if n == "mpv" else None)
    search(app, win)
    win.video_panel.preview_btn.click()
    assert "Could not start the video player" in win.status_label.text()


def test_preview_never_plays_a_file_the_controller_did_not_issue(app, win, monkeypatch):
    started = []
    monkeypatch.setattr(mw, "launch", lambda argv, name: started.append(argv) or type("P", (), {"poll": lambda s: None})())
    monkeypatch.setattr(mw.shutil, "which", lambda n: "/usr/bin/mpv" if n == "mpv" else None)
    sneaky = AssetCandidate(slot="video", url="file:///etc/passwd", kind="file", source="your file", label="sneaky")
    win.video_panel.set_candidates([sneaky])                                   # as if a web source had offered it
    assert sneaky.url not in win.video_panel.local_urls
    win.preview_video(sneaky, 0, 5)
    assert started == [] and "not a plain http(s) link" in win.status_label.text()


def test_web_search_uses_a_detached_launch_without_blocking(app, win, monkeypatch):
    started = []
    monkeypatch.setattr(mw, "launch", lambda argv, name: started.append((argv, name)))
    monkeypatch.setattr(mw.shutil, "which", lambda n: "/usr/bin/xdg-open" if n == "xdg-open" else None)
    pick(app, win, "Tidepool")
    win.logo_panel.web_requested.emit("logo")
    assert pump(app, lambda: started)
    argv, name = started[0]
    assert argv[0] == "/usr/bin/xdg-open" and argv[1].startswith("https://duckduckgo.com/?q=Tidepool")
    assert pump(app, lambda: "web search was opened" in win.status_label.text())


def test_web_search_without_a_browser_copies_the_address(app, win, monkeypatch):
    monkeypatch.setattr(mw.shutil, "which", lambda n: None)
    pick(app, win, "Tidepool")
    win.logo_panel.web_requested.emit("logo")
    assert pump(app, lambda: "No web browser" in win.status_label.text())
    assert "duckduckgo.com" in QtWidgets.QApplication.clipboard().text()
    assert "duckduckgo.com" in win.status_label.text()                         # visible even if the clipboard is unavailable


# ---------------------------------------------------------------- notices and problems

def test_notices_and_gamelist_problem_stay_visible(app, env):
    w = MainWindow(env.controller, notices=["config.json: bad value"])
    w.show()
    assert pump(app, lambda: w.list.count() == 1)
    assert not w.notice_label.isHidden() and "bad value" in w.notice_label.text()
    (env.root / "roms" / "switch" / "gamelist.xml").write_text("<gameList><oops>")
    w.system_box.setCurrentText("switch")
    assert pump(app, lambda: w.list.count() == 3)
    assert "could not be read" in w.notice_label.text() and "bad value" in w.notice_label.text()
    w.status("something else")
    assert "could not be read" in w.notice_label.text()                       # not overwritten by status messages
    w.close()


def test_save_report_hint_only_when_emulationstation_cannot_be_reloaded(app, win, env, monkeypatch):
    confirm(monkeypatch)
    ctl = env.controller
    monkeypatch.setattr(ctl.saver, "_check_es", lambda: True)
    monkeypatch.setattr(ctl, "_es_running", lambda: True)
    monkeypatch.setattr(ctl, "_es_reload", lambda: False)                      # running, but its reload address does not answer
    monkeypatch.setattr(ctl, "_sleep", lambda s: None)
    search(app, win)
    ready(app, win)
    win.video_panel.use_box.setChecked(False)
    win.do_save()
    assert pump(app, lambda: ES_RESTART_HINT[:40] in win.status_label.text(), 20), win.status_label.text()
    assert "Game lists reloaded" not in win.status_label.text()


def test_save_says_the_lists_were_reloaded_and_when_they_were_applied_again(app, win, env, monkeypatch):
    confirm(monkeypatch)
    ctl = env.controller
    sw = env.root / "roms" / "switch"
    state = {"calls": 0, "stale": None}

    def reload():
        state["calls"] += 1
        data = (sw / "gamelist.xml").read_bytes()
        if state["calls"] == 1:
            state["stale"] = data                                              # the flush: ES remembers this list
        elif state["calls"] == 2:
            (sw / "gamelist.xml").write_bytes(state["stale"])                  # ... and writes it back over our edit
        return True

    monkeypatch.setattr(ctl.saver, "_check_es", lambda: True)
    monkeypatch.setattr(ctl, "_es_running", lambda: True)
    monkeypatch.setattr(ctl, "_es_reload", reload)
    monkeypatch.setattr(ctl, "_sleep", lambda s: None)
    search(app, win)
    ready(app, win)
    win.video_panel.use_box.setChecked(False)
    win.do_save()
    assert pump(app, lambda: "Game lists reloaded in EmulationStation." in win.status_label.text(), 20), win.status_label.text()
    assert "EmulationStation overwrote the list once; it was applied again." in win.status_label.text()
    assert ES_RESTART_HINT[:40] not in win.status_label.text()
    assert "<name>Tidepool Tactics (demo)</name>" in (sw / "gamelist.xml").read_text()


def test_no_roms_folder_is_explained(app, tmp_path):
    e = build_demo(tmp_path / "d2")
    for p in (e.root / "roms").iterdir():
        for q in sorted(p.rglob("*"), reverse=True):
            q.unlink() if q.is_file() else q.rmdir()
        p.rmdir()
    w = MainWindow(e.controller)
    w.show()
    assert pump(app, lambda: "No game folders found" in w.status_label.text())
    w.close()


# ---------------------------------------------------------------- the smoke script and the app entry

def test_ui_smoke_script_runs():
    out = subprocess.run([sys.executable, str(ROOT / "tools" / "ui_smoke.py")], capture_output=True, text=True, timeout=120,
                         env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONPATH": str(ROOT)})
    assert "UI SMOKE OK" in out.stdout, out.stdout + out.stderr


def test_app_module_imports_and_parses_arguments():
    from scrappy import app as scrappy_app
    args, rest = scrappy_app.parse_args(["--demo", "-style", "fusion"])
    assert args.demo and rest == ["-style", "fusion"]
    assert scrappy_app.parse_args([])[0].demo is False


# ---------------------------------------------------------------- review round: tooltips, JPEG, mnemonics, small behaviours

HOSTILE = "<b>x</b><img src=file:///etc/hosts>"


def shown_as_text(tip):
    """What a tooltip would display: parse it as rich text (Qt does for anything that looks like HTML)."""
    doc = QtGui.QTextDocument()
    if QtCore.Qt.mightBeRichText(tip):
        doc.setHtml(tip)
    else:
        doc.setPlainText(tip)
    return doc.toPlainText(), doc.toHtml()


def test_tooltips_never_interpret_web_text(app, win, env):
    ctl = env.controller
    real = ctl.load_info

    def load_info(match):
        info = real(match)
        info.name = HOSTILE
        return info

    ctl.load_info = load_info
    win.status(HOSTILE)
    texts = [win.status_label.toolTip()]
    search(app, win)
    win.date_edit.setText(HOSTILE)
    texts.append(win.date_edit.toolTip())
    texts += [win.list.item(i).toolTip() for i in range(win.list.count())]
    win.on_rows(("switch", [RomRowLike(HOSTILE)]))
    texts.append(win.list.item(0).toolTip())
    for tip in texts:
        assert tip
        plain, html = shown_as_text(tip)
        assert "<img" not in html and "<b>x" not in html, tip
    win.status(HOSTILE)
    plain, _ = shown_as_text(win.status_label.toolTip())
    assert plain == HOSTILE                                                    # shown literally, not interpreted
    plain, _ = shown_as_text(win.list.item(0).toolTip())
    assert HOSTILE in plain


class RomRowLike:
    def __init__(self, rom_path):
        self.rom_path, self.title, self.status, self.problem = rom_path, "T", {}, ""
        self.complete, self.missing = False, ["desc"]


def test_every_tooltip_in_the_window_is_safe_for_hostile_text(app, win):
    win.status(HOSTILE)
    for w in win.findChildren(QtWidgets.QWidget):
        tip = w.toolTip()
        if tip:
            assert "<img" not in tip and "<b>x" not in tip


def test_previews_do_not_depend_on_qts_jpeg_plugin(app, win, monkeypatch):
    def broken(*args, **kwargs):
        raise AssertionError("Qt's own image decoder was used")

    monkeypatch.setattr(QtGui.QImage, "fromData", staticmethod(broken))
    monkeypatch.setattr(QtGui.QImage, "loadFromData", broken)
    search(app, win)
    ready(app, win)
    assert win.image_panel.out[:3] == b"\xff\xd8\xff" and win.image_panel.preview.has_image()   # the JPEG slot is previewed
    for panel in (win.logo_panel, win.box_panel):
        assert panel.preview.has_image()
    win.make_text_logo()
    assert pump(app, lambda: "generated" in win.logo_panel.info.text())
    win.video_panel.combo.setCurrentIndex(1)
    assert pump(app, lambda: win.video_panel.thumb.has_image())


def mnemonic_of(text):
    import re
    m = re.search(r"(?<!&)&([^&\s])", text)
    return m.group(1).lower() if m else ""


def test_mnemonics_are_unique(app, win):
    texts = [w.text() for w in win.findChildren((QtWidgets.QPushButton, QtWidgets.QLabel, QtWidgets.QCheckBox))]
    texts += [a.text() for a in win.menuBar().actions()]
    keys = [mnemonic_of(t) for t in texts if mnemonic_of(t)]
    assert len(keys) == len(set(keys)), keys
    assert "s" in keys                                                         # Search keeps Alt+S; System moved to another letter
    assert mnemonic_of("Save") == "" and win.save_btn.text() == "Save"


def test_undo_cannot_be_interrupted_by_closing(app, win, env, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    real = env.controller.undo

    def slow_undo(force=False):
        entered.set()
        release.wait(5)
        return real(force=force)

    env.controller.undo = slow_undo
    answers(monkeypatch, "yes")
    win.do_undo()
    assert pump(app, entered.is_set)
    win.close()
    assert win.isVisible() and "in progress" in win.status_label.text()
    release.set()
    assert pump(app, lambda: not win.jobs.active())
    win.close()
    assert not win.isVisible()


def test_zero_matches_clear_the_previous_game(app, win, env):
    search(app, win)
    ready(app, win)
    assert win.name_edit.text() and win.logo_panel.candidates
    from scrappy.controller import SearchResults
    env.controller.search = lambda title, system="", cancelled=None: SearchResults()
    win.search_edit.setText("Zzzz Nothing")
    win.do_search()
    assert pump(app, lambda: "Nothing found for Zzzz Nothing" in win.status_label.text()), win.status_label.text()
    assert not win.name_edit.text() and not win.desc_edit.toPlainText()
    assert not win.logo_panel.candidates and win.logo_panel.out is None and win.box_panel.out is None
    assert win.match_box.count() == 0 and win.video_panel.selection() is None


def test_enter_while_searching_says_so(app, win, env):
    gate = threading.Event()
    real = env.controller.search
    env.controller.search = lambda title, system="", cancelled=None: (gate.wait(5), real(title, system, cancelled))[1]
    pick(app, win, "Tidepool")
    win.search_edit.returnPressed.emit()
    assert win.jobs.running("search")
    win.search_edit.returnPressed.emit()
    assert "Still searching" in win.status_label.text()
    gate.set()
    assert pump(app, lambda: win.match_box.count() >= 1)


def test_skip_at_the_last_row_says_so(app, win):
    win.list.setCurrentRow(win.list.count() - 1)
    win.skip_btn.click()
    assert "last one" in win.status_label.text()
    assert win.list.currentRow() == win.list.count() - 1


def test_per_game_choices_are_reset(app, win):
    search(app, win)
    ready(app, win)
    win.logo_panel.remove_bg.setChecked(True)
    win.logo_panel.use_box.setChecked(False)
    win.video_panel.use_box.setChecked(False)
    win.video_panel.whole.setChecked(True)
    pick(app, win, "Cinder")
    assert not win.logo_panel.remove_bg.isChecked() and win.logo_panel.use_box.isChecked()
    assert win.video_panel.use_box.isChecked() and not win.video_panel.whole.isChecked()


def test_drops_are_limited_to_six_items(app, win, tmp_path):
    pick(app, win, "Tidepool")
    files = []
    for i in range(10):
        f = tmp_path / f"d{i}.png"
        f.write_bytes(demo_picture("demo://shot-1"))
        files.append(QtCore.QUrl.fromLocalFile(str(f)))
    mime = QtCore.QMimeData()
    mime.setUrls(files)
    win.image_panel.dropEvent(QtGui.QDropEvent(QtCore.QPointF(5, 5), QtCore.Qt.CopyAction, mime, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier))
    assert pump(app, lambda: len(win.image_panel.candidates) == 6)         # only the first 6 dropped items
    assert "first 6" in win.status_label.text()


def test_adding_a_picture_without_a_game_is_refused_plainly(app, win, tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(demo_picture("demo://shot-1"))
    assert win.current_row is None
    for panel in (win.logo_panel, win.box_panel, win.image_panel):
        win.status("")
        panel.add_link_text("https://demo.invalid/a.png")
        assert "Choose a game" in win.status_label.text() and not panel.candidates
        win.status("")
        panel.add_file_path(str(f))
        assert "Choose a game" in win.status_label.text() and not panel.candidates
    win.status("")
    win.video_panel.add_file_path(str(f))
    assert "Choose a game" in win.status_label.text() and win.video_panel.combo.count() == 0
    mime = QtCore.QMimeData()
    mime.setUrls([QtCore.QUrl.fromLocalFile(str(f))])
    win.image_panel.dropEvent(QtGui.QDropEvent(QtCore.QPointF(5, 5), QtCore.Qt.CopyAction, mime, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier))
    assert not win.image_panel.candidates and pump(app, lambda: True)


def test_long_warning_is_not_clipped(app, win):
    win.resize(1100, 740)
    panel = win.logo_panel
    long_text = "⚠ " + "this picture has a solid background; " * 4 + "\nLogo · Demo · 600×300 PNG · 1/2"
    panel.info.setText(long_text)
    app.processEvents()
    needed = panel.info.fontMetrics().boundingRect(0, 0, panel.info.width(), 10000, QtCore.Qt.TextWordWrap, long_text).height()
    assert panel.info.minimumHeight() >= needed


def test_banner_does_not_raise_the_minimum_height(app, env):
    quiet = MainWindow(env.controller)
    base = quiet.minimumSizeHint().height()
    quiet.close()
    loud = MainWindow(env.controller, notices=["config.json: " + "something is wrong with this setting. " * 12])
    loud.show()
    app.processEvents()
    assert not loud.notice_label.isHidden()
    assert loud.minimumSizeHint().height() <= max(base, 717)
    loud.close()
    set_reporter(None)


# ---------------------------------------------------------------- mutants that survived the first review

def test_include_box_is_honoured_for_every_picture_slot(app, win):
    search(app, win)
    ready(app, win)
    panels = {"image_jpg": win.image_panel, "thumb_png": win.box_panel, "marquee_png": win.logo_panel}
    for name, panel in panels.items():
        panel.use_box.setChecked(False)
        req = win.request_for(win.make_plan())
        for other, _ in panels.items():
            assert (getattr(req, other) is None) == (other == name), (name, other)
        panel.use_box.setChecked(True)


def test_save_waits_while_pictures_are_still_loading(app, win, monkeypatch):
    search(app, win)
    ready(app, win)
    win.box_panel.loading = True
    win.do_save()
    assert "still loading" in win.status_label.text() and not win.jobs.running("save")
    win.box_panel.loading = False


def test_rows_for_another_system_are_ignored(app, win):
    before = [win.list.item(i).text() for i in range(win.list.count())]
    win.on_rows(("snes", [RomRowLike("./Other.sfc")]))
    assert [win.list.item(i).text() for i in range(win.list.count())] == before
    assert len(win.rows) == 3


def test_all_panels_accept_drops(app, win):
    assert all(p.acceptDrops() for p in (win.logo_panel, win.box_panel, win.image_panel))


def test_main_window_reports_slot_errors_through_the_excepthook_path(app, win):
    from scrappy.ui.jobs import report_exception
    report_exception(RuntimeError("late"), "test")
    assert "Something went wrong inside the program: RuntimeError: late" in win.status_label.text()


# ---------------------------------------------------------------- real-box findings

class ExitedPlayer:
    def __init__(self, code, last_error):
        self.code, self.last_error = code, last_error

    def poll(self):
        return self.code

    def terminate(self):
        pass


def test_a_player_that_dies_at_once_shows_its_last_error_line(app, win, monkeypatch):
    monkeypatch.setattr(mw, "PLAYER_CHECK_MS", 20)
    monkeypatch.setattr(mw, "launch", lambda argv, name: ExitedPlayer(1, "Error parsing option ytdl (option not found)"))
    monkeypatch.setattr(mw.shutil, "which", lambda n: "/usr/bin/mpv" if n == "mpv" else None)
    search(app, win)
    win.video_panel.preview_btn.click()
    assert "Playing the preview" in win.status_label.text()
    assert pump(app, lambda: "Error parsing option ytdl" in win.status_label.text())
    assert "stopped" in win.status_label.text()


def test_a_player_that_keeps_running_or_ends_cleanly_is_left_alone(app, win, monkeypatch):
    monkeypatch.setattr(mw, "PLAYER_CHECK_MS", 20)
    monkeypatch.setattr(mw.shutil, "which", lambda n: "/usr/bin/mpv" if n == "mpv" else None)
    search(app, win)
    for code in (None, 0):
        monkeypatch.setattr(mw, "launch", lambda argv, name, c=code: ExitedPlayer(c, "ignored"))
        win.video_panel.preview_btn.click()
        pump(app, lambda: False, 0.2)
        assert "Playing the preview" in win.status_label.text()


def test_browser_order_is_chrome_then_xdg_open_then_clipboard(app, win, monkeypatch):
    pick(app, win, "Tidepool")
    available = {"chrome": "/usr/bin/chrome", "xdg-open": "/usr/bin/xdg-open"}
    tried = []

    def fake_launch(argv, name):
        tried.append(argv[0])
        if argv[0] in failing:
            raise OSError(2, "No such file or directory")
        return object()

    monkeypatch.setattr(mw, "launch", fake_launch)
    monkeypatch.setattr(mw.shutil, "which", lambda n: available.get(n))
    failing = set()
    win.logo_panel.web_requested.emit("logo")
    assert pump(app, lambda: "web search was opened" in win.status_label.text())
    assert tried == ["/usr/bin/chrome"] and tried and win.jobs.running("web") is None
    tried.clear()
    failing.add("/usr/bin/chrome")                                              # chrome is there but cannot be started
    win.status("")
    win.logo_panel.web_requested.emit("logo")
    assert pump(app, lambda: "web search was opened" in win.status_label.text())
    assert tried == ["/usr/bin/chrome", "/usr/bin/xdg-open"]
    tried.clear()
    del available["chrome"]
    win.status("")
    win.logo_panel.web_requested.emit("logo")
    assert pump(app, lambda: "web search was opened" in win.status_label.text())
    assert tried == ["/usr/bin/xdg-open"]
    tried.clear()
    failing.add("/usr/bin/xdg-open")
    win.logo_panel.web_requested.emit("logo")
    assert pump(app, lambda: "No web browser" in win.status_label.text())
    assert "duckduckgo.com" in QtWidgets.QApplication.clipboard().text()


def test_browser_gets_the_search_address_as_its_only_argument(app, win, monkeypatch):
    seen = []
    monkeypatch.setattr(mw, "launch", lambda argv, name: seen.append(argv))
    monkeypatch.setattr(mw.shutil, "which", lambda n: "/usr/bin/chrome" if n == "chrome" else None)
    pick(app, win, "Tidepool")
    win.search_edit.setText("Tidepool & Co <b>")
    win.box_panel.web_requested.emit("boxart")
    assert pump(app, lambda: seen)
    assert len(seen[0]) == 2 and seen[0][0] == "/usr/bin/chrome" and seen[0][1].startswith("https://duckduckgo.com/?q=Tidepool+%26+Co")


def test_a_picture_that_cannot_be_used_is_also_named_in_the_status_line(app, win):
    search(app, win)
    ready(app, win)
    win.image_panel.step(-1)                                                    # "A picture that fails to load"
    assert pump(app, lambda: "A picture that fails to load" in win.status_label.text())
    assert "demo server has no such picture" in win.status_label.text()


# ---------------------------------------------------------------- Tools: progress dialogs and the contextual entry points

from scrappy.providers.youtube import InstallResult, YouTubeCancelled, YouTubeError  # noqa: E402
from scrappy.ui import task_dialog as td  # noqa: E402
from scrappy.ui.main_window import INTRO_TIP  # noqa: E402


def finished(app, w, seconds=10.0):
    """The Tools dialog that is open, once it shows its final state."""
    dialog = w._task_dialog
    assert dialog is not None, "no dialog was opened"
    assert pump(app, lambda: dialog.finished_state, seconds), (dialog.state, w.status_label.text())
    return dialog


@pytest.fixture
def quick(env):
    """The demo's downloader and EmulationStation answer at once (no artificial pauses)."""
    env.controller.ytdlp.delay = False
    env.controller._es_reload = lambda: True
    return env.controller


def test_install_in_demo_mode_runs_in_a_dialog_and_ends_with_the_version(app, win, quick):
    quick.ytdlp.installed = False
    win.install_ytdlp()
    dialog = finished(app, win)
    assert dialog.windowTitle() == "Install the YouTube downloader" and dialog.isVisible() and dialog.state == td.SUCCESS
    assert dialog.heading.text() == "YouTube downloader installed."
    assert "Version: 2026.08.19" in dialog.lines.text() and "Search again" in dialog.lines.text()
    assert "installed" in win.status_label.text() and "Version" in win.status_label.text()
    assert quick.ytdlp.available()
    dialog.ok_btn.click()
    assert win._task_dialog is None and not dialog.isVisible()


def test_update_in_demo_mode_says_updated_from_to(app, win, quick):
    win.install_ytdlp()
    dialog = finished(app, win)
    assert dialog.windowTitle() == "Update the YouTube downloader" and dialog.heading.text() == "YouTube downloader updated."
    assert "Updated from version 2026.07.31 to 2026.08.19." in dialog.lines.text()
    dialog.ok_btn.click()


def test_install_shows_a_determinate_bar_while_it_downloads_and_cancel_stops_it(app, win, quick):
    def install(opener=None, progress=None, cancelled=None, stage=None):
        progress(1_000_000, 3_000_000)
        while not cancelled():
            time.sleep(0.002)
        raise YouTubeCancelled("Cancelled")

    quick.ytdlp.install = install
    win.install_ytdlp()
    dialog = win._task_dialog
    assert pump(app, lambda: dialog.size_label.text() == "1.0 of 3.0 MB")
    assert dialog.bar.maximum() == td.PROGRESS_STEPS and dialog.bar.value() == 333
    assert dialog.cancel_btn.isVisible() and dialog.cancel_btn.isEnabled() and dialog.state == td.RUNNING
    dialog.cancel_btn.click()
    dialog = finished(app, win)
    assert dialog.state == td.CANCELLED and "was not changed" in dialog.message.text() and win.status_label.text().startswith("Cancelled")
    dialog.ok_btn.click()
    assert win._task_dialog is None


def test_a_failed_install_shows_the_reason_a_hint_and_details(app, win, quick):
    def install(opener=None, progress=None, cancelled=None, stage=None):
        raise YouTubeError("Could not get the YouTube downloader (check the internet connection)")

    quick.ytdlp.install = install
    quick.ytdlp.installed = False
    win.install_ytdlp()
    dialog = finished(app, win)
    assert dialog.state == td.FAILED and dialog.heading.text() == "Could not install the YouTube downloader"
    assert dialog.message.text() == "Could not get the YouTube downloader (check the internet connection)"
    assert "online" in dialog.hint.text() and dialog.details_btn.isVisible()
    assert win.status_label.text().startswith("Could not install the YouTube downloader: Could not get")
    assert not win.video_panel.install_btn.isHidden()                          # still missing: the button stays
    dialog.ok_btn.click()


def test_an_unexpected_error_in_the_install_still_ends_in_the_failure_state(app, win, quick):
    def install(opener=None, progress=None, cancelled=None, stage=None):
        raise ZeroDivisionError("division by zero")

    quick.ytdlp.install = install
    win.install_ytdlp()
    dialog = finished(app, win)
    assert dialog.state == td.FAILED and "ZeroDivisionError" in dialog.message.text() and "Traceback" in dialog.details.toPlainText()
    dialog.ok_btn.click()


def test_a_second_request_while_a_dialog_is_open_does_not_start_another_task(app, win, quick):
    release, runs = threading.Event(), []

    def install(opener=None, progress=None, cancelled=None, stage=None):
        runs.append(1)
        release.wait(5)
        return InstallResult(quick.ytdlp.path, "1", "", False)

    quick.ytdlp.install = install
    win.install_ytdlp()
    first = win._task_dialog
    win.install_ytdlp()                                                         # a double click, a menu entry, the button
    win.video_panel.install_btn.click()
    win.reload_es()
    assert win._task_dialog is first and win.status_label.text() == "Please finish the open window first."
    release.set()
    finished(app, win)
    assert runs == [1]
    first.ok_btn.click()
    win.reload_es()                                                             # now a new one may open
    assert win._task_dialog is not None and win._task_dialog is not first
    finished(app, win).ok_btn.click()


def test_reload_in_demo_mode_says_the_lists_were_reloaded(app, win, quick):
    win.reload_es()
    dialog = win._task_dialog
    assert dialog.windowTitle() == "Reload game lists in EmulationStation"
    dialog = finished(app, win)
    assert dialog.state == td.SUCCESS and dialog.heading.text() == "EmulationStation reloaded its game lists."
    assert win.status_label.text() == "EmulationStation reloaded its game lists."
    dialog.ok_btn.click()


def test_reload_that_cannot_reach_emulationstation_says_why_and_how_to_do_it_by_hand(app, win, quick):
    quick._es_reload = lambda: False
    win.reload_es()
    dialog = finished(app, win)
    assert dialog.state == td.FAILED and dialog.heading.text() == "EmulationStation was not reached"
    assert dialog.message.text() == "Could not reach EmulationStation, so its game lists were not reloaded."
    assert dialog.hint.text() == "Do it by hand: Start > Game Settings > Update Gamelists."
    assert "127.0.0.1:1234/reloadgames" in dialog.details.toPlainText()
    assert win.status_label.text() == "Could not reach EmulationStation. Use Start > Game Settings > Update Gamelists."
    dialog.ok_btn.click()


def test_reload_that_raises_is_a_failure_too(app, win, quick):
    def boom():
        raise RuntimeError("socket exploded")

    quick._es_reload = boom                                                     # (the controller turns it into 'no answer')
    win.reload_es()
    dialog = finished(app, win)
    assert dialog.state == td.FAILED and dialog.ok_btn.isEnabled()
    dialog.ok_btn.click()


def test_the_tools_menu_entries_open_the_dialogs(app, win, quick):
    actions = {a.text(): a for a in win.menuBar().actions()[0].menu().actions()}
    actions["Install / update YouTube downloader"].trigger()
    finished(app, win).ok_btn.click()
    actions["Reload game lists in EmulationStation"].trigger()
    finished(app, win).ok_btn.click()


def test_closing_the_window_with_a_dialog_open_stops_its_task_quietly(app, env, quick):
    reported = []
    set_reporter(reported.append)
    w = MainWindow(env.controller)
    w.show()
    assert pump(app, lambda: w.system_box.count() == 2)
    stopped = threading.Event()

    def install(opener=None, progress=None, cancelled=None, stage=None):
        while not cancelled():
            time.sleep(0.002)
        stopped.set()
        raise YouTubeCancelled("Cancelled")

    quick.ytdlp.install = install
    w.install_ytdlp()
    dialog = w._task_dialog
    w.close()
    assert stopped.wait(5) and not dialog.isVisible() and w._task_dialog is None
    for _ in range(20):
        app.processEvents()
        time.sleep(0.005)
    assert not [r for r in reported if "Something went wrong" in r]
    set_reporter(None)


def test_the_trailer_panel_offers_the_install_button_only_while_the_downloader_is_missing(app, env, quick):
    quick.ytdlp.installed = False
    w = MainWindow(env.controller)
    w.show()
    assert pump(app, lambda: w.system_box.count() == 2)
    button = w.video_panel.install_btn
    assert button.isVisible() and button.text() == "Install YouTube downloader" and button.parent() is w.video_panel
    assert w.video_panel.note.isVisible()                                      # it sits beside the hint line
    button.click()
    dialog = finished(app, w)
    assert dialog.state == td.SUCCESS and button.isHidden()                    # installed: the button goes away
    dialog.ok_btn.click()
    w.close()


def test_the_install_button_is_not_shown_when_the_downloader_is_there(app, win, quick):
    assert win.video_panel.install_btn.isHidden()
    win.video_panel.set_installer_missing(True)
    assert win.video_panel.install_btn.isVisible()
    win.video_panel.set_installer_missing(False)
    assert win.video_panel.install_btn.isHidden()


def _report(**kw):
    from types import SimpleNamespace
    base = dict(written=[], kept=[], dry_run=False, linked=[], es_warning="", es_reloaded=False, es_running=True, journal=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_reload_button_after_a_save_is_shown_only_when_emulationstation_did_not_take_the_reload(app, win, quick):
    assert win.reload_btn.isHidden()                                           # nothing saved yet
    win.on_saved(_report(es_running=True, es_reloaded=False))
    assert win.reload_btn.isVisible() and win.reload_btn.text() == "Reload game lists"
    win.on_saved(_report(es_running=True, es_reloaded=True))
    assert win.reload_btn.isHidden()
    win.on_saved(_report(es_running=False))
    assert win.reload_btn.isHidden()                                           # ES is not running: nothing to reload
    win.on_saved(_report(es_running=True, es_reloaded=False, es_warning="ES overwrote the list."))
    assert win.reload_btn.isHidden()                                           # reloading would not help; the warning says what to do


def test_reload_button_runs_the_reload_dialog_and_goes_away_when_it_worked(app, win, quick):
    win.on_saved(_report())
    quick._es_reload = lambda: False
    win.reload_btn.click()
    dialog = finished(app, win)
    assert dialog.state == td.FAILED and win.reload_btn.isVisible()           # failed: still offered
    dialog.ok_btn.click()
    quick._es_reload = lambda: True
    win.reload_btn.click()
    dialog = finished(app, win)
    assert dialog.state == td.SUCCESS and win.reload_btn.isHidden()
    dialog.ok_btn.click()


def test_a_real_save_while_emulationstation_ignores_the_reload_offers_the_button(app, win, env, monkeypatch):
    confirm(monkeypatch)
    ctl = env.controller
    monkeypatch.setattr(ctl.saver, "_check_es", lambda: True)
    monkeypatch.setattr(ctl, "_es_running", lambda: True)
    monkeypatch.setattr(ctl, "_es_reload", lambda: False)
    monkeypatch.setattr(ctl, "_sleep", lambda s: None)
    search(app, win)
    ready(app, win)
    win.video_panel.use_box.setChecked(False)
    win.do_save()
    assert pump(app, lambda: ES_RESTART_HINT[:40] in win.status_label.text(), 20), win.status_label.text()
    assert win.reload_btn.isVisible()


# ---- the one-time tip about the Tools menu

def _read_config(env):
    import json
    path = env.controller.cfg.path
    return json.loads(path.read_text()) if path.is_file() else None


def test_first_start_shows_the_tip_once_and_remembers_it(app, env):
    assert env.controller.cfg["intro_seen"] is False and _read_config(env) is None      # no config.json yet
    w = MainWindow(env.controller)
    w.show()
    assert pump(app, lambda: INTRO_TIP in w.status_label.text()), w.status_label.text()
    assert w.status_label.text() == "Choose a game on the left. " + INTRO_TIP
    assert INTRO_TIP == "Tip: the Tools menu (top left) installs the YouTube downloader and reloads game lists."
    assert pump(app, lambda: _read_config(env) == {"intro_seen": True})
    w.close()
    again = MainWindow(env.controller)                                          # a later start: no tip
    again.show()
    assert pump(app, lambda: again.system_box.count() == 2)
    assert INTRO_TIP not in again.status_label.text() and again.status_label.text() == "Choose a game on the left."
    again.close()
    from scrappy.config import load_config
    assert load_config(path=env.controller.cfg.path)["intro_seen"] is True      # a later start reads it back from the file


def test_the_tip_is_replaced_as_soon_as_the_user_does_something(app, win):
    pick(app, win, "Tidepool")
    assert INTRO_TIP not in win.status_label.text()


def test_no_tip_when_the_file_already_says_it_was_seen_and_no_file_is_written(app, env):
    env.controller.cfg["intro_seen"] = True
    w = MainWindow(env.controller)
    w.show()
    assert pump(app, lambda: w.system_box.count() == 2)
    assert INTRO_TIP not in w.status_label.text() and _read_config(env) is None
    w.close()


def test_a_settings_file_with_problems_is_never_rewritten_by_the_tip(app, env):
    path = env.controller.cfg.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json")
    env.controller.cfg.warnings.append("config.json is not valid JSON")
    w = MainWindow(env.controller)
    w.show()
    assert pump(app, lambda: INTRO_TIP in w.status_label.text())
    for _ in range(20):
        app.processEvents()
        time.sleep(0.005)
    assert path.read_text() == "{ not json" and not path.with_name("config.json.corrupt").exists()
    w.close()
