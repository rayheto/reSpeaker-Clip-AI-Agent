# 把 Clip 服务部署为设备网关

一份独立指南：**不带 AI agent** 运行 Clip 服务——把 Clip 当作*你自己的* ASR/agent 的语音输入，本服务只负责设备本身并把音频交给你。

```bash
respeaker-clip serve --source /opt/reSpeaker-Clip-AI-Agent --no-agent
```

[English](./DEPLOYMENT-GATEWAY.md) · [完整部署文档（agent 模式）](./DEPLOYMENT.zh-cn.md)

---

## 1. 你部署的是什么

一个 Flask 进程，持有到一台 Clip 的一条 BLE 连接，对外提供 `/api/clip/*` 和 `/api/health`。在这个模式下它：

- **不导入** agent 技术栈（LangGraph、Groq、Mem0、Pinecone、对话存储）——启动时不会访问 Pinecone 或 Supabase 的对话数据；
- **不做转写**、不做回复：每段结束的 utterance 和每个下载完的 SD 会话都被重新封装为 Ogg、保留在磁盘上，并通过 SSE 带 URL 通知你去取；
- **不提供** `/api/chat`、`/api/voice`、`/api/tts`、`/api/composio`（404），`GET /` 返回端点索引 JSON 而不是聊天界面。

## 2. 前置条件

| 要求 | 原因 | 检查 |
| --- | --- | --- |
| Node ≥ 18.17 | 运行 CLI | `node --version` |
| Python ≥ 3.10 且带 `venv` | 服务本体（Debian/Ubuntu：`sudo apt install python3-venv`） | `python3 -m venv --help` |
| 蓝牙主机（Linux + BlueZ，或 Windows） | Clip 走 BLE | `bluetoothctl power on` |
| 一个已上电、已配对的 Clip | 语音输入 | `bluetoothctl devices` |
| 空闲 TCP 端口（默认 5000） | HTTP API | `ss -ltnp \| grep 5000` |

**不需要 `GROQ_API_KEY`**——这正是该模式的意义。Tavily/FMP/Pinecone/Supabase/Composio 的 key 同样不需要：`.env` 里留着什么，这个进程都不会去读。

## 3. 安装与运行

```bash
npm install -g respeaker-clip

sudo mkdir -p /opt/respeaker-clip && sudo chown "$USER" /opt/respeaker-clip
cd /opt/respeaker-clip

respeaker-clip doctor                                        # node/python/venv/bluez
respeaker-clip serve --source /opt/reSpeaker-Clip-AI-Agent --no-agent
```

工作目录决定 `.env`、`chat.db`（Clip 入库记账）和 `clip_audio/`（保留的音频）存放位置。

| 参数 | 默认值 | 在该模式下的含义 |
| --- | --- | --- |
| `--no-agent` | 关 | **本模式必需** |
| `--source <dir>` | pip 包 `respeaker-clip-service` | 从 checkout 运行（推荐，见完整文档 §7） |
| `--venv <dir>` | `$RESPEAKER_CLIP_HOME/venv` | 虚拟环境位置 |
| `--pip-spec <spec>` / `--skip-install` | — | 安装行为 |
| `--host` / `--port` | `0.0.0.0` / `5000` | 绑定地址——见 §10 |
| `--input-mode` | `clip` | 只接受 `clip`/`both`；`both` 会被收窄为 `clip` |
| `--ble-address` / `--ble-name` | 取自环境变量 | 固定设备，或按名称扫描 |
| `--env-file <path>` | `./.env` | 配置文件 |

会被拒绝的组合：`--no-agent --no-clip`（什么都不提供）、`--no-agent --input-mode browser`（浏览器语音需要 agent）。

## 4. 配置

```bash
cat > .env <<'EOF'
CLIP_BLE_ADDRESS=F0:FB:BF:05:FD:EE     # 或 CLIP_BLE_NAME=Clip 走扫描
CLIP_TEMP_DIR=clip_audio               # 保留音频的落盘位置
EOF
```

环境里的 `AGENT_ENABLED=false`（或 `--no-agent`）就是选择本模式的方式；`--no-agent` 会设置它。`.env` 其余部分是设备/运行时相关配置：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CLIP_BLE_ADDRESS` / `CLIP_BLE_NAME` | — / `Clip` | 固定或扫描 |
| `CLIP_TEMP_DIR` | `clip_audio` | **保留音频就放在这里**（§7） |
| `CLIP_RECORD_MODE` | `enhanced` | 传统 SD 录音质量 |
| `CLIP_DOWNLOAD_TIMEOUT` | `300` | 单会话下载超时（秒） |
| `RTC_AUTO_ARM` | `true` | 连接后自动武装温暂停会话——要保持低延迟 utterance 就开着 |
| `RTC_MIN_UTTERANCE_FRAMES` | `25` | 更短的 utterance 会以 `skipped: too short` 到达 |
| `RTC_PARTIAL_INTERVAL` | `2.0` | 本模式下无效：滚动临时转写只服务 STT，而本模式不做 STT |
| `RTC_MAX_UTTERANCE_FRAMES` / `RTC_PRE_ROLL_FRAMES` | `180000` / `15` | utterance 缓冲上限 |
| `DATABASE_URL` | `sqlite:///chat.db` | Clip 入库记账（配了 Supabase 用它，否则 SQLite） |

确认进程实际解析到的配置：

```bash
respeaker-clip serve --dry-run              # CLI 将要执行的完整命令
python3 -m backend.service_cli --no-agent --print-config
# → {"input_mode": "clip", "agent_enabled": false, ...}
```

## 5. 用 systemd 运行

`/etc/systemd/system/respeaker-clip.service`：

```ini
[Unit]
Description=reSpeaker Clip device gateway
After=network-online.target bluetooth.target
Wants=network-online.target bluetooth.target

[Service]
Type=simple
User=clip                                   # 必须能访问 BlueZ
WorkingDirectory=/opt/respeaker-clip
Environment=RESPEAKER_CLIP_HOME=/opt/respeaker-clip
EnvironmentFile=/opt/respeaker-clip/.env
ExecStart=/usr/local/bin/respeaker-clip serve --source /opt/reSpeaker-Clip-AI-Agent --no-agent
Restart=always
RestartSec=10
TimeoutStopSec=20
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now respeaker-clip
journalctl -u respeaker-clip -f
```

**BLE 权限。** 服务需要通过 D-Bus 访问 BlueZ，裸的系统账号通常没有这个权限：用已配对过 Clip 的桌面用户来跑，或把服务用户加入 `bluetooth` 组并为 `org.bluez` 添加 polkit 规则。BLE 被拒绝时 API 仍然健康，原因写在 `last_error` 里。

**只能有一个进程。** 每进程一条 BLE 连接，Flask reloader 也是刻意关闭的——不要跑第二个实例，也不要配多 worker。

## 6. 取用音频

### 事件

`GET /api/clip/events`（SSE）在本模式下会发出：

```
event: connection      data: {"connected": true, "status": {...}}
event: rtc_state       data: {"phase": "arming|paused|capturing|finalizing|stopped|disconnected", "utterance_id": 4, "trigger": "device|web"}
event: recording       data: {"action": "started|stopped", "session": "20260920101234", "trigger": "physical|web"}
event: workflow        data: {"status": "stopped|downloading|processing|completed|failed", "session": "...", "mode": "exchange"}
event: utterance_audio data: {"utterance_id": 4, "session": "20260920101234", "url": "/api/clip/utterances/20260920101234/4/audio", "bytes": 41216, "content_type": "audio/ogg", "trigger": "device"}
event: session_audio   data: {"session": "20260920101234", "url": "/api/clip/sessions/20260920101234/audio", "bytes": 80128, "content_type": "audio/ogg", "trigger": "physical"}
```

`utterance_audio` 表示一段话结束。太短而不保留的 utterance 到达时没有 URL：

```
event: utterance_audio data: {"utterance_id": 5, "session": "...", "skipped": "too short"}
```

事件流每 15 秒发一条 `: ping` 注释；客户端重连时带上 `Last-Event-ID` 即可补发错过的消息。

### 取文件

```bash
curl -o utterance.ogg http://127.0.0.1:5000/api/clip/utterances/20260920101234/4/audio
curl -o session.ogg   http://127.0.0.1:5000/api/clip/sessions/20260920101234/audio
```

| 端点 | 返回 |
| --- | --- |
| `GET /api/clip/utterances/<session_id>/<utterance_id>/audio` | `audio/ogg`，一段 utterance |
| `GET /api/clip/sessions/<session_id>/audio` | `audio/ogg`，一个下载完的 SD 会话 |

这两个路由只读文件——不需要 BLE 链路，Clip 离线时也能取。文件不存在时返回
`404 {"error": "no audio retained..."}`（见 §7）；session id 格式错误返回 `400`。

格式：Ogg Opus。RTC utterance 为 16 kHz 单声道；SD 会话使用设备 `session.json`
里记录的采样率/声道数。响应带 `Content-Disposition: inline`，浏览器可直接播放。

### SDK

```js
import { ClipClient } from 'respeaker-clip';

const clip = new ClipClient({ baseUrl: 'http://localhost:5000' });

clip.subscribe({
  onUtteranceAudio: async (event) => {
    if (!event.url) return;                       // 被跳过
    const ogg = await clip.utteranceAudio(event.session, event.utterance_id);
    await myOwnStt(ogg);                          // 之后交给你的管线
  },
  onSessionAudio: async (event) => {
    const ogg = await clip.sessionAudio(event.session);
    await myOwnStt(ogg);
  },
  onError: (err) => console.warn('clip stream:', err.code, err.message),
});

await clip.streamResume();    // 或双击设备
// …说话…
await clip.streamPause();     // 随后会有一条 utterance_audio
```

不用 SDK 时，`POST /api/clip/stream/resume` 与 `.../pause` 驱动同一套 utterance。

### 两条输入路径分别产出什么

| 路径 | 触发方式 | 事件 |
| --- | --- | --- |
| RTC 温暂停（默认） | 双击设备，或 `stream/resume` + `stream/pause` | `utterance_audio` |
| SD 录音 | `POST /api/clip/recordings/start` / `stop` | `workflow` → `session_audio` |

RTC 会话武装期间它独占唯一的帧通道，传统录音会被 `409` 拒绝——想用 SD 路径就设
`RTC_AUTO_ARM=false`。

## 7. 保留策略与磁盘

与 agent 路径不同（转写后会删除会话音频），网关模式**保留所有文件**，因为音频*就是*交付物：

```
clip_audio/<session_id>.ogg                 下载完的 SD 会话
clip_audio/<session_id>/…                   其原始数据包与 session.json
clip_audio/rtc/<session_id>/<utterance>.ogg RTC utterance
```

因此 `CLIP_TEMP_DIR` 会随使用增长，清理是你的责任：

- 按设备估算容量：连接后 `/api/clip/status` 会带 `bitrate` 与 `free_space_mb`，每个事件也带精确的 `bytes`；
- Ogg 文件**和**原始数据包目录都会被保留——两者都要清理；
- 随时删除文件都是安全的——对应 URL 只是开始返回 `404`，事件流不会被回退；
- 一条 cron 就能兜住：

  ```bash
  find /opt/respeaker-clip/clip_audio -type f -mtime +7 -delete
  find /opt/respeaker-clip/clip_audio -type d -empty -delete
  ```

不要为了省空间删 `chat.db`：它保存入库记账，重试幂等依赖它。

## 8. 反向代理

```nginx
location /api/ {
    proxy_pass         http://127.0.0.1:5000;
    proxy_http_version 1.1;
    proxy_set_header   Host $host;
    proxy_set_header   Connection "";
    proxy_buffering    off;            # /api/clip/events 必需
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
```

服务本身已在事件流上发送 `X-Accel-Buffering: no`，且每 15 秒 ping 一次。如果你拆分了 location，注意只有 `/api/clip/events` 是长连接——两个音频路由都是普通的小文件 GET。

## 9. 验证

```bash
respeaker-clip status --base-url http://127.0.0.1:5000
#   input mode    : clip
#   agent mode    : off (device gateway)      ← 模式确认
#   connected     : yes · rtc phase : paused

curl -s http://127.0.0.1:5000/ | jq .reason   # "agent disabled: device gateway"
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5000/api/chat   # 404
curl -sN http://127.0.0.1:5000/api/clip/events | head -20
```

端到端：双击 Clip（或 `curl -XPOST .../api/clip/stream/resume` → 说话 → `.../stream/pause`），
观察是否收到恰好一条 `utterance_audio`，再取它的 URL 确认文件能播放。

`respeaker-clip status` 的退出码：Clip 已连接 `0`，服务可用但设备离线 `3`，传输错误 `1`——可直接用于健康检查。

## 10. 安全

- `/api/clip/*` **没有鉴权**，且 CORS 对所有来源开放：用户访问的任意网页都能驱动设备并下载其音频。请限制在局域网内；消费端在本机时用 `--host 127.0.0.1`。
- **音频就是原始人声。** 默认绑定 `0.0.0.0` 加上文件长期保留，意味着同网段任何人都能取走录音——用防火墙限制，并把 `clip_audio/` 当作录音资料来对待。
- 用专用非特权用户运行；工作目录 `chmod 700`。
- 需要远程消费时，在代理层终止 TLS **并且**加上你自己的鉴权（mTLS、鉴权代理、WireGuard），不要直接暴露端口。

## 11. 故障排查

| 现象 | 原因 / 处理 |
| --- | --- |
| `/api/chat` 可用、`agent mode` 显示 `on` | 没有传 `--no-agent`，环境里也没有 `AGENT_ENABLED=false` |
| 启动退出：`--no-agent with --no-clip would serve nothing` | 去掉 `--no-clip` |
| `--no-agent cannot serve browser voice input` | `VOICE_INPUT_MODE`/`--input-mode` 是 `browser`，改成 `clip` |
| 事件流上什么都没有 | 事件流只负责通知；utterance 由双击设备或 `stream/resume` 触发 |
| `utterance_audio` 带 `skipped: too short` | 低于 `RTC_MIN_UTTERANCE_FRAMES`（25 ≈ 0.5 秒）；很短的指令可调低 |
| `/recordings/start` 返回 `409` | RTC 会话占用帧通道；要用 SD 录音就把 `RTC_AUTO_ARM=false` |
| `404 no audio retained…` | 文件已被清理，或该 utterance 被跳过——不是错误状态 |
| 磁盘被占满 | 不清理时的预期结果：见 §7 |
| `503 Clip runtime is not enabled` | 用了 `--no-clip`；网关需要运行时 |
| `last_error` 里是 BLE 错误 | 见 §5 权限 / 适配器是否开启 / 配对；这期间 API 仍可用 |