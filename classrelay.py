"""
ClassRelay (师生局域网依赖中继/代理分发服务)

Single-file, stdlib-only caching relay for classroom labs. The teacher machine
is online; student machines fetch pip, npm, Docker, and PlatformIO/Espressif
assets through it. Also serves an HTTP/HTTPS proxy, a small web console, and
one-click setup scripts.

Security defaults:
- CONNECT is limited to port 443, and the plain HTTP proxy to ports 80/443.
- Loopback, link-local, private, CGNAT, multicast, and documentation targets
  are refused after DNS resolution, unless --allow-private is set.
- The forward proxy does not cache. Mirror cache entries never store
  Set-Cookie, and a request that carries Cookie is not cached.
- Optional HTTP Basic auth (--auth user:pass). Leave it off for a simple lab.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import http.server
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import socketserver
import ssl
import sys
import tempfile
import threading
import time
import urllib.parse
import zlib
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

VERSION = "1.2.0"
DEFAULT_PORT = 8080
DEFAULT_CACHE_DIR = os.path.expanduser("~/.classrelay/cache")
DEFAULT_MAX_CACHE_BYTES = 1024 * 1024 * 1024
DEFAULT_INDEX_TTL = 10 * 60
DEFAULT_ASSET_TTL = 7 * 24 * 3600
DEFAULT_TUNNEL_IDLE = 600
DEFAULT_MAX_CONNECTIONS = 64
MAX_INDEX_BYTES = 16 * 1024 * 1024
CONNECT_TIMEOUT = 15
READ_TIMEOUT = 120
DNS_TIMEOUT = 8
MAX_REDIRECTS = 5

UPSTREAMS = {
    "pypi": "https://pypi.tuna.tsinghua.edu.cn/simple",
    "npm": "https://registry.npmmirror.com",
    "docker": "https://docker.m.daocloud.io",
    "platformio": "https://api.registry.platformio.org",
    "github_raw": "https://raw.githubusercontent.com",
    "espressif": "https://dl.espressif.com",
}

# Hosts whose package-file URLs should be rewritten back through the relay.
PYPI_FILE_HOSTS = {
    "files.pythonhosted.org",
    "pypi.org",
    "pypi.python.org",
    "pypi.tuna.tsinghua.edu.cn",
    "mirrors.aliyun.com",
    "mirrors.cloud.tencent.com",
    "mirror.baidu.com",
}

ASSET_SUFFIXES = (
    ".whl",
    ".tar.gz",
    ".zip",
    ".tgz",
    ".egg",
    ".bz2",
    ".xz",
    ".msi",
    ".exe",
    ".apk",
)

HOP_BY_HOP = {
    "connection",
    "proxy-connection",
    "keep-alive",
    "transfer-encoding",
    "te",
    "trailer",
    "upgrade",
    "proxy-authenticate",
    "proxy-authorization",
}

DROP_QUERY_KEYS = {
    "token",
    "auth",
    "password",
    "access_token",
    "auth_token",
}

_BLOCK_V4 = [
    ipaddress.ip_network(item)
    for item in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
]
_BLOCK_V6 = [
    ipaddress.ip_network(item)
    for item in (
        "::/128",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
        "fec0::/10",
        "ff00::/8",
        "2001:db8::/32",
    )
]

_USERINFO_RE = re.compile(r"(https?://)([^/\s:@]+):([^/\s@]+)@")
_HREF_RE = re.compile(r"""href\s*=\s*(['"])(.*?)\1""", re.IGNORECASE)
_REALM_RE = re.compile(r'realm="([^"]*)"', re.IGNORECASE)
_MARK_START = "# >>> ClassRelay >>>"
_MARK_END = "# <<< ClassRelay <<<"

LOG = logging.getLogger("classrelay")


class PolicyError(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


class UpstreamError(Exception):
    pass


def configure_logging() -> None:
    if LOG.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    LOG.propagate = False


def redact_text(text: str) -> str:
    return _USERINFO_RE.sub(r"\1***:***@", text)


def is_blocked_ip(ip: str) -> bool:
    """True for addresses a student proxy must not reach."""
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        return True
    nets = _BLOCK_V4 if isinstance(addr, ipaddress.IPv4Address) else _BLOCK_V6
    return any(addr in net for net in nets)


def resolve_addresses(host: str, port: int):
    """DNS lookup. Tests monkeypatch this."""
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


def _resolve_with_timeout(host: str, port: int, timeout: float = DNS_TIMEOUT):
    box: Dict[str, object] = {}

    def run() -> None:
        try:
            box["infos"] = resolve_addresses(host, port)
        except Exception as exc:  # noqa: BLE001 - propagated to the caller
            box["err"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise PolicyError(504, "DNS timeout")
    if "err" in box:
        err = box["err"]
        if isinstance(err, socket.gaierror):
            raise UpstreamError("DNS resolution failed") from err
        raise err
    return box.get("infos") or []


def resolve_destination(host: str, port: int, *, private_ok: bool):
    """Resolve host and refuse the whole name if any address is blocked.

    Connecting later uses these results, so a DNS rebinding between the check
    and the TCP connect does not apply to CONNECT or to this client.
    """
    if not host or len(host) > 255:
        raise PolicyError(400, "Bad target host")
    infos = _resolve_with_timeout(host, port)
    if not infos:
        raise UpstreamError("DNS resolution failed")
    for info in infos:
        ip = str(info[4][0]).split("%", 1)[0]
        if is_blocked_ip(ip) and not private_ok:
            raise PolicyError(403, "Private or non-public address blocked")
    return infos


def host_permitted(host: str, suffixes: Sequence[str]) -> bool:
    if not suffixes:
        return True
    name = host.lower().rstrip(".")
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    for suffix in suffixes:
        item = suffix.lower().strip().lstrip(".")
        if not item:
            continue
        if name == item or name.endswith("." + item):
            return True
    return False


def parse_connect_target(path: str) -> Tuple[str, int]:
    raw = path.strip()
    if not raw or any(ch in raw for ch in " \r\n\t\\/"):
        raise ValueError("bad target")
    if raw.startswith("["):
        end = raw.find("]")
        if end <= 1:
            raise ValueError("bad ipv6")
        host = raw[1:end]
        rest = raw[end + 1 :]
        if not rest.startswith(":") or rest.count(":") != 1:
            raise ValueError("bad port")
        port_s = rest[1:]
    else:
        if raw.count(":") != 1:
            raise ValueError("bad target")
        host, port_s = raw.split(":", 1)
    if not host or not port_s.isdigit():
        raise ValueError("bad port")
    port = int(port_s)
    if not 1 <= port <= 65535:
        raise ValueError("bad port")
    return host, port


def strip_userinfo(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = host
    if parts.port:
        netloc = f"{host}:{parts.port}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def sanitize_query(query: str) -> str:
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    kept = [(key, value) for key, value in pairs if key.lower() not in DROP_QUERY_KEYS]
    return urllib.parse.urlencode(kept)


def endpoint_of(url: str) -> Optional[Tuple[str, int]]:
    parts = urllib.parse.urlsplit(url)
    if not parts.hostname or parts.scheme not in ("http", "https"):
        return None
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.hostname.lower(), port


def origin_of(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def simple_root(pypi_upstream: str) -> str:
    parts = urllib.parse.urlsplit(pypi_upstream)
    path = parts.path.rstrip("/")
    if path.endswith("/simple"):
        path = path[: -len("/simple")]
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def join_upstream(base: str, rest: str, query: str) -> str:
    if rest in ("", "/"):
        suffix = "/"
    else:
        if not rest.startswith("/"):
            rest = "/" + rest
        decoded = urllib.parse.unquote(rest)
        if "\\" in decoded or "\x00" in decoded or decoded.startswith("//"):
            raise ValueError("bad path")
        if any(part == ".." for part in decoded.split("/")):
            raise ValueError("bad path")
        suffix = rest
    base_parts = urllib.parse.urlsplit(base)
    base_path = base_parts.path.rstrip("/")
    path = (base_path + suffix) if suffix != "/" else (base_path + "/")
    safe_query = sanitize_query(query)
    url = urllib.parse.urlunsplit((base_parts.scheme, base_parts.netloc, path, safe_query, ""))
    if urllib.parse.urlsplit(url).hostname != base_parts.hostname:
        raise ValueError("bad host")
    return url


def basic_matches(header_value: str, user: str, password: str) -> bool:
    if not header_value:
        return False
    scheme, _, blob = header_value.strip().partition(" ")
    if scheme.lower() != "basic" or not blob or any(ch in blob for ch in "\r\n"):
        return False
    try:
        raw = base64.b64decode(blob, validate=True)
        text = raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    got_user, sep, got_pass = text.partition(":")
    if not sep:
        return False
    user_ok = hmac.compare_digest(
        hashlib.sha256(got_user.encode("utf-8")).digest(),
        hashlib.sha256(user.encode("utf-8")).digest(),
    )
    pass_ok = hmac.compare_digest(
        hashlib.sha256(got_pass.encode("utf-8")).digest(),
        hashlib.sha256(password.encode("utf-8")).digest(),
    )
    return user_ok and pass_ok


def seal_url(url: str, secret: bytes) -> str:
    mac = hmac.new(secret, url.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    payload = f"{mac}:{url}".encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def unseal_url(token: str, secret: bytes) -> Optional[str]:
    if not token or len(token) > 4096:
        return None
    pad = "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(token + pad).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    mac, sep, url = raw.partition(":")
    if not sep or not url:
        return None
    expect = hmac.new(secret, url.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(mac, expect):
        return None
    return url


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def ascii_reason(reason: str) -> str:
    cleaned = "".join(ch if 32 <= ord(ch) < 127 else " " for ch in (reason or ""))
    cleaned = " ".join(cleaned.split())
    return cleaned[:80] or "OK"


def header_values(headers: Sequence[Tuple[str, str]], name: str) -> List[str]:
    lowered = name.lower()
    return [value for key, value in headers if key.lower() == lowered]


def header_value(headers: Sequence[Tuple[str, str]], name: str) -> Optional[str]:
    values = header_values(headers, name)
    return values[-1] if values else None


def get_local_ips() -> List[str]:
    """Non-loopback IPv4 addresses, best-effort and computed once at startup."""
    ips: List[str] = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.settimeout(1)
            probe.connect(("223.5.5.5", 80))
            primary = probe.getsockname()[0]
        finally:
            probe.close()
        if primary and not primary.startswith("127."):
            ips.append(primary)
    except Exception:
        pass
    box: Dict[str, object] = {}

    def lookup() -> None:
        try:
            box["ips"] = socket.gethostbyname_ex(socket.gethostname())[2]
        except Exception as exc:
            box["err"] = exc

    thread = threading.Thread(target=lookup, daemon=True)
    thread.start()
    thread.join(2)
    for ip in box.get("ips") or []:
        if isinstance(ip, str) and not ip.startswith("127.") and ip not in ips:
            ips.append(ip)
    if not ips:
        ips.append("127.0.0.1")
    return ips


def format_host(hostname: str, port: int, scheme: str) -> str:
    host = f"[{hostname}]" if ":" in hostname else hostname
    default = 443 if scheme == "https" else 80
    if port != default:
        return f"{host}:{port}"
    return host


def _connect_addrs(infos, timeout: float) -> socket.socket:
    last: Optional[BaseException] = None
    for family, socktype, proto, _canon, sockaddr in infos:
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(timeout)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        try:
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last = exc
            sock.close()
    raise UpstreamError(f"connect failed: {last}")


def _read_response_head(fp) -> Tuple[int, str, List[Tuple[str, str]]]:
    status_line = fp.readline(8192)
    if not status_line:
        raise UpstreamError("empty upstream response")
    if len(status_line) >= 8192:
        raise UpstreamError("upstream status line too long")
    try:
        text = status_line.decode("iso-8859-1").rstrip("\r\n")
        parts = text.split(" ", 2)
        if len(parts) < 2:
            raise ValueError("short status")
        code = int(parts[1])
        reason = parts[2] if len(parts) > 2 else ""
    except (ValueError, UnicodeError) as exc:
        raise UpstreamError("bad upstream status line") from exc
    headers: List[Tuple[str, str]] = []
    total = 0
    while True:
        line = fp.readline(65536)
        if not line:
            raise UpstreamError("truncated upstream headers")
        total += len(line)
        if total > 65536:
            raise UpstreamError("upstream headers too large")
        if line in (b"\r\n", b"\n"):
            break
        if line[:1] in (b" ", b"\t") and headers:
            key, value = headers[-1]
            headers[-1] = (key, value + " " + line.decode("iso-8859-1").strip())
            continue
        decoded = line.decode("iso-8859-1").rstrip("\r\n")
        if ":" not in decoded:
            continue
        name, value = decoded.split(":", 1)
        headers.append((name.strip(), value.strip()))
    lengths = header_values(headers, "content-length")
    if len({item.strip() for item in lengths}) > 1:
        raise UpstreamError("conflicting content-length")
    return code, reason, headers


def _iter_length(fp, content_length: str) -> Iterator[bytes]:
    try:
        remaining = int(content_length)
    except ValueError as exc:
        raise UpstreamError("bad content-length") from exc
    if remaining < 0:
        raise UpstreamError("bad content-length")
    while remaining:
        chunk = fp.read(min(65536, remaining))
        if not chunk:
            raise UpstreamError("short upstream body")
        remaining -= len(chunk)
        yield chunk


def _iter_eof(fp) -> Iterator[bytes]:
    while True:
        chunk = fp.read(65536)
        if not chunk:
            break
        yield chunk


def _iter_chunked(fp) -> Iterator[bytes]:
    while True:
        line = fp.readline(8192)
        if not line:
            raise UpstreamError("truncated chunk")
        try:
            size = int(line.split(b";", 1)[0].strip(), 16)
        except ValueError as exc:
            raise UpstreamError("bad chunk size") from exc
        if size < 0 or size > 2 * 1024 * 1024 * 1024:
            raise UpstreamError("bad chunk size")
        if size == 0:
            while True:
                trailer = fp.readline(8192)
                if trailer in (b"\r\n", b"\n", b""):
                    break
            return
        remaining = size
        while remaining:
            chunk = fp.read(min(65536, remaining))
            if not chunk:
                raise UpstreamError("short chunk")
            remaining -= len(chunk)
            yield chunk
        delim = fp.readline(8)
        if delim not in (b"\r\n", b"\n"):
            raise UpstreamError("bad chunk delimiter")


def _iter_gzip(stream: Iterable[bytes]) -> Iterator[bytes]:
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    for chunk in stream:
        data = decoder.decompress(chunk)
        if data:
            yield data
    tail = decoder.flush()
    if tail:
        yield tail


class UpstreamResponse:
    def __init__(self, status: int, reason: str, headers: List[Tuple[str, str]], fp, sock, head: bool):
        self.status = status
        self.reason = reason
        self.headers = headers
        self._fp = fp
        self._sock = sock
        self._head = head
        self.decoded_gzip = False
        self._consumed = False

    def iter_body(self) -> Iterator[bytes]:
        if self._head or self._consumed:
            return
        self._consumed = True
        transfer = (header_value(self.headers, "transfer-encoding") or "").lower()
        if "chunked" in transfer:
            stream: Iterable[bytes] = _iter_chunked(self._fp)
        else:
            length = header_value(self.headers, "content-length")
            if length is not None:
                stream = _iter_length(self._fp, length)
            else:
                stream = _iter_eof(self._fp)
        encoding = (header_value(self.headers, "content-encoding") or "").lower()
        if "gzip" in encoding:
            self.decoded_gzip = True
            yield from _iter_gzip(stream)
        else:
            yield from stream

    def read_all(self, limit: int) -> bytes:
        chunks: List[bytes] = []
        total = 0
        for chunk in self.iter_body():
            total += len(chunk)
            if total > limit:
                raise UpstreamError("upstream body too large")
            chunks.append(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        for closer in (self._fp, self._sock):
            try:
                if closer is not None:
                    closer.close()
            except Exception:
                pass
        self._fp = None
        self._sock = None

    def __enter__(self) -> "UpstreamResponse":
        return self

    def __exit__(self, *_args) -> None:
        self.close()


def fetch(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    allow_private: bool = False,
    trusted_endpoints: Iterable[Tuple[str, int]] = (),
    allow_hosts: Sequence[str] = (),
    check_allow_hosts: bool = False,
    allowed_ports: Optional[Iterable[int]] = None,
    timeout: float = CONNECT_TIMEOUT,
) -> UpstreamResponse:
    """Fetch a URL with pinned DNS, redirect checks, and no hop-by-hop leakage."""
    request_headers = {key: value for key, value in (headers or {}).items() if key.lower() != "host"}
    trusted = {(host.lower(), port) for host, port in trusted_endpoints}
    ports = set(allowed_ports) if allowed_ports is not None else None
    current = url
    current_method = method.upper()
    for _hop in range(MAX_REDIRECTS + 1):
        if any(ch in current for ch in "\r\n\x00"):
            raise PolicyError(400, "Bad URL")
        current = strip_userinfo(current)
        parts = urllib.parse.urlsplit(current)
        if parts.scheme not in ("http", "https"):
            raise PolicyError(400, "Unsupported URL scheme")
        host = parts.hostname
        if not host:
            raise PolicyError(400, "Missing host")
        try:
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError as exc:
            raise PolicyError(400, "Bad port") from exc
        if ports is not None and port not in ports:
            raise PolicyError(403, "Port not allowed")
        if check_allow_hosts and not host_permitted(host, allow_hosts):
            raise PolicyError(403, "Host not in allowlist")
        private_ok = allow_private or (host.lower(), port) in trusted
        infos = resolve_destination(host, port, private_ok=private_ok)
        sock = _connect_addrs(infos, timeout)
        sock.settimeout(READ_TIMEOUT)
        try:
            if parts.scheme == "https":
                context = ssl.create_default_context()
                sock = context.wrap_socket(sock, server_hostname=host)
            fp = sock.makefile("rb")
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            if any(ch in path for ch in "\r\n\x00"):
                raise PolicyError(400, "Bad URL")
            lines = [
                f"{current_method} {path} HTTP/1.1",
                f"Host: {format_host(host, port, parts.scheme)}",
            ]
            for key, value in request_headers.items():
                if any(ch in key + value for ch in "\r\n\x00"):
                    continue
                lines.append(f"{key}: {value}")
            if not any(key.lower() == "connection" for key in request_headers):
                lines.append("Connection: close")
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii", "strict"))
            status, reason, resp_headers = _read_response_head(fp)
            if status in (301, 302, 303, 307, 308):
                location = header_value(resp_headers, "location")
                fp.close()
                sock.close()
                if not location:
                    raise UpstreamError("redirect without location")
                next_url = urllib.parse.urljoin(current, location.strip())
                next_host = urllib.parse.urlsplit(next_url).hostname
                if next_host and host and next_host.lower() != host.lower():
                    request_headers = {
                        key: value
                        for key, value in request_headers.items()
                        if key.lower() != "authorization"
                    }
                if status == 303:
                    current_method = "GET"
                current = next_url
                continue
            return UpstreamResponse(status, reason, resp_headers, fp, sock, current_method == "HEAD")
        except Exception:
            try:
                sock.close()
            except Exception:
                pass
            raise
    raise UpstreamError("too many redirects")


def map_pypi_url(
    url: str,
    *,
    page_url: str,
    upstream_hosts: Iterable[str],
    relay_base: str,
) -> Optional[str]:
    if not url or url.startswith(("#", "mailto:", "javascript:")):
        return None
    joined = url
    if joined.startswith("//"):
        joined = "https:" + joined
    absolute = urllib.parse.urljoin(page_url, joined)
    parts = urllib.parse.urlsplit(absolute)
    host = (parts.hostname or "").lower()
    if not host:
        return None
    known = {item.lower() for item in upstream_hosts}
    known.update(PYPI_FILE_HOSTS)
    if host not in known:
        return None
    path = parts.path or ""
    query = f"?{parts.query}" if parts.query else ""
    frag = f"#{parts.fragment}" if parts.fragment else ""
    pkg_at = path.find("/packages/")
    if pkg_at != -1:
        return f"{relay_base}/pypi{path[pkg_at:]}{query}{frag}"
    if path.startswith("/simple/"):
        return f"{relay_base}/pypi{path[len('/simple'):]}{query}{frag}"
    return None


def rewrite_pypi_html(body: bytes, *, page_url: str, upstream_hosts: Iterable[str], relay_base: str) -> bytes:
    text = body.decode("utf-8", errors="replace")

    def replace(match: "re.Match[str]") -> str:
        quote = match.group(1)
        href = html.unescape(match.group(2))
        mapped = map_pypi_url(href, page_url=page_url, upstream_hosts=upstream_hosts, relay_base=relay_base)
        if not mapped:
            return match.group(0)
        return f"href={quote}{mapped}{quote}"

    return _HREF_RE.sub(replace, text).encode("utf-8")


def rewrite_pypi_json(body: bytes, *, page_url: str, upstream_hosts: Iterable[str], relay_base: str) -> bytes:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return body
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list):
        return body
    for item in files:
        if isinstance(item, dict) and isinstance(item.get("url"), str):
            mapped = map_pypi_url(
                item["url"], page_url=page_url, upstream_hosts=upstream_hosts, relay_base=relay_base
            )
            if mapped:
                item["url"] = mapped
    return json.dumps(payload).encode("utf-8")


def rewrite_npm_json(body: bytes, *, relay_base: str) -> bytes:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return body

    def map_tarball(url: str) -> Optional[str]:
        if not url.startswith(("http://", "https://")):
            return None
        parts = urllib.parse.urlsplit(url)
        if not parts.path or parts.path.startswith("/npm/"):
            return None
        query = f"?{parts.query}" if parts.query else ""
        return f"{relay_base}/npm{parts.path}{query}"

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in list(node.items()):
                if key == "tarball" and isinstance(value, str):
                    mapped = map_tarball(value)
                    if mapped:
                        node[key] = mapped
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return json.dumps(payload).encode("utf-8")


def rewrite_www_authenticate(value: str, *, relay_base: str, secret: bytes) -> str:
    def replace(match: "re.Match[str]") -> str:
        realm = match.group(1)
        if not realm.startswith(("http://", "https://")):
            return match.group(0)
        sealed = seal_url(realm, secret)
        return f'realm="{relay_base}/_classrelay/docker-token/{sealed}"'

    return _REALM_RE.sub(replace, value)


def render_setup_scripts(origin: str, ip: str) -> Dict[str, str]:
    """Student setup/restore scripts. Registry URLs are plain HTTP, so npm strict-ssl is left alone."""
    target = origin.rstrip("/")
    pip_index = target + "/pypi/"
    npm_registry = target + "/npm/"
    q_target = sh_quote(target)
    q_ip = sh_quote(ip)
    q_pip = sh_quote(pip_index)
    q_npm = sh_quote(npm_registry)
    ps_target = ps_quote(target)
    ps_ip = ps_quote(ip)
    ps_pip = ps_quote(pip_index)
    ps_npm = ps_quote(npm_registry)
    block = "\n".join(
        [
            _MARK_START,
            f"export HTTP_PROXY={q_target}",
            f"export HTTPS_PROXY={q_target}",
            f"export http_proxy={q_target}",
            f"export https_proxy={q_target}",
            _MARK_END,
        ]
    )
    remove_fn = f"""remove_classrelay_block() {{
  file="$1"
  [ -f "$file" ] || return 0
  tmp=$(mktemp)
  awk '
    $0 == "{_MARK_START}" {{ skip = 1; next }}
    $0 == "{_MARK_END}" {{ skip = 0; next }}
    skip != 1 {{ print }}
  ' "$file" > "$tmp" && mv "$tmp" "$file"
}}"""
    bash = f"""#!/usr/bin/env bash
# ClassRelay student setup.
# Served over plain HTTP. Download and read it before you run it.
# curl|bash cannot export variables into the shell that launched it.
{remove_fn}
append_classrelay_block() {{
  file="$1"
  touch "$file"
  remove_classrelay_block "$file"
  cat >> "$file" <<'EOF'
{block}
EOF
}}
append_classrelay_block "$HOME/.bashrc"
append_classrelay_block "$HOME/.zshrc"
pip config set global.index-url {q_pip} || true
pip config set global.trusted-host {q_ip} || true
npm config set registry {q_npm} || true
npm config delete strict-ssl >/dev/null 2>&1 || true
git config --global http.proxy {q_target} || true
git config --global https.proxy {q_target} || true
sourced=0
if [ -n "${{BASH_SOURCE-}}" ] && [ "${{BASH_SOURCE[0]}}" != "$0" ]; then
  sourced=1
fi
if [ -n "${{ZSH_EVAL_CONTEXT-}}" ]; then
  case $ZSH_EVAL_CONTEXT in *:file) sourced=1 ;; esac
fi
if [ "$sourced" -eq 1 ]; then
  export HTTP_PROXY={q_target}
  export HTTPS_PROXY={q_target}
  export http_proxy={q_target}
  export https_proxy={q_target}
  echo ">>> Applied proxy variables to the current shell."
else
  echo ">>> Saved proxy variables to ~/.bashrc and ~/.zshrc."
  echo ">>> curl|bash cannot change the current shell. Open a new terminal, or run: source ~/.bashrc"
fi
echo ">>> [SUCCESS] Student configuration complete."
"""
    unbash = f"""#!/usr/bin/env bash
# ClassRelay student restore.
{remove_fn}
remove_classrelay_block "$HOME/.bashrc"
remove_classrelay_block "$HOME/.zshrc"
pip config unset global.index-url || true
pip config unset global.trusted-host || true
npm config delete registry || true
npm config delete strict-ssl || true
git config --global --unset http.proxy || true
git config --global --unset https.proxy || true
sourced=0
if [ -n "${{BASH_SOURCE-}}" ] && [ "${{BASH_SOURCE[0]}}" != "$0" ]; then
  sourced=1
fi
if [ "$sourced" -eq 1 ]; then
  unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
fi
echo ">>> Open a new terminal so exported proxy variables are cleared."
echo ">>> [SUCCESS] Defaults restored."
"""
    windows = f"""# ClassRelay student setup for Windows.
# Served over plain HTTP. Download and read it before you run it.
$target = {ps_target}
$ip = {ps_ip}
Write-Host ">>> Configuring student workstation for $target" -ForegroundColor Cyan
pip config set global.index-url {ps_pip}
pip config set global.trusted-host {ps_ip}
npm config set registry {ps_npm}
npm config delete strict-ssl
[System.Environment]::SetEnvironmentVariable('HTTP_PROXY', $target, 'User')
[System.Environment]::SetEnvironmentVariable('HTTPS_PROXY', $target, 'User')
$env:HTTP_PROXY = $target
$env:HTTPS_PROXY = $target
git config --global http.proxy $target
git config --global https.proxy $target
Write-Host ">>> [SUCCESS] Student configuration complete. Open a new terminal so proxy variables apply." -ForegroundColor Green
"""
    unwindows = """# ClassRelay student restore for Windows.
Write-Host ">>> Restoring student workstation defaults..." -ForegroundColor Yellow
pip config unset global.index-url
pip config unset global.trusted-host
npm config delete registry
npm config delete strict-ssl
[System.Environment]::SetEnvironmentVariable('HTTP_PROXY', $null, 'User')
[System.Environment]::SetEnvironmentVariable('HTTPS_PROXY', $null, 'User')
$env:HTTP_PROXY = ''
$env:HTTPS_PROXY = ''
git config --global --unset http.proxy
git config --global --unset https.proxy
Write-Host ">>> [SUCCESS] Defaults restored. Open a new terminal so proxy variables are cleared." -ForegroundColor Green
"""
    return {"bash": bash, "unbash": unbash, "windows": windows, "unwindows": unwindows}


class CacheEntry:
    def __init__(self, status: int, headers: List[Tuple[str, str]], path: str, size: int, kind: str):
        self.status = status
        self.headers = headers
        self.path = path
        self.size = size
        self.kind = kind


class CacheManager:
    """Disk cache with TTL, a size cap, atomic publish, and singleflight."""

    def __init__(self, cache_dir: str, max_bytes: int, index_ttl: int, asset_ttl: int):
        self.cache_dir = cache_dir
        self.data_dir = os.path.join(cache_dir, "data")
        self.meta_dir = os.path.join(cache_dir, "meta")
        self.max_bytes = max_bytes
        self.index_ttl = index_ttl
        self.asset_ttl = asset_ttl
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.meta_dir, exist_ok=True)
        self.stats = {
            "hits": 0,
            "misses": 0,
            "cached_bytes": 0,
            "start_time": time.time(),
        }
        self.lock = threading.Lock()
        self._flight_lock = threading.Lock()
        self._flight: Dict[str, threading.Event] = {}
        self.index: Dict[str, Dict[str, object]] = {}
        self._load_existing()

    def _load_existing(self) -> None:
        referenced = set()
        total = 0
        try:
            metas = list(os.scandir(self.meta_dir))
        except OSError:
            metas = []
        for entry in metas:
            if not entry.is_file() or not entry.name.endswith(".json"):
                continue
            key = entry.name[: -len(".json")]
            try:
                with open(entry.path, "r", encoding="utf-8") as handle:
                    meta = json.load(handle)
                filename = meta["file"]
                size = int(meta["size"])
                path = os.path.join(self.data_dir, filename)
                if os.path.getsize(path) != size:
                    continue
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            self.index[key] = {
                "file": filename,
                "size": size,
                "timestamp": float(meta.get("timestamp", 0)),
            }
            referenced.add(filename)
            total += size
        self.stats["cached_bytes"] = total
        try:
            for entry in os.scandir(self.data_dir):
                if entry.is_file() and entry.name not in referenced:
                    try:
                        os.remove(entry.path)
                    except OSError:
                        pass
        except OSError:
            pass

    def note_miss(self) -> None:
        with self.lock:
            self.stats["misses"] += 1

    def lookup(self, key: str) -> Optional[CacheEntry]:
        meta_path = os.path.join(self.meta_dir, key + ".json")
        try:
            with open(meta_path, "r", encoding="utf-8") as handle:
                meta = json.load(handle)
            filename = meta["file"]
            size = int(meta["size"])
            kind = meta.get("kind", "asset")
            timestamp = float(meta.get("timestamp", 0))
            path = os.path.join(self.data_dir, filename)
            if os.path.getsize(path) != size:
                return None
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        ttl = self.index_ttl if kind == "index" else self.asset_ttl
        if ttl >= 0 and (time.time() - timestamp) > ttl:
            return None
        headers = [(str(item[0]), str(item[1])) for item in meta.get("headers", []) if len(item) == 2]
        with self.lock:
            self.stats["hits"] += 1
        return CacheEntry(int(meta.get("status", 200)), headers, path, size, str(kind))

    def lead(self, key: str) -> bool:
        with self._flight_lock:
            if key in self._flight:
                return False
            self._flight[key] = threading.Event()
            return True

    def follow(self, key: str) -> None:
        with self._flight_lock:
            event = self._flight.get(key)
        if event is not None:
            event.wait()

    def finish(self, key: str) -> None:
        with self._flight_lock:
            event = self._flight.pop(key, None)
        if event is not None:
            event.set()

    def new_temp(self) -> str:
        fd, path = tempfile.mkstemp(prefix=".part-", dir=self.data_dir)
        os.close(fd)
        return path

    def abort(self, path: Optional[str]) -> None:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass

    def commit(
        self,
        key: str,
        tmp_path: str,
        *,
        url: str,
        headers: Sequence[Tuple[str, str]],
        status: int,
        kind: str,
    ) -> bool:
        try:
            size = os.path.getsize(tmp_path)
        except OSError:
            return False
        if self.max_bytes >= 0 and size > self.max_bytes:
            self.abort(tmp_path)
            return False
        filename = f"{key}-{secrets.token_hex(8)}"
        final = os.path.join(self.data_dir, filename)
        try:
            with open(tmp_path, "rb") as handle:
                os.fsync(handle.fileno())
            os.replace(tmp_path, final)
        except OSError as exc:
            LOG.warning("cache write failed: %s", exc)
            self.abort(tmp_path)
            return False
        stored_headers = [
            [key_name, value]
            for key_name, value in headers
            if key_name.lower() not in HOP_BY_HOP
            and key_name.lower() not in {"content-length", "set-cookie", "content-encoding"}
            and "\r" not in value
            and "\n" not in value
        ]
        meta = {
            "url": url,
            "file": filename,
            "headers": stored_headers,
            "status": status,
            "size": size,
            "kind": kind,
            "timestamp": time.time(),
        }
        try:
            _atomic_write_json(os.path.join(self.meta_dir, key + ".json"), meta)
        except OSError as exc:
            LOG.warning("cache meta write failed: %s", exc)
            try:
                os.remove(final)
            except OSError:
                pass
            return False
        old_files: List[str] = []
        with self.lock:
            prev = self.index.get(key)
            self.index[key] = {"file": filename, "size": size, "timestamp": meta["timestamp"]}
            self.stats["cached_bytes"] += size
            if prev:
                self.stats["cached_bytes"] -= int(prev["size"])
                old_files.append(str(prev["file"]))
            for victim in self._victims_locked(protect=filename):
                old_files.append(str(victim["file"]))
        for name in old_files:
            self._unlink(name)
        return True

    def _victims_locked(self, protect: str) -> List[Dict[str, object]]:
        victims = []
        if self.max_bytes < 0:
            return victims
        while self.stats["cached_bytes"] > self.max_bytes and len(self.index) > 1:
            oldest_key = None
            oldest = None
            for key, item in self.index.items():
                if item["file"] == protect:
                    continue
                if oldest is None or float(item["timestamp"]) < float(oldest["timestamp"]):
                    oldest_key = key
                    oldest = item
            if oldest is None:
                break
            victims.append({"key": oldest_key, "file": oldest["file"], "size": int(oldest["size"])})
            self.stats["cached_bytes"] -= int(oldest["size"])
            self.index.pop(oldest_key, None)
        return victims

    def _unlink(self, filename: str) -> None:
        try:
            os.remove(os.path.join(self.data_dir, filename))
        except OSError:
            pass


def _atomic_write_json(path: str, payload: dict) -> None:
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".meta-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        for _attempt in range(8):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                time.sleep(0.05)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _cache_control_blocks(value: str) -> bool:
    lowered = value.lower()
    return any(flag in lowered for flag in ("private", "no-store", "no-cache"))


def response_cacheable(status: int, headers: Sequence[Tuple[str, str]], request_ok: bool) -> bool:
    if not request_ok or status != 200:
        return False
    if header_values(headers, "set-cookie"):
        return False
    cache_control = header_value(headers, "cache-control") or ""
    if _cache_control_blocks(cache_control):
        return False
    vary = (header_value(headers, "vary") or "").lower()
    if vary.strip() == "*" or "cookie" in vary:
        return False
    return True


def classify_kind(path: str, content_type: str) -> str:
    lower_path = path.lower()
    if lower_path.endswith(ASSET_SUFFIXES) or "/blobs/" in lower_path:
        return "asset"
    content = content_type.lower()
    if "json" in content or "html" in content or content.startswith("text/"):
        return "index"
    return "asset"


def rewrite_mode(mirror: str, path: str, content_type: str) -> Optional[str]:
    lower_path = path.lower().split("?", 1)[0]
    if lower_path.endswith(ASSET_SUFFIXES):
        return None
    content = content_type.lower()
    if mirror == "pypi" and "/packages/" not in lower_path and ("html" in content or "json" in content):
        return "pypi-json" if "json" in content else "pypi-html"
    if mirror == "npm" and "json" in content:
        return "npm-json"
    return None


class ClassRelayHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"ClassRelay/{VERSION}"

    def log_message(self, fmt: str, *args) -> None:
        try:
            message = fmt % args if args else str(fmt)
        except Exception:
            message = str(fmt)
        LOG.info("%s %s", self.client_address[0], redact_text(message))

    def handle_one_request(self) -> None:
        self._started = False
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            self.close_connection = True
        except Exception:
            LOG.exception("request failed")
            self.close_connection = True
            if not getattr(self, "_started", False):
                try:
                    self.send_error(500, "Internal Server Error")
                except Exception:
                    pass

    def send_response(self, code: int, message: Optional[str] = None) -> None:
        self._started = True
        super().send_response(code, message)

    @property
    def cache(self) -> CacheManager:
        return self.server.cache

    def _check_auth(self, proxy: bool) -> bool:
        auth = self.server.auth
        if not auth:
            return True
        if proxy:
            presented = (
                self.headers.get("Proxy-Authorization", ""),
                self.headers.get("Authorization", ""),
            )
        else:
            presented = (
                self.headers.get("Authorization", ""),
                self.headers.get("Proxy-Authorization", ""),
            )
        return any(basic_matches(value, auth[0], auth[1]) for value in presented)

    def _challenge(self, proxy: bool) -> None:
        if proxy:
            self.send_response(407, "Proxy Authentication Required")
            self.send_header("Proxy-Authenticate", 'Basic realm="ClassRelay"')
        else:
            self.send_response(401, "Unauthorized")
            self.send_header("WWW-Authenticate", 'Basic realm="ClassRelay"')
        body = b"Authentication required. Use http://user:pass@host:port\n"
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _listen_endpoint(self) -> Tuple[str, int]:
        sockname = self.connection.getsockname()
        ip = str(sockname[0])
        port = int(sockname[1])
        if ip.startswith("::ffff:"):
            ip = ip.split("::ffff:", 1)[1]
        if ip in ("0.0.0.0", "::"):
            ip = (self.server.local_ips or ["127.0.0.1"])[0]
        return ip, port

    def _origin(self, with_auth: bool) -> Tuple[str, str, int]:
        ip, port = self._listen_endpoint()
        host = f"[{ip}]" if ":" in ip else ip
        auth = self.server.auth
        if with_auth and auth:
            user = urllib.parse.quote(auth[0], safe="")
            password = urllib.parse.quote(auth[1], safe="")
            return f"http://{user}:{password}@{host}:{port}", ip, port
        return f"http://{host}:{port}", ip, port

    def do_CONNECT(self) -> None:
        self.close_connection = True
        if not self._check_auth(proxy=True):
            self._challenge(proxy=True)
            return
        try:
            host, port = parse_connect_target(self.path)
        except ValueError:
            self.send_error(400, "Bad CONNECT target")
            return
        if port not in self.server.connect_ports:
            self.send_error(403, "Port not allowed")
            return
        if not host_permitted(host, self.server.allow_hosts):
            self.send_error(403, "Host not in allowlist")
            return
        remote = None
        established = False
        try:
            private_ok = bool(self.server.allow_private)
            infos = resolve_destination(host, port, private_ok=private_ok)
            remote = _connect_addrs(infos, CONNECT_TIMEOUT)
            self.send_response(200, "Connection Established")
            self.end_headers()
            established = True
            try:
                self.wfile.flush()
            except Exception:
                return
            self._tunnel(self.connection, remote)
        except PolicyError as exc:
            if not established:
                self.send_error(exc.status, exc.reason)
        except Exception as exc:
            LOG.info("CONNECT failed: %s", redact_text(str(exc)))
            if not established:
                try:
                    self.send_error(502, "Bad Gateway")
                except Exception:
                    pass
        finally:
            if remote is not None:
                try:
                    remote.close()
                except Exception:
                    pass

    def _tunnel(self, client: socket.socket, remote: socket.socket) -> None:
        idle = self.server.tunnel_idle
        if idle and idle > 0:
            client.settimeout(idle)
            remote.settimeout(idle)
        else:
            client.settimeout(None)
            remote.settimeout(None)

        def pump(src: socket.socket, dst: socket.socket) -> None:
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except Exception:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except Exception:
                    pass

        left = threading.Thread(target=pump, args=(client, remote), daemon=True)
        right = threading.Thread(target=pump, args=(remote, client), daemon=True)
        left.start()
        right.start()
        left.join()
        right.join()

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        proxy = self.path.startswith("http://") or self.path.startswith("https://")
        if not self._check_auth(proxy=proxy):
            self._challenge(proxy=proxy)
            return
        if proxy:
            self._handle_forward()
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path or "/"
        if "\\" in path or path.startswith("//") or "\x00" in path:
            self.send_error(400, "Bad path")
            return
        if path in ("", "/", "/index.html"):
            self._serve_dashboard()
            return
        if path == "/api/status":
            self._serve_api_status()
            return
        if path.startswith("/setup/"):
            self._serve_setup_script(path)
            return
        if path.startswith("/_classrelay/docker-token/"):
            self._serve_docker_token(path, parsed.query)
            return
        try:
            upstream, mirror, trusted = self._mirror_target(path, parsed.query)
        except KeyError:
            self.send_error(404, "Endpoint not found on ClassRelay")
            return
        except ValueError:
            self.send_error(400, "Bad mirror path")
            return
        if upstream is None:
            self.send_error(404, "Endpoint not found on ClassRelay")
            return
        self._proxy_mirror(upstream, mirror, trusted)

    def _mirror_target(self, path: str, query: str):
        upstreams = self.server.upstreams
        if path == "/pypi":
            path = "/pypi/"
        if path == "/npm":
            path = "/npm/"
        if path == "/pypi" or path.startswith("/pypi/"):
            rest = path[len("/pypi") :] or "/"
            base_url = upstreams["pypi"]
            if rest.startswith("/packages/"):
                base_url = simple_root(base_url)
            elif rest.startswith("/simple/"):
                base_url = simple_root(base_url)
            upstream = join_upstream(base_url, rest, query)
            return upstream, "pypi", endpoint_of(base_url)
        if path.startswith("/npm/"):
            base_url = upstreams["npm"]
            upstream = join_upstream(base_url, path[len("/npm") :], query)
            return upstream, "npm", endpoint_of(base_url)
        if path == "/v2" or path.startswith("/v2/"):
            base_url = origin_of(upstreams["docker"])
            upstream = join_upstream(base_url, path if path != "/v2" else "/v2/", query)
            return upstream, "docker", endpoint_of(upstreams["docker"])
        if path.startswith("/docker/"):
            base_url = origin_of(upstreams["docker"])
            upstream = join_upstream(base_url, path[len("/docker") :], query)
            return upstream, "docker", endpoint_of(upstreams["docker"])
        if path.startswith("/platformio/"):
            base_url = upstreams["platformio"]
            upstream = join_upstream(base_url, path[len("/platformio") :], query)
            return upstream, "platformio", endpoint_of(base_url)
        if path.startswith("/espressif/"):
            base_url = upstreams["espressif"]
            upstream = join_upstream(base_url, path[len("/espressif") :], query)
            return upstream, "espressif", endpoint_of(base_url)
        return None, "", None

    def _serve_dashboard(self) -> None:
        origin, _ip, port = self._origin(with_auth=True)
        _bare, bare_ip, _port = self._origin(with_auth=False)
        scripts = render_setup_scripts(origin, bare_ip)
        hashes = {name: hashlib.sha256(body.encode("utf-8")).hexdigest() for name, body in scripts.items()}
        display = html.escape(origin, quote=True)
        safe_ip = html.escape(bare_ip, quote=True)
        others = []
        for candidate in self.server.local_ips or []:
            if candidate != bare_ip:
                others.append(html.escape(candidate, quote=True))
        other_html = "".join(f'<span class="ip-item">http://{item}:{port}</span>' for item in others)
        auth_note = ""
        if self.server.auth:
            auth_note = "<p>已开启 HTTP Basic 认证。下面的命令含密码，只应在已登录的控制台中查看。</p>"
        html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ClassRelay - 教师机局域网依赖中继代理</title>
    <style>
        :root {{ --bg: #0f172a; --card: #1e293b; --text: #f8fafc; --accent: #38bdf8; --border: #334155; --code-bg: #090d16; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 24px; }}
        .container {{ max-width: 960px; margin: 0 auto; }}
        header {{ border-bottom: 1px solid var(--border); padding-bottom: 16px; margin-bottom: 24px; display: flex; justify-content: space-between; align-items: center; }}
        h1 {{ margin: 0; font-size: 24px; color: var(--accent); }}
        .badge {{ background: #0284c7; color: white; padding: 4px 10px; border-radius: 9999px; font-size: 13px; font-weight: bold; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-bottom: 24px; }}
        .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 18px; }}
        .card h3 {{ margin-top: 0; font-size: 16px; border-bottom: 1px solid var(--border); padding-bottom: 8px; }}
        pre {{ background: var(--code-bg); padding: 12px; border-radius: 6px; overflow-x: auto; font-size: 13px; border: 1px solid var(--border); }}
        code {{ color: #7dd3fc; }}
        .stat-val {{ font-size: 28px; font-weight: bold; color: var(--accent); margin: 8px 0; }}
        .ip-list {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }}
        .ip-item {{ background: #0369a1; padding: 3px 8px; border-radius: 4px; font-size: 13px; }}
        .warn {{ color: #fcd34d; font-size: 13px; }}
    </style>
</head>
<body>
<div class="container">
    <header>
        <div>
            <h1>ClassRelay 依赖中继服务器</h1>
            <p style="margin: 4px 0 0 0; color: #94a3b8; font-size: 14px;">在无外网学生机环境中，利用教师机中继与缓存 pip / npm / 嵌入式开发依赖</p>
        </div>
        <span class="badge">v{VERSION} 运行中</span>
    </header>
    <div class="grid">
        <div class="card">
            <h3>本次连接地址</h3>
            <p style="font-size: 13px; color: #94a3b8; margin: 0;">学生机这次连到的教师机地址（优先使用它，而不是猜测网卡）：</p>
            <div class="ip-list"><span class="ip-item">{display}</span></div>
            <p style="font-size: 13px; color: #94a3b8;">地址 {safe_ip} ，端口 {port}</p>
            {f'<p style="font-size: 13px; color: #94a3b8;">本机其他网卡（仅供参考）：</p><div class="ip-list">{other_html}</div>' if other_html else ''}
        </div>
        <div class="card">
            <h3>缓存与命中统计</h3>
            <div class="stat-val" id="hit-rate">-</div>
            <div style="font-size: 13px; color: #94a3b8;" id="stat-detail">加载统计中...</div>
        </div>
    </div>
    <div class="card" style="margin-bottom: 20px;">
        <h3>一键配置前请先看</h3>
        <p class="warn">脚本通过明文 HTTP 下发。同网段的人可以伪造教师机。请先把脚本下载下来核对 SHA256，再执行，不要习惯性直接 <code>curl | bash</code> / <code>irm | iex</code>。</p>
        {auth_note}
        <p style="margin-bottom: 4px; font-size: 13px; font-weight: bold; color: #38bdf8;">Windows</p>
        <pre><code>irm {display}/setup/windows -OutFile classrelay-setup.ps1
# SHA256 {hashes['windows']}
# 核对后再执行:
# powershell -File .\\classrelay-setup.ps1</code></pre>
        <p style="margin-bottom: 4px; font-size: 13px; font-weight: bold; color: #38bdf8;">Linux / macOS</p>
        <pre><code>curl -fsSL {display}/setup/bash -o classrelay-setup.sh
# SHA256 {hashes['bash']}
# less classrelay-setup.sh
# bash classrelay-setup.sh
# 当前终端若要立刻生效: source ~/.bashrc</code></pre>
        <p style="margin-bottom: 4px; font-size: 13px; font-weight: bold; color: #38bdf8;">课后还原</p>
        <pre><code># Windows  SHA256 {hashes['unwindows']}
irm {display}/setup/unwindows -OutFile classrelay-restore.ps1
# Linux/macOS  SHA256 {hashes['unbash']}
curl -fsSL {display}/setup/unbash -o classrelay-restore.sh</code></pre>
    </div>
    <div class="grid">
        <div class="card">
            <h3>Python / pip</h3>
            <pre><code>pip config set global.index-url {display}/pypi/
pip config set global.trusted-host {safe_ip}</code></pre>
        </div>
        <div class="card">
            <h3>Node.js / npm</h3>
            <p style="font-size: 13px; color: #94a3b8;">镜像本身是 HTTP，不需要关闭 strict-ssl。</p>
            <pre><code>npm config set registry {display}/npm/</code></pre>
        </div>
        <div class="card">
            <h3>Docker（实验性）</h3>
            <p style="font-size: 13px; color: #94a3b8;">支持 Registry v2 的 HEAD/GET、Accept 协商和 token realm 回写。不含 push、Range 断点续传。</p>
            <pre><code>{{
  "registry-mirrors": ["{display}"],
  "insecure-registries": ["{safe_ip}:{port}"]
}}</code></pre>
        </div>
        <div class="card">
            <h3>PlatformIO / Git 代理</h3>
            <pre><code>export HTTP_PROXY={display}
export HTTPS_PROXY={display}
git config --global http.proxy {display}
git config --global https.proxy {display}</code></pre>
        </div>
    </div>
</div>
<script>
    async function updateStats() {{
        try {{
            const res = await fetch('/api/status');
            const data = await res.json();
            const total = data.hits + data.misses;
            const rate = total > 0 ? ((data.hits / total) * 100).toFixed(1) + '%' : '0.0%';
            document.getElementById('hit-rate').textContent = '缓存命中率: ' + rate;
            const mb = (data.cached_bytes / 1024 / 1024).toFixed(2);
            document.getElementById('stat-detail').textContent = '命中: ' + data.hits + ' 次 | 请求: ' + total + ' 次 | 磁盘缓存: ' + mb + ' MB';
        }} catch (e) {{}}
    }}
    updateStats();
    setInterval(updateStats, 3000);
</script>
</body>
</html>"""
        self._send_bytes(200, "OK", [("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")], html_doc.encode("utf-8"), None)

    def _serve_api_status(self) -> None:
        with self.cache.lock:
            payload = dict(self.cache.stats)
        body = json.dumps(payload).encode("utf-8")
        self._send_bytes(200, "OK", [("Content-Type", "application/json"), ("Cache-Control", "no-store")], body, None)

    def _serve_setup_script(self, path: str) -> None:
        name = path[len("/setup/") :].strip("/")
        if name not in {"windows", "unwindows", "bash", "unbash"}:
            self.send_error(404, "Endpoint not found on ClassRelay")
            return
        origin, _ip, _port = self._origin(with_auth=True)
        _bare, bare_ip, _bare_port = self._origin(with_auth=False)
        script = render_setup_scripts(origin, bare_ip)[name]
        body = script.encode("utf-8")
        digest = hashlib.sha256(body).hexdigest()
        self._send_bytes(
            200,
            "OK",
            [
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Cache-Control", "no-store"),
                ("X-ClassRelay-SHA256", digest),
            ],
            body,
            None,
        )

    def _serve_docker_token(self, path: str, query: str) -> None:
        token = path[len("/_classrelay/docker-token/") :]
        url = unseal_url(token, self.server.secret)
        if not url:
            self.send_error(400, "Bad token endpoint")
            return
        safe_query = sanitize_query(query)
        if safe_query:
            parts = urllib.parse.urlsplit(url)
            merged = parts.query + ("&" if parts.query and safe_query else "") + safe_query
            url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, merged, ""))
        docker_ep = endpoint_of(self.server.upstreams["docker"])
        trusted = (docker_ep,) if docker_ep else ()
        self._proxy_direct(url, trusted_endpoints=trusted, mirror="docker")

    def _handle_forward(self) -> None:
        try:
            parts = urllib.parse.urlsplit(self.path)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError("bad url")
        except ValueError:
            self.send_error(400, "Bad proxy URL")
            return
        headers = self._filtered_client_headers(drop_authorization=False)
        self._proxy_direct(
            strip_userinfo(self.path),
            trusted_endpoints=(),
            mirror="forward",
            request_headers=headers,
            allow_private=bool(self.server.allow_private),
            check_allow_hosts=True,
            allowed_ports=self.server.proxy_ports,
            cache_label="BYPASS",
        )

    def _filtered_client_headers(self, *, drop_authorization: bool) -> Dict[str, str]:
        hop = set(HOP_BY_HOP)
        connection = self.headers.get("Connection", "")
        for token in connection.split(","):
            name = token.strip().lower()
            if name:
                hop.add(name)
        out: Dict[str, str] = {}
        for key in self.headers:
            lowered = key.lower()
            if lowered in hop or lowered in {"host", "cookie"} and drop_authorization:
                continue
            if lowered in hop or lowered == "host":
                continue
            if drop_authorization and lowered in {"authorization", "cookie"}:
                continue
            if lowered == "proxy-authorization":
                continue
            value = self.headers.get(key, "")
            if any(ch in value for ch in "\r\n\x00"):
                continue
            out[key] = value
        out["Accept-Encoding"] = "identity"
        out["Connection"] = "close"
        out.setdefault("User-Agent", f"ClassRelay/{VERSION}")
        return out

    def _mirror_headers(self, mirror: str, upstream_url: str) -> Dict[str, str]:
        path = urllib.parse.urlsplit(upstream_url).path.lower()
        headers = {
            "User-Agent": f"ClassRelay/{VERSION}",
            "Accept-Encoding": "identity",
            "Connection": "close",
            "Accept": "*/*",
        }
        if mirror == "pypi" and "/packages/" not in path:
            headers["Accept"] = "application/vnd.pypi.simple.v1+html, text/html;q=0.9"
        elif mirror == "npm" and not path.endswith(".tgz"):
            headers["Accept"] = "application/vnd.npm.install-v1+json, application/json;q=0.9"
        elif mirror == "docker":
            accept = self.headers.get("Accept")
            if accept and not any(ch in accept for ch in "\r\n"):
                headers["Accept"] = accept
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("bearer ") and not any(ch in auth for ch in "\r\n"):
                headers["Authorization"] = auth
            elif (
                auth.lower().startswith("basic ")
                and not any(ch in auth for ch in "\r\n")
                and not (self.server.auth and basic_matches(auth, self.server.auth[0], self.server.auth[1]))
            ):
                headers["Authorization"] = auth
        range_header = self.headers.get("Range")
        if range_header and not any(ch in range_header for ch in "\r\n"):
            headers["Range"] = range_header
        return headers

    def _proxy_mirror(self, upstream_url: str, mirror: str, trusted: Optional[Tuple[str, int]]) -> None:
        request_headers = self._mirror_headers(mirror, upstream_url)
        has_cookie = bool(self.headers.get("Cookie"))
        has_range = "Range" in request_headers
        request_ok = not has_cookie and not has_range and self.command != "HEAD"
        variant = ""
        upstream_path = urllib.parse.urlsplit(upstream_url).path
        if mirror == "docker" and "/manifests/" in upstream_path:
            variant = request_headers.get("Accept", "")
        auth_fp = ""
        forwarded_auth = request_headers.get("Authorization", "")
        if forwarded_auth:
            auth_fp = hashlib.sha256(forwarded_auth.encode("utf-8")).hexdigest()[:16]
        # Rewritten index HTML/JSON embeds the address the student connected to.
        # A second NIC must not reuse that body. Package bytes do not.
        lower_path = upstream_path.lower()
        base_tag = ""
        if mirror in {"pypi", "npm"} and "/packages/" not in lower_path and not lower_path.endswith(ASSET_SUFFIXES):
            base_tag = self._origin(with_auth=False)[0]
        key = hashlib.sha256(
            f"{upstream_url}\n{variant}\n{auth_fp}\n{base_tag}".encode("utf-8")
        ).hexdigest()
        if request_ok and self._serve_cached(key):
            return
        leader = False
        if request_ok:
            leader = self.cache.lead(key)
            if not leader:
                self.cache.follow(key)
                if self._serve_cached(key):
                    return
                leader = self.cache.lead(key)
                if not leader:
                    self.cache.follow(key)
                    if self._serve_cached(key):
                        return
                    self.send_error(502, "Upstream fetch failed")
                    return
        released = False

        def release() -> None:
            nonlocal released
            if leader and not released:
                self.cache.finish(key)
                released = True

        try:
            self.cache.note_miss()
            trusted_eps = (trusted,) if trusted else ()
            with fetch(
                upstream_url,
                method="HEAD" if self.command == "HEAD" else "GET",
                headers=request_headers,
                allow_private=bool(self.server.allow_private),
                trusted_endpoints=trusted_eps,
            ) as resp:
                self._deliver_mirror(
                    resp,
                    upstream_url=upstream_url,
                    mirror=mirror,
                    key=key,
                    request_ok=request_ok,
                    release=release,
                )
        except PolicyError as exc:
            release()
            if not getattr(self, "_started", False):
                self.send_error(exc.status, exc.reason)
        except Exception as exc:
            release()
            LOG.info("mirror error: %s", redact_text(str(exc)))
            if not getattr(self, "_started", False):
                self.send_error(502, "Bad Gateway")
        finally:
            release()

    def _deliver_mirror(
        self,
        resp: UpstreamResponse,
        *,
        upstream_url: str,
        mirror: str,
        key: str,
        request_ok: bool,
        release: Callable[[], None],
    ) -> None:
        headers = list(resp.headers)
        if mirror == "docker":
            relay_base = self._origin(with_auth=False)[0]
            headers = [
                (
                    name,
                    rewrite_www_authenticate(value, relay_base=relay_base, secret=self.server.secret)
                    if name.lower() == "www-authenticate"
                    else value,
                )
                for name, value in headers
            ]
        if self.command == "HEAD":
            release()
            self._send_upstream_head(resp.status, resp.reason, headers)
            return
        content_type = header_value(headers, "content-type") or ""
        mode = rewrite_mode(mirror, urllib.parse.urlsplit(upstream_url).path, content_type)
        cacheable = response_cacheable(resp.status, headers, request_ok)
        if not cacheable:
            release()
        if mode and resp.status == 200:
            body = resp.read_all(MAX_INDEX_BYTES)
            relay_base = self._origin(with_auth=False)[0]
            upstream_hosts = {urllib.parse.urlsplit(upstream_url).hostname or ""}
            if mode == "pypi-html":
                body = rewrite_pypi_html(
                    body, page_url=upstream_url, upstream_hosts=upstream_hosts, relay_base=relay_base
                )
            elif mode == "pypi-json":
                body = rewrite_pypi_json(
                    body, page_url=upstream_url, upstream_hosts=upstream_hosts, relay_base=relay_base
                )
            elif mode == "npm-json":
                body = rewrite_npm_json(body, relay_base=relay_base)
            if cacheable:
                self._commit_bytes(key, upstream_url, headers, resp.status, body, content_type)
            self._send_bytes(resp.status, resp.reason, headers, body, "MISS")
            return
        self._stream_upstream(
            resp,
            headers,
            cache_key=key if cacheable else None,
            upstream_url=upstream_url,
            content_type=content_type,
            cache_label="MISS",
        )

    def _proxy_direct(
        self,
        url: str,
        *,
        trusted_endpoints,
        mirror: str,
        request_headers: Optional[Dict[str, str]] = None,
        allow_private: Optional[bool] = None,
        check_allow_hosts: bool = False,
        allowed_ports=None,
        cache_label: str = "BYPASS",
    ) -> None:
        headers = request_headers
        if headers is None and mirror == "docker":
            headers = {
                "User-Agent": f"ClassRelay/{VERSION}",
                "Accept": self.headers.get("Accept", "*/*"),
                "Accept-Encoding": "identity",
                "Connection": "close",
            }
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith(("bearer ", "basic ")) and not any(ch in auth for ch in "\r\n"):
                if not (self.server.auth and basic_matches(auth, self.server.auth[0], self.server.auth[1])):
                    headers["Authorization"] = auth
        try:
            with fetch(
                url,
                method="HEAD" if self.command == "HEAD" else "GET",
                headers=headers,
                allow_private=bool(self.server.allow_private if allow_private is None else allow_private),
                trusted_endpoints=trusted_endpoints,
                allow_hosts=self.server.allow_hosts,
                check_allow_hosts=check_allow_hosts,
                allowed_ports=allowed_ports,
            ) as resp:
                out_headers = list(resp.headers)
                if self.command == "HEAD":
                    self._send_upstream_head(resp.status, resp.reason, out_headers, cache_label=cache_label)
                    return
                self._stream_upstream(
                    resp,
                    out_headers,
                    cache_key=None,
                    upstream_url=url,
                    content_type=header_value(out_headers, "content-type") or "",
                    cache_label=cache_label,
                )
        except PolicyError as exc:
            if not getattr(self, "_started", False):
                self.send_error(exc.status, exc.reason)
        except Exception as exc:
            LOG.info("proxy error: %s", redact_text(str(exc)))
            if not getattr(self, "_started", False):
                self.send_error(502, "Bad Gateway")

    def _commit_bytes(self, key, url, headers, status, body: bytes, content_type: str) -> None:
        tmp = self.cache.new_temp()
        try:
            with open(tmp, "wb") as handle:
                handle.write(body)
            self.cache.commit(
                key,
                tmp,
                url=url,
                headers=self._stored_headers(headers),
                status=status,
                kind=classify_kind(urllib.parse.urlsplit(url).path, content_type),
            )
        except Exception:
            self.cache.abort(tmp)
            raise

    def _stored_headers(self, headers: Sequence[Tuple[str, str]]) -> List[Tuple[str, str]]:
        return [(key, value) for key, value in headers if key.lower() != "set-cookie"]

    def _serve_cached(self, key: str) -> bool:
        entry = self.cache.lookup(key)
        if entry is None:
            return False
        try:
            self.send_response(entry.status, "OK")
            self._write_safe_headers(entry.headers, decoded_gzip=False)
            self.send_header("X-ClassRelay-Cache", "HIT")
            self.send_header("Content-Length", str(entry.size))
            self.end_headers()
            if self.command == "HEAD":
                return True
            with open(entry.path, "rb") as handle:
                while True:
                    chunk = handle.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        return True

    def _send_bytes(self, status: int, reason: str, headers, body: bytes, cache_label: Optional[str]) -> None:
        self.send_response(status, ascii_reason(reason))
        self._write_safe_headers(headers, decoded_gzip=True)
        if cache_label:
            self.send_header("X-ClassRelay-Cache", cache_label)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.close_connection = True

    def _send_upstream_head(self, status: int, reason: str, headers, cache_label: str = "MISS") -> None:
        self.send_response(status, ascii_reason(reason))
        self._write_safe_headers(headers, decoded_gzip=False, keep_length=True)
        self.send_header("X-ClassRelay-Cache", cache_label)
        self.end_headers()

    def _write_safe_headers(self, headers, *, decoded_gzip: bool, keep_length: bool = False) -> None:
        skip = set(HOP_BY_HOP)
        skip.add("content-length")
        if decoded_gzip:
            skip.add("content-encoding")
        connection = header_value(headers, "connection") or ""
        for token in connection.split(","):
            name = token.strip().lower()
            if name:
                skip.add(name)
        if keep_length:
            skip.discard("content-length")
        for key, value in headers:
            if key.lower() in skip:
                continue
            if any(ch in value for ch in "\r\n\x00"):
                continue
            try:
                self.send_header(key, value)
            except UnicodeEncodeError:
                continue

    def _stream_upstream(
        self,
        resp: UpstreamResponse,
        headers,
        *,
        cache_key: Optional[str],
        upstream_url: str,
        content_type: str,
        cache_label: str,
    ) -> None:
        tmp = self.cache.new_temp() if cache_key else None
        tmp_handle = open(tmp, "wb") if tmp else None
        stored = 0
        keep_store = cache_key is not None
        client_ok = True
        finished_body = False
        try:
            self.send_response(resp.status, ascii_reason(resp.reason))
            self._write_safe_headers(headers, decoded_gzip=True)
            self.send_header("X-ClassRelay-Cache", cache_label)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for chunk in resp.iter_body():
                if not chunk:
                    continue
                if keep_store and tmp_handle is not None:
                    if self.cache.max_bytes >= 0 and stored + len(chunk) > self.cache.max_bytes:
                        keep_store = False
                        tmp_handle.close()
                        tmp_handle = None
                        self.cache.abort(tmp)
                        tmp = None
                    else:
                        tmp_handle.write(chunk)
                        stored += len(chunk)
                if client_ok and self.command != "HEAD":
                    try:
                        self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                        self.wfile.write(chunk)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        client_ok = False
                        self.close_connection = True
                        if not keep_store:
                            break
            else:
                finished_body = True
            if client_ok and finished_body and self.command != "HEAD":
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    self.close_connection = True
        finally:
            if tmp_handle is not None:
                try:
                    tmp_handle.close()
                except OSError:
                    pass
            if finished_body and keep_store and tmp and cache_key and resp.status == 200:
                self.cache.commit(
                    cache_key,
                    tmp,
                    url=upstream_url,
                    headers=self._stored_headers(headers),
                    status=resp.status,
                    kind=classify_kind(urllib.parse.urlsplit(upstream_url).path, content_type),
                )
            else:
                self.cache.abort(tmp)


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(
        self,
        server_address,
        request_handler_class,
        cache_dir: str,
        auth: Optional[Tuple[str, str]] = None,
        upstreams: Optional[Dict[str, str]] = None,
        allow_private: bool = False,
        allow_hosts: Sequence[str] = (),
        connect_ports: Optional[Iterable[int]] = None,
        proxy_ports: Optional[Iterable[int]] = None,
        max_cache_bytes: int = DEFAULT_MAX_CACHE_BYTES,
        index_ttl: int = DEFAULT_INDEX_TTL,
        asset_ttl: int = DEFAULT_ASSET_TTL,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        tunnel_idle: int = DEFAULT_TUNNEL_IDLE,
        local_ips: Optional[Sequence[str]] = None,
    ):
        self.auth = auth
        self.upstreams = dict(UPSTREAMS)
        if upstreams:
            self.upstreams.update(upstreams)
        self.allow_private = allow_private
        self.allow_hosts = tuple(allow_hosts)
        self.connect_ports = frozenset(connect_ports or (443,))
        self.proxy_ports = frozenset(proxy_ports or (80, 443))
        self.tunnel_idle = tunnel_idle
        self.max_connections = max(1, int(max_connections))
        self.local_ips = list(local_ips) if local_ips is not None else get_local_ips()
        self.secret = secrets.token_bytes(32)
        self._sem = threading.BoundedSemaphore(self.max_connections)
        super().__init__(server_address, request_handler_class)
        self.cache = CacheManager(cache_dir, max_cache_bytes, index_ttl, asset_ttl)

    def process_request(self, request, client_address) -> None:
        if not self._sem.acquire(blocking=False):
            try:
                body = b"Too many connections"
                message = (
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Connection: close\r\n"
                    b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
                    b"\r\n" + body
                )
                request.sendall(message)
            except Exception:
                pass
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._sem.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._sem.release()


def _split_multi(values: Optional[Sequence[str]]) -> Tuple[str, ...]:
    if not values:
        return ()
    out = []
    for value in values:
        for part in value.split(","):
            item = part.strip().lower().lstrip(".")
            if item:
                out.append(item)
    return tuple(out)


def _validate_auth(raw: str) -> Tuple[str, str]:
    if ":" not in raw:
        raise SystemExit("--auth 需要 user:password 形式")
    user, password = raw.split(":", 1)
    if not user or not password:
        raise SystemExit("--auth 的用户名和密码都不能为空")
    for label, value in (("用户名", user), ("密码", password)):
        if any(ch.isspace() or ord(ch) < 32 for ch in value):
            raise SystemExit(f"--auth {label}不能包含空白或控制字符")
    return user, password


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ClassRelay - 局域网机房依赖中继与缓存分发服务")
    parser.add_argument("--port", "-p", type=int, default=DEFAULT_PORT, help=f"监听端口 (默认: {DEFAULT_PORT})")
    parser.add_argument("--host", "-H", type=str, default="0.0.0.0", help="监听地址 (默认: 0.0.0.0 局域网全绑定)")
    parser.add_argument("--cache-dir", "-c", type=str, default=DEFAULT_CACHE_DIR, help="本地缓存目录")
    parser.add_argument(
        "--auth",
        default=None,
        help="可选 HTTP Basic 认证，形式 user:password。pip/npm 使用 http://user:pass@教师机:端口 。默认关闭。",
    )
    parser.add_argument(
        "--max-cache-mb",
        type=int,
        default=DEFAULT_MAX_CACHE_BYTES // (1024 * 1024),
        help="缓存上限（MB）。0 表示不落盘。默认 1024。",
    )
    parser.add_argument("--index-ttl", type=int, default=DEFAULT_INDEX_TTL, help="索引/元数据缓存秒数，默认 600。")
    parser.add_argument("--asset-ttl", type=int, default=DEFAULT_ASSET_TTL, help="安装包缓存秒数，默认 7 天。")
    parser.add_argument(
        "--allow-private",
        action="store_true",
        help="允许代理访问本机和内网地址。默认关闭。仅在你清楚风险时使用。",
    )
    parser.add_argument(
        "--allow-host",
        action="append",
        default=None,
        help="限制 CONNECT/正向代理的目标域名后缀，可重复。默认不限制公网域名。",
    )
    parser.add_argument("--connect-port", action="append", type=int, default=None, help="额外允许的 CONNECT 端口。默认仅 443。")
    parser.add_argument("--proxy-port", action="append", type=int, default=None, help="额外允许的明文代理端口。默认 80 和 443。")
    parser.add_argument("--max-connections", type=int, default=DEFAULT_MAX_CONNECTIONS, help="同时处理的连接数上限，默认 64。")
    parser.add_argument("--tunnel-idle", type=int, default=DEFAULT_TUNNEL_IDLE, help="CONNECT 隧道空闲秒数，默认 600。0 表示不限制。")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> None:
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    auth = _validate_auth(args.auth) if args.auth else None
    connect_ports = {443}
    if args.connect_port:
        connect_ports.update(args.connect_port)
    proxy_ports = {80, 443}
    if args.proxy_port:
        proxy_ports.update(args.proxy_port)
    local_ips = get_local_ips()
    print("=" * 60, flush=True)
    print(f"  ClassRelay v{VERSION} 教师机依赖中继与代理服务已启动", flush=True)
    print("=" * 60, flush=True)
    print(f"  Web 控制台与配置入口:  http://{local_ips[0]}:{args.port}/", flush=True)
    print(f"  PyPI 镜像中继:        http://{local_ips[0]}:{args.port}/pypi/", flush=True)
    print(f"  NPM 镜像中继:         http://{local_ips[0]}:{args.port}/npm/", flush=True)
    print(f"  Docker Hub 镜像中继:  http://{local_ips[0]}:{args.port}/v2/   (实验性)", flush=True)
    print(f"  PlatformIO 镜像中继:  http://{local_ips[0]}:{args.port}/platformio/", flush=True)
    print(f"  HTTP/HTTPS 代理隧道:  http://{local_ips[0]}:{args.port}", flush=True)
    print(f"  本地缓存目录:          {args.cache_dir}", flush=True)
    print("  代理默认拒绝本机、内网、链路本地和非常用端口。", flush=True)
    if auth:
        print(f"  HTTP Basic 认证已开启，用户名 {auth[0]}。密码不会写在这里。", flush=True)
    else:
        print("  未开启认证。课堂外网可控时建议加上 --auth user:password。", flush=True)
    print("=" * 60, flush=True)
    print("  学生机一键配置命令 (Windows PowerShell):", flush=True)
    print(f"    irm http://{local_ips[0]}:{args.port}/setup/windows | iex", flush=True)
    print("  学生机一键配置命令 (Linux/macOS Bash):", flush=True)
    print(f"    curl -fsSL http://{local_ips[0]}:{args.port}/setup/bash | bash", flush=True)
    print("  更稳妥的做法是先下载脚本、核对控制台上的 SHA256，再执行。", flush=True)
    print("=" * 60, flush=True)
    try:
        server = ThreadedHTTPServer(
            (args.host, args.port),
            ClassRelayHandler,
            cache_dir=args.cache_dir,
            auth=auth,
            allow_private=args.allow_private,
            allow_hosts=_split_multi(args.allow_host),
            connect_ports=connect_ports,
            proxy_ports=proxy_ports,
            max_cache_bytes=args.max_cache_mb * 1024 * 1024,
            index_ttl=args.index_ttl,
            asset_ttl=args.asset_ttl,
            max_connections=args.max_connections,
            tunnel_idle=args.tunnel_idle,
            local_ips=local_ips,
        )
    except OSError as exc:
        print(f"\n[错误] 无法监听 {args.host}:{args.port} —— {exc}", flush=True)
        if "in use" in str(exc).lower() or getattr(exc, "errno", None) in (98, 10048):
            print("      端口已被其他程序占用（常见：上次未完全退出、Tomcat / 其他代理软件占用了 8080）。", flush=True)
            print("      解决办法二选一：", flush=True)
            print("        1) 换一个端口启动：python classrelay.py --port 8081", flush=True)
            print(f"        2) 关闭占用 {args.port} 端口的程序后重试。", flush=True)
        else:
            print("      请检查端口是否被防火墙/权限限制，或改用其他端口：python classrelay.py --port 8081", flush=True)
        sys.exit(1)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止 ClassRelay...", flush=True)
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
