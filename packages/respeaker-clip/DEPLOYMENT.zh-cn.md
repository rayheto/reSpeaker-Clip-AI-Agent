# 部署 Clip 服务（`respeaker-clip serve`）

本文说明如何在带蓝牙的主机上把 reSpeaker Clip 服务作为常驻进程运行——也就是 `npx respeaker-clip serve` 背后真正跑的东西。

[English](./DEPLOYMENT.md)

**实际运行的是什么：** 一个 Flask 进程，持有到**一个** Clip 的**一条** BLE 连接，外加一套 `/api/clip` HTTP API（REST + SSE）。Node CLI 只是安装器/启动器：它建虚拟环境、把 Python 服务装进去，然后 `exec` 执行 `python -m backend.service_cli`。服务起来之后就可以不用再管 Node 了。

---

## 1. 前置条件

| 要求 | 原因 | 检查 |
| --- | --- | --- |
| Node ≥ 18.17 | 运行 CLI | `node --version` |
| Python ≥ 3.10 且带 `venv` | 服务本体（Debian/Ubuntu：`sudo apt install python3-venv`） | `python3 -m venv --help` |
| 蓝牙主机（Linux + BlueZ，或 Windows） | Clip 走 BLE | `bluetoothctl power on` |
| 一个已上电、已配对的 Clip | 语音输入 | `bluetoothctl devices` |
| `GROQ_API_KEY` | LLM + Whisper STT + Orpheus TTS | 见 §3 |
| 空闲 TCP 端口（默认 5000） | HTTP API | `ss -ltnp \| grep 5000` |

**即使 BLE 不可用，服务也会启动并对外提供 API**——它会带退避地重试，并报告 `connected: false`。所以可以在硬件到货前先部署，然后用 `respeaker-clip status` 观察它上线。

---

## 2. 部署

```bash
# 1. 安装 CLI（或用 npx 一次性运行）
npm install -g respeaker-clip

# 2. 检查主机
respeaker-clip doctor

# 3. 运行
sudo mkdir -p /opt/respeaker-clip            # 状态目录：venv、.env、chat.db、clip_audio/
sudo chown "$USER" /opt/respeaker-clip
cd /opt/respeaker-clip
respeaker-clip serve --source /opt/reSpeaker-Clip-AI-Agent
```

`serve` 会决定运行什么、状态放哪里：

| | 默认值 | 覆盖方式 |
| --- | --- | --- |
| 服务来源 | pip 包 `respeaker-clip-service` | `--source <checkout>`（当前推荐，见 §7） |
| 虚拟环境 | `$RESPEAKER_CLIP_HOME/venv`，否则 `~/.cache/respeaker-clip/venv` | `--venv <dir>` |
| 工作目录 | 你运行 CLI 时所在的目录 | — |

工作目录很关键：SQLite 回退库（`chat.db`）、下载的音频（`clip_audio/`）和 `.env` 都从它解析。

首次运行会创建 venv 并 pip 安装（`sentence-transformers` 会花几分钟）。之后复用；加 `--skip-install` 就完全不再碰 pip。

### 参数

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `--host` / `--port` | `0.0.0.0` / `5000` | 绑定地址——注意 §6，API **没有鉴权** |
| `--input-mode` | `clip` | `clip`（仅设备）\| `browser`（系统麦克风）\| `both` |
| `--ble-address` / `--ble-name` | 取自环境变量 | 固定设备，或按名称扫描 |
| `--no-clip` | 关 | 只提供 API，不启动 BLE 运行时（无语音输入） |
| `--env-file` | `./.env` | 配置文件（见 §3） |
| `--venv` / `--pip-spec` / `--skip-install` | 见上 | 安装行为 |
| `-- <参数…>` | — | 其余参数原样转发给服务 |

CLI 环境变量：`RESPEAKER_CLIP_HOME`、`RESPEAKER_CLIP_SERVICE_ROOT`、
`RESPEAKER_CLIP_PIP_SPEC`、`RESPEAKER_CLIP_PYTHON`、`RESPEAKER_CLIP_ENV_FILE`、
`RESPEAKER_CLIP_BASE_URL`（供 `status` 使用）。

---

## 3. 配置

服务读取**工作目录**下的 `.env`（或用 `--env-file` 指定）。真实环境变量始终优先，因此 systemd 的 `EnvironmentFile=` 和容器环境变量会覆盖文件内容。

```bash
cp /opt/reSpeaker-Clip-AI-Agent/.env.example .env
chmod 600 .env                     # 里面有 API key
# 编辑：GROQ_API_KEY、CLIP_BLE_ADDRESS 或 CLIP_BLE_NAME
```

最小可用配置：

```ini
GROQ_API_KEY=gsk_...
CLIP_BLE_ADDRESS=F0:FB:BF:05:FD:EB     # 或 CLIP_BLE_NAME=Clip 走扫描
VOICE_INPUT_MODE=clip
```

生产环境需要关注的其余默认值：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RTC_AUTO_ARM` | `true` | 连接后自动武装温暂停 RTC 会话——实时语音链路要保持开启 |
| `RTC_ARM_TIMEOUT` | `15` | 等待串流启动的秒数，超时后重新武装 |
| `RTC_PARTIAL_INTERVAL` | `2.0` | 滚动临时转写的间隔 |
| `GROQ_RTC_PARTIAL_MODEL` / `GROQ_RTC_FINAL_MODEL` | `whisper-large-v3-turbo` / `whisper-large-v3` | 临时转写用快模型，最终转写用准模型 |
| `DATABASE_URL` | `sqlite:///chat.db` | 若你用 Supabase 则填其 URL（其表缺失时回退 SQLite） |
| `CLIP_TEMP_DIR` | `clip_audio` | 下载的会话音频；注意磁盘容量或定期清理 |
| `CLIP_DOWNLOAD_TIMEOUT` | `300` | 单会话下载超时 |

确认服务实际解析到的配置：

```bash
python3 -m backend.service_cli --print-config     # 在 checkout 目录下
respeaker-clip serve --dry-run                    # CLI 将要执行的命令
```

---

## 4. 用 systemd 运行（Linux）

`/etc/systemd/system/respeaker-clip.service`：

```ini
[Unit]
Description=reSpeaker Clip voice service
After=network-online.target bluetooth.target
Wants=network-online.target bluetooth.target

[Service]
Type=simple
User=clip                                    # 能访问 BlueZ 的用户（见下）
WorkingDirectory=/opt/respeaker-clip
Environment=RESPEAKER_CLIP_HOME=/opt/respeaker-clip
EnvironmentFile=/opt/respeaker-clip/.env     # 可选；真实环境变量本就优先于 .env
ExecStart=/usr/local/bin/respeaker-clip serve --source /opt/reSpeaker-Clip-AI-Agent
Restart=always
RestartSec=10                                # 运行时自己会重连；这里只兜住进程崩溃
TimeoutStopSec=20                            # 留时间给运行时关闭 BLE、写完数据
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now respeaker-clip
journalctl -u respeaker-clip -f
```

**BLE 权限。** 服务需要通过 D-Bus 访问 BlueZ，因此服务用户必须被允许与之通信——一个裸的系统账号通常不行：

- 最简单：用已经配对过 Clip 的那个桌面用户来跑；或
- 把服务用户加入 `bluetooth` 组，并为 `org.bluez` 添加 polkit 规则；或
- 在无头机器上，以该用户执行一次 `bluetoothctl power on`，再用 `respeaker-clip status` 确认设备可见。

如果 BLE 被拒绝，你仍然会得到一个健康的 HTTP API 和 `connected: false`，错误信息在 `last_error` 里——这一点很容易被误读成「服务正常」。

**永远只跑一个进程。** 运行时每个进程持有且仅持有一条 BLE 连接，reloader 也是刻意关闭的。不要同时跑两个实例（包括在 unit 之外又跑一个 `python app.py`），也不要把服务配成多 worker。

---

## 5. 反向代理

API 就是普通 HTTP + SSE，用 nginx 没问题——但 SSE 不能被缓冲：

```nginx
location /api/ {
    proxy_pass         http://127.0.0.1:5000;
    proxy_http_version 1.1;
    proxy_set_header   Host $host;
    proxy_set_header   Connection "";
    proxy_buffering    off;            # 服务自身也会发 X-Accel-Buffering: no
    proxy_read_timeout 3600s;          # /api/clip/events 是长连接
    proxy_send_timeout 3600s;
    chunked_transfer_encoding on;
}
```

事件流每 15 秒发一条 `: ping` 注释，避免空闲代理和负载均衡器断连。客户端重连时会通过 `Last-Event-ID` 补发错过的消息。

TLS 在代理层终止——服务本身按设计只提供明文 HTTP。

---

## 6. 安全

对外暴露要有意识地做选择：

- `/api/clip/*` **没有鉴权**：能访问该端口的人都能开始/停止录音、读取转写和回复。
- `flask_cors` 对**所有**来源开启，任意站点的网页都能调用这个 API。请把它当作仅限内网的服务。
- 默认 `--host 0.0.0.0` 会绑定所有网卡。客户端在同一台主机时用 `--host 127.0.0.1`，或用防火墙把端口限制在你的网段内。
- 用专用的非特权用户运行；`.env` 保持 `chmod 600`。
- 需要远程访问时，放在代理后面并启用 TLS **和**你自己的鉴权（mTLS、鉴权代理、WireGuard），不要直接裸露。

---

## 7. 选哪种安装方式？

| | `--source <checkout>` | pip 包（默认） |
| --- | --- | --- |
| 安装内容 | `requirements.txt`——仓库的精确锁定版本，含来自 RTC 分支的 BLE SDK | `respeaker-clip-service` 及其 `[clip]` extra |
| 当前可用 | 可用 | 仅当 PyPI 上已有 wheel，或你自己提供 git spec |
| 适用场景 | 当前的生产部署，以及任何依赖锁定 SDK 的场景 | 自己发布该 Python 包的发行渠道 |

在 Python 包有 PyPI 发布之前，请用 `--source` 部署。等价的手动安装：

```bash
pip install "respeaker-clip-service[clip] @ git+https://github.com/KasunThushara/reSpeaker-Clip-AI-Agent.git"
```

从 pip 安装运行时**只提供 API**——wheel 里不含 `frontend/`，此时 `GET /` 返回端点索引 JSON 而不是 Web 界面。

---

## 8. 验证部署

```bash
respeaker-clip status                                  # 0 已连接 · 3 Clip 离线 · 1 不可达
curl -s http://127.0.0.1:5000/api/clip/status | jq
curl -sN http://127.0.0.1:5000/api/clip/events | head -20
```

API 正常且 Clip 在线时大致是：

```json
{ "connected": true, "device_id": "F0:FB:BF:05:FD:EB", "recording": false,
  "rtc_phase": "paused", "rtc_session": "20260920101234",
  "rtc_utterance_id": 4, "rtc_processing": false, "input_mode": "clip" }
```

事件流先发一条快照事件，随后是 `rtc_state`、`transcript`（先滚动临时结果，再一条 `final`）、`thinking`、`token` 和 `result`：

```
event: connection
data: {"type": "connection", "connected": true, "status": {...}}

id: 12
event: rtc_state
data: {"phase": "capturing", "utterance_id": 4, "trigger": "device"}
```

语音链路的端到端冒烟测试：双击 Clip（或先 `curl -XPOST .../api/clip/stream/resume`，再 `.../stream/pause`），观察是否出现一条 `final` 转写，随后跟一条 `result`。

---

## 9. 升级与状态

```bash
sudo systemctl stop respeaker-clip
git -C /opt/reSpeaker-Clip-AI-Agent pull            # 或：respeaker-clip serve --pip-spec '<新 spec>'
sudo systemctl start respeaker-clip                 # --source 模式下会重新执行 pip install
```

- 每次 `serve` 都会重跑 pip；`--skip-install` 可跳过。想彻底重建：`rm -rf /opt/respeaker-clip/venv`。
- 需要备份或清理的状态：`.env`、`chat.db`（SQLite 回退库）、`clip_audio/`（下载的会话），以及 venv（可随时丢弃）。
- `systemctl restart` 是安全的：关闭时运行时会取消任务、向设备发送 best-effort STOP（让设备停止串流）、中止 RTC 会话并关闭 BLE。注意它**不会**把正在处理中的那段话 finalize——若你需要那条转写，请先让它跑完（或 `POST /api/clip/stream/pause`）再重启。

---

## 10. 故障排查

| 现象 | 原因 / 处理 |
| --- | --- |
| `503 {"error": "Clip runtime is not enabled on this server"}` | 用了 `--no-clip`，或 `--input-mode browser`。改用 `--input-mode clip` 重启 |
| `503`，`last_error: BLE discovery connect failed` | Bleak 连不到 BlueZ：适配器未开（`bluetoothctl power on`）、`CLIP_BLE_ADDRESS` 不对、设备未配对，或服务用户缺少 D-Bus/polkit 权限（§4） |
| 主机休眠后 `connected: false` | 在 supervisor 重连成功前属正常（退避上限 30 秒）；这期间 API 仍可用 |
| `env file not found: …` | `--env-file` 指向的文件不存在；去掉该参数即使用 `./.env` |
| `.env` 里的 key「没生效」 | 文件必须在**工作目录**下（systemd 用 `WorkingDirectory=`）——或传 `--env-file`，或把这些变量放进真实环境 |
| `python3 -m venv` 报错 | 未安装 `python3-venv`（Debian/Ubuntu） |
| pip 卡住 / 因代理失败 | 为服务用户设置 `HTTP_PROXY`/`HTTPS_PROXY`；venv 里的 pip 不会继承你 shell 的代理 |
| SSE 一次性才到达或超时 | 代理在缓冲：`proxy_buffering off` + 加长 `proxy_read_timeout`（§5） |
| 端口被占用 | 已有另一个实例在跑——`ss -ltnp \| grep 5000` 查一下；同时跑两个是不支持的（§4） |
| 语音识别正常但回复始终不来 | `GROQ_API_KEY` 缺失或额度用尽：查 `last_error` 与 `journalctl -u respeaker-clip` |

---

## 11. Docker（草案，未在仓库内验证）

难点在 BLE：容器需要宿主机的 D-Bus socket 和蓝牙访问权限，通常意味着 `--network host`、挂载 `/var/run/dbus/system_bus_socket`，并授予适配器访问权。再挂一个卷作为工作目录（`.env`、`chat.db`、`clip_audio/`），并用 `--source` 指向只读挂载的 checkout。鉴于与宿主机蓝牙的强耦合，用 systemd 直接在宿主机跑 CLI（§4）是更好的默认选择；只有当你已有成熟的宿主机蓝牙容器方案时再考虑容器化。