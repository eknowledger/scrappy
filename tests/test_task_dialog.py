"""The task dialog (scrappy/ui/task_dialog.py), driven headless with fake tasks.

Skipped where PyQt5 is not installed. The tasks wait on events that the test sets, so nothing sleeps longer than the event loop
needs; ``pump`` runs the Qt event loop until the expected state shows up.
"""
import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt5.QtWidgets")
from PyQt5 import QtCore, QtTest  # noqa: E402

from scrappy.providers.base import ProviderError  # noqa: E402
from scrappy.ui import task_dialog as td  # noqa: E402
from scrappy.ui.jobs import set_reporter  # noqa: E402
from scrappy.ui.task_dialog import TaskDialog, TaskOutcome  # noqa: E402
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
        time.sleep(0.005)
    app.processEvents()
    return bool(until())


@pytest.fixture
def reported():
    """Unexpected errors that reach the window's status line (there must be none)."""
    seen = []
    set_reporter(seen.append)
    yield seen
    set_reporter(None)


@pytest.fixture
def parent(app):
    window = QtWidgets.QWidget()
    window.show()
    yield window
    window.close()
    app.processEvents()


def make(parent, work, **kw):
    kw.setdefault("title", "Install the thing")
    kw.setdefault("message", "Downloading the thing…")
    dialog = TaskDialog(parent, new_group(), kw.pop("title"), kw.pop("message"), work, **kw)
    dialog.outcomes = []
    dialog.finished_with.connect(dialog.outcomes.append)
    return dialog


class Gate:
    """A task that runs until the test lets it go; the work function is `gate.work`."""

    def __init__(self, result="done", before=None):
        self.go, self.reached, self.result, self.before, self.calls = threading.Event(), threading.Event(), result, before, 0

    def work(self, ctx):
        self.calls += 1
        if self.before:
            self.before(ctx)
        self.reached.set()
        assert self.go.wait(5), "the test never released the task"
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


# ---------------------------------------------------------------- the running state

def test_it_shows_a_busy_bar_and_the_message_while_it_runs(app, parent, reported):
    gate = Gate()
    dialog = make(parent, gate.work)
    dialog.show()
    assert dialog.state == td.IDLE and dialog.start() and dialog.state == td.RUNNING
    assert gate.reached.wait(5)
    assert dialog.windowTitle() == "Install the thing" and dialog.message.text() == "Downloading the thing…"
    assert (dialog.bar.minimum(), dialog.bar.maximum()) == (0, 0)                  # endless bar
    assert not dialog.ok_btn.isEnabled() and not dialog.cancel_btn.isVisible()     # (this task cannot be cancelled)
    gate.go.set()
    assert pump(app, lambda: dialog.state == td.SUCCESS) and reported == []


def test_progress_with_a_known_total_fills_the_bar_and_says_x_of_y_mb(app, parent):
    gate = Gate(before=lambda ctx: ctx.progress(1_500_000, 3_000_000))
    dialog = make(parent, gate.work)
    dialog.show()
    dialog.start()
    assert pump(app, lambda: dialog.bar.maximum() == td.PROGRESS_STEPS)
    assert dialog.bar.value() == 500 and dialog.size_label.text() == "1.5 of 3.0 MB"
    gate.go.set()
    assert pump(app, lambda: dialog.state == td.SUCCESS)


def test_progress_with_an_unknown_total_stays_busy_and_counts_the_megabytes(app, parent):
    gate = Gate(before=lambda ctx: ctx.progress(1_500_000, None))
    dialog = make(parent, gate.work)
    dialog.show()
    dialog.start()
    assert pump(app, lambda: dialog.size_label.text() == "1.5 MB downloaded")
    assert dialog.bar.maximum() == 0
    gate.go.set()
    assert pump(app, lambda: dialog.state == td.SUCCESS)


def test_a_message_from_the_task_replaces_the_line_and_goes_back_to_the_busy_bar(app, parent):
    def before(ctx):
        ctx.progress(10, 100)
        ctx.message("Checking the file…")

    gate = Gate(before=before)
    dialog = make(parent, gate.work)
    dialog.show()
    dialog.start()
    assert pump(app, lambda: dialog.message.text() == "Checking the file…")
    assert dialog.bar.maximum() == 0 and dialog.size_label.text() == ""
    gate.go.set()
    assert pump(app, lambda: dialog.state == td.SUCCESS)


def test_progress_reports_are_spaced_out_but_the_last_one_always_gets_through(app, parent):
    posted = []

    class Job:
        def cancelled(self):
            return False

        def notify(self, item):
            posted.append(item)

    ctx = td.TaskContext(Job(), threading.Event())
    for done in range(0, 101):
        ctx.progress(done, 100)
    assert posted[0] == ("progress", 0, 100) and posted[-1] == ("progress", 100, 100)
    assert len(posted) < 20                                                         # not one report per call


def test_size_text_and_technical_text():
    assert td.size_text(1_700_000, 3_000_000) == "1.7 of 3.0 MB" and td.size_text(2_000_000, None) == "2.0 MB downloaded"
    text = td.technical_text(RuntimeError("x" * 10_000))
    assert len(text) <= td.TECHNICAL_LIMIT and text.startswith("RuntimeError: xxx")


# ---------------------------------------------------------------- the final states

def test_success_shows_the_check_mark_what_was_done_and_waits_for_ok(app, parent, reported):
    gate = Gate(TaskOutcome.success("Thing installed.", "Version: 1.2", "File: /bin/thing"))
    dialog = make(parent, gate.work)
    dialog.show()
    dialog.start()
    gate.go.set()
    assert pump(app, lambda: dialog.state == td.SUCCESS)
    assert dialog.badge.mark == "✓" and dialog.badge.color == "#2e7d32"
    assert dialog.heading.text() == "Thing installed." and dialog.lines.text() == "Version: 1.2\nFile: /bin/thing"
    assert dialog.lines.isVisible() and not dialog.bar.isVisible() and not dialog.details_btn.isVisible()
    assert dialog.ok_btn.isEnabled() and dialog.ok_btn.text() == "OK" and not dialog.cancel_btn.isVisible()
    app.processEvents()
    assert dialog.isVisible()                                                       # it stays until OK is pressed
    assert len(dialog.outcomes) == 1 and dialog.outcomes[0].ok
    dialog.ok_btn.click()
    assert not dialog.isVisible() and reported == []


def test_a_plain_text_result_is_the_summary_and_no_result_still_says_done(app, parent):
    for result, heading in (("All set.", "All set."), (None, "Install the thing - done.")):
        gate = Gate(result)
        dialog = make(parent, gate.work)
        dialog.show()
        dialog.start()
        gate.go.set()
        assert pump(app, lambda: dialog.state == td.SUCCESS)
        assert dialog.heading.text() == heading


def test_failure_shows_the_cross_the_plain_reason_a_hint_and_collapsible_details(app, parent, reported):
    cause = OSError(8, "nodename nor servname provided")
    error = ProviderError("Could not get the thing (check the internet connection)")
    error.__cause__ = cause
    gate = Gate(error)
    dialog = make(parent, gate.work, hint="Check the network and try again.", failed_title="Could not install the thing")
    dialog.show()
    dialog.start()
    gate.go.set()
    assert pump(app, lambda: dialog.state == td.FAILED)
    assert dialog.badge.mark == "✗" and dialog.badge.color == "#c62828"
    assert dialog.heading.text() == "Could not install the thing"
    assert dialog.message.text() == "Could not get the thing (check the internet connection)"
    assert dialog.hint.text() == "Check the network and try again." and dialog.hint.isVisible()
    assert dialog.details_btn.isVisible() and not dialog.details.isVisible()        # collapsed to begin with
    dialog.details_btn.click()
    assert dialog.details.isVisible() and "nodename nor servname provided" in dialog.details.toPlainText()
    assert "ProviderError: Could not get the thing" in dialog.details.toPlainText()
    dialog.details_btn.click()
    assert not dialog.details.isVisible()
    assert dialog.ok_btn.text() == "Close" and dialog.ok_btn.isEnabled() and not dialog.outcomes[0].ok
    assert reported == []


@pytest.mark.parametrize("raised", [ValueError("boom"), SystemExit(3), KeyboardInterrupt(), MemoryError()])
def test_whatever_the_task_raises_ends_in_the_failure_state(app, parent, reported, raised):
    gate = Gate(raised)
    dialog = make(parent, gate.work)
    dialog.show()
    dialog.start()
    gate.go.set()
    assert pump(app, lambda: dialog.state == td.FAILED)
    assert type(raised).__name__ in dialog.message.text() or type(raised).__name__ in dialog.details.toPlainText()
    assert "Traceback" in dialog.details.toPlainText()                              # an unexpected error keeps its traceback
    assert dialog.ok_btn.isEnabled() and reported == []


def test_a_task_can_return_its_own_failure_with_hint_and_technical_text(app, parent):
    gate = Gate(TaskOutcome.failure("It was not reached.", "Do it by hand.", "GET http://x did not answer"))
    dialog = make(parent, gate.work, hint="the dialog's default")
    dialog.show()
    dialog.start()
    gate.go.set()
    assert pump(app, lambda: dialog.state == td.FAILED)
    assert dialog.hint.text() == "Do it by hand." and "did not answer" in dialog.details.toPlainText()


def test_text_from_a_task_is_shown_as_plain_text(app, parent):
    gate = Gate(TaskOutcome.failure("<b>bold</b> <img src=file:///etc/hosts>\x1b[31m red", "<i>try</i>", "<a href=x>t</a>"))
    dialog = make(parent, gate.work)
    dialog.show()
    dialog.start()
    gate.go.set()
    assert pump(app, lambda: dialog.state == td.FAILED)
    for label in (dialog.heading, dialog.message, dialog.hint, dialog.lines):
        assert label.textFormat() == QtCore.Qt.PlainText
    assert "<b>bold</b>" in dialog.message.text() and "\x1b" not in dialog.message.text()


# ---------------------------------------------------------------- cancel, close, double start

def test_cancel_stops_the_task_and_ends_in_the_cancelled_state(app, parent, reported):
    stopped = []

    def work(ctx):
        while not ctx.cancelled():
            time.sleep(0.002)
        stopped.append(True)
        raise InterruptedError("cancelled")

    dialog = make(parent, work, cancellable=True, cancelled_text="Cancelled. Nothing was installed.")
    dialog.show()
    dialog.start()
    assert dialog.cancel_btn.isVisible() and dialog.cancel_btn.isEnabled()
    dialog.cancel_btn.click()
    assert dialog.message.text() == "Cancelling…" and not dialog.cancel_btn.isEnabled() and dialog.state == td.RUNNING
    assert pump(app, lambda: dialog.state == td.CANCELLED)
    assert stopped == [True] and dialog.heading.text() == "Cancelled"
    assert dialog.message.text() == "Cancelled. Nothing was installed."
    assert dialog.ok_btn.isEnabled() and not dialog.cancel_btn.isVisible() and dialog.outcomes[0].cancelled
    assert not dialog.details_btn.isVisible() and reported == []


def test_progress_that_arrives_after_cancel_does_not_undo_the_cancelling_text(app, parent):
    def work(ctx):
        while not ctx.cancelled():
            time.sleep(0.002)
        ctx.progress(5, 10)
        raise InterruptedError("cancelled")

    dialog = make(parent, work, cancellable=True)
    dialog.show()
    dialog.start()
    dialog.request_cancel()
    assert not dialog.request_cancel()                                              # (asking twice does nothing new)
    assert pump(app, lambda: dialog.state == td.CANCELLED)


def test_a_task_that_finishes_anyway_after_cancel_is_reported_as_done(app, parent):
    gate = Gate("It had finished already.")
    dialog = make(parent, gate.work, cancellable=True)
    dialog.show()
    dialog.start()
    assert gate.reached.wait(5)
    dialog.request_cancel()
    gate.go.set()
    assert pump(app, lambda: dialog.finished_state)
    assert dialog.state == td.SUCCESS and dialog.heading.text() == "It had finished already."


def test_a_task_that_cannot_be_cancelled_has_no_cancel_button_and_ignores_close(app, parent):
    gate = Gate()
    dialog = make(parent, gate.work)
    dialog.show()
    dialog.start()
    assert not dialog.cancel_btn.isVisible() and not dialog.request_cancel()
    dialog.reject()
    dialog.close()
    QtTest.QTest.keyClick(dialog, QtCore.Qt.Key_Escape)
    QtTest.QTest.keyClick(dialog, QtCore.Qt.Key_Return)
    assert dialog.isVisible() and dialog.state == td.RUNNING and not dialog.outcomes
    gate.go.set()
    assert pump(app, lambda: dialog.state == td.SUCCESS)
    dialog.reject()                                                                  # once it is done, Esc closes
    assert not dialog.isVisible()


def test_closing_while_a_cancellable_task_runs_cancels_it_then_closes(app, parent, reported):
    def work(ctx):
        while not ctx.cancelled():
            time.sleep(0.002)
        raise InterruptedError("cancelled")

    dialog = make(parent, work, cancellable=True)
    dialog.show()
    dialog.start()
    dialog.close()                                                                   # the window's close button
    assert dialog.isVisible() and dialog.message.text() == "Cancelling…"             # stays until the task has stopped ...
    assert pump(app, lambda: not dialog.isVisible())                                 # ... then goes by itself
    assert dialog.state == td.CANCELLED and len(dialog.outcomes) == 1 and reported == []


def test_closing_twice_while_it_is_still_stopping_lets_the_user_go(app, parent, reported):
    release = threading.Event()

    def work(ctx):
        while not ctx.cancelled():
            time.sleep(0.002)
        release.wait(5)                                                              # a task slow to stop
        raise InterruptedError("cancelled")

    dialog = make(parent, work, cancellable=True)
    dialog.show()
    dialog.start()
    dialog.close()
    dialog.close()
    assert not dialog.isVisible() and dialog.state == td.CANCELLED and dialog.outcomes[0].cancelled
    release.set()
    for _ in range(20):
        app.processEvents()
        time.sleep(0.005)
    assert len(dialog.outcomes) == 1 and reported == []                              # the late result is dropped


def test_the_second_start_is_ignored_and_the_task_runs_once(app, parent):
    gate = Gate()
    dialog = make(parent, gate.work)
    dialog.show()
    assert dialog.start() and not dialog.start() and not dialog.start()
    gate.go.set()
    assert pump(app, lambda: dialog.state == td.SUCCESS)
    assert not dialog.start() and gate.calls == 1


def test_a_task_that_finishes_before_the_dialog_is_shown_still_shows_its_result(app, parent, reported):
    dialog = make(parent, lambda ctx: TaskOutcome.success("Quick one.", "File: /x"))
    dialog.start()                                                                   # (not shown yet)
    assert pump(app, lambda: dialog.state == td.SUCCESS)
    assert not dialog.isVisible()
    dialog.show()
    assert dialog.isVisible() and dialog.heading.text() == "Quick one." and dialog.ok_btn.isEnabled()
    assert len(dialog.outcomes) == 1 and reported == []


def test_start_before_show_and_a_failure_before_show_both_end_in_a_final_state(app, parent):
    dialog = make(parent, lambda ctx: (_ for _ in ()).throw(ProviderError("Nope.")))
    dialog.start()
    assert pump(app, lambda: dialog.state == td.FAILED)
    dialog.show()
    assert dialog.message.text() == "Nope." and dialog.ok_btn.isEnabled()


def test_if_the_job_cannot_even_be_started_the_dialog_says_so(app, parent):
    class NoThreads:
        def start(self, *args, **kw):
            raise RuntimeError("can't start new thread")

    dialog = TaskDialog(parent, NoThreads(), "Install", "Working…", lambda ctx: None)
    dialog.show()
    dialog.start()
    assert dialog.state == td.FAILED and "can't start new thread" in dialog.message.text() and dialog.ok_btn.isEnabled()


def test_abort_drops_the_result_and_never_touches_the_deleted_dialog(app, parent, reported):
    gate = Gate()
    dialog = make(parent, gate.work, cancellable=True)
    dialog.show()
    dialog.start()
    assert gate.reached.wait(5)
    dialog.abort()                                                                   # the window is closing
    assert not dialog.isVisible()
    gate.go.set()
    for _ in range(30):
        app.processEvents()
        time.sleep(0.005)
    assert dialog.outcomes == [] and reported == []


def test_deleting_the_parent_while_the_task_runs_is_harmless(app, reported):
    window = QtWidgets.QWidget()
    gate = Gate()
    dialog = make(window, gate.work)
    window.show()
    dialog.show()
    dialog.start()
    assert gate.reached.wait(5)
    window.deleteLater()
    app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
    gate.go.set()
    for _ in range(30):
        app.processEvents()
        time.sleep(0.005)
    assert reported == []


def test_real_clicks_and_keys_are_ignored_for_a_moment_after_the_result_but_code_can_click(app, parent, monkeypatch):
    monkeypatch.setattr(td, "SETTLE_MS", 150)
    dialog = make(parent, lambda ctx: "Quick one.")
    dialog.show()
    dialog.start()
    assert pump(app, lambda: dialog.state == td.SUCCESS)
    QtTest.QTest.mouseClick(dialog.ok_btn, QtCore.Qt.LeftButton)                    # the second click of a double click
    QtTest.QTest.keyClick(dialog, QtCore.Qt.Key_Return)
    QtTest.QTest.keyClick(dialog.ok_btn, QtCore.Qt.Key_Space)
    assert dialog.isVisible()
    assert pump(app, lambda: not dialog._too_soon())
    QtTest.QTest.mouseClick(dialog.ok_btn, QtCore.Qt.LeftButton)
    assert not dialog.isVisible()


def test_a_click_in_code_always_works(app, parent):
    dialog = make(parent, lambda ctx: "Quick one.")
    dialog.show()
    dialog.start()
    assert pump(app, lambda: dialog.state == td.SUCCESS)
    dialog.ok_btn.click()
    assert not dialog.isVisible()
