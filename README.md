# ClassRelay 🎓

> **专为在校教师与机房教学打造的轻量级局域网依赖中继与缓存代理服务器**
>
> 解决痛点：机房网络管制/断网教学环境下，学生机无法访问外网，但教学实验（Python、Node.js、嵌入式 STM32/ESP32 PlatformIO 等）急需拉取外部库与包依赖。

---

## 🌟 核心特性

- **零依赖单文件**：纯 Python 标准库实现，教师机无需安装任何第三方 pip 包或重量级反代工具（如 Nginx、Squid）。
- **多协议全支持**：
  - **PyPI / pip 镜像**：提供 `/pypi/` 镜像端点，自动中继国内清华等高速镜像源。
  - **NPM Registry 镜像**：提供 `/npm/` 镜像端点，中继 npmmirror 镜像源。
  - **Docker Hub 镜像加速**：提供 `/v2/` 镜像端点，中继 Docker Registry 镜像源。
  - **嵌入式/通用开发工具链**：支持 PlatformIO、Espressif 等工具链资产中继与 HTTP/HTTPS CONNECT 正向代理隧道。
- **本地高效持久化缓存**：同一个包只要一位同学拉取过一次，教师机自动落盘缓存，其余所有同学直接局域网秒级下载（节约教师机出口带宽）。
- **学生机一键配置 / 一键还原**：
  - Windows: `irm http://<教师机IP>:8080/setup/windows | iex`
  - Linux / macOS: `curl -fsSL http://<教师机IP>:8080/setup/bash | bash`
  - 课后一键还原学生机系统默认配置，不破坏机房原有环境。
- **实时 Web 控制台**：内置暗色 Dashboard，直观查看教师机局域网 IP、缓存命中率、请求次数与磁盘占用。

---

## 🚀 快速开始

### 1. 教师机一键启动

确保教师机已联网并安装了 Python 3.7+：

#### Windows 教师机
双击运行 `start.bat` 或在终端运行：
```powershell
python classrelay.py
```

#### Linux / macOS 教师机
```bash
chmod +x start.sh
./start.sh
```

启动后终端将打印教师机内网 IP 与配置入口，例如：
```text
============================================================
  ClassRelay v1.0.0 教师机依赖中继与代理服务已启动
============================================================
  Web 控制台与配置入口:  http://192.168.1.100:8080/
  PyPI 镜像中继:        http://192.168.1.100:8080/pypi/
  NPM 镜像中继:         http://192.168.1.100:8080/npm/
  PlatformIO 镜像中继:  http://192.168.1.100:8080/platformio/
  HTTP/HTTPS 代理隧道:  http://192.168.1.100:8080
============================================================
```

---

### 2. 学生机配置

> 假设教师机局域网 IP 为 `192.168.1.100`，端口为 `8080`。

#### 方案 A：一键命令配置（推荐）

- **Windows 学生机 (PowerShell)**:
  ```powershell
  irm http://192.168.1.100:8080/setup/windows | iex
  ```
- **Linux / macOS 学生机 (Bash)**:
  ```bash
  curl -fsSL http://192.168.1.100:8080/setup/bash | bash
  ```

#### 方案 B：独立工具配置

- **pip (Python)**:
  ```bash
  pip config set global.index-url http://192.168.1.100:8080/pypi/
  pip config set global.trusted-host 192.168.1.100
  ```
- **npm (Node.js)**:
  ```bash
  npm config set registry http://192.168.1.100:8080/npm/
  npm config set strict-ssl false
  ```
- **Docker Hub (容器镜像加速)**:
  在学生机 `/etc/docker/daemon.json`（或 Docker Desktop 设置）中添加：
  ```json
  {
    "registry-mirrors": ["http://192.168.1.100:8080"],
    "insecure-registries": ["192.168.1.100:8080"]
  }
  ```
  然后重启 Docker 服务：`sudo systemctl restart docker`。
- **PlatformIO / ESP32 / STM32 (终端代理)**:
  ```bash
  # Windows PowerShell
  $env:HTTP_PROXY="http://192.168.1.100:8080"
  $env:HTTPS_PROXY="http://192.168.1.100:8080"

  # Linux / macOS
  export HTTP_PROXY=http://192.168.1.100:8080
  export HTTPS_PROXY=http://192.168.1.100:8080
  ```
- **Git**:
  ```bash
  git config --global http.proxy http://192.168.1.100:8080
  git config --global https.proxy http://192.168.1.100:8080
  ```

---

### 3. 课后一键还原学生机网络配置

课程结束后，执行还原脚本即可清除机房代理与镜像设定：

- **Windows**:
  ```powershell
  irm http://192.168.1.100:8080/setup/unwindows | iex
  ```
- **Linux / macOS**:
  ```bash
  curl -fsSL http://192.168.1.100:8080/setup/unbash | bash
  ```

---

## 🛠️ 命令行参数

```text
usage: classrelay.py [-h] [--port PORT] [--host HOST] [--cache-dir CACHE_DIR]
                     [--auth-token AUTH_TOKEN] [--max-cache-mb MAX_CACHE_MB]

选项:
  -h, --help                 显示帮助信息
  --port PORT, -p PORT       监听端口 (默认: 8080)
  --host HOST, -H HOST       监听地址 (默认: 0.0.0.0 全局绑定)
  --cache-dir CACHE_DIR      本地缓存路径 (默认: ~/.classrelay/cache)
  --auth-token AUTH_TOKEN    可选：代理/镜像访问密码（默认关闭，见下方安全说明）
  --max-cache-mb N           缓存目录上限 (MB)，超过按最旧优先清理 (默认 200)
```

### 🔐 安全说明：开放代理的暴露风险

ClassRelay 默认监听 `0.0.0.0`，也就是**局域网内任何一台设备都能借用这台教师机的出口带宽**。
校园网 / 机房场景下这通常是预期的，但如果教师机所在网段不完全可控（例如接了访客 Wi-Fi），
建议显式加口令：

```bash
python classrelay.py --auth-token "你的口令"
```

启用后：
- **镜像端点**（`/pypi/`、`/npm/`、`/v2/`、`/platformio/`、`/espressif/`）与 **HTTP 正向代理** 需要携带口令；
- 携带方式二选一：
  - URL 查询参数：`http://<教师机IP>:8080/pypi/?token=你的口令`（pip: `--index-url` 带 `?token=`）
  - 请求头：`X-ClassRelay-Token: 你的口令`
- 不带口令请求会收到 `407 Proxy Authentication Required`；
- Web 控制台（`/`）、一键配置脚本（`/setup/*`）、状态 API（`/api/status`）**不校验口令**，
  方便同学自助配置。

> 更严格的收紧方式：`--host 127.0.0.1`（仅本机，配合本机转发）或在系统防火墙里只放行机房网段。

---

## 📦 缓存策略

- **索引 / 元数据类**（PyPI simple index、npm packument、Docker `/v2/` 目录等）：短 TTL（默认 10 分钟），
  上游发新版本后学生机能在 10 分钟内看到，不会永久锁定在旧索引上。
- **包文件 / tarball 等静态资源**：长 TTL（默认 7 天），够覆盖整节课乃至一周。
- **容量上限**：缓存目录超过 `--max-cache-mb`（默认 200 MB）时，按文件 mtime 由旧到新自动淘汰，
  避免长期占用教师机磁盘。

响应头 `X-ClassRelay-Cache: HIT / MISS` 可用于确认是否命中教师机缓存。

---

## 📄 开源许可证

MIT License.
