# ClassRelay 🎓

> **专为在校教师与机房教学打造的轻量级局域网依赖中继与缓存代理服务器**
>
> 解决痛点：机房网络管制或断网教学时，学生机访问不了外网，但 Python、Node.js、嵌入式 PlatformIO / ESP32 实验又需要拉包。教师机联网，学生机只访问教师机。

---

## 核心特性

- **零依赖单文件**：只使用 Python 标准库。教师机不用装 Nginx、Squid，也不用装第三方 pip 包。运行方式仍是 `python classrelay.py`。
- **镜像中继**：
  - **PyPI / pip**：`/pypi/`，默认上游清华 simple 源。简单索引里的相对包链接会改写成教师机上的绝对地址，包文件走缓存。
  - **npm**：`/npm/`，默认上游 npmmirror。packument 里的 `dist.tarball` 会改写成教师机绝对地址，tarball 走缓存。
  - **Docker Registry v2（实验性）**：`/v2/`，默认上游 daocloud。支持 pull 所需的 HEAD/GET、Accept 内容协商，以及把 `WWW-Authenticate` 的 realm 改回教师机再代拉 token。不含 push、blob mount、Range 断点续传。
  - **PlatformIO / Espressif**：`/platformio/`、`/espressif/`，以及 HTTP/HTTPS 代理隧道。
- **磁盘缓存**：索引默认缓存 10 分钟，安装包默认 7 天，总容量默认 1 GB，超出后按最旧优先删除。同一个包并发下载时只向上游要一次，写完才对其他同学可见。
- **学生机配置脚本**：控制台给出 Windows / Linux / macOS 命令，以及脚本 SHA256。bash 会把代理写进 `~/.bashrc` 和 `~/.zshrc` 的标记块；还原脚本会删掉标记，并执行 `npm config delete strict-ssl`。镜像是 HTTP，脚本不会再设置 `strict-ssl false`。
- **Web 控制台**：显示学生机这次实际连到的地址、缓存命中和占用。

---

## 安全说明

默认绑定 `0.0.0.0`，同一局域网里的人都能连上。程序做了这些限制：

- **CONNECT 默认只允许 443**，明文正向代理默认只允许 **80 和 443**。
- **拒绝本机、回环、链路本地、RFC1918 私网、运营商级 NAT（100.64/10）、组播、文档地址和云元数据地址**。判断发生在 DNS 解析之后；一个域名只要有任何一条记录落在这些地址上，整次请求都会被拒绝。
- **正向代理不缓存**。带 `Cookie` 的镜像请求不缓存；响应里有 `Set-Cookie`、`Cache-Control: private/no-store/no-cache` 时也不缓存。
- **可选 HTTP Basic 认证**（`--auth 用户:密码`）。pip、npm、git 使用 `http://用户:密码@教师机:端口` 这种标准写法。默认关闭，方便教室里零配置开用。口令比较使用 `hmac.compare_digest`，访问日志会抹掉 URL 里的用户名密码。开启后，控制台和脚本也要带认证；Docker 客户端往往把 `Authorization` 留给仓库自己的 Bearer token，和本程序的 Basic 认证不好同时使用，需要认证时优先用 pip/npm/代理，把 Docker 留在关闭认证的网络里。
- **仍可按域名收紧代理**：重复传入 `--allow-host github.com` 这类后缀后，CONNECT 和正向代理只放行这些域名。镜像路由走的是教师机配置的上游，不受这份名单限制。
- `--allow-private` 会重新允许访问教师机本机和内网，等于关掉上面的地址防护，只在你清楚风险时使用。

学生机仍然可以通过教师机访问**公网** 80/443。这是 PlatformIO、git clone 需要的能力。如果机房不希望学生任意上网，请加上 `--allow-host`。

一键脚本通过明文 HTTP 下发。同网段的人可以冒充教师机。请先下载脚本，对照控制台上的 SHA256，确认内容后再执行，不要把 `curl | bash` 或 `irm | iex` 当成日常习惯。`curl | bash` 也不能给当前终端设置环境变量；脚本会写入 shell 启动文件，并提示新开一个终端或 `source ~/.bashrc`。

---

## 快速开始

### 1. 教师机启动

需要 Python 3.8+（持续在 3.9 和 3.13 上测试）：

#### Windows

双击 `start.bat`，或：

```powershell
python classrelay.py
```

#### Linux / macOS

```bash
chmod +x start.sh
./start.sh
```

启动后终端会打印地址，例如：

```text
============================================================
  ClassRelay v1.2.0 教师机依赖中继与代理服务已启动
============================================================
  Web 控制台与配置入口:  http://192.168.1.100:8080/
  PyPI 镜像中继:        http://192.168.1.100:8080/pypi/
  NPM 镜像中继:         http://192.168.1.100:8080/npm/
  Docker Hub 镜像中继:  http://192.168.1.100:8080/v2/   (实验性)
  HTTP/HTTPS 代理隧道:  http://192.168.1.100:8080
============================================================
```

多网卡时，以学生机浏览器里看到的「本次连接地址」为准。启动横幅里的 IP 只是教师机自己探测到的一块网卡。

建议在有口令的课堂上：

```bash
python classrelay.py --auth teacher:classroom-pass
```

学生机则使用 `http://teacher:classroom-pass@192.168.1.100:8080`。用户名和密码不要包含空格或控制字符。

### 2. 学生机配置

下面假设教师机地址是 `192.168.1.100`，端口 `8080`，且没有开启认证。开了认证就把 `http://192.168.1.100:8080` 换成 `http://用户:密码@192.168.1.100:8080`。

更稳妥的做法是打开 `http://192.168.1.100:8080/`，按页面上的 SHA256 核对后再执行。

- **pip**

  ```bash
  pip config set global.index-url http://192.168.1.100:8080/pypi/
  pip config set global.trusted-host 192.168.1.100
  ```

- **npm**（HTTP 镜像，不要关 `strict-ssl`）

  ```bash
  npm config set registry http://192.168.1.100:8080/npm/
  ```

- **代理**（PlatformIO、git、其他 HTTPS 工具）

  ```bash
  export HTTP_PROXY=http://192.168.1.100:8080
  export HTTPS_PROXY=http://192.168.1.100:8080
  git config --global http.proxy http://192.168.1.100:8080
  git config --global https.proxy http://192.168.1.100:8080
  ```

- **Docker（实验性）**

  ```json
  {
    "registry-mirrors": ["http://192.168.1.100:8080"],
    "insecure-registries": ["192.168.1.100:8080"]
  }
  ```

  然后重启 Docker。只保证常见的公开镜像 pull 流程，不保证所有客户端特性。

### 3. 课后还原

- Windows：`http://<教师机>:8080/setup/unwindows`
- Linux / macOS：`http://<教师机>:8080/setup/unbash`

还原会去掉 pip / npm / git 代理，删除 shell 启动文件里的 ClassRelay 标记，并执行 `npm config delete strict-ssl`。当前已经打开的终端要关掉重开，环境变量才会消失。

---

## 命令行参数

```text
usage: classrelay.py [-h] [--port PORT] [--host HOST] [--cache-dir CACHE_DIR]
                     [--auth AUTH] [--max-cache-mb MAX_CACHE_MB]
                     [--index-ttl INDEX_TTL] [--asset-ttl ASSET_TTL]
                     [--allow-private] [--allow-host ALLOW_HOST]
                     [--connect-port CONNECT_PORT] [--proxy-port PROXY_PORT]
                     [--max-connections MAX_CONNECTIONS]
                     [--tunnel-idle TUNNEL_IDLE]
```

| 选项 | 含义 |
| --- | --- |
| `--port` / `-p` | 监听端口，默认 8080 |
| `--host` / `-H` | 监听地址，默认 `0.0.0.0` |
| `--cache-dir` / `-c` | 缓存目录，默认 `~/.classrelay/cache` |
| `--auth user:password` | 可选 HTTP Basic。默认关闭 |
| `--max-cache-mb` | 缓存上限，默认 1024。`0` 表示不落盘 |
| `--index-ttl` | 索引缓存秒数，默认 600 |
| `--asset-ttl` | 安装包缓存秒数，默认 7 天 |
| `--allow-private` | 允许代理访问本机和内网。默认关闭 |
| `--allow-host` | 限制代理目标域名后缀，可重复，也可用逗号分隔 |
| `--connect-port` | 在 443 之外额外允许的 CONNECT 端口 |
| `--proxy-port` | 在 80/443 之外额外允许的明文代理端口 |
| `--max-connections` | 同时处理的连接数，默认 64，超出返回 503 |
| `--tunnel-idle` | CONNECT 空闲断开秒数，默认 600。`0` 表示不限制 |

原有的 `--port`、`--host`、`--cache-dir` 用法不变。

---

## 开发与测试

测试使用本地假上游，不访问外网：

```bash
python -m pip install pytest
python -m pytest -q
```

GitHub Actions 会在 Linux 和 Windows 上，用 Python 3.9 和 3.13 跑同一套测试。运行时仍然只有标准库，pytest 只在测试时需要。

---

## 开源许可证

MIT License.
