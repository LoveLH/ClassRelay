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
import re
from typing import Tuple, Optional, Dict, Any

DEFAULT_PORT = 8080
DEFAULT_CACHE_DIR = os.path.expanduser("~/.classrelay/cache")
VERSION = "1.1.0"

# 上游镜像站默认映射
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
        # 连接公网 DNS 地址以探测本机出口 IP（不实际发数据）
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
    """磁盘缓存层，带 TTL 与上限清理，避免无限增长。"""

    # index 类响应（simple index / docker v2 / npm registry 元数据）TTL 较短：上游发版即可看到
    INDEX_TTL_SECONDS = 600  # 10 分钟
    # 包文件 / tarball 等静态资源 TTL 较长（覆盖整节课足够长）
    ASSET_TTL_SECONDS = 7 * 24 * 3600  # 7 天
    # 缓存总字节上限（默认 ~200 MB）；超过则按 mtime 优先清理
    DEFAULT_MAX_BYTES = 200 * 1024 * 1024

    def __init__(self, cache_dir: str, max_bytes: Optional[int] = None):
        self.cache_dir = cache_dir
        self.max_bytes = max_bytes if max_bytes is not None else self.DEFAULT_MAX_BYTES
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

    @staticmethod
    def _classify(url: str, headers: Dict[str, str]) -> str:
        """根据 URL / 响应头粗略判断是否属于 index 类（短 TTL），否则按静态资源（长 TTL）。"""
        ct = headers.get("Content-Type", "")
        path = urllib.parse.urlparse(url).path
        # 文本 / HTML / JSON 都视为索引/元数据，TTL 短
        if "text/html" in ct or "application/json" in ct or "text/plain" in ct:
            return "index"
        # docker registry /v2/ 根路径永远 401，也是元数据
        if path.startswith("/v2/"):
            return "index"
        return "asset"

    def _ttl_for(self, kind: str) -> int:
        return self.INDEX_TTL_SECONDS if kind == "index" else self.ASSET_TTL_SECONDS

    def get(self, url: str) -> Optional[Tuple[bytes, Dict[str, str], int]]:
        url_hash = self._url_to_hash(url)
        data_path = os.path.join(self.cache_dir, "data", url_hash)
        meta_path = os.path.join(self.cache_dir, "meta", url_hash + ".json")

        if os.path.exists(data_path) and os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                kind = meta.get("kind", "asset")
                ttl = self._ttl_for(kind)
                ts = float(meta.get("timestamp", 0))
                if ttl > 0 and (time.time() - ts) > ttl:
                    # 过期则忽略缓存条目（视为 MISS，但不删除，惰性过期）
                    with self.lock:
                        self.stats["misses"] += 1
                    return None
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

    def _enforce_size_limit(self):
        """若缓存总量超过 max_bytes，按 mtime 升序删除最旧条目直至达标。"""
        try:
            data_dir = os.path.join(self.cache_dir, "data")
            total = 0
            entries = []
            for entry in os.scandir(data_dir):
                if entry.is_file():
                    st = entry.stat()
                    entries.append((st.st_mtime, entry.path, st.st_size))
                    total += st.st_size
            if total <= self.max_bytes or not entries:
                return
            # 最旧优先淘汰
            entries.sort()
            for mt, path, sz in entries:
                if total <= self.max_bytes:
                    break
                try:
                    os.remove(path)
                    url_hash = os.path.basename(path)
                    meta_path = os.path.join(self.cache_dir, "meta", url_hash + ".json")
                    if os.path.exists(meta_path):
                        os.remove(meta_path)
                    total -= sz
                except Exception:
                    continue
            with self.lock:
                self.stats["cached_bytes"] = total
        except Exception:
            pass

    def put(self, url: str, data: bytes, headers: Dict[str, str], status: int = 200):
        # 仅缓存成功的 GET 响应体
        if status != 200 or len(data) == 0:
            return
        url_hash = self._url_to_hash(url)
        kind = self._classify(url, headers)
        data_path = os.path.join(self.cache_dir, "data", url_hash)
        meta_path = os.path.join(self.cache_dir, "meta", url_hash + ".json")

        try:
            clean_headers = {
                k: v for k, v in headers.items() if k.lower() not in ["transfer-encoding", "connection"]
            }
            with open(data_path, "wb") as f:
                f.write(data)
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump({
                    "url": url,
                    "headers": clean_headers,
                    "status": status,
                    "timestamp": time.time(),
                    "kind": kind,
                }, f)
            with self.lock:
                self.stats["cached_bytes"] += len(data)
            # 写入后做一次容量控制，避免无限增长
            self._enforce_size_limit()
        except Exception as e:
            sys.stderr.write(f"[Cache Error] Failed to write cache: {e}\n")


# PyPI simple index 中的包文件链接默认为相对路径 "../../packages/..."，pip 解析后会请求教师机的 /packages/*，
# 而 ClassRelay 没有该路由。需要把 href 重写为教师机可缓存的 /pypi/packages/*。
_PYPI_HREF_RE = re.compile(r'href="([^"]+)"')


def _rewrite_pypi_simple_index(html_text: str) -> Optional[str]:
    """重写 PyPI simple index HTML 中的 href，使其指向教师机 /pypi/ 镜像。

    返回处理后的 HTML；解析失败返回 None。
    """
    def _rewrite(match: "re.Match[str]") -> str:
        href = match.group(1)
        # 已是绝对 URL：仅当指向已知上游 simple 域名时改写，否则保留避免破坏上游直链场景
        if href.startswith("http://") or href.startswith("https://"):
            for upstream_host in ("pypi.tuna.tsinghua.edu.cn", "pypi.org", "files.pythonhosted.org"):
                if upstream_host in href:
                    # 抽出 path 段（去掉 fragment）
                    parsed = urllib.parse.urlparse(href)
                    new_path = parsed.path
                    return f'href="/pypi{new_path}"'
            return match.group(0)
        # 相对路径：把常见的 ../../packages/xx/yy/<pkg> 改成 /pypi/packages/xx/yy/<pkg>
        # fragment (#sha256=...) 由客户端自行丢弃，这里保留也可。
        if href.startswith("../../"):
            tail = href[6:]  # 去掉 "../.."
        elif href.startswith("./"):
            tail = href[2:]
        elif href.startswith("/"):
            return match.group(0)  # 已是绝对路径但属上游站外，无需改写
        else:
            tail = href
        return f'href="/pypi/{tail}"'

    new_html, count = _PYPI_HREF_RE.subn(_rewrite, html_text)
    if count == 0:
        return None
    return new_html


def _rewrite_npm_packument(payload: Any) -> Optional[Dict[str, Any]]:
    """把 npm packument 中所有 dist.tarball 重写为教师机 /npm/ 镜像 URL，确保包文件走缓存。"""
    changed = False

    def _rewrite_url(url: Any) -> Any:
        nonlocal changed
        if not isinstance(url, str):
            return url
        if not (url.startswith("http://") or url.startswith("https://")):
            return url
        # 抽出 path，把上游 tarball URL（形如 .../vue/-/vue-1.2.3.tgz）改成 /npm/vue/-/vue-1.2.3.tgz
        parsed = urllib.parse.urlparse(url)
        new_url = f"/npm{parsed.path}"
        changed = True
        return new_url

    if isinstance(payload, dict):
        dist = payload.get("dist")
        if isinstance(dist, dict) and isinstance(dist.get("tarball"), str):
            new_tar = _rewrite_url(dist["tarball"])
            if new_tar != dist["tarball"]:
                dist["tarball"] = new_tar
                changed = True
        versions = payload.get("versions")
        if isinstance(versions, dict):
            for ver_data in versions.values():
                if isinstance(ver_data, dict):
                    d = ver_data.get("dist")
                    if isinstance(d, dict) and isinstance(d.get("tarball"), str):
                        new_tar = _rewrite_url(d["tarball"])
                        if new_tar != d["tarball"]:
                            d["tarball"] = new_tar
                            changed = True
    if not changed:
        return None
    return payload


class ClassRelayHandler(http.server.BaseHTTPRequestHandler):
    """处理 HTTP CONNECT 代理、REST 镜像端点、Web UI 与一键配置脚本。"""

    server_version = f"ClassRelay/{VERSION}"

    # 由 ThreadedHTTPServer 通过 auth_token 注入；为空表示不启用认证（默认保持开箱即用）
    auth_token: Optional[str] = None

    def log_message(self, format, *args):
        # 简洁访问日志：使用 format % args 安全拼接，避免 send_error 等仅传 2 个参数时触发 IndexError
        try:
            message = format % args
        except Exception:
            message = format if isinstance(format, str) else str(format)
        sys.stdout.write(f"[{self.log_date_time_string()}] {self.client_address[0]} {message}\n")

    @property
    def cache(self) -> CacheManager:
        return self.server.cache_manager

    def do_CONNECT(self):
        """Standard HTTP forward proxy via CONNECT tunneling (HTTPS)."""
        # Format: host:port (IPv6 时为 [::1]:port)。host:port 解析放进 try，避免畸形/ValueError 崩线程。
        target_sock: Optional["socket"] = None
        try:
            if ":" in self.path and not self.path.startswith("["):
                host, port_str = self.path.split(":", 1)
            elif self.path.startswith("[") and "]" in self.path:
                host_part, rest = self.path[1:].split("]", 1)
                host = host_part
                port_str = rest.lstrip(":") or "443"
            else:
                host, port_str = (self.path, "443")
            try:
                port = int(port_str)
            except ValueError:
                self.send_error(502, f"Bad Gateway: invalid CONNECT target {self.path}")
                return

            target_sock = socket.create_connection((host, port), timeout=15)
            self.send_response(200, "Connection Established")
            self.end_headers()

            # 客户端 socket 与远端 server socket 之间做双向管道转发
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
            try:
                self.send_error(502, f"Bad Gateway CONNECT failed: {e}")
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            if target_sock is not None:
                try:
                    target_sock.close()
                except Exception:
                    pass

    def _check_auth(self) -> bool:
        """检查请求是否携带有效 token。若未启用 token（默认）则放行。

        Token 通过查询参数 `?token=xxx` 或请求头 `X-ClassRelay-Token: xxx` 传入。
        返回 True 表示已认证。
        """
        token = self.auth_token
        if not token:
            return True
        # 查询参数
        try:
            parsed_q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            for t in parsed_q.get("token", []):
                if t == token:
                    return True
        except Exception:
            pass
        # 自定义请求头
        header_token = self.headers.get("X-ClassRelay-Token", "")
        if header_token and header_token == token:
            return True
        return False

    def do_GET(self):
        """路由分发：Dashboard / 脚本 / 镜像 / HTTP 正向代理。"""
        # 1. 正向代理：路径以 http:// 或 https:// 开头
        if self.path.startswith("http://") or self.path.startswith("https://"):
            if not self._check_auth():
                self.send_error(407, "Proxy Authentication Required: provide ?token=... or X-ClassRelay-Token header")
                return
            self._handle_forward_http()
            return

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # 2. Web 控制台 Dashboard（无需认证：用于让同学扫描/查看代理使用方式）
        if path in ["", "/", "/index.html"]:
            self._serve_dashboard()
            return

        # 3. Status API
        if path == "/api/status":
            self._serve_api_status()
            return

        # 4. 一键配置脚本
        if path.startswith("/setup/"):
            self._serve_setup_script(path)
            return

        # 5. 镜像路由（需要 token 防止被盗用带宽）
        if path.startswith("/pypi/") or path.startswith("/npm/") or path.startswith("/v2/") or path == "/v2" or path.startswith("/docker/") or path.startswith("/platformio/") or path.startswith("/espressif/"):
            if not self._check_auth():
                self.send_error(407, "Proxy Authentication Required: provide ?token=... or X-ClassRelay-Token header")
                return
            if path.startswith("/pypi/"):
                # simple index 与包文件两类上游路径不同：
                #   /pypi/<package>/         → upstream /simple/<package>/
                #   /pypi/packages/<...>     → upstream /packages/<...>  （用于 simple 页面相对链接 ../packages/）
                #   /pypi/simple/<...>       → upstream /simple/<...>    （直接访问 raw source 形式）
                sub = path[len("/pypi"):]
                if sub.startswith("/packages/"):
                    upstream = UPSTREAMS["pypi"].replace("/simple", "") + "/"
                    self._proxy_mirror(sub, upstream, "pypi")
                elif sub.startswith("/simple/"):
                    self._proxy_mirror(sub[len("/simple"):], sub, "pypi")
                else:
                    self._proxy_mirror(sub, UPSTREAMS["pypi"], "pypi")
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

        # 兜底 404
        self.send_error(404, "Endpoint not found on ClassRelay")

    def _parse_host_header(self) -> Tuple[str, str]:
        """解析 Host 头，兼容 IPv6（[::1]:8080）。返回 (server_ip, port)。"""
        host = self.headers.get("Host", f"127.0.0.1:{DEFAULT_PORT}")
        # 去除 IPv6 方括号前的字面量影响
        try:
            if host.startswith("["):
                # IPv6: [::1]:8080
                end = host.find("]")
                if end == -1:
                    server_ip = host
                    port = str(DEFAULT_PORT)
                else:
                    server_ip = host[: end + 1]
                    rest = host[end + 1 :]
                    port = rest.lstrip(":") or str(DEFAULT_PORT)
            else:
                # 简单 host:port，使用 rsplit 保证 IPv4 / hostname 正常
                if ":" in host:
                    server_ip, _, port = host.rpartition(":")
                    if not port.isdigit():
                        port = str(DEFAULT_PORT)
                else:
                    server_ip = host
                    port = str(DEFAULT_PORT)
        except Exception:
            server_ip, port = "127.0.0.1", str(DEFAULT_PORT)
        return server_ip, port

    def _serve_dashboard(self):
        server_ip, port = self._parse_host_header()
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
        server_ip, port = self._parse_host_header()
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
        # 去掉子路径开头可能存在的斜杠
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
            # 跳过 transfer-encoding / connection / 原始 Content-Length（我们另行 set our own）
            for k, v in headers.items():
                if k.lower() in ("transfer-encoding", "connection", "content-length"):
                    continue
                self.send_header(k, v)
            self.send_header("X-ClassRelay-Cache", "HIT")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        # 用教师机的网络连接去拉取上游
        try:
            req = urllib.request.Request(
                upstream_url,
                headers={"User-Agent": "ClassRelay-Teacher-Proxy/1.0", "Accept": "*/*"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                status = resp.getcode()
                resp_headers = dict(resp.info())

                # PyPI simple index 页面需要把包文件 href 重写为教师机可缓存路径，
                # 否则 pip 会按相对路径解析为 http://<host>/packages/...，该路径无路由 → 404
                if mirror_type == "pypi" and "text/html" in resp_headers.get("Content-Type", ""):
                    try:
                        html_text = data.decode("utf-8", errors="replace")
                        rewritten = _rewrite_pypi_simple_index(html_text)
                        if rewritten is not None:
                            data = rewritten.encode("utf-8")
                    except Exception:
                        pass

                # NPM packument 需要重写 dist.tarball，确保 npm 拉取的包文件也走教师机缓存
                if mirror_type == "npm" and "application/json" in resp_headers.get("Content-Type", ""):
                    try:
                        payload = json.loads(data.decode("utf-8", errors="replace"))
                        rewritten_npm = _rewrite_npm_packument(payload)
                        if rewritten_npm is not None:
                            data = json.dumps(rewritten_npm).encode("utf-8")
                    except Exception:
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
                if k.lower() in ("transfer-encoding", "connection", "content-length"):
                    continue
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

    def __init__(self, server_address, RequestHandlerClass, cache_dir: str,
                 auth_token: Optional[str] = None, max_cache_bytes: Optional[int] = None):
        super().__init__(server_address, RequestHandlerClass)
        self.cache_manager = CacheManager(cache_dir, max_bytes=max_cache_bytes)
        # 将 token 注入 handler 类属性（默认 None 即关闭认证）
        RequestHandlerClass.auth_token = auth_token


def main():
    parser = argparse.ArgumentParser(description="ClassRelay - 局域网机房依赖中继与缓存分发服务")
    parser.add_argument("--port", "-p", type=int, default=DEFAULT_PORT, help=f"监听端口 (默认: {DEFAULT_PORT})")
    parser.add_argument("--host", "-H", type=str, default="0.0.0.0", help="监听地址 (默认: 0.0.0.0 局域网全绑定)")
    parser.add_argument("--cache-dir", "-c", type=str, default=DEFAULT_CACHE_DIR, help="本地缓存目录")
    parser.add_argument("--auth-token", type=str, default=None,
                        help="可选：代理/镜像访问密码。开启后需在 pip/npm 中以 ?token=xxx 或 X-ClassRelay-Token 头携带。"
                             "默认关闭以保持开箱即用。注意：默认 0.0.0.0 绑定会暴露给整个局域网，建议课堂场景显式启用。")
    parser.add_argument("--max-cache-mb", type=int, default=None,
                        help=f"缓存目录上限（MB）。超过则按最旧优先清理，默认 {CacheManager.DEFAULT_MAX_BYTES // (1024*1024)} MB。")
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
        max_bytes = args.max_cache_mb * 1024 * 1024 if args.max_cache_mb else None
        server = ThreadedHTTPServer(
            (args.host, args.port),
            ClassRelayHandler,
            cache_dir=args.cache_dir,
            auth_token=args.auth_token,
            max_cache_bytes=max_bytes,
        )
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
