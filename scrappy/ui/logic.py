"""Window logic that needs no Qt: texts, sizes, link checks and the decisions around saving.

Kept apart from the widgets so it can be unit-tested on any machine. Everything here treats text that came from the web
(titles, labels, error messages) as plain data: it only ever builds strings, the widgets show them as plain text.
"""
from __future__ import annotations

import html
import re
from urllib.parse import quote_plus, urlsplit

from ..dates import from_gamelist, to_gamelist
from ..providers.youtube import video_id_from_url

# ---------------------------------------------------------------- checkerboard behind transparent pictures

CHECKER_SQUARE = 12
CHECKER_LIGHT = 205
CHECKER_DARK = 160


def checker_shade(x: int, y: int, size: int = CHECKER_SQUARE) -> int:
    """Grey level of the checkerboard at pixel (x, y): light and dark squares alternate, the top-left one is light."""
    return CHECKER_LIGHT if ((x // size) + (y // size)) % 2 == 0 else CHECKER_DARK


def fit_within(width: int, height: int, box_w: int, box_h: int) -> tuple:
    """Size of a `width` x `height` picture scaled (up or down) to fit a box, keeping its shape. (0, 0) if nothing fits."""
    if min(width, height, box_w, box_h) <= 0:
        return 0, 0
    scale = min(box_w / width, box_h / height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def window_size(avail_w: int, avail_h: int) -> tuple:
    """A comfortable window for the free screen area: about 85 % of it, but never more than 1900 x 1000 (so a 3440 x 1440
    ultrawide does not get a stretched window), never less than 1100 x 680 unless the screen itself is smaller."""
    w = min(max(1100, int(avail_w * 0.85)), 1900)
    h = min(max(680, int(avail_h * 0.88)), 1000)
    return min(w, max(avail_w, 320)), min(h, max(avail_h, 240))


# ---------------------------------------------------------------- list and status texts

STATUS_NAMES = {"desc": "description", "image": "image", "thumbnail": "box art", "marquee": "logo", "video": "video"}


def names(tags) -> str:
    return ", ".join(STATUS_NAMES.get(t, t) for t in tags)


def row_text(row) -> str:
    """'✓ Title' for a finished game, '✗ Title   (missing: logo, video)' otherwise."""
    if row.complete:
        return f"✓ {row.title}"
    return f"✗ {row.title}   (missing: {names(row.missing)})"


def match_text(match) -> str:
    year = f" ({match.year})" if match.year else ""
    return f"{match.title}{year}  [{match.source}]  {int(max(0.0, min(1.0, match.score)) * 100)}%"


def format_errors(errors: dict) -> str:
    return "; ".join(f"{name}: {message}" for name, message in errors.items())


def candidate_caption(cand, note: str, index: int, total: int) -> str:
    parts = [p for p in (cand.label or cand.url[:60], cand.source, note) if p]
    return " · ".join(parts) + f" · {index + 1}/{total}"


def display_value(tag: str, value: str) -> str:
    """A gamelist value as a text box shows it: dates as YYYY-MM-DD (kept as they are when they cannot be converted)."""
    value = value or ""
    return (from_gamelist(value) or value) if tag == "releasedate" else value


def current_text(asset) -> str:
    """What a Current file is, in a few words: '640×360, 54 KB' for a picture, '1:23, 14 MB' for a video."""
    cand = asset.candidate
    size = size_text(asset.size_bytes)
    if cand.slot == "video":
        return f"{seconds_text(asset.duration) if asset.duration > 0 else 'length unknown'}, {size}"
    return f"{cand.width}×{cand.height}, {size}" if cand.width and cand.height else size


def current_caption(asset, index: int, total: int) -> str:
    """Caption of a picture panel that shows what the game has now."""
    cand = asset.candidate
    what = f"{cand.width}×{cand.height} · {size_text(asset.size_bytes)}" if cand.width and cand.height else \
        f"{asset.problem or 'could not be shown'} · {size_text(asset.size_bytes)}"
    counter = f" · {index + 1}/{total}" if total > 1 else ""
    if asset.tag_linked:
        return f"Current - {what} · stays as it is{counter}"
    return f"Current - not in the game list · {what} · Save links it{counter}"


def video_label(cand) -> str:
    label = cand.label or cand.url
    return f"{label}  [{cand.source}]" if cand.source else label


def seconds_text(seconds: float) -> str:
    s = int(round(max(0.0, seconds)))
    return f"{s // 60}:{s % 60:02d}"


# ---------------------------------------------------------------- video range

MIN_CLIP_SECONDS = 1.0


def clip_range(start: float, end: float, whole: bool, duration: float = 0.0) -> tuple:
    """(start, end) for the controller. `end` None means 'until the end' (the encoder cuts at the maximum length).
    The end is always at least a second after the start, and never beyond a known duration."""
    if whole:
        return 0.0, None
    start = max(0.0, float(start))
    end = max(float(end), start + MIN_CLIP_SECONDS)
    if duration and duration > 0:
        end = min(end, float(duration))
        start = min(start, max(0.0, end - MIN_CLIP_SECONDS))
    return start, end


def clip_length(start: float, end, duration: float, max_seconds: float):
    """Seconds the saved clip will last, or None when it is 'the whole trailer' of unknown length. Mirrors the encoder:
    never beyond the video's length (if known) and never beyond `max_seconds`."""
    if end is None:
        return min(float(duration), float(max_seconds)) if duration and duration > 0 else None
    stop = min(float(end), float(duration)) if duration and duration > 0 else float(end)
    return min(max(0.0, stop - float(start)), float(max_seconds))


def size_text(size: int) -> str:
    """'900 B', '48 KB', '1.5 MB', '14 MB' (decimal units, like the rest of the program)."""
    size = max(0, int(size))
    if size < 1000:
        return f"{size} B"
    if size < 1_000_000:
        return f"{size / 1000:.0f} KB"
    value = size / 1_000_000
    return f"{value:.0f} MB" if value >= 10 else f"{value:.1f}".rstrip("0").rstrip(".") + " MB"


def clip_summary(start: float, end, max_seconds: float) -> str:
    if end is None:
        return f"The whole trailer, cut at {seconds_text(max_seconds)} if longer."
    length = end - start
    text = f"Clip from {seconds_text(start)} to {seconds_text(end)} ({length:.0f} s)."
    if length > max_seconds:
        text += f" Only the first {seconds_text(max_seconds)} are kept."
    return text


# ---------------------------------------------------------------- links, web search, preview

SEARCH_WORDS = {"logo": "game logo transparent png", "boxart": "game box art cover", "image": "game screenshot",
                "video": "game trailer"}


def web_search_url(title: str, slot: str) -> str:
    words = SEARCH_WORDS.get(slot, "game")
    return "https://duckduckgo.com/?q=" + quote_plus(f"{title.strip()} {words}") + "&iax=images&ia=images"


def _plain_http(url: str) -> bool:
    if not isinstance(url, str) or not url or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        return False
    try:
        parts = urlsplit(url)
        parts.port                                             # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.hostname) and not parts.username and not parts.password


def normalize_link(text: str) -> str:
    """The address typed into 'Paste link', or ValueError with a plain message."""
    link = (text or "").strip()
    if not _plain_http(link):
        raise ValueError("That is not a web link. It must start with http:// or https:// and contain no spaces.")
    return link


def preview_command(cand, start: float, end, *, which, ytdlp_path: str = "", ytdlp_ok: bool = False, local_ok: bool = False) -> tuple:
    """(argv, "") to preview a video in an external player, or (None, reason).

    `which` is shutil.which (or a fake). Only http(s) links are handed to a player, YouTube links only as the
    canonical watch link of the video id, (the box's mpv 0.38 has no `ytdl` option, so none is passed).
    A ``file://`` address is played only with `local_ok` (the user chose that file in a dialog).
    """
    if local_ok and cand.url.startswith("file:///") and "\x00" not in cand.url:
        url = cand.url[7:]
    elif cand.kind == "youtube":
        vid = video_id_from_url(cand.url)
        if not vid:
            return None, "That is not a YouTube video link, so it cannot be previewed."
        url = f"https://www.youtube.com/watch?v={vid}"
    elif _plain_http(cand.url):
        url = cand.url
    else:
        return None, "That video address is not a plain http(s) link, so it cannot be previewed."
    start = max(0.0, float(start))
    mpv, vlc, ffplay = which("mpv"), which("cvlc") or which("vlc"), which("ffplay")
    if mpv:
        argv = [mpv, f"--start={start:g}", "--autofit=60%", "--msg-level=all=error"]       # quiet, but errors reach the log
        if end:
            argv.append(f"--end={float(end):g}")
        if cand.kind == "youtube":
            if not ytdlp_ok:
                return None, "The YouTube downloader is not installed yet (Tools > Install / update YouTube downloader)."
            argv.append(f"--script-opts=ytdl_hook-ytdl_path={ytdlp_path}")     # (the box's mpv has no --ytdl option)
        return argv + ["--", url], ""
    if cand.kind == "youtube" and (vlc or ffplay):
        return None, ("Previewing a YouTube video needs mpv with the YouTube downloader; the player found here "
                      f"({'vlc' if vlc else 'ffplay'}) cannot use it. The trailer can still be saved without a preview.")
    if vlc:
        argv = [vlc, "--play-and-exit", "--no-video-title-show", f"--start-time={start:g}"]
        if end:
            argv.append(f"--stop-time={float(end):g}")
        return argv + ["--", url], ""
    if ffplay:
        argv = [ffplay, "-loglevel", "error", "-autoexit", "-x", "1024", "-ss", f"{start:g}"]
        if not url.startswith("/"):                                  # a web address may only use web protocols
            argv += ["-protocol_whitelist", "http,https,tcp,tls,crypto"]
        if end:
            argv += ["-t", f"{max(1.0, float(end) - start):g}"]
        return argv + [url], ""
    return None, "No video player (mpv or vlc) was found, so the preview cannot be shown."


# ---------------------------------------------------------------- saving

ES_OVERWROTE_ONCE = "EmulationStation overwrote the list once; it was applied again."
ES_RELOADED = "Game lists reloaded in EmulationStation."


def saved_message(report, hint: str) -> str:
    """What the status line says after a save. `hint` (ES_RESTART_HINT) is shown only when EmulationStation runs but could
    not be asked to reload; when the reload worked the line says so, and a list EmulationStation overwrote is reported."""
    n, kept = len(report.written), len(report.kept)
    if report.dry_run:
        return (f"Dry run: nothing was written to the real folders ({n} file(s) went to "
                f"{report.journal.parent.parent}).")
    files = f"Saved {n} picture/video file(s)" if n else "Saved the game details"
    text = f"{files}; kept {kept} existing. Gamelist backed up. Use Undo last save to revert."
    linked = getattr(report, "linked", None) or []
    if linked:
        text += f" Linked {len(linked)} existing file(s) in the game list."
    warning = getattr(report, "es_warning", "")
    if warning:
        return text + " " + warning
    if getattr(report, "es_reloaded", False):
        return text + (" " + ES_OVERWROTE_ONCE if getattr(report, "es_reapplied", False) else "") + " " + ES_RELOADED
    return text + (" " + hint if report.es_running else "")


def install_texts(result) -> tuple:
    """(headline, detail lines) for a finished install or update of the YouTube downloader (an ``InstallResult``)."""
    version, before = result.version, result.previous_version
    if not result.was_installed:
        headline = "YouTube downloader installed."
        lines = [f"Version: {version}"] if version else []
    elif result.unchanged:
        headline = "YouTube downloader is up to date."
        lines = [f"Version {version} is the latest, so nothing needed to change."]
    else:
        headline = "YouTube downloader updated."
        lines = [f"Updated from version {before} to {version}." if before and version
                 else f"Now version {version}." if version else "The new copy replaced the old one."]
    return headline, lines + [f"File: {result.path}", "Search again to find YouTube videos."]


def undo_message(entry) -> str:
    if entry is None:
        return "Nothing to undo."
    left = entry.get("left_alone")
    text = f"Undone: {entry.get('rom_path', 'the last save')}" + (f" (left {len(left)} file(s) you changed since)" if left else "")
    if entry.get("es_warning"):
        return text + ". " + entry["es_warning"]
    if entry.get("es_reloaded"):
        again = " EmulationStation wrote its old list back; the undo was restored again." if entry.get("es_reapplied") else ""
        return text + "." + again + " " + ES_RELOADED
    return text


_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def one_line(text: str, limit: int = 600) -> str:
    """A message for the status line: control characters removed, long text shortened."""
    text = " ".join(_CTRL.sub(" ", str(text)).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


DATE_FORMATS = "2026-02-18 or Feb 18, 2026"


def date_problem(text: str) -> str:
    """'' if the release date box is empty or readable; otherwise a plain sentence saying what to type instead."""
    text = (text or "").strip()
    if not text or to_gamelist(text):
        return ""
    return (f"The release date “{text}” is not in a form I can read, so it would be left out. "
            f"Type it like {DATE_FORMATS} (or a year such as 2026), or clear the box.")


def plain_tooltip(text: str, limit: int = 600) -> str:
    """A tooltip that shows `text` literally. Qt treats anything that looks like HTML as rich text, so the text is escaped
    and wrapped in a paragraph: a title like '<img src=file:///etc/hosts>' is displayed, never interpreted."""
    text = str(text or "")
    if not text:
        return ""
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    return '<p style="white-space:pre-wrap">' + html.escape(text, quote=False) + "</p>"
