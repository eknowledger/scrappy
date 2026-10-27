"""Background jobs and the rule that no error may escape a Qt slot. No Qt imports, so it is tested on any machine.

``JobGroup.start`` runs ``fn(job)`` on a daemon thread and hands the outcome back through ``post`` (in the window,
``post`` queues the call onto the GUI thread). ``fn`` gets its ``Job``: ``job.cancelled`` is the callable the controller
methods take as ``cancelled=``, ``job.notify(text)`` posts a progress text, and ``job.enter_critical()`` marks the point
of no return (a save that has started writing cannot be cancelled, so the window will not close under it).

Results of cancelled jobs are dropped. Whatever happens, ``on_change`` runs on the GUI thread when a job ends, so busy
indicators and buttons can be refreshed from one place.
"""
from __future__ import annotations

import functools
import logging
import threading
from typing import Callable, Optional

from ..providers.base import ProviderError, describe_error

log = logging.getLogger("scrappy.ui")

_reporter: Optional[Callable[[str], None]] = None


def set_reporter(fn: Optional[Callable[[str], None]]) -> None:
    """Where unexpected errors from slots are shown (the window's status line)."""
    global _reporter
    _reporter = fn


def report_exception(exc: BaseException, where: str = "") -> None:
    """Log an unexpected error and show it plainly. Never raises (this is the last line of defence)."""
    try:
        log.error("Unexpected error%s: %s", f" in {where}" if where else "", exc, exc_info=(type(exc), exc, exc.__traceback__))
        if _reporter is not None:
            _reporter("Something went wrong inside the program: " + describe_error(exc)
                      + ". You can carry on; if it repeats, restart the program.")
    except Exception:                                              # noqa: BLE001
        pass


def safe(fn: Callable, nargs: int = 0) -> Callable:
    """Wrap a slot: signal arguments beyond `nargs` are dropped (Qt passes e.g. `checked`), exceptions are reported."""
    @functools.wraps(fn)
    def wrapper(*args):
        try:
            return fn(*args[:nargs])
        except Exception as exc:                                   # noqa: BLE001
            report_exception(exc, getattr(fn, "__name__", "slot"))
            return None
    return wrapper


def guarded_event(fn: Callable) -> Callable:
    """Decorator for Qt event overrides (paintEvent, dropEvent, ...): an exception is reported, not raised into Qt."""
    @functools.wraps(fn)
    def wrapper(self, event, *rest):
        try:
            return fn(self, event, *rest)
        except Exception as exc:                                   # noqa: BLE001
            report_exception(exc, fn.__name__)
            return None
    return wrapper


class Job:
    def __init__(self, name: str = "", post: Optional[Callable] = None, on_progress: Optional[Callable] = None):
        self.name = name
        self._post, self._on_progress = post, on_progress
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._critical = False
        self.finished = False                                      # set on the GUI thread when the outcome was delivered

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self) -> bool:
        """Ask the job to stop. False if it already passed the point of no return."""
        with self._lock:
            if self._critical:
                return False
            self._cancel.set()
            return True

    def enter_critical(self) -> bool:
        """Called by the job itself before something that must not be interrupted. False if it was cancelled already."""
        with self._lock:
            if self._cancel.is_set():
                return False
            self._critical = True
            return True

    @property
    def critical(self) -> bool:
        return self._critical

    def notify(self, text: str) -> None:
        """Progress text for the GUI (called from the job's thread)."""
        if self._post is not None and self._on_progress is not None and not self.cancelled():
            try:
                self._post(lambda: None if self.cancelled() else self._on_progress(text))
            except Exception:                                      # noqa: BLE001
                pass


def _thread_spawn(target: Callable, name: str) -> None:
    threading.Thread(target=target, name=name, daemon=True).start()


class JobGroup:
    """All jobs of one window. Use from the GUI thread only."""

    def __init__(self, post: Callable, on_change: Optional[Callable[[], None]] = None,
                 spawn: Callable = _thread_spawn, describe: Callable = describe_error):
        self._post, self._on_change, self._spawn, self._describe = post, on_change, spawn, describe
        self._jobs: list = []

    def start(self, name: str, fn: Callable, on_result: Callable, on_error: Optional[Callable] = None,
              on_progress: Optional[Callable] = None, label: str = "") -> Job:
        """`label` (e.g. the candidate's name) is added to the log line when the job fails."""
        job = Job(name, self._post, on_progress)
        self._jobs.append(job)

        def deliver(ok: bool, value) -> None:
            job.finished = True
            if job in self._jobs:
                self._jobs.remove(job)
            try:
                if not job.cancelled():
                    if ok:
                        on_result(value)
                    elif on_error is not None:
                        on_error(value)
                    elif _reporter is not None:                    # nobody asked for the failure: still tell the user
                        _reporter(f"{name} failed: {value}")
            except Exception as exc:                               # noqa: BLE001
                report_exception(exc, f"the {name} callback")
            if self._on_change is not None:
                try:
                    self._on_change()
                except Exception as exc:                           # noqa: BLE001
                    report_exception(exc, "on_change")

        def run() -> None:
            try:
                value = fn(job)
                ok = True
            except Exception as exc:                               # noqa: BLE001 - every failure becomes a plain message
                what = f"{name} ({label})" if label else name
                if isinstance(exc, ProviderError):                 # a plain, expected failure: the message is enough
                    log.info("job %s failed: %s", what, exc)
                else:
                    log.warning("job %s failed unexpectedly: %s", what, exc, exc_info=True)
                value, ok = self._describe(exc), False
            try:
                self._post(lambda: deliver(ok, value))
            except Exception:                                      # noqa: BLE001 - the GUI is gone
                pass

        self._spawn(run, f"scrappy-{name}")
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception as exc:                               # noqa: BLE001
                report_exception(exc, "on_change")
        return job

    def running(self, name: str) -> Optional[Job]:
        """The newest job called `name` that has not been cancelled, if any."""
        for job in reversed(self._jobs):
            if job.name == name and not job.cancelled():
                return job
        return None

    def active(self) -> list:
        return [j for j in self._jobs if not j.cancelled()]

    def cancel(self, name: str) -> None:
        for job in list(self._jobs):
            if job.name == name:
                job.cancel()

    def cancel_all(self) -> list:
        """Cancel everything that can be cancelled; returns the jobs that cannot be (a save that is writing)."""
        return [j for j in list(self._jobs) if not j.cancel()]
