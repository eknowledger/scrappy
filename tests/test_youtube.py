import io
import json
import os
import pathlib
import subprocess
import sys
import time
import zipfile

import pytest

from scrappy.models import AssetCandidate
from scrappy.providers.base import ProviderError, describe_error
from scrappy.providers import youtube
from scrappy.providers.youtube import (
    ProcessResult, YouTubeCancelled, YouTubeError, YouTubeTimeout, YtDlp, parse_search_output, rank_candidates,
    run_process, video_id_from_url, youtube_query,
)

FIX = pathlib.Path(__file__).parent / "fixtures"


def entry(vid="abc12345678", title="Game Official Trailer", duration=90, channel="Studio", **extra):
    d = {"_type": "url", "id": vid, "title": title, "duration": duration, "channel": channel}
    d.update(extra)
    return d


def lines(*items):
    return "\n".join(i if isinstance(i, str) else json.dumps(i) for i in items) + "\n"


# ---- query and ids -------------------------------------------------------------------------------

def test_youtube_query_uses_platform_hint():
    assert youtube_query("Tidepool Tactics Chapter 5", "switch") == "Tidepool Tactics Chapter 5 Nintendo Switch official trailer"
    assert youtube_query("Quasar Quokka", "") == "Quasar Quokka official trailer"
    assert youtube_query("Quasar Quokka", "unknownsys") == "Quasar Quokka official trailer"


@pytest.mark.parametrize("url, expected", [
    ("https://www.youtube.com/watch?v=abc_DEF-123", "abc_DEF-123"),
    ("https://youtube.com/watch?v=abc_DEF-123&list=PL1&t=5s", "abc_DEF-123"),
    ("https://m.youtube.com/watch?feature=share&v=abc_DEF-123", "abc_DEF-123"),
    ("https://youtu.be/abc_DEF-123?t=4", "abc_DEF-123"),
    ("abc_DEF-123", "abc_DEF-123"),
    ("-poSVOan2fw", "-poSVOan2fw"),
    ("https://www.youtube.com/watch?v=short", ""),
    ("https://www.youtube.com/watch?v=abc_DEF-1234", ""),             # 12 characters
    ("https://www.youtube.com/watch?v=abc DEF-123", ""),
    ("http://www.youtube.com/watch?v=abc_DEF-123", ""),               # not https
    ("https://www.youtube.com.evil.com/watch?v=abc_DEF-123", ""),
    ("https://evil.com/watch?v=abc_DEF-123", ""),
    ("https://user@www.youtube.com/watch?v=abc_DEF-123", ""),
    ("https://www.youtube.com/playlist?list=abc_DEF-123", ""),
    ("file:///etc/passwd", ""),
    ("--exec=touch", ""),
    ("", ""),
    (None, ""),
    (12345678901, ""),
])
def test_video_id_from_url(url, expected):
    assert video_id_from_url(url) == expected


# ---- parsing search output -----------------------------------------------------------------------

def test_parse_real_search_output():
    got = parse_search_output((FIX / "youtube_search_supertuxkart.jsonl").read_text(encoding="utf-8"))
    assert [c.url for c in got] == ["https://www.youtube.com/watch?v=" + i for i in
                                    ("Lm1TFDBiIIg", "H1Lo7PlTGBk", "0FEwDH7XU9Q", "ef9vYcuEDL4", "WutAN4i98_o", "tKD3aWJDg3M")]
    first = got[0]
    assert (first.slot, first.kind, first.source, first.duration) == ("video", "youtube", "YouTube", 89.0)
    assert first.label == "SuperTuxKart 1.0 Official Trailer [1:29] SuperTuxKart"
    assert first.thumb_url.startswith("https://i.ytimg.com/vi/Lm1TFDBiIIg/")


def test_parse_skips_bad_lines_and_bad_ids():
    out = lines(entry("goodidgood1"), "not json", "[1, 2]", '"text"', "null", entry("short"), entry("has space!!"),
                {"title": "no id"}, entry(["x"]), entry(12345678901), "{broken")
    assert [c.url for c in parse_search_output(out)] == ["https://www.youtube.com/watch?v=goodidgood1"]


def test_parse_ignores_url_field_and_uses_only_the_id():
    out = lines(entry("goodidgood1", url="file:///etc/passwd", webpage_url="http://evil.example/x"))
    assert parse_search_output(out)[0].url == "https://www.youtube.com/watch?v=goodidgood1"


def test_parse_dedupes_ids():
    assert len(parse_search_output(lines(entry("goodidgood1"), entry("goodidgood1", title="again")))) == 1


def test_parse_thumbnails_only_https_ytimg():
    good = {"url": "https://i.ytimg.com/vi/goodidgood1/hq720.jpg?sqp=abc&rs=def", "width": 720, "height": 404}
    small = {"url": "https://i.ytimg.com/vi/goodidgood1/default.jpg", "width": 120, "height": 90}
    bad = [{"url": "http://i.ytimg.com/vi/x/1.jpg", "width": 9999, "height": 9999},
           {"url": "https://i.ytimg.com.evil.com/x.jpg", "width": 9999, "height": 9999},
           {"url": "https://evil.com/i.ytimg.com/x.jpg", "width": 9999, "height": 9999},
           {"url": "https://user@i.ytimg.com/x.jpg", "width": 9999, "height": 9999},
           {"url": "https://i.ytimg.com:8443/x.jpg", "width": 9999, "height": 9999},
           {"url": "javascript:alert(1)"}, {"url": 5}, "junk", None]
    got = parse_search_output(lines(entry("goodidgood1", thumbnails=[small, *bad, good])))[0]
    assert got.thumb_url == good["url"]


def test_parse_thumbnail_falls_back_to_standard_url():
    for thumbs in (None, [], "x", [{"url": "http://evil/x.jpg"}]):
        got = parse_search_output(lines(entry("goodidgood1", thumbnails=thumbs)))[0]
        assert got.thumb_url == "https://i.ytimg.com/vi/goodidgood1/hqdefault.jpg"


def test_parse_sanitises_text_and_duration():
    got = parse_search_output(lines(entry("goodidgood1", title="Bad\x00 \x1b[31mTitle‮\n" + "x" * 500, duration="NaN",
                                          channel=["list"], uploader="Fallback\tName")))[0]
    assert "\x00" not in got.label and "\x1b" not in got.label and "\n" not in got.label
    assert len(got.label) < 250 and got.duration == 0.0
    assert got.label.endswith("Fallback Name")
    for dur in (True, float("inf"), -5, None, "12"):
        assert parse_search_output(lines(entry("goodidgood1", duration=dur)))[0].duration == 0.0


def test_parse_drops_live_and_members_only():
    out = lines(entry("liveidlive1", live_status="is_live", duration=None), entry("upcomingid1", live_status="is_upcoming"),
                entry("membersonly", availability="subscriber_only"), entry("okidokidoki", live_status="not_live"))
    assert [c.url[-11:] for c in parse_search_output(out)] == ["okidokidoki"]


def test_label_formats_duration():
    assert "[0:09]" in parse_search_output(lines(entry("goodidgood1", duration=9)))[0].label
    assert "[1:01:05]" in parse_search_output(lines(entry("goodidgood1", duration=3665)))[0].label
    assert "[?]" in parse_search_output(lines(entry("goodidgood1", duration=None)))[0].label


def test_parse_handles_huge_output_without_crashing():
    out = lines(*[entry(f"id{n:09d}") for n in range(2000)])
    assert len(parse_search_output(out)) <= youtube.MAX_RESULTS_PARSED


# ---- ranking -------------------------------------------------------------------------------------

def cand(vid, title, duration=90, channel="X", verified=False):
    raw = entry(vid.ljust(11, "0"), title=title, duration=duration, channel=channel, channel_is_verified=verified)
    return parse_search_output(lines(raw))[0]


def ids(cands):
    return [c.url[-11:].rstrip("0") for c in cands]


def test_rank_drops_shorts_and_very_long_videos():
    got = rank_candidates([cand("short", "Game trailer", 8), cand("ok", "Game trailer", 10), cand("long", "Game trailer", 1201),
                           cand("edge", "Game trailer", 1200), cand("movie", "Game trailer", 3 * 3600)])
    assert sorted(ids(got)) == ["edge", "ok"]


def test_rank_keeps_unknown_duration_last_among_equals():
    got = rank_candidates([cand("unknown", "Game Official Trailer", None), cand("known", "Game Official Trailer", 90)])
    assert ids(got) == ["known", "unknown"]


def test_rank_official_trailers_above_reactions_reviews_and_no_commentary():
    got = rank_candidates([
        cand("react", "REACTING to the Game trailer!", channel="Fan", verified=False),
        cand("review", "Game Review - is it worth it?"),
        cand("nocom", "Game gameplay no commentary part 1"),
        cand("walk", "Game Walkthrough Let's Play"),
        cand("official", "Game | Official Launch Trailer", channel="Studio", verified=True),
    ])
    assert ids(got)[0] == "official"
    assert set(ids(got)[1:]) == {"react", "review", "nocom", "walk"}


def test_rank_verified_channel_breaks_tie_and_original_order_is_stable():
    got = rank_candidates([cand("fan", "Game trailer"), cand("verif", "Game trailer", verified=True), cand("fan2", "Game trailer")])
    assert ids(got) == ["verif", "fan", "fan2"]


def test_rank_prefers_titles_that_name_the_game_when_given():
    got = rank_candidates([cand("wrong", "Other Game Official Trailer", verified=True),
                           cand("right", "Tidepool Tactics Chapter 5 Official Trailer")], title="Tidepool Tactics Chapter 5")
    assert ids(got) == ["right", "wrong"]


def test_rank_real_search_output_keeps_official_first():
    got = rank_candidates(parse_search_output((FIX / "youtube_search_supertuxkart.jsonl").read_text(encoding="utf-8")),
                          title="SuperTuxKart")
    assert "Official Trailer" in got[0].label
    assert [c.label for c in got[:3]] == [c.label for c in got if "official trailer" in c.label.lower()][:3]


# ---- fake runner ---------------------------------------------------------------------------------

class FakeRunner:
    def __init__(self, stdout="", returncode=0, stderr="", side_effect=None):
        self.result = ProcessResult(returncode, stdout, stderr)
        self.side_effect = side_effect
        self.calls = []

    def __call__(self, cmd, timeout, cancelled=None, cwd=None, max_stdout=youtube.MAX_STDOUT):
        self.calls.append({"cmd": list(cmd), "timeout": timeout, "cancelled": cancelled, "cwd": cwd})
        if self.side_effect:
            self.side_effect(cmd, cwd)
        return self.result

    @property
    def cmd(self):
        return self.calls[-1]["cmd"]


def installed(tmp_path, runner, **kw):
    yt = YtDlp(tmp_path / "bin", python="/usr/bin/python-test", runner=runner, **kw)
    yt.path.parent.mkdir(parents=True, exist_ok=True)
    yt.path.write_bytes(b"#!/usr/bin/env python3\n")
    return yt


# ---- search --------------------------------------------------------------------------------------

def test_search_builds_safe_argument_list(tmp_path):
    runner = FakeRunner(stdout=lines(entry("goodidgood1")))
    yt = installed(tmp_path, runner)
    yt.search("--exec touch /tmp/pwned  ; rm -rf ~", count=3)
    cmd = runner.cmd
    assert isinstance(cmd, list) and cmd[:3] == ["/usr/bin/python-test", "-I", str(yt.path)]
    assert "--ignore-config" in cmd and "--flat-playlist" in cmd and "--dump-json" in cmd
    assert cmd[-2] == "--"                                              # after this nothing can be an option
    assert cmd[-1] == "ytsearch6:--exec touch /tmp/pwned ; rm -rf ~"    # one argument, prefix first
    assert runner.calls[0]["timeout"] == youtube.SEARCH_TIMEOUT


def test_search_cleans_query_and_ignores_empty(tmp_path):
    runner = FakeRunner(stdout=lines(entry("goodidgood1")))
    yt = installed(tmp_path, runner)
    assert yt.search("   \x00\n ") == [] and runner.calls == []
    yt.search("a\x00b\n\tc " + "z" * 1000)
    assert "\x00" not in runner.cmd[-1] and "\n" not in runner.cmd[-1] and len(runner.cmd[-1]) < 300


def test_search_returns_ranked_filtered_top_count(tmp_path):
    out = lines(entry("reactreact1", title="Game REACTION", duration=60),
                entry("shortshort1", title="Game trailer", duration=5),
                entry("officialid1", title="Game Official Trailer", duration=100, channel_is_verified=True),
                entry("longlonglo1", title="Game Official Trailer 3 hours", duration=10800),
                entry("otherother1", title="Game launch trailer", duration=70))
    got = installed(tmp_path, FakeRunner(stdout=out)).search("Game", count=2, title="Game")
    assert [c.url[-11:] for c in got] == ["officialid1", "otherother1"]


def test_search_clamps_count(tmp_path):
    runner = FakeRunner(stdout="")
    yt = installed(tmp_path, runner)
    assert yt.search("x", count=0) == [] and runner.cmd[-1].startswith("ytsearch2:")
    yt.search("x", count=500)
    assert runner.cmd[-1].startswith(f"ytsearch{youtube.MAX_SEARCH_FETCH}:")


def test_search_uses_partial_results_when_exit_code_is_nonzero(tmp_path):
    runner = FakeRunner(stdout=lines(entry("goodidgood1")), returncode=1, stderr="ERROR: one entry broke")
    assert len(installed(tmp_path, runner).search("x")) == 1


def test_search_failure_is_a_plain_error_without_raw_stderr(tmp_path):
    stderr = "Traceback (most recent call last):\n  File \"/home/user/secret/x.py\"\nERROR: boom \x1b[31mred\x1b[0m"
    with pytest.raises(YouTubeError) as err:
        installed(tmp_path, FakeRunner(returncode=1, stderr=stderr)).search("x")
    message = describe_error(err.value)
    assert "boom red" in message and "Traceback" not in message and "\x1b" not in message and "secret" not in message
    assert isinstance(err.value, ProviderError)


@pytest.mark.parametrize("stderr, needle", [
    ("ImportError: You are using an unsupported version of Python. Only Python versions 3.10 and above", "newer Python"),
    ("ERROR: [youtube] x: Unable to extract initial data; please report this issue", "Update"),
    ("ERROR: unable to download video data: HTTP Error 403: Forbidden", "Update"),
    ("ERROR: Requested format is not available. Use --list-formats", "Update"),
    ("ERROR: [youtube] abc: Sign in to confirm you're not a bot", "Update"),
    ("ERROR: [youtube] abc: Video unavailable. This video is private", "not available"),
    ("ERROR: Unable to download webpage: <urlopen error [Errno -3] Temporary failure in name resolution>", "internet"),
    ("ERROR: HTTP Error 429: Too Many Requests", "try again"),
])
def test_known_failures_get_plain_guidance(tmp_path, stderr, needle):
    with pytest.raises(YouTubeError) as err:
        installed(tmp_path, FakeRunner(returncode=1, stderr=stderr)).search("x")
    assert needle in str(err.value)
    assert len(str(err.value)) < 300


def test_unknown_failure_shows_short_sanitised_tail(tmp_path):
    stderr = "noise\n" * 100 + "ERROR: " + "weird " * 200
    with pytest.raises(YouTubeError) as err:
        installed(tmp_path, FakeRunner(returncode=2, stderr=stderr)).search("x")
    assert len(str(err.value)) < 300 and "weird" in str(err.value)
    with pytest.raises(YouTubeError) as err:
        installed(tmp_path, FakeRunner(returncode=2, stderr="")).search("x")
    assert "exit code 2" in str(err.value)


def test_search_without_installed_downloader_says_how_to_fix(tmp_path):
    runner = FakeRunner()
    yt = YtDlp(tmp_path / "bin", runner=runner)
    with pytest.raises(YouTubeError) as err:
        yt.search("x")
    assert "not installed" in str(err.value) and "Install / update YouTube downloader" in str(err.value)
    with pytest.raises(YouTubeError):
        yt.download("https://www.youtube.com/watch?v=abc_DEF-123", tmp_path)
    assert runner.calls == []


def test_missing_python_is_a_plain_error(tmp_path):
    def boom(*a, **k):
        raise FileNotFoundError(2, "No such file", "/nowhere/python")
    with pytest.raises(YouTubeError) as err:
        installed(tmp_path, boom).search("x")
    assert "Python" in str(err.value)


def test_timeout_and_cancel_errors_from_runner_propagate(tmp_path):
    def slow(*a, **k):
        raise YouTubeTimeout("took too long")
    with pytest.raises(YouTubeTimeout):
        installed(tmp_path, slow).search("x")
    assert issubclass(YouTubeTimeout, YouTubeError) and issubclass(YouTubeCancelled, YouTubeError)


def test_cancel_hook_is_passed_to_the_runner(tmp_path):
    runner = FakeRunner(stdout="")
    hook = lambda: False
    installed(tmp_path, runner).search("x", cancelled=hook)
    assert runner.calls[0]["cancelled"] is hook


# ---- download ------------------------------------------------------------------------------------

URL = "https://www.youtube.com/watch?v=abc_DEF-123"


def writes(name="video.mp4", data=b"x" * 10, extra=()):
    def effect(cmd, cwd):
        pathlib.Path(cwd, name).write_bytes(data)
        for other in extra:
            pathlib.Path(cwd, other).write_bytes(b"junk")
    return effect


def test_download_builds_safe_command_and_returns_final_file(tmp_path):
    runner = FakeRunner(side_effect=writes())
    yt = installed(tmp_path, runner)
    dest = tmp_path / "tmp 100%"            # a '%' must not be read as an output template
    got = yt.download("https://youtu.be/abc_DEF-123?list=PLxyz", dest)
    assert got == dest / "abc_DEF-123.mp4" and got.read_bytes() == b"x" * 10
    cmd = runner.cmd
    assert cmd[:3] == ["/usr/bin/python-test", "-I", str(yt.path)]
    assert cmd[-2:] == ["--", URL]                                     # rebuilt from the validated id
    for flag in ("--ignore-config", "--no-playlist", "--quiet", "--no-warnings", "--no-progress", "--no-cache-dir",
                 "--no-mtime"):
        assert flag in cmd
    assert not any("cookies" in c for c in cmd) and "--exec" not in cmd
    fmt = cmd[cmd.index("-f") + 1]
    assert "height<=720" in fmt and "height<=1080" not in fmt and "ext=mp4" in fmt and "ext=webm" in fmt
    assert cmd[cmd.index("-o") + 1] == "video.%(ext)s"                 # relative: the work dir is the cwd
    assert "--max-filesize" in cmd and runner.calls[0]["timeout"] == youtube.DOWNLOAD_TIMEOUT
    assert sorted(p.name for p in dest.iterdir()) == ["abc_DEF-123.mp4"]    # work dir removed


def test_download_accepts_a_bare_id_and_webm(tmp_path):
    yt = installed(tmp_path, FakeRunner(side_effect=writes("video.webm")))
    assert yt.download("abc_DEF-123", tmp_path / "d").name == "abc_DEF-123.webm"


def test_download_height_is_limited(tmp_path):
    runner = FakeRunner(side_effect=writes())
    installed(tmp_path, runner).download(URL, tmp_path / "d", max_height=480)
    assert "height<=480" in runner.cmd[runner.cmd.index("-f") + 1]
    for bad in (1080, 0, "720", None):
        with pytest.raises(ValueError):
            installed(tmp_path, runner).download(URL, tmp_path / "d", max_height=bad)


@pytest.mark.parametrize("url", ["https://evil.example/watch?v=abc_DEF-123", "file:///etc/passwd", "--exec=x", "", "http://youtu.be/abc_DEF-123",
                                 "https://www.youtube.com/playlist?list=PLxyz"])
def test_download_rejects_non_youtube_video_urls(tmp_path, url):
    runner = FakeRunner(side_effect=writes())
    with pytest.raises(YouTubeError) as err:
        installed(tmp_path, runner).download(url, tmp_path / "d")
    assert "YouTube" in str(err.value) and runner.calls == []


def test_download_ignores_partial_and_unexpected_files(tmp_path):
    runner = FakeRunner(side_effect=writes("video.mp4", extra=("video.mp4.part", "video.f137.mp4.part", "video.info.json", "video.jpg")))
    got = installed(tmp_path, runner).download(URL, tmp_path / "d")
    assert got.name == "abc_DEF-123.mp4" and [p.name for p in (tmp_path / "d").iterdir()] == ["abc_DEF-123.mp4"]


def test_download_with_only_unfinished_parts_fails_and_cleans_up(tmp_path):
    runner = FakeRunner(side_effect=writes("video.mp4.part"))
    with pytest.raises(YouTubeError) as err:
        installed(tmp_path, runner).download(URL, tmp_path / "d")
    assert "no video file" in str(err.value) and list((tmp_path / "d").iterdir()) == []


def test_download_empty_file_is_an_error(tmp_path):
    with pytest.raises(YouTubeError):
        installed(tmp_path, FakeRunner(side_effect=writes(data=b""))).download(URL, tmp_path / "d")


def test_download_failure_cleans_up_and_explains(tmp_path):
    runner = FakeRunner(returncode=1, stderr="ERROR: [youtube] abc: Video unavailable", side_effect=writes("video.mp4.part"))
    with pytest.raises(YouTubeError) as err:
        installed(tmp_path, runner).download(URL, tmp_path / "d")
    assert "not available" in str(err.value) and list((tmp_path / "d").iterdir()) == []


def test_download_cancel_cleans_up(tmp_path):
    def cancel(cmd, cwd):
        pathlib.Path(cwd, "video.mp4.part").write_bytes(b"x")
        raise YouTubeCancelled("download cancelled")
    with pytest.raises(YouTubeCancelled):
        installed(tmp_path, FakeRunner(side_effect=cancel)).download(URL, tmp_path / "d")
    assert list((tmp_path / "d").iterdir()) == []


def test_download_replaces_an_existing_file_atomically(tmp_path):
    dest = tmp_path / "d"
    dest.mkdir()
    (dest / "abc_DEF-123.mp4").write_bytes(b"old")
    installed(tmp_path, FakeRunner(side_effect=writes(data=b"new!"))).download(URL, dest)
    assert (dest / "abc_DEF-123.mp4").read_bytes() == b"new!"


def test_download_ignores_symlinked_output(tmp_path):
    secret = tmp_path / "secret.mp4"
    secret.write_bytes(b"secret")

    def link(cmd, cwd):
        os.symlink(secret, pathlib.Path(cwd, "video.mp4"))
    with pytest.raises(YouTubeError):
        installed(tmp_path, FakeRunner(side_effect=link)).download(URL, tmp_path / "d")
    assert secret.read_bytes() == b"secret"


# ---- real process runner (a stand-in script, no yt-dlp needed) -----------------------------------

def py(code):
    return [sys.executable, "-c", code]


def test_run_process_collects_output():
    r = run_process(py("import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"), timeout=20)
    assert (r.returncode, r.stdout.strip(), r.stderr.strip()) == (3, "out", "err")


def test_run_process_times_out_and_kills():
    start = time.monotonic()
    with pytest.raises(YouTubeTimeout):
        run_process(py("import time; time.sleep(60)"), timeout=0.5)
    assert time.monotonic() - start < 15


def test_run_process_cancel_kills():
    start = time.monotonic()
    state = {"n": 0}

    def cancelled():
        state["n"] += 1
        return state["n"] > 3
    with pytest.raises(YouTubeCancelled):
        run_process(py("import time; time.sleep(60)"), timeout=60, cancelled=cancelled)
    assert time.monotonic() - start < 15


def test_run_process_already_cancelled_never_starts(tmp_path):
    marker = tmp_path / "ran"
    with pytest.raises(YouTubeCancelled):
        run_process(py(f"open({str(marker)!r}, 'w')"), timeout=20, cancelled=lambda: True)
    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="process groups")
def test_run_process_kills_child_processes_too(tmp_path):
    pidfile = tmp_path / "pid"
    code = ("import subprocess, sys, time\n"
            f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
            "time.sleep(60)\n")
    with pytest.raises(YouTubeTimeout):
        run_process(py(code), timeout=1.5)
    pid = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, 9)
        pytest.fail("grandchild still running")


def test_run_process_caps_stdout():
    with pytest.raises(YouTubeError) as err:
        run_process(py("import sys\nwhile True: sys.stdout.write('x' * 65536)"), timeout=30, max_stdout=200_000)
    assert "too much" in str(err.value)


def test_run_process_keeps_only_a_stderr_tail():
    r = run_process(py("import sys; sys.stderr.write('a' * 3_000_000 + 'END')"), timeout=30)
    assert r.stderr.endswith("END") and len(r.stderr) <= youtube.STDERR_KEEP


def test_run_process_reports_missing_program():
    with pytest.raises(FileNotFoundError):
        run_process(["/nonexistent/program"], timeout=5)


def test_run_process_runs_in_cwd(tmp_path):
    r = run_process(py("import os; print(os.getcwd())"), timeout=20, cwd=tmp_path)
    assert os.path.realpath(r.stdout.strip()) == os.path.realpath(tmp_path)


# ---- installing yt-dlp ---------------------------------------------------------------------------

def make_zipapp(main=b"print('hi')\n", prefix=b"#!/usr/bin/env python3\n"):
    buf = io.BytesIO()
    buf.write(prefix)
    with zipfile.ZipFile(buf, "a") as z:
        z.writestr("__main__.py", main)
    return buf.getvalue()


class FakeResp:
    def __init__(self, data, url="https://github.com/yt-dlp/yt-dlp/releases/download/x/yt-dlp"):
        self.stream, self.url = io.BytesIO(data), url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self.stream.read(n)

    def geturl(self):
        return self.url


def opener_for(data, calls, **kw):
    def opener(req, timeout=None):
        calls.append((req.full_url, timeout))
        return FakeResp(data, **kw)
    return opener


def test_ensure_downloads_once_and_is_executable(tmp_path):
    calls = []
    yt = YtDlp(tmp_path / "bin")
    assert not yt.available()
    assert yt.ensure(opener=opener_for(make_zipapp(), calls)) == yt.path
    assert yt.available() and os.access(yt.path, os.X_OK) and yt.path.name == "yt-dlp"
    assert calls == [(youtube.YTDLP_URL, 60)] and youtube.YTDLP_URL.startswith("https://github.com/yt-dlp/yt-dlp/")
    yt.ensure(opener=opener_for(make_zipapp(), calls))
    assert len(calls) == 1
    assert [p.name for p in yt.path.parent.iterdir()] == ["yt-dlp"]            # no temp leftovers
    assert zipfile.is_zipfile(yt.path)


def test_update_replaces_atomically_and_keeps_old_on_failure(tmp_path):
    yt = YtDlp(tmp_path / "bin")
    yt.ensure(opener=opener_for(make_zipapp(b"print('v1')\n"), []))
    old = yt.path.read_bytes()
    yt.update(opener=opener_for(make_zipapp(b"print('v2')\n"), []))
    assert yt.path.read_bytes() != old and b"v2" not in old
    keep = yt.path.read_bytes()
    with pytest.raises(YouTubeError):
        yt.update(opener=opener_for(b"<html>rate limited</html>", []))
    assert yt.path.read_bytes() == keep and [p.name for p in yt.path.parent.iterdir()] == ["yt-dlp"]


@pytest.mark.parametrize("data", [b"", b"<html>not found</html>", b"#!/usr/bin/env python3\nprint('no zip')\n",
                                  make_zipapp(main=b"x")[:-30]])
def test_install_rejects_things_that_are_not_a_zipapp(tmp_path, data):
    yt = YtDlp(tmp_path / "bin")
    with pytest.raises(YouTubeError) as err:
        yt.ensure(opener=opener_for(data, []))
    assert "downloader" in str(err.value) and not yt.available()
    assert list((tmp_path / "bin").glob("*")) == []


def test_install_requires_shebang_and_main(tmp_path):
    yt = YtDlp(tmp_path / "bin")
    with pytest.raises(YouTubeError):
        yt.ensure(opener=opener_for(make_zipapp(prefix=b""), []))
    buf = io.BytesIO(b"#!/usr/bin/env python3\n")
    with zipfile.ZipFile(buf, "a") as z:
        z.writestr("other.py", b"x")
    with pytest.raises(YouTubeError):
        yt.ensure(opener=opener_for(buf.getvalue(), []))
    assert not yt.available()


def test_install_rejects_oversized_download(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube, "MAX_BINARY_BYTES", 1000)
    with pytest.raises(YouTubeError) as err:
        YtDlp(tmp_path / "bin").ensure(opener=opener_for(make_zipapp(main=b"#" * 5000), []))
    assert "too large" in str(err.value)
    assert list((tmp_path / "bin").glob("*")) == []


@pytest.mark.parametrize("final", ["http://github.com/x", "https://evil.example/yt-dlp", "https://github.com.evil.com/x",
                                   "file:///etc/passwd"])
def test_install_rejects_redirects_off_github(tmp_path, final):
    yt = YtDlp(tmp_path / "bin")
    with pytest.raises(YouTubeError):
        yt.ensure(opener=opener_for(make_zipapp(), [], url=final))
    assert not yt.available()


def test_install_accepts_github_release_cdn(tmp_path):
    yt = YtDlp(tmp_path / "bin")
    yt.ensure(opener=opener_for(make_zipapp(), [], url="https://release-assets.githubusercontent.com/github-production-release-asset/1?x=y"))
    assert yt.available()


@pytest.mark.parametrize("exc", [OSError("down"), TimeoutError(), __import__("urllib.error").error.URLError("dns"),
                                 __import__("urllib.error").error.HTTPError("u", 404, "nf", {}, None)])
def test_install_network_errors_are_plain(tmp_path, exc):
    def opener(req, timeout=None):
        raise exc
    yt = YtDlp(tmp_path / "bin")
    with pytest.raises(YouTubeError) as err:
        yt.ensure(opener=opener)
    assert "internet" in str(err.value) or "downloader" in str(err.value)
    assert not yt.available()


def test_install_failure_mid_stream_leaves_no_temp_file(tmp_path):
    class Broken(FakeResp):
        def read(self, n=-1):
            raise OSError("connection reset")
    yt = YtDlp(tmp_path / "bin")
    with pytest.raises(YouTubeError):
        yt.ensure(opener=lambda req, timeout=None: Broken(b""))
    assert list((tmp_path / "bin").glob("*")) == []


# ---- review follow-ups: ranking vs the game's own name, no plugins, safer install -------------------

def test_rank_does_not_penalise_words_that_are_part_of_the_game_title():
    got = rank_candidates([cand("other", "Some Other Game Official Trailer", verified=True),
                           cand("mvc", "Marvel vs. Capcom Official Trailer")], title="Marvel vs. Capcom")
    assert ids(got) == ["mvc", "other"]
    got = rank_candidates([cand("other", "Some Other Game Official Trailer", verified=True),
                           cand("snk", "Capcom vs. SNK 2 Official Trailer")], title="Capcom vs. SNK 2")
    assert ids(got)[0] == "snk"
    # the same words are still penalised when they are not part of the game's name
    got = rank_candidates([cand("other", "Some Other Game Official Trailer", verified=True),
                           cand("vs", "Game vs Other Game Official Trailer")], title="Game")
    assert ids(got)[0] == "other"
    got = rank_candidates([cand("other", "Some Other Game Official Trailer", verified=True),
                           cand("rev", "Game Review Official Trailer")], title="Game")
    assert ids(got)[0] == "other"


def test_rank_penalises_shorts():
    got = rank_candidates([cand("tagged", "Game trailer #shorts"), cand("word", "Game Shorts trailer"),
                           cand("plain", "Game trailer")])
    assert ids(got)[0] == "plain"
    got = rank_candidates([cand("short", "A Short Hike trailer"), cand("tag", "A Short Hike trailer #Shorts")],
                          title="A Short Hike")
    assert ids(got) == ["short", "tag"]


def test_every_yt_dlp_call_disables_config_and_plugins(tmp_path):
    search_runner, download_runner = FakeRunner(stdout=""), FakeRunner(side_effect=writes())
    installed(tmp_path, search_runner).search("x")
    installed(tmp_path, download_runner).download(URL, tmp_path / "d")
    for runner in (search_runner, download_runner):
        assert "--ignore-config" in runner.cmd and "--no-plugin-dirs" in runner.cmd


def test_download_move_failure_is_a_plain_error(tmp_path, monkeypatch):
    def refuse(src, dst):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(youtube.os, "replace", refuse)
    with pytest.raises(YouTubeError) as err:
        installed(tmp_path, FakeRunner(side_effect=writes())).download(URL, tmp_path / "d")
    assert "Permission denied" in str(err.value) and list((tmp_path / "d").iterdir()) == []


class SmokeRunner:
    def __init__(self, returncode=0, stdout="2026.08.19\n", stderr="", exc=None):
        self.result, self.exc, self.calls = ProcessResult(returncode, stdout, stderr), exc, []

    def __call__(self, cmd, timeout, cancelled=None, cwd=None, max_stdout=youtube.MAX_STDOUT):
        self.calls.append((list(cmd), timeout))
        if self.exc:
            raise self.exc
        return self.result


def test_install_smoke_runs_the_new_file_before_replacing(tmp_path):
    runner = SmokeRunner()
    yt = YtDlp(tmp_path / "bin", python="/usr/bin/python-test", runner=runner)
    yt.ensure(opener=opener_for(make_zipapp(), []))
    (cmd, timeout), = runner.calls
    assert cmd[:2] == ["/usr/bin/python-test", "-I"] and cmd[-1] == "--version" and cmd[2] != str(yt.path)
    assert pathlib.Path(cmd[2]).parent == yt.path.parent and timeout <= 60       # the temp copy, not the installed one
    assert yt.available()


@pytest.mark.parametrize("runner", [
    SmokeRunner(returncode=1, stdout="", stderr="ImportError: You are using an unsupported version of Python."),
    SmokeRunner(returncode=0, stdout="   \n"),
    SmokeRunner(exc=YouTubeTimeout("slow")),
    SmokeRunner(exc=FileNotFoundError(2, "No such file")),
])
def test_failed_smoke_run_keeps_the_working_copy(tmp_path, runner):
    yt = YtDlp(tmp_path / "bin", python="/usr/bin/python-test", runner=SmokeRunner())
    yt.ensure(opener=opener_for(make_zipapp(b"print('v1')\n"), []))
    old = yt.path.read_bytes()
    yt.runner = runner
    with pytest.raises(YouTubeError) as err:
        yt.update(opener=opener_for(make_zipapp(b"print('v2')\n"), []))
    assert "old one was kept" in str(err.value) or "Python" in str(err.value)
    assert yt.path.read_bytes() == old and [p.name for p in yt.path.parent.iterdir()] == ["yt-dlp"]


def test_failed_smoke_run_explains_the_python_problem(tmp_path):
    yt = YtDlp(tmp_path / "bin", runner=SmokeRunner(returncode=1, stderr="ImportError: unsupported version of Python"))
    with pytest.raises(YouTubeError) as err:
        yt.ensure(opener=opener_for(make_zipapp(), []))
    assert "newer Python" in str(err.value) and not yt.available()


def test_install_fails_closed_when_the_final_address_is_unknown(tmp_path):
    class NoUrl(FakeResp):
        geturl = None
    yt = YtDlp(tmp_path / "bin", runner=SmokeRunner())
    with pytest.raises(YouTubeError) as err:
        yt.ensure(opener=lambda req, timeout=None: NoUrl(make_zipapp()))
    assert "address" in str(err.value) and not yt.available()

    class EmptyUrl(FakeResp):
        def geturl(self):
            return ""
    with pytest.raises(YouTubeError):
        yt.ensure(opener=lambda req, timeout=None: EmptyUrl(make_zipapp()))
    assert not yt.available()


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs permission checks")
def test_install_into_a_read_only_folder_says_it_cannot_write(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    bin_dir.chmod(0o500)
    try:
        with pytest.raises(YouTubeError) as err:
            YtDlp(bin_dir, runner=SmokeRunner()).ensure(opener=opener_for(make_zipapp(), []))
    finally:
        bin_dir.chmod(0o700)
    assert "could not write" in str(err.value).lower() and "internet" not in str(err.value)


def test_install_write_error_is_not_reported_as_a_network_problem(tmp_path, monkeypatch):
    def full_disk(fd, *a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(youtube.os, "fsync", full_disk)
    with pytest.raises(YouTubeError) as err:
        YtDlp(tmp_path / "bin", runner=SmokeRunner()).ensure(opener=opener_for(make_zipapp(), []))
    assert "No space left" in str(err.value) and "internet" not in str(err.value)


def test_install_replace_error_is_a_write_error(tmp_path, monkeypatch):
    def refuse(src, dst):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(youtube.os, "replace", refuse)
    with pytest.raises(YouTubeError) as err:
        YtDlp(tmp_path / "bin", runner=SmokeRunner()).ensure(opener=opener_for(make_zipapp(), []))
    assert "could not write" in str(err.value).lower() and list((tmp_path / "bin").glob("*")) == []


def test_install_network_read_error_still_says_internet(tmp_path):
    class Broken(FakeResp):
        def read(self, n=-1):
            raise ConnectionResetError("reset")
    with pytest.raises(YouTubeError) as err:
        YtDlp(tmp_path / "bin", runner=SmokeRunner()).ensure(opener=lambda req, timeout=None: Broken(b""))
    assert "internet" in str(err.value)


def test_real_smoke_run_accepts_a_working_zipapp_and_rejects_a_broken_one(tmp_path):
    ok = make_zipapp(b"import sys\nprint('2026.01.01' if sys.argv[1:] == ['--version'] else 'x')\n")
    yt = YtDlp(tmp_path / "bin")                      # real runner, real Python
    yt.ensure(opener=opener_for(ok, []))
    assert yt.available()
    with pytest.raises(YouTubeError):
        yt.update(opener=opener_for(make_zipapp(b"raise SystemExit('broken build')\n"), []))
    assert b"2026.01.01" in yt.path.read_bytes()


# ---- progress, cancel and the install result ----------------------------------------------------

class SizedResp(FakeResp):
    """A download that announces its size (like GitHub does) unless `announce` is None."""

    def __init__(self, data, announce="size", **kw):
        super().__init__(data, **kw)
        self.headers = {} if announce is None else {"Content-Length": str(len(data) if announce == "size" else announce)}


def big_zipapp(n=200_000):
    return make_zipapp(main=b"#" * n)


def test_install_reports_progress_against_the_announced_size(tmp_path):
    data, seen = big_zipapp(), []
    yt = YtDlp(tmp_path / "bin", runner=SmokeRunner())
    yt.install(opener=lambda req, timeout=None: SizedResp(data), progress=lambda done, total: seen.append((done, total)))
    assert seen[0] == (0, len(data)) and seen[-1] == (len(data), len(data))
    assert len(seen) >= 4 and [d for d, _ in seen] == sorted(d for d, _ in seen) and {t for _, t in seen} == {len(data)}
    assert yt.available() and [p.name for p in yt.path.parent.iterdir()] == ["yt-dlp"]


def test_install_reports_progress_without_a_total_when_the_server_does_not_say(tmp_path):
    data, seen = big_zipapp(), []
    yt = YtDlp(tmp_path / "bin", runner=SmokeRunner())
    yt.install(opener=lambda req, timeout=None: SizedResp(data, announce=None), progress=lambda done, total: seen.append((done, total)))
    assert seen[0] == (0, None) and seen[-1] == (len(data), None) and {t for _, t in seen} == {None}


@pytest.mark.parametrize("header", ["abc", "-5", "0", ""])
def test_a_nonsense_size_header_means_unknown_total(tmp_path, header):
    seen = []
    resp = SizedResp(big_zipapp(), announce=None)
    resp.headers = {"Content-Length": header}
    YtDlp(tmp_path / "bin", runner=SmokeRunner()).install(opener=lambda req, timeout=None: resp,
                                                         progress=lambda done, total: seen.append(total))
    assert set(seen) == {None}


def test_an_announced_size_over_the_cap_is_refused_before_any_byte_is_read(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube, "MAX_BINARY_BYTES", 1000)
    reads = []

    class Spy(SizedResp):
        def read(self, n=-1):
            reads.append(n)
            return super().read(n)

    with pytest.raises(YouTubeError) as err:
        YtDlp(tmp_path / "bin").install(opener=lambda req, timeout=None: Spy(big_zipapp()))
    assert "too large" in str(err.value) and reads == [] and list((tmp_path / "bin").glob("*")) == []


def test_a_download_that_ends_before_the_announced_size_is_an_error(tmp_path):
    data = big_zipapp()
    yt = YtDlp(tmp_path / "bin", runner=SmokeRunner())
    with pytest.raises(YouTubeError) as err:
        yt.install(opener=lambda req, timeout=None: SizedResp(data[:-1000], announce=len(data)))
    assert "internet" in str(err.value) and list((tmp_path / "bin").glob("*")) == []


def test_cancelling_mid_download_raises_and_leaves_no_partial_file(tmp_path):
    yt = YtDlp(tmp_path / "bin", runner=SmokeRunner())
    flag = {"stop": False}

    def progress(done, total):
        flag["stop"] = done >= 65536                                  # cancel after the first chunk

    with pytest.raises(YouTubeCancelled):
        yt.install(opener=lambda req, timeout=None: SizedResp(big_zipapp()), progress=progress, cancelled=lambda: flag["stop"])
    assert not yt.available() and list((tmp_path / "bin").glob("*")) == []


def test_cancelling_an_update_keeps_the_old_copy(tmp_path):
    yt = YtDlp(tmp_path / "bin", runner=SmokeRunner())
    yt.install(opener=opener_for(make_zipapp(b"print('v1')\n"), []))
    old = yt.path.read_bytes()
    with pytest.raises(YouTubeCancelled):
        yt.install(opener=lambda req, timeout=None: SizedResp(big_zipapp()), cancelled=lambda: True)
    assert yt.path.read_bytes() == old and [p.name for p in yt.path.parent.iterdir()] == ["yt-dlp"]


def test_a_cancel_during_the_smoke_run_is_a_cancel_not_a_failure(tmp_path):
    yt = YtDlp(tmp_path / "bin", runner=SmokeRunner(exc=YouTubeCancelled("Cancelled")))
    with pytest.raises(YouTubeCancelled):
        yt.install(opener=opener_for(make_zipapp(), []))
    assert list((tmp_path / "bin").glob("*")) == []


def test_install_announces_each_quiet_step_in_order(tmp_path):
    steps = []
    YtDlp(tmp_path / "bin", runner=SmokeRunner()).install(opener=opener_for(make_zipapp(), []), stage=steps.append)
    assert steps == ["Connecting to GitHub…", "Checking the downloaded file…", "Testing that the new downloader starts…", "Installing…"]
    steps.clear()
    yt = YtDlp(tmp_path / "bin", runner=SmokeRunner())
    yt.install(opener=opener_for(make_zipapp(), []), stage=steps.append)
    assert "Comparing with the installed version…" in steps and steps[-1] == "Installing…"


def test_install_result_says_install_update_and_the_versions(tmp_path):
    class Versions(SmokeRunner):
        def __init__(self, *versions):
            super().__init__()
            self.versions = list(versions)

        def __call__(self, cmd, timeout, cancelled=None, cwd=None, max_stdout=youtube.MAX_STDOUT):
            self.result = ProcessResult(0, self.versions.pop(0) + "\n", "")
            return super().__call__(cmd, timeout, cancelled, cwd, max_stdout)

    yt = YtDlp(tmp_path / "bin", runner=Versions("2026.07.31"))
    first = yt.install(opener=opener_for(make_zipapp(), []))
    assert (first.path, first.version, first.previous_version, first.was_installed, first.unchanged) == (yt.path, "2026.07.31", "", False, False)
    yt.runner = Versions("2026.08.19", "2026.07.31")                   # the new copy is smoke-run first, then the old one asked
    second = yt.install(opener=opener_for(make_zipapp(b"print(2)\n"), []))
    assert (second.version, second.previous_version, second.was_installed, second.unchanged) == ("2026.08.19", "2026.07.31", True, False)
    yt.runner = Versions("2026.08.19", "2026.08.19")
    assert yt.install(opener=opener_for(make_zipapp(b"print(3)\n"), [])).unchanged


def test_a_broken_old_copy_does_not_stop_an_update(tmp_path):
    yt = YtDlp(tmp_path / "bin", runner=SmokeRunner())
    yt.install(opener=opener_for(make_zipapp(b"print(1)\n"), []))
    calls = []

    def runner(cmd, timeout, cancelled=None, cwd=None, max_stdout=youtube.MAX_STDOUT):
        calls.append(cmd[2])
        if cmd[2] == str(yt.path):                                      # the installed copy cannot tell its version
            raise YouTubeError("broken")
        return ProcessResult(0, "2026.08.19\n", "")

    yt.runner = runner
    result = yt.install(opener=opener_for(make_zipapp(b"print(2)\n"), []))
    assert result.was_installed and result.previous_version == "" and result.version == "2026.08.19" and str(yt.path) in calls


def test_update_and_ensure_still_return_the_path(tmp_path):
    yt = YtDlp(tmp_path / "bin", runner=SmokeRunner())
    assert yt.ensure(opener=opener_for(make_zipapp(), [])) == yt.path
    assert yt.update(opener=opener_for(make_zipapp(b"print(2)\n"), [])) == yt.path
