# Compatibility

What has been tried, on what, and what has not. "Verified" means it was run on that system and the result was looked at;
"tests only" means the behaviour is covered by the automated tests (with fakes) but is not recorded as run on a real
system; "not verified" means exactly that. If you can move something from one list to another, please
[report your setup](#report-your-setup).

## Verified

| | |
|---|---|
| System | Batocera v41 on an x86_64 PC, Python 3.11, PyQt5 and ffmpeg as shipped with it |
| Theme | one. Other themes are not verified |
| What was run | a game that Batocera's scraper had not found: search on Steam, preview of a Steam trailer, save, and the logo, box art, image, trailer and description showing in EmulationStation |
| Installer | `deploy/install.sh` installed it on that system |

## Tested in a Batocera-like ARM64 virtual machine

The installer's aarch64 path (architecture detection, the aarch64 Pillow wheel, staging, verification, swap) was tried in an ARM64
virtual machine made to look like Batocera. It is not Batocera and not a real ARM board. A real Raspberry Pi or other ARM board
has not been used, so treat aarch64 as "should work".

## Tests only (not recorded as run on a real system)

All of this runs in the automated tests with Steam, YouTube, ffmpeg, EmulationStation, `ssh` and `/userdata` faked, and the window
on Qt's offscreen platform (CI runs Python 3.11 and 3.12). The first six items are not recorded as run on a real system.

- The automatic EmulationStation path: flush before a save, reload after, verify, apply again once; and the same for Undo.
- The review window and the Current panels on a real screen, and the layout on very wide screens and at 1920x1080.
- Pasting links, dropping files and Choose file in all three picture slots.
- YouTube search and download (Tools > Install / update YouTube downloader, then a search and a save).
- The Web search button opening the browser.
- Undo while EmulationStation is running.
- Individual systems. The file names and folders follow Batocera's scraper for every system the same way;
  `tools/verify_media_naming.py` compares them with your own library, read-only.
- Python 3.12 (3.11 is what Batocera v41 ships).

## Not verified

- **RetroBat and other EmulationStation based systems.** The folder layout (`roms/<system>/gamelist.xml`, `images/`, `videos/`),
  the reload address (`127.0.0.1:1234/reloadgames`) and the install folder (`/userdata/system`) are Batocera's. Another
  frontend may share some of them and not others. A "frontend profile" is on the [roadmap](ROADMAP.md), not implemented.
- **Other themes.** The pictures and the trailer are written in fixed sizes (640x360 JPEG image, 396x640 PNG box art, 600x300 PNG
  logo, 1280x720 MP4 trailer). The program does not read the theme.
- **Real ARM boards** (Raspberry Pi and others), and any Batocera version other than v41.
- **32-bit machines** (armv7l, i686): refused by the installer on purpose, since no Pillow is pinned for them.
- **A Batocera with another Python than 3.11:** the vendored Pillow is for 3.11 and the installer's check would stop the install.
- ROM folders on other kinds of storage (network shares, other file systems, symlinked folders), and very large libraries.

## Not supported

- Windows, macOS or desktop Linux as the place Scrappy runs. The installer runs on macOS or Linux and installs to the Batocera; the
  tests and the demo run on a developer machine, but Scrappy is made for the Batocera desktop.
- Editing anything but the seven text fields (name, description, release date, developer, publisher, genre, players) and the four
  media tags (image, thumbnail, marquee, video).

## Report your setup

Whether it works or not, a report helps the next person. Open a GitHub issue with the **I tried it on my setup** form (or copy this
into an issue). Everything below is read-only.

1. On the Batocera (over ssh or in a terminal):

   ```sh
   cat /userdata/system/scrappy/VERSION
   uname -m
   cd /userdata/system/scrappy
   PYTHONPATH=$PWD/vendor:$PWD python3 tools/probe_environment.py
   ```

2. The self test. It runs in a throw-away folder, touches none of your games, and prints `BOX SELFTEST OK` when everything passes:

   ```sh
   PYTHONPATH=$PWD/vendor:$PWD python3 tools/box_selftest.py
   ```

3. Try one game from search to Save, then look at it in EmulationStation, then press Undo last save. Tell us what you saw, and which
   theme you use.

4. If you want to check that the file names match what your own scraper wrote (read-only):

   ```sh
   PYTHONPATH=$PWD python3 tools/verify_media_naming.py --all
   ```

**What to paste and what to hide.** The version, `uname -m`, the probe and the self test output are what we need. User names, host
names and addresses on your network are not: remove them before you paste. Game titles are fine.
