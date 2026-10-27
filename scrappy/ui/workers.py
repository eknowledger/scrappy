"""Run blocking work off the GUI thread and deliver results back on it (the Qt side; the logic is in ``jobs``)."""
from __future__ import annotations

from PyQt5 import QtCore

from .jobs import Job, JobGroup, report_exception, safe  # noqa: F401 - re-exported for the widgets


class _Dispatcher(QtCore.QObject):
    """Lives on the GUI thread. A signal emitted from any thread is queued and runs ``fn`` here."""
    call = QtCore.pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(self._invoke, QtCore.Qt.QueuedConnection)

    @QtCore.pyqtSlot(object)
    def _invoke(self, fn):
        try:
            fn()
        except Exception as exc:                          # noqa: BLE001 - an exception out of a slot would abort the program
            report_exception(exc, "a background result")


_dispatcher = None


def ensure_dispatcher() -> _Dispatcher:
    """Create the dispatcher once, from the GUI thread (the window does this before starting any job)."""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = _Dispatcher()
    return _dispatcher


def post(fn) -> None:
    """Run `fn` on the GUI thread (callable from any thread once ``ensure_dispatcher`` ran)."""
    ensure_dispatcher().call.emit(fn)


def new_group(on_change=None) -> JobGroup:
    ensure_dispatcher()
    return JobGroup(post, on_change)
