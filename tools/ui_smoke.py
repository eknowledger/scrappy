"""Builds the real window on the made-up library and checks the wiring. No network, no real files touched.

Run on the Batocera (or anywhere with PyQt5) with:
    QT_QPA_PLATFORM=offscreen PYTHONPATH="$DEST/vendor:$DEST" python3 tools/ui_smoke.py
(the vendored Pillow first: the system Pillow cannot read or write JPEG on the box)
Prints 'UI SMOKE OK' and exits 0 on success; any failed check exits with a message.
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt5 import QtCore, QtWidgets  # noqa: E402

from scrappy.ui.demo import SAMPLE_ROM, build_minimal_demo as build_demo  # noqa: E402
from scrappy.ui.main_window import MainWindow  # noqa: E402


def main() -> int:
    app = QtWidgets.QApplication(sys.argv[:1])
    env = build_demo()
    win = MainWindow(env.controller)
    win.show()

    def pump(until, seconds=20):
        end = time.time() + seconds
        while time.time() < end and not until():
            app.processEvents()
            time.sleep(0.02)
        return until()

    def check(ok, what):
        if not ok:
            print(f"UI SMOKE FAILED: {what}\nstatus line: {win.status_label.text()}", flush=True)
            env.cleanup()
            os._exit(1)

    reviews = []

    def answer_review(press_save: bool) -> None:
        """Called by a timer while the real review dialog is open (modal): look at it, grab it, then press a button."""
        dialog = app.activeModalWidget()
        if dialog is None:
            QtCore.QTimer.singleShot(50, lambda: answer_review(press_save))
            return
        reviews.append(dialog)
        dialog.grab().save(str(env.root / f"review-{len(reviews)}.png"))
        button = dialog.save_btn if press_save and dialog.save_btn is not None else dialog.cancel_btn
        button.click()

    def review_with(press_save: bool) -> None:
        QtCore.QTimer.singleShot(50, lambda: answer_review(press_save))
        win.do_save()

    try:
        check(pump(lambda: win.system_box.count() == 2), "systems listed")
        win.system_box.setCurrentText("switch")
        check(pump(lambda: win.list.count() == 3), "ROM list")

        # a game with partial assets: Current for what it has, a placeholder for the rest, then a search keeps Current
        for i in range(win.list.count()):
            if "Cinder" in win.list.item(i).text():
                win.list.setCurrentRow(i)
        check(pump(lambda: win.current is not None and win.image_panel.shows_current), "Current loaded for a partial game")
        check(win.box_panel.shows_current and win.logo_panel.info.text().startswith("Current: none"), "placeholder for the missing logo")
        check(win.dev_edit.text() == "Old Studio", "current details shown")
        win.search_edit.returnPressed.emit()
        check(pump(lambda: win.info is not None and win.logo_panel.out), "search results next to Current")
        check(win.image_panel.shows_current and not win.hints["developer"].isHidden(), "Current kept, found developer offered")
        review_with(False)                                            # Cancel in the review dialog: nothing is written
        check(len(reviews) == 1 and win.status_label.text() == "Not saved.", "review dialog cancelled")
        sw = env.root / "roms" / "switch"
        check(not (sw / "images" / "Cinder Citadel [0100BBBB00000000][v0]-marquee.png").exists(), "cancel wrote nothing")

        for i in range(win.list.count()):
            if "Tidepool" in win.list.item(i).text():
                win.list.setCurrentRow(i)
        check(win.search_edit.text() == "Tidepool Tactics", f"cleaned title ({win.search_edit.text()!r})")
        win.search_edit.returnPressed.emit()                              # Enter in the name box searches
        check(pump(lambda: win.match_box.count() == 2), "search results")
        check(pump(lambda: win.image_panel.out and win.logo_panel.out and win.box_panel.out), "pictures processed")
        check(win.name_edit.text() == "Tidepool Tactics (demo)" and win.date_edit.text() == "2026-02-18", "details filled")
        plan = win.make_plan()
        req = win.request_for(plan)
        check(req.image_jpg and req.marquee_png and req.thumb_png, "request has the three pictures")
        win.make_text_logo()
        check(pump(lambda: "generated" in win.logo_panel.info.text()), "text logo")
        win.video_panel.use_box.setChecked(False)
        review_with(True)                                             # Save in the review dialog
        check(len(reviews) == 2 and reviews[1].save_btn is not None, "review dialog shown")
        check(pump(lambda: "Saved" in win.status_label.text()), "save finished")
        check((sw / "gamelist.xml").exists(), "gamelist written")
        check((sw / "images" / f"{Path(SAMPLE_ROM).stem}-image.jpg").exists(), "image written with Batocera's name")
        check(pump(lambda: win.image_panel.shows_current), "the saved picture is Current now")
        win.grab().save(str(env.root / "window.png"))
        check((env.root / "window.png").stat().st_size > 5000, "window renders")
        check((env.root / "review-2.png").stat().st_size > 3000, "review dialog renders")

        # a file that is on disk under the standard name but missing from the game list: Current says so, Save links it
        cinder = "Cinder Citadel [0100BBBB00000000][v0]"
        (sw / "images" / f"{cinder}-marquee.png").write_bytes((sw / "images" / "Finished Game-marquee.png").read_bytes())
        for i in range(win.list.count()):
            if "Cinder" in win.list.item(i).text():
                win.list.setCurrentRow(i)
        check(pump(lambda: win.current is not None and win.logo_panel.shows_current), "Current loaded for the unlinked logo")
        check("not in the game list" in win.logo_panel.info.text(), "unlinked file is labelled")
        win.image_panel.use_box.setChecked(False)
        win.box_panel.use_box.setChecked(False)
        review_with(True)
        check(len(reviews) == 3 and [i.slot for i in reviews[2].plan.links] == ["logo"], "review lists the file to link")
        check(pump(lambda: "Linked 1 existing file" in win.status_label.text()), "link finished")
        check(f"<marquee>./images/{cinder}-marquee.png</marquee>" in (sw / "gamelist.xml").read_text(), "tag written")
        # the two Tools actions: a progress window that ends in a result and stays until OK (the demo pretends to download)
        win.install_ytdlp()
        dialog = win._task_dialog
        check(dialog is not None and dialog.state == "running", "the install opens its progress window")
        check(pump(lambda: dialog.finished_state), "the install window reaches its final state")
        check(dialog.state == "success" and "version" in dialog.lines.text().lower(), f"the install succeeded ({dialog.state})")
        dialog.ok_btn.click()
        win.reload_es()
        dialog = win._task_dialog
        check(pump(lambda: dialog.finished_state) and dialog.state == "success", "the reload window reports success")
        dialog.ok_btn.click()
        check(win._task_dialog is None, "the Tools window closed on OK")
        print("UI SMOKE OK", flush=True)
        return 0
    finally:
        env.cleanup()


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)               # worker threads are daemons; do not wait for Qt teardown
