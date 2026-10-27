"""The parts of the window that need no Qt: texts, sizes, links, jobs, picture pipeline, demo library."""
import io
import queue
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from scrappy.controller import RomRow
from scrappy.models import AssetCandidate, Match
from scrappy.providers.base import ProviderError
from scrappy.ui import logic, pipeline
from scrappy.ui.demo import SAMPLE_ROM, build_minimal_demo as build_demo, demo_picture
from scrappy.ui.jobs import JobGroup, report_exception, safe, set_reporter
from scrappy.ui.launch import launch


# ---------------------------------------------------------------- logic

def test_checker_shades_alternate():
    s = logic.CHECKER_SQUARE
    assert logic.checker_shade(0, 0) == logic.CHECKER_LIGHT
    assert logic.checker_shade(s - 1, s - 1) == logic.CHECKER_LIGHT
    assert logic.checker_shade(s, 0) == logic.CHECKER_DARK
    assert logic.checker_shade(0, s) == logic.CHECKER_DARK
    assert logic.checker_shade(s, s) == logic.CHECKER_LIGHT
    assert logic.CHECKER_LIGHT != logic.CHECKER_DARK


@pytest.mark.parametrize("size,box,expected", [
    ((600, 300), (300, 300), (300, 150)),
    ((600, 300), (1200, 1200), (1200, 600)),        # small previews are enlarged to the box
    ((396, 640), (200, 200), (124, 200)),
    ((0, 300), (200, 200), (0, 0)),
    ((600, 300), (0, 50), (0, 0)),
])
def test_fit_within(size, box, expected):
    assert logic.fit_within(*size, *box) == expected


@pytest.mark.parametrize("avail,expected", [
    ((3440, 1440), (1900, 1000)),                   # ultrawide: not stretched across the whole screen
    ((1920, 1040), (1632, 915)),
    ((1366, 728), (1161, 680)),
    ((800, 600), (800, 600)),                       # a tiny screen: never larger than it
])
def test_window_size(avail, expected):
    w, h = logic.window_size(*avail)
    assert (w, h) == expected and w <= avail[0] and h <= avail[1]


def row(status, title="Tidepool"):
    return RomRow("./Tidepool.nsp", title, status)


def test_row_text_and_missing_names():
    done = row({"complete": True})
    assert logic.row_text(done) == "✓ Tidepool"
    part = row({"desc": True, "image": True, "thumbnail": False, "marquee": False, "video": False})
    assert logic.row_text(part) == "✗ Tidepool   (missing: box art, logo, video)"


def test_match_text_and_errors():
    assert logic.match_text(Match("Cinder Citadel", "Steam", "1", 0.874, "2024")) == "Cinder Citadel (2024)  [Steam]  87%"
    assert logic.match_text(Match("X", "S", "1", 7.0)).endswith("100%")
    assert logic.format_errors({"Steam": "down", "YouTube": "slow"}) == "Steam: down; YouTube: slow"


def test_candidate_caption_and_video_label():
    c = AssetCandidate(slot="logo", url="https://x/y.png", label="Logo", source="Steam")
    assert logic.candidate_caption(c, "600×300 PNG", 0, 3) == "Logo · Steam · 600×300 PNG · 1/3"
    assert logic.video_label(AssetCandidate(slot="video", url="u", label="Trailer", source="Steam")) == "Trailer  [Steam]"


def test_clip_range():
    assert logic.clip_range(0, 90, False) == (0.0, 90.0)
    assert logic.clip_range(10, 5, False) == (10.0, 11.0)               # an end before the start is pushed to start + 1 s
    assert logic.clip_range(5, 50, True) == (0.0, None)                 # whole trailer
    assert logic.clip_range(0, 90, False, duration=30) == (0.0, 30.0)   # never past a known duration
    assert logic.clip_range(29.5, 90, False, duration=30) == (29.0, 30.0)
    assert "cut at 2:00" in logic.clip_summary(0, None, 120)
    assert "Only the first 2:00" in logic.clip_summary(0, 200, 120)


def test_web_search_url_and_links():
    url = logic.web_search_url("Tidepool Tactics & Co", "logo")
    assert url.startswith("https://duckduckgo.com/?q=Tidepool+Tactics+%26+Co+game+logo")
    assert logic.normalize_link("  https://a.example/x.png ") == "https://a.example/x.png"
    for bad in ("", "ftp://x/y.png", "file:///etc/passwd", "javascript:alert(1)", "https://a b/c", "http://user:pw@h/x", "https:///x", "not a link"):
        with pytest.raises(ValueError):
            logic.normalize_link(bad)


def which_only(*names):
    return lambda n: f"/usr/bin/{n}" if n in names else None


def test_preview_command_mpv_steam_and_youtube():
    hls = AssetCandidate(slot="video", url="https://cdn.example/t/master.m3u8", kind="hls")
    argv, why = logic.preview_command(hls, 10, 70, which=which_only("mpv"))
    assert why == "" and argv[0] == "/usr/bin/mpv" and argv[-2:] == ["--", hls.url]
    assert "--start=10" in argv and "--end=70" in argv
    assert not any("ytdl" in a and a.startswith("--ytdl") for a in argv)             # this box's mpv 0.38 has no "ytdl" option
    yt = AssetCandidate(slot="video", url="https://www.youtube.com/watch?v=abcdefghijk&list=zzz", kind="youtube")
    argv, why = logic.preview_command(yt, 0, None, which=which_only("mpv"), ytdlp_path="/d/yt-dlp", ytdlp_ok=True)
    assert argv[-1] == "https://www.youtube.com/watch?v=abcdefghijk" and not any(a.startswith("--ytdl") for a in argv)
    assert "--script-opts=ytdl_hook-ytdl_path=/d/yt-dlp" in argv and not any(a.startswith("--end") for a in argv)
    argv, why = logic.preview_command(yt, 0, None, which=which_only("mpv"), ytdlp_ok=False)
    assert argv is None and "not installed" in why


def test_preview_command_plays_a_chosen_file_only_when_allowed():
    mine = AssetCandidate(slot="video", url="file:///home/me/My Clip.mp4", kind="file", source="your file")
    argv, why = logic.preview_command(mine, 5, 20, which=which_only("mpv"), local_ok=True)
    assert argv[-2:] == ["--", "/home/me/My Clip.mp4"] and not any(a.startswith("--ytdl") for a in argv)
    argv, why = logic.preview_command(mine, 5, 20, which=which_only("mpv"))
    assert argv is None and why                                       # a file address from anywhere else is never played


def test_preview_command_refuses_odd_addresses_and_missing_players():
    for url in ("file:///etc/passwd", "edl://x", "-fs", "https://a b", "av://lavfi:testsrc", "/etc/passwd"):
        argv, why = logic.preview_command(AssetCandidate(slot="video", url=url), 0, None, which=which_only("mpv"))
        assert argv is None and why
    argv, why = logic.preview_command(AssetCandidate(slot="video", url="javascript:1", kind="youtube"), 0, None, which=which_only("mpv"))
    assert argv is None
    ok = AssetCandidate(slot="video", url="https://h/x.mp4")
    argv, why = logic.preview_command(ok, 0, 5, which=which_only("ffplay"))
    assert argv[0] == "/usr/bin/ffplay" and argv[-1] == ok.url and "-t" in argv
    argv, why = logic.preview_command(ok, 0, 5, which=which_only())
    assert argv is None and "No video player" in why


def test_exact_mpv_argv_has_no_ytdl_option_anywhere():
    web = AssetCandidate(slot="video", url="https://cdn.example/t/master.m3u8", kind="hls")
    argv, _ = logic.preview_command(web, 1.5, 61.5, which=which_only("mpv"))
    assert argv == ["/usr/bin/mpv", "--start=1.5", "--autofit=60%", "--msg-level=all=error", "--end=61.5", "--",
                    "https://cdn.example/t/master.m3u8"]
    yt = AssetCandidate(slot="video", url="https://www.youtube.com/watch?v=abcdefghijk", kind="youtube")
    argv, _ = logic.preview_command(yt, 0, 30, which=which_only("mpv"), ytdlp_path="/d/yt-dlp", ytdlp_ok=True)
    assert argv == ["/usr/bin/mpv", "--start=0", "--autofit=60%", "--msg-level=all=error", "--end=30",
                    "--script-opts=ytdl_hook-ytdl_path=/d/yt-dlp", "--", "https://www.youtube.com/watch?v=abcdefghijk"]


def test_preview_prefers_mpv_then_vlc_and_errors_are_logged_not_hidden():
    ok = AssetCandidate(slot="video", url="https://h/x.mp4")
    argv, _ = logic.preview_command(ok, 0, 5, which=which_only("mpv", "cvlc", "ffplay"))
    assert argv[0] == "/usr/bin/mpv" and "--really-quiet" not in argv and "--msg-level=all=error" in argv
    argv, _ = logic.preview_command(ok, 3, 8, which=which_only("cvlc", "ffplay"))
    assert argv[0] == "/usr/bin/cvlc" and "--play-and-exit" in argv and "--start-time=3" in argv and "--stop-time=8" in argv
    assert argv[-2:] == ["--", ok.url]
    argv, _ = logic.preview_command(ok, 0, None, which=lambda n: "/usr/bin/vlc" if n == "vlc" else None)
    assert argv[0] == "/usr/bin/vlc" and "--stop-time=0" not in argv and not any(a.startswith("--stop-time") for a in argv)


def test_youtube_preview_with_a_player_that_cannot_use_the_downloader_says_so():
    yt = AssetCandidate(slot="video", url="https://www.youtube.com/watch?v=abcdefghijk", kind="youtube")
    for players in (("cvlc",), ("ffplay",), ("cvlc", "ffplay")):
        argv, why = logic.preview_command(yt, 0, None, which=which_only(*players), ytdlp_path="/d/yt-dlp", ytdlp_ok=True)
        assert argv is None and "needs mpv" in why and "No video player" not in why
    argv, why = logic.preview_command(yt, 0, None, which=which_only(), ytdlp_ok=True)
    assert argv is None and "No video player" in why


HOSTILE = "<b>x</b><img src=file:///etc/hosts> & <script>"


def test_plain_tooltip_escapes_everything():
    tip = logic.plain_tooltip(HOSTILE)
    assert "<b>" not in tip and "<img" not in tip and "<script" not in tip
    assert "&lt;b&gt;x&lt;/b&gt;&lt;img" in tip and "&amp;" in tip
    assert logic.plain_tooltip("") == "" and len(logic.plain_tooltip("x" * 5000)) < 1500


def test_decode_rgba_gives_raw_pixels_for_jpeg_and_png_and_caps_the_size():
    jpg = pipeline.process("image", png(1920, 1080, (200, 0, 0, 255))).data
    w, h, raw = pipeline.decode_rgba(jpg)
    assert (w, h) == (640, 360) and len(raw) == w * h * 4 and raw[0] > 150 and raw[3] == 255
    w, h, raw = pipeline.decode_rgba(png(3000, 1000, (0, 0, 0, 0)), max_side=500)
    assert (w, h) == (500, 167) and raw[3] == 0                      # transparent stays transparent
    with pytest.raises(Exception):
        pipeline.decode_rgba(b"nope")


def test_launch_logs_player_errors_and_reaps_the_process(caplog):
    import logging
    import sys
    with caplog.at_level(logging.INFO, logger="scrappy.ui"):
        proc = launch([sys.executable, "-c", "import sys; print('mpv: cannot open display', file=sys.stderr)"], "player")
        proc.log_thread.join(10)
        proc.reaper.join(10)
    assert proc.returncode == 0                                      # waited for: no zombie
    assert any("player" in r.getMessage() and "cannot open display" in r.getMessage() for r in caplog.records)


def test_launch_keeps_the_last_error_line_for_the_window():
    import sys
    proc = launch([sys.executable, "-c", "import sys; print('first', file=sys.stderr); print('Error parsing option ytdl', file=sys.stderr); sys.exit(2)"],
                  "player")
    proc.log_thread.join(10)
    proc.reaper.join(10)
    assert proc.returncode == 2 and proc.last_error == "Error parsing option ytdl"


def test_launch_reports_a_missing_program_as_oserror():
    with pytest.raises(OSError):
        launch(["/nonexistent/player"], "player")


def test_display_problem_messages():
    from scrappy.app import display_problem
    assert display_problem({"DISPLAY": ":0"}, "linux") == ""
    assert display_problem({"WAYLAND_DISPLAY": "wayland-0"}, "linux") == ""
    assert display_problem({"QT_QPA_PLATFORM": "offscreen"}, "linux") == ""
    assert "DISPLAY" in display_problem({}, "linux") and "desktop" in display_problem({}, "linux")
    assert display_problem({}, "darwin") == ""                          # only Linux needs an X display


def test_missing_pyqt5_gives_a_plain_message_not_a_traceback(monkeypatch, capsys):
    import sys
    from scrappy import app as scrappy_app
    shown = []
    monkeypatch.setitem(sys.modules, "PyQt5", None)                     # import PyQt5 -> ImportError
    monkeypatch.setattr(scrappy_app, "fatal_dialog", lambda text: shown.append(text))
    assert scrappy_app.main([]) == 1
    err = capsys.readouterr().err
    assert "PyQt5" in err and "Traceback" not in err and shown and "PyQt5" in shown[0]


def test_no_display_gives_a_plain_message(monkeypatch, capsys):
    from scrappy import app as scrappy_app
    shown = []
    monkeypatch.setattr(scrappy_app, "display_problem", lambda env=None, platform=None: "No screen found.")
    monkeypatch.setattr(scrappy_app, "fatal_dialog", lambda text: shown.append(text))
    pytest.importorskip("PyQt5.QtWidgets")
    assert scrappy_app.main([]) == 1 and shown == ["No screen found."]
    assert "No screen found." in capsys.readouterr().err


def test_fatal_dialog_uses_zenity_then_xmessage_and_never_raises(monkeypatch):
    from scrappy import app as scrappy_app
    calls = []
    monkeypatch.setattr(scrappy_app.shutil, "which", lambda n: f"/usr/bin/{n}" if n == "xmessage" else None)
    monkeypatch.setattr(scrappy_app.subprocess, "run", lambda argv, **kw: calls.append(argv))
    scrappy_app.fatal_dialog("Boom")
    assert calls and calls[0][0] == "/usr/bin/xmessage" and calls[0][-1] == "Boom"
    monkeypatch.setattr(scrappy_app.shutil, "which", lambda n: "/usr/bin/zenity" if n == "zenity" else None)
    calls.clear()
    scrappy_app.fatal_dialog("Boom")
    assert calls[0][0] == "/usr/bin/zenity" and "--error" in calls[0]
    monkeypatch.setattr(scrappy_app.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no X")))
    scrappy_app.fatal_dialog("Boom")                                        # must not raise


def test_excepthooks_are_installed_and_report(monkeypatch):
    import sys
    import threading
    from scrappy import app as scrappy_app
    shown = []
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    set_reporter(shown.append)
    try:
        scrappy_app.install_excepthooks()
        assert sys.excepthook is not sys.__excepthook__ and threading.excepthook is not threading.__excepthook__
        try:
            1 / 0
        except ZeroDivisionError as exc:
            sys.excepthook(type(exc), exc, exc.__traceback__)
        assert shown and "ZeroDivisionError" in shown[0]
    finally:
        set_reporter(None)


def report(**kw):
    base = dict(written=["a", "b"], kept=["c"], dry_run=False, es_running=False, journal=Path("/d/journal/1"))
    base.update(kw)
    return SimpleNamespace(**base)


def test_saved_message_mentions_linked_files():
    text = logic.saved_message(report(written=[], kept=[], linked=["marquee", "image"]), "HINT")
    assert text.startswith("Saved the game details") and "Linked 2 existing file(s)" in text
    assert "Linked" not in logic.saved_message(report(linked=[]), "HINT")


def test_saved_message_tells_what_happened_with_emulationstation():
    hint = "RESTART HINT"
    quiet = logic.saved_message(report(es_running=True, es_reloaded=True), hint)
    assert "Game lists reloaded in EmulationStation." in quiet and hint not in quiet
    again = logic.saved_message(report(es_running=True, es_reloaded=True, es_reapplied=True), hint)
    assert "EmulationStation overwrote the list once; it was applied again." in again and "Game lists reloaded" in again and hint not in again
    warned = logic.saved_message(report(es_running=True, es_reloaded=True, es_warning="Quit EmulationStation and save again."), hint)
    assert "Quit EmulationStation and save again." in warned and "Game lists reloaded" not in warned
    failed = logic.saved_message(report(es_running=True, es_reloaded=False), hint)
    assert failed.endswith(hint) and "Game lists reloaded" not in failed
    assert hint not in logic.saved_message(report(), hint)


def test_undo_message_tells_what_happened_with_emulationstation():
    base = {"rom_path": "./A.nsp"}
    assert logic.undo_message(dict(base, es_reloaded=True)) == "Undone: ./A.nsp. Game lists reloaded in EmulationStation."
    assert "restored again" in logic.undo_message(dict(base, es_reloaded=True, es_reapplied=True))
    assert logic.undo_message(dict(base, es_warning="Quit it.")).endswith("Quit it.")
    assert logic.undo_message(base) == "Undone: ./A.nsp"


def test_saved_and_undo_messages():
    assert logic.saved_message(report(), "HINT") == "Saved 2 picture/video file(s); kept 1 existing. Gamelist backed up. Use Undo last save to revert."
    assert logic.saved_message(report(es_running=True), "HINT").endswith(" HINT")
    assert "Saved the game details" in logic.saved_message(report(written=[]), "HINT")
    dry = logic.saved_message(report(dry_run=True), "HINT")
    assert dry.startswith("Dry run: nothing was written to the real folders (2 file(s) went to /d")
    assert logic.undo_message(None) == "Nothing to undo."
    assert logic.undo_message({"rom_path": "./A.nsp", "left_alone": ["x"]}) == "Undone: ./A.nsp (left 1 file(s) you changed since)"


@pytest.mark.parametrize("text", ["", "  ", "2026-02-18", "Feb 18, 2026", "2026", "2026/2/8"])
def test_date_ok(text):
    assert logic.date_problem(text) == ""


@pytest.mark.parametrize("text", ["18.02.2026", "02/08/2026", "Feb 18th, 2026", "soon", "2026-02-30"])
def test_date_problem_is_spoken_not_dropped(text):
    msg = logic.date_problem(text)
    assert text in msg and "2026-02-18" in msg and "Feb 18, 2026" in msg


def test_one_line():
    assert logic.one_line("a\nb\t c\x00d") == "a b c d"
    assert len(logic.one_line("x" * 5000)) == 600


# ---------------------------------------------------------------- jobs (fake thread and fake GUI queue)

class Gui:
    """post() queues, pump() runs the queued calls on 'the GUI thread' (the test thread)."""

    def __init__(self):
        self.q = queue.Queue()
        self.threads = []

    def post(self, fn):
        self.q.put(fn)

    def spawn(self, target, name):
        t = threading.Thread(target=target, name=name, daemon=True)
        self.threads.append(t)
        t.start()

    def pump(self, n=1, timeout=5):
        for _ in range(n):
            self.q.get(timeout=timeout)()

    def settle(self):
        for t in self.threads:
            t.join(5)


def group(gui, changes=None):
    return JobGroup(gui.post, (lambda: changes.append(1)) if changes is not None else None, spawn=gui.spawn)


def test_job_result_and_error_are_delivered_on_the_gui_side():
    gui, got = Gui(), []
    g = group(gui)
    g.start("a", lambda job: 7, lambda v: got.append(("ok", v)), lambda m: got.append(("err", m)))
    g.start("b", lambda job: 1 / 0, lambda v: got.append(("ok", v)), lambda m: got.append(("err", m)))
    g.start("c", lambda job: (_ for _ in ()).throw(ProviderError("Steam is down.")), lambda v: None, lambda m: got.append(("err", m)))
    gui.settle()
    assert got == []                                   # nothing runs until the GUI side pumps
    gui.pump(3)
    assert sorted(got) == [("err", "Steam is down."), ("err", "ZeroDivisionError: division by zero"), ("ok", 7)]
    assert g.active() == []


def test_cancelled_job_is_dropped_but_bookkeeping_runs():
    gui, got, changes = Gui(), [], []
    g = group(gui, changes)
    started = threading.Event()
    release = threading.Event()

    def slow(job):
        started.set()
        release.wait(5)
        return "late"

    job = g.start("search", slow, got.append, got.append)
    started.wait(5)
    assert g.running("search") is job
    job.cancel()
    assert g.running("search") is None                 # a cancelled job no longer counts as running
    release.set()
    gui.pump()
    assert got == [] and job.finished and g.active() == []
    assert len(changes) == 2                           # once when started, once when it ended


def test_critical_job_cannot_be_cancelled_and_cancel_all_reports_it():
    gui = Gui()
    g = group(gui)
    in_critical, release = threading.Event(), threading.Event()
    results = []

    def save(job):
        assert job.enter_critical()
        in_critical.set()
        release.wait(5)
        return "saved"

    saving = g.start("save", save, results.append)
    other = g.start("search", lambda job: release.wait(5), lambda v: None)
    in_critical.wait(5)
    assert saving.cancel() is False and not saving.cancelled()
    assert g.cancel_all() == [saving] and other.cancelled()
    release.set()
    gui.pump(2)
    assert results == ["saved"]                        # a committed save always reports back


def test_enter_critical_refused_after_cancel():
    gui = Gui()
    g = group(gui)
    job = g.start("save", lambda job: None, lambda v: None)
    assert job.cancel() is True
    assert job.enter_critical() is False
    gui.pump()


def test_notify_posts_progress_until_cancelled():
    gui, texts = Gui(), []
    g = group(gui)
    job = g.start("save", lambda job: (job.notify("Saving…"), "x")[1], lambda v: None, on_progress=texts.append)
    gui.settle()
    gui.pump(2)
    assert texts == ["Saving…"]
    job2 = g.start("save", lambda job: (job.cancel(), job.notify("late"))[1], lambda v: None, on_progress=texts.append)
    gui.settle()
    gui.pump(1)
    assert texts == ["Saving…"] and job2.cancelled()


def test_callback_exceptions_are_reported_not_raised():
    gui, shown = Gui(), []
    set_reporter(shown.append)
    try:
        g = group(gui)
        g.start("a", lambda job: 1, lambda v: 1 / 0)
        gui.settle()
        gui.pump()                                     # must not raise
        assert shown and shown[0].startswith("Something went wrong inside the program: ZeroDivisionError")
        shown.clear()
        assert safe(lambda: 1 / 0)() is None           # a slot wrapper swallows and reports
        assert len(shown) == 1
        assert safe(lambda a, b: (a, b), nargs=2)(1, 2, 3) == (1, 2)      # extra signal arguments are dropped
        report_exception(ValueError("x"))
    finally:
        set_reporter(None)


def test_failure_log_names_the_candidate_and_plain_errors_have_no_traceback(caplog):
    import logging
    gui = Gui()
    g = group(gui)
    with caplog.at_level(logging.INFO, logger="scrappy.ui"):
        g.start("picture", lambda job: (_ for _ in ()).throw(ProviderError("Steam is down.")), lambda v: None, lambda m: None,
                label="Steam header")
        g.start("picture", lambda job: 1 / 0, lambda v: None, lambda m: None, label="Odd picture")
        gui.settle()
        gui.pump(2)
    plain = [r for r in caplog.records if "Steam header" in r.getMessage()]
    odd = [r for r in caplog.records if "Odd picture" in r.getMessage()]
    assert plain and "Steam is down." in plain[0].getMessage() and plain[0].exc_info is None
    assert odd and odd[0].exc_info is not None                       # an unexpected error keeps its traceback in the log


def test_failure_without_a_handler_is_still_shown():
    gui, shown = Gui(), []
    set_reporter(shown.append)
    try:
        g = group(gui)
        g.start("tool", lambda job: (_ for _ in ()).throw(ProviderError("No network.")), lambda v: None)
        gui.settle()
        gui.pump()
        assert shown and "No network." in shown[0]
    finally:
        set_reporter(None)


def test_newest_result_wins_pattern_with_two_searches():
    """The window cancels the old job when the user moves on; only the new result is delivered."""
    gui, got = Gui(), []
    g = group(gui)
    gate = threading.Event()
    old = g.start("search", lambda job: (gate.wait(5), "old game")[1], got.append)
    old.cancel()
    new = g.start("search", lambda job: "new game", got.append)
    gate.set()
    gui.settle()
    gui.pump(2)
    assert got == ["new game"] and new is not old


# ---------------------------------------------------------------- picture pipeline

def png(w, h, color):
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def test_process_slots_make_the_theme_formats():
    img = pipeline.process("image", png(1920, 1080, (200, 0, 0, 255)))
    assert img.data[:3] == b"\xff\xd8\xff" and "640×360" in img.note and "from 1920×1080" in img.note
    box = pipeline.process("boxart", png(1920, 1080, (0, 0, 200, 255)))
    assert box.data[:8] == b"\x89PNG\r\n\x1a\n" and "cropped" in box.note
    assert "cropped" not in pipeline.process("boxart", png(396, 640, (0, 0, 200, 255))).note


def test_logo_transparency_and_background_removal():
    transparent = pipeline.process("logo", demo_picture("demo://logo-transparent"))
    assert "transparent ✓" in transparent.note and not transparent.warning
    solid = demo_picture("demo://logo-solid")
    kept = pipeline.process("logo", solid)
    assert kept.warning and "Remove background" in kept.warning
    cut = pipeline.process("logo", solid, remove_bg=True)
    assert "background removed" in cut.note and not cut.warning
    flat = pipeline.process("logo", png(300, 100, (10, 120, 30, 255)), remove_bg=True)   # nothing to remove: say so
    assert "left nothing" in flat.warning                  # one flat colour: everything counts as background
    busy = Image.effect_noise((200, 100), 80).convert("RGBA")           # corners differ: no solid background to find
    buf = io.BytesIO()
    busy.save(buf, "PNG")
    assert "no solid background" in pipeline.process("logo", buf.getvalue(), remove_bg=True).warning


def test_load_candidate_local_ok_only_when_told_and_errors_pass_through():
    calls = []

    def fetch(url, local_ok=False, cancelled=None):
        calls.append((url, local_ok))
        if url.startswith("file://") and not local_ok:
            raise ValueError("local files are only read when you chose them yourself")
        return png(100, 100, (1, 2, 3, 255))

    web = AssetCandidate(slot="image", url="https://x/a.png")
    mine = AssetCandidate(slot="image", url="file:///home/me/a.png", source="your file")
    pipeline.load_candidate(fetch, web, "image")
    pipeline.load_candidate(fetch, mine, "image", local_ok=True)
    assert calls == [("https://x/a.png", False), ("file:///home/me/a.png", True)]
    spoof = AssetCandidate(slot="image", url="file:///etc/passwd", source="your file")     # source text alone grants nothing
    with pytest.raises(ValueError):
        pipeline.load_candidate(fetch, spoof, "image", local_ok=False)
    with pytest.raises(InterruptedError):
        pipeline.load_candidate(fetch, web, "image", cancelled=lambda: True)


def test_bad_bytes_raise_plain_errors_and_previews_are_small():
    with pytest.raises(Exception):
        pipeline.process("logo", b"<html>not a picture</html>")
    small = pipeline.make_preview(png(1920, 1080, (5, 5, 5, 255)))
    assert Image.open(io.BytesIO(small)).size == (320, 180)
    assert pipeline.text_logo("   ").note.endswith("generated text logo")            # an empty title still gives a logo


# ---------------------------------------------------------------- the demo library works with the real controller

def test_demo_library_end_to_end_without_qt(tmp_path):
    env = build_demo(tmp_path / "demo")
    ctl = env.controller
    assert ctl.list_systems() == ["snes", "switch"]
    rows = ctl.rows("switch")
    assert {r.title for r in rows} == {"Cinder Citadel", "Tidepool Tactics", "Finished Game"}
    assert rows[-1].complete and rows[-1].title == "Finished Game" and any(r.rom_path == "./" + SAMPLE_ROM for r in rows)
    res = ctl.search("Tidepool Tactics", "switch")
    assert res.matches and res.videos and not res.errors
    info = ctl.load_info(res.matches[0])
    assert {a.slot for a in info.assets} == {"logo", "boxart", "image", "video"}
    env.cleanup()
    assert not env.root.exists()
