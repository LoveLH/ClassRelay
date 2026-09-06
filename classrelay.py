"""
ClassRelay (师生局域网依赖中继/代理分发服务)
Single-file zero-dependency LAN caching & forward proxy for classroom teaching.
Supports: HTTP/HTTPS CONNECT forward proxy, PyPI/pip mirror proxy, NPM registry mirror proxy,
PlatformIO/ESP32/STM32 toolchain asset cache, and Web Dashboard + one-click student config scripts.
"""

import http.server
import http.client
import socketserver
import urllib.request
import urllib.error
import urllib.parse
import threading
import select
import socket
import json
import time
import os
import sys
import argparse
import hashlib
from typing import Tuple, Optional, Dict, Any

DEFAULT_PORT = 8080
DEFAULT_CACHE_DIR = os.path.expanduser("~/.classrelay/cache")
VERSION = "1.0.0"

# Default upstream mappings
UPSTREAMS = {
    "pypi": "https://pypi.tuna.tsinghua.edu.cn/simple",
    "npm": "https://registry.npmmirror.com",
    "docker": "https://docker.m.daocloud.io",
    "platformio": "https://api.registry.platformio.org",
    "github_raw": "https://raw.githubusercontent.com",
    "espressif": "https://dl.espressif.com",
}


def get_local_ips():
    """Retrieve non-loopback local IPv4 addresses."""
    ips = []
    try:
        # Connect to public DNS address without sending data
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.5.5.5", 80))
        primary_ip = s.getsockname()[0]
        s.close()
        ips.append(primary_ip)
    except Exception:
        pass

    try:
        host_ips = socket.gethostbyname_ex(socket.gethostname())[2]
        for ip in host_ips:
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass

    if not ips:
        ips.append("127.0.0.1")
    return ips


class CacheManager:
    """Disk-backed caching layer for requested HTTP assets."""

    def __init__(self, cache_dir: str):
        self.cache_dir = cache_dir
        os.makedirs(os.path.join(cache_dir, "meta"), exist_ok=True)
        os.makedirs(os.path.join(cache_dir, "data"), exist_ok=True)
        self.stats = {
            "hits": 0,
            "misses": 0,
            "cached_bytes": 0,
            "start_time": time.time(),
        }
        self.lock = threading.Lock()
        self._calc_existing_cache_size()

    def _calc_existing_cache_size(self):
        total = 0
        data_dir = os.path.join(self.cache_dir, "data")
        try:
            for entry in os.scandir(data_dir):
                if entry.is_file():
                    total += entry.stat().st_size
        except Exception:
            pass
        self.stats["cached_bytes"] = total

    def _url_to_hash(self, url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def get(self, url: str) -> Optional[Tuple[bytes, Dict[str, str], int]]:
        url_hash = self._url_to_hash(url)
        data_path = os.path.join(self.cache_dir, "data", url_hash)
        meta_path = os.path.join(self.cache_dir, "meta", url_hash + ".json")

        if os.path.exists(data_path) and os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                with open(data_path, "rb") as f:
                    data = f.read()
                with self.lock:
                    self.stats["hits"] += 1
                return data, meta.get("headers", {}), meta.get("status", 200)
            except Exception:
                pass

        with self.lock:
            self.stats["misses"] += 1
        return None

    def put(self, url: str, data: bytes, headers: Dict[str, str], status: int = 200):
        # Cache successful GET payloads only
        if status != 200 or len(data) == 0:
            return
        url_hash = self._url_to_hash(url)
        data_path = os.path.join(self.cache_dir, "data", url_hash)
        meta_path = os.path.join(self.cache_dir, "meta", url_hash + ".json")

        try:
            # ponytail: in-memory string headers; sufficient for standard pip/npm packages
            clean_headers = {
                k: v for k, v in headers.items() if k.lower() not in ["transfer-encoding", "connection"]
            }
            with open(data_path, "wb") as f:
                f.write(data)
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump({"url": url, "headers": clean_headers, "status": status, "timestamp": time.time()}, f)
            with self.lock:
                self.stats["cached_bytes"] += len(data)
        except Exception as e:
            sys.stderr.write(f"[Cache Error] Failed to write cache: {e}\n")


class ClassRelayHandler(http.server.BaseHTTPRequestHandler):
    """Handles HTTP CONNECT proxy, REST mirror endpoints, Web UI and client scripts."""

    server_version = f"ClassRelay/{VERSION}"

    def log_message(self, format, *args):
        # Terse custom logging
        sys.stdout.write(f"[{self.log_date_time_string()}] {self.client_address[0]} {args[0]} {args[1]} {args[2]}\n")

    @property
    def cache(self) -> CacheManager:
        return self.server.cache_manager

    def do_CONNECT(self):
        """Standard HTTP forward proxy via CONNECT tunneling (HTTPS)."""
        # Format: host:port
        host, port_str = self.path.split(":") if ":" in self.path else (self.path, "443")
        port = int(port_str)

        try:
            target_sock = socket.create_connection((host, port), timeout=15)
            self.send_response(200, "Connection Established")
            self.end_headers()

            # Bidirectional pipe between client socket and remote server socket
            conns = [self.connection, target_sock]
            self.connection.setblocking(False)
            target_sock.setblocking(False)

            while True:
                rlist, _, xlist = select.select(conns, [], conns, 60)
                if xlist or not rlist:
                    break
                for s in rlist:
                    other = target_sock if s is self.connection else self.connection
                    data = s.recv(65536)
                    if not data:
                        return
                    other.sendall(data)
        except Exception as e:
            self.send_error(502, f"Bad Gateway CONNECT failed: {e}")
        finally:
            try:
                target_sock.close()
            except Exception:
                pass

    def do_GET(self):
        """Route to Dashboard, Scripts, Mirrors, or standard forward HTTP proxy."""
        # 1. Forward proxy handling if path starts with http:// or https://
        if self.path.startswith("http://") or self.path.startswith("https://"):
            self._handle_forward_http()
            return

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # 2. Web UI Dashboard
        if path in ["", "/", "/index.html"]:
            self._serve_dashboard()
            return

        # 3. Status API
        if path == "/api/status":
            self._serve_api_status()
            return

        # 4. One-click setup scripts
        if path.startswith("/setup/"):
            self._serve_setup_script(path)
            return

        # 5. Mirror routes: /pypi/, /npm/, /v2/ (Docker Registry v2), /platformio/, /espressif/
        if path.startswith("/pypi/"):
            self._proxy_mirror(path[len("/pypi"):], UPSTREAMS["pypi"], "pypi")
            return
        elif path.startswith("/npm/"):
            self._proxy_mirror(path[len("/npm"):], UPSTREAMS["npm"], "npm")
            return
        elif path.startswith("/v2/") or path == "/v2":
            self._proxy_mirror(path, UPSTREAMS["docker"], "docker")
            return
        elif path.startswith("/docker/"):
            self._proxy_mirror(path[len("/docker"):], UPSTREAMS["docker"], "docker")
            return
        elif path.startswith("/platformio/"):
            self._proxy_mirror(path[len("/platformio"):], UPSTREAMS["platformio"], "platformio")
            return
        elif path.startswith("/espressif/"):
            self._proxy_mirror(path[len("/espressif"):], UPSTREAMS["espressif"], "espressif")
            return

        # Fallback 404
        self.send_error(404, "Endpoint not found on ClassRelay")

    def _serve_dashboard(self):
        host = self.headers.get("Host", f"127.0.0.1:{DEFAULT_PORT}")
        server_ip = host.split(":")[0]
        port = host.split(":")[1] if ":" in host else str(DEFAULT_PORT)
        ips = get_local_ips()
        display_ip = ips[0] if ips else server_ip

        html = f"""<!DOCTYPE html>
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
        .cmd-box {{ position: relative; margin-bottom: 12px; }}
        .stat-val {{ font-size: 28px; font-weight: bold; color: var(--accent); margin: 8px 0; }}
        .ip-list {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }}
        .ip-item {{ background: #0369a1; padding: 3px 8px; border-radius: 4px; font-size: 13px; }}
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
            <h3>中继服务器网络地址</h3>
            <p style="font-size: 13px; color: #94a3b8; margin: 0;">教师机内网 IP（学生机请确保能 ping 通）：</p>
            <div class="ip-list">
                {"".join([f'<span class="ip-item">http://{ip}:{port}</span>' for ip in ips])}
            </div>
        </div>
        <div class="card">
            <h3>缓存与命中统计</h3>
            <div id="stats-container">
                <div class="stat-val" id="hit-rate">-</div>
                <div style="font-size: 13px; color: #94a3b8;" id="stat-detail">加载统计中...</div>
            </div>
        </div>
    </div>

    <div class="card" style="margin-bottom: 20px;">
        <h3>一键学生机配置命令 (Windows / Linux / macOS)</h3>
        <p style="font-size: 14px; color: #cbd5e1;">在学生机打开终端或 PowerShell 执行对应命令，自动将 pip、npm 与 HTTP 代理指向教师机：</p>

        <p style="margin-bottom: 4px; font-size: 13px; font-weight: bold; color: #38bdf8;">Windows (PowerShell 管理员或普通终端):</p>
        <pre><code>irm http://{display_ip}:{port}/setup/windows | iex</code></pre>

        <p style="margin-bottom: 4px; font-size: 13px; font-weight: bold; color: #38bdf8;">Linux / macOS (Bash / Terminal):</p>
        <pre><code>curl -fsSL http://{display_ip}:{port}/setup/bash | bash</code></pre>

        <p style="margin-bottom: 4px; font-size: 13px; font-weight: bold; color: #38bdf8;">课后一键还原学生机网络配置 (恢复官方默认):</p>
        <pre><code># Windows:
irm http://{display_ip}:{port}/setup/unwindows | iex

# Linux/macOS:
curl -fsSL http://{display_ip}:{port}/setup/unbash | bash</code></pre>
    </div>

    <div class="grid">
        <div class="card">
            <h3>Python / pip 独立配置</h3>
            <p style="font-size: 13px; color: #94a3b8;">直接使用教师机镜像或全局代理：</p>
            <pre><code>pip config set global.index-url http://{display_ip}:{port}/pypi/
pip config set global.trusted-host {display_ip}</code></pre>
        </div>

        <div class="card">
            <h3>Node.js / npm 独立配置</h3>
            <p style="font-size: 13px; color: #94a3b8;">指定教师机作为 npm Registry：</p>
            <pre><code>npm config set registry http://{display_ip}:{port}/npm/
npm config set strict-ssl false</code></pre>
        </div>

        <div class="card">
            <h3>Docker / 镜像加速配置</h3>
            <p style="font-size: 13px; color: #94a3b8;">将教师机配置为 Docker Registry Mirror (/etc/docker/daemon.json)：</p>
            <pre><code>{{
  "registry-mirrors": ["http://{display_ip}:{port}"],
  "insecure-registries": ["{display_ip}:{port}"]
}}</code></pre>
        </div>

        <div class="card">
            <h3>嵌入式开发 / PlatformIO / ESP32</h3>
            <p style="font-size: 13px; color: #94a3b8;">在学生机环境变量中设置 HTTP 代理：</p>
            <pre><code># Windows PowerShell
$env:HTTP_PROXY="http://{display_ip}:{port}"
$env:HTTPS_PROXY="http://{display_ip}:{port}"

# Linux / Mac
export HTTP_PROXY=http://{display_ip}:{port}
export HTTPS_PROXY=http://{display_ip}:{port}</code></pre>
        </div>

        <div class="card">
            <h3>Git 依赖拉取代理</h3>
            <p style="font-size: 13px; color: #94a3b8;">让 student git clone 通过教师机 CONNECT 隧道：</p>
            <pre><code>git config --global http.proxy http://{display_ip}:{port}
git config --global https.proxy http://{display_ip}:{port}</code></pre>
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
            document.getElementById('stat-detail').textContent = `命中: ${{data.hits}} 次 | 请求: ${{total}} 次 | 磁盘缓存: ${{mb}} MB`;
        }} catch (e) {{}}
    }}
    updateStats();
    setInterval(updateStats, 3000);
</script>
</body>
</html>"""
        encoded = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _serve_api_status(self):
        stats = dict(self.cache.stats)
        encoded = json.dumps(stats).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _serve_setup_script(self, path: str):
        host = self.headers.get("Host", f"127.0.0.1:{DEFAULT_PORT}")
        server_ip = host.split(":")[0]
        port = host.split(":")[1] if ":" in host else str(DEFAULT_PORT)
        ips = get_local_ips()
        target_ip = ips[0] if ips else server_ip

        if path.endswith("/windows"):
            script = f"""# ClassRelay One-click student setup for Windows
$target = "http://{target_ip}:{port}"
$ip = "{target_ip}"
Write-Host ">>> Configuring student workstation to use ClassRelay Teacher Proxy: $target" -ForegroundColor Cyan

# 1. Pip config
pip config set global.index-url "$target/pypi/"
pip config set global.trusted-host "$ip"

# 2. NPM config
npm config set registry "$target/npm/"
npm config set strict-ssl false

# 3. Environment variable for current session & system
[System.Environment]::SetEnvironmentVariable('HTTP_PROXY', $target, 'User')
[System.Environment]::SetEnvironmentVariable('HTTPS_PROXY', $target, 'User')
$env:HTTP_PROXY = $target
$env:HTTPS_PROXY = $target

# 4. Git config
git config --global http.proxy $target
git config --global https.proxy $target

Write-Host ">>> [SUCCESS] Student configuration complete! You can now run pip install / npm install / platformio." -ForegroundColor Green
"""
        elif path.endswith("/unwindows"):
            script = """# ClassRelay Restore default settings
Write-Host ">>> Restoring student workstation configuration to defaults..." -ForegroundColor Yellow
pip config unset global.index-url
pip config unset global.trusted-host
npm config delete registry
npm config set strict-ssl true
[System.Environment]::SetEnvironmentVariable('HTTP_PROXY', $null, 'User')
[System.Environment]::SetEnvironmentVariable('HTTPS_PROXY', $null, 'User')
$env:HTTP_PROXY = ''
$env:HTTPS_PROXY = ''
git config --global --unset http.proxy
git config --global --unset https.proxy
Write-Host ">>> [SUCCESS] Defaults restored." -ForegroundColor Green
"""
        elif path.endswith("/bash"):
            script = f"""#!/usr/bin/env bash
# ClassRelay One-click student setup for Linux/macOS
TARGET="http://{target_ip}:{port}"
IP="{target_ip}"
echo ">>> Configuring student workstation to use ClassRelay Teacher Proxy: $TARGET"

pip config set global.index-url "$TARGET/pypi/" || true
pip config set global.trusted-host "$IP" || true
npm config set registry "$TARGET/npm/" || true
npm config set strict-ssl false || true
git config --global http.proxy "$TARGET" || true
git config --global https.proxy "$TARGET" || true

export HTTP_PROXY="$TARGET"
export HTTPS_PROXY="$TARGET"
export http_proxy="$TARGET"
export https_proxy="$TARGET"

echo ">>> [SUCCESS] Student configuration complete!"
"""
        else:  # unbash
            script = """#!/usr/bin/env bash
# ClassRelay Restore defaults for Linux/macOS
echo ">>> Restoring defaults..."
pip config unset global.index-url || true
pip config unset global.trusted-host || true
npm config delete registry || true
git config --global --unset http.proxy || true
git config --global --unset https.proxy || true
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
echo ">>> [SUCCESS] Defaults restored."
"""
        encoded = script.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _proxy_mirror(self, subpath: str, upstream_base: str, mirror_type: str):
        """Proxy and cache mirror requests to upstream mirrors (PyPI, npm, PlatformIO, etc.)."""
        # Strip leading slash if present
        subpath = subpath.lstrip("/")
        upstream_url = f"{upstream_base.rstrip('/')}/{subpath}"
        if self.path.find("?") != -1:
            query = self.path[self.path.find("?") :]
            upstream_url += query

        # Check Cache
        cached = self.cache.get(upstream_url)
        if cached:
            data, headers, status = cached
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("X-ClassRelay-Cache", "HIT")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        # Fetch from upstream with teacher machine connection
        try:
            req = urllib.request.Request(
                upstream_url,
                headers={"User-Agent": "ClassRelay-Teacher-Proxy/1.0", "Accept": "*/*"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                status = resp.getcode()
                resp_headers = dict(resp.info())

                # If PyPI simple index, rewrite URLs to point to teacher mirror if relative
                if mirror_type == "pypi" and "text/html" in resp_headers.get("Content-Type", ""):
                    # Replace relative or direct package URLs to maintain caching
                    pass

                self.cache.put(upstream_url, data, resp_headers, status)

                self.send_response(status)
                for k, v in resp_headers.items():
                    if k.lower() not in ["transfer-encoding", "content-length"]:
                        self.send_header(k, v)
                self.send_header("X-ClassRelay-Cache", "MISS")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            try:
                self.send_response(e.code)
                for k, v in dict(e.headers).items():
                    if k.lower() not in ["transfer-encoding", "content-length"]:
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(e.read())
            except (BrokenPipeError, ConnectionResetError):
                pass
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self.send_error(502, f"Failed to fetch upstream {upstream_url}: {e}")
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _handle_forward_http(self):
        """Standard HTTP forward proxy."""
        url = self.path
        cached = self.cache.get(url)
        if cached:
            data, headers, status = cached
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("X-ClassRelay-Cache", "HIT")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        try:
            req = urllib.request.Request(url, headers={k: v for k, v in self.headers.items() if k.lower() != "host"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                status = resp.getcode()
                resp_headers = dict(resp.info())

                self.cache.put(url, data, resp_headers, status)

                self.send_response(status)
                for k, v in resp_headers.items():
                    if k.lower() not in ["transfer-encoding", "content-length"]:
                        self.send_header(k, v)
                self.send_header("X-ClassRelay-Cache", "MISS")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except Exception as e:
            self.send_error(502, f"Forward proxy error: {e}")


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, RequestHandlerClass, cache_dir: str):
        super().__init__(server_address, RequestHandlerClass)
        self.cache_manager = CacheManager(cache_dir)


def main():
    parser = argparse.ArgumentParser(description="ClassRelay - 局域网机房依赖中继与缓存分发服务")
    parser.add_argument("--port", "-p", type=int, default=DEFAULT_PORT, help=f"监听端口 (默认: {DEFAULT_PORT})")
    parser.add_argument("--host", "-H", type=str, default="0.0.0.0", help="监听地址 (默认: 0.0.0.0 局域网全绑定)")
    parser.add_argument("--cache-dir", "-c", type=str, default=DEFAULT_CACHE_DIR, help="本地缓存目录")
    args = parser.parse_args()

    ips = get_local_ips()
    print("=" * 60)
    print(f"  ClassRelay v{VERSION} 教师机依赖中继与代理服务已启动")
    print("=" * 60)
    print(f"  Web 控制台与配置入口:  http://{ips[0]}:{args.port}/")
    print(f"  PyPI 镜像中继:        http://{ips[0]}:{args.port}/pypi/")
    print(f"  NPM 镜像中继:         http://{ips[0]}:{args.port}/npm/")
    print(f"  Docker Hub 镜像中继:  http://{ips[0]}:{args.port}/v2/")
    print(f"  PlatformIO 镜像中继:  http://{ips[0]}:{args.port}/platformio/")
    print(f"  HTTP/HTTPS 代理隧道:  http://{ips[0]}:{args.port}")
    print(f"  本地缓存目录:          {args.cache_dir}")
    print("=" * 60)
    print("  学生机一键配置命令 (Windows PowerShell):")
    print(f"    irm http://{ips[0]}:{args.port}/setup/windows | iex")
    print("  学生机一键配置命令 (Linux/macOS Bash):")
    print(f"    curl -fsSL http://{ips[0]}:{args.port}/setup/bash | bash")
    print("=" * 60)

    try:
        server = ThreadedHTTPServer((args.host, args.port), ClassRelayHandler, cache_dir=args.cache_dir)
    except OSError as e:
        print(f"\n[错误] 无法监听 {args.host}:{args.port} —— {e}")
        if "in use" in str(e).lower() or e.errno in (98, 10048):
            print("      端口已被其他程序占用（常见：上次未完全退出、Tomcat / 其他代理软件占用了 8080）。")
            print("      解决办法二选一：")
            print(f"        1) 换一个端口启动：python classrelay.py --port 8081")
            print(f"        2) 关闭占用 {args.port} 端口的程序后重试。")
            print("      提示：本服务已开启地址复用，关闭后通常需等待几十秒才能重新绑定。")
        else:
            print("      请检查端口是否被防火墙/权限限制，或改用其他端口：python classrelay.py --port 8081")
        sys.exit(1)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止 ClassRelay...")
        server.shutdown()


if __name__ == "__main__":
    main()
