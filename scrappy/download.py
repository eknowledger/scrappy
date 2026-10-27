"""Download pictures and files from the web, which is untrusted, safely.

* Only http and https, also after every redirect. No credentials in URLs, no cookies, no proxies, no netrc.
* The size is capped while streaming (Content-Length is only used to refuse early), compressed answers are refused,
  and there are connect/read timeouts plus one total deadline. A `cancelled` callback is polled like in the
  video and YouTube modules.
* This program runs on a home network, so servers that live on it (loopback, private, link-local, ... addresses) are
  off limits. The name is resolved once, every address must be public, and the connection goes to exactly that checked
  address (no second lookup an attacker could answer differently). `allow_private=True` is the explicit opt-in for tests
  and local use; `resolver` and `address_policy` let tests replace DNS and the rule.
* Time: a watchdog timer shuts the sockets down when the total deadline passes, so even a server that stays silent (or
  drips bytes) cannot keep a read blocked past it. The `cancelled` callback is polled in the calling thread between
  reads, so cancelling a stalled server takes effect when the read gives up (at most `read_timeout`). Resolving a name
  (getaddrinfo) cannot be interrupted or bounded by Python; the deadline is checked before and after it.
* Response headers are limited to 64 KB in total (http.client alone allows about 6 MB), and a secure (https) page
  may not redirect to plain http.
* Content-Type is reported but never trusted: the real validation is `processing.images.open_image`.
* Files are written to a temporary file next to the target and moved into place, so a failure leaves nothing behind.

Private urllib3 attributes this module relies on (tests/test_download.py checks that they exist, so an upgrade that
removes one fails loudly): `HTTPConnection._dns_host` (the host name a connection is for, to resolve and pin it) and
`HTTPResponse._fp` (the underlying http.client response, whose `read1` returns as soon as some bytes arrive; urllib3
2.0 has no public `read1`). Needs requests >= 2.31 and urllib3 >= 2.0.
"""
from __future__ import annotations

import hashlib
import http.client
import http.cookiejar
import ipaddress
import os
import re
import functools
import socket
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.util.connection import create_connection

from .providers.base import ProviderError

USER_AGENT = "Mozilla/5.0 (compatible; Scrappy/0.1; personal Batocera game-art tool)"
MAX_IMAGE_BYTES = 25_000_000      # the largest picture we accept
CONNECT_TIMEOUT = 10.0            # seconds to open a connection
READ_TIMEOUT = 10.0               # seconds of silence from the server before we give up
TOTAL_DEADLINE = 60.0             # seconds for the whole download, redirects included
MAX_REDIRECTS = 5
CHUNK = 65536
MAX_HEADER_BYTES = 65536     # status line and headers together
REDIRECT_CODES = (301, 302, 303, 307, 308)
_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,5}")


class DownloadError(ProviderError):
    """A download failed; the text is plain language for the user."""


class UnsupportedURL(DownloadError):
    """Not an http(s) address we are willing to fetch."""


class BlockedDestination(DownloadError):
    """The address points at a machine on the local network (or is otherwise not on the public internet)."""


class DownloadTooLarge(DownloadError):
    """The answer is bigger than the limit."""


class DownloadTimeout(DownloadError):
    """The server was too slow."""


class DownloadCancelled(DownloadError):
    """The caller cancelled."""


@dataclass
class Downloaded:
    data: bytes
    url: str                  # where the bytes finally came from, after redirects
    content_type: str = ""    # what the server claimed; a hint only


# ---------------------------------------------------------------- addresses

_DENIED_V4 = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24",
    "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
    "224.0.0.0/4", "240.0.0.0/4")]
_DENIED_V6 = [ipaddress.ip_network(n) for n in (
    "::/128", "::1/128", "64:ff9b:1::/48", "100::/64", "2001::/32", "2001:10::/28", "2001:20::/28", "2001:db8::/32",
    "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8")]
# IPv6 forms that carry an IPv4 address: (network, bit position of the IPv4 part); they are judged by that address
_GLOBAL_UNICAST_V6 = ipaddress.ip_network("2000::/3")
_EMBEDDING_V6 = [(ipaddress.ip_network(n), shift) for n, shift in (
    ("::ffff:0:0/96", 0),          # IPv4-mapped
    ("::ffff:0:0:0/96", 0),        # IPv4-translated (SIIT)
    ("64:ff9b::/96", 0),           # NAT64
    ("::/96", 0),                  # IPv4-compatible (::  and ::1 are denied before this applies)
    ("2002::/16", 80))]            # 6to4


def is_public_address(ip) -> bool:
    """True only for addresses on the public internet.

    The rule is our own list (not ``ipaddress.is_global``, which differs between Python versions). IPv6 forms that carry
    an IPv4 address are judged by that address.
    """
    try:
        addr = ipaddress.ip_address(str(ip).split("%")[0])
    except ValueError:
        return False
    if addr.version == 6:
        if any(addr in net for net in _DENIED_V6):
            return False
        for net, shift in _EMBEDDING_V6:
            if addr in net:
                addr = ipaddress.IPv4Address((int(addr) >> shift) & 0xFFFFFFFF)
                break
        else:                                    # ordinary IPv6: public only inside global unicast 2000::/3
            return addr in _GLOBAL_UNICAST_V6 and not _special(addr)
    return not any(addr in net for net in _DENIED_V4) and not _special(addr)


def _special(addr) -> bool:
    return (addr.is_reserved or addr.is_unspecified or addr.is_multicast or addr.is_link_local or addr.is_loopback
            or addr.is_private)


def system_resolver(host: str, port: int) -> list:
    """Every address the operating system gives for `host`."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0].split("%")[0] for info in infos))


def check_destination(host: str, port: int, resolver=None, address_policy=None) -> list:
    """Resolve `host` and return its addresses, or raise BlockedDestination unless every one passes the policy."""
    policy = address_policy or is_public_address
    addresses = list((resolver or system_resolver)(host, port))
    if not addresses or not all(policy(a) for a in addresses):
        raise BlockedDestination(f"{host} is on your own network or not a public website, so it was not contacted")
    return addresses


class _Guard:
    """Per-download state shared with its connections: name checking, and the watchdog that cuts sockets at the deadline."""

    def __init__(self, resolve):
        self.resolve = resolve
        self.expired = False
        self._lock = threading.Lock()
        self._sockets = []                  # (connection, raw socket) pairs

    def track(self, conn, sock) -> None:
        with self._lock:
            if self.expired:
                sock.close()
                raise DownloadTimeout("The server did not answer in time")
            self._sockets.append((conn, sock))

    def expire(self) -> None:
        """Deadline passed: make every blocked read or write on this download's sockets fail now."""
        with self._lock:
            self.expired = True
            pairs = list(self._sockets)
        for conn, raw in pairs:
            for sock in (getattr(conn, "sock", None), raw):       # the TLS socket wraps (and detaches) the raw one
                try:
                    if sock is not None:
                        sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


class _BudgetedReader:
    """Wraps a response's file only while its status line and headers are read; refuses more than `limit` bytes."""

    def __init__(self, fp, limit):
        self._fp, self._left = fp, limit

    def readline(self, size=-1):
        line = self._fp.readline(size)
        self._left -= len(line)
        if self._left < 0:
            raise DownloadError("The server sent unreasonably large response headers")
        return line

    def __getattr__(self, name):
        return getattr(self._fp, name)


class _LimitedResponse(http.client.HTTPResponse):
    def begin(self):
        wrapper = self.fp = _BudgetedReader(self.fp, MAX_HEADER_BYTES)
        try:
            super().begin()
        finally:
            if self.fp is wrapper:                   # (a bodiless answer has already closed and dropped it)
                self.fp = wrapper._fp


class _GuardedConnection:
    """Mixin for urllib3 connections: only ever connect to the addresses the guard allows, to the very address it checked."""
    response_class = _LimitedResponse

    def __init__(self, *args, guard=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._guard = guard

    def _new_conn(self):
        if self._guard is None:
            raise RuntimeError("guarded connection without a guard")
        host, port = self._dns_host, self.port          # private urllib3 attribute: see the module docstring
        last = None
        for ip in self._guard.resolve(host, port):
            try:
                sock = create_connection((ip, port), timeout=self.timeout, source_address=self.source_address,
                                         socket_options=self.socket_options)
            except socket.timeout as exc:
                last = urllib3.exceptions.ConnectTimeoutError(self, f"Connection to {host} timed out")
                last.__cause__ = exc
            except OSError as exc:
                last = urllib3.exceptions.NewConnectionError(self, f"Failed to connect to {host}: {exc}")
                last.__cause__ = exc
            else:
                self._guard.track(self, sock)
                return sock
        raise last or urllib3.exceptions.NewConnectionError(self, f"No address for {host}")


class _GuardedHTTPConnection(_GuardedConnection, HTTPConnection):
    pass


class _GuardedHTTPSConnection(_GuardedConnection, HTTPSConnection):
    pass


class _GuardedPool:
    def __init__(self, *args, guard=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.conn_kw["guard"] = guard                   # handed to every connection the pool makes


class _GuardedHTTPPool(_GuardedPool, HTTPConnectionPool):
    ConnectionCls = _GuardedHTTPConnection


class _GuardedHTTPSPool(_GuardedPool, HTTPSConnectionPool):
    ConnectionCls = _GuardedHTTPSConnection


class _GuardedAdapter(HTTPAdapter):
    def __init__(self, guard):
        self._guard = guard
        super().__init__(max_retries=0)

    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            "http": functools.partial(_GuardedHTTPPool, guard=self._guard),
            "https": functools.partial(_GuardedHTTPSPool, guard=self._guard)}


def _session(guard) -> requests.Session:
    s = requests.Session()
    s.trust_env = False                                               # no proxy variables, no ~/.netrc
    s.cookies.set_policy(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))     # never store or send a cookie
    s.headers.clear()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "image/*,*/*;q=0.5", "Accept-Encoding": "identity"})
    adapter = _GuardedAdapter(guard)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


# ---------------------------------------------------------------- helpers

def _check_url(url) -> tuple:
    if not isinstance(url, str) or not url.strip():
        raise UnsupportedURL("There is no web address to download from")
    try:
        parts = urlsplit(url.strip())
        host, _ = parts.hostname, parts.port
    except ValueError:
        raise UnsupportedURL("That is not a valid web address") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise UnsupportedURL(f"Only http and https addresses can be downloaded, not '{parts.scheme or 'no scheme'}:'")
    if not host:
        raise UnsupportedURL("That web address has no server name")
    if parts.username is not None or parts.password is not None:
        raise UnsupportedURL("Web addresses with a user name or password are not downloaded")
    return parts.geturl(), host


def _size_text(n: int) -> str:
    return f"{n / 1_000_000:g} MB" if n >= 1_000_000 else f"{max(1, round(n / 1000))} KB"


def _status_error(code: int, host: str) -> DownloadError:
    if code in (401, 403):
        return DownloadError(f"{host} refused the download (HTTP {code})")
    if code in (404, 410):
        return DownloadError(f"The file was not found on {host} (HTTP {code})")
    if code == 429:
        return DownloadError(f"{host} asked us to slow down, try again in a minute (HTTP 429)")
    if code >= 500:
        return DownloadError(f"{host} has a problem right now (HTTP {code})")
    return DownloadError(f"{host} answered with an unexpected status (HTTP {code})")


def _stop_if_needed(deadline: float, cancelled) -> None:
    if cancelled is not None and cancelled():
        raise DownloadCancelled("The download was cancelled")
    if time.monotonic() >= deadline:
        raise DownloadTimeout("The server did not finish in time")


def _chunk_reader(raw):
    """A function returning the next bytes of the body as soon as *some* arrive (never waiting to fill a whole chunk)."""
    read1 = getattr(raw, "read1", None)                      # urllib3 2.1 and newer
    if read1 is not None:
        return lambda n: read1(n, decode_content=False)
    read1 = getattr(getattr(raw, "_fp", None), "read1", None)         # urllib3 2.0: the http.client response underneath
    if read1 is not None:
        return read1
    return lambda n: raw.read(min(n, 4096), decode_content=False)     # last resort: blocks until that many bytes arrive


def _read_body(response, write, max_bytes: int, deadline: float, cancelled, guard) -> int:
    """Stream the body into `write`, enforcing the size limit, the deadline and cancellation as it goes."""
    declared = response.headers.get("Content-Length", "")
    declared = int(declared) if declared.isdigit() else None
    too_big = DownloadTooLarge(f"That file is too big (more than {_size_text(max_bytes)} is not downloaded)")
    if declared is not None and declared > max_bytes:
        raise too_big
    read = _chunk_reader(response.raw)
    total = 0
    try:
        while True:
            _stop_if_needed(deadline, cancelled)
            chunk = read(CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise too_big
            write(chunk)
    except (urllib3.exceptions.ReadTimeoutError, socket.timeout) as exc:
        raise DownloadTimeout("The server did not answer in time") from exc
    except (urllib3.exceptions.HTTPError, http.client.HTTPException, OSError, ValueError) as exc:
        if guard.expired:
            raise DownloadTimeout("The server did not finish in time") from exc
        raise DownloadError("The connection broke during the download") from exc
    if guard.expired:
        raise DownloadTimeout("The server did not finish in time")
    if declared is not None and total != declared:
        raise DownloadError("The connection broke during the download")
    return total


def check_redirect(old_url: str, new_url: str) -> None:
    """A redirect may not lead from a secure (https) address to a plain http one."""
    if urlsplit(old_url).scheme.lower() == "https" and urlsplit(new_url).scheme.lower() == "http":
        raise DownloadError("The server tried to move the download from a secure to an insecure address, which was refused")


def _connection_problem(exc, host: str, guard) -> DownloadError:
    if guard.expired:
        return DownloadTimeout(f"{host} did not answer in time")
    if isinstance(exc, requests.exceptions.SSLError):
        return DownloadError(f"The security certificate of {host} is invalid or not trusted, so nothing was downloaded")
    if exc.args and isinstance(exc.args[0], urllib3.exceptions.ProtocolError):
        return DownloadError(f"{host} closed the connection or sent an answer that could not be understood")
    return DownloadError(f"Could not connect to {host} (is it down, or is the address wrong?)")


def _run(url, write, max_bytes=MAX_IMAGE_BYTES, connect_timeout=CONNECT_TIMEOUT, read_timeout=READ_TIMEOUT,
         deadline=TOTAL_DEADLINE, max_redirects=MAX_REDIRECTS, cancelled=None, resolver=None, allow_private=False,
         address_policy=None) -> tuple:
    """Follow redirects by hand (each target checked) and stream the final answer into `write`. -> (url, content_type)"""
    policy = address_policy if address_policy is not None else ((lambda ip: True) if allow_private else None)
    resolve = resolver or system_resolver

    def checked(host, port):
        try:
            return check_destination(host, port, resolve, policy)
        except socket.gaierror as exc:
            raise DownloadError(f"The server '{host}' could not be found (check the address and the internet connection)") from exc

    url, _ = _check_url(url)
    end = time.monotonic() + deadline
    guard = _Guard(checked)
    watchdog = threading.Timer(max(0.0, deadline), guard.expire)
    watchdog.daemon = True
    watchdog.start()
    session = _session(guard)
    try:
        for _ in range(max_redirects + 1):
            _stop_if_needed(end, cancelled)
            host = urlsplit(url).hostname
            left = end - time.monotonic()
            try:
                response = session.get(url, stream=True, allow_redirects=False,
                                       timeout=(max(0.05, min(connect_timeout, left)), max(0.05, min(read_timeout, left))))
            except requests.Timeout as exc:
                raise DownloadTimeout(f"{host} did not answer in time") from exc
            except requests.ConnectionError as exc:
                raise _connection_problem(exc, host, guard) from exc
            except requests.RequestException as exc:
                raise DownloadError(f"The download from {host} failed") from exc
            with response:
                if response.status_code in REDIRECT_CODES:
                    target = response.headers.get("Location")
                    if not target:
                        raise DownloadError(f"{host} redirected without saying where")
                    new_url, _ = _check_url(urljoin(url, target.strip()))
                    check_redirect(url, new_url)
                    url = new_url
                    continue
                if response.status_code != 200:
                    raise _status_error(response.status_code, host)
                if response.headers.get("Content-Encoding", "identity").strip().lower() not in ("", "identity"):
                    raise DownloadError(f"{host} sent a compressed file, which is not accepted")
                _read_body(response, write, max_bytes, end, cancelled, guard)
                return url, response.headers.get("Content-Type", "")
        raise DownloadError("The address redirects too many times")
    finally:
        watchdog.cancel()
        session.close()


# ---------------------------------------------------------------- public functions

def download(url: str, max_bytes: int = MAX_IMAGE_BYTES, **options) -> Downloaded:
    """Fetch `url` into memory. Raises DownloadError (a ProviderError) with a plain message on any failure.

    Options: connect_timeout, read_timeout, deadline (seconds, whole download), max_redirects,
    cancelled (callable -> bool), resolver(host, port) -> addresses, allow_private, address_policy(ip) -> bool.
    """
    buf = bytearray()
    final, ctype = _run(url, buf.extend, max_bytes, **options)
    return Downloaded(bytes(buf), final, ctype)


def download_bytes(url: str, max_bytes: int = MAX_IMAGE_BYTES, **options) -> bytes:
    """Like `download`, returning just the bytes (what the controller uses as its fetch function)."""
    return download(url, max_bytes, **options).data


def download_to_file(url: str, dest, max_bytes: int = MAX_IMAGE_BYTES, **options) -> Path:
    """Stream `url` into `dest` (parent folders are created). On any failure `dest` is left as it was."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=dest.name + ".", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            _run(url, f.write, max_bytes, **options)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, dest)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return dest


def fetch_cached(cache_dir, url: str, max_bytes: int = MAX_IMAGE_BYTES, **options) -> Path:
    """The cache file for `url`, downloading it first if it is not there yet."""
    path = cache_path(cache_dir, url)
    if path.is_file() and path.stat().st_size > 0:
        return path
    return download_to_file(url, path, max_bytes, **options)


def cache_path(cache_dir, url: str) -> Path:
    """A stable file name in `cache_dir` for `url`; only a short, plain extension is kept."""
    suffix = os.path.splitext(urlsplit(str(url)).path)[1]
    suffix = suffix.lower() if _SUFFIX.fullmatch(suffix) else ""
    return Path(cache_dir) / (hashlib.sha256(str(url).encode("utf-8")).hexdigest()[:40] + suffix)
