import json
import pathlib

import pytest
import requests

from scrappy.models import Match
from scrappy.providers.base import Provider, ProviderError
from scrappy.providers.steam import (
    MAX_MATCHES, MAX_SCREENSHOTS, MAX_VIDEOS, SteamProvider, clean_html, parse_browse_assets, parse_details,
    parse_release_date,
)

FIX = pathlib.Path(__file__).parent / "fixtures"
# The add-on case (apps 9000002 and 9000001) is anonymised: real structure, invented names and ids (tests/fixtures/README.md).


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def details(appid):
    return load(f"steam_details_{appid}.json")[str(appid)]["data"]


# ---- release dates -------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("Feb 18, 2026", "20260218T000000"),
    ("18 Feb, 2026", "20260218T000000"),
    ("February 18, 2026", "20260218T000000"),
    ("Sept 3, 2026", "20260903T000000"),
    ("Feb 2026", "20260201T000000"),
    ("February 2026", "20260201T000000"),
    ("2026", "20260101T000000"),
    ("  Apr 18, 2011 ", "20110418T000000"),
    ("Coming soon", ""),
    ("To be announced", ""),
    ("Q3 2026", ""),
    ("Feb 30, 2026", ""),         # not a real day
    ("Feb 18, 1850", ""),         # outside what a gamelist can hold
    ("", ""),
    (None, ""),
    (2026, ""),
    (["Feb 18, 2026"], ""),
])
def test_parse_release_date(text, expected):
    assert parse_release_date(text) == expected


# ---- text cleaning -------------------------------------------------------------------------------

def test_clean_html_keeps_paragraph_breaks_and_decodes_entities():
    raw = ('<p class="bb_paragraph"><strong>Hello</strong>   &amp; welcome</p><p>Second&nbsp;para &quot;quoted&quot;</p>'
           "Line one<br>\t\t\tLine two<ul><li>a</li><li>b</li></ul>")
    assert clean_html(raw) == 'Hello & welcome\n\nSecond para "quoted"\n\nLine one\nLine two\n\n- a\n- b'


def test_clean_html_drops_scripts_and_survives_junk():
    assert clean_html("a<script>alert(1)</script>b<style>p{}</style>c") == "a b c"
    assert clean_html("1 < 2 and 3 > 2") == "1 < 2 and 3 > 2"
    for junk in (None, 5, [], {}, b"x"):
        assert clean_html(junk) == ""


def test_clean_html_collapses_blank_lines_and_trims():
    assert clean_html("<br><br><br>x<br><br><br><br>y<br>") == "x\n\ny"


# ---- parse_details on real data ------------------------------------------------------------------

def test_parse_details_dlc():
    info = parse_details(details(9000002))
    assert info.name == "Tidepool Tactics - Chapter 5"
    assert info.release_date == "20260218T000000"
    assert info.developer == "Kettle Works" and info.publisher == "Kettle Works"
    assert "Captain Pickles" in info.description and "<" not in info.description and "&" not in info.description
    assert info.genre == "Action / Adventure"            # 'Indie' is dropped
    assert info.players == "1"
    assert info.source == "Steam" and info.source_id == "9000002"
    videos = info.assets_for("video")
    assert len(videos) == 4 and all(v.kind == "hls" and ".m3u8" in v.url and v.label for v in videos)
    assert all(v.thumb_url.startswith("https://") for v in videos)
    assert len(info.assets_for("image")) >= 12            # screenshots + header + background
    assert all(a.url.startswith("https://") and a.source == "Steam" for a in info.assets)
    assert info.assets_for("logo") == [] and info.assets_for("boxart") == []


def test_parse_details_openttd_free_open_source_game():
    info = parse_details(details(1536610))
    assert info.name == "OpenTTD" and info.release_date == "20210401T000000"
    assert info.developer == "OpenTTD" and info.publisher == "OpenTTD"
    assert "transporting passengers" in info.description and "<" not in info.description
    assert info.genre == "Simulation"                           # Indie and Casual are dropped (SKIP_GENRES)
    assert info.players == ""                                  # it has multiplayer, so not "1"
    assert info.source == "Steam" and info.source_id == "1536610"
    assert info.assets_for("video") and all(v.kind in ("hls", "file") for v in info.assets_for("video"))
    assert len(info.assets_for("image")) >= 12
    assert all(a.url.startswith("https://") and a.source == "Steam" for a in info.assets)


def test_parse_details_portal2_older_game():
    info = parse_details(details(620))
    assert info.name == "Portal 2"
    assert info.release_date == "20110418T000000"
    assert info.developer == "Valve" and info.publisher == "Valve"
    assert info.genre == "Action / Adventure"
    assert info.players == ""                              # has co-op, so not "1"
    assert '"Perpetual Testing Initiative"' in info.description    # &quot; decoded
    assert len(info.assets_for("video")) == MAX_VIDEOS     # 17 movies on the page, capped
    assert len([a for a in info.assets_for("image") if a.label == "Steam screenshot"]) == 12
    urls = [a.url for a in info.assets]
    assert len(urls) == len(set(urls))


# ---- parse_details on hostile data ---------------------------------------------------------------

@pytest.mark.parametrize("bad", [None, [], "x", 5, {}])
def test_parse_details_never_crashes_on_wrong_top_level(bad):
    info = parse_details(bad)
    assert info.name == "" and info.assets == []


def test_parse_details_survives_wrong_field_types():
    data = {
        "name": 123, "steam_appid": None, "short_description": None, "about_the_game": ["x"],
        "release_date": "Feb 18, 2026", "developers": "Solo Dev", "publishers": [None, 5, "Pub"],
        "genres": [{"description": None}, "Action", {"description": "Strategy"}, None, {"description": "Indie"}],
        "categories": {"description": "Single-player"},
        "screenshots": {"a": 1}, "movies": "none", "header_image": 42, "background": ["x"],
    }
    info = parse_details(data)
    assert info.name == "123" and info.source_id == ""
    assert info.description == "" and info.release_date == ""
    assert info.developer == "Solo Dev" and info.publisher == "Pub"
    assert info.genre == "Strategy" and info.players == ""
    assert info.assets == []


def test_parse_details_coming_soon_has_no_date_even_if_one_is_given():
    data = {"name": "X", "release_date": {"coming_soon": True, "date": "2026"}}
    assert parse_details(data).release_date == ""
    assert parse_details({"name": "X", "release_date": {"coming_soon": False, "date": "Q3 2026"}}).release_date == ""
    assert parse_details({"name": "X", "release_date": "Feb 18, 2026"}).release_date == ""
    assert parse_details({"name": "X"}).release_date == ""


def test_parse_details_only_keeps_https_urls_on_steam_hosts():
    ok = "https://shared.akamai.steamstatic.com"
    shots = [{"path_full": u, "path_thumbnail": ""} for u in
             ("javascript:alert(1)", "file:///etc/passwd", "ftp://h/x.jpg", "//cdn/x.jpg", "https:///nohost.jpg", "",
              "http://shared.akamai.steamstatic.com/plain-http.jpg",            # http is dropped
              "https://evil.example/a.jpg", "https://steamstatic.com.evil.example/a.jpg",
              "https://evilsteamstatic.com/a.jpg", "https://user:pw@shared.akamai.steamstatic.com/u.jpg",
              "https://shared.akamai.steamstatic.com:8443/p.jpg", "https://shared.akamai.steamstatic.com/a b.jpg",
              ok + "/b.jpg", "https://steamcdn-a.akamaihd.net/c.jpg", "https://SHARED.AKAMAI.STEAMSTATIC.COM/d.jpg")]
    movies = [{"name": "m1", "hls_h264": "file:///tmp/x.m3u8", "thumbnail": "javascript:x"},
              {"name": "m2", "hls_h264": "https://video.akamai.steamstatic.com/m.m3u8", "thumbnail": "https://evil.example/t.jpg"},
              {"name": "m3", "mp4": {"480": "ftp://v/x.mp4", "max": "https://video.akamai.steamstatic.com/max.mp4"}},
              {"name": "m4", "hls_h264": "https://evil.example/m.m3u8"}]
    info = parse_details({"name": "X", "screenshots": shots, "header_image": "data:image/png;base64,AAAA",
                          "background_raw": "gopher://x", "background": "https://store.akamai.steamstatic.com/bg.jpg",
                          "movies": movies})
    assert [a.url for a in info.assets_for("image")] == [
        ok + "/b.jpg", "https://steamcdn-a.akamaihd.net/c.jpg", "https://SHARED.AKAMAI.STEAMSTATIC.COM/d.jpg",
        "https://store.akamai.steamstatic.com/bg.jpg"]
    assert [(v.url, v.kind, v.thumb_url) for v in info.assets_for("video")] == [
        ("https://video.akamai.steamstatic.com/m.m3u8", "hls", ""),
        ("https://video.akamai.steamstatic.com/max.mp4", "file", "")]


def test_every_asset_url_in_the_captured_fixtures_is_on_an_allowed_host():
    """The allow-list must not throw away anything Steam really sends for assets."""
    for appid, shots, movies in ((9000002, 12, 4), (620, 12, MAX_VIDEOS)):
        info = parse_details(details(appid))
        assert len(info.assets_for("video")) == movies
        assert len([a for a in info.assets_for("image") if a.label == "Steam screenshot"]) == shots
        assert any(a.label == "Steam header" for a in info.assets) and any(a.label == "Steam page background" for a in info.assets)


def test_parse_details_prefers_hls_then_dash_then_mp4_and_falls_through():
    h = "https://video.akamai.steamstatic.com/"
    movies = [{"name": "a", "hls_h264": h + "a.m3u8", "dash_h264": h + "a.mpd", "mp4": {"max": h + "a.mp4"}},
              {"name": "b", "dash_h264": h + "b.mpd", "mp4": {"max": h + "b.mp4"}},
              {"name": "c", "mp4": {"max": h + "c.mp4"}},
              {"name": "d", "webm": {"max": h + "d.webm"}},
              {"name": "e", "mp4": "not a dict"}]
    got = [(v.url, v.kind) for v in parse_details({"name": "X", "movies": movies}).assets_for("video")]
    assert got == [(h + "a.m3u8", "hls"), (h + "b.mpd", "file"), (h + "c.mp4", "file"), (h + "d.webm", "file")]


def test_clean_text_drops_lone_surrogates_and_noncharacters():
    nasty = "Good\ud800 text\udfff with \ufffe\uffff junk &#xD800; and &#65535;"
    info = parse_details({"name": "Na\ud800me", "short_description": nasty, "developers": ["De\udc00v"]})
    assert info.description.startswith("Good text with junk")
    assert not any(0xD800 <= ord(c) <= 0xDFFF or ord(c) in (0xFFFE, 0xFFFF) for c in info.description)
    assert info.name == "Name" and info.developer == "Dev"
    for text in (info.description, info.name, info.developer):
        text.encode("utf-8")
    assert clean_html("a\ud800b").encode("utf-8") == b"ab"


def test_parse_details_caps_list_sizes_and_removes_duplicates():
    shots = [{"path_full": f"https://s.steamstatic.com/{i}.jpg"} for i in range(MAX_SCREENSHOTS + 15)]
    shots.append({"path_full": "https://s.steamstatic.com/0.jpg"})
    movies = [{"name": str(i), "hls_h264": f"https://v.steamstatic.com/{i}.m3u8"} for i in range(MAX_VIDEOS + 5)]
    info = parse_details({"name": "X", "screenshots": shots, "movies": movies, "developers": [f"d{i}" for i in range(9)],
                          "short_description": "word " * 1000})
    assert len(info.assets_for("image")) == MAX_SCREENSHOTS
    assert len(info.assets_for("video")) == MAX_VIDEOS
    assert info.developer.count(",") < 5
    assert len(info.description) <= 900 and info.description.endswith("...")


def test_parse_details_description_fallback_and_truncation_at_a_word():
    assert parse_details({"name": "X", "short_description": "", "about_the_game": "<p>From <b>about</b></p>"}).description == "From about"
    long = " ".join(["abcdefghi"] * 200)
    desc = parse_details({"name": "X", "short_description": long}).description
    assert len(desc) <= 900 and desc.endswith("abcdefghi...")


def test_parse_details_players_only_for_pure_single_player():
    def players(*cats):
        return parse_details({"name": "X", "categories": [{"description": c} for c in cats]}).players
    assert players("Single-player", "Steam Achievements") == "1"
    assert players("Single-player", "Online Co-op") == ""
    assert players("Single-player", "Multi-player") == ""
    assert players("Multi-player") == ""
    assert players() == ""


# ---- parse_browse_assets -------------------------------------------------------------------------

def test_parse_browse_assets_dlc_has_no_logo_or_boxart():
    slots = {a.slot for a in parse_browse_assets(load("steam_browse_9000002.json"))}
    assert "image" in slots and "logo" not in slots and "boxart" not in slots


def test_parse_browse_assets_openttd_has_library_art():
    got = {a.slot: a for a in parse_browse_assets(load("steam_browse_1536610.json"))}
    assert got["boxart"].url.startswith("https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/1536610/library_600x900_2x.jpg?t=")
    assert (got["boxart"].width, got["boxart"].height) == (600, 900)


def test_parse_browse_assets_portal2_has_library_art():
    got = {a.slot: a for a in parse_browse_assets(load("steam_browse_620.json"))}
    box = got["boxart"]
    assert box.url.startswith("https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/620/library_600x900_2x.jpg?t=")
    assert (box.width, box.height) == (600, 900)            # measured: the _2x file really is 600x900
    assert any("library_hero" in a.url for a in parse_browse_assets(load("steam_browse_620.json")) if a.slot == "image")


def test_parse_browse_assets_maps_library_art():
    browse = {"response": {"store_items": [{"assets": {
        "asset_url_format": "steam/apps/1/${FILENAME}?t=1",
        "library_capsule_2x": "aa/library_600x900_2x.jpg", "library_hero": "bb/library_hero.jpg", "library_logo": "cc/logo.png"}}]}}
    got = {a.slot: a for a in parse_browse_assets(browse)}
    assert got["boxart"].url.endswith("steam/apps/1/aa/library_600x900_2x.jpg?t=1")
    assert got["logo"].transparent is True and got["logo"].url.endswith("cc/logo.png?t=1")
    assert got["image"].url.endswith("bb/library_hero.jpg?t=1")


def test_parse_browse_assets_box_art_prefers_the_2x_file_and_declares_true_sizes():
    def browse(**files):
        return {"response": {"store_items": [{"assets": {"asset_url_format": "steam/apps/1/${FILENAME}?t=1", **files}}]}}
    both = [a for a in parse_browse_assets(browse(library_capsule="a/c.jpg", library_capsule_2x="a/c_2x.jpg")) if a.slot == "boxart"]
    assert [(a.url.split("/")[-1], a.width, a.height) for a in both] == [("c_2x.jpg?t=1", 600, 900)]
    only_1x = [a for a in parse_browse_assets(browse(library_capsule="a/c.jpg")) if a.slot == "boxart"]
    assert [(a.url.split("/")[-1], a.width, a.height) for a in only_1x] == [("c.jpg?t=1", 300, 450)]
    junk_2x = [a for a in parse_browse_assets(browse(library_capsule="a/c.jpg", library_capsule_2x="../x.jpg")) if a.slot == "boxart"]
    assert [(a.width, a.height) for a in junk_2x] == [(300, 450)]
    assert [a for a in parse_browse_assets(browse(library_capsule_2x=None)) if a.slot == "boxart"] == []


def test_parse_browse_assets_declares_measured_sizes_for_the_other_art():
    got = {(a.label, a.width, a.height) for a in parse_browse_assets(load("steam_browse_620.json"))}
    assert ("Steam library hero", 1920, 620) in got
    assert ("Steam capsule", 616, 353) in got
    assert ("Steam hero capsule", 374, 448) in got               # nearly portrait, not a wide picture


@pytest.mark.parametrize("browse", [
    None, [], "x", {}, {"response": None}, {"response": []}, {"response": {"store_items": None}},
    {"response": {"store_items": []}}, {"response": {"store_items": ["x", None, 3]}},
    {"response": {"store_items": [{"assets": None}]}}, {"response": {"store_items": [{"assets": {}}]}},
    load("steam_browse_missing.json"),
])
def test_parse_browse_assets_tolerates_junk_and_missing_games(browse):
    assert parse_browse_assets(browse) == []


@pytest.mark.parametrize("fmt", [
    None, 5, "", "steam/apps/1/no-placeholder.jpg", "https://evil.example/${FILENAME}", "//evil.example/${FILENAME}",
    "/${FILENAME}", "../../${FILENAME}", "steam/apps/1/${FILENAME} x", "steam\\apps\\${FILENAME}",
])
def test_parse_browse_assets_rejects_suspicious_url_formats(fmt):
    browse = {"response": {"store_items": [{"assets": {"asset_url_format": fmt, "library_capsule": "a/b.jpg"}}]}}
    assert parse_browse_assets(browse) == []


def test_parse_browse_assets_skips_suspicious_file_names():
    browse = {"response": {"store_items": [{"assets": {
        "asset_url_format": "steam/apps/1/${FILENAME}?t=1",
        "library_capsule": "../../../x.jpg", "library_logo": "https://evil.example/l.png", "library_hero": 7,
        "main_capsule": "ok/capsule_616x353.jpg", "hero_capsule": None}}]}}
    got = parse_browse_assets(browse)
    assert [(a.slot, a.url.rsplit("/", 2)[-2:]) for a in got] == [("image", ["ok", "capsule_616x353.jpg?t=1"])]


# ---- the provider --------------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload, error=None):
        self.payload, self.error = payload, error

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeHead:
    def __init__(self, status=200, content_type="image/png"):
        self.status_code = status
        self.headers = {"Content-Type": content_type} if content_type else {}


class FakeSession:
    """routes: str or (str, str) key -> payload; the key must appear in url + parameter values.
    heads: url fragment -> FakeHead or an exception; every other HEAD answers 404."""

    def __init__(self, routes, heads=None):
        self.routes, self.heads, self.headers, self.calls, self.head_calls = routes, heads or {}, {}, [], []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        haystack = url + " " + " ".join(str(v) for v in (params or {}).values())
        for key, payload in self.routes.items():
            if all(part in haystack for part in ((key,) if isinstance(key, str) else key)):
                if isinstance(payload, Exception) and not isinstance(payload, ValueError):
                    raise payload
                return payload if isinstance(payload, FakeResponse) else FakeResponse(payload)
        raise AssertionError("unexpected url " + haystack)

    def head(self, url, timeout=None, **kwargs):
        self.head_calls.append((url, timeout))
        for fragment, answer in self.heads.items():
            if fragment in url:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return FakeHead(404, "text/html")


def browse_key(appid):
    return ("IStoreBrowseService", f'"appid": {appid}')


def fixture_routes(appid="9000002", search="steam_search_dlc.json", browse=None):
    routes = {"storesearch": load(search), "appdetails": load(f"steam_details_{appid}.json"),
              browse_key(appid): load(f"steam_browse_{appid}.json") if browse is None else browse}
    if appid == "9000002":
        routes[browse_key(9000001)] = load("steam_browse_9000001.json")
    return routes


PARENT_LOGO = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/9000001/logo.png"


def dlc_data(**extra):
    data = {"name": "A DLC", "type": "dlc", "steam_appid": 55, "fullgame": {"appid": "44", "name": "The Base Game"}}
    data.update(extra)
    return {"55": {"success": True, "data": data}}


def browse_with(appid, **files):
    return {"response": {"store_items": [{"success": 1, "assets": {
        "asset_url_format": f"steam/apps/{appid}/${{FILENAME}}?t=1", **files}}]}}


def fetch(session, appid="9000002"):
    return SteamProvider(session=session).fetch(Match(title="x", source="Steam", source_id=appid))


def test_provider_is_a_provider_with_a_name():
    assert issubclass(SteamProvider, Provider) and SteamProvider.name == "Steam"
    assert issubclass(ProviderError, Exception)


def test_provider_search_and_fetch_with_fake_session():
    session = FakeSession(fixture_routes(), heads={"/9000001/logo.png": FakeHead()})
    p = SteamProvider(session=session)
    matches = p.search("Tidepool Tactics Chapter 5")
    assert matches and matches[0].source == "Steam" and matches[0].source_id == "9000002"
    assert matches[0].title == "Tidepool Tactics - Chapter 5" and matches[0].score == 1.0
    assert len(session.calls) == 1
    info = p.fetch(Match(title="x", source="Steam", source_id="9000002"))
    assert info.name == "Tidepool Tactics - Chapter 5" and info.source == "Steam" and info.source_id == "9000002"
    assert len(info.assets_for("video")) == 4
    # one search; then details + own browse + parent browse, and the two logo.png checks
    assert len(session.calls) == 4 and len(session.head_calls) == 2


def test_provider_sets_user_agent_and_timeout():
    session = FakeSession(fixture_routes())
    p = SteamProvider(session=session, timeout=7)
    assert "Mozilla" in session.headers["User-Agent"]
    p.search("Portal 2")
    p.fetch(Match(title="x", source="Steam", source_id="9000002"))
    assert len(session.calls) == 4 and {c[2] for c in session.calls} == {7}
    assert {h[1] for h in session.head_calls} == {7}
    assert SteamProvider().http.headers["User-Agent"] == session.headers["User-Agent"]


def test_provider_fetch_portal2_includes_library_art_and_no_base_game_borrowing():
    session = FakeSession(fixture_routes("620", "steam_search_portal2.json"))
    info = fetch(session, "620")
    assert info.name == "Portal 2" and info.assets_for("boxart")
    urls = [(a.slot, a.url) for a in info.assets]
    assert len(urls) == len(set(urls))
    assert not any("base game" in a.label for a in info.assets)
    assert len(session.calls) == 2 and len(session.head_calls) == 1          # details + browse, one logo.png check


# ---- logo.png (HEAD checked) ---------------------------------------------------------------------

LOGO_620 = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/620/logo.png"


def test_logo_png_is_added_when_head_says_png():
    info = fetch(FakeSession(fixture_routes("620"), heads={"/620/logo.png": FakeHead(200, "image/png")}), "620")
    logos = info.assets_for("logo")
    assert [(a.url, a.transparent, a.label, a.source) for a in logos] == [(LOGO_620, True, "Steam logo", "Steam")]


@pytest.mark.parametrize("answer", [
    FakeHead(404, "text/html"), FakeHead(200, "text/html"), FakeHead(200, None), FakeHead(200, "image/jpeg"),
    FakeHead(301, "image/png"), FakeHead(500, "image/png"), requests.Timeout("slow"), requests.ConnectionError("down"),
    RuntimeError("weird"), None,
])
def test_logo_png_is_silently_skipped_unless_head_is_200_png(answer):
    info = fetch(FakeSession(fixture_routes("620"), heads={"/620/logo.png": answer}), "620")
    assert info.assets_for("logo") == [] and info.name == "Portal 2"


def test_logo_png_content_type_may_carry_parameters_and_any_case():
    info = fetch(FakeSession(fixture_routes("620"), heads={"/620/logo.png": FakeHead(200, "Image/PNG; charset=binary")}), "620")
    assert len(info.assets_for("logo")) == 1


def test_logo_png_check_survives_a_session_without_head():
    class NoHead(FakeSession):
        head = None
    assert fetch(NoHead(fixture_routes("620")), "620").assets_for("logo") == []


# ---- DLC: borrow box art and logo from the base game ---------------------------------------------

def test_dlc_without_art_gets_labelled_base_game_box_art_and_logo():
    session = FakeSession(fixture_routes(), heads={"/9000001/logo.png": FakeHead()})
    info = fetch(session)
    own = [a for a in info.assets if "base game" not in a.label]
    borrowed = [a for a in info.assets if "base game" in a.label]
    assert info.assets[:len(own)] == own                  # appended after the DLC's own candidates
    assert [(a.slot, a.label) for a in borrowed] == [("boxart", "Library box art (base game: Tidepool Tactics)"),
                                                     ("logo", "Logo (base game: Tidepool Tactics)")]
    box, logo = borrowed
    assert "/steam/apps/9000001/" in box.url and "library_capsule_2x" in box.url and (box.width, box.height) == (600, 900)
    assert logo.url == PARENT_LOGO and logo.transparent is True
    assert all(a.source == "Steam" for a in borrowed)


def test_dlc_borrows_only_box_art_and_logo_never_pictures_or_trailers():
    info = fetch(FakeSession(fixture_routes(), heads={"/9000001/logo.png": FakeHead()}))
    assert not any("9000001" in a.url for a in info.assets if a.slot in ("image", "video"))
    assert len(info.assets_for("video")) == 4 and info.description.startswith("In the latest chapter")


def test_dlc_without_a_base_game_logo_still_gets_box_art():
    info = fetch(FakeSession(fixture_routes()))              # every HEAD answers 404
    assert [a.slot for a in info.assets if "base game" in a.label] == ["boxart"]


def test_dlc_that_already_has_art_does_not_fall_back():
    own = browse_with(9000002, library_capsule_2x="x/library_capsule_2x.jpg")
    session = FakeSession(fixture_routes(browse=own), heads={"/9000002/logo.png": FakeHead(), "/9000001/logo.png": FakeHead()})
    info = fetch(session)
    assert len(info.assets_for("boxart")) == 1 and len(info.assets_for("logo")) == 1
    assert not any("base game" in a.label for a in info.assets)
    assert len(session.calls) == 2 and len(session.head_calls) == 1       # no parent browse, no parent HEAD


def test_dlc_with_box_art_but_no_logo_borrows_only_the_logo():
    own = browse_with(9000002, library_capsule_2x="x/library_capsule_2x.jpg")
    info = fetch(FakeSession(fixture_routes(browse=own), heads={"/9000001/logo.png": FakeHead()}))
    assert [(a.slot, a.url) for a in info.assets if "base game" in a.label] == [("logo", PARENT_LOGO)]
    assert len(info.assets_for("boxart")) == 1


def test_dlc_with_logo_but_no_box_art_borrows_only_the_box_art():
    session = FakeSession(fixture_routes(), heads={"/9000002/logo.png": FakeHead(), "/9000001/logo.png": FakeHead()})
    info = fetch(session)
    assert [a.slot for a in info.assets if "base game" in a.label] == ["boxart"]
    assert len(info.assets_for("logo")) == 1
    assert len(session.head_calls) == 1                               # the parent's logo.png is not even checked


def test_dlc_never_makes_more_than_two_head_requests():
    session = FakeSession(fixture_routes(), heads={"/9000001/logo.png": FakeHead()})
    fetch(session)
    assert len(session.head_calls) == 2 and len(session.calls) == 3


@pytest.mark.parametrize("parent_failure", [
    requests.ConnectionError("down"), requests.Timeout("slow"), FakeResponse(ValueError("bad json")),
    {"response": "junk"}, ["x"], load("steam_browse_missing.json"),
])
def test_dlc_parent_request_failure_is_ignored(parent_failure):
    routes = fixture_routes()
    routes[browse_key(9000001)] = parent_failure
    session = FakeSession(routes, heads={"/9000001/logo.png": requests.Timeout("slow")})
    info = fetch(session)
    assert info.name == "Tidepool Tactics - Chapter 5" and len(info.assets_for("video")) == 4
    assert not any("base game" in a.label for a in info.assets)


@pytest.mark.parametrize("fullgame", [None, "x", [], {}, {"appid": None}, {"appid": "../x"}, {"appid": "1 2"}, {"appid": "55"},
                                      {"appid": True}, {"name": "No id"}])
def test_dlc_with_an_unusable_base_game_reference_makes_no_parent_requests(fullgame):
    payload = dlc_data(fullgame=fullgame)
    session = FakeSession({"appdetails": payload, browse_key(55): browse_with(55)})
    info = fetch(session, "55")
    assert info.name == "A DLC"
    assert len(session.calls) == 2 and len(session.head_calls) == 1


def test_dlc_base_game_name_falls_back_when_missing_and_is_cleaned():
    for fullgame, expected in (({"appid": 44}, "app 44"), ({"appid": "44", "name": "  Big\x00 Game \ud800 "}, "Big Game")):
        session = FakeSession({"appdetails": dlc_data(fullgame=fullgame), browse_key(55): browse_with(55),
                               browse_key(44): browse_with(44, library_capsule="c.jpg")})
        info = fetch(session, "55")
        assert [a.label for a in info.assets if a.slot == "boxart"] == [f"Library box art (base game: {expected})"]
        assert info.assets_for("boxart")[0].url.endswith("/steam/apps/44/c.jpg?t=1")
        assert (info.assets_for("boxart")[0].width, info.assets_for("boxart")[0].height) == (300, 450)


def test_only_dlc_type_falls_back():
    for kind in ("game", "music", "demo", "", None, ["dlc"]):
        session = FakeSession({"appdetails": dlc_data(type=kind), browse_key(55): browse_with(55)})
        info = fetch(session, "55")
        assert len(session.calls) == 2 and not any("base game" in a.label for a in info.assets)


def test_provider_search_ranks_by_title_similarity_not_steam_order():
    p = SteamProvider(session=FakeSession(fixture_routes("620", "steam_search_portal2.json")))
    got = p.search("Portal 2")
    assert got[0].source_id == "620" and got[0].title == "Portal 2"
    assert [m.score for m in got] == sorted((m.score for m in got), reverse=True)
    titles = [m.title for m in got]
    assert titles.index("Portal 2") < titles.index("Portal 2 Soundtrack")
    assert len(got) <= MAX_MATCHES


def test_provider_search_prefers_the_game_over_soundtrack_demo_and_dlc():
    items = [{"type": "app", "id": 1, "name": "Tidepool Tactics Chapter 5 Soundtrack"},
             {"type": "app", "id": 2, "name": "Tidepool Tactics Chapter 5 Demo"},
             {"type": "app", "id": 3, "name": "Tidepool Tactics Chapter 5 - Deluxe DLC"},
             {"type": "app", "id": 4, "name": "Tidepool Tactics - Chapter 5"}]
    p = SteamProvider(session=FakeSession({"storesearch": {"items": items}}))
    got = p.search("Tidepool Tactics Chapter 5")
    assert got[0].source_id == "4"
    assert sorted(m.source_id for m in got[1:]) == ["1", "2", "3"]


def test_provider_search_tolerates_junk_items_and_caps_results():
    items = [None, "x", 5, {}, {"name": "No id"}, {"id": 7}, {"id": "abc", "name": "Bad id"}, {"id": True, "name": "Bool"},
             {"type": "bundle", "id": 8, "name": "Some Bundle"}, {"type": "sub", "id": 9, "name": "Some Sub"},
             {"id": 10, "name": "Fine game"}, {"id": "11", "name": "Fine game 2", "type": "app"}]
    items += [{"id": 100 + i, "name": f"Game {i}"} for i in range(30)]
    got = SteamProvider(session=FakeSession({"storesearch": {"items": items}})).search("Fine game")
    assert [m.source_id for m in got[:2]] == ["10", "11"]
    assert len(got) == MAX_MATCHES
    assert all(m.source_id.isdigit() and m.title for m in got)


@pytest.mark.parametrize("payload", [{}, {"items": None}, {"items": []}, {"items": {}}, {"items": "x"},
                                     load("steam_search_nothing.json")])
def test_provider_search_empty_results(payload):
    assert SteamProvider(session=FakeSession({"storesearch": payload})).search("anything") == []


def test_provider_search_blank_title_makes_no_request():
    session = FakeSession({})
    p = SteamProvider(session=session)
    assert p.search("") == [] and p.search("   ") == [] and p.search(None) == []
    assert session.calls == []


@pytest.mark.parametrize("failure", [
    requests.ConnectionError("down"), requests.Timeout("slow"), OSError("boom"),
    FakeResponse({}, error=requests.HTTPError("429 Too Many Requests")),
    FakeResponse(ValueError("not json")),
    FakeResponse(None, error=RuntimeError("weird")),
])
def test_provider_failures_raise_provider_error(failure):
    p = SteamProvider(session=FakeSession({"storesearch": failure, "appdetails": failure}))
    with pytest.raises(ProviderError) as e:
        p.search("Portal 2")
    assert "Steam" in str(e.value)
    with pytest.raises(ProviderError):
        p.fetch(Match(title="x", source="Steam", source_id="620"))


def test_provider_non_object_json_is_a_provider_error():
    p = SteamProvider(session=FakeSession({"appdetails": "<html>maintenance</html>", "storesearch": ["x"]}))
    with pytest.raises(ProviderError):
        p.fetch(Match(title="x", source="Steam", source_id="620"))
    with pytest.raises(ProviderError):
        p.search("Portal 2")


@pytest.mark.parametrize("payload", [
    load("steam_details_missing.json"),
    {"620": {"success": True, "data": []}},          # Steam's quirk: [] instead of {}
    {"620": {"success": True}},
    {"620": {"success": False, "data": []}},
    {"620": None}, {"620": []}, {},
])
def test_provider_fetch_without_data_raises_provider_error(payload):
    session = FakeSession({"appdetails": payload})
    with pytest.raises(ProviderError) as e:
        SteamProvider(session=session).fetch(Match(title="x", source="Steam", source_id="620"))
    assert "620" in str(e.value)
    assert len(session.calls) == 1                    # no browse call for a game that has no data


@pytest.mark.parametrize("bad_id", ["", "abc", "12 34", "1&appids=2", "../x", "-5", None, "620\n"])
def test_provider_fetch_rejects_non_numeric_ids_without_a_request(bad_id):
    session = FakeSession({})
    with pytest.raises(ProviderError):
        SteamProvider(session=session).fetch(Match(title="x", source="Steam", source_id=bad_id))
    assert session.calls == []


def test_provider_fetch_accepts_integer_ids():
    p = SteamProvider(session=FakeSession(fixture_routes()))
    assert p.fetch(Match(title="x", source="Steam", source_id=9000002)).source_id == "9000002"


def test_provider_fetch_keeps_going_when_the_extra_art_request_fails():
    for failing in (requests.ConnectionError("down"), {"response": "junk"}, ["x"], FakeResponse(ValueError("bad json"))):
        routes = fixture_routes()
        routes[browse_key(9000002)] = failing
        info = fetch(FakeSession(routes))
        assert info.name == "Tidepool Tactics - Chapter 5" and len(info.assets_for("video")) == 4


@pytest.mark.parametrize("status", [429, 403])
def test_provider_rate_limit_or_refusal_has_its_own_plain_message(status):
    err = requests.HTTPError(str(status))
    err.response = type("R", (), {"status_code": status})()
    p = SteamProvider(session=FakeSession({"storesearch": FakeResponse({}, error=err)}))
    with pytest.raises(ProviderError, match="slow down"):
        p.search("Portal 2")


def test_provider_other_http_errors_are_not_called_rate_limits():
    err = requests.HTTPError("500")
    err.response = type("R", (), {"status_code": 500})()
    p = SteamProvider(session=FakeSession({"storesearch": FakeResponse({}, error=err)}))
    with pytest.raises(ProviderError) as e:
        p.search("Portal 2")
    assert "slow down" not in str(e.value)


# ---- names that end up in labels are capped ------------------------------------------------------

def test_trailer_names_are_capped_at_80_characters():
    long_name = "Trailer " + "word " * 60
    h = "https://video.akamai.steamstatic.com/a.m3u8"
    label = parse_details({"name": "X", "movies": [{"name": long_name, "hls_h264": h}]}).assets_for("video")[0].label
    assert len(label) <= 80 and label.endswith("...") and label.startswith("Trailer word")
    short = parse_details({"name": "X", "movies": [{"name": "Launch Trailer", "hls_h264": h}]}).assets_for("video")[0].label
    assert short == "Launch Trailer"


def test_base_game_name_in_borrowed_labels_is_capped_at_80_characters():
    data = dlc_data()
    data["55"]["data"]["fullgame"]["name"] = "The Very Long Base Game " + "Name " * 60
    session = FakeSession({"appdetails": data, browse_key(55): browse_with(55), browse_key(44): browse_with(44, library_capsule_2x="box.jpg")},
                          heads={"/44/logo.png": FakeHead()})
    info = fetch(session, "55")
    borrowed = [a for a in info.assets if "base game" in a.label]
    assert borrowed
    for a in borrowed:
        name = a.label.split("(base game: ", 1)[1].rstrip(")")
        assert 0 < len(name) <= 80 and name.endswith("...")
