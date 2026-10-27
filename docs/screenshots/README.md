# Screenshots

Trademarks and artwork. The screenshots in this folder show the names, box art and in-game pictures of commercial video games and consoles. All of them belong to their respective owners and are shown only to illustrate how this tool works. This project is not affiliated with, endorsed by or sponsored by any of them. The artwork is not part of this project's source code, is not covered by its MIT licence, and is not needed to run the program. If you are a rights holder and want an image removed, open an issue at https://github.com/eknowledger/scrappy/issues and it will be removed promptly.

## What the pictures show

`07-desktop.png` is the same search as `03-search-results.png` (Stardew Valley), taken by `tools/make_desktop_screenshot.py --real` on a real desktop so that the title bar and the program's icon are in the picture. The first six pictures (`01-library.png` to `06-trailer.png`) are made by `tools/make_screenshots.py --real` from the real window, running on a throw-away library of 16 well-known games on eight systems (`nes`, `snes`, `megadrive`, `gb`, `psx`, `switch`, `windows`, `ps3`). The library holds one-byte placeholder files, not games. The artwork is downloaded once into a cache folder outside this repository by `tools/fetch_demo_art.py` and is never committed; only the finished screenshots are.

| Game | System | Shown in | Box art | Logo | In-game pictures | Trailer |
| --- | --- | --- | --- | --- | --- | --- |
| The Legend of Zelda | nes | 01 | libretro thumbnails (`Named_Boxarts`) | none yet (empty panel) | libretro thumbnails (`Named_Titles`, title screen) | none yet |
| Mega Man 2 | nes | 01 (name only) | - | - | - | - |
| Hades | switch | 02, 04, 05 | Steam (library capsule, 600x900) | current: the program's own generated text logo; new: Steam (`logo.png`) | Steam (store screenshots) | thumbnail: Steam (movie thumbnail); the clip itself is a slideshow made by ffmpeg from the Steam pictures |
| Celeste | switch | 05 (name only) | - | - | - | - |
| Stardew Valley | windows | 03, 06, 07 | Steam (library capsule, 600x900) | Steam (`logo.png`) | Steam (store screenshots) | thumbnail: Steam (movie thumbnail); the "YouTube" entry in 06 is a stand-in that shows a Steam screenshot |
| Cuphead | windows | 03 (name only) | - | - | - | - |

Notes:

* The names of the other games and of the systems appear only where the window lists them (the system drop-down in `02-current.png` names all eight).
* Descriptions, release dates, developers and genres are facts or short texts written for this project; for Steam titles the facts come from the store's public data. No store description is reproduced.
* Steam is a real source of the program. "libretro thumbnails" is not: the program has no such source, the demo uses a stand-in named after the place the retro pictures were taken from (https://github.com/libretro-thumbnails). The retro games have no logo there, so the program's own "Make text logo" result is offered instead.
* No trailer or video of any game is used. The "Trailer" entries play a 6-second slideshow that ffmpeg generates from the cached pictures.
* To regenerate this set: `.venv/bin/python tools/fetch_demo_art.py`, then `.venv/bin/python tools/make_screenshots.py --real`. The invented demo library (no real names or pictures, works offline) makes the same six pictures with `.venv/bin/python tools/make_screenshots.py` (without `--real`).

## The annotated pictures

`annotated/` holds the twelve numbered pictures of [docs/USAGE.md](../USAGE.md). They show the invented demo library only (made-up games,
generated artwork, no real title or picture) and are made by `tools/make_annotated_screenshots.py`; `annotated/spec.json` lists their
callouts. The notice above does not apply to them.
