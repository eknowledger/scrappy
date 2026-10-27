"""The picture and video panels with a Current entry: candidates are [Current] + found, Current is never written."""
import io
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt5.QtWidgets")

from PIL import Image  # noqa: E402

from scrappy.models import AssetCandidate, CurrentAsset  # noqa: E402
from scrappy.ui import widgets  # noqa: E402
from scrappy.ui.pipeline import Processed  # noqa: E402
from scrappy.ui.workers import ensure_dispatcher, new_group  # noqa: E402


@pytest.fixture(scope="session")
def app():
    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication(["scrappy-tests"])
    ensure_dispatcher()
    return application


def pump(app, until, seconds=5.0):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        if until():
            return True
        time.sleep(0.01)
    return bool(until())


def png(w=40, h=20, color=(9, 9, 9, 255)):
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def current_asset(slot="image", w=640, h=360, size=54_000, preview=True, problem="", duration=0.0, linked=True):
    cand = AssetCandidate(slot=slot, url=f"current://{slot}", label="Current", source="current", current=True,
                          width=w if preview else 0, height=h if preview else 0, duration=duration)
    return CurrentAsset(cand, f"/roms/images/Game-{slot}.jpg", f"/roms/images/Game-{slot}.jpg", True, size,
                        png(w // 8, h // 8) if preview else b"", problem, duration, tag_linked=linked)


def found(n, slot="image"):
    return [AssetCandidate(slot=slot, url=f"demo://{i}", label=f"Shot {i}", source="Steam") for i in range(n)]


def new_bytes(i):
    return png(40 + i, 20)                                                      # a real picture that differs per candidate


class Loader:
    def __init__(self):
        self.calls = []

    def __call__(self, cand, remove_bg, local_ok, cancelled):
        assert not cand.current, "a Current candidate reached the loader"
        self.calls.append(cand.url)
        return Processed(new_bytes(int(cand.url[-1])), f"new {cand.url}")


@pytest.fixture
def panel(app):
    loader = Loader()
    p = widgets.AssetPanel("Image", "image", new_group(), loader)
    p.loader_calls = loader.calls
    p.show()
    yield p
    p.close()


def with_current(panel, asset=None):
    asset = asset or current_asset()
    panel.set_current(asset, widgets.to_qimage(asset.preview) if asset.preview else None)
    return asset


# ---------------------------------------------------------------- the Current entry

def test_current_is_the_first_candidate_and_is_shown_without_loading(app, panel):
    asset = with_current(panel)
    assert panel.candidates == [asset.candidate] and panel.index == 0 and panel.shows_current
    assert panel.preview.has_image() and not panel.busy and panel.loader_calls == []
    assert panel.info.text().startswith("Current - 640×360") and "54 KB" in panel.info.text()
    assert panel.out is None and panel.current_output() is None


def test_search_adds_candidates_after_current_and_stays_on_current(app, panel):
    asset = with_current(panel)
    panel.set_candidates(found(3))
    assert [c.current for c in panel.candidates] == [True, False, False, False]
    assert panel.index == 0 and panel.shows_current and panel.loader_calls == []
    assert "1/4" in panel.info.text()


def test_stepping_goes_to_the_new_ones_and_back_to_current(app, panel):
    with_current(panel)
    panel.set_candidates(found(2))
    panel.step(1)
    assert pump(app, lambda: panel.out is not None) and panel.out == new_bytes(0) and not panel.shows_current
    assert "New - Shot 0" in panel.info.text() and "2/3" in panel.info.text()
    panel.step(-1)
    assert panel.shows_current and panel.out is None and panel.current_output() is None
    assert panel.info.text().startswith("Current - ")
    panel.step(-1)                                                              # wraps to the last new candidate
    assert pump(app, lambda: panel.out == new_bytes(1))
    panel.step(1)                                                               # and forward again to Current
    assert panel.shows_current


def test_current_with_include_ticked_writes_nothing_but_a_new_candidate_does(app, panel):
    with_current(panel)
    panel.set_candidates(found(1))
    assert panel.use_box.isChecked() and panel.current_output() is None        # Current shown: nothing new
    panel.step(1)
    assert pump(app, lambda: panel.out is not None)
    assert panel.current_output() == new_bytes(0)
    panel.use_box.setChecked(False)
    assert panel.current_output() is None                                       # unticked: untouched


def test_current_output_is_none_for_current_even_if_out_is_stale(app, panel):
    with_current(panel)
    panel.out = b"stale bytes"                                                  # whatever the state, Current is never written
    assert panel.current_output() is None


def test_a_slot_without_current_shows_a_placeholder_and_then_the_first_new_one(app, panel):
    panel.set_current(None, None)
    assert "Current: none" in panel.info.text() and not panel.preview.has_image() and panel.candidates == []
    panel.set_candidates(found(2))
    assert panel.index == 0 and pump(app, lambda: panel.out == new_bytes(0))
    assert panel.info.text().startswith("Shot 0")                               # no Current to compare with: as before


def test_a_picture_that_cannot_be_shown_is_still_current(app, panel):
    asset = with_current(panel, current_asset(preview=False, problem="This file could not be read as a picture (x)."))
    assert panel.shows_current and not panel.preview.has_image()
    assert "could not be read" in panel.info.text() and "54 KB" in panel.info.text()


def test_clear_forgets_current_and_found(app, panel):
    with_current(panel)
    panel.set_candidates(found(2))
    panel.clear()
    assert panel.candidates == [] and not panel.shows_current and "Current: none" not in panel.info.text()
    assert panel.index == -1 and not panel.preview.has_image()


def test_a_new_search_returns_to_current(app, panel):
    with_current(panel)
    panel.set_candidates(found(2))
    panel.step(1)
    assert pump(app, lambda: panel.out is not None)
    panel.set_candidates(found(3))
    assert panel.shows_current and len(panel.candidates) == 4


def test_pasted_links_come_after_everything_and_current_stays_first(app, panel):
    with_current(panel)
    panel.set_candidates(found(1))
    panel.add_link_text("https://example.com/a.png")
    assert panel.candidates[0].current and panel.candidates[-1].label == "your link" and panel.index == 2
    assert not panel.shows_current


def test_set_current_keeps_found_candidates_and_returns_to_current(app, panel):
    panel.set_candidates(found(2))
    with_current(panel)                                                         # e.g. re-read after a save
    assert [c.current for c in panel.candidates] == [True, False, False] and panel.shows_current


def test_pick_describes_what_the_panel_shows(app, panel):
    with_current(panel)
    panel.set_candidates(found(1))
    pick = panel.pick()
    assert pick.slot == "image" and pick.candidate.current and pick.data is None and pick.included
    panel.step(1)
    assert pump(app, lambda: panel.out is not None)
    pick = panel.pick()
    assert pick.candidate.url == "demo://0" and pick.data == new_bytes(0) and pick.note == "new demo://0"
    assert pick.image is not None and not pick.candidate.current
    panel.use_box.setChecked(False)
    assert panel.pick().included is False


def test_pick_after_a_failed_load_has_no_data(app):
    def failing(cand, remove_bg, local_ok, cancelled):
        raise ValueError("nope")

    p = widgets.AssetPanel("Image", "image", new_group(), failing)
    p.set_candidates(found(1))
    assert pump(app, lambda: "Could not use this one" in p.info.text())
    pick = p.pick()
    assert pick.candidate is not None and pick.data is None
    p.close()


def test_the_text_logo_is_a_pick_of_its_own(app):
    p = widgets.AssetPanel("Logo", "logo", new_group(), Loader(), logo_tools=True)
    with_current(p, current_asset("logo", 600, 300))
    p.set_direct(Processed(b"\x89PNGgen", "600×300 PNG · generated text logo"), widgets.to_qimage(png()), "generated text logo")
    pick = p.pick()
    assert pick.data == b"\x89PNGgen" and pick.candidate.source == "generated" and not pick.candidate.current
    assert p.current_output() == b"\x89PNGgen"
    p.close()


def test_current_image_is_available_for_the_comparison_view(app, panel):
    with_current(panel)
    assert panel.current_image is not None
    panel.clear()
    assert panel.current_image is None


# ---------------------------------------------------------------- the trailer panel

@pytest.fixture
def vpanel(app):
    p = widgets.VideoPanel(90, 120, new_group(), lambda *a, **k: b"", lambda *a: None)
    p.show()
    yield p
    p.close()


def trailers(n=2):
    return [AssetCandidate(slot="video", url=f"https://x/{i}.m3u8", label=f"Trailer {i}", source="Steam", kind="hls",
                           duration=100.0) for i in range(n)]


def test_video_panel_lists_current_first_and_stays_on_it(app, vpanel):
    asset = current_asset("video", 0, 0, size=14_000_000, preview=False, duration=83.0)
    vpanel.set_current(asset)
    vpanel.set_candidates(trailers())
    assert vpanel.combo.count() == 3 and vpanel.combo.currentIndex() == 0
    assert vpanel.combo.itemText(0).startswith("Current video") and "1:23" in vpanel.combo.itemText(0)
    assert vpanel.selected_candidate().current and vpanel.selection() is None            # Include ticked: still nothing new
    assert vpanel.use_box.isChecked()
    assert "Nothing will be written" in vpanel.summary.text() and "14 MB" in vpanel.summary.text()
    assert not vpanel.start.isEnabled() and not vpanel.end.isEnabled()


def test_video_panel_choosing_a_new_trailer_makes_it_writable_and_back(app, vpanel):
    vpanel.set_current(current_asset("video", 0, 0, preview=False, duration=83.0))
    vpanel.set_candidates(trailers())
    vpanel.combo.setCurrentIndex(1)
    cand, start, end = vpanel.selection()
    assert cand.label == "Trailer 0" and start == 0.0 and end == 90.0 and vpanel.start.isEnabled()
    vpanel.use_box.setChecked(False)
    assert vpanel.selection() is None
    vpanel.use_box.setChecked(True)
    vpanel.combo.setCurrentIndex(0)
    assert vpanel.selection() is None and not vpanel.start.isEnabled()


def test_video_panel_without_current_behaves_as_before(app, vpanel):
    vpanel.set_current(None)
    assert "Current: none" in vpanel.note.text() or vpanel.combo.count() == 0
    vpanel.set_candidates(trailers())
    assert vpanel.combo.count() == 2 and vpanel.selection()[0].label == "Trailer 0"


def test_video_panel_reset_forgets_current(app, vpanel):
    vpanel.set_current(current_asset("video", 0, 0, preview=False))
    vpanel.set_candidates(trailers(1))
    vpanel.reset()
    assert vpanel.combo.count() == 0 and vpanel.selection() is None
    vpanel.set_candidates(trailers(1))
    assert vpanel.combo.count() == 1 and not vpanel.selected_candidate().current


def test_video_panel_a_chosen_file_is_selected_after_current(app, vpanel, tmp_path):
    vpanel.set_current(current_asset("video", 0, 0, preview=False))
    vpanel.set_candidates([])
    cand = AssetCandidate(slot="video", url="file://" + str(tmp_path / "v.mp4"), label="v.mp4", source="your file")
    vpanel._add_local(cand)
    assert vpanel.combo.count() == 2 and vpanel.selected_candidate() is cand and vpanel.selection() is not None


def test_video_panel_pick_describes_the_choice(app, vpanel):
    vpanel.set_current(current_asset("video", 0, 0, preview=False))
    vpanel.set_candidates(trailers(1))
    assert vpanel.pick().candidate.current and vpanel.pick().included
    vpanel.combo.setCurrentIndex(1)
    pick = vpanel.pick()
    assert pick.candidate.label == "Trailer 0" and (pick.start, pick.end) == (0.0, 90.0) and pick.thumb is None
    vpanel.whole.setChecked(True)
    assert vpanel.pick().end is None


def test_video_panel_reports_the_current_video_to_preview(app, vpanel):
    seen = []
    vpanel.preview_cb = lambda cand, *rest: seen.append(cand)
    asset = current_asset("video", 0, 0, preview=False)
    vpanel.set_current(asset)
    vpanel.set_candidates([])
    vpanel.preview_btn.click()
    assert seen == [asset.candidate]


# ---------------------------------------------------------------- files the game list does not point at

def test_an_unlinked_current_picture_says_so_in_its_caption(app, panel):
    with_current(panel, current_asset(linked=False))
    text = panel.info.text()
    assert text.startswith("Current - not in the game list") and "640×360" in text and "stays as it is" not in text
    panel.set_candidates(found(1))
    assert "1/2" in panel.info.text() and panel.shows_current
    assert panel.current_output() is None                                       # linking happens in the plan, never as bytes


def test_a_linked_current_picture_does_not_mention_the_game_list(app, panel):
    with_current(panel)
    assert "not in the game list" not in panel.info.text()


def test_an_unlinked_current_video_says_so(app, vpanel):
    vpanel.set_current(current_asset("video", 0, 0, preview=False, duration=45.0, size=14_000_000, linked=False))
    assert "not in the game list" in vpanel.combo.itemText(0) and "0:45" in vpanel.combo.itemText(0)
    assert "not in the game list" in vpanel.summary.text() and "links it" in vpanel.summary.text()


# ---------------------------------------------------------------- the end of a new trailer is not Current's length

def test_current_length_does_not_leak_into_a_new_trailer(app, vpanel):
    vpanel.set_current(current_asset("video", 0, 0, preview=False, duration=45.0))
    vpanel.set_candidates([AssetCandidate(slot="video", url="https://x/s.m3u8", label="Steam trailer", source="Steam", kind="hls")])
    vpanel.combo.setCurrentIndex(1)
    cand, start, end = vpanel.selection()
    assert (start, end) == (0.0, 90.0) and vpanel.end.value() == 90.0 and vpanel.end.maximum() == 3600
    vpanel.combo.setCurrentIndex(0)
    vpanel.combo.setCurrentIndex(1)
    assert vpanel.end.value() == 90.0


def test_choosing_a_trailer_without_a_length_after_a_short_one_restores_the_default_end(app, vpanel):
    short = AssetCandidate(slot="video", url="https://x/a", label="Short", source="Steam", kind="hls", duration=30.0)
    unknown = AssetCandidate(slot="video", url="https://x/b", label="Unknown", source="Steam", kind="hls")
    vpanel.set_candidates([short, unknown])
    assert vpanel.end.value() == 30.0
    vpanel.combo.setCurrentIndex(1)
    assert vpanel.end.value() == 90.0 and vpanel.start.value() == 0.0
