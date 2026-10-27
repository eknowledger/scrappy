## What and why

<!-- What changed, and what problem it solves. Link the issue if there is one. If it came from a defect, say
     what you saw and what you expected instead. -->

## How it was tested

- [ ] `QT_QPA_PLATFORM=offscreen python -m pytest` passes (no Batocera, no network, no EmulationStation needed)
- [ ] New behaviour has tests, written first where practical; nothing in them reaches the internet or a real `/userdata`
- [ ] Tried on a Batocera: <!-- version and architecture, theme, what you did (one game from search to Save and Undo is
      enough), or "not applicable". A setup that is not in COMPATIBILITY.md earns a new line. -->

## Checklist

- [ ] Docs updated in the same change (README, `docs/USAGE.md`, `docs/ARCHITECTURE.md`, `CHANGELOG.md`), including any new setting
- [ ] Nothing is written outside the game's system folder (`images/`, `videos/`, `gamelist.xml`) and the program's own folder
- [ ] Every new network call goes through `scrappy/download.py` (public hosts only) or is listed in "What it contacts" in
      the README and in `SECURITY.md`
- [ ] The installer still touches nothing outside `/userdata/system/scrappy`, its shortcuts and the launcher log
      (and `tests/test_deploy.py` covers any change to it)
- [ ] No personal data in code, tests, docs or examples (real user or host names, private addresses); use
      `root@batocera.local` and `192.168.1.100`
- [ ] No third-party artwork or trailers added to the repository (`docs/screenshots/README.md` explains the one exception)
- [ ] Compatible with Python 3.11

## Anything you are unsure about

<!-- Optional, and genuinely useful. A PR that names its own weak spot gets a better review. -->
