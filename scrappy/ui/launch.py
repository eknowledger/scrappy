"""Start an external program (video player, browser) without ever blocking the window. No Qt imports.

The program's error output goes to the application log instead of being thrown away (so "the preview did nothing" can be
diagnosed from ``scrappy.log``), and the process is always waited for by a small background thread, so no zombie is left
behind, even when it was started by a window that has already closed.
"""
from __future__ import annotations

import logging
import subprocess
import threading

log = logging.getLogger("scrappy.ui")

MAX_LOGGED_LINES = 200


def launch(argv, name: str):
    """Popen `argv` detached from the keyboard, with stderr logged under `name`. Raises OSError if it cannot start.
    The returned Popen has ``log_thread`` and ``reaper`` threads (daemon) and ``last_error`` (last stderr line) attached."""
    proc = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            start_new_session=True)

    proc.last_error = ""                                  # the last non-empty error line, for the window to show

    def read_errors() -> None:
        count = 0
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", "replace").rstrip()[:300]
                if line.strip():
                    proc.last_error = line.strip()
                if count < MAX_LOGGED_LINES:
                    log.info("%s: %s", name, line)
                    count += 1
        except (OSError, ValueError):
            pass
        finally:
            try:
                proc.stderr.close()
            except OSError:
                pass

    def reap() -> None:
        try:
            proc.wait()
        except OSError:
            pass

    proc.log_thread = threading.Thread(target=read_errors, name=f"scrappy-{name}-log", daemon=True)
    proc.reaper = threading.Thread(target=reap, name=f"scrappy-{name}-wait", daemon=True)
    proc.log_thread.start()
    proc.reaper.start()
    return proc
