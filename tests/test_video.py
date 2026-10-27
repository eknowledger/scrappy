import functools
import http.server
import json
import os
import shutil
import socket
import subprocess
import threading
import time

import pytest

from scrappy.processing import video
from scrappy.processing.video import (EncodeCancelled, EncodeTimeout, VideoError, build_encode_cmd, encode_clip,
                                  probe_duration)


# ---------------------------------------------------------------- command line (pure)

def test_build_cmd_basic():
    cmd = build_encode_cmd("in.mp4", "out.mp4", start=1, end=2.5)
    s = " ".join(cmd)
    assert "-ss 1.00" in s and "-t 1.50" in s and "scale=1280:720" in s and "libx264" in s
    assert cmd[-1] == "out.mp4"
    assert cmd.index("-ss") < cmd.index("-i")          # fast seek before the input


def test_build_cmd_caps_length_at_max_seconds():
    assert "-t 120.00" in " ".join(build_encode_cmd("in.mp4", "out.mp4", start=0, end=500, max_seconds=120))


def test_build_cmd_default_length_without_end():
    assert "-t 90.00" in " ".join(build_encode_cmd("in.mp4", "out.mp4", start=0, end=None, max_seconds=90))


def test_build_cmd_is_an_argument_list_of_strings():
    cmd = build_encode_cmd("a b; rm -rf ~.mp4", "o ut.mp4")
    assert isinstance(cmd, list) and all(isinstance(a, str) for a in cmd)
    assert "a b; rm -rf ~.mp4" in cmd and cmd[-1] == "o ut.mp4"        # one argv element each, never split


def test_build_cmd_output_format_stream_selection_and_faststart():
    cmd = build_encode_cmd("in.mp4", "out.mp4")
    s = " ".join(cmd)
    assert "-movflags +faststart" in s and "-f mp4" in s
    assert "-map" not in cmd                                   # default selection: best video, best audio
    assert "-sn" in cmd and "-dn" in cmd and "-nostdin" in cmd
    assert "fps=30" in s and "format=yuv420p" in s and "pad=1280:720" in s
    assert "-c:a aac" in s


def test_build_cmd_letterboxes_instead_of_stretching():
    vf = build_encode_cmd("in.mp4", "out.mp4")[build_encode_cmd("in.mp4", "out.mp4").index("-vf") + 1]
    assert "force_original_aspect_ratio=decrease" in vf and vf.index("scale=1280:720") < vf.index("pad=1280:720")


@pytest.mark.parametrize("src,dst,expected_src,expected_dst", [
    ("-evil.mp4", "out.mp4", "./-evil.mp4", "out.mp4"),
    ("in.mp4", "-out.mp4", "in.mp4", "./-out.mp4"),
    ("--version", "-y", "./--version", "./-y"),
])
def test_build_cmd_paths_starting_with_dash_are_not_options(src, dst, expected_src, expected_dst):
    cmd = build_encode_cmd(src, dst)
    assert cmd[cmd.index("-i") + 1] == expected_src
    assert cmd[-1] == expected_dst


def test_build_cmd_url_input_restricts_protocols():
    cmd = build_encode_cmd("https://cdn.example.com/a/b.m3u8?t=1&u=2", "out.mp4")
    i = cmd.index("-protocol_whitelist")
    assert cmd[i + 1] == video.URL_PROTOCOLS
    assert i < cmd.index("-i") and cmd[cmd.index("-i") + 1] == "https://cdn.example.com/a/b.m3u8?t=1&u=2"


def test_url_whitelist_contents():
    allowed = set(video.URL_PROTOCOLS.split(","))
    assert {"http", "https", "tcp", "tls", "crypto", "httpproxy", "data"} <= allowed    # httpproxy: used when http(s)_proxy is set
    assert not allowed & {"file", "concat", "concatf", "subfile", "rtp", "udp", "pipe", "ftp", "rtmp"}


def test_build_cmd_local_input_only_allows_file_protocol():
    cmd = build_encode_cmd("/tmp/in.mp4", "out.mp4")
    i = cmd.index("-protocol_whitelist")
    assert cmd[i + 1] == "file" and i < cmd.index("-i")


@pytest.mark.parametrize("src", ["file:///etc/passwd", "ftp://example.com/a.mp4", "rtmp://example.com/live",
                                 "gopher://example.com/", "http://", "https:///nohost.mp4", ""])
def test_build_cmd_rejects_unsafe_sources(src):
    with pytest.raises(ValueError):
        build_encode_cmd(src, "out.mp4")


def test_build_cmd_protocol_looking_local_names_stay_local_files():
    # "concat:a|b" is a protocol to ffmpeg; as a local-file argument the whitelist ("file" only) denies it.
    cmd = build_encode_cmd("concat:a.mp4|b.mp4", "out.mp4")
    assert cmd[cmd.index("-protocol_whitelist") + 1] == "file"


@pytest.mark.parametrize("start,end", [(float("nan"), None), (0, float("nan")), (float("inf"), None), (0, float("inf"))])
def test_build_cmd_rejects_non_finite_times(start, end):
    with pytest.raises(ValueError):
        build_encode_cmd("in.mp4", "out.mp4", start=start, end=end)


def test_build_cmd_negative_start_becomes_zero():
    cmd = build_encode_cmd("in.mp4", "out.mp4", start=-5, end=10)
    assert "-ss" not in cmd and cmd[cmd.index("-t") + 1] == "10.00"


def test_build_cmd_clamps_end_to_probed_duration():
    cmd = build_encode_cmd("in.mp4", "out.mp4", start=10, end=500, duration=60)
    assert cmd[cmd.index("-ss") + 1] == "10.00" and cmd[cmd.index("-t") + 1] == "50.00"


def test_build_cmd_clamps_start_past_the_end_of_the_video():
    cmd = build_encode_cmd("in.mp4", "out.mp4", start=200, end=300, duration=60)
    start, length = float(cmd[cmd.index("-ss") + 1]), float(cmd[cmd.index("-t") + 1])
    assert start == 59.0 and length == 1.0 and start + length <= 60


def test_build_cmd_clip_shorter_than_one_second():
    cmd = build_encode_cmd("in.mp4", "out.mp4", start=0, end=None, duration=0.5)
    assert cmd[cmd.index("-t") + 1] == "0.50" and "-ss" not in cmd


def test_build_cmd_unknown_duration_zero_is_ignored():
    cmd = build_encode_cmd("in.mp4", "out.mp4", start=0, end=30, duration=0)
    assert cmd[cmd.index("-t") + 1] == "30.00"


# ---------------------------------------------------------------- encode_clip with a fake process

class FakeProc:
    """Stands in for subprocess.Popen: writes the output file like ffmpeg would, then behaves as scripted."""

    def __init__(self, cmd, *, returncode=0, err="", write=b"MP4DATA", hang=False, **kwargs):
        self.cmd, self.kwargs = cmd, kwargs
        self.returncode = None
        self._final, self._stderr, self._hang = returncode, err, hang
        self.killed = self.reaped = False
        self.tmp = cmd[-1]
        if write is not None:
            with open(self.tmp, "wb") as f:
                f.write(write)

    def communicate(self, timeout=None):
        if self._hang and not self.killed:
            threading.Event().wait(timeout or 0)
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        self.reaped = True
        self.returncode = -9 if self.killed else self._final
        return "", self._stderr

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.reaped = True
        self.returncode = -9 if self.killed else self._final
        return self.returncode


def factory(created, **opts):
    def popen(cmd, **kwargs):
        p = FakeProc(cmd, **opts, **kwargs)
        created.append(p)
        return p
    return popen


def no_probe(_src):
    return 0.0


def test_encode_clip_success_is_atomic_and_leaves_no_tmp(tmp_path):
    dst = tmp_path / "out.mp4"
    procs = []
    assert encode_clip("in.mp4", str(dst), probe=no_probe, popen=factory(procs)) == str(dst)
    assert dst.read_bytes() == b"MP4DATA"
    assert os.listdir(tmp_path) == ["out.mp4"]
    p = procs[0]
    assert p.cmd[-1] != str(dst)                                    # ffmpeg wrote to a temporary file
    assert os.path.dirname(p.cmd[-1]) == str(tmp_path)              # in the same folder so replace is atomic
    assert p.kwargs.get("shell") in (None, False) and isinstance(p.cmd, list)
    assert p.kwargs["stdin"] == subprocess.DEVNULL


def test_encode_clip_failure_keeps_old_file_and_removes_tmp(tmp_path):
    dst = tmp_path / "out.mp4"
    dst.write_bytes(b"OLD")
    with pytest.raises(VideoError, match="boom"):
        encode_clip("in.mp4", str(dst), probe=no_probe, popen=factory([], returncode=1, err="x\nboom"))
    assert dst.read_bytes() == b"OLD" and os.listdir(tmp_path) == ["out.mp4"]


def test_video_error_is_a_runtime_error():
    assert issubclass(VideoError, RuntimeError) and issubclass(EncodeTimeout, VideoError)
    assert issubclass(EncodeCancelled, VideoError)


def test_encode_clip_empty_output_is_a_failure(tmp_path):
    dst = tmp_path / "out.mp4"
    with pytest.raises(VideoError):
        encode_clip("in.mp4", str(dst), probe=no_probe, popen=factory([], write=b""))
    assert os.listdir(tmp_path) == []


def test_encode_clip_timeout_kills_and_reaps_process(tmp_path):
    procs = []
    with pytest.raises(EncodeTimeout):
        encode_clip("in.mp4", str(tmp_path / "out.mp4"), probe=no_probe, timeout=0.05,
                    popen=factory(procs, hang=True))
    assert procs[0].killed and procs[0].reaped
    assert os.listdir(tmp_path) == []


def test_encode_clip_cancel_kills_and_reaps_process(tmp_path):
    procs = []
    calls = []

    def cancelled():
        calls.append(1)
        return len(calls) >= 2

    with pytest.raises(EncodeCancelled):
        encode_clip("in.mp4", str(tmp_path / "out.mp4"), probe=no_probe, cancelled=cancelled, timeout=30,
                    popen=factory(procs, hang=True))
    assert procs[0].killed and procs[0].reaped and os.listdir(tmp_path) == []


def test_encode_clip_cancelled_before_start_runs_nothing(tmp_path):
    procs = []
    with pytest.raises(EncodeCancelled):
        encode_clip("in.mp4", str(tmp_path / "out.mp4"), probe=no_probe, cancelled=lambda: True,
                    popen=factory(procs))
    assert procs == []


def test_encode_clip_kills_process_on_unexpected_exception(tmp_path):
    procs = []

    def cancelled():
        if procs:
            raise KeyboardInterrupt
        return False

    with pytest.raises(KeyboardInterrupt):
        encode_clip("in.mp4", str(tmp_path / "out.mp4"), probe=no_probe, cancelled=cancelled,
                    popen=factory(procs, hang=True))
    assert procs[0].killed and procs[0].reaped and os.listdir(tmp_path) == []


def test_encode_clip_missing_ffmpeg_is_a_video_error(tmp_path):
    with pytest.raises(VideoError, match="ffmpeg"):
        encode_clip("in.mp4", str(tmp_path / "out.mp4"), ffmpeg="/no/such/ffmpeg-binary", probe=no_probe)
    assert os.listdir(tmp_path) == []


def test_encode_clip_uses_probed_duration_to_clamp(tmp_path):
    procs = []
    encode_clip("in.mp4", str(tmp_path / "out.mp4"), start=0, end=500, probe=lambda s: 20.0, popen=factory(procs))
    assert procs[0].cmd[procs[0].cmd.index("-t") + 1] == "20.00"


def test_encode_clip_explicit_duration_skips_probe(tmp_path):
    def boom(_):
        raise AssertionError("must not probe")

    procs = []
    encode_clip("in.mp4", str(tmp_path / "out.mp4"), start=0, end=500, duration=15, probe=boom, popen=factory(procs))
    assert procs[0].cmd[procs[0].cmd.index("-t") + 1] == "15.00"


def test_encode_clip_refuses_to_overwrite_its_own_source(tmp_path):
    src = tmp_path / "in.mp4"
    src.write_bytes(b"SRC")
    with pytest.raises(ValueError):
        encode_clip(str(src), str(src), probe=no_probe, popen=factory([]))
    assert src.read_bytes() == b"SRC"


def test_encode_clip_writes_through_a_symlinked_destination(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link.mp4"
    link.symlink_to(real / "v.mp4")
    encode_clip("in.mp4", str(link), probe=no_probe, popen=factory([]))
    assert link.is_symlink() and (real / "v.mp4").read_bytes() == b"MP4DATA"


# ---------------------------------------------------------------- probe_duration with a fake runner

def fake_run(stdout="", returncode=0, raises=None):
    seen = []

    def run(cmd, **kwargs):
        seen.append((cmd, kwargs))
        if raises:
            raise raises
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

    run.seen = seen
    return run


def test_probe_duration_parses_json():
    run = fake_run(json.dumps({"format": {"duration": "74.25"}}))
    assert probe_duration("in.mp4", runner=run) == 74.25
    cmd, kwargs = run.seen[0]
    assert cmd[0] == "ffprobe" and kwargs["timeout"] > 0 and kwargs.get("shell") in (None, False)


def test_probe_duration_protects_path_and_protocols():
    run = fake_run("{}")
    probe_duration("-x.mp4", runner=run)
    probe_duration("https://example.com/a.m3u8", runner=run)
    local, remote = run.seen[0][0], run.seen[1][0]
    assert local[local.index("-i") + 1] == "./-x.mp4" and local[local.index("-protocol_whitelist") + 1] == "file"
    assert remote[remote.index("-protocol_whitelist") + 1] == video.URL_PROTOCOLS
    assert remote[remote.index("-rw_timeout") + 1] == video.NETWORK_IO_TIMEOUT_US
    assert "-rw_timeout" not in local


@pytest.mark.parametrize("stdout", ["", "not json", "{}", '{"format": {}}', '{"format": {"duration": "N/A"}}',
                                    '{"format": {"duration": "nan"}}', '{"format": {"duration": "-3"}}',
                                    '{"format": {"duration": "inf"}}', '{"format": null}', "[]"])
def test_probe_duration_garbage_is_zero(stdout):
    assert probe_duration("in.mp4", runner=fake_run(stdout)) == 0.0


def test_probe_duration_failure_and_timeout_are_zero():
    assert probe_duration("in.mp4", runner=fake_run("", returncode=1)) == 0.0
    assert probe_duration("in.mp4", runner=fake_run(raises=subprocess.TimeoutExpired("ffprobe", 1))) == 0.0


def test_probe_duration_rejects_unsafe_source():
    with pytest.raises(ValueError):
        probe_duration("file:///etc/passwd", runner=fake_run("{}"))


# ---------------------------------------------------------------- real ffmpeg


def _has_x264():
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        return False
    return b"libx264" in subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True).stdout


needs_ffmpeg = pytest.mark.skipif(not _has_x264(), reason="ffmpeg with libx264 not available")


def make_source(path, lavfi="testsrc=duration=4:size=640x360:rate=25", audio=True, extra=(), seconds=4):
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", lavfi]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=duration={seconds}"]
    cmd += [*extra, "-t", str(seconds), "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True)
    return path


def probe_streams(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,codec_name,width,height,pix_fmt,r_frame_rate,sample_aspect_ratio",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)["streams"]


def gray_pixels(path, positions):
    """Luma (0-255) of the first frame at the given (x, y) positions."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-f", "rawvideo",
                          "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
    assert len(raw) == 1280 * 720
    return [raw[y * 1280 + x] for x, y in positions]


@needs_ffmpeg
def test_encode_clip_end_to_end(tmp_path):
    src = tmp_path / "in.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=4:size=640x360:rate=25",
                    "-f", "lavfi", "-i", "sine=duration=4", "-shortest", "-pix_fmt", "yuv420p", str(src)], check=True)
    dst = tmp_path / "out.mp4"
    encode_clip(str(src), str(dst), start=1, end=3)
    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height",
         "-of", "json", str(dst)], capture_output=True, text=True).stdout)["streams"][0]
    assert (info["codec_name"], info["width"], info["height"]) == ("h264", 1280, 720)
    assert 1.5 < probe_duration(str(dst)) < 2.6
    assert sorted(os.listdir(tmp_path)) == ["in.mp4", "out.mp4"]


@needs_ffmpeg
def test_encode_clip_output_format_details(tmp_path):
    src = make_source(tmp_path / "in.mp4")
    dst = tmp_path / "out.mp4"
    encode_clip(str(src), str(dst))
    streams = {s["codec_type"]: s for s in probe_streams(dst)}
    assert streams["video"]["pix_fmt"] == "yuv420p" and streams["video"]["r_frame_rate"] == "30/1"
    assert streams["audio"]["codec_name"] == "aac"
    data = dst.read_bytes()
    assert 0 <= data.find(b"moov") < data.find(b"mdat")             # faststart: index before the media data


@needs_ffmpeg
def test_encode_clip_without_audio_has_no_audio_stream(tmp_path):
    src = make_source(tmp_path / "in.mp4", audio=False)
    dst = tmp_path / "out.mp4"
    encode_clip(str(src), str(dst))
    assert [s["codec_type"] for s in probe_streams(dst)] == ["video"]


@needs_ffmpeg
def test_encode_clip_pillarboxes_4_3_instead_of_stretching(tmp_path):
    src = make_source(tmp_path / "in.mp4", lavfi="color=c=white:size=640x480:rate=25:duration=4", audio=False)
    dst = tmp_path / "out.mp4"
    encode_clip(str(src), str(dst))
    left_bar, centre, right_bar = gray_pixels(dst, [(100, 360), (640, 360), (1180, 360)])
    assert left_bar < 40 and right_bar < 40 and centre > 200        # 960x720 picture centred between black bars


@needs_ffmpeg
def test_encode_clip_letterboxes_wide_source(tmp_path):
    src = make_source(tmp_path / "in.mp4", lavfi="color=c=white:size=1280x400:rate=25:duration=4", audio=False)
    dst = tmp_path / "out.mp4"
    encode_clip(str(src), str(dst))
    top, centre, bottom = gray_pixels(dst, [(640, 20), (640, 360), (640, 700)])
    assert top < 40 and bottom < 40 and centre > 200


@needs_ffmpeg
def test_encode_clip_honours_non_square_pixels(tmp_path):
    # 480x480 stored with 4:3 pixels is displayed 640x480, so it must become 960x720 (bars < x=160), not 720x720.
    src = make_source(tmp_path / "in.mp4", lavfi="color=c=white:size=480x480:rate=25:duration=4,setsar=4/3",
                      audio=False)
    dst = tmp_path / "out.mp4"
    encode_clip(str(src), str(dst))
    bar, picture = gray_pixels(dst, [(120, 360), (200, 360)])
    assert bar < 40 and picture > 200
    assert probe_streams(dst)[0]["sample_aspect_ratio"] == "1:1"


@needs_ffmpeg
def test_encode_clip_clamps_to_the_real_duration(tmp_path):
    src = make_source(tmp_path / "in.mp4")
    dst = tmp_path / "out.mp4"
    encode_clip(str(src), str(dst), start=3, end=500)               # 1 s of material left
    assert 0.5 < probe_duration(str(dst)) < 1.6
    dst2 = tmp_path / "out2.mp4"
    encode_clip(str(src), str(dst2), start=100, end=200)            # start past the end -> last second, still a video
    assert 0.5 < probe_duration(str(dst2)) < 1.6


@needs_ffmpeg
def test_encode_clip_bad_input_leaves_nothing_behind(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"this is not a video")
    dst = tmp_path / "out.mp4"
    with pytest.raises(VideoError, match="ffmpeg failed"):
        encode_clip(str(bad), str(dst))
    assert sorted(os.listdir(tmp_path)) == ["bad.mp4"]


@needs_ffmpeg
def test_encode_clip_handles_dash_prefixed_names(tmp_path, monkeypatch):
    make_source(tmp_path / "-in.mp4", audio=False)
    monkeypatch.chdir(tmp_path)
    encode_clip("-in.mp4", "-out.mp4")
    assert (tmp_path / "-out.mp4").stat().st_size > 0
    assert probe_duration("-out.mp4") > 3


@needs_ffmpeg
def test_encode_clip_real_cancel_kills_ffmpeg(tmp_path):
    # A 60 s 720p source cannot be encoded in the ~0.3 s before the cancel fires.
    src = make_source(tmp_path / "in.mp4", lavfi="testsrc2=duration=60:size=1280x720:rate=30", audio=False,
                      extra=("-preset", "ultrafast"), seconds=60)
    dst = tmp_path / "out.mp4"
    ticks = []

    def cancelled():
        ticks.append(1)
        return len(ticks) > 3

    started = time.monotonic()
    with pytest.raises(EncodeCancelled):
        encode_clip(str(src), str(dst), max_seconds=60, cancelled=cancelled, duration=60)
    assert time.monotonic() - started < 20
    assert sorted(os.listdir(tmp_path)) == ["in.mp4"]


@needs_ffmpeg
def test_probe_duration_real_and_missing_file(tmp_path):
    src = make_source(tmp_path / "in.mp4")
    assert 3.5 < probe_duration(str(src)) < 4.5
    assert probe_duration(str(tmp_path / "nope.mp4")) == 0.0


@pytest.fixture
def http_server(tmp_path):
    root = tmp_path / "www"
    root.mkdir()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield root, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join(5)


@needs_ffmpeg
def test_encode_clip_from_http_url(tmp_path, http_server):
    root, base = http_server
    make_source(root / "clip.mp4", audio=False)
    dst = tmp_path / "out.mp4"
    encode_clip(f"{base}/clip.mp4", str(dst), start=1, end=3)
    assert 1.5 < probe_duration(str(dst)) < 2.6


@needs_ffmpeg
def test_proxy_env_does_not_trip_the_protocol_whitelist(tmp_path, monkeypatch):
    # With http_proxy set (the only variable ffmpeg reads) it tunnels https URLs through its internal
    # "httpproxy" protocol, which must be whitelisted. A closed proxy port would fail before that protocol is opened, so a tiny proxy answers 502 to the CONNECT.
    received = []
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    listener.settimeout(0.2)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                continue
            with conn:
                conn.settimeout(5)
                received.append(conn.recv(4096))
                conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "no_proxy", "NO_PROXY", "all_proxy",
                "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{listener.getsockname()[1]}")
    try:
        with pytest.raises(VideoError) as err:
            encode_clip("https://example.invalid/clip.mp4", str(tmp_path / "out.mp4"), duration=10)
    finally:
        stop.set()
        thread.join(5)
        listener.close()
    assert received and received[0].startswith(b"CONNECT example.invalid")      # it really went through the proxy
    assert "whitelist" not in str(err.value).lower() and "5XX" in str(err.value)
    assert os.listdir(tmp_path) == []


def make_hls(root, name, lavfi, seconds=4):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", lavfi, "-t", str(seconds),
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", "-f", "hls", "-hls_time", "2",
                    "-hls_playlist_type", "vod", "-hls_segment_filename", str(root / f"{name}_%d.ts"),
                    str(root / f"{name}.m3u8")], check=True)


@needs_ffmpeg
def test_encode_clip_uses_the_highest_resolution_hls_variant(tmp_path, http_server):
    root, base = http_server
    make_hls(root, "low", "color=c=white:size=320x240:rate=25")             # 4:3 -> would be pillarboxed
    make_hls(root, "high", "color=c=white:size=1280x720:rate=25")           # 16:9 -> fills the frame
    (root / "master.m3u8").write_text(
        "#EXTM3U\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=320x240\nlow.m3u8\n'     # listed FIRST on purpose
        '#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1280x720\nhigh.m3u8\n')
    dst = tmp_path / "out.mp4"
    encode_clip(f"{base}/master.m3u8", str(dst))
    bar_position, centre = gray_pixels(dst, [(100, 360), (640, 360)])
    assert bar_position > 200 and centre > 200                              # no black bar: encoded from the 720p variant


@needs_ffmpeg
def test_encode_clip_keeps_a_single_audio_stream_from_multi_audio_files(tmp_path):
    src = tmp_path / "in.mkv"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x180:rate=25",
                    "-f", "lavfi", "-i", "sine=duration=2", "-f", "lavfi", "-i", "sine=frequency=880:duration=2",
                    "-map", "0", "-map", "1", "-map", "2", "-pix_fmt", "yuv420p", str(src)], check=True)
    dst = tmp_path / "out.mp4"
    encode_clip(str(src), str(dst))
    assert [s["codec_type"] for s in probe_streams(dst)].count("audio") == 1


@needs_ffmpeg
def test_hostile_playlist_cannot_read_local_files(tmp_path, http_server):
    root, base = http_server
    secret = tmp_path / "secret.mp4"
    make_source(secret, audio=False)                                # a perfectly valid local video
    (root / "evil.m3u8").write_text(
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\n"
        f"file://{secret}\n#EXT-X-ENDLIST\n")
    dst = tmp_path / "out.mp4"
    with pytest.raises(VideoError):
        encode_clip(f"{base}/evil.m3u8", str(dst))
    assert not dst.exists() and sorted(os.listdir(tmp_path)) == ["secret.mp4", "www"]


def test_module_exposes_no_shell_usage():
    src = open(video.__file__).read()
    assert "shell=True" not in src and "os.system" not in src
