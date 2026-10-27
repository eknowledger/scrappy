"""A modal dialog for one slow task: it shows that work is going on, then says plainly how it ended.

The work runs through the window's background jobs (``scrappy.ui.jobs``), never on the GUI thread. The dialog has four states:

* running   a message, a progress bar (endless "busy" bar, or a percentage with "x of y MB" once the task reports sizes) and
            Cancel when the task can be stopped safely;
* success   a green check mark, what was done and where (the task's own lines, e.g. the file and the version);
* failed    a red cross, the reason in plain words (``describe_error``), what to try, and a collapsible "Details" box with the
            technical text;
* cancelled a grey mark and one sentence saying that nothing was changed.

The final state stays on screen until OK is pressed. Rules kept here: the dialog starts its task once (a second ``start`` is
ignored); whatever the task does (returns, raises, even SystemExit) ends in a final state, never in a hang; a result that arrives
after the dialog was closed or deleted is dropped; Esc or the window's close button while running means "Cancel" (and closes when
the task has stopped), and when the task cannot be cancelled the dialog simply stays until it is done; every text is shown as
plain text.

The task is ``work(ctx) -> TaskOutcome | str | None``. ``ctx.cancelled()`` is polled by the task, ``ctx.progress(done, total)``
reports bytes (``total`` None when unknown), ``ctx.message(text)`` changes the line under the title. A task that fails raises;
one that wants to say more (a hint, technical text) returns ``TaskOutcome.failure(...)``.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import traceback
from dataclasses import dataclass

from PyQt5 import QtCore, QtGui, QtWidgets

try:
    from PyQt5 import sip
except ImportError:                                            # pragma: no cover - a PyQt5 without its sip module
    sip = None

from ..providers.base import ProviderError, describe_error
from . import logic
from .jobs import guarded_event, safe
from .widgets import FitLabel, plain_label

log = logging.getLogger("scrappy.ui")

PROGRESS_STEPS = 1000                  # the determinate bar counts in thousandths
PROGRESS_EVERY = 0.05                  # seconds between two progress reports sent to the window
SETTLE_MS = 400                        # real clicks and keys are ignored for this long after the result appears (see _too_soon)
TECHNICAL_LIMIT = 4000                 # characters of technical text kept
WIDTH = 540

RUNNING, SUCCESS, FAILED, CANCELLED, IDLE = "running", "success", "failed", "cancelled", "idle"
# (accent colour, mark) per state; the mark is drawn in a round badge
LOOK = {IDLE: ("#1565c0", "…"), RUNNING: ("#1565c0", "…"), SUCCESS: ("#2e7d32", "✓"), FAILED: ("#c62828", "✗"),
        CANCELLED: ("#6b7280", "–")}
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def megabytes(n: float) -> str:
    return f"{n / 1_000_000:.1f}"


def size_text(done: int, total) -> str:
    """'1.7 of 3.0 MB' when the total is known, '1.7 MB downloaded' when not."""
    if total:
        return f"{megabytes(done)} of {megabytes(total)} MB"
    return f"{megabytes(done)} MB downloaded"


def technical_text(exc: BaseException) -> str:
    """The text of an error and of the errors behind it, and for an unexpected one the end of its traceback."""
    lines, seen, error = [], set(), exc
    while error is not None and id(error) not in seen and len(lines) < 6:
        seen.add(id(error))
        lines.append(f"{type(error).__name__}: {error}")
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    if not isinstance(exc, ProviderError):
        trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip().splitlines()
        lines += ["", *trace[-12:]]
    text = _CONTROL.sub(" ", "\n".join(lines))
    return text if len(text) <= TECHNICAL_LIMIT else text[:TECHNICAL_LIMIT - 1] + "…"


@dataclass
class TaskOutcome:
    """How a task ended. ``summary`` is one plain sentence; ``details`` are short lines under it (success: what was done and
    where; failure: nothing needed); ``hint`` is what to try after a failure; ``technical`` goes into the Details box."""
    ok: bool
    summary: str
    details: tuple = ()
    hint: str = ""
    technical: str = ""
    cancelled: bool = False

    @classmethod
    def success(cls, summary: str, *details: str) -> "TaskOutcome":
        return cls(True, summary, tuple(details))

    @classmethod
    def failure(cls, summary: str, hint: str = "", technical: str = "") -> "TaskOutcome":
        return cls(False, summary, (), hint, technical)


class TaskContext:
    """What the task sees (it runs in a worker thread): cancellation, progress and the message line."""

    def __init__(self, job, cancel: threading.Event):
        self._job, self._cancel = job, cancel
        self._last = 0.0

    def cancelled(self) -> bool:
        return self._cancel.is_set() or self._job.cancelled()

    def progress(self, done: int, total=None) -> None:
        """Bytes so far, and the total when it is known. Reports are spaced out, so calling this for every chunk is fine."""
        now = time.monotonic()
        finished = bool(total) and done >= total
        if finished or now - self._last >= PROGRESS_EVERY:
            self._last = now
            self._job.notify(("progress", int(done), int(total) if total else None))

    def message(self, text: str, busy: bool = True) -> None:
        """A new line under the title. `busy`: show the endless bar again (the quiet steps after a download)."""
        self._job.notify(("message", str(text), bool(busy)))


class StateMark(QtWidgets.QWidget):
    """The round mark at the top left: three dots while it works, a check mark, a cross, or a dash. It is drawn with a painter, not
    as a character of a font, so it looks the same on every machine (the Batocera's fonts may lack the glyphs)."""
    SIZE = 48

    def __init__(self):
        super().__init__()
        self.setFixedSize(self.SIZE, self.SIZE)
        self.color, self.mark = LOOK[IDLE]

    def set_look(self, colour: str, mark: str) -> None:
        self.color, self.mark = colour, mark
        self.update()

    @guarded_event
    def paintEvent(self, _event):
        p = QtGui.QPainter(self)
        try:
            p.setRenderHint(QtGui.QPainter.Antialiasing)
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(QtGui.QColor(self.color))
            p.drawEllipse(QtCore.QRectF(1, 1, self.SIZE - 2, self.SIZE - 2))
            white = QtGui.QColor("white")
            if self.mark == "…":
                p.setBrush(white)
                for x in (15, 24, 33):
                    p.drawEllipse(QtCore.QPointF(x, 24), 3.2, 3.2)
                return
            p.setBrush(QtCore.Qt.NoBrush)
            p.setPen(QtGui.QPen(white, 4.6, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin))
            if self.mark == "✓":
                p.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(14, 25), QtCore.QPointF(21, 32), QtCore.QPointF(34, 17)]))
            elif self.mark == "✗":
                p.drawLine(QtCore.QPointF(16, 16), QtCore.QPointF(32, 32))
                p.drawLine(QtCore.QPointF(32, 16), QtCore.QPointF(16, 32))
            else:
                p.drawLine(QtCore.QPointF(15, 24), QtCore.QPointF(33, 24))
        finally:
            p.end()


def _alive(widget) -> bool:
    try:
        return not (sip is not None and sip.isdeleted(widget))
    except Exception:                                          # noqa: BLE001
        return False


class TaskDialog(QtWidgets.QDialog):
    """See the module text. ``finished_with(outcome)`` is emitted once, after the final state is on screen."""
    finished_with = QtCore.pyqtSignal(object)

    def __init__(self, parent, jobs, title: str, message: str, work, *, cancellable: bool = False, hint: str = "",
                 job_name: str = "tool", cancelled_text: str = "Cancelled. Nothing was changed.",
                 failed_title: str = "Could not finish"):
        """`jobs`: the window's JobGroup. `work(ctx)`: the task. `hint`: what to try when it fails (a task's own hint wins).
        `cancellable`: the task polls ``ctx.cancelled()`` and stops without leaving anything behind."""
        super().__init__(parent)
        self.jobs, self._work, self.job_name = jobs, work, job_name
        self.title_text, self.cancellable, self.hint_text = title, bool(cancellable), hint
        self.cancelled_text, self.failed_title = cancelled_text, failed_title
        self.state = IDLE
        self.outcome = None
        self.job = None
        self._cancel = threading.Event()
        self._close_when_done = False
        self._detached = False
        self._emitted = False
        self._final_at = QtCore.QElapsedTimer()                # started when the result appears
        self.setWindowTitle(title)
        self.setAccessibleName(title)
        self.setModal(True)
        self.setFixedWidth(WIDTH)
        self._build(message)
        self._apply_state()

    # ------------------------------------------------------------------ layout
    def _build(self, message: str) -> None:
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 12)
        outer.setSpacing(10)
        self.strip = QtWidgets.QFrame()                       # the accent: blue while it works, then green, red or grey
        self.strip.setFixedHeight(6)
        outer.addWidget(self.strip)
        body = QtWidgets.QVBoxLayout()
        body.setContentsMargins(16, 6, 16, 0)
        body.setSpacing(10)
        outer.addLayout(body)

        head = QtWidgets.QHBoxLayout()
        head.setSpacing(14)
        self.badge = StateMark()
        self.badge.setAccessibleName("State")
        head.addWidget(self.badge, 0, QtCore.Qt.AlignTop)
        texts = QtWidgets.QVBoxLayout()
        texts.setSpacing(4)
        self.heading = plain_label(self.title_text)
        font = self.heading.font()                            # larger, not bold: the size measures the same on every run, bold does not
        font.setPointSize(round(max(font.pointSizeF(), 9.0) * 1.25))
        self.heading.setFont(font)
        self.heading.setAccessibleName("Heading")
        self.message = plain_label(logic.one_line(message, 400), lines=1)
        self.message.setAccessibleName("Message")
        texts.addWidget(self.heading)
        texts.addWidget(self.message)
        head.addLayout(texts, 1)
        body.addLayout(head)

        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 0)                               # busy until the task reports sizes
        self.bar.setAccessibleName("Progress")
        self.bar.setFixedHeight(18)
        self.size_label = plain_label("", lines=1)
        self.size_label.setAlignment(QtCore.Qt.AlignRight)
        self.size_label.setAccessibleName("Size")
        body.addWidget(self.bar)
        body.addWidget(self.size_label)

        self.lines = plain_label("", lines=1)                 # success: what was done and where
        self.lines.setAccessibleName("Result")
        self.hint = plain_label("", lines=1)                  # failure: what to try
        self.hint.setAccessibleName("What to try")
        body.addWidget(self.lines)
        body.addWidget(self.hint)
        self.details_btn = QtWidgets.QToolButton()
        self.details_btn.setText("Details")
        self.details_btn.setCheckable(True)
        self.details_btn.setArrowType(QtCore.Qt.RightArrow)
        self.details_btn.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.details_btn.setAutoRaise(True)
        self.details_btn.setAccessibleName("Details")
        self.details = QtWidgets.QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setAccessibleName("Technical details")
        self.details.setMaximumHeight(120)
        self.details.setLineWrapMode(QtWidgets.QPlainTextEdit.WidgetWidth)
        body.addWidget(self.details_btn, 0, QtCore.Qt.AlignLeft)
        body.addWidget(self.details)
        self.details_btn.toggled.connect(safe(self._toggle_details, 1))

        buttons = QtWidgets.QHBoxLayout()
        buttons.setContentsMargins(16, 0, 16, 0)
        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.cancel_btn.setAutoDefault(False)
        self.ok_btn = QtWidgets.QPushButton("OK")
        self.ok_btn.setDefault(True)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_btn)
        buttons.addWidget(self.ok_btn)
        outer.addLayout(buttons)
        self.cancel_btn.clicked.connect(safe(self.request_cancel))
        self.ok_btn.clicked.connect(safe(self.accept))
        self.ok_btn.installEventFilter(self)

    def _toggle_details(self, shown: bool) -> None:
        self.details_btn.setArrowType(QtCore.Qt.DownArrow if shown else QtCore.Qt.RightArrow)
        self.details.setVisible(shown)
        self._resize()

    def _resize(self) -> None:
        """Make the dialog exactly tall enough for what it shows (the wrapped labels settle over a few layout passes)."""
        layout = self.layout()
        labels = self.findChildren(FitLabel)
        for _ in range(5):
            layout.activate()
            for label in labels:                               # (their height follows their width; do not wait for resize events)
                label._fit()
            layout.activate()
            need = layout.minimumSize().height()
            if need == self.height():
                break
            self.resize(WIDTH, need)

    def _apply_state(self) -> None:
        colour, mark = LOOK[self.state]
        self.strip.setStyleSheet(f"QFrame {{ background: {colour}; }}")
        self.badge.set_look(colour, mark)
        self.badge.setAccessibleDescription({IDLE: "working", RUNNING: "working", SUCCESS: "done", FAILED: "failed",
                                             CANCELLED: "cancelled"}[self.state])
        self.heading.setStyleSheet(f"QLabel {{ color: {colour}; }}" if self.state in (SUCCESS, FAILED) else "")
        running = self.state in (IDLE, RUNNING)
        self.bar.setVisible(running)
        self.size_label.setVisible(running)
        self.lines.setVisible(not running and bool(self.lines.text()))
        self.hint.setVisible(not running and bool(self.hint.text()))
        has_details = self.state == FAILED and bool(self.details.toPlainText())
        self.details_btn.setVisible(has_details)
        self.details.setVisible(has_details and self.details_btn.isChecked())
        self.cancel_btn.setVisible(running and self.cancellable)
        self.cancel_btn.setEnabled(self.state == RUNNING and not self._cancel.is_set())
        self.ok_btn.setEnabled(not running)
        self.ok_btn.setText("OK" if self.state != FAILED else "Close")
        if not running:
            self.ok_btn.setFocus()
        self._resize()

    # ------------------------------------------------------------------ running
    @property
    def finished_state(self) -> bool:
        return self.state in (SUCCESS, FAILED, CANCELLED)

    def start(self) -> bool:
        """Start the task. False (and nothing happens) when it was started before: a second click cannot run it twice."""
        if self.state != IDLE:
            return False
        self.state = RUNNING
        self._apply_state()
        try:
            self.job = self.jobs.start(self.job_name, self._run, self._on_result, self._on_error, on_progress=self._on_progress)
        except Exception as exc:                               # noqa: BLE001 - e.g. no thread could be started
            log.warning("could not start the task %s: %s", self.title_text, exc, exc_info=True)
            self._finish(self._failed_outcome(exc))
        return True

    def _run(self, job):
        """In the worker thread. Never raises: whatever happens is handed back as a value."""
        try:
            return self._work(TaskContext(job, self._cancel))
        except BaseException as exc:                           # noqa: BLE001 - also SystemExit: the dialog must always end
            if isinstance(exc, ProviderError):
                log.info("task %s failed: %s", self.title_text, exc)
            else:
                log.warning("task %s failed unexpectedly: %s", self.title_text, exc, exc_info=True)
            return exc

    def _on_progress(self, item) -> None:
        if not self._usable() or self._cancel.is_set():
            return
        kind = item[0]
        if kind == "message":
            self.message.setText(logic.one_line(item[1], 400))
            if item[2]:
                self.bar.setRange(0, 0)
                self.size_label.setText("")
        elif kind == "progress":
            done, total = item[1], item[2]
            self.size_label.setText(size_text(done, total))
            if total:
                self.bar.setRange(0, PROGRESS_STEPS)
                self.bar.setValue(min(PROGRESS_STEPS, done * PROGRESS_STEPS // total))
            else:
                self.bar.setRange(0, 0)

    def _on_result(self, value) -> None:
        if not self._usable():
            return
        if isinstance(value, BaseException):
            outcome = self._cancelled_outcome() if self._cancel.is_set() else self._failed_outcome(value)
        elif isinstance(value, TaskOutcome):
            outcome = value
            if not outcome.ok and not outcome.hint:
                outcome.hint = self.hint_text
        elif isinstance(value, str) and value:
            outcome = TaskOutcome.success(value)
        else:
            outcome = TaskOutcome.success(self.title_text + " - done.")
        self._finish(outcome)

    def _on_error(self, message: str) -> None:
        """(JobGroup reports a failure here only if ``_run`` itself failed, which it cannot; this is the safety net.)"""
        if self._usable():
            self._finish(TaskOutcome.failure(str(message) or "It did not work.", self.hint_text))

    def _usable(self) -> bool:
        return _alive(self) and not self._detached and self.state == RUNNING

    def _failed_outcome(self, exc: BaseException) -> TaskOutcome:
        return TaskOutcome.failure(logic.one_line(describe_error(exc), 400), self.hint_text, technical_text(exc))

    def _cancelled_outcome(self) -> TaskOutcome:
        return TaskOutcome(False, self.cancelled_text, cancelled=True)

    # ------------------------------------------------------------------ cancelling and closing
    def request_cancel(self) -> bool:
        """Ask the task to stop (it polls ``ctx.cancelled()``). False when it cannot be cancelled or already was asked."""
        if self.state != RUNNING or not self.cancellable or self._cancel.is_set():
            return False
        self._cancel.set()
        self.message.setText("Cancelling…")
        self.bar.setRange(0, 0)
        self.size_label.setText("")
        self.cancel_btn.setEnabled(False)
        return True

    def abort(self) -> None:
        """The window is closing: stop the task if it can be stopped, drop its result and go away without a word."""
        self._cancel.set()
        self._detached = True
        if self.job is not None:
            self.job.cancel()
        if _alive(self):
            self.hide()
            self.deleteLater()

    def reject(self) -> None:
        """Esc and the close button. While running this is Cancel (the dialog closes when the task has stopped); a task that
        cannot be cancelled keeps the dialog open until it is done. Once it is finished, it closes."""
        if self.state == RUNNING:
            if self._cancel.is_set():
                self._detach()                                  # a second try while it is still stopping: let the user go
                return
            if self.request_cancel():
                self._close_when_done = True
            else:
                self.message.setToolTip("Please wait a moment: this cannot be stopped half way, and it is short.")
            return
        super().reject()

    def accept(self) -> None:
        if self.state == RUNNING:
            return
        super().accept()

    @guarded_event
    def closeEvent(self, event):
        if self.state == RUNNING and not self._detached:
            event.ignore()
            self.reject()
            return
        super().closeEvent(event)

    def _detach(self) -> None:
        """Closing while the task is still stopping: the result is dropped; the task cleans up behind the closed dialog."""
        self._detached = True
        outcome = self._cancelled_outcome()
        self.outcome, self.state = outcome, CANCELLED
        self._emit(outcome)
        QtWidgets.QDialog.reject(self)

    # ------------------------------------------------------------------ the final state
    def _finish(self, outcome: TaskOutcome) -> None:
        self.outcome = outcome
        self.state = SUCCESS if outcome.ok else CANCELLED if outcome.cancelled else FAILED
        self.heading.setText({SUCCESS: logic.one_line(outcome.summary, 300), FAILED: self.failed_title,
                              CANCELLED: "Cancelled"}[self.state])
        self.lines.setText("\n".join(logic.one_line(line, 300) for line in outcome.details))
        self.message.setText(logic.one_line(outcome.summary, 400) if self.state != SUCCESS else "")
        self.message.setVisible(bool(self.message.text()))
        self.hint.setText(logic.one_line(outcome.hint, 400) if outcome.hint else "")
        self.details.setPlainText(outcome.technical)
        self._final_at.start()
        self.setWindowTitle(self.title_text)
        self._apply_state()
        self._emit(outcome)
        if self._close_when_done:
            self.accept()

    def _emit(self, outcome: TaskOutcome) -> None:
        if not self._emitted:
            self._emitted = True
            try:
                self.finished_with.emit(outcome)
            except Exception as exc:                            # noqa: BLE001
                log.warning("task listener failed: %s", exc, exc_info=True)

    def _too_soon(self) -> bool:
        """A result that appears at once (a reload takes a moment) must not be dismissed by the second click of a double click, or
        by an Enter that is still held down: real input is ignored for a moment after the result shows. (``button.click()`` in
        code is not affected.)"""
        return self._final_at.isValid() and self._final_at.elapsed() < SETTLE_MS

    def eventFilter(self, obj, event) -> bool:
        if obj is self.ok_btn and self._too_soon() and event.type() in (
                QtCore.QEvent.MouseButtonPress, QtCore.QEvent.MouseButtonRelease, QtCore.QEvent.MouseButtonDblClick,
                QtCore.QEvent.KeyPress, QtCore.QEvent.KeyRelease):
            return True
        return super().eventFilter(obj, event)

    @guarded_event
    def keyPressEvent(self, event):
        if event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter) and (self.state == RUNNING or self._too_soon()):
            event.accept()                                      # Enter must not close a dialog that is working, or one that just finished
            return
        super().keyPressEvent(event)
