#!/bin/bash
# Installs (or removes) Scrappy on the Batocera. Run it on the MAC, from anywhere:
#
#   deploy/install.sh [--dry-run] [HOST]                  install / update      (HOST default: root@batocera.local)
#   deploy/install.sh --uninstall [--dry-run] [HOST]      remove the app and its shortcuts, KEEP the user data
#   deploy/install.sh --uninstall --purge --yes [HOST]    remove everything, including backups/ and journal/
#
# Options: --dry-run | -n   print what would happen; nothing is sent to or changed on the Batocera
#          --host HOST      the same as the positional HOST (the SCRAPPY_HOST environment variable also works)
#          SCRAPPY_VENDOR_FILE / SCRAPPY_WHEEL_CACHE (environment)  another vendor.txt / another wheel cache folder (tests)
#          SCRAPPY_ARCH (environment)  x86_64, aarch64 or arm64: the machine architecture ASSUMED BY --dry-run (default x86_64),
#                   because a dry run makes no ssh connection. A real install ignores it and always asks the machine.
#          --no-probe       do not run tools/probe_environment.py at the end of an install
#          --min-free-mb N  require more than the default 200 MB free on /userdata
#
# What an install does (every remote step is a short POSIX sh script sent over ssh; read them below):
#   0. asks the machine for its architecture (`uname -m`, read-only: x86_64 or aarch64/arm64; anything else stops here,
#      before anything is built or uploaded), then fetches the Pillow wheel pinned for that architecture (deploy/vendor.txt)
#      into ~/.cache/scrappy/wheels (never into git) and refuses it unless its sha256 equals the pin; the box's own
#      Pillow cannot do JPEG, so a working one ships in vendor/
#   1. builds a tarball of scrappy/ + tools/ + launcher + desktop file + icon + MANIFEST.sha256 + the wheel (no tests, .venv, docs, caches)
#   2. REFUSES to continue if the Batocera has less than 200 MB free on /userdata
#   3. uploads the tarball and compares its sha256; extracts it into a NEW staging folder inside the install folder;
#      checks the manifest, that every .py compiles on the box's Python and that `import scrappy` works, unpacks the wheel with
#      python's zipfile (validated member paths, only PIL, pillow.libs and the dist-info) into vendor/ and runs a REAL JPEG
#      round trip (save, open, pixel check) with that Pillow - all BEFORE touching the installed version
#   4. swaps scrappy/, tools/ and vendor/ in (renames; the old ones stay as scrappy.previous/ etc.; a failure rolls back)
#   5. installs the three shortcuts exactly like the proven Eden-0.2.1-config shortcut
#   6. verifies the result (manifest, import, file count, launcher executable) and runs the read-only probe
#
#   7. an install under the OLD name (game-asset-fixer, before the project was renamed to Scrappy) is migrated, after the
#      new version is in place and verified: config.json, backups/, journal/, cache/ and bin/ are MOVED into the new
#      folder (each only if the new folder does not already have it, nothing is ever overwritten; an Undo of a save made by
#      the old version still works), the old shortcuts are removed, and the whole old folder is renamed to
#      /userdata/system/game-asset-fixer.old-<timestamp> (a backup that is never deleted). It refuses to start while the
#      old program is running. The old launcher log files stay where they are. With no old install this step does nothing.
#
# NEVER touched by install or --uninstall (user data): bin/ cache/ backups/ journal/ logs/ config.json (and anything else
# that is not part of the app). Only --uninstall --purge --yes deletes them. (The migration in step 7 moves that data
# from the old folder into the new one; it deletes nothing.) --uninstall leaves an old-name install and its
# .old-<timestamp> backups alone and says so.
#
# Written for macOS bash 3.2 (no associative arrays, no ${var,,}, no mapfile) and for POSIX sh on the Batocera.

set -eu
set -o pipefail

# ---------------------------------------------------------------- constants (the Batocera side)
DEST=/userdata/system/scrappy
APPS_DIR=/userdata/system/.local/share/applications     # persistent: survives reboots
DESKTOP_DIR=/userdata/system/Desktop                    # the icon on the desktop
SYS_APPS_DIR=/usr/share/applications                    # immediate; rebuilt at boot, so only a convenience
ENTRY_NAME=scrappy.desktop                     # in APPS_DIR and SYS_APPS_DIR
DESKTOP_NAME=scrappy.desktop                   # in DESKTOP_DIR
MIN_FREE_MB_FLOOR=200
# the names before the project was renamed (only the migration step and the uninstall note read these)
OLD_DEST=/userdata/system/game-asset-fixer
OLD_ENTRY_NAME=game-asset-fixer.desktop                 # in APPS_DIR and SYS_APPS_DIR (what the old installer wrote)
OLD_DESKTOP_NAME=Game-Asset-Fixer.desktop               # in DESKTOP_DIR (what the old installer wrote there)
VENDOR_FILE=${SCRAPPY_VENDOR_FILE:-}        # default: deploy/vendor.txt (set below, once ROOT is known)
WHEEL_CACHE=${SCRAPPY_WHEEL_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/scrappy/wheels}

# ---------------------------------------------------------------- options
HOST=${SCRAPPY_HOST:-root@batocera.local}
DRY=0
UNINSTALL=0
PURGE=0
YES=0
PROBE=1
MIN_FREE_MB=$MIN_FREE_MB_FLOOR
SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=10"

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//; /^$/d'
}

while [ $# -gt 0 ]; do
    case $1 in
        --dry-run|-n)    DRY=1 ;;
        --uninstall)     UNINSTALL=1 ;;
        --purge)         PURGE=1 ;;
        --yes|-y)        YES=1 ;;
        --no-probe)      PROBE=0 ;;
        --host)          [ $# -ge 2 ] || die "--host needs a value"; HOST=$2; shift ;;
        --min-free-mb)   [ $# -ge 2 ] || die "--min-free-mb needs a value"; MIN_FREE_MB=$2; shift ;;
        -h|--help)       usage; exit 0 ;;
        -*)              die "unknown option: $1 (see --help)" ;;
        *)               HOST=$1 ;;
    esac
    shift
done

case $HOST in
    ''|-*|*[!A-Za-z0-9._@:-]*) die "that does not look like [user@]host: '$HOST'" ;;
esac
case $MIN_FREE_MB in
    ''|*[!0-9]*) die "--min-free-mb must be a whole number" ;;
esac
[ "$MIN_FREE_MB" -ge "$MIN_FREE_MB_FLOOR" ] || die "--min-free-mb cannot go below $MIN_FREE_MB_FLOOR"
[ "$PURGE" = 0 ] || [ "$UNINSTALL" = 1 ] || die "--purge only makes sense together with --uninstall"
if [ "$PURGE" = 1 ] && [ "$YES" = 0 ] && [ "$DRY" = 0 ]; then
    die "--purge deletes backups/ and journal/ (the only way to Undo a save) and config.json; add --yes if you mean it"
fi

ROOT=$(cd "$(dirname "$0")/.." && pwd)
[ -n "$VENDOR_FILE" ] || VENDOR_FILE=$ROOT/deploy/vendor.txt
TS=$(date +%Y%m%d-%H%M%S)-$$
STAGE=$DEST/.staging-$TS
STAGE_TGZ=$STAGE.tar.gz
PKG_DIR=""
CLEAN_REMOTE=0
PRELUDE_NOTE=""

# ---------------------------------------------------------------- local helpers
sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

# ---------------------------------------------------------------- the machine architecture (read-only)
# ARCH is the architecture of the target, in the words of the wheel platform tags: x86_64 or aarch64.
ARCH=""

normalize_arch() {   # uname -m / SCRAPPY_ARCH value -> x86_64 | aarch64 | (nothing when unsupported)
    case $1 in
        x86_64)          echo x86_64 ;;
        aarch64|arm64)   echo aarch64 ;;
    esac
}

clean_text() {   # a value from the machine, safe to print (no control characters)
    printf '%s' "$1" | tr -cd 'A-Za-z0-9_.-' | cut -c1-40
}

# A real run asks the machine (`uname -m` over the usual ssh options; SCRAPPY_ARCH is ignored, an override could ship a wrong
# wheel). A dry run makes no connection, so it uses SCRAPPY_ARCH (default x86_64) and says so.
detect_arch() {
    step "Find out the architecture of the machine (decides which Pillow wheel is shipped)"
    if [ "$DRY" = 1 ]; then
        want=${SCRAPPY_ARCH:-x86_64}
        ARCH=$(normalize_arch "$want")
        [ -n "$ARCH" ] || die "SCRAPPY_ARCH='$(clean_text "$want")' is not a supported architecture (x86_64, aarch64 or arm64)"
        say "[dry-run] would run: ssh $SSH_OPTS $HOST uname -m   (read-only; a dry run makes no connection)"
        say "architecture: $ARCH (taken from SCRAPPY_ARCH, default x86_64; NOT read from $HOST)"
        return 0
    fi
    [ -z "${SCRAPPY_ARCH:-}" ] || say "note: SCRAPPY_ARCH is set but is ignored in a real run: the machine is always asked"
    # shellcheck disable=SC2086   # SSH_OPTS is a list of words on purpose
    raw=$(ssh $SSH_OPTS "$HOST" 'uname -m' </dev/null) || die "step failed on $HOST: read the machine architecture (uname -m)"
    raw=$(printf '%s\n' "$raw" | awk 'NF {print $1; exit}')
    ARCH=$(normalize_arch "$raw")
    if [ -z "$ARCH" ]; then
        die "$HOST reports the architecture '$(clean_text "$raw")', and no Pillow wheel is pinned for it in deploy/vendor.txt (only x86_64 and aarch64 are); nothing was changed"
    fi
    say "architecture: $ARCH (read from $HOST with uname -m: '$(clean_text "$raw")')"
}

# ---------------------------------------------------------------- the vendored Pillow wheel (local)
# The architecture a pin belongs to is the platform tag at the end of its wheel file name (x86_64 or aarch64).
wheel_arch() {   # <wheel file name> -> x86_64 | aarch64 | (nothing when the tag has neither, or both)
    plat=${1%.whl}
    plat=${plat##*-}
    is_x=0; is_a=0
    case $plat in *x86_64*)  is_x=1 ;; esac
    case $plat in *aarch64*) is_a=1 ;; esac
    if [ "$is_x" = 1 ] && [ "$is_a" = 0 ]; then echo x86_64; fi
    if [ "$is_a" = 1 ] && [ "$is_x" = 0 ]; then echo aarch64; fi
}

# Reads vendor.txt (every pin line is checked, one pin per architecture), takes the pin for ARCH, finds or downloads the
# wheel in WHEEL_CACHE and checks its sha256 -> WHEEL_PATH WHEEL_NAME WHEEL_SHA. A cached file with the wrong hash is
# deleted and fetched again; a downloaded one that does not match the pin is deleted and the install stops.
fetch_wheel() {
    [ -f "$VENDOR_FILE" ] || die "vendor pin file not found: $VENDOR_FILE"
    [ -n "$ARCH" ] || die "internal error: the architecture is not known yet"
    pins=$(grep -v '^[[:space:]]*#' "$VENDOR_FILE" | awk 'NF' || true)
    [ -n "$pins" ] || die "no wheel is pinned in $VENDOR_FILE"
    WHEEL_SHA=""; WHEEL_NAME=""; wheel_url=""
    seen=" "
    while IFS= read -r pin; do
        p_sha=$(printf '%s\n' "$pin" | awk '{print $1}')
        p_name=$(printf '%s\n' "$pin" | awk '{print $2}')
        p_url=$(printf '%s\n' "$pin" | awk '{print $3}')
        case $p_sha in
            *[!0-9a-f]*|'') die "bad sha256 in $VENDOR_FILE" ;;
        esac
        [ "${#p_sha}" = 64 ] || die "bad sha256 in $VENDOR_FILE (not 64 hex digits)"
        case $p_name in
            ''|*[!A-Za-z0-9._+-]*|*/*) die "bad wheel file name in $VENDOR_FILE: '$p_name'" ;;
            *.whl) ;;
            *) die "the pinned file is not a .whl: $p_name" ;;
        esac
        case $p_url in
            https://files.pythonhosted.org/*) ;;
            *) die "the wheel URL must start with https://files.pythonhosted.org/ (in $VENDOR_FILE)" ;;
        esac
        p_arch=$(wheel_arch "$p_name")
        [ -n "$p_arch" ] || die "cannot tell the architecture of the pinned wheel $p_name in $VENDOR_FILE (its platform tag must name x86_64 or aarch64)"
        case $seen in *" $p_arch "*) die "two wheels are pinned for $p_arch in $VENDOR_FILE (one per architecture)" ;; esac
        seen="$seen$p_arch "
        if [ "$p_arch" = "$ARCH" ]; then
            WHEEL_SHA=$p_sha; WHEEL_NAME=$p_name; wheel_url=$p_url
        fi
    done <<EOF
$pins
EOF
    [ -n "$WHEEL_NAME" ] || die "no Pillow wheel is pinned for $ARCH in $VENDOR_FILE; nothing was changed"
    say "pinned wheel for $ARCH: $WHEEL_NAME"
    WHEEL_PATH=$WHEEL_CACHE/$WHEEL_NAME
    mkdir -p "$WHEEL_CACHE"
    if [ -f "$WHEEL_PATH" ]; then
        if [ "$(sha256_of "$WHEEL_PATH")" = "$WHEEL_SHA" ]; then
            say "vendored wheel: $WHEEL_PATH (cached, sha256 matches the pin)"
            return 0
        fi
        say "the cached $WHEEL_NAME does not match the pin; deleting it and downloading again"
        rm -f "$WHEEL_PATH"
    fi
    say "downloading $wheel_url"
    part=$WHEEL_PATH.part.$$
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --retry 3 --connect-timeout 15 --max-time 600 -o "$part" "$wheel_url" || { rm -f "$part"; die "download failed: $wheel_url"; }
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' "$wheel_url" "$part" \
            || { rm -f "$part"; die "download failed: $wheel_url"; }
    else
        die "neither curl nor python3 found to download $wheel_url (or put the file in $WHEEL_CACHE yourself)"
    fi
    got=$(sha256_of "$part")
    if [ "$got" != "$WHEEL_SHA" ]; then
        rm -f "$part"
        die "the downloaded wheel has sha256 $got but the pin says $WHEEL_SHA; refusing it (nothing was changed on the box)"
    fi
    mv "$part" "$WHEEL_PATH"
    say "vendored wheel: $WHEEL_PATH (downloaded, sha256 matches the pin)"
}

# ---------------------------------------------------------------- remote scripts
# Each function prints one POSIX sh script. The values it needs arrive from remote_prelude. Scripts are fed to `sh -s`
# on the Batocera, so nothing in them may read standard input (python and tar get </dev/null).

remote_prelude() {
    printf "DEST='%s'\nAPPS_DIR='%s'\nDESKTOP_DIR='%s'\nSYS_APPS_DIR='%s'\n" "$DEST" "$APPS_DIR" "$DESKTOP_DIR" "$SYS_APPS_DIR"
    printf "ENTRY_NAME='%s'\nDESKTOP_NAME='%s'\nSTAGE='%s'\nSTAGE_TGZ='%s'\n" "$ENTRY_NAME" "$DESKTOP_NAME" "$STAGE" "$STAGE_TGZ"
    printf "MIN_FREE_MB='%s'\nEXPECT_SHA='%s'\nPURGE='%s'\nRUN_PROBE='%s'\n" "$MIN_FREE_MB" "${TARBALL_SHA:-}" "$PURGE" "$PROBE"
    printf "WHEEL_NAME='%s'\nWHEEL_SHA='%s'\n" "${WHEEL_NAME:-}" "${WHEEL_SHA:-}"
    printf "OLD_DEST='%s'\nOLD_ENTRY_NAME='%s'\nOLD_DESKTOP_NAME='%s'\nTS='%s'\n" "$OLD_DEST" "$OLD_ENTRY_NAME" "$OLD_DESKTOP_NAME" "$TS"
    cat <<'EOF'
set -eu
die() { echo "ERROR: $*" >&2; exit 1; }
PY=$(command -v python3 || command -v python || true)
EOF
}

script_preflight() {
    cat <<'EOF'
[ -d /userdata/system ] || die "/userdata/system not found: is this a Batocera?"
for tool in tar gzip sha256sum awk df; do
    command -v "$tool" >/dev/null 2>&1 || die "'$tool' is missing on this machine"
done
[ -n "$PY" ] || die "no python3 (or python) found on this machine"
free=$(df -Pm /userdata | awk 'NR==2 {print $4}')
case $free in ''|*[!0-9]*) die "cannot read the free space of /userdata" ;; esac
echo "free space on /userdata: $free MB (need at least $MIN_FREE_MB MB)"
[ "$free" -ge "$MIN_FREE_MB" ] || die "only $free MB free on /userdata, need at least $MIN_FREE_MB MB; nothing was changed"
echo "python: $($PY --version 2>&1) at $PY"
if [ -d "$DEST" ]; then echo "an existing install was found in $DEST (it will be kept as .previous)"; fi
if [ -d "$OLD_DEST" ] || [ -L "$OLD_DEST" ]; then
    echo "an install under the old name was found ($OLD_DEST): it will be migrated after the new version is in place (the old folder is kept as $OLD_DEST.old-<timestamp>)"
fi
EOF
}

script_stage() {
    cat <<'EOF'
cd "$DEST"
got=$(sha256sum "$STAGE_TGZ" | awk '{print $1}')
[ "$got" = "$EXPECT_SHA" ] || die "the uploaded tarball is damaged (sha256 $got, expected $EXPECT_SHA); nothing was changed"
echo "tarball sha256 matches: $got"
bad=$(tar -tzf "$STAGE_TGZ" </dev/null | grep -E '(^/|(^|/)\.\.(/|$))' || true)
[ -z "$bad" ] || die "the tarball has unsafe paths: $bad"
rm -rf "$STAGE"
mkdir "$STAGE"
tar -xzf "$STAGE_TGZ" -C "$STAGE" </dev/null
for f in scrappy/__init__.py scrappy/app.py tools/probe_environment.py wheels/extract_wheel.py "wheels/$WHEEL_NAME" scrappy.sh scrappy.desktop icon.png MANIFEST.sha256 VERSION; do
    [ -f "$STAGE/$f" ] || die "the tarball lacks $f"
done
(cd "$STAGE" && sha256sum -c MANIFEST.sha256 </dev/null >/dev/null) || die "MANIFEST.sha256 does not match the extracted files"
echo "manifest ok: $(wc -l < "$STAGE/MANIFEST.sha256" | tr -d ' ') files"
# From here on run from inside the staged copy: python puts the current folder first on sys.path, and in $DEST that
# would be the OLD scrappy/ (the import test would then pass or fail for the wrong version).
cd "$STAGE"
# every file must compile on THIS Python (the Mac may be newer); nothing is written
STAGE_DIR="$STAGE" "$PY" -c '
import os, sys
root = os.environ["STAGE_DIR"]
bad = 0
for base, dirs, files in os.walk(root):
    for name in files:
        if name.endswith(".py"):
            path = os.path.join(base, name)
            try:
                compile(open(path, "rb").read(), path, "exec")
            except SyntaxError as exc:
                bad += 1
                print("syntax error:", path, exc)
sys.exit(1 if bad else 0)
' </dev/null || die "some files do not compile with this Python; nothing was changed"
STAGE_DIR="$STAGE" PYTHONPATH="$STAGE" PYTHONDONTWRITEBYTECODE=1 "$PY" -c '
import scrappy, os
assert os.path.dirname(os.path.dirname(os.path.realpath(scrappy.__file__))) == os.path.realpath(os.environ["STAGE_DIR"]), scrappy.__file__
' </dev/null || die "'import scrappy' fails on the staged copy; nothing was changed"
echo "staged copy compiles and imports"
# The vendored Pillow: unpack into the staging folder (refuses unsafe member names), then a REAL JPEG round trip with it.
"$PY" "$STAGE/wheels/extract_wheel.py" "$STAGE/wheels/$WHEEL_NAME" "$STAGE/vendor" "$WHEEL_SHA" "$STAGE/VENDOR.sha256" </dev/null \
    || die "the vendored Pillow wheel was refused; nothing was changed"
(cd "$STAGE" && sha256sum -c VENDOR.sha256 </dev/null >/dev/null) || die "VENDOR.sha256 does not match the extracted vendor files"
rm -rf "$STAGE/wheels"
"$PY" tools/probe_environment.py --pillow-check --strict --vendor-dir "$STAGE/vendor" </dev/null \
    || die "the vendored Pillow does not work on this machine (JPEG round trip failed); nothing was changed"
EOF
}

script_swap() {
    cat <<'EOF'
cd "$DEST"
mkdir -p "$DEST/bin" "$DEST/cache" "$DEST/backups" "$DEST/journal" /userdata/system/logs
# 1. move the old app folders aside (kept as .previous), 2. move the new ones in; any failure puts the old ones back
for n in scrappy tools vendor; do
    rm -rf "$n.previous"
    if [ -e "$n" ]; then mv "$n" "$n.previous"; fi
done
rollback() {
    echo "ERROR: could not move the new version in; restoring the old one" >&2
    for n in scrappy tools vendor; do
        rm -rf "$n"
        if [ -e "$n.previous" ]; then mv "$n.previous" "$n"; fi
    done
    exit 1
}
for n in scrappy tools vendor; do
    mv "$STAGE/$n" "$n" || rollback
done
for f in scrappy.sh icon.png scrappy.desktop MANIFEST.sha256 VENDOR.sha256 VERSION; do
    cp "$STAGE/$f" "$DEST/.$f.new"
    case $f in *.sh) chmod 755 "$DEST/.$f.new" ;; esac
    mv -f "$DEST/.$f.new" "$DEST/$f"
done
echo "installed version: $(cat "$DEST/VERSION")"
EOF
}

script_shortcuts() {
    cat <<'EOF'
failed=0
put() {   # put <folder> <file name> <required: 1|0>
    dir=$1; name=$2; required=$3
    if mkdir -p "$dir" 2>/dev/null && cp "$DEST/scrappy.desktop" "$dir/$name.new" \
        && chmod +x "$dir/$name.new" && mv -f "$dir/$name.new" "$dir/$name"; then
        echo "shortcut: $dir/$name"
    else
        rm -f "$dir/$name.new" 2>/dev/null || true
        if [ "$required" = 1 ]; then echo "ERROR: could not write $dir/$name" >&2; failed=1
        else echo "WARN: could not write $dir/$name (it is only a convenience until the next reboot)"; fi
    fi
}
put "$APPS_DIR" "$ENTRY_NAME" 1
put "$DESKTOP_DIR" "$DESKTOP_NAME" 1
put "$SYS_APPS_DIR" "$ENTRY_NAME" 0
[ "$failed" = 0 ] || exit 1
EOF
}

script_verify() {
    cat <<'EOF'
cd "$DEST"
(sha256sum -c MANIFEST.sha256 </dev/null >/dev/null) || die "verification: installed files do not match MANIFEST.sha256"
have=$(find scrappy tools -type f ! -name '*.pyc' ! -path '*/__pycache__/*' | wc -l | tr -d ' ')
want=$(grep -cE ' (scrappy|tools)/' MANIFEST.sha256 || true)
[ "$have" = "$want" ] || die "verification: $have files installed, the manifest lists $want"
(sha256sum -c VENDOR.sha256 </dev/null >/dev/null) || die "verification: the vendored Pillow files do not match VENDOR.sha256"
PYTHONPATH="$DEST/vendor:$DEST" "$PY" -c 'import scrappy' </dev/null || die "verification: 'python3 -c import scrappy' fails in $DEST"
[ -x scrappy.sh ] || die "verification: scrappy.sh is not executable (does $DEST allow execute permission?)"
bash -n scrappy.sh 2>/dev/null || sh -n scrappy.sh || die "verification: the launcher has a syntax error"
for f in "$APPS_DIR/$ENTRY_NAME" "$DESKTOP_DIR/$DESKTOP_NAME"; do
    cmp -s "$f" "$DEST/scrappy.desktop" || die "verification: $f differs from the packaged shortcut"
done
"$PY" tools/probe_environment.py --pillow-check --strict --vendor-dir "$DEST/vendor" </dev/null \
    || die "verification: the installed vendored Pillow fails the JPEG round trip"
echo "files installed: $have (all match the manifest); import scrappy: ok; launcher executable: ok; shortcuts: ok"
echo "VERIFY-OK"
EOF
}

script_probe() {
    cat <<'EOF'
if [ "$RUN_PROBE" = 1 ]; then
    echo
    echo "---- environment probe (read-only) ----"
    (cd "$DEST" && "$PY" tools/probe_environment.py </dev/null) || true
fi
EOF
}

script_migrate() {
    cat <<'EOF'
# MIGRATE: moves an install under the old name (game-asset-fixer) to the new one. Runs after the new version is installed
# and verified, so a failed install never costs the old one. Nothing is deleted except the old shortcuts: user data is
# MOVED, the old folder is RENAMED. Safe to run again: once migrated there is nothing under the old name any more.
shortcuts="$APPS_DIR/$OLD_ENTRY_NAME $DESKTOP_DIR/$OLD_DESKTOP_NAME $SYS_APPS_DIR/$OLD_ENTRY_NAME"
found_dest=0; found_shortcut=0
if [ -e "$OLD_DEST" ] || [ -L "$OLD_DEST" ]; then found_dest=1; fi
for f in $shortcuts; do
    if [ -e "$f" ] || [ -L "$f" ]; then found_shortcut=1; fi
done
if [ "$found_dest" = 0 ] && [ "$found_shortcut" = 0 ]; then
    echo "no install under the old name found: nothing to migrate"
    exit 0
fi
[ "$OLD_DEST" != "$DEST" ] || die "refusing to migrate: the old and the new install folder are the same"
if [ "$found_dest" = 1 ] && { [ ! -d "$OLD_DEST" ] || [ -L "$OLD_DEST" ]; }; then
    die "$OLD_DEST is not a plain folder; nothing was changed. Look at it by hand"
fi
echo "found an install under the old name: migrating it to $DEST"
# 1. never move a folder out from under a running program (it would keep writing to the old place)
for d in /proc/[0-9]*; do
    [ -r "$d/cmdline" ] || continue
    if tr '\0' ' ' < "$d/cmdline" 2>/dev/null | grep -q 'gaf\.app'; then
        die "the program of the old install is still running (process ${d##*/}); close it, then run the installer again. The new version is installed; nothing of the old install was changed"
    fi
done
# 2. the user data moves into the new folder, but never over something that is already there (the empty folders the
#    installer just created do not count as something)
moved_config=0
move_item() {   # move_item <name>
    src=$OLD_DEST/$1; dst=$DEST/$1
    if [ ! -e "$src" ] && [ ! -L "$src" ]; then return 0; fi
    if [ -d "$src" ] && [ ! -L "$src" ] && [ -d "$dst" ] && [ ! -L "$dst" ] && [ -z "$(ls -A "$dst")" ]; then rmdir "$dst"; fi
    if [ -e "$dst" ] || [ -L "$dst" ]; then
        echo "kept the existing $dst; the old $src stays in the backup folder, merge it by hand if you want it"
        return 0
    fi
    mv "$src" "$dst" || die "could not move $src to $dst; nothing was deleted. Run the installer again"
    echo "moved $src -> $dst"
    if [ "$1" = config.json ]; then moved_config=1; fi
}
if [ "$found_dest" = 1 ]; then
    for item in config.json backups journal cache bin; do move_item "$item"; done
    # a setting that names the old data folder would send backups and the undo journal to the old place
    if [ "$moved_config" = 1 ] && grep -qF "$OLD_DEST" "$DEST/config.json"; then
        cp -p "$DEST/config.json" "$DEST/config.json.before-migration" || die "could not copy $DEST/config.json"
        sed "s#$OLD_DEST\([/\"]\)#$DEST\1#g" "$DEST/config.json" > "$DEST/.config.json.new" \
            && mv -f "$DEST/.config.json.new" "$DEST/config.json" || die "could not update $DEST/config.json"
        echo "config.json named the old data folder; it now names $DEST (the original text is kept as config.json.before-migration)"
    fi
    # 3. the whole old folder is kept as a backup next to the old place
    backup="$OLD_DEST.old-$TS"
    [ ! -e "$backup" ] || die "$backup exists already; the old folder was left as it is"
    mv "$OLD_DEST" "$backup" || die "could not move $OLD_DEST to $backup; your data was moved already, the old folder was left as it is. Run the installer again"
    echo "kept the old app folder as $backup (nothing was deleted; remove it yourself when you no longer need it)"
else
    echo "there is no old install folder ($OLD_DEST): only old shortcuts were left"
fi
# 4. the old shortcuts go (the new ones were installed and verified before this step)
for f in $shortcuts; do
    if [ -e "$f" ] || [ -L "$f" ]; then
        rm -f "$f" || die "could not remove the old shortcut $f; the old app folder was already dealt with above. Run the installer again"
        echo "removed the old shortcut $f"
    fi
done
for f in /userdata/system/logs/game-asset-fixer.log*; do
    if [ -e "$f" ]; then
        echo "left the old launcher log files (/userdata/system/logs/game-asset-fixer.log*) where they are"
        break
    fi
done
echo "MIGRATE-OK"
EOF
}

script_cleanup() {
    cat <<'EOF'
case $STAGE in "$DEST"/.staging-*) rm -rf "$STAGE" "$STAGE_TGZ" ;; esac
EOF
}

script_uninstall() {
    cat <<'EOF'
# the app itself
if [ -d "$DEST" ]; then
    cd "$DEST"
    rm -rf scrappy scrappy.previous tools tools.previous vendor vendor.previous
    rm -f scrappy.sh icon.png scrappy.desktop MANIFEST.sha256 VENDOR.sha256 VERSION
    rm -rf .staging-*
fi
# the shortcuts
rm -f "$APPS_DIR/$ENTRY_NAME" "$DESKTOP_DIR/$DESKTOP_NAME" "$SYS_APPS_DIR/$ENTRY_NAME"
echo "removed the app and its shortcuts"
if [ -d "$OLD_DEST" ] || [ -L "$OLD_DEST" ] || [ -e "$APPS_DIR/$OLD_ENTRY_NAME" ] || [ -e "$DESKTOP_DIR/$OLD_DESKTOP_NAME" ]; then
    echo "NOTE: an install under the old name ($OLD_DEST) exists and is left alone; run the installer without --uninstall to migrate it, or remove it by hand"
fi
for b in "$OLD_DEST".old-*; do
    if [ -e "$b" ]; then echo "NOTE: kept the backup of the old install: $b (never touched by this script)"; fi
done
if [ "$PURGE" = 1 ]; then
    [ "$DEST" = /userdata/system/scrappy ] || die "refusing to purge an unexpected folder: $DEST"
    rm -rf "$DEST"
    rm -f /userdata/system/logs/scrappy.log /userdata/system/logs/scrappy.log.1 \
          /userdata/system/logs/scrappy.log.2 /userdata/system/logs/scrappy.log.3
    echo "purged $DEST (user data, backups and journal included) and the launcher log"
else
    if [ -d "$DEST" ]; then
        if rmdir "$DEST" 2>/dev/null; then
            echo "$DEST was empty and is removed too"
        else
            echo "kept your data in $DEST (use --purge --yes to delete it too):"
            ls -A "$DEST" | sed 's/^/    /'
        fi
    fi
fi
EOF
}

# ---------------------------------------------------------------- running a remote step
# remote <description> <script function>
remote() {
    step "$1"
    if [ "$DRY" = 1 ]; then
        say "[dry-run] would run this on $HOST via 'ssh $SSH_OPTS $HOST sh -s'$PRELUDE_NOTE:"
        if [ -z "$PRELUDE_NOTE" ]; then
            remote_prelude | sed 's/^/    | /'
            PRELUDE_NOTE=" (after the same variable lines as above)"
        fi
        "$2" | sed 's/^/    | /'
        return 0
    fi
    # shellcheck disable=SC2086   # SSH_OPTS is a list of words on purpose
    { remote_prelude; "$2"; } | ssh $SSH_OPTS "$HOST" 'sh -s' || die "step failed on $HOST: $1"
}

cleanup() {
    rc=$?
    if [ "$CLEAN_REMOTE" = 1 ]; then
        CLEAN_REMOTE=0
        # shellcheck disable=SC2086
        { remote_prelude; script_cleanup; } | ssh $SSH_OPTS "$HOST" 'sh -s' >/dev/null 2>&1 || true
    fi
    [ -z "$PKG_DIR" ] || rm -rf "$PKG_DIR"
    exit "$rc"
}
trap cleanup EXIT

# ---------------------------------------------------------------- building the tarball (local, harmless)
build_package() {
    [ -f "$ROOT/scrappy/__init__.py" ] || die "scrappy/__init__.py not found under $ROOT"
    [ -f "$ROOT/deploy/icon.png" ] || die "deploy/icon.png is missing: make it from the logo with: rsvg-convert -w 256 -h 256 assets/logo.svg -o deploy/icon.png"
    PKG_DIR=$(mktemp -d "${TMPDIR:-/tmp}/scrappy-pkg.XXXXXX")
    PKG=$PKG_DIR/pkg
    mkdir "$PKG"
    LIST=$PKG_DIR/files.txt
    # regular files only (no symlinks), without caches, editor leftovers or hidden files
    (cd "$ROOT" && find scrappy tools -type f \
        ! -path '*/__pycache__/*' ! -name '*.pyc' ! -name '.*' ! -name '*.orig' ! -name '*.rej' \
        ! -name '*~' ! -name '*.swp' ! -name '*.log' ! -name '*.mp4' | LC_ALL=C sort) > "$LIST"
    while IFS= read -r f; do
        mkdir -p "$PKG/$(dirname "$f")"
        cp -p "$ROOT/$f" "$PKG/$f"
    done < "$LIST"
    cp -p "$ROOT/deploy/scrappy.sh" "$ROOT/deploy/scrappy.desktop" "$ROOT/deploy/icon.png" "$PKG/"
    # wheels/ is staging-only: the extractor and the wheel. They are checked by the tarball sha256 and the wheel pin, are
    # not in MANIFEST.sha256, and are deleted on the box after unpacking.
    [ -f "$ROOT/deploy/extract_wheel.py" ] || die "deploy/extract_wheel.py is missing"
    mkdir "$PKG/wheels"
    cp -p "$ROOT/deploy/extract_wheel.py" "$PKG/wheels/"
    cp -p "$WHEEL_PATH" "$PKG/wheels/$WHEEL_NAME"
    chmod 755 "$PKG/scrappy.sh"
    {
        if command -v git >/dev/null 2>&1 && git -C "$ROOT" rev-parse --short HEAD >/dev/null 2>&1; then
            rev=$(git -C "$ROOT" rev-parse --short HEAD)
            if [ -n "$(git -C "$ROOT" status --porcelain -- scrappy tools deploy 2>/dev/null)" ]; then rev="$rev-dirty"; fi
        else
            rev=unknown
        fi
        echo "$rev built $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } > "$PKG/VERSION"
    # MANIFEST.sha256: "hash  path" for every packaged file, checkable with `sha256sum -c`
    (cd "$PKG" && find . -type f ! -name MANIFEST.sha256 ! -path './wheels/*' | sed 's|^\./||' | LC_ALL=C sort | while IFS= read -r f; do
        printf '%s  %s\n' "$(sha256_of "$f")" "$f"
    done) > "$PKG/MANIFEST.sha256"

    if tar --version 2>&1 | grep -qi bsdtar; then
        OWNER_FLAGS="--no-xattrs --format ustar --uid 0 --gid 0 --uname root --gname root"   # plain ustar: no PAX/xattr headers for the Batocera tar
    else
        OWNER_FLAGS="--format=ustar --owner=0 --group=0 --numeric-owner"
    fi
    TARBALL=$PKG_DIR/scrappy-$TS.tar.gz
    # shellcheck disable=SC2086
    (cd "$PKG" && COPYFILE_DISABLE=1 tar $OWNER_FLAGS -cf - scrappy tools wheels scrappy.sh scrappy.desktop icon.png \
        MANIFEST.sha256 VERSION | gzip -n > "$TARBALL")
    TARBALL_SHA=$(sha256_of "$TARBALL")
    # last line of defence: nothing that does not belong may be in the tarball
    junk=$(tar -tzf "$TARBALL" | grep -E '__pycache__|\.pyc$|\.DS_Store|(^|/)\._|(^|/)(tests|\.venv|docs|\.git|node_modules)(/|$)|(^|/)(bin|cache|backups|journal)/|(^|/)config\.json$' || true)
    [ -z "$junk" ] || die "the tarball would contain files that do not belong in it: $junk"
    tar -tzf "$TARBALL" | grep -vE '/$' > "$PKG_DIR/tar-list.txt" || true
}

# ---------------------------------------------------------------- main
if [ "$UNINSTALL" = 1 ]; then
    [ "$DRY" = 0 ] || say "DRY RUN: nothing will be changed on $HOST."
    say "Scrappy: uninstall from $HOST"
    if [ "$PURGE" = 1 ]; then say "(--purge: user data, backups and journal will be deleted too)"; else say "(user data - bin cache backups journal logs config.json - stays)"; fi
    remote "Remove the app and the shortcuts" script_uninstall
    [ "$DRY" = 1 ] && say "" && say "DRY RUN finished: nothing was changed."
    exit 0
fi

[ "$DRY" = 0 ] || say "DRY RUN: nothing will be sent to or changed on $HOST."
say "Scrappy: install to $HOST:$DEST"

detect_arch
step "Build the tarball (on this Mac)"
fetch_wheel
build_package
say "tarball: $TARBALL"
say "sha256:  $TARBALL_SHA"
say "version: $(cat "$PKG/VERSION")"
say "contents ($(wc -l < "$PKG_DIR/tar-list.txt" | tr -d ' ') files):"
sed 's/^/    /' "$PKG_DIR/tar-list.txt"

remote "Check the Batocera (needs at least $MIN_FREE_MB MB free on /userdata)" script_preflight

step "Upload the tarball to $HOST:$STAGE_TGZ"
if [ "$DRY" = 1 ]; then
    say "[dry-run] would run: ssh $SSH_OPTS $HOST \"mkdir -p '$DEST' && cat > '$STAGE_TGZ'\" < tarball"
else
    CLEAN_REMOTE=1
    # SC2086: SSH_OPTS is a list of words on purpose. SC2029: DEST and STAGE_TGZ are constants of this script and are meant
    # to be expanded here, on this computer, before the command is sent.
    # shellcheck disable=SC2086,SC2029
    ssh $SSH_OPTS "$HOST" "mkdir -p '$DEST' && cat > '$STAGE_TGZ'" < "$TARBALL" || die "the upload failed"
fi

remote "Unpack into a staging folder and check it (the installed version is still untouched)" script_stage
remote "Swap the new version in (old one kept as scrappy.previous / tools.previous)" script_swap
remote "Install the shortcuts (Applications menu, Desktop)" script_shortcuts
remote "Verify the installation" script_verify
if [ "$DRY" = 0 ]; then
    CLEAN_REMOTE=0
    # shellcheck disable=SC2086
    { remote_prelude; script_cleanup; } | ssh $SSH_OPTS "$HOST" 'sh -s' >/dev/null 2>&1 || true
fi
remote "Migrate an install under the old name (game-asset-fixer), if there is one (the old folder is kept as a backup)" script_migrate
remote "Run the environment probe" script_probe

step "Done"
if [ "$DRY" = 1 ]; then
    say "DRY RUN finished: nothing was changed."
else
    say "installed to $DEST"
    say "Start it from the Applications menu (F1) or the Desktop icon, or:  ssh $HOST $DEST/scrappy.sh"
    say "Log: /userdata/system/logs/scrappy.log"
fi
