# Known limitations (0.1.0)

An honest list of what the program does not do, and of risks that were looked at and accepted. Most come from the
comments in the code (`scrappy/saver.py`, `scrappy/controller.py`, `scrappy/download.py`, `scrappy/config.py`) and from the reviews of
it. For what might change see [ROADMAP.md](../ROADMAP.md); for what has been tried on which machine, [COMPATIBILITY.md](../COMPATIBILITY.md);
for the threat model, [SECURITY.md](../SECURITY.md).

## Your files and `gamelist.xml`

* **Comments in `gamelist.xml` are lost.** The file is read and written with Python's ElementTree, which drops XML
  comments. EmulationStation writes none, so this only matters if you added comments by hand. The file is also
  re-serialised as a whole, so tiny cosmetic differences (for example how an empty tag or a special character is
  written) can appear even in games the program did not touch. The content of those games is unchanged. Every save keeps a
  copy of the previous file, so nothing is lost.
* **Two ROMs with the same file name in different sub-folders share their media.** Media names come from the ROM file name
  only (`<ROM name>-image.jpg` in the system's `images/` folder), the same rule Batocera's scraper follows. Saving art
  for one of them will find the files already there for the other, and keep them unless you choose Replace.
* **Other tools name media files differently, and the program never touches those files.** Other scrapers and tools use their own
  naming schemes. Scrappy reads and writes the names listed above and in [USAGE.md](USAGE.md#files-scrappy-writes); `tools/verify_media_naming.py`
  compares a library with them, read-only. Rule: art that a game's `gamelist.xml` already points to is kept, whatever it is
  named, unless you choose Replace. If you do replace such art, the new file is written under the standard name, `gamelist.xml`
  is pointed at it, and the old file stays on disk, unused. The program never deletes your files.
* **Art on disk that `gamelist.xml` does not mention counts as missing** in the game list. (Saving the game links the
  program's standard file name if it is there.)
* **Only seven text fields are ever written:** title, description, release date, developer, publisher, genre, players.
  Rating, language, hash and everything else stay as they are. Nothing in the program can clear a field: an empty box
  means "leave as it is". Only fields whose text changed are written.
* **A save is not cancellable once files start to be written**, and Undo only goes back one save at a time, up to the last
  20 saves.
* **Journal and backup limits.** The journal keeps the newest 20 saves and 20 undone saves; backups keep the first
  `gamelist.xml` of each system forever plus the newest 30. Older ones are deleted, including the old copies of
  replaced media. Undo cannot go back further than that. A journal folder without a `journal.json` (left by a crash before
  one was written) is neither counted nor deleted.
* **EmulationStation can overwrite your text changes.** ES keeps its own copy of every `gamelist.xml` and may write it back
  when it exits, when you press "update game lists", or when a play count changes. With ES running, a save first asks ES to
  flush (reload) and waits ~2 s, then saves, asks ES to reload again, waits ~3 s and checks the file; if ES wrote its old list
  back the changes are applied once more, and if that fails too you get a warning to quit ES and save again. Undo does the same.
  This relies on ES's reload address (`127.0.0.1:1234/reloadgames`); if ES cannot be reached you only get a reminder to
  restart it. The check happens a few seconds after the save, so an overwrite that comes later (ES exit, a game played) can
  still undo the text changes: save the game again, the files are kept and re-linked. The program does not stop or restart ES.
  The program notices ES by looking for its process on this machine.

## Safety limits that were accepted

* **No lock between programs.** Saves and Undo are serialised inside one running copy of the program. Two copies of the
  program, or the program and a script of yours, do not lock against each other. The program does compare
  `gamelist.xml` with what it read just before writing, and re-reads and re-applies its change if it changed; if the
  file keeps changing (three tries) it gives up and changes nothing.
* **A tiny time window remains.** Between that last comparison and the file being swapped in, a few microseconds pass in
  which another program could still write the file; only stopping ES closes that window completely. Likewise, media
  paths are checked to be inside the system folder just before each write, but something could swap a folder for a link
  between the check and the write. Both were judged acceptable for the intended use: one user on a home network.
* **DNS lookups are not bounded.** Looking up a web address's name cannot be interrupted from Python. The time limits are
  checked before and after the lookup, so a very slow DNS server can make a download or search wait longer than the
  timeouts say.
* **Trailer addresses are only partly protected.** Steam and YouTube hosts are trusted by name. Any other video address
  must resolve to public addresses, but ffmpeg looks the name up again itself, so this is a guard against honest mistakes
  and simple tricks, not a complete defence.
* **Pictures from your own network are refused.** Links to a server on your home network (private or local addresses) are
  never downloaded, by design. Use **Choose file…** instead.
* **Cancel is not instant.** Cancelling a search or download stops it at the next check. In the progress window of the YouTube
  downloader this shows as "Cancelling…" until the download notices (up to the 60-second read timeout if GitHub has gone silent);
  pressing Esc again leaves at once. The reload of EmulationStation cannot be cancelled; it ends within about four seconds. If a server has gone silent, that is
  when its read times out (about ten seconds of silence for picture downloads; longer for the video tools). The encoder (ffmpeg) is stopped quickly.
* **Saves made by pre-release versions carry another name.** Their `<scrap name="...">` marker in `gamelist.xml` differs from the one new saves write, `<scrap name="Scrappy" date="...">`. Nothing reads the marker, so the mix is harmless.
* **The first-start tip writes one line to `config.json`.** After it is shown, `intro_seen` is set to `true`. If the data folder cannot
  be written, or the file has a problem (then it is never rewritten), the tip shows again at the next start.
* **Settings typos are ignored.** A setting name the program does not know in `config.json` is kept but silently not
  used, so a misspelt name has no effect and gives no message. The program also always reads `config.json` from the
  default folder, even if `data_dir` points elsewhere.

## Sources and conversion

* **The sources are Steam and YouTube, built into the program.** Games that Steam does not sell (most older console games,
  console exclusives, anything region-specific) get no match, and you supply the pictures and a trailer yourself and type the
  text. A provider contract exists in the code, and a plugin system for more sources is planned: see
  [PROVIDERS.md](PROVIDERS.md) and [ROADMAP.md](../ROADMAP.md).
* **Steam facts are PC facts.** The release date, developer and genre come from the PC store page. A console release can be
  years later or by another publisher. The description is Steam's short text, which may be marketing wording. Edit before
  you save.
* **Steam add-ons (DLC) borrow from the base game.** For a DLC entry the pictures of the base game are used (box art and
  logo) and labelled "(base game: …)". Check that they suit the game.
* **Logos are often missing.** Steam rarely has a transparent logo. The program then offers a picture that may need
  **Remove background** (which only works on a plain, solid background), or a generated text logo, which is plain.
* **The sizes are fixed, not read from the theme.** Pictures are written as 640x360 JPEG (image), 396x640 PNG (box art) and 600x300
  PNG (logo), and the trailer as 1280x720 H.264 at 30 fps. The program does not read
  the theme, so another theme may scale or crop them differently. Only one theme is verified.
* **Wide art becomes a crop.** Box art is portrait (396x640). If only a wide picture exists it is cropped, and the panel says so.
* **Trailer formats.** Steam trailers that are HLS streams (`.m3u8`) are the expected path. Trailers offered only as DASH streams
  (`.mpd`) have not been tested and may fail to encode. Steam trailers have varying quality.
* **YouTube downloads may be AV1 or VP9.** To avoid YouTube's refusals, the downloader accepts any codec at 720p or less
  and the program re-encodes everything to H.264. That works on the box's ffmpeg but can be slow, and re-encoding a
  YouTube video loses a little quality. Videos shorter than 10 seconds or longer than 20 minutes are not offered.
* **yt-dlp must be kept up to date.** YouTube changes often; when downloads fail, use Tools > Install / update YouTube
  downloader (or the Install YouTube downloader button in the Trailer panel). It needs Python 3.10 or newer on the box, and an
  internet connection to GitHub. The progress window shows the percentage only when GitHub announces the file size; otherwise it
  shows the megabytes so far. The downloaded file is checked (it must be a Python zip application that starts), not compared
  with a published checksum.
* **Video size and time.** Trailers are cut to at most `max_video_seconds` (120 by default; 600 at most). Encoding can take
  a minute or more on the box, depending on the clip. The trailer is encoded at about 1.5 Mbps with the sound kept.
  A video file of your own can be up to 4 GB; a picture up to 25 MB.
* **Preview is only a check.** It opens an external player (mpv; ffplay for non-YouTube videos) with the original online
  address, so it does not show the encoded result, it needs the internet, and YouTube previews also need the downloader. If
  neither player is installed there is no preview.
* **The vendored Pillow only fits some machine types.** `deploy/vendor.txt` pins two Python 3.11 (`cp311`) Linux wheels,
  one for 64-bit x86 (`x86_64`) and one for 64-bit ARM (`aarch64`, for example a Raspberry Pi). 32-bit ARM (`armv7l`) and
  32-bit x86 are not supported: the installer refuses them before it sends anything. If Batocera moves to another Python
  version, `deploy/vendor.txt` needs new pins.

## The window

* **Current is read from the files, not from ES.** The "Current" pictures and details are what `gamelist.xml` and the files on
  disk say. If EmulationStation holds unsaved changes in memory, they are not seen until it has written them (the save flushes
  them first). A picture of the current art is shown at most 640 px wide; a damaged picture file is listed as Current but
  without a preview, and can still be replaced. The current video has no thumbnail, and its length is shown only when
  `ffprobe` answers within a few seconds.
* **The review window shows previews, not the final bytes of the video.** The trailer is encoded after you press Save, so
  the review lists source, start, end and length, not a picture of the result.
* **Long descriptions are shortened on screen in the review window** (the full text is what gets saved).
* **No web search inside the program,** and **no paste link or drop for trailers.** (A trailer file can be chosen.)
* **No batch mode.** One game at a time.
* **No special gamepad support.** It is an ordinary desktop window: mouse, keyboard, Tab and arrow keys.
* **Text from the web is shown as plain text,** so descriptions with formatting arrive without it.
* **Languages.** Steam is asked for its US store in English, so titles and descriptions are in English. A game whose English name differs from its file name needs the
  name typed in by hand.
* **Release dates** must be a real date between 1970 and 2100 in a form the program understands; day-first forms such as
  18.02.2026 are refused because they are ambiguous.
