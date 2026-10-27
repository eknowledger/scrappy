import functools
import gzip
import http.server
import shutil
import socket
import ssl
import subprocess
import threading
import time

import pytest

from scrappy.download import (BlockedDestination, DownloadCancelled, DownloadError, DownloadTimeout, DownloadTooLarge,
                          UnsupportedURL, cache_path, check_destination, check_redirect, download_bytes,
                          download_to_file, fetch_cached, is_public_address)
from scrappy.providers.base import ProviderError, describe_error

LOOPBACK_ONLY = lambda ip: ip == "127.0.0.1"          # lets the tests reach their own server but nothing else


class Site:
    """A tiny local web server; `routes` maps a path to a function(handler) that writes the answer."""

    def __init__(self):
        self.routes, self.requests = {}, []
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                site.requests.append((self.path, dict(self.headers)))
                route = site.routes.get(self.path.split("?")[0])
                if route is None:
                    return self.reply(404, b"no")
                route(self)

            def reply(self, code, body=b"", headers=()):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                for k, v in headers:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

        class Quiet(http.server.ThreadingHTTPServer):
            def handle_error(self, request, client_address):           # clients hanging up is part of the tests
                pass

        self.httpd = Quiet(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def redirect(self, path, to, code=302, headers=()):
        self.routes[path] = lambda h: h.reply(code, b"", [("Location", to), *headers])


@pytest.fixture
def site():
    s = Site()
    s.routes["/small.bin"] = lambda h: h.reply(200, b"a" * 1000)
    s.routes["/big.bin"] = lambda h: h.reply(200, b"b" * 5000)
    yield s
    s.httpd.shutdown()


def get(url, **kw):
    kw.setdefault("address_policy", LOOPBACK_ONLY)
    return download_bytes(url, **kw)


# ---------------------------------------------------------------- the happy path

def test_download_bytes(site):
    assert get(site.base + "/small.bin") == b"a" * 1000


def test_download_too_large(site):
    with pytest.raises(DownloadTooLarge):
        get(site.base + "/big.bin", max_bytes=2000)


def test_size_limit_is_enforced_mid_stream_when_the_server_does_not_say_how_big_it_is(site):
    sent = []

    def endless(h):                                  # chunked, no Content-Length, never ends
        h.send_response(200)
        h.send_header("Transfer-Encoding", "chunked")
        h.end_headers()
        try:
            for _ in range(10_000):
                h.wfile.write(b"1000\r\n" + b"x" * 4096 + b"\r\n")
                sent.append(1)
        except OSError:
            pass
    site.routes["/endless"] = endless
    with pytest.raises(DownloadTooLarge):
        get(site.base + "/endless", max_bytes=100_000)
    assert len(sent) < 1000                           # we hung up long before the server ran out


def test_a_huge_content_length_is_refused_before_reading(site):
    def lie(h):
        h.send_response(200)
        h.send_header("Content-Length", "999999999")
        h.end_headers()
    site.routes["/lie"] = lie
    with pytest.raises(DownloadTooLarge) as err:
        get(site.base + "/lie", max_bytes=1000)
    assert "too big" in str(err.value).lower()


def test_default_limit_suits_pictures():
    from scrappy.download import MAX_IMAGE_BYTES
    assert MAX_IMAGE_BYTES == 25_000_000


def test_content_type_is_only_a_hint(site):
    site.routes["/pic"] = lambda h: h.reply(200, b"\x89PNG....", [("Content-Type", "text/html")])
    from scrappy.download import download
    got = download(site.base + "/pic", address_policy=LOOPBACK_ONLY)
    assert got.data == b"\x89PNG...." and got.content_type == "text/html" and got.url.endswith("/pic")


def test_http_errors_are_explained_plainly(site):
    site.routes["/gone"] = lambda h: h.reply(404, b"")
    site.routes["/busy"] = lambda h: h.reply(429, b"")
    site.routes["/broken"] = lambda h: h.reply(503, b"")
    for path, word in (("/gone", "not found"), ("/busy", "slow down"), ("/broken", "problem")):
        with pytest.raises(DownloadError) as err:
            get(site.base + path)
        assert word in str(err.value).lower()
        assert describe_error(err.value) == str(err.value)           # shown to the user as it is
    assert isinstance(err.value, ProviderError)


def test_compressed_answers_are_refused(site):
    site.routes["/gz"] = lambda h: h.reply(200, gzip.compress(b"a" * 100), [("Content-Encoding", "gzip")])
    with pytest.raises(DownloadError):
        get(site.base + "/gz")


def test_short_body_is_an_error(site):
    def short(h):
        h.send_response(200)
        h.send_header("Content-Length", "100")
        h.end_headers()
        h.wfile.write(b"only a bit")
        h.close_connection = True
    site.routes["/short"] = short
    with pytest.raises(DownloadError):
        get(site.base + "/short")


# ---------------------------------------------------------------- what we send

def test_request_is_anonymous_and_identifies_itself(site):
    site.routes["/c1"] = lambda h: h.reply(302, b"", [("Location", "/c2"), ("Set-Cookie", "session=secret")])
    site.routes["/c2"] = lambda h: h.reply(200, b"ok")
    assert get(site.base + "/c1") == b"ok"
    (_, first), (_, second) = site.requests
    assert "Scrappy" in first["User-Agent"]
    assert "Cookie" not in second                        # cookies are never kept
    assert "Authorization" not in first and "Authorization" not in second
    assert first["Accept-Encoding"] == "identity"


def test_credentials_in_the_url_are_refused():
    with pytest.raises(UnsupportedURL):
        download_bytes("http://user:pass@example.com/a.png")


# ---------------------------------------------------------------- which URLs are allowed

@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/a.png", "data:image/png;base64,AAAA",
                                 "gopher://example.com/", "javascript:alert(1)", "//example.com/a.png",
                                 "/etc/passwd", "", "http:///nohost", "https://", None, 42])
def test_only_http_and_https_urls_are_accepted(url):
    with pytest.raises(UnsupportedURL):
        download_bytes(url, address_policy=lambda ip: True)


def test_redirect_to_a_local_file_or_other_protocol_is_refused(site):
    for target in ("file:///etc/passwd", "ftp://example.com/x", "data:text/plain,hi", "gopher://example.com/"):
        site.redirect("/r", target)
        with pytest.raises(UnsupportedURL):
            get(site.base + "/r")


def test_redirects_are_followed_including_relative_ones(site):
    site.redirect("/a", "/b", 301)
    site.redirect("/b", site.base + "/small.bin", 307)
    from scrappy.download import download
    got = download(site.base + "/a", address_policy=LOOPBACK_ONLY)
    assert got.data == b"a" * 1000 and got.url == site.base + "/small.bin"


def test_too_many_redirects(site):
    site.redirect("/loop", "/loop")
    with pytest.raises(DownloadError) as err:
        get(site.base + "/loop", max_redirects=3)
    assert "redirect" in str(err.value).lower()
    assert len(site.requests) == 4


def test_redirect_without_a_target_is_an_error(site):
    site.routes["/nowhere"] = lambda h: h.reply(302, b"")
    with pytest.raises(DownloadError):
        get(site.base + "/nowhere")


def test_redirect_to_a_private_address_is_refused(site):
    names = {"internal.test": ["10.0.0.5"], "router.test": ["192.168.1.1"], "meta.test": ["169.254.169.254"]}
    for host in names:
        site.redirect("/r", f"http://{host}:{site.port}/admin")
        with pytest.raises(BlockedDestination):
            get(site.base + "/r", resolver=lambda h, p: names.get(h, ["127.0.0.1"]))
    assert [p for p, _ in site.requests] == ["/r", "/r", "/r"]          # nothing was ever requested from the targets


def test_private_destinations_are_blocked_by_default(site):
    with pytest.raises(BlockedDestination) as err:
        download_bytes(site.base + "/small.bin")
    assert site.requests == []
    assert "own network" in str(err.value)


def test_allow_private_is_an_explicit_opt_in(site):
    assert download_bytes(site.base + "/small.bin", allow_private=True) == b"a" * 1000


def test_name_is_resolved_once_and_the_checked_address_is_the_one_used(site):
    calls = []

    def resolver(host, port):
        calls.append(host)
        return ["127.0.0.1"]
    assert get(f"http://pinned.test:{site.port}/small.bin", resolver=resolver) == b"a" * 1000
    assert calls == ["pinned.test"]


@pytest.mark.parametrize("ip", ["10.1.2.3", "172.16.0.1", "192.168.0.10", "127.0.0.1", "169.254.169.254", "0.0.0.0",
                                "100.64.0.1", "224.0.0.1", "255.255.255.255", "::1", "::", "fe80::1", "fc00::1",
                                "fd12:3456::1", "::ffff:10.0.0.1", "::ffff:127.0.0.1", "64:ff9b::a00:1",
                                "2002:a00:1::1", "ff02::1", "not an address", "",
                                "::a00:1", "::7f00:1", "::ffff:0:a00:1", "fec0::1", "64:ff9b:1::1", "192.0.0.8",
                                "198.18.0.1", "192.0.2.1", "203.0.113.5", "240.0.0.1", "2001:db8::1", "100::1",
                                "::ffff:0:7f00:1", "0.0.0.1"])
def test_addresses_that_are_not_on_the_public_internet(ip):
    assert not is_public_address(ip)


@pytest.mark.parametrize("ip", ["8.8.8.8", "93.184.216.34", "2606:4700:4700::1111", "::ffff:8.8.8.8", "104.16.0.1", "1.1.1.1"])
def test_public_addresses(ip):
    assert is_public_address(ip)


def test_check_destination_needs_every_address_to_be_public():
    assert check_destination("a.test", 80, resolver=lambda h, p: ["8.8.8.8", "1.1.1.1"]) == ["8.8.8.8", "1.1.1.1"]
    with pytest.raises(BlockedDestination):
        check_destination("a.test", 80, resolver=lambda h, p: ["8.8.8.8", "10.0.0.1"])
    with pytest.raises(BlockedDestination):
        check_destination("a.test", 80, resolver=lambda h, p: [])


def test_unknown_host_and_refused_connection_have_plain_messages(site):
    def nxdomain(host, port):
        raise socket.gaierror(8, "nodename nor servname provided")
    with pytest.raises(DownloadError) as err:
        get("http://nonexistent.test/a.png", resolver=nxdomain)
    assert "nonexistent.test" in str(err.value) and "gaierror" not in str(err.value)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(DownloadError) as err:
        get(f"http://127.0.0.1:{port}/", connect_timeout=2)
    assert "connect" in str(err.value).lower()


# ---------------------------------------------------------------- time limits and cancelling

def test_silent_server_times_out(site):
    site.routes["/hang"] = lambda h: time.sleep(2)
    start = time.time()
    with pytest.raises(DownloadTimeout) as err:
        get(site.base + "/hang", read_timeout=0.3)
    assert time.time() - start < 1.5
    assert "in time" in str(err.value)


def test_slow_trickle_hits_the_total_deadline(site):
    def trickle(h):
        h.send_response(200)
        h.send_header("Content-Length", "100000")
        h.end_headers()
        try:
            for _ in range(100):
                h.wfile.write(b"x")
                h.wfile.flush()
                time.sleep(0.1)
        except OSError:
            pass
    site.routes["/trickle"] = trickle
    start = time.time()
    with pytest.raises(DownloadTimeout):
        get(site.base + "/trickle", read_timeout=5, deadline=0.6)
    assert time.time() - start < 2.0


def test_cancel_before_start_makes_no_request(site):
    with pytest.raises(DownloadCancelled):
        get(site.base + "/small.bin", cancelled=lambda: True)
    assert site.requests == []


def test_cancel_during_download(site):
    def trickle(h):
        h.send_response(200)
        h.send_header("Content-Length", "100000")
        h.end_headers()
        try:
            for _ in range(100):
                h.wfile.write(b"x" * 10)
                h.wfile.flush()
                time.sleep(0.05)
        except OSError:
            pass
    site.routes["/trickle"] = trickle
    start = time.time()
    with pytest.raises(DownloadCancelled):
        get(site.base + "/trickle", cancelled=lambda: time.time() - start > 0.3)
    assert time.time() - start < 2.0


# ---------------------------------------------------------------- to disk

def test_download_to_file_places_the_file_atomically(site, tmp_path):
    dest = tmp_path / "out" / "pic.bin"
    got = download_to_file(site.base + "/small.bin", dest, address_policy=LOOPBACK_ONLY)
    assert dest.read_bytes() == b"a" * 1000 and got == dest
    assert [p.name for p in dest.parent.iterdir()] == ["pic.bin"]


def test_failed_download_leaves_old_file_and_no_leftovers(site, tmp_path):
    dest = tmp_path / "pic.bin"
    dest.write_bytes(b"old")
    with pytest.raises(DownloadTooLarge):
        download_to_file(site.base + "/big.bin", dest, max_bytes=2000, address_policy=LOOPBACK_ONLY)
    assert dest.read_bytes() == b"old" and [p.name for p in tmp_path.iterdir()] == ["pic.bin"]
    with pytest.raises(DownloadError):
        download_to_file(site.base + "/missing", tmp_path / "new.bin", address_policy=LOOPBACK_ONLY)
    assert [p.name for p in tmp_path.iterdir()] == ["pic.bin"]


def test_fetch_cached_downloads_once(site, tmp_path):
    cache = tmp_path / "cache"
    first = fetch_cached(cache, site.base + "/small.bin", address_policy=LOOPBACK_ONLY)
    second = fetch_cached(cache, site.base + "/small.bin", address_policy=LOOPBACK_ONLY)
    assert first == second == cache_path(cache, site.base + "/small.bin")
    assert first.read_bytes() == b"a" * 1000 and len(site.requests) == 1


def test_cache_path_is_stable(tmp_path):
    a = cache_path(tmp_path, "http://x/y.jpg")
    assert a == cache_path(tmp_path, "http://x/y.jpg") and a != cache_path(tmp_path, "http://x/z.jpg")
    assert a.suffix == ".jpg"


def test_cache_path_suffix_is_harmless(tmp_path):
    assert cache_path(tmp_path, "http://x/y.JPG?size=1").suffix == ".jpg"
    assert cache_path(tmp_path, "http://x/y").suffix == ""
    assert cache_path(tmp_path, "http://x/y.p%2f..").suffix == ""
    assert cache_path(tmp_path, "http://x/y.toolongsuffix").suffix == ""
    assert cache_path(tmp_path, "http://x/y.jpg").parent == tmp_path


def test_address_rule_does_not_depend_on_the_pythons_is_global(monkeypatch):
    # Python versions disagree about ipaddress.is_global for some ranges; the rule must be our own.
    import ipaddress
    monkeypatch.setattr(ipaddress.IPv4Address, "is_global", property(lambda self: True))
    monkeypatch.setattr(ipaddress.IPv6Address, "is_global", property(lambda self: True))
    for ip in ("10.0.0.1", "192.0.0.8", "fec0::1", "64:ff9b:1::1", "::a00:1", "::ffff:0:a00:1", "198.18.0.1"):
        assert not is_public_address(ip)
    assert is_public_address("8.8.8.8") and is_public_address("2606:4700:4700::1111")


# ---------------------------------------------------------------- headers, stalls, redirects, certificates

def test_too_many_response_headers_is_a_clean_error(site):
    def many(h):
        h.send_response(200)
        for i in range(300):
            h.send_header(f"X-{i}", "v")
        h.send_header("Content-Length", "1")
        h.end_headers()
        h.wfile.write(b"x")
    site.routes["/many"] = many
    with pytest.raises(DownloadError) as err:
        get(site.base + "/many")
    assert "Traceback" not in str(err.value) and "http.client" not in str(err.value)


def test_oversized_response_headers_are_refused(site):
    def huge_line(h):
        h.send_response(200)
        h.send_header("X-Big", "v" * 100_000)
        h.send_header("Content-Length", "1")
        h.end_headers()
        h.wfile.write(b"x")

    def many_big(h):                                  # each line is fine, together they are far too much
        h.send_response(200)
        for i in range(60):
            h.send_header(f"X-{i}", "v" * 4000)
        h.send_header("Content-Length", "1")
        h.end_headers()
        h.wfile.write(b"x")
    site.routes["/line"], site.routes["/total"] = huge_line, many_big
    for path in ("/line", "/total"):
        with pytest.raises(DownloadError) as err:
            get(site.base + path)
        assert "header" in str(err.value).lower()


def test_normal_headers_still_work_after_the_limit_is_added(site):
    def normal(h):
        h.send_response(200)
        for i in range(40):
            h.send_header(f"X-{i}", "v" * 200)
        h.send_header("Content-Length", "3")
        h.end_headers()
        h.wfile.write(b"abc")
    site.routes["/normal"] = normal
    assert get(site.base + "/normal") == b"abc"


def test_silent_server_mid_body_cannot_outlast_the_deadline(site):
    def stall(h):
        h.send_response(200)
        h.send_header("Content-Length", "100000")
        h.end_headers()
        h.wfile.write(b"x" * 10)
        h.wfile.flush()
        time.sleep(4)
    site.routes["/stall"] = stall
    start = time.time()
    with pytest.raises(DownloadTimeout):
        get(site.base + "/stall", read_timeout=30, deadline=0.7)
    assert time.time() - start < 2.5


def test_silent_server_before_headers_cannot_outlast_the_deadline(site):
    site.routes["/hang"] = lambda h: time.sleep(4)
    start = time.time()
    with pytest.raises(DownloadTimeout):
        get(site.base + "/hang", read_timeout=30, deadline=0.7)
    assert time.time() - start < 2.5


def test_a_secure_page_may_not_send_us_to_plain_http():
    with pytest.raises(DownloadError) as err:
        check_redirect("https://example.com/a", "http://example.com/b")
    assert "secure" in str(err.value).lower()
    check_redirect("http://example.com/a", "https://example.com/b")          # upgrades are fine
    check_redirect("https://example.com/a", "https://other.com/b")
    check_redirect("http://example.com/a", "http://example.com/b")


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl command to make a certificate")
def test_untrusted_certificate_is_explained_plainly(tmp_path):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
                    "-days", "1", "-subj", "/CN=localhost"], check=True, capture_output=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))

    class Quiet(http.server.ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            pass
    httpd = Quiet(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    try:
        with pytest.raises(DownloadError) as err:
            get(f"https://localhost:{httpd.server_address[1]}/", resolver=lambda h, p: ["127.0.0.1"])
        assert "certificate" in str(err.value).lower()
    finally:
        httpd.shutdown()


def test_the_guarded_connection_classes_are_built_once():
    from scrappy.download import _session
    a = _session(lambda h, p: []).get_adapter("http://x").poolmanager.pool_classes_by_scheme
    b = _session(lambda h, p: []).get_adapter("http://x").poolmanager.pool_classes_by_scheme
    assert a["http"].func is b["http"].func and a["https"].func is b["https"].func
    assert a["http"].func.ConnectionCls is b["http"].func.ConnectionCls


def test_urllib3_internals_we_rely_on_are_still_there(site):
    """The downloader uses HTTPConnection._dns_host (the name to connect to) and HTTPResponse._fp
    (the http.client response, for read1). If a urllib3 upgrade removes
    them this fails loudly instead of the downloader quietly losing its limits."""
    import urllib3
    from urllib3.connection import HTTPConnection
    assert hasattr(HTTPConnection("example.com", 80), "_dns_host")
    pool = urllib3.HTTPConnectionPool("127.0.0.1", site.port)
    response = pool.urlopen("GET", "/small.bin", preload_content=False, retries=False)
    try:
        assert hasattr(response, "_fp") and hasattr(response._fp, "read1")
    finally:
        response.release_conn()
        response.close()


def test_headers_dripped_one_byte_at_a_time_cannot_outlast_the_deadline(site):
    def drip(h):
        try:
            h.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
            for _ in range(60):
                h.wfile.write(b"v")
                h.wfile.flush()
                time.sleep(0.1)
        except OSError:
            pass
        h.close_connection = True
    site.routes["/drip"] = drip
    start = time.time()
    with pytest.raises(DownloadTimeout):
        get(site.base + "/drip", read_timeout=30, deadline=0.6)
    assert time.time() - start < 2.5
