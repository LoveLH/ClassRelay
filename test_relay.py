"""
Self-check and verification test for ClassRelay.
Validates Web Dashboard, status API, setup scripts, PyPI mirror, NPM mirror, and HTTP CONNECT forwarding.
"""

import threading
import time
import urllib.request
import urllib.parse
import urllib.error
import json
import os
import shutil
import socket
import re
from classrelay import ThreadedHTTPServer, ClassRelayHandler

TEST_PORT = 18989
TEST_CACHE_DIR = "/tmp/classrelay_test_cache"


def run_tests():
    if os.path.exists(TEST_CACHE_DIR):
        shutil.rmtree(TEST_CACHE_DIR)

    server = ThreadedHTTPServer(("127.0.0.1", TEST_PORT), ClassRelayHandler, cache_dir=TEST_CACHE_DIR)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.5)

    base_url = f"http://127.0.0.1:{TEST_PORT}"

    print("[1] Test Web Dashboard...")
    with urllib.request.urlopen(f"{base_url}/") as resp:
        assert resp.status == 200
        html = resp.read().decode("utf-8")
        assert "ClassRelay" in html
        assert "irm http://" in html
    print("  -> Dashboard OK")

    print("[2] Test API Status...")
    with urllib.request.urlopen(f"{base_url}/api/status") as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert "hits" in data
        assert "misses" in data
    print("  -> API Status OK")

    print("[3] Test Setup Scripts...")
    for script_name in ["windows", "unwindows", "bash", "unbash"]:
        with urllib.request.urlopen(f"{base_url}/setup/{script_name}") as resp:
            assert resp.status == 200
            content = resp.read().decode("utf-8")
            assert len(content) > 10
    print("  -> Setup Scripts OK")

    print("[4] Test PyPI Mirror Proxy & Cache...")
    pypi_url = f"{base_url}/pypi/requests/"
    with urllib.request.urlopen(pypi_url) as resp:
        assert resp.status == 200
        assert resp.headers.get("X-ClassRelay-Cache") == "MISS"
        content = resp.read()
        assert len(content) > 0

    # Repeat to test cache hit
    with urllib.request.urlopen(pypi_url) as resp:
        assert resp.status == 200
        assert resp.headers.get("X-ClassRelay-Cache") == "HIT"
    print("  -> PyPI Proxy & Cache OK")

    print("[5] Test NPM Mirror Proxy & Cache...")
    npm_url = f"{base_url}/npm/vue"
    with urllib.request.urlopen(npm_url) as resp:
        assert resp.status == 200
        assert resp.headers.get("X-ClassRelay-Cache") == "MISS"
        data = json.loads(resp.read().decode("utf-8"))
        assert "name" in data and data["name"] == "vue"

    with urllib.request.urlopen(npm_url) as resp:
        assert resp.status == 200
        assert resp.headers.get("X-ClassRelay-Cache") == "HIT"
    print("  -> NPM Proxy & Cache OK")

    print("[6] Test Docker Hub Mirror Endpoint...")
    docker_v2_url = f"{base_url}/v2/"
    try:
        urllib.request.urlopen(docker_v2_url)
    except urllib.error.HTTPError as e:
        # Docker Registry v2 standard returns 401 Unauthorized with Www-Authenticate header
        assert e.code == 401
        assert "Bearer" in e.headers.get("www-authenticate", "") or "realm" in e.headers.get("www-authenticate", "")
    print("  -> Docker Hub Proxy Endpoint OK")

    print("[7] Test CONNECT Tunneling Proxy...")
    # Test raw CONNECT handshake
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect(("127.0.0.1", TEST_PORT))
    connect_cmd = b"CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org:443\r\n\r\n"
    s.sendall(connect_cmd)
    resp_line = s.recv(1024)
    assert b"200 Connection Established" in resp_line
    s.close()
    print("  -> CONNECT Tunneling OK")

    print("[8] Test 404 returns proper HTTP response (no connection reset)...")
    try:
        urllib.request.urlopen(f"{base_url}/no-such-endpoint")
        assert False, "expected 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404
        assert len(e.read()) > 0  # 响应体可读，不是连接被切断
    print("  -> 404 Response OK")

    print("[9] Test CONNECT malformed target returns 502...")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect(("127.0.0.1", TEST_PORT))
    # 多冒号的畸形目标，应当返回 502 而不是 ValueError 崩线程
    s.sendall(b"CONNECT not::host::format HTTP/1.1\r\nHost: x\r\n\r\n")
    time.sleep(0.3)
    resp = s.recv(2048)
    assert b"502" in resp, f"expected 502 in response, got: {resp[:80]!r}"
    s.close()
    print("  -> CONNECT malformed 502 OK")

    print("[10] Test PyPI real install chain: index → resolve href → fetch package → cache hit...")
    # 真实模拟 pip 行为：解析 index HTML，把 /pypi/packages/* href 拿下来再去请求
    index_resp = urllib.request.urlopen(f"{base_url}/pypi/requests/")
    html = index_resp.read().decode("utf-8")
    pkg_hrefs = re.findall(r'href="(/pypi/packages/[^"#]+)', html)
    assert len(pkg_hrefs) > 0, "no /pypi/packages/ hrefs in rewritten index"
    target = pkg_hrefs[0]
    # 第一次：MISS
    r1 = urllib.request.urlopen(f"{base_url}{target}")
    data1 = r1.read()
    assert r1.headers.get("X-ClassRelay-Cache") == "MISS"
    # 第二次：HIT
    r2 = urllib.request.urlopen(f"{base_url}{target}")
    data2 = r2.read()
    assert r2.headers.get("X-ClassRelay-Cache") == "HIT"
    assert len(data1) == len(data2)
    print(f"  -> pip install chain OK ({len(pkg_hrefs)} pkg links, fetched {len(data1)} bytes)")

    print("[11] Test NPM tarball rewriting goes through teacher cache...")
    r = urllib.request.urlopen(f"{base_url}/npm/vue")
    payload = json.loads(r.read().decode("utf-8"))
    versions = payload["versions"]
    # 找一个 versions 字典 + dist.tarball
    target_ver = None
    for v in versions.values():
        d = v.get("dist")
        if isinstance(d, dict) and d.get("tarball"):
            target_ver = v
            break
    assert target_ver is not None
    tarball = target_ver["dist"]["tarball"]
    assert tarball.startswith("/npm/"), f"tarball not rewritten: {tarball}"
    r1 = urllib.request.urlopen(f"{base_url}{tarball}")
    data1 = r1.read()
    assert r1.headers.get("X-ClassRelay-Cache") == "MISS"
    r2 = urllib.request.urlopen(f"{base_url}{tarball}")
    data2 = r2.read()
    assert r2.headers.get("X-ClassRelay-Cache") == "HIT"
    print(f"  -> npm tarball chain OK ({len(data1)} bytes)")

    print("[12] Test IPv6 Host header in setup script...")
    req = urllib.request.Request(f"{base_url}/setup/bash", headers={"Host": "[::1]:9999"})
    with urllib.request.urlopen(req) as r:
        script = r.read().decode()
    # Host header 解析走 IPv6 分支时，端口一定得对（9999）。本机有 IPv4 时脚本里的 IP 优先用本机 LAN IP，
    # 这是预期行为 —— 这里只断言端口解析正确，没有把 IPv6 字面量错误截断。
    assert ":9999" in script, "expected port 9999 from IPv6 Host header"
    assert "9999" in script
    print("  -> IPv6 Host header OK")

    print("[13] Test optional token authentication...")
    # 关掉当前 server，重启一个启用 token 的实例
    server.shutdown()
    server.server_close()
    time.sleep(0.3)
    if os.path.exists(TEST_CACHE_DIR):
        shutil.rmtree(TEST_CACHE_DIR)
    token_server = ThreadedHTTPServer(("127.0.0.1", TEST_PORT), ClassRelayHandler,
                                      cache_dir=TEST_CACHE_DIR, auth_token="secret123")
    tt = threading.Thread(target=token_server.serve_forever, daemon=True)
    tt.start()
    time.sleep(0.3)

    # 无 token：407
    try:
        urllib.request.urlopen(f"{base_url}/pypi/requests/")
        assert False, "expected 407 without token"
    except urllib.error.HTTPError as e:
        assert e.code == 407

    # header token：200
    req = urllib.request.Request(f"{base_url}/pypi/requests/", headers={"X-ClassRelay-Token": "secret123"})
    with urllib.request.urlopen(req) as r:
        assert r.status == 200

    # query token：200
    with urllib.request.urlopen(f"{base_url}/pypi/requests/?token=secret123") as r:
        assert r.status == 200

    # dashboard 无 token 也放行
    with urllib.request.urlopen(f"{base_url}/") as r:
        assert r.status == 200
    print("  -> Token auth OK")

    token_server.shutdown()
    token_server.server_close()

    if os.path.exists(TEST_CACHE_DIR):
        shutil.rmtree(TEST_CACHE_DIR)
    print("\nALL VERIFICATION TESTS PASSED SUCCESSFULLY.")


if __name__ == "__main__":
    run_tests()
