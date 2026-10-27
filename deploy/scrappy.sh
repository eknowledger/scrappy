#!/bin/bash
# Starts Scrappy on the Batocera desktop (installed by deploy/install.sh; the shortcuts run this file).
#
# Why plain `python3 -m scrappy.app` and NOT `python3 -I`: -I (isolated mode) ignores PYTHONPATH and does not put the
# current folder on sys.path, so `-m scrappy.app` could not find the package that lives next to this script. Instead we
# cd into the install folder and set PYTHONPATH (vendor/ first, then the install folder) explicitly; that is the same isolation that matters here.
#
# Overrides (tests and special setups): SCRAPPY_HOME (install folder), SCRAPPY_LOG (log file), SCRAPPY_PYTHON (interpreter).
APP_DIR=${SCRAPPY_HOME:-/userdata/system/scrappy}
LOG=${SCRAPPY_LOG:-/userdata/system/logs/scrappy.log}
LOG_MAX_BYTES=1000000          # rotate when the log is bigger than this ...
LOG_KEEP=3                     # ... keeping scrappy.log.1 .. .3

cd "$APP_DIR" || { echo "Scrappy is not installed in $APP_DIR" >&2; exit 1; }

mkdir -p "$(dirname "$LOG")" 2>/dev/null
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG" 2>/dev/null || echo 0)" -gt "$LOG_MAX_BYTES" ]; then
    i=$LOG_KEEP
    while [ "$i" -gt 1 ]; do
        prev=$((i - 1))
        [ -f "$LOG.$prev" ] && mv -f "$LOG.$prev" "$LOG.$i"
        i=$prev
    done
    mv -f "$LOG" "$LOG.1"
fi

# From the desktop DISPLAY is already set; from an ssh session it is not, and the box's X server is on :0.0.
[ -z "$DISPLAY" ] && export DISPLAY=:0.0
export QT_QPA_PLATFORM=xcb
# vendor/ FIRST: it holds a Pillow that can read and write JPEG (the system Pillow cannot on this box). To run a tool by
# hand use the same path, e.g.
#   cd /userdata/system/scrappy && PYTHONPATH=/userdata/system/scrappy/vendor:/userdata/system/scrappy \
#       QT_QPA_PLATFORM=offscreen python3 tools/ui_smoke.py
export PYTHONPATH="$APP_DIR/vendor:$APP_DIR"
export PYTHONUNBUFFERED=1

PY=${SCRAPPY_PYTHON:-$(command -v python3 || command -v python)}
if [ -z "$PY" ]; then
    echo "$(date '+%F %T') no python3 found" >> "$LOG"
    exit 1
fi

{
    echo "==== $(date '+%F %T') starting: $PY -m scrappy.app $* (DISPLAY=$DISPLAY) ===="
} >> "$LOG" 2>&1
exec "$PY" -m scrappy.app "$@" >> "$LOG" 2>&1
