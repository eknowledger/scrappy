# Contributing

Thanks for helping. This is a small project; the rules are short. Before changing code, read
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how it is put together, why, and which test guards what.

## Set up and test

```sh
git clone https://github.com/eknowledger/scrappy.git
cd scrappy
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt PyQt5
.venv/bin/python -m pytest
```

The suite needs no Batocera, no EmulationStation, no `/userdata` and no internet. Steam, YouTube, ffmpeg's trailers, `ssh` and the game
lists are faked, and the window tests run on Qt's offscreen platform (they set `QT_QPA_PLATFORM=offscreen` themselves). It takes
about four to five minutes. `PyQt5` is not in `requirements-dev.txt` on purpose, because on Batocera it comes with the system
Python. Without it the window tests are skipped, and CI fails if they are. Some tests also use `ffmpeg` and skip without it; CI
installs both.

CI (see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#12-packaging-deployment-and-ci)) runs the tests on Python 3.11 and 3.12,
shellchecks the scripts and builds the screenshots. Batocera v41 has Python 3.11, so keep the code compatible with it.

## Ground rules

- **Test first.** A bug fix starts with a failing test; a feature comes with tests. The program is built as a core without Qt
  (`scrappy/`, apart from `app.py` and `scrappy/ui/`) with injectable I/O, a thin window on top, and the window's decisions
  moved into Qt-free modules (`scrappy/ui/logic.py`, `plan.py`, `pipeline.py`) so they can be tested on any machine. Keep it that
  way. `tests/test_controller.py::test_controller_has_no_gui_imports` guards the first half.
- **Be careful with the player's files.** The only things written to a library are the four media files of one game and the tags of
  that game in its system's `gamelist.xml`, through `scrappy/saver.py`, which backs up, journals and can roll back. Do not write
  there from anywhere else, do not add tags to the writable set (`TEXT_FIELDS`, `MEDIA_TAGS`) without discussing it first, and
  never delete a file you did not make.
- **Everything from the web is untrusted.** Pictures come through `scrappy/download.py` (public hosts only). A new network call needs
  a line in "What it contacts" in the [README](README.md#what-it-contacts) and in [SECURITY.md](SECURITY.md). Text from the web is
  shown as plain text in the window, never as rich text. Programs are started with argument lists, never through a shell.
- **Be careful with the box.** `deploy/install.sh` must stay inside `/userdata/system/scrappy`, its shortcuts and the launcher's log
  (plus, once, the move of an install made by a pre-release version). `tests/test_deploy.py` runs the real remote scripts against a fake
  `ssh` and a sandbox and checks that no other file changes; extend it when you add a step. Never run the installer against a
  real machine in a test.
- **Docs move with the code.** If you change a setting, a menu entry, the installer or a host the program contacts, update the
  README, `docs/USAGE.md` and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) in the same change. `tests/test_docs.py` checks that links
  and anchors resolve, that every setting is documented, that every test named in the architecture document exists, and that no
  personal data is in the tree.
- **No personal data.** Use `root@batocera.local` for the box and `192.168.1.100` for addresses on the network, and game titles from
  the demo library in examples. A test fails if a user name, a real-looking private address or another known personal string
  appears anywhere in the repository.
- **No third-party artwork or trailers in git.** The six screenshots in `docs/screenshots/` are the one exception, with their notice
  (`docs/screenshots/README.md`). Test fixtures are invented, anonymised or captured answers from public endpoints, never pictures (`tests/fixtures/README.md`).
  `tests/test_real_showcase.py` fails when a picture or video is tracked outside `docs/screenshots/` and `assets/`.
- Keep messages for users in plain language and say how to fix the problem.

## Testing on a Batocera

`tools/probe_environment.py` (read-only report), `tools/box_selftest.py` (end to end in a throw-away folder),
`tools/ui_smoke.py` (the real window, headless) and `tools/verify_media_naming.py` (read-only comparison with your library) run on the
machine. If you try Scrappy on a setup that is not in [COMPATIBILITY.md](COMPATIBILITY.md), or on a Batocera version, theme or
board that works or does not, please open an issue with the "I tried it on my setup" form (the steps are in the compatibility page).

## Screenshots

All pictures are made by scripts that need no display (Qt runs "offscreen"), PyQt5, Pillow and, for the trailers, ffmpeg. The
invented demo library ships in the repository and works offline.

| Set | Folder | Made by | Library |
|---|---|---|---|
| Annotated pictures of the user guide | `docs/screenshots/annotated/` | `tools/make_annotated_screenshots.py` | invented only |
| The six pictures of the README | `docs/screenshots/` | `tools/make_screenshots.py` | real titles (`--real`) or invented (`--out DIR`) |

### Annotated pictures (`docs/USAGE.md`)

```
.venv/bin/python tools/make_annotated_screenshots.py            # writes docs/screenshots/annotated/ (under a minute)
.venv/bin/python tools/make_annotated_screenshots.py --out /tmp/ann --layout     # elsewhere, and print where every badge went
```

* The script drives the real window on the invented library, grabs it and draws numbered badges next to the controls. Where a
  badge goes comes from the live widget geometry (`mapTo`, the rows of the list, the actions of the menu), never from pixel numbers,
  so it keeps working when the layout changes.
* The callouts of every picture (number, which control, the name used in the legend, which side) are data in
  `tools/annotated_spec.py`. The legend tables in `docs/USAGE.md` must list the same numbers and names;
  `tests/test_annotated_screenshots.py` fails when a picture and its legend differ.
* To add or change a callout: edit `tools/annotated_spec.py` and the legend table, run the script, look at the picture, and commit the
  PNGs (they are deterministic and each stays under about 350 KB). Use only titles of the invented library.
* The script ends with a message and exit code 1 when a step fails, and with code 2 when PyQt5 is not installed.

### The six pictures of the README (`docs/screenshots/`)

```
.venv/bin/python tools/make_screenshots.py --out /tmp/shots   # the invented library, nothing real
.venv/bin/python tools/make_screenshots.py                    # ... written into docs/screenshots/ (replaces the real-title set!)
```

The set in `docs/screenshots/` shows real games and is made with `--real` from artwork that is fetched into a cache outside the
repository. If you change the window, regenerate it only if you can do `--real`; otherwise say so in the pull request. The invented
pictures are the same on every run. The script ends with a non-zero exit code when something fails, and with code 2 when PyQt5 is
not installed.

### Real titles (`--real`, developer tool)

The real-titles set needs artwork that must **never be committed**: it is downloaded once into `~/.cache/scrappy-demo-art/` (set
`SCRAPPY_DEMO_ART` to use another folder), and everything after that works offline.

```
.venv/bin/python tools/fetch_demo_art.py          # once: downloads the artwork of the 16 games (idempotent, about 90 requests)
.venv/bin/python tools/fetch_demo_art.py --list   # what is cached or missing; exit code 1 if a game lacks box art or a screenshot
.venv/bin/python tools/make_screenshots.py --real --out /tmp/real-shots
```

* The 16 games (two on each of `nes`, `snes`, `megadrive`, `gb`, `psx`, `switch`, `windows`, `ps3`) and their texts are listed in
  `tools/real_showcase.py`; the repository holds only these titles and short texts written for this project.
* The only hosts the fetch tool contacts: Steam (`store.steampowered.com` search and appdetails, and the `*.steamstatic.com` picture
  hosts) for the Switch and Windows titles; the libretro-thumbnails repositories on `api.github.com` and `raw.githubusercontent.com`
  for the retro and PS3 titles. It identifies itself (`scrappy-demo-tool/1.0`), sends one request at a time with a pause, caches
  everything, never asks twice for the same thing, stops after three errors in a row or when a source asks to slow down, and stops for
  good after 400 requests in total (`requests.json` in the cache keeps the count).
* No real trailer is downloaded. The "Trailer" and YouTube candidates are 6-second slideshows that ffmpeg makes from the cached
  pictures. The retro titles have no logo at the source, so they offer the program's own generated text logo.
* `--real` ends with a message and exit code 1 when the cache is incomplete. It is not reachable from `--demo`.
* Do not `git add` anything from the cache; `tests/test_real_showcase.py` fails when a picture or video is tracked outside
  `docs/screenshots/` and `assets/`.

## Commits and pull requests

Small commits with a short imperative subject (`feat: ...`, `fix: ...`, `docs: ...`, `test: ...`) and the reason in the body when it
is not obvious. Describe how you tested the change, and on what machine if it touched the box. The pull request template carries the
checklist.

By contributing you agree that your work is released under the MIT license of this project.
