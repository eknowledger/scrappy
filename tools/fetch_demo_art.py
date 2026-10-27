"""Downloads the artwork of the 16 real games of tools/real_showcase.py into a cache folder OUTSIDE the repository.

    python tools/fetch_demo_art.py                 # fetch whatever is not cached yet (idempotent)
    python tools/fetch_demo_art.py --list          # show what is cached / missing; exit 1 if a game lacks box art or a screenshot
    python tools/fetch_demo_art.py --only hades    # one game (slug), may be repeated

The cache is ``~/.cache/scrappy-demo-art/`` (environment variable ``SCRAPPY_DEMO_ART`` overrides it). NOTHING from it may ever be added
to git: the pictures belong to their owners (see docs/screenshots/README.md).

Sources (the only hosts this tool contacts):
* Steam, for the Switch and Windows titles: ``store.steampowered.com`` (storesearch, appdetails) and the image hosts
  ``shared.akamai.steamstatic.com`` / ``cdn.cloudflare.steamstatic.com`` / ``cdn.akamai.steamstatic.com``.
* libretro thumbnails, for the retro and PS3 titles: ``api.github.com`` (one file listing per system, cached) and
  ``raw.githubusercontent.com`` (the pictures).

Manners: a descriptive User-Agent, strictly one request at a time with a pause, everything is cached and never fetched twice,
a hard limit on the total number of requests (counted across runs in ``requests.json``), and the run stops after repeated
errors or at the first "slow down" answer. No trailer is downloaded; the clips are made later from the stills.

Layout of the cache:  index.json  requests.json  requests.log  listings/<repo>.json  steam/<id>-search.json
steam/<id>-details.json  files/<game>/<name>.<ext>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from difflib import get_close_matches
from pathlib import Path
from urllib.parse import quote, urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import requests  # noqa: E402

import real_showcase as rs  # noqa: E402
from scrappy.titlematch import title_score  # noqa: E402

USER_AGENT = "scrappy-demo-tool/1.0 (+https://github.com/eknowledger)"
PAUSE = 0.8                                      # seconds between two requests
MAX_TOTAL_REQUESTS = 400                         # over all runs
MAX_CONSECUTIVE_ERRORS = 3
ALLOWED_HOSTS = ("store.steampowered.com", "shared.akamai.steamstatic.com", "cdn.cloudflare.steamstatic.com",
                 "cdn.akamai.steamstatic.com", "api.github.com", "raw.githubusercontent.com")
SEARCH = "https://store.steampowered.com/api/storesearch/"
DETAILS = "https://store.steampowered.com/api/appdetails"
TREE = "https://api.github.com/repos/libretro-thumbnails/{repo}/git/trees/{branch}?recursive=1"
ROOT_TREE = "https://api.github.com/repos/libretro-thumbnails/{repo}/git/trees/master"
SUBTREE = "https://api.github.com/repos/libretro-thumbnails/{repo}/git/trees/{sha}"
STEAM_SCREENSHOTS = 3
IMAGE_MAGIC = ((b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"), (b"RIFF", ".webp"), (b"GIF8", ".gif"))


class Stop(Exception):
    """The run must end now (too many errors, the request limit, or a source asked us to slow down)."""


class Http:
    def __init__(self, folder: Path, pause: float = PAUSE, limit: int = MAX_TOTAL_REQUESTS):
        self.folder, self.pause, self.limit = folder, pause, limit
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.counts = self._load()
        self.run_count = 0
        self.errors = 0
        self._last = 0.0

    def _load(self) -> dict:
        try:
            data = json.loads((self.folder / "requests.json").read_text(encoding="utf-8"))
            return {"total": int(data.get("total", 0)), "hosts": dict(data.get("hosts", {}))}
        except (OSError, ValueError, TypeError, AttributeError):
            return {"total": 0, "hosts": {}}

    def _save(self) -> None:
        write_atomic(self.folder / "requests.json", json.dumps(self.counts, indent=1).encode())

    def get(self, url: str, accept: str = "") -> "bytes | None":
        """The body, or None for a 404. Anything else that is not a 200 counts as an error."""
        host = urlsplit(url).hostname or ""
        if urlsplit(url).scheme != "https" or host not in ALLOWED_HOSTS:
            raise Stop(f"refusing to contact {host or url}: not one of the allowed hosts")
        if self.counts["total"] >= self.limit:
            raise Stop(f"the limit of {self.limit} requests in total is reached")
        wait = self.pause - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        self.counts["total"] += 1
        self.counts["hosts"][host] = self.counts["hosts"].get(host, 0) + 1
        self.run_count += 1
        status, body = 0, None
        try:
            response = self.session.get(url, timeout=30, headers={"Accept": accept} if accept else None)
            status = response.status_code
            if status == 200:
                body = response.content
        except requests.RequestException as exc:
            status = -1
            print(f"  network error: {exc.__class__.__name__}")
        with (self.folder / "requests.log").open("a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {status} {url}\n")
        self._save()
        if status in (403, 429):
            raise Stop(f"{host} answered {status} (asking us to slow down): stopping")
        if status == 200:
            self.errors = 0
            return body
        if status == 404:
            self.errors = 0
            return None
        self.errors += 1
        print(f"  {url}: HTTP {status}")
        if self.errors >= MAX_CONSECUTIVE_ERRORS:
            raise Stop(f"{self.errors} errors in a row: stopping")
        return None


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def symlink_target(data: bytes, url: str) -> str:
    """raw.githubusercontent.com serves a git symlink as a tiny text file holding the target's relative path (a name in the
    same folder). Returns the address of the target, or '' when `data` is not such a file."""
    if len(data) > 300:
        return ""
    try:
        text = data.decode("utf-8").strip()
    except UnicodeDecodeError:
        return ""
    if not text.lower().endswith(".png") or "\n" in text or text.startswith(("/", ".")) or "/" in text:
        return ""
    return url.rsplit("/", 1)[0] + "/" + quote(text, safe="")


def image_extension(data: bytes) -> str:
    for magic, ext in IMAGE_MAGIC:
        if data.startswith(magic) and (ext != ".webp" or data[8:12] == b"WEBP"):
            return ext
    return ""


# ----------------------------------------------------------------------------------------------------- the index
def load_index(folder: Path) -> dict:
    return rs.ArtCache(folder).index


def save_index(folder: Path, index: dict) -> None:
    games = [index[g.slug] for g in rs.GAMES if g.slug in index]
    write_atomic(folder / "index.json", json.dumps({"version": 1, "games": games}, indent=1, ensure_ascii=False).encode())


def new_entry(game: rs.RealGame) -> dict:
    return {"slug": game.slug, "system": game.system, "title": game.title, "rom": game.rom, "source": game.source,
            "slots": {"logo": [], "boxart": [], "image": [], "video": []}, "not_found": []}


class Fetcher:
    def __init__(self, folder: Path, http: Http):
        self.folder, self.http = folder, http
        self.index = load_index(folder)

    # ----- one picture
    def picture(self, game, slot: str, role: str, name: str, url: str) -> bool:
        """Make sure the picture is cached and listed in the index. True when it is there afterwards."""
        entry = self.index.setdefault(game.slug, new_entry(game))
        have = [e for e in entry["slots"][slot] if e.get("role") == role and e.get("name") == name]
        if have and (self.folder / have[0]["file"]).is_file() and (self.folder / have[0]["file"]).stat().st_size > 0:
            return True
        if name in entry.setdefault("not_found", []):          # asked before: the source does not have it
            return False
        data = self.http.get(url)
        target = symlink_target(data, url) if data and not image_extension(data) else ""
        if target:                                    # libretro stores some pictures as git symlinks to another file
            print(f"  {name}: is a link to {target.rsplit('/', 1)[-1]}")
            data = self.http.get(target)
        ext = image_extension(data) if data else ""
        if not ext:
            if data is None:
                entry["not_found"].append(name)
                save_index(self.folder, self.index)
                print(f"  {name}: not there (404)")
            else:
                print(f"  {name}: not a picture, skipped")
            return False
        rel = f"files/{game.slug}/{name}{ext}"
        write_atomic(self.folder / rel, data)
        entry["slots"][slot] = [e for e in entry["slots"][slot] if not (e.get("role") == role and e.get("name") == name)]
        entry["slots"][slot].append({"file": rel, "url": url, "role": role, "name": name})
        save_index(self.folder, self.index)
        print(f"  {slot}/{name}: {len(data) // 1024} KB")
        return True

    def sort_slots(self, game) -> None:
        order = {"snap": 0, "screenshot": 0, "title": 1, "header": 2, "hero": 3}
        entry = self.index.get(game.slug)
        if entry:
            entry["slots"]["image"].sort(key=lambda e: (order.get(e.get("role"), 9), e.get("name", "")))
            save_index(self.folder, self.index)

    # ----- libretro
    def listing(self, repo: str) -> dict:
        path = self.folder / "listings" / f"{repo}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        folders = {name: [] for name in rs.LIBRETRO_FOLDERS.values()}
        data = self.http.get(TREE.format(repo=quote(repo), branch="master"), accept="application/vnd.github+json")
        if data is not None:
            tree = json.loads(data)
            if tree.get("truncated"):
                print(f"  warning: the listing of {repo} is truncated by GitHub; some names may be missing")
            items = tree.get("tree", [])
        else:                                    # too big for one answer: root tree, then one tree per picture folder
            self.http.errors = 0
            root = self.http.get(ROOT_TREE.format(repo=quote(repo)), accept="application/vnd.github+json")
            if root is None:
                raise Stop(f"could not list the pictures of {repo}")
            items = []
            for node in json.loads(root).get("tree", []):
                if node.get("path") in folders and node.get("sha"):
                    sub = self.http.get(SUBTREE.format(repo=quote(repo), sha=node["sha"]), accept="application/vnd.github+json")
                    if sub is None:
                        raise Stop(f"could not list {node['path']} of {repo}")
                    sub = json.loads(sub)
                    if sub.get("truncated"):
                        print(f"  warning: the listing of {node['path']} in {repo} is truncated")
                    items += [{"path": f"{node['path']}/{i.get('path')}"} for i in sub.get("tree", [])]
        for item in items:
            top, _, rest = str(item.get("path", "")).partition("/")
            if top in folders and rest and "/" not in rest and rest.lower().endswith(".png"):
                folders[top].append(rest[:-4])
        write_atomic(path, json.dumps(folders).encode())
        print(f"  listing of {repo}: " + ", ".join(f"{k} {len(v)}" for k, v in folders.items()))
        return folders

    def fetch_libretro(self, game) -> None:
        repo = rs.LIBRETRO_REPOS[game.system]
        names = self.listing(repo)
        for kind, folder in rs.LIBRETRO_FOLDERS.items():
            want = rs.libretro_file_name(game.libretro_name(kind), "")
            if want not in names.get(folder, []):
                close = get_close_matches(want, names.get(folder, []), n=3, cutoff=0.6)
                print(f"  {folder}: no file named {want!r}" + (f"; similar: {close}" if close else ""))
                continue
            slot, role = ("boxart", "boxart") if kind == "boxart" else ("image", kind)
            self.picture(game, slot, role, kind, rs.libretro_url(repo, kind, game.libretro_name(kind)))
        self.sort_slots(game)

    # ----- Steam
    def json_cached(self, rel: str, url: str):
        path = self.folder / rel
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        data = self.http.get(url)
        if data is None:
            return None
        parsed = json.loads(data)
        write_atomic(path, data)
        return parsed

    def fetch_steam(self, game) -> None:
        appid = game.steam_id
        found = self.json_cached(f"steam/{appid}-search.json",
                                 f"{SEARCH}?term={quote(game.title)}&cc=us&l=en")
        ids = [it.get("id") for it in (found or {}).get("items", []) if isinstance(it, dict)]
        best = max(((title_score(game.title, it.get("name", "")), it.get("id")) for it in (found or {}).get("items", [])
                    if isinstance(it, dict)), default=(0, None))
        if appid not in ids:
            print(f"  warning: the Steam search for {game.title!r} did not list app {appid}; best was {best}")
        details = self.json_cached(f"steam/{appid}-details.json", f"{DETAILS}?appids={appid}&cc=us&l=english")
        data = ((details or {}).get(str(appid)) or {}).get("data") or {}
        if not data:
            print(f"  no appdetails for {appid}")
        base = f"{rs.STEAM_ASSETS}{appid}/"
        self.picture(game, "logo", "logo", "logo", base + "logo.png")
        if not self.picture(game, "boxart", "library", "library_600x900_2x", base + "library_600x900_2x.jpg"):
            self.picture(game, "boxart", "library", "library_600x900", base + "library_600x900.jpg")
        shots = [s.get("path_full") for s in data.get("screenshots", []) if isinstance(s, dict)]
        for n, url in enumerate([u for u in shots if self.allowed(u)][:STEAM_SCREENSHOTS], 1):
            self.picture(game, "image", "screenshot", f"screenshot-{n}", url)
        if self.allowed(data.get("header_image")):
            self.picture(game, "image", "header", "header", data["header_image"])
        self.picture(game, "image", "hero", "library_hero", base + "library_hero.jpg")
        movies = [m for m in data.get("movies", []) if isinstance(m, dict) and self.allowed(m.get("thumbnail"))]
        if movies:
            self.picture(game, "video", "movie-thumb", "movie-thumb", movies[0]["thumbnail"])
        self.sort_slots(game)

    @staticmethod
    def allowed(url) -> bool:
        if not isinstance(url, str):
            return False
        parts = urlsplit(url)
        return parts.scheme == "https" and parts.hostname in ALLOWED_HOSTS and not parts.username


# ------------------------------------------------------------------------------------------------------------ report
def status_lines(cache: rs.ArtCache, games) -> list:
    lines = []
    for g in games:
        parts = []
        for slot in ("logo", "boxart", "image", "video"):
            parts.append(f"{slot} {len(cache.entries(g.slug, slot))}")
        lacks = [s for s in rs.MIN_SLOTS if not cache.entries(g.slug, s)]
        lines.append(f"{'OK     ' if not lacks else 'MISSING'} {g.system:<9} {g.title:<40} " + ", ".join(parts)
                     + (f"   lacks: {', '.join(lacks)}" if lacks else ""))
    return lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Download the artwork for the real-titles screenshots (developer tool).")
    parser.add_argument("--list", action="store_true", help="only show what is cached and what is missing")
    parser.add_argument("--only", action="append", default=[], metavar="SLUG", help="fetch only this game (repeatable)")
    parser.add_argument("--pause", type=float, default=PAUSE, help="seconds between two requests (default %(default)s)")
    args = parser.parse_args(argv)
    folder = rs.cache_dir()
    games = [g for g in rs.GAMES if not args.only or g.slug in args.only]
    if args.only and not games:
        print("no such game; known: " + ", ".join(g.slug for g in rs.GAMES))
        return 2
    if not args.list:
        folder.mkdir(parents=True, exist_ok=True)
        http = Http(folder, pause=args.pause)
        fetcher = Fetcher(folder, http)
        try:
            for g in games:
                print(f"{g.system}: {g.title} ({g.source})")
                (fetcher.fetch_steam if g.source == rs.STEAM else fetcher.fetch_libretro)(g)
        except Stop as exc:
            print(f"stopped: {exc}")
        finally:
            print(f"requests this run: {http.run_count}; in total: {http.counts['total']} {http.counts['hosts']}")
    cache = rs.ArtCache(folder)
    print(f"\ncache: {folder}")
    print("\n".join(status_lines(cache, games)))
    return 1 if cache.missing(games) else 0


if __name__ == "__main__":
    sys.exit(main())
