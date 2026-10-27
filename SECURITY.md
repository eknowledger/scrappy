# Security

## Reporting a problem

Please do not open a public issue for a problem that could be exploited. Report it privately through GitHub's private
vulnerability reporting: <https://github.com/eknowledger/scrappy/security/advisories/new>.

Include what you did, what you expected and what happened. We will acknowledge a report and say what we plan to do.

## Supported versions

Only the latest release is supported (the project is at version 0.1.0).

## Threat model

Scrappy is a desktop program that runs as root on a Batocera machine on a home network, with one user at the keyboard. It reads
text and pictures from the internet and hands some of it to other programs. The things worth protecting are the player's game
lists and media (`gamelist.xml` and the files next to the ROMs), the machine itself (everything on Batocera runs as root), and
other machines on the home network.

What is trusted: the person at the keyboard, the files already in the library, and the program's own code and the packages
Batocera ships (Python, PyQt5, ffmpeg, mpv).

What is **not** trusted: everything that arrives from the web. That is Steam's answers (titles, descriptions, links), pictures
and trailers, YouTube's answers (titles, channel names, thumbnail links), yt-dlp's output, file names in the library, and
anything the user pastes or drops. The design treats each as data to be checked and shown as plain text, never as a command or a
path.

What it does **not** defend against: someone who already has a shell on the box, or write access to `/userdata` (they can edit
the program itself), and a compromised Steam, YouTube or GitHub. Where a limit of the checks is known, it is listed below and in
[docs/KNOWN-LIMITATIONS.md](docs/KNOWN-LIMITATIONS.md).

## What the program does on your machine

Scrappy opens no network port and runs no server. It contacts the hosts in the table "What it contacts" in the
[README](README.md#what-it-contacts), nothing else, and sends no account, cookie, key or telemetry. There is no automatic update:
the program never fetches a new version of itself, and it fetches yt-dlp only when you choose **Tools > Install / update YouTube
downloader**.

### What it downloads, and how it is checked

| Download | Path | Checks |
|---|---|---|
| Steam answers (search, store details, library assets) | `requests` to fixed `https://` addresses in `scrappy/providers/steam.py` (store search, store details, the browse service, and a `HEAD` for a library `logo.png`) | Every field is type-checked; only `https` addresses on `*.steamstatic.com` or `*.akamaihd.net` are kept; lists are capped; a malformed answer gives partial results, not an exception. These calls follow the system's proxy variables; the picture downloader does not |
| Pictures (Steam art, YouTube thumbnails, links you paste or drop) | `scrappy/download.py` | `http` and `https` only, also after every redirect; no credentials in addresses; **public addresses only** (see below); at most 25 MB; compressed answers refused; connect and read timeouts and one total deadline of 60 s; at most 5 redirects; response headers limited to 64 KB; an `https` page may not redirect to `http`; no cookies, no proxy variables, no `.netrc`; written to a temporary file next to the target and renamed; the bytes must start like a picture before they are cached, and are decoded only by the formats in `ALLOWED_FORMATS` (JPEG, PNG, WebP, GIF, BMP, TIFF) with Pillow's decompression-bomb limit on |
| Trailers (Steam streams) | `ffmpeg`, started by the program | Only an `https` address on a Steam asset host, or one that resolves to public addresses only; ffmpeg gets `-protocol_whitelist http,https,tcp,tls,crypto,httpproxy,data` (never `file`, `concat` or `subfile`), so a playlist cannot make it read local files |
| Trailers and search results (YouTube) | `yt-dlp`, started by the program | A video is identified by an 11-character id that passed validation, and every address given to yt-dlp is rebuilt from that id; yt-dlp is started with `--ignore-config`, `--no-plugin-dirs` and `--no-cache-dir`, is given no cookies, and the search text always comes after `--`; 720p or lower, at most 300 MB, between 10 seconds and 20 minutes; its output is size-capped and its messages never reach the window except as one short sanitised line |
| yt-dlp itself (only on request) | `urllib` to `https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp` | The final address after redirects must be `github.com`, `*.github.com` or `*.githubusercontent.com` over `https`; at most 25 MB; it must be a zip application with a `__main__.py`; `python -I yt-dlp --version` must succeed before it replaces the installed copy. It is **not** compared with a published checksum or signature |

### The public-host rule (SSRF)

The downloader refuses to talk to anything on your own network. The name is resolved once; **every** address it returns must
be public, and the connection is then opened to exactly that checked address, so a second lookup cannot give a different answer.
Refused: loopback, private, link-local, carrier-grade NAT, multicast, reserved and documentation ranges, in IPv4 and IPv6, and IPv6
forms that carry an IPv4 address (mapped, translated, NAT64, 6to4) are judged by that IPv4 address. The rule is the project's own
list (`scrappy/download.py`, `is_public_address`), not `ipaddress.is_global`, which differs between Python versions. The only way
around it is `allow_private=True`, which nothing in the program passes; it exists for tests.

Known limits, accepted:

- Looking up a name cannot be interrupted from Python. The time limits are checked before and after, so a very slow DNS server can
  make a download wait longer than the timeouts say.
- **ffmpeg looks the name up again itself.** For a trailer address on a host that is not a Steam or YouTube host, the program checks
  that the name resolves to public addresses, but ffmpeg resolves it again and follows the playlist's own links. This is a guard
  against honest mistakes and simple tricks, not a complete defence. Today the program only produces Steam and YouTube trailer
  addresses; the day pasting trailer links is added, they will be downloaded through the safe downloader first (see
  [ROADMAP.md](ROADMAP.md)).
- Steam and YouTube host names are trusted by name (`*.steamstatic.com`, `*.akamaihd.net`, `*.youtube.com`, `*.googlevideo.com`).
  `akamaihd.net` is a shared CDN; narrowing it is on the roadmap.

### What it executes

All programs are started with argument lists, never through a shell. Text from the web is never placed in a program name or in an
option position.

| Program | When | Notes |
|---|---|---|
| `ffmpeg`, `ffprobe` | Encoding a trailer; measuring the length of a video | Source: a Steam address as above, a file you chose, or a file the program downloaded itself. A file name that starts with `-` is turned into `./name`. Output is a new file next to the clip, renamed into place |
| `python3 -I <data>/bin/yt-dlp` | YouTube search and download | The system Python in isolated mode; runs as the same user as Scrappy (root on Batocera) |
| `mpv`, `cvlc` or `vlc`, `ffplay` | Preview | Only `http(s)` addresses, the canonical watch link of a validated YouTube id, or a file you chose. `--` precedes the address (for ffplay the address always starts with `http` or `/`). The player's error output is logged |
| `chrome`, `xdg-open` | Web search | Given one argument, the DuckDuckGo address built from the game's title. If neither starts, the address is copied to the clipboard instead |
| `zenity` or `xmessage` | Only when the program cannot start (no display, PyQt5 missing) | Shows the error text |
| `python3 tools/*.py` | Installer checks and the tools you run | Read-only except `box_selftest.py`, which writes only in a throw-away folder |

No file of the library is ever executed, and nothing from the web is executed except the yt-dlp program you asked the program to
install.

### What it writes, and where

| What | Where | Why |
|---|---|---|
| The game's pictures and trailer | `<roms_root>/<system>/images/<ROM name>-image.jpg`, `-thumb.png`, `-marquee.png` and `<roms_root>/<system>/videos/<ROM name>-video.mp4`; the two folders are created if missing | the point of the program |
| `gamelist.xml` of that system | the same system folder | the seven text tags and the four media tags of the one game, plus a `<scrap>` marker; the rest of the file is kept |
| Temporary files | `.scrappy-<16 hex>.tmp` beside the target, removed on failure | atomic writes |
| Backups and the undo journal | `<data_dir>/backups`, `<data_dir>/journal` | Undo. Kept: the first `gamelist.xml` of each system forever, the newest 30 backups, the newest 20 saves |
| Its own work folders | `<data_dir>/cache` (pictures, about 200 MB at most), `tmp` (clips; leftovers older than 24 hours are removed), `bin/yt-dlp`, `logs/scrappy.log` | cache and logs |
| The launcher's log | `/userdata/system/logs/scrappy.log` | what the shortcuts hide |

`<data_dir>` is `/userdata/system/scrappy`. Before each write the media path is resolved and must lie **inside the system folder**:
`..`, absolute paths, a symlinked `images` or `videos` folder, or a symlinked file that leads elsewhere are refused, and the check
is repeated right before the write. Temporary files are created with `O_EXCL | O_NOFOLLOW`, so a link planted at a guessable name
is not followed. A game path may not be absolute or contain `..`. The program deletes only files it made (named like its own
temporary files, clips and cache entries), never through a symbolic link, and never a file you own.

It never writes to ROMs, emulator settings, `es_settings.cfg` (it does not read ScreenScraper credentials either), or any other
system's folder. It edits `config.json` only to write `intro_seen` once, after it has shown its first-start tip; every other line
stays as it is, and a file with a problem is left alone.

Known limits, accepted: between the last comparison of `gamelist.xml` and the rename a few microseconds pass in which another
program could still write the file, and a folder could be swapped for a link between the check and the write. Both were judged
acceptable on a private network with one user. Logs can contain game titles and web addresses; they stay on the box.

### EmulationStation

When EmulationStation is running, the program asks it to reload its game lists with a `GET` to
`http://127.0.0.1:1234/reloadgames`, on this machine, without any proxy. The environment probe reads only `/` and
`/runningGame` of the same address and refuses every other path. The program notices EmulationStation by looking for its process in
`/proc`. It never stops or restarts it.

## The installer

`deploy/install.sh` runs on the computer you install from and uses `ssh` to the Batocera (with `BatchMode=yes`, so it needs a key,
and a connect timeout of 10 seconds). The remote steps are short POSIX `sh` scripts sent over the connection and run as the user
you name (`root` on Batocera). `--dry-run` prints every one of them and connects to nothing.

On the Batocera it:

- reads, before anything else, `uname -m`, the free space of `/userdata` (it refuses with less than 200 MB), and the Python version;
- uploads one tarball to `/userdata/system/scrappy/.staging-<time>.tar.gz`, compares its sha256, rejects absolute or `..` paths,
  extracts it into a new staging folder, checks `MANIFEST.sha256`, compiles every `.py` and imports the package from the staged
  copy, unpacks the Pillow wheel with Python's `zipfile` (every member name is validated first; only `PIL`, `pillow.libs` and the
  `dist-info` folder are extracted; symlinks refused) and runs a real JPEG round trip with it, **all before touching the installed
  version**;
- then swaps `scrappy/`, `tools/` and `vendor/` in by renaming (the old ones stay as `*.previous`; a failure puts them back),
  installs `scrappy.desktop` as `/userdata/system/.local/share/applications/scrappy.desktop`, `/userdata/system/Desktop/scrappy.desktop`
  and (a convenience that Batocera rebuilds at boot) `/usr/share/applications/scrappy.desktop`, and verifies the result;
- migrates an install made by a pre-release version: user data (`config.json`, `backups`, `journal`, `cache`, `bin`) is **moved**,
  never over something already there; the old shortcuts are removed; the old folder is **renamed** to a backup folder
  (`<old folder>.old-<time>`, next to it in `/userdata/system`) and never deleted. It refuses while the old program runs.

It does not touch ROMs, `batocera.conf`, any service list, or any file outside the places above. `tests/test_deploy.py` runs the real
remote scripts against a fake `ssh` and a sandboxed `/userdata`, seeds files that must not change and checks that none does.

On the computer you install from, the installer downloads one file: the pinned Pillow wheel from `files.pythonhosted.org`, into
`~/.cache/scrappy/wheels`, and refuses it unless its sha256 equals the pin in `deploy/vendor.txt`. The wheel is never stored in git.
The tarball excludes tests, docs, caches, hidden files and anything named like user data.

`--uninstall` removes the program, `scrappy.previous` and the like, and the shortcuts, and keeps `bin`, `cache`, `backups`,
`journal`, `logs` and `config.json`. Only `--uninstall --purge --yes` deletes the data too. Neither touches an install under the old
name or its `.old-<time>` backups.

## Turning things off, summary

- **No network at all:** the demo (`python -m scrappy.app --demo`) uses invented sources and touches nothing real. In the real
  program, Steam is asked whenever you press Search, and there is no setting that turns it off; disconnect the machine and use
  **Choose file** instead.
- **No YouTube:** do not choose Tools > Install / update YouTube downloader. Without `bin/yt-dlp` the YouTube search does not run, and
  nothing is contacted for it. Delete `bin/yt-dlp` to switch it off again.
- **No real writes:** set `"dry_run_dir"` in `config.json` (see [docs/USAGE.md](docs/USAGE.md#settings-configjson)); saves go to a
  sandbox copy.
- **No traces:** `--uninstall --purge --yes` removes everything under `/userdata/system/scrappy` and the launcher's log. Files
  already written to a system's `images/` and `videos/` folders, and the entries in `gamelist.xml`, are yours; Undo last save
  removes the ones from a save.
