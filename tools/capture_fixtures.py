"""Save real Steam and YouTube responses as offline test fixtures.

Usage: .venv/bin/python tools/capture_fixtures.py [group ...]
Groups: openttd, portal2, missing, youtube (default: all). Re-capturing a group overwrites its files.

Only public, keyless endpoints are used, one request each, one second apart. The YouTube group runs
yt-dlp (it must be installed) with the same search options the program uses.
The script is deliberately independent of the scrappy package (it must not depend on the code the
fixtures test), so the endpoint URLs are repeated here.

Games: 'OpenTTD' (a free, open-source game on Steam, with library art),
'Portal 2' (an old, well-known game with library box art and many trailers) and, for YouTube,
'SuperTuxKart official trailer' (a free, open-source game whose project channel publishes trailers).

The DLC fixtures (`steam_*_9000001/9000002.json`, `steam_search_dlc.json`) are NOT captured here: they
are an add-on and its base game with invented names and ids, kept by hand (see tests/fixtures/README.md).

The only trimming is in the appdetails payload: fields the parser never reads and that only add
size (long HTML, system requirements, prices, ratings, ...) are dropped, listed in DETAILS_DROP.
Everything else is stored exactly as Steam returned it. In the YouTube answer only the fields
that identify the person who ran the capture (none are expected) would be removed; see YOUTUBE_DROP.
"""
import json
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.parse

import requests

OUT = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures"
UA = "Mozilla/5.0 (Scrappy fixture capture)"

SEARCH = "https://store.steampowered.com/api/storesearch/"
DETAILS = "https://store.steampowered.com/api/appdetails"
BROWSE = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1"

# appdetails fields the parser does not read
DETAILS_DROP = (
    "detailed_description", "supported_languages", "pc_requirements", "mac_requirements",
    "linux_requirements", "legal_notice", "price_overview", "packages", "package_groups",
    "ratings", "support_info", "content_descriptors", "capsule_imagev5", "recommendations",
    "achievements", "metacritic", "reviews", "demos", "dlc",
)

# group -> (search text, search fixture name, app id)
GAMES = {
    "openttd": ("OpenTTD", "steam_search_openttd.json", 1536610),
    "portal2": ("Portal 2", "steam_search_portal2.json", 620),
}
YOUTUBE_QUERY = "SuperTuxKart official trailer"
YOUTUBE_FILE = "youtube_search_supertuxkart.jsonl"
YOUTUBE_COUNT = 6
YOUTUBE_DROP = ("__x_forwarded_for_ip",)      # yt-dlp may echo the public address of the machine
MISSING_APPID = 99999999          # Steam answers {"success": false} / success 15 for ids that do not exist


def get(url, params):
    response = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=30)
    response.raise_for_status()
    time.sleep(1)                 # be polite
    return response.json()


def save(name, payload):
    (OUT / name).write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def browse_input(appid):
    return json.dumps({"ids": [{"appid": appid}], "context": {"language": "english", "country_code": "US"},
                       "data_request": {"include_assets": True}})


def capture_game(term, search_name, appid):
    save(search_name, get(SEARCH, {"term": term, "cc": "us", "l": "en"}))
    details = get(DETAILS, {"appids": appid, "cc": "us", "l": "en"})
    data = details.get(str(appid), {}).get("data")
    if isinstance(data, dict):
        for key in DETAILS_DROP:
            data.pop(key, None)
    save(f"steam_details_{appid}.json", details)
    save(f"steam_browse_{appid}.json", get(BROWSE, {"input_json": browse_input(appid)}))


def capture_youtube():
    """The same yt-dlp options as scrappy/providers/youtube.py search(), one search, flat results."""
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        raise SystemExit("the youtube group needs yt-dlp on the PATH")
    done = subprocess.run([ytdlp, "--ignore-config", "--no-plugin-dirs", "--flat-playlist", "--dump-json", "--no-warnings",
                           "--no-cache-dir", "--color", "never", "--socket-timeout", "20", "--",
                           f"ytsearch{YOUTUBE_COUNT}:{YOUTUBE_QUERY}"],
                          capture_output=True, text=True, timeout=120, check=True)
    lines = []
    for line in done.stdout.splitlines():
        entry = json.loads(line)
        for key in YOUTUBE_DROP:
            entry.pop(key, None)
        lines.append(json.dumps(entry, ensure_ascii=True))
    (OUT / YOUTUBE_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(groups):
    OUT.mkdir(parents=True, exist_ok=True)
    known = {*GAMES, "missing", "youtube"}
    wanted = set(groups) or known
    unknown = wanted - known
    if unknown:
        raise SystemExit(f"unknown group(s): {sorted(unknown)}")
    for group, args in GAMES.items():
        if group in wanted:
            capture_game(*args)
    if "youtube" in wanted:
        capture_youtube()
    if "missing" in wanted:
        save("steam_search_nothing.json", get(SEARCH, {"term": "zzzzqqqxx", "cc": "us", "l": "en"}))
        save("steam_details_missing.json", get(DETAILS, {"appids": MISSING_APPID, "cc": "us", "l": "en"}))
        save("steam_browse_missing.json", get(BROWSE, {"input_json": browse_input(MISSING_APPID)}))
    print("saved", sorted(p.name for p in OUT.glob("steam_*.json")))


if __name__ == "__main__":
    main(sys.argv[1:])
