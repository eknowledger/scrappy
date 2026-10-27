"""Steam store data: no key needed.

Everything that comes back from the network is untrusted: fields are type-checked, only http(s) URLs are
kept, lists are capped, and a malformed answer gives partial results instead of an exception.
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime
from dataclasses import replace
from urllib.parse import urlsplit

import requests

from ..models import AssetCandidate, GameInfo, Match
from ..titlematch import title_score
from .base import Provider, ProviderError

SEARCH = "https://store.steampowered.com/api/storesearch/"
DETAILS = "https://store.steampowered.com/api/appdetails"
BROWSE = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1"
ASSET_BASE = "https://shared.akamai.steamstatic.com/store_item_assets/"
USER_AGENT = "Mozilla/5.0 (Scrappy)"
SKIP_GENRES = {"indie", "free to play", "early access", "casual", "massively multiplayer"}
MULTIPLAYER_HINTS = ("multi", "co-op", "pvp", "split screen", "mmo")

MAX_MATCHES = 8
MAX_SCREENSHOTS = 20
MAX_VIDEOS = 8
MAX_DESCRIPTION = 900
MAX_NAMES = 3                    # developers / publishers kept
MAX_LABEL_NAME = 80              # a trailer's or base game's name inside a candidate label

# asset and trailer URLs must be https on these hosts (every host seen in the captured answers ends in .steamstatic.com)
ASSET_HOST_SUFFIXES = (".steamstatic.com", ".akamaihd.net")

_MONTHS = {}
for _number, _names in enumerate(("jan january", "feb february", "mar march", "apr april", "may", "jun june", "jul july",
                                  "aug august", "sep sept september", "oct october", "nov november", "dec december"), 1):
    for _name in _names.split():
        _MONTHS[_name] = _number
_MONTH_DAY_YEAR = re.compile(r"([A-Za-z]+)\.? (\d{1,2}),? (\d{4})")
_DAY_MONTH_YEAR = re.compile(r"(\d{1,2}) ([A-Za-z]+)\.?,? (\d{4})")
_MONTH_YEAR = re.compile(r"([A-Za-z]+)\.?,? (\d{4})")
_YEAR = re.compile(r"\d{4}")
_ASSET_FORMAT = re.compile(r"[A-Za-z0-9_./-]*\$\{FILENAME\}[A-Za-z0-9_./?=&%-]*")
_ASSET_FILE = re.compile(r"[A-Za-z0-9_./-]+")
_APP_ID = re.compile(r"[0-9]{1,12}")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff\ufffe\uffff]")   # invalid in XML / UTF-8


# ---- untrusted-value helpers ---------------------------------------------------------------------

def _text(value) -> str:
    """A plain one-line string from a str or number; anything else is ''."""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return " ".join(_CONTROL_CHARS.sub("", value).split())
    return ""


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _strings(value) -> list:
    """A list of non-empty strings from a str or a list that may contain junk."""
    items = [value] if isinstance(value, str) else _list(value)
    return [t for t in (_text(i) for i in items if isinstance(i, str)) if t]


def _asset_url(value) -> str:
    """The URL if it is a plain https URL on a Steam asset host, else ''."""
    if not isinstance(value, str):
        return ""
    url = value.strip()
    if not url or len(url) > 2048 or _CONTROL_CHARS.search(url) or any(c.isspace() for c in url):
        return ""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return ""
    if parts.scheme.lower() != "https" or not host or parts.username or parts.password or port not in (None, 443):
        return ""
    return url if host.endswith(ASSET_HOST_SUFFIXES) else ""


def clean_html(text) -> str:
    """HTML description -> plain text with paragraph breaks (blank line) and line breaks kept."""
    if not isinstance(text, str):
        return ""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|h[1-6]|ul|ol|blockquote|table)\s*>", "\n\n", text)
    text = re.sub(r"(?i)<(ul|ol)\b[^>]*>", "\n\n", text)
    text = re.sub(r"(?i)<li\b[^>]*>", "\n- ", text)
    text = re.sub(r"</?[A-Za-z!][^>]*>", " ", text)          # real tags only: '1 < 2' stays
    text = _CONTROL_CHARS.sub("", html.unescape(text.replace("\r\n", "\n").replace("\r", "\n")))
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _shorten(text: str, limit: int = MAX_DESCRIPTION) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit - 3]
    m = re.search(r"\s\S*$", cut)
    if m and m.start() > limit // 2:
        cut = cut[:m.start()]
    return cut.rstrip() + "..."


# ---- release dates -------------------------------------------------------------------------------

def parse_release_date(text) -> str:
    """'Feb 18, 2026' -> '20260218T000000'. Anything that is not a real date ('Coming soon', 'Q3 2026') -> ''."""
    if not isinstance(text, str):
        return ""
    text = " ".join(text.split())
    year = month = day = None
    if m := _MONTH_DAY_YEAR.fullmatch(text):
        month, day, year = _MONTHS.get(m[1].lower()), m[2], m[3]
    elif m := _DAY_MONTH_YEAR.fullmatch(text):
        day, month, year = m[1], _MONTHS.get(m[2].lower()), m[3]
    elif m := _MONTH_YEAR.fullmatch(text):
        month, day, year = _MONTHS.get(m[1].lower()), "1", m[2]
    elif _YEAR.fullmatch(text):
        month, day, year = 1, "1", text
    if not (year and month and day):
        return ""
    try:
        when = datetime(int(year), int(month), int(day))
    except ValueError:
        return ""
    return when.strftime("%Y%m%dT000000") if 1970 <= when.year <= 2100 else ""


# ---- store page (appdetails) ---------------------------------------------------------------------

def _movie_url(movie: dict) -> str:
    candidates = [movie.get("hls_h264"), movie.get("dash_h264")]
    for key in ("mp4", "webm"):
        sizes = movie.get(key)
        if isinstance(sizes, dict):
            candidates += [sizes.get("max"), sizes.get("480")]
    return next((u for u in map(_asset_url, candidates) if u), "")


def parse_details(data) -> GameInfo:
    """GameInfo from the 'data' object of an appdetails answer. Never raises on malformed input."""
    data = data if isinstance(data, dict) else {}
    genres = [d for d in (_text(g.get("description")) for g in _list(data.get("genres")) if isinstance(g, dict))
              if d and d.lower() not in SKIP_GENRES]
    categories = {d.lower() for d in (_text(c.get("description")) for c in _list(data.get("categories"))
                                      if isinstance(c, dict)) if d}
    release = data.get("release_date")
    release = release if isinstance(release, dict) else {}
    appid = _text(data.get("steam_appid"))
    single_only = "single-player" in categories and not any(h in c for c in categories for h in MULTIPLAYER_HINTS)
    info = GameInfo(
        name=_text(data.get("name")),
        description=_shorten(clean_html(data.get("short_description")) or clean_html(data.get("about_the_game"))),
        release_date="" if release.get("coming_soon") is True else parse_release_date(release.get("date")),
        developer=", ".join(_strings(data.get("developers"))[:MAX_NAMES]),
        publisher=", ".join(_strings(data.get("publishers"))[:MAX_NAMES]),
        genre=" / ".join(genres[:2]),
        players="1" if single_only else "",
        source="Steam",
        source_id=appid if _APP_ID.fullmatch(appid) else "",
    )
    seen = set()

    def add(candidate):
        if candidate.url and (candidate.slot, candidate.url) not in seen:
            seen.add((candidate.slot, candidate.url))
            info.assets.append(candidate)
            return True
        return False

    shots = 0
    for shot in _list(data.get("screenshots")):
        if shots >= MAX_SCREENSHOTS:
            break
        shot = shot if isinstance(shot, dict) else {}
        full = _asset_url(shot.get("path_full"))
        url = full or _asset_url(shot.get("path_thumbnail"))
        size = (1920, 1080) if full else (600, 338)
        shots += add(AssetCandidate(slot="image", url=url, label="Steam screenshot", source="Steam",
                                    width=size[0], height=size[1]))
    add(AssetCandidate(slot="image", url=_asset_url(data.get("header_image")), label="Steam header", source="Steam",
                       width=460, height=215))
    add(AssetCandidate(slot="image", url=_asset_url(data.get("background_raw")) or _asset_url(data.get("background")),
                       label="Steam page background", source="Steam"))
    videos = 0
    for movie in _list(data.get("movies")):
        if videos >= MAX_VIDEOS:
            break
        movie = movie if isinstance(movie, dict) else {}
        url = _movie_url(movie)
        videos += add(AssetCandidate(
            slot="video", url=url, label=_shorten(_text(movie.get("name")), MAX_LABEL_NAME) or "Steam trailer", source="Steam",
            kind="hls" if urlsplit(url).path.lower().endswith(".m3u8") else "file",
            thumb_url=_asset_url(movie.get("thumbnail"))))
    return info


# ---- hashed asset URLs (IStoreBrowseService) -----------------------------------------------------

def _first_store_item_assets(browse) -> dict:
    response = browse.get("response") if isinstance(browse, dict) else None
    items = response.get("store_items") if isinstance(response, dict) else None
    for item in _list(items):
        if isinstance(item, dict) and item.get("success", 1) == 1 and isinstance(item.get("assets"), dict):
            return item["assets"]
    return {}


def parse_browse_assets(browse) -> list:
    """Hashed asset URLs from IStoreBrowseService: library art exists only for some games."""
    assets = _first_store_item_assets(browse)
    fmt = assets.get("asset_url_format")
    if not (isinstance(fmt, str) and _ASSET_FORMAT.fullmatch(fmt) and fmt.count("${FILENAME}") == 1
            and not fmt.startswith("/") and ".." not in fmt):
        return []

    def url(key):
        name = assets.get(key)
        if isinstance(name, str) and _ASSET_FILE.fullmatch(name) and ".." not in name and not name.startswith("/"):
            return ASSET_BASE + fmt.replace("${FILENAME}", name)
        return ""

    # (file key, slot, width, height, transparent, label): sizes were measured on the real files.
    # The 2x box art is the true 600x900 picture and is preferred; the 1x file is only 300x450.
    table = [("library_capsule_2x", "boxart", 600, 900, False, "Steam library box art"),
             ("library_capsule", "boxart", 300, 450, False, "Steam library box art"),
             ("library_logo", "logo", 0, 0, True, "Steam library logo"),
             ("library_hero", "image", 1920, 620, False, "Steam library hero"),
             ("main_capsule", "image", 616, 353, False, "Steam capsule"),
             ("hero_capsule", "image", 374, 448, False, "Steam hero capsule")]
    out, boxart_done = [], False
    for key, slot, w, h, transparent, label in table:
        if not url(key) or (slot == "boxart" and boxart_done):
            continue
        boxart_done = boxart_done or slot == "boxart"
        out.append(AssetCandidate(slot=slot, url=url(key), label=label, source="Steam", width=w, height=h,
                                  transparent=transparent))
    return out


# ---- the provider --------------------------------------------------------------------------------

class SteamProvider(Provider):
    name = "Steam"

    def __init__(self, session=None, timeout: float = 15.0):
        self.http = session if session is not None else requests.Session()
        self.http.headers.update({"User-Agent": USER_AGENT})
        self.timeout = timeout

    def _get_json(self, url, params=None) -> dict:
        """One GET. Any failure becomes a ProviderError with a plain message."""
        try:
            response = self.http.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
        except requests.Timeout as exc:
            raise ProviderError("Steam did not answer in time") from exc
        except requests.ConnectionError as exc:
            raise ProviderError("Steam could not be reached (check the internet connection)") from exc
        except Exception as exc:        # HTTP errors, bad JSON, anything a session may raise
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (429, 403):
                raise ProviderError("Steam asked us to slow down or refused the request, try again in a minute") from exc
            raise ProviderError("Steam gave an unusable answer") from exc
        if not isinstance(data, dict):
            raise ProviderError("Steam gave an unexpected answer")
        return data

    def search(self, title: str) -> list:
        title = _text(title)
        if not title:
            return []
        items = self._get_json(SEARCH, {"term": title, "cc": "us", "l": "en"}).get("items")
        matches = []
        for item in _list(items):
            if not isinstance(item, dict) or item.get("type", "app") != "app":
                continue
            appid, name = item.get("id"), _text(item.get("name"))
            if isinstance(appid, bool) or not isinstance(appid, (int, str)) or not _APP_ID.fullmatch(str(appid)) or not name:
                continue
            matches.append(Match(title=name, source=self.name, source_id=str(appid), score=title_score(title, name)))
        matches.sort(key=lambda m: -m.score)           # stable: Steam's own order breaks ties
        return matches[:MAX_MATCHES]

    def _browse_assets(self, appid: str) -> list:
        """Hashed library art of one app; the extra art is optional, so a failure gives nothing."""
        inp = {"ids": [{"appid": int(appid)}], "context": {"language": "english", "country_code": "US"},
               "data_request": {"include_assets": True}}
        try:
            return parse_browse_assets(self._get_json(BROWSE, {"input_json": json.dumps(inp)}))
        except ProviderError:
            return []

    def _logo_png(self, appid: str, label: str) -> list:
        """The store's transparent logo.png, if a HEAD request shows it exists (not every app has one)."""
        url = f"{ASSET_BASE}steam/apps/{appid}/logo.png"
        try:
            answer = self.http.head(url, timeout=self.timeout)
            content_type = str(answer.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            found = answer.status_code == 200 and content_type == "image/png"
        except Exception:               # no logo is not an error
            found = False
        return [AssetCandidate(slot="logo", url=url, label=label, source="Steam", transparent=True)] if found else []

    def _base_game_art(self, data: dict, appid: str, have: set) -> list:
        """A DLC has no library art of its own: box art and logo of its base game, clearly labelled. Nothing else is borrowed."""
        base = data.get("fullgame")
        base_id = base.get("appid") if isinstance(base, dict) else None
        base_id = str(base_id) if isinstance(base_id, (int, str)) and not isinstance(base_id, bool) else ""
        missing = {"boxart", "logo"} - have
        if data.get("type") != "dlc" or not _APP_ID.fullmatch(base_id) or base_id == appid or not missing:
            return []
        names = {"boxart": "Library box art", "logo": "Logo"}
        suffix = f"(base game: {_shorten(_text(base.get('name')), MAX_LABEL_NAME) or 'app ' + base_id})"
        borrowed = [replace(a, label=f"{names[a.slot]} {suffix}") for a in self._browse_assets(base_id) if a.slot in missing]
        if "logo" in missing and not any(a.slot == "logo" for a in borrowed):
            borrowed += self._logo_png(base_id, f"{names['logo']} {suffix}")
        return borrowed

    def fetch(self, match: Match) -> GameInfo:
        appid = match.source_id if isinstance(match.source_id, (int, str)) and not isinstance(match.source_id, bool) else ""
        appid = str(appid)
        if not _APP_ID.fullmatch(appid):
            raise ProviderError(f"Steam: '{appid}' is not a Steam app id")
        entry = self._get_json(DETAILS, {"appids": appid, "cc": "us", "l": "en"}).get(appid)
        if not (isinstance(entry, dict) and entry.get("success") is True and isinstance(entry.get("data"), dict)):
            raise ProviderError(f"Steam has no data for app {appid}")
        info = parse_details(entry["data"])
        info.name = info.name or _text(match.title)
        info.source, info.source_id = self.name, appid
        extra = self._browse_assets(appid) + self._logo_png(appid, "Steam logo")
        extra += self._base_game_art(entry["data"], appid, {a.slot for a in info.assets + extra})
        seen = {(a.slot, a.url) for a in info.assets}
        for asset in extra:
            if (asset.slot, asset.url) not in seen:
                seen.add((asset.slot, asset.url))
                info.assets.append(asset)
        return info
