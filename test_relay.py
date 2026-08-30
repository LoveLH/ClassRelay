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

    server.shutdown()
    if os.path.exists(TEST_CACHE_DIR):
        shutil.rmtree(TEST_CACHE_DIR)
    print("\nALL VERIFICATION TESTS PASSED SUCCESSFULLY.")


if __name__ == "__main__":
    run_tests()
