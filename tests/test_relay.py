"""Offline tests for ClassRelay. Upstream services are local fakes."""

import base64
import hashlib
import io
import json
import logging
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import pytest

import classrelay


class UpstreamHandler(classrelay.http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        self._respond(head=False)

    def do_HEAD(self):
        self._respond(head=True)

    def _respond(self, head: bool) -> None:
        server = self.server
        with server.lock:
            server.hits[self.path] = server.hits.get(self.path, 0) + 1
            server.seen.append(
                (
                    self.command,
                    self.path,
                    {key.lower(): value for key, value in self.headers.items()},
                )
            )
        status, headers, body = server.router(self)
        body = body or b""
        self.send_response(status)
        for key, value in headers:
            self.send_header(key, value)
        if not any(key.lower() == "content-length" for key, _value in headers):
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)


class ThreadedServer(socketserver.ThreadingMixIn, classrelay.http.server.HTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True


@pytest.fixture
def cleanup():
    closers = []
    yield closers
    for close in reversed(closers):
        try:
            close()
        except Exception:
            pass


def serve_forever(httpd, cleanup):
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    cleanup.append(lambda: (httpd.shutdown(), httpd.server_close()))
    return httpd


def start_upstream(router, cleanup):
    httpd = ThreadedServer(("127.0.0.1", 0), UpstreamHandler)
    httpd.router = router
    httpd.hits = {}
    httpd.seen = []
    httpd.lock = threading.Lock()
    return serve_forever(httpd, cleanup)


def start_relay(tmp_path, cleanup, upstreams=None, **kwargs):
    cache = tmp_path / f"cache-{time.time_ns()}"
    kwargs.setdefault("local_ips", ["127.0.0.1"])
    httpd = classrelay.ThreadedHTTPServer(
        ("127.0.0.1", 0),
        classrelay.ClassRelayHandler,
        cache_dir=str(cache),
        upstreams=upstreams,
        **kwargs,
    )
    return serve_forever(httpd, cleanup)


def port_of(httpd) -> int:
    return int(httpd.server_address[1])


def fetch(url, headers=None, method="GET", timeout=10):
    request = urllib.request.Request(url, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def basic(user, password) -> str:
    token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


def connect_status(relay_port: int, payload: bytes, timeout=3):
    sock = socket.create_connection(("127.0.0.1", relay_port), timeout=timeout)
    sock.settimeout(timeout)
    sock.sendall(payload)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    line = data.split(b"\r\n", 1)[0].decode("iso-8859-1", "replace")
    code = int(line.split(" ", 2)[1])
    return code, data, sock


def make_wheel() -> bytes:
    files = {
        "classrelay_local_demo/__init__.py": b'MARKER = "classrelay-local-demo-ok"\n',
        "classrelay_local_demo-1.0.0.dist-info/METADATA": (
            "Metadata-Version: 2.1\n"
            "Name: classrelay-local-demo\n"
            "Version: 1.0.0\n"
            "Summary: ClassRelay offline test package\n"
        ).encode(),
        "classrelay_local_demo-1.0.0.dist-info/WHEEL": (
            "Wheel-Version: 1.0\n"
            "Generator: classrelay-test\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
        ).encode(),
    }
    lines = []
    for path, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        lines.append(f"{path},sha256={digest},{len(data)}")
    lines.append("classrelay_local_demo-1.0.0.dist-info/RECORD,,")
    files["classrelay_local_demo-1.0.0.dist-info/RECORD"] = ("\n".join(lines) + "\n").encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, data in files.items():
            archive.writestr(path, data)
    return buffer.getvalue()


def make_npm_tarball() -> bytes:
    package = {"name": "classrelay-local-pkg", "version": "1.0.0", "main": "index.js"}
    index = 'module.exports = "classrelay-local-pkg-ok";\n'
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for name, data in (
            ("package/package.json", json.dumps(package).encode()),
            ("package/index.js", index.encode()),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
    import gzip

    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", mtime=0) as handle:
        handle.write(raw.getvalue())
    return compressed.getvalue()


def simple_html(wheel: bytes, filename: str) -> bytes:
    digest = hashlib.sha256(wheel).hexdigest()
    href = f"../../packages/ab/cd/{filename}#sha256={digest}"
    page = (
        "<!DOCTYPE html><html><head>"
        '<meta name="pypi:repository-version" content="1.0">'
        "</head><body>"
        "<h1>Links for classrelay-local-demo</h1>"
        f'<a href="{href}">{filename}</a>'
        "</body></html>"
    )
    return page.encode()


def npm_packument(tarball: bytes) -> bytes:
    sha1 = hashlib.sha1(tarball).hexdigest()
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(tarball).digest()).decode()
    payload = {
        "name": "classrelay-local-pkg",
        "dist-tags": {"latest": "1.0.0"},
        "versions": {
            "1.0.0": {
                "name": "classrelay-local-pkg",
                "version": "1.0.0",
                "dist": {
                    "tarball": "https://registry.npmmirror.com/classrelay-local-pkg/-/classrelay-local-pkg-1.0.0.tgz",
                    "shasum": sha1,
                    "integrity": integrity,
                },
            }
        },
    }
    return json.dumps(payload).encode()


# --- policy and rewriters -------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "0.0.0.0",
        "100.64.1.1",
        "192.0.2.1",
        "224.0.0.1",
        "255.255.255.255",
        "::1",
        "fe80::1",
        "fc00::1",
        "2001:db8::1",
        "::ffff:127.0.0.1",
    ],
)
def test_blocked_addresses(ip):
    assert classrelay.is_blocked_ip(ip)


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "2001:4860:4860::8888"])
def test_public_addresses_allowed(ip):
    assert not classrelay.is_blocked_ip(ip)


@pytest.mark.parametrize(
    "host,ip",
    [
        ("2130706433", "127.0.0.1"),
        ("0x7f000001", "127.0.0.1"),
        ("0X7F000001", "127.0.0.1"),
        ("127.1", "127.0.0.1"),
        ("127.0.1", "127.0.0.1"),
        ("0177.0.0.1", "127.0.0.1"),
        ("0x7f.0.0.1", "127.0.0.1"),
        ("134744072", "8.8.8.8"),
        ("8.8.8.8", "8.8.8.8"),
    ],
)
def test_ipv4_literal_normalization(host, ip):
    assert classrelay.normalize_ipv4_literal(host) == ip


@pytest.mark.parametrize("host", ["example.com", "localhost", "", "08", "999.1.1.1", "127.0.0.1.extra"])
def test_non_ipv4_literals_stay_names(host):
    assert classrelay.normalize_ipv4_literal(host) is None


def test_decimal_and_odd_literals_resolve_to_loopback(monkeypatch):
    def fake(host, port):
        assert host == "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))]

    monkeypatch.setattr(classrelay, "resolve_addresses", fake)
    for host in ("2130706433", "0x7f000001", "127.1"):
        infos = classrelay.resolve_destination(host, 80, private_ok=True)
        ips = {item[4][0].split("%", 1)[0] for item in infos}
        assert "127.0.0.1" in ips
        with pytest.raises(classrelay.PolicyError) as raised:
            classrelay.resolve_destination(host, 80, private_ok=False)
        assert raised.value.status == 403


def test_mixed_dns_answers_are_blocked(monkeypatch):
    def fake(host, port):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ]

    monkeypatch.setattr(classrelay, "resolve_addresses", fake)
    with pytest.raises(classrelay.PolicyError) as raised:
        classrelay.resolve_destination("example.com", 443, private_ok=False)
    assert raised.value.status == 403


def test_public_resolution_is_allowed(monkeypatch):
    def fake(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]

    monkeypatch.setattr(classrelay, "resolve_addresses", fake)
    infos = classrelay.resolve_destination("pypi.org", 443, private_ok=False)
    assert infos[0][4][0] == "8.8.8.8"


def test_pypi_relative_href_becomes_absolute():
    page = "https://pypi.example/simple/demo/"
    html = b'<a href="../../packages/ab/cd/demo-1.0.0-py3-none-any.whl#sha256=abc">demo</a>'
    rewritten = classrelay.rewrite_pypi_html(
        html, page_url=page, upstream_hosts={"pypi.example"}, relay_base="http://10.0.0.8:8080"
    ).decode()
    assert 'href="http://10.0.0.8:8080/pypi/packages/ab/cd/demo-1.0.0-py3-none-any.whl#sha256=abc"' in rewritten


def test_npm_tarball_becomes_absolute():
    body = json.dumps({"versions": {"1.0.0": {"dist": {"tarball": "https://registry.npmmirror.com/left-pad/-/left-pad-1.0.0.tgz"}}}}).encode()
    rewritten = json.loads(classrelay.rewrite_npm_json(body, relay_base="http://10.0.0.8:8080"))
    assert rewritten["versions"]["1.0.0"]["dist"]["tarball"] == "http://10.0.0.8:8080/npm/left-pad/-/left-pad-1.0.0.tgz"


def test_seal_roundtrip_and_reject_forged():
    secret = b"s" * 32
    token = classrelay.seal_url("https://auth.example/token", secret)
    assert classrelay.unseal_url(token, secret) == "https://auth.example/token"
    forged = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert classrelay.unseal_url(forged, secret) is None


def test_query_token_is_stripped():
    assert "token" not in classrelay.sanitize_query("token=secret&service=reg")
    assert "service=reg" in classrelay.sanitize_query("token=secret&service=reg")


def test_basic_compare_rejects_mismatch():
    good = basic("teacher", "s3cret")
    assert classrelay.basic_matches(good, "teacher", "s3cret")
    assert not classrelay.basic_matches(basic("teacher", "nope"), "teacher", "s3cret")
    assert not classrelay.basic_matches("Bearer abc", "teacher", "s3cret")


def test_redact_userinfo():
    assert "s3cret" not in classrelay.redact_text('GET http://teacher:s3cret@127.0.0.1/ HTTP/1.1')


def test_setup_scripts_do_not_disable_strict_ssl():
    scripts = classrelay.render_setup_scripts("http://10.0.0.5:8080", "10.0.0.5")
    assert "strict-ssl false" not in scripts["bash"]
    assert "strict-ssl false" not in scripts["windows"]
    assert "npm config delete strict-ssl" in scripts["unbash"]
    assert "npm config delete strict-ssl" in scripts["unwindows"]
    assert "# >>> ClassRelay >>>" in scripts["bash"]
    assert "source ~/.bashrc" in scripts["bash"]
    assert ".bashrc" in scripts["unbash"]


def test_cache_ttl_size_and_partial_file(tmp_path):
    cache = classrelay.CacheManager(str(tmp_path), max_bytes=1000, index_ttl=30, asset_ttl=30)
    tmp = cache.new_temp()
    with open(tmp, "wb") as handle:
        handle.write(b"hello")
    assert cache.commit(
        "idx",
        tmp,
        url="http://example/simple/demo/",
        headers=[("Content-Type", "text/html"), ("Set-Cookie", "a=b")],
        status=200,
        kind="index",
    )
    entry = cache.lookup("idx")
    assert entry is not None
    assert entry.size == 5
    assert all(key.lower() != "set-cookie" for key, _value in entry.headers)
    meta_path = tmp_path / "meta" / "idx.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["timestamp"] = time.time() - 999
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    assert cache.lookup("idx") is None
    meta["timestamp"] = time.time()
    meta["size"] = 999
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    assert cache.lookup("idx") is None

    def store(key, size):
        path = cache.new_temp()
        with open(path, "wb") as handle:
            handle.write(b"z" * size)
        cache.commit(
            key,
            path,
            url="http://example/" + key,
            headers=[("Content-Type", "application/octet-stream")],
            status=200,
            kind="asset",
        )

    store("a", 700)
    store("b", 700)
    assert cache.stats["cached_bytes"] == 700
    assert "a" not in cache.index
    assert "b" in cache.index
    store("b", 200)
    assert cache.stats["cached_bytes"] == 200


# --- HTTP behaviour -------------------------------------------------------


def test_error_responses_keep_the_connection(tmp_path, cleanup):
    relay = start_relay(tmp_path, cleanup, upstreams={"pypi": "http://127.0.0.1:1/simple"})
    base = f"http://127.0.0.1:{port_of(relay)}"
    status, _headers, body = fetch(base + "/nope")
    assert status == 404
    assert b"404" in body
    status, _headers, body = fetch(base + "/", method="POST")
    assert status == 501
    assert body
    status, _headers, body = fetch(base + "/pypi/missing/")
    assert status == 502
    assert body


def test_connect_policy_and_malformed_targets(tmp_path, cleanup, monkeypatch):
    def fail_dns(host, port):
        raise AssertionError(f"DNS should not run for {host}")

    monkeypatch.setattr(classrelay, "resolve_addresses", fail_dns)
    relay = start_relay(tmp_path, cleanup, allow_hosts=("pypi.org",))
    relay_port = port_of(relay)
    code, data, sock = connect_status(relay_port, b"CONNECT example.com:22 HTTP/1.1\r\nHost: example.com:22\r\n\r\n")
    sock.close()
    assert code == 403
    assert b"Port not allowed" in data
    code, _data, sock = connect_status(relay_port, b"CONNECT other.example:443 HTTP/1.1\r\nHost: other.example:443\r\n\r\n")
    sock.close()
    assert code == 403

    monkeypatch.setattr(classrelay, "resolve_addresses", classrelay.socket.getaddrinfo)
    relay2 = start_relay(tmp_path, cleanup)
    relay_port = port_of(relay2)
    for payload in (
        b"CONNECT host:abc HTTP/1.1\r\nHost: host:abc\r\n\r\n",
        b"CONNECT just-a-host HTTP/1.1\r\nHost: just-a-host\r\n\r\n",
        b"CONNECT 127.0.0.1:443 HTTP/1.1\r\nHost: 127.0.0.1:443\r\n\r\n",
        b"CONNECT [::1]:443 HTTP/1.1\r\nHost: [::1]:443\r\n\r\n",
        b"CONNECT 10.1.2.3:443 HTTP/1.1\r\nHost: 10.1.2.3:443\r\n\r\n",
        b"CONNECT 169.254.169.254:443 HTTP/1.1\r\nHost: 169.254.169.254:443\r\n\r\n",
        b"CONNECT 2130706433:443 HTTP/1.1\r\nHost: 2130706433:443\r\n\r\n",
    ):
        code, data, sock = connect_status(relay_port, payload)
        sock.close()
        assert code in (400, 403), payload
        assert data.startswith(b"HTTP/1.1")

    status, _headers, body = fetch(f"http://127.0.0.1:{relay_port}", headers={}, method="GET")
    # absolute-form is what a proxy client sends; a normal GET is the dashboard.
    assert status == 200
    proxy = urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{relay_port}"})
    opener = urllib.request.build_opener(proxy)
    request = urllib.request.Request("http://10.1.2.3/")
    with pytest.raises(urllib.error.HTTPError) as raised:
        opener.open(request, timeout=5)
    assert raised.value.code == 403
    request = urllib.request.Request("http://example.com:8080/")
    with pytest.raises(urllib.error.HTTPError) as raised:
        opener.open(request, timeout=5)
    assert raised.value.code == 403


def test_large_connect_transfer(tmp_path, cleanup):
    payload = os.urandom(2 * 1024 * 1024)
    received = {}

    def accept_loop(listener):
        try:
            conn, _addr = listener.accept()
        except OSError:
            return
        try:
            conn.settimeout(10)
            # Pause so the proxy has to block in sendall instead of failing a non-blocking write.
            time.sleep(0.3)
            data = b""
            while len(data) < len(payload):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            received["data"] = data
            conn.sendall(hashlib.sha256(data).digest())
        finally:
            conn.close()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    cleanup.append(listener.close)
    threading.Thread(target=accept_loop, args=(listener,), daemon=True).start()
    target_port = listener.getsockname()[1]
    relay = start_relay(
        tmp_path,
        cleanup,
        allow_private=True,
        tunnel_idle=30,
        connect_ports={443, target_port},
    )
    code, _data, sock = connect_status(
        port_of(relay),
        f"CONNECT 127.0.0.1:{target_port} HTTP/1.1\r\nHost: 127.0.0.1:{target_port}\r\n\r\n".encode(),
    )
    assert code == 200
    sock.settimeout(10)
    sock.sendall(payload)
    digest = b""
    while len(digest) < 32:
        chunk = sock.recv(32 - len(digest))
        if not chunk:
            break
        digest += chunk
    sock.close()
    assert received["data"] == payload
    assert digest == hashlib.sha256(payload).digest()


def test_tunnel_idle_closes(tmp_path, cleanup):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    cleanup.append(listener.close)

    def accept_one():
        conn, _addr = listener.accept()
        conn.settimeout(5)
        try:
            while conn.recv(1024):
                pass
        except OSError:
            pass
        finally:
            conn.close()

    threading.Thread(target=accept_one, daemon=True).start()
    relay = start_relay(
        tmp_path,
        cleanup,
        allow_private=True,
        tunnel_idle=1,
        connect_ports={443, listener.getsockname()[1]},
    )
    code, _data, sock = connect_status(
        port_of(relay),
        f"CONNECT 127.0.0.1:{listener.getsockname()[1]} HTTP/1.1\r\n\r\n".encode(),
        timeout=3,
    )
    assert code == 200
    sock.settimeout(4)
    assert sock.recv(16) == b""
    sock.close()


def test_auth_covers_origin_and_connect(tmp_path, cleanup):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    cleanup.append(listener.close)
    hits = {"n": 0}

    def accept_one():
        try:
            conn, _addr = listener.accept()
            hits["n"] += 1
            conn.close()
        except OSError:
            pass

    relay = start_relay(
        tmp_path,
        cleanup,
        auth=("teacher", "s3cret"),
        allow_private=True,
        connect_ports={443, listener.getsockname()[1], 9},
    )
    relay_port = port_of(relay)
    base = f"http://127.0.0.1:{relay_port}"
    status, headers, _body = fetch(base + "/pypi/demo/")
    assert status == 401
    assert "basic" in headers.get("WWW-Authenticate", "").lower()
    code, data, sock = connect_status(relay_port, b"CONNECT 127.0.0.1:9 HTTP/1.1\r\n\r\n")
    sock.close()
    assert code == 407
    assert b"Proxy-Authenticate" in data
    status, _headers, _body = fetch(base + "/", headers={"Authorization": basic("teacher", "nope")})
    assert status == 401
    status, _headers, body = fetch(base + "/setup/bash", headers={"Authorization": basic("teacher", "s3cret")})
    assert status == 200
    assert b"teacher:s3cret@" in body or b"teacher:s3cret" in body
    threading.Thread(target=accept_one, daemon=True).start()
    time.sleep(0.05)
    code, _data, sock = connect_status(
        relay_port,
        (
            "CONNECT 127.0.0.1:%d HTTP/1.1\r\nProxy-Authorization: %s\r\n\r\n"
            % (listener.getsockname()[1], basic("teacher", "s3cret"))
        ).encode(),
    )
    assert code == 200
    sock.close()
    deadline = time.time() + 2
    while hits["n"] == 0 and time.time() < deadline:
        time.sleep(0.02)
    assert hits["n"] == 1


def test_password_not_logged_or_forwarded(tmp_path, cleanup):
    seen = {}

    def router(handler):
        seen["path"] = handler.path
        seen["headers"] = {key.lower(): value for key, value in handler.headers.items()}
        return 200, [("Content-Type", "text/html; charset=utf-8")], b"<html>ok</html>"

    upstream = start_upstream(router, cleanup)
    relay = start_relay(
        tmp_path,
        cleanup,
        upstreams={"pypi": f"http://127.0.0.1:{port_of(upstream)}/simple"},
        auth=("teacher", "s3cret"),
    )
    stream = io.StringIO()
    log_handler = logging.StreamHandler(stream)
    classrelay.LOG.addHandler(log_handler)
    try:
        status, _headers, body = fetch(
            f"http://127.0.0.1:{port_of(relay)}/pypi/demo/?token=s3cret",
            headers={
                "Authorization": basic("teacher", "s3cret"),
                "Cookie": "session=s3cret",
                "Proxy-Connection": "Keep-Alive",
            },
        )
    finally:
        classrelay.LOG.removeHandler(log_handler)
    assert status == 200
    assert b"ok" in body
    assert "token" not in seen["path"]
    blob = " ".join(seen["headers"].values()).lower()
    assert "s3cret" not in blob
    assert "cookie" not in seen["headers"]
    assert "proxy-connection" not in seen["headers"]
    assert "s3cret" not in stream.getvalue()


def test_forward_proxy_does_not_share_cookies(tmp_path, cleanup):
    def router(handler):
        cookie = handler.headers.get("Cookie", "")
        who = "alice" if "alice" in cookie else "anon"
        body = f"secret-for:{who}".encode()
        return 200, [("Content-Type", "text/plain"), ("Set-Cookie", f"sess={who}")], body

    upstream = start_upstream(router, cleanup)
    relay = start_relay(
        tmp_path,
        cleanup,
        allow_private=True,
        proxy_ports={80, 443, port_of(upstream)},
    )
    proxy = urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{port_of(relay)}"})
    opener = urllib.request.build_opener(proxy)
    url = f"http://127.0.0.1:{port_of(upstream)}/me"
    first = opener.open(urllib.request.Request(url, headers={"Cookie": "user=alice", "Proxy-Connection": "Keep-Alive"}), timeout=5)
    first_body = first.read()
    first_set_cookie = first.headers.get("Set-Cookie")
    first_cache = first.headers.get("X-ClassRelay-Cache")
    second = opener.open(urllib.request.Request(url), timeout=5)
    second_body = second.read()
    assert first_body == b"secret-for:alice"
    assert second_body == b"secret-for:anon"
    assert "alice" in (first_set_cookie or "")
    assert "alice" not in (second.headers.get("Set-Cookie") or "")
    assert first_cache == "BYPASS"
    assert second.headers.get("X-ClassRelay-Cache") == "BYPASS"
    assert upstream.hits[ "/me"] == 2
    forwarded = " ".join(value.lower() for _path, _count in [] )
    assert all("proxy-connection" not in item[2] for item in upstream.seen)
    assert any("cookie" in item[2] for item in upstream.seen)
    _ = forwarded


def test_redirect_to_loopback_is_blocked(tmp_path, cleanup):
    secret_hits = {"n": 0}
    secret = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    secret.bind(("127.0.0.1", 0))
    secret.listen(5)
    secret.settimeout(0.2)
    cleanup.append(secret.close)

    def sniff():
        while True:
            try:
                conn, _addr = secret.accept()
            except OSError:
                return
            secret_hits["n"] += 1
            conn.close()

    threading.Thread(target=sniff, daemon=True).start()

    def router(handler):
        location = f"http://127.0.0.1:{secret.getsockname()[1]}/secret"
        return 302, [("Location", location)], b""

    upstream = start_upstream(router, cleanup)
    relay = start_relay(
        tmp_path,
        cleanup,
        upstreams={"pypi": f"http://127.0.0.1:{port_of(upstream)}/simple"},
    )
    status, _headers, _body = fetch(f"http://127.0.0.1:{port_of(relay)}/pypi/packages/ab/cd/demo.whl")
    assert status == 403
    time.sleep(0.3)
    assert secret_hits["n"] == 0


def test_chunked_upstream_and_cache(tmp_path, cleanup):
    body = b"chunked-payload-xyz"
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    server.settimeout(5)
    cleanup.append(server.close)
    hits = {"n": 0}

    def loop():
        while True:
            try:
                conn, _addr = server.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    def handle(conn):
        try:
            data = b""
            conn.settimeout(5)
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            hits["n"] += 1
            parts = [body[index : index + 5] for index in range(0, len(body), 5)]
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
            )
            for part in parts:
                conn.sendall(f"{len(part):X}\r\n".encode() + part + b"\r\n")
            conn.sendall(b"0\r\n\r\n")
        finally:
            conn.close()

    threading.Thread(target=loop, daemon=True).start()
    relay = start_relay(
        tmp_path,
        cleanup,
        upstreams={"espressif": f"http://127.0.0.1:{server.getsockname()[1]}"},
    )
    url = f"http://127.0.0.1:{port_of(relay)}/espressif/toolchain.bin"
    status, headers, first = fetch(url)
    assert status == 200
    assert first == body
    assert headers.get("X-ClassRelay-Cache") == "MISS"
    status, headers, second = fetch(url)
    assert status == 200
    assert second == body
    assert headers.get("X-ClassRelay-Cache") == "HIT"
    assert hits["n"] == 1


def test_singleflight_and_no_partial_cache(tmp_path, cleanup):
    started = threading.Event()
    release = threading.Event()

    def router(handler):
        started.set()
        assert release.wait(5)
        return 200, [("Content-Type", "application/octet-stream")], b"package-bytes" * 100

    upstream = start_upstream(router, cleanup)
    relay = start_relay(
        tmp_path,
        cleanup,
        upstreams={"espressif": f"http://127.0.0.1:{port_of(upstream)}"},
    )
    url = f"http://127.0.0.1:{port_of(relay)}/espressif/big.bin"
    errors = []
    bodies = []

    def worker():
        try:
            status, _headers, body = fetch(url, timeout=8)
            if status != 200:
                errors.append(status)
            bodies.append(body)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _index in range(6)]
    for thread in threads:
        thread.start()
    assert started.wait(5)
    time.sleep(0.15)
    meta_dir = Path(relay.cache.meta_dir)
    assert list(meta_dir.glob("*.json")) == []
    assert sum(upstream.hits.values()) == 1
    release.set()
    for thread in threads:
        thread.join(8)
    assert errors == []
    assert len(bodies) == 6
    assert len(set(bodies)) == 1
    assert sum(upstream.hits.values()) == 1
    status, headers, body = fetch(url)
    assert headers.get("X-ClassRelay-Cache") == "HIT"
    assert body == bodies[0]
    assert sum(upstream.hits.values()) == 1


def test_connection_limit(tmp_path, cleanup):
    started = threading.Event()
    release = threading.Event()

    def router(handler):
        started.set()
        release.wait(5)
        return 200, [("Content-Type", "text/plain")], b"ok"

    upstream = start_upstream(router, cleanup)
    relay = start_relay(
        tmp_path,
        cleanup,
        upstreams={"espressif": f"http://127.0.0.1:{port_of(upstream)}"},
        max_connections=1,
    )
    url = f"http://127.0.0.1:{port_of(relay)}/espressif/slow"
    result = {}

    def first():
        result["first"] = fetch(url, timeout=8)

    thread = threading.Thread(target=first)
    thread.start()
    assert started.wait(5)
    status, _headers, body = fetch(url, timeout=3)
    assert status == 503
    assert b"Too many" in body
    release.set()
    thread.join(5)
    assert result["first"][0] == 200


def test_dashboard_uses_connected_address_and_hashes(tmp_path, cleanup):
    relay = start_relay(tmp_path, cleanup, local_ips=["10.9.9.9"])
    base = f"http://127.0.0.1:{port_of(relay)}"
    status, _headers, page = fetch(base + "/", headers={"Host": "evil.example:8080"})
    assert status == 200
    text = page.decode()
    assert "127.0.0.1" in text
    assert "evil.example" not in text
    assert "明文 HTTP" in text
    assert "strict-ssl false" not in text
    assert "实验性" in text
    status, headers, script = fetch(base + "/setup/bash", headers={"Host": "[::1]:8080"})
    assert status == 200
    assert b"127.0.0.1" in script
    assert b"evil" not in script
    assert b"[::1]" not in script
    digest = hashlib.sha256(script).hexdigest()
    assert headers.get("X-ClassRelay-SHA256") == digest
    assert digest in text
    assert b"strict-ssl false" not in script
    status, _headers, restore = fetch(base + "/setup/unbash")
    assert b"npm config delete strict-ssl" in restore
    status, _headers, windows = fetch(base + "/setup/windows")
    assert b"strict-ssl false" not in windows
    status, _headers, unwindows = fetch(base + "/setup/unwindows")
    assert b"npm config delete strict-ssl" in unwindows


def test_bash_setup_persists_and_restore_removes_it(tmp_path, cleanup):
    bash = shutil.which("bash")
    if bash is None or os.name == "nt":
        pytest.skip("bash setup execution is exercised on Linux/macOS")
    relay = start_relay(tmp_path, cleanup)
    script = fetch(f"http://127.0.0.1:{port_of(relay)}/setup/bash")[2]
    restore = fetch(f"http://127.0.0.1:{port_of(relay)}/setup/unbash")[2]
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PATH": os.environ.get("PATH", ""),
        "TMPDIR": str(tmp_path),
        "LANG": "C",
    }
    subprocess.run([bash], input=script, env=env, check=True, timeout=30)
    bashrc = (home / ".bashrc").read_text(encoding="utf-8")
    assert bashrc.count("# >>> ClassRelay >>>") == 1
    assert "HTTP_PROXY=" in bashrc
    subprocess.run([bash], input=script, env=env, check=True, timeout=30)
    bashrc = (home / ".bashrc").read_text(encoding="utf-8")
    assert bashrc.count("# >>> ClassRelay >>>") == 1
    subprocess.run([bash], input=restore, env=env, check=True, timeout=30)
    text = (home / ".bashrc").read_text(encoding="utf-8") if (home / ".bashrc").exists() else ""
    assert "ClassRelay" not in text


def _pypi_router(wheel: bytes, html: bytes, filename: str):
    def router(handler):
        path = urllib.parse.urlsplit(handler.path).path
        if path.rstrip("/") in ("/simple/classrelay-local-demo", "/simple/classrelay_local_demo"):
            return 200, [("Content-Type", "text/html; charset=utf-8")], html
        if path == f"/packages/ab/cd/{filename}":
            return 200, [("Content-Type", "application/octet-stream")], wheel
        return 404, [("Content-Type", "text/plain")], b"missing"
    return router


def test_rewritten_index_is_not_shared_across_addresses(tmp_path, cleanup, monkeypatch):
    filename = "classrelay_local_demo-1.0.0-py3-none-any.whl"
    wheel = make_wheel()
    html = simple_html(wheel, filename)
    upstream = start_upstream(_pypi_router(wheel, html, filename), cleanup)
    relay = start_relay(
        tmp_path,
        cleanup,
        upstreams={"pypi": f"http://127.0.0.1:{port_of(upstream)}/simple"},
    )
    current = {"origin": "http://10.1.1.1:8080", "ip": "10.1.1.1"}

    def fake_origin(self, with_auth=False):
        return current["origin"], current["ip"], 8080

    monkeypatch.setattr(classrelay.ClassRelayHandler, "_origin", fake_origin)
    url = f"http://127.0.0.1:{port_of(relay)}/pypi/classrelay-local-demo/"
    _status, _headers, first = fetch(url)
    assert b"http://10.1.1.1:8080/pypi/packages/" in first
    current["origin"] = "http://10.2.2.2:8080"
    current["ip"] = "10.2.2.2"
    _status, headers, second = fetch(url)
    assert b"http://10.2.2.2:8080/pypi/packages/" in second
    assert headers.get("X-ClassRelay-Cache") == "MISS"
    assert sum(upstream.hits.values()) == 2
    _status, headers, third = fetch(url)
    assert headers.get("X-ClassRelay-Cache") == "HIT"
    assert third == second
    assert sum(upstream.hits.values()) == 2


def test_pip_install_through_relay(tmp_path, cleanup):
    filename = "classrelay_local_demo-1.0.0-py3-none-any.whl"
    wheel = make_wheel()
    html = simple_html(wheel, filename)
    upstream = start_upstream(_pypi_router(wheel, html, filename), cleanup)
    relay = start_relay(
        tmp_path,
        cleanup,
        upstreams={"pypi": f"http://127.0.0.1:{port_of(upstream)}/simple"},
    )
    base = f"http://127.0.0.1:{port_of(relay)}"
    status, headers, page = fetch(base + "/pypi/classrelay-local-demo/")
    assert status == 200
    assert headers.get("X-ClassRelay-Cache") == "MISS"
    assert f"{base}/pypi/packages/ab/cd/{filename}#sha256=".encode() in page
    assert b"../../packages/" not in page
    status, headers, package = fetch(base + f"/pypi/packages/ab/cd/{filename}")
    assert status == 200
    assert package == wheel
    assert headers.get("X-ClassRelay-Cache") == "MISS"
    status, headers, package = fetch(base + f"/pypi/packages/ab/cd/{filename}")
    assert package == wheel
    assert headers.get("X-ClassRelay-Cache") == "HIT"

    # /pypi/simple/... must use the simple root, not the path itself (branch bug B-4).
    status, _headers, page = fetch(base + "/pypi/simple/classrelay-local-demo/")
    assert status == 200
    assert b"/pypi/packages/" in page

    target = tmp_path / "py-site"
    env = os.environ.copy()
    for key in list(env):
        if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "pip_index_url", "pip_extra_index_url"}:
            env.pop(key, None)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--index-url",
        base + "/pypi/",
        "--trusted-host",
        "127.0.0.1",
        "--target",
        str(target),
        "--no-deps",
        "--no-cache-dir",
        "--disable-pip-version-check",
        "classrelay-local-demo==1.0.0",
    ]
    completed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=90)
    assert completed.returncode == 0, completed.stderr + "\n" + completed.stdout
    installed = (target / "classrelay_local_demo" / "__init__.py").read_text(encoding="utf-8")
    assert "classrelay-local-demo-ok" in installed
    wheel_hits = upstream.hits.get(f"/packages/ab/cd/{filename}", 0)
    assert wheel_hits == 1

    other = tmp_path / "py-site-2"
    command[command.index(str(target))] = str(other)
    completed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=90)
    assert completed.returncode == 0, completed.stderr
    assert upstream.hits.get(f"/packages/ab/cd/{filename}", 0) == 1


def test_pip_install_with_basic_auth(tmp_path, cleanup):
    filename = "classrelay_local_demo-1.0.0-py3-none-any.whl"
    wheel = make_wheel()
    html = simple_html(wheel, filename)
    upstream = start_upstream(_pypi_router(wheel, html, filename), cleanup)
    relay = start_relay(
        tmp_path,
        cleanup,
        upstreams={"pypi": f"http://127.0.0.1:{port_of(upstream)}/simple"},
        auth=("teacher", "s3cret"),
    )
    base = f"http://teacher:s3cret@127.0.0.1:{port_of(relay)}"
    target = tmp_path / "auth-site"
    env = os.environ.copy()
    for key in list(env):
        if "proxy" in key.lower() or key.lower() in {"pip_index_url", "pip_extra_index_url"}:
            env.pop(key, None)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--index-url",
            base + "/pypi/",
            "--trusted-host",
            "127.0.0.1",
            "--target",
            str(target),
            "--no-deps",
            "--no-cache-dir",
            "--disable-pip-version-check",
            "classrelay-local-demo==1.0.0",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert completed.returncode == 0, completed.stderr + "\n" + completed.stdout
    assert "classrelay-local-demo-ok" in (target / "classrelay_local_demo" / "__init__.py").read_text(encoding="utf-8")
    blob = " ".join(item[2].get("authorization", "") for item in upstream.seen)
    assert "s3cret" not in blob


def test_npm_install_through_relay(tmp_path, cleanup):
    tarball = make_npm_tarball()
    packument = npm_packument(tarball)

    def router(handler):
        path = urllib.parse.urlsplit(handler.path).path.rstrip("/")
        if path == "/classrelay-local-pkg":
            return 200, [("Content-Type", "application/json")], packument
        if path == "/classrelay-local-pkg/-/classrelay-local-pkg-1.0.0.tgz":
            return 200, [("Content-Type", "application/octet-stream")], tarball
        return 404, [("Content-Type", "text/plain")], b"missing"

    upstream = start_upstream(router, cleanup)
    relay = start_relay(
        tmp_path,
        cleanup,
        upstreams={"npm": f"http://127.0.0.1:{port_of(upstream)}"},
    )
    base = f"http://127.0.0.1:{port_of(relay)}"
    status, headers, body = fetch(base + "/npm/classrelay-local-pkg")
    assert status == 200
    assert headers.get("X-ClassRelay-Cache") == "MISS"
    meta = json.loads(body)
    tarball_url = meta["versions"]["1.0.0"]["dist"]["tarball"]
    assert tarball_url == base + "/npm/classrelay-local-pkg/-/classrelay-local-pkg-1.0.0.tgz"
    status, headers, payload = fetch(tarball_url)
    assert status == 200
    assert payload == tarball
    status, headers, payload = fetch(tarball_url)
    assert headers.get("X-ClassRelay-Cache") == "HIT"
    assert payload == tarball
    assert upstream.hits["/classrelay-local-pkg/-/classrelay-local-pkg-1.0.0.tgz"] == 1

    npm = shutil.which("npm")
    if npm is None:
        pytest.skip("npm is not installed")
    prefix = tmp_path / "npm-site"
    prefix.mkdir()
    env = os.environ.copy()
    for key in list(env):
        if "proxy" in key.lower():
            env.pop(key, None)
    env.update(
        {
            "npm_config_registry": base + "/npm/",
            "npm_config_audit": "false",
            "npm_config_fund": "false",
            "npm_config_update_notifier": "false",
            "npm_config_ignore_scripts": "true",
            "npm_config_fetch_retries": "0",
            "npm_config_cache": str(tmp_path / "npm-cache"),
        }
    )
    completed = subprocess.run(
        [npm, "install", "classrelay-local-pkg", "--ignore-scripts", "--no-audit", "--no-fund", "--prefix", str(prefix)],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        cwd=str(prefix),
    )
    assert completed.returncode == 0, completed.stderr + "\n" + completed.stdout
    installed = prefix / "node_modules" / "classrelay-local-pkg" / "index.js"
    assert "classrelay-local-pkg-ok" in installed.read_text(encoding="utf-8")
    assert upstream.hits["/classrelay-local-pkg/-/classrelay-local-pkg-1.0.0.tgz"] == 1


def test_docker_registry_flow(tmp_path, cleanup):
    def router(handler):
        path = urllib.parse.urlsplit(handler.path).path
        if path in ("/v2", "/v2/"):
            realm = f"http://127.0.0.1:{port_of(handler.server)}/auth/token"
            challenge = f'Bearer realm="{realm}",service="reg",scope="repository:demo:pull"'
            return 401, [("WWW-Authenticate", challenge), ("Content-Type", "application/json")], b"{}"
        if path == "/auth/token":
            return 200, [("Content-Type", "application/json")], b'{"token":"tok123","access_token":"tok123"}'
        if path == "/v2/evil":
            challenge = 'Bearer realm="http://169.254.169.254/latest",service="reg"'
            return 401, [("WWW-Authenticate", challenge)], b""
        if path == "/v2/demo/manifests/latest":
            media = handler.headers.get("Accept", "application/json")
            body = json.dumps({"schemaVersion": 2, "mediaType": media}).encode()
            return 200, [("Content-Type", media), ("Docker-Content-Digest", "sha256:dead")], body
        if path == "/v2/demo/blobs/sha256:abc":
            return 200, [("Content-Type", "application/octet-stream")], b"layer-bytes"
        return 404, [("Content-Type", "text/plain")], b"nope"

    upstream = start_upstream(router, cleanup)
    # router closes over port via handler.server; fill it after bind by using the live port.
    # The /v2/ realm uses handler.server inside the request, so it is available.
    relay = start_relay(
        tmp_path,
        cleanup,
        upstreams={"docker": f"http://127.0.0.1:{port_of(upstream)}"},
    )
    base = f"http://127.0.0.1:{port_of(relay)}"
    status, headers, _body = fetch(base + "/v2/")
    assert status == 401
    challenge = headers.get("WWW-Authenticate", "")
    assert "/_classrelay/docker-token/" in challenge
    assert "169.254" not in challenge
    realm = challenge.split('realm="', 1)[1].split('"', 1)[0]
    status, _headers, token_body = fetch(realm + "?service=reg&scope=repository:demo:pull")
    assert status == 200
    assert json.loads(token_body)["token"] == "tok123"
    forged = realm.rstrip("/")[:-1] + ("A" if not realm.endswith("A") else "B")
    status, _headers, _body = fetch(forged)
    assert status == 400

    accept_v2 = "application/vnd.docker.distribution.manifest.v2+json"
    accept_oci = "application/vnd.oci.image.manifest.v1+json"
    status, headers, body = fetch(
        base + "/v2/demo/manifests/latest",
        headers={"Accept": accept_v2, "Authorization": "Bearer tok123"},
    )
    assert status == 200
    assert json.loads(body)["mediaType"] == accept_v2
    assert headers.get("Docker-Content-Digest") == "sha256:dead"
    manifest_seen = [item for item in upstream.seen if "/manifests/" in item[1] and item[0] == "GET"]
    assert manifest_seen[-1][2].get("accept") == accept_v2
    assert manifest_seen[-1][2].get("authorization") == "Bearer tok123"
    status, _headers, other = fetch(
        base + "/v2/demo/manifests/latest",
        headers={"Accept": accept_oci, "Authorization": "Bearer tok123"},
    )
    assert json.loads(other)["mediaType"] == accept_oci
    assert sum(1 for item in upstream.seen if item[0] == "GET" and "/manifests/" in item[1]) == 2

    status, headers, head_body = fetch(base + "/v2/demo/manifests/latest", method="HEAD", headers={"Accept": accept_v2})
    assert status == 200
    assert head_body == b""
    assert headers.get("Docker-Content-Digest") == "sha256:dead"

    status, headers, blob = fetch(base + "/v2/demo/blobs/sha256:abc")
    assert blob == b"layer-bytes"
    assert headers.get("X-ClassRelay-Cache") == "MISS"
    status, headers, blob = fetch(base + "/v2/demo/blobs/sha256:abc")
    assert blob == b"layer-bytes"
    assert headers.get("X-ClassRelay-Cache") == "HIT"
    assert upstream.hits["/v2/demo/blobs/sha256:abc"] == 1

    status, headers, _body = fetch(base + "/v2/evil")
    assert status == 401
    evil = headers.get("WWW-Authenticate", "")
    evil_realm = evil.split('realm="', 1)[1].split('"', 1)[0]
    status, _headers, _body = fetch(evil_realm)
    assert status == 403


def test_cli_help_stays_compatible():
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[1] / "classrelay.py"), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    assert completed.returncode == 0
    assert "--port" in completed.stdout
    assert "--cache-dir" in completed.stdout
    assert "--auth" in completed.stdout
    with pytest.raises(SystemExit):
        classrelay._validate_auth("only-user")


def test_mirror_set_cookie_is_not_cached(tmp_path, cleanup):
    def router(handler):
        return 200, [("Content-Type", "text/html"), ("Set-Cookie", "a=b"), ("Cache-Control", "private")], b"<html>private</html>"

    upstream = start_upstream(router, cleanup)
    relay = start_relay(
        tmp_path,
        cleanup,
        upstreams={"pypi": f"http://127.0.0.1:{port_of(upstream)}/simple"},
    )
    url = f"http://127.0.0.1:{port_of(relay)}/pypi/demo/"
    status, headers, body = fetch(url)
    assert status == 200
    assert b"private" in body
    assert headers.get("Set-Cookie") == "a=b"
    status, headers, _body = fetch(url)
    assert headers.get("X-ClassRelay-Cache") != "HIT"
    assert sum(upstream.hits.values()) == 2
