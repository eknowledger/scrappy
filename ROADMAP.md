# Roadmap

What might come next, in rough order of priority. Nothing here is a promise. A small project maintained in spare
time moves when somebody has the setup to test on, and that is the real limit: most items below need a Batocera, a theme or a
board that a unit test cannot stand in for. For what Scrappy cannot do today, see
[docs/KNOWN-LIMITATIONS.md](docs/KNOWN-LIMITATIONS.md); for what is verified, see [COMPATIBILITY.md](COMPATIBILITY.md).

## More sources

Today the sources are Steam and YouTube, built into the program, so a game Steam does not sell gets no match. The plan to fix
that, in this order, is written out in [docs/PROVIDERS.md](docs/PROVIDERS.md); none of it is implemented yet.

1. **More built-in providers.** libretro thumbnails (box art and in-game pictures for retro systems, no key), Wikidata and
   Wikipedia (description, release date, developer), SteamGridDB and IGDB (free API keys, kept out of `config.json` and out
   of logs), and others people ask for.
2. **A stable, versioned provider contract.** A `provider_api` version, the capabilities each provider declares (text, logo,
   box art, image, video), the hosts it declares and the downloader enforces, per-provider settings and enable/disable in
   `config.json`, per-provider timeouts and error isolation.
3. **Declarative providers.** A small JSON or YAML description of a REST/JSON source (URL templates, field mapping) that runs
   without any third-party code.
4. **Code plugins.** A drop-in folder (`/userdata/system/scrappy/providers/`), a manifest, install/remove/list from the command
   line and the Tools menu, and an honest trust model: a plugin is code running as root on your machine, so it is shown,
   enabled explicitly, pinned by hash and never updated by itself.
5. **A curated index** of plugin manifests, so users can browse and install community providers; possibly a small marketplace.

## Near

- **More setups in [COMPATIBILITY.md](COMPATIBILITY.md).** Only Batocera v41 on x86_64 is verified. A real ARM board,
  another theme, another Batocera version: each one verified turns a "should work" into a line. This is the most useful
  contribution and needs no code (use the "I tried it on my setup" issue form, which also takes "it works").
- **Record the checks that are still open on a real system.** The automatic reload path of EmulationStation, Undo while it
  runs, pasting links in all three picture slots, YouTube trailers and the layout on very wide screens are covered by tests
  only. See [COMPATIBILITY.md](COMPATIBILITY.md#tests-only-not-recorded-as-run-on-a-real-system).
- **Small hardening items**, each with a test first:
  - stop trusting the shared `.akamaihd.net` host by name for trailers (Steam trailers came from `steamstatic.com` in every
    captured answer) and refuse video addresses containing `#`, `\` or `@`;
  - check once at run time whether the installed mpv has the `--ytdl` option, and use it only then; keep the VLC fallback to
    local files the user chose;
  - make the encoder's temporary names exclusive-create through `scrappy/atomic.py`;
  - clean up journal folders that have no `journal.json` (left by a crash before one was written); today they are neither
    counted nor deleted;
  - say so in the window when `config.json` has a setting name the program does not know (today a typo is ignored silently).

## Next

- **Trailers from a pasted link or a dropped file.** Pictures already take a pasted link and a drop. For trailers the link would
  first be downloaded through the safe downloader and ffmpeg would only ever be given a local file, which also closes the
  "ffmpeg looks the name up again" gap described in `SECURITY.md`.
- **Image search inside the program** (DuckDuckGo, best effort) instead of the Web search button that opens a browser and asks
  you to copy an address back.
- **A batch queue** for doing many games in a row.
- **A preview that does not depend on an external player.** Today the preview is mpv or ffplay in its own window; PyQt5's
  QtMultimedia is not used because it is not guaranteed to be on the box.
- **Keep or Replace per text field**, instead of the all-changed-fields-are-written rule.

## Maybe

- **Other EmulationStation frontends, such as RetroBat, through a "frontend profile".** Today the folder names, the media
  names, the sizes and the reload address are constants for Batocera. A profile would carry them as data. Not implemented;
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#115-support-another-frontend-a-frontend-profile) sketches where it would go. It needs
  somebody with that frontend to test.
- **Reading the theme** to find the picture and video sizes it wants, instead of the fixed values built in.
- **More architectures and Python versions.** The vendored Pillow pins are for Python 3.11 on x86_64 and aarch64. 32-bit
  machines and another Batocera Python need new pins in `deploy/vendor.txt`.

## Not planned

- **Taking over Batocera's own scraper job.** Scrappy fills gaps for games the scraper could not find. It does not read
  ScreenScraper credentials or settings and does not scrape whole libraries.
- **Changing ROMs, emulator settings or anything outside the game's system folder and Scrappy's own folder.**
- **Gamepad control of the window.** It is an ordinary desktop window: mouse, keyboard, Tab and arrow keys.
- **Storing secrets in `config.json`.**
