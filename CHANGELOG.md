# Scrappy changelog

All notable changes to Scrappy are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-26

First public release. The Python package is `scrappy`, the install folder is `/userdata/system/scrappy` and the environment
variables are `SCRAPPY_*`. Installs made by a pre-release version under the name game-asset-fixer are migrated by the installer.

### Added

- A progress window (`scrappy/ui/task_dialog.py`) for **Tools > Install / update YouTube downloader** and **Tools > Reload game lists in
  EmulationStation**. It shows what is going on with a busy or percentage bar ("1.8 of 3.2 MB"), has **Cancel** when stopping is safe, and ends in a
  result that stays until OK is pressed: a green check mark with what was done (for the downloader: install or update, the version, the file), or a red
  cross with the reason in plain words, what to try and a **Details** box with the technical text. Closing it while it works means Cancel.
- The downloader install reports the bytes downloaded against the size GitHub announces (`YtDlp.install(progress=...)`), can be cancelled without
  leaving a temporary file or touching an installed copy, refuses an announced size over the cap before reading, and returns an `InstallResult`.
- An **Install YouTube downloader** button beside the hint line of the Trailer panel while the downloader is missing, and a **Reload game lists**
  button beside the status line after a save that EmulationStation did not take. Both open the same windows.
- The first time Scrappy starts, the status line shows "Tip: the Tools menu (top left) installs the YouTube downloader and reloads game lists." once.
  New setting `intro_seen` (default `false`); Scrappy sets it to `true` itself, the only setting it writes, and it never rewrites a settings file that
  has a problem.
- `Controller.ask_es_to_reload()`. In `--demo` both Tools actions work without any network: a simulated download with progress, and a simulated reload.
- Three annotated pictures in the user guide: `task-dialog-running`, `task-dialog-result`, `task-dialog-failed`.
- A desktop window (PyQt5) for Batocera: a list of systems and their games with a "Missing something" filter and a mark
  per game (complete, or what is missing); newest ROM files first. The title is cleaned from the ROM file name
  (`Tidepool Tactics [0100AAAA00000000][v0].nsp` becomes `Tidepool Tactics`).
- **Current**: selecting a game shows what it has now (logo, box art, image, trailer, and the seven text fields), read from
  `gamelist.xml` and the files on disk, and never written back by itself.
- Sources: **Steam** (title, description, release date, developer, publisher, genre, players, screenshots, header, page
  background, library box art, transparent `logo.png`, trailers as HLS streams; a DLC borrows box art and logo from its base
  game and says so) and **YouTube** through yt-dlp (trailer search ranked against the game's title). Both are asked at the
  same time; one failing never blocks the other.
- Pictures and files of your own: Paste link, Choose file, drag and drop (pictures), Web search (opens your browser).
- Conversion to fixed formats: image 640x360 JPEG, box art 396x640 PNG, logo 600x300
  transparent PNG (with Remove background for plain backgrounds and a generated text logo as a fallback), trailer MP4
  H.264 1280x720 30 fps at about 1.5 Mbps with sound, cut to a start and end you choose (at most 120 s by default).
- A review window before anything is written: every new picture next to the current one, the trailer, every changed
  detail with its old and new value, the files that will be replaced, linked or left alone. Cancel is the default button when
  files would be replaced.
- Saving as one transaction (`scrappy/saver.py`): media written atomically, `gamelist.xml` edited only in the tags asked
  for, a backup of the previous `gamelist.xml`, a journal, a full rollback on any failure, and **Undo last save** (the last
  20 saves, also after a restart). Existing art is kept unless you chose to replace it, and a replaced file is copied to the
  journal first. A dry-run mode (`dry_run_dir`) writes to a sandbox copy instead.
- EmulationStation awareness: when it is running, a save asks it to flush its game lists, saves, asks it to reload, checks that
  the list still has the change and applies it once more if EmulationStation wrote its older copy back. Undo does the same.
- A safe downloader (`scrappy/download.py`): http and https only, public addresses only (checked on the address the
  connection really uses), size, time and header limits, no cookies, no proxy variables, no credentials in addresses.
- Settings in an optional `config.json` (`roms_root`, `data_dir`, `max_video_seconds`, `default_video_seconds`,
  `video_bitrate`, `search_timeout`, `youtube_search_timeout`, `dry_run_dir`); a bad file or value gives the defaults and a
  note in the window.
- A Tools menu: install or update the YouTube downloader (yt-dlp, from GitHub, only when you ask) and reload the game lists in
  EmulationStation.
- Installer (`deploy/install.sh`, run on the computer you install from): `--dry-run`, `--uninstall`, `--purge --yes`; reads the
  machine's architecture (x86_64 or aarch64) and ships the matching vendored Pillow; checks the free space; stages and tests the
  new version on the box before swapping it in (the old one is kept as `.previous`); installs the shortcuts; runs a read-only
  probe. Migrates an install made by a pre-release version.
- A Pillow that works: Batocera's own Pillow cannot read or write JPEG, so a pinned Pillow 11.3.0 wheel is vendored into the
  install folder after its sha256 is checked, and a real JPEG round trip must pass before the install continues.
- Tools for the box: `tools/probe_environment.py`, `tools/box_selftest.py`, `tools/ui_smoke.py`,
  `tools/verify_media_naming.py`.
- A demo mode, `python -m scrappy.app --demo`, on a fictional library of 15 invented games, and `tools/make_screenshots.py`,
  which makes the six pictures in `docs/screenshots/` headless. The real-title set needs artwork that is fetched into a
  cache outside the repository and is never committed (`tools/fetch_demo_art.py`, `tools/real_showcase.py`).
- `docs/USAGE.md` as a visual guide with nine annotated screenshots of the invented demo library (`tools/make_annotated_screenshots.py`,
  `docs/screenshots/annotated/`); a test keeps each picture and its legend table in step.
- About 1500 automated tests with Steam, YouTube, ffmpeg, EmulationStation, `ssh` and `/userdata` faked, and the window run on
  Qt's offscreen platform. GitHub Actions run them on Python 3.11 and 3.12, shellcheck the scripts and build the screenshots.
- Documentation: README, `docs/USAGE.md`, `docs/ARCHITECTURE.md`, `docs/KNOWN-LIMITATIONS.md`, `COMPATIBILITY.md`,
  `SECURITY.md`, `ROADMAP.md`, `CONTRIBUTING.md`.

### Known limitations

- Verified: Batocera v41 on x86_64, with one theme. The installer's aarch64 path is tested in a virtual machine; no ARM board
  is verified. See `COMPATIBILITY.md`.
- The sources are Steam and YouTube, built into the program. Games Steam does not sell (most older console games) find
  nothing; you supply the pictures and trailer yourself. More sources and a plugin system are planned
  (`docs/PROVIDERS.md`, `ROADMAP.md`).
- EmulationStation can overwrite text changes later (at exit, or after a game is played); the save checks once, a few seconds
  after. If your changes vanish, save the game again. See `docs/KNOWN-LIMITATIONS.md`.
- The pictures and trailer are written in fixed formats; the program does not read the theme to find out what it wants.
- Trailers cannot be added by pasting a link or dropping a file yet (Choose file works).
- The program identifies itself to Steam and to picture servers, and marks the games it touched in `gamelist.xml`
  (`<scrap name="Scrappy">`; saves made by pre-release versions carry another name). See `docs/KNOWN-LIMITATIONS.md`.
