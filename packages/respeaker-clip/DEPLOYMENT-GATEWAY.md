# Deploying the Clip service as a device gateway

A standalone guide to running the Clip service **without the AI agent** — the
Clip becomes a voice input for *your* ASR/agent, and this service only runs the
device and hands you audio.

```bash
respeaker-clip serve --source /opt/reSpeaker-Clip-AI-Agent --no-agent
```

[中文](./DEPLOYMENT-GATEWAY.zh-cn.md) · [Full deployment guide (agent mode)](./DEPLOYMENT.md)

---

## 1. What you are deploying

One Flask process holding one BLE connection to one Clip, exposing `/api/clip/*`
plus `/api/health`. In this mode it:

- **does not import** the agent stack (LangGraph, Groq, Mem0, Pinecone, the
  conversation store) — no startup call to Pinecone or Supabase conversations;
- **does not transcribe** and does not answer: each finalized utterance and each
  downloaded SD session is re-containerized to Ogg, kept on disk, and announced
  over SSE with a URL you fetch;
- serves **no** `/api/chat`, `/api/voice`, `/api/tts`, `/api/composio` (404), and
  `GET /` returns a JSON endpoint index instead of the chat UI.

## 2. Prerequisites

| Requirement | Why | Check |
| --- | --- | --- |
| Node ≥ 18.17 | runs the CLI | `node --version` |
| Python ≥ 3.10 with `venv` | the service (Debian/Ubuntu: `sudo apt install python3-venv`) | `python3 -m venv --help` |
| Bluetooth host (Linux + BlueZ, or Windows) | the Clip talks BLE | `bluetoothctl power on` |
| A Clip, powered and paired | voice input | `bluetoothctl devices` |
| Free TCP port (default 5000) | the HTTP API | `ss -ltnp \| grep 5000` |

**No `GROQ_API_KEY`** — that is the point of the mode. Tavily/FMP/Pinecone/
Supabase/Composio keys are likewise unnecessary: whatever you leave in `.env` is
simply never read by this process.

## 3. Install and run

```bash
npm install -g respeaker-clip

sudo mkdir -p /opt/respeaker-clip && sudo chown "$USER" /opt/respeaker-clip
cd /opt/respeaker-clip

respeaker-clip doctor                                        # node/python/venv/bluez
respeaker-clip serve --source /opt/reSpeaker-Clip-AI-Agent --no-agent
```

The working directory is where the service keeps `.env`, `chat.db` (Clip
ingestion bookkeeping) and `clip_audio/` — the retained audio.

| Flag | Default | Meaning in this mode |
| --- | --- | --- |
| `--no-agent` | off | **Required** for gateway mode |
| `--source <dir>` | pip spec `respeaker-clip-service` | Run from a checkout (recommended, §7 of the full guide) |
| `--venv <dir>` | `$RESPEAKER_CLIP_HOME/venv` | Virtualenv location |
| `--pip-spec <spec>` / `--skip-install` | — | Install behaviour |
| `--host` / `--port` | `0.0.0.0` / `5000` | Bind address — see §10 |
| `--input-mode` | `clip` | Only `clip`/`both` are accepted; `both` is narrowed to `clip` |
| `--ble-address` / `--ble-name` | from env | Pin the device, or scan by name |
| `--env-file <path>` | `./.env` | Configuration file |

Rejected combinations: `--no-agent --no-clip` (nothing would be served) and
`--no-agent --input-mode browser` (browser voice needs the agent).

## 4. Configuration

```bash
cat > .env <<'EOF'
CLIP_BLE_ADDRESS=F0:FB:BF:05:FD:EE     # or CLIP_BLE_NAME=Clip to scan
CLIP_TEMP_DIR=clip_audio               # where retained audio lands
EOF
```

`AGENT_ENABLED=false` in the environment (or `--no-agent`) is what selects this
mode; `--no-agent` sets it. The rest of `.env` is the device/runtime set:

| Variable | Default | Notes |
| --- | --- | --- |
| `CLIP_BLE_ADDRESS` / `CLIP_BLE_NAME` | — / `Clip` | Pin or scan |
| `CLIP_TEMP_DIR` | `clip_audio` | **Retained audio lives here** (§6) |
| `CLIP_RECORD_MODE` | `enhanced` | Legacy SD recording quality |
| `CLIP_DOWNLOAD_TIMEOUT` | `300` | Per-session download timeout (s) |
| `RTC_AUTO_ARM` | `true` | Arm the warm-pause session on connect — keep on for low-latency utterances |
| `RTC_MIN_UTTERANCE_FRAMES` | `25` | Shorter utterances arrive as `skipped: too short` |
| `RTC_PARTIAL_INTERVAL` | `2.0` | Ignored here: rolling partials only feed STT, which this mode does not run |
| `RTC_MAX_UTTERANCE_FRAMES` / `RTC_PRE_ROLL_FRAMES` | `180000` / `15` | Utterance buffering bounds |
| `DATABASE_URL` | `sqlite:///chat.db` | Clip ingestion bookkeeping (Supabase if configured, SQLite otherwise) |

Confirm what the process resolved:

```bash
respeaker-clip serve --dry-run              # the exact command the CLI runs
python3 -m backend.service_cli --no-agent --print-config
# → {"input_mode": "clip", "agent_enabled": false, ...}
```

## 5. Run it under systemd

`/etc/systemd/system/respeaker-clip.service`:

```ini
[Unit]
Description=reSpeaker Clip device gateway
After=network-online.target bluetooth.target
Wants=network-online.target bluetooth.target

[Service]
Type=simple
User=clip                                   # must be able to reach BlueZ
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

**BLE permissions.** The service needs D-Bus access to BlueZ, which a bare
system account usually lacks: run it as the desktop user that paired the Clip, or
add the service user to the `bluetooth` group and grant an `org.bluez` polkit
rule. If BLE is refused the API stays healthy and `last_error` explains it.

**One process only.** One BLE connection per process, and the Flask reloader is
deliberately off — never run a second instance or a multi-worker configuration.

## 6. Consuming the audio

### Events

`GET /api/clip/events` (SSE) emits, in this mode:

```
event: connection      data: {"connected": true, "status": {...}}
event: rtc_state       data: {"phase": "arming|paused|capturing|finalizing|stopped|disconnected", "utterance_id": 4, "trigger": "device|web"}
event: recording       data: {"action": "started|stopped", "session": "20260920101234", "trigger": "physical|web"}
event: workflow        data: {"status": "stopped|downloading|processing|completed|failed", "session": "...", "mode": "exchange"}
event: utterance_audio data: {"utterance_id": 4, "session": "20260920101234", "url": "/api/clip/utterances/20260920101234/4/audio", "bytes": 41216, "content_type": "audio/ogg", "trigger": "device"}
event: session_audio   data: {"session": "20260920101234", "url": "/api/clip/sessions/20260920101234/audio", "bytes": 80128, "content_type": "audio/ogg", "trigger": "physical"}
```

`utterance_audio` marks the end of one spoken utterance. An utterance too short
to keep arrives without a URL:

```
event: utterance_audio data: {"utterance_id": 5, "session": "...", "skipped": "too short"}
```

The stream sends a `: ping` comment every 15 s; a reconnecting client passes
`Last-Event-ID` to replay what it missed.

### Fetching

```bash
curl -o utterance.ogg http://127.0.0.1:5000/api/clip/utterances/20260920101234/4/audio
curl -o session.ogg   http://127.0.0.1:5000/api/clip/sessions/20260920101234/audio
```

| Endpoint | Returns |
| --- | --- |
| `GET /api/clip/utterances/<session_id>/<utterance_id>/audio` | `audio/ogg`, one utterance |
| `GET /api/clip/sessions/<session_id>/audio` | `audio/ogg`, one downloaded SD session |

These two routes read files only — they need no BLE link and answer even while
the Clip is offline. Missing audio is a `404 {"error": "no audio retained..."}`
(see §7); a malformed session id is `400`.

Format: Ogg Opus, 16 kHz mono for RTC utterances; SD sessions use the sample
rate/channels recorded in the device's `session.json`. Sent with
`Content-Disposition: inline`, so a browser plays it directly.

### SDK

```js
import { ClipClient } from 'respeaker-clip';

const clip = new ClipClient({ baseUrl: 'http://localhost:5000' });

clip.subscribe({
  onUtteranceAudio: async (event) => {
    if (!event.url) return;                       // skipped
    const ogg = await clip.utteranceAudio(event.session, event.utterance_id);
    await myOwnStt(ogg);                          // your pipeline from here
  },
  onSessionAudio: async (event) => {
    const ogg = await clip.sessionAudio(event.session);
    await myOwnStt(ogg);
  },
  onError: (err) => console.warn('clip stream:', err.code, err.message),
});

await clip.streamResume();    // or double-click the device
// …speak…
await clip.streamPause();     // one utterance_audio follows
```

Without the SDK, `POST /api/clip/stream/resume` and `.../pause` drive the same
utterances.

### Which input path produces what

| Path | How it starts | Event |
| --- | --- | --- |
| RTC warm pause (default) | device double-click, or `stream/resume` + `stream/pause` | `utterance_audio` |
| SD recording | `POST /api/clip/recordings/start` / `stop` | `workflow` → `session_audio` |

While an RTC session is armed it owns the single frame channel and legacy
recording is refused with `409` — if you want the SD path, set
`RTC_AUTO_ARM=false`.

## 7. Retention and disk

Unlike the agent path — which deletes session audio after transcribing — gateway
mode **keeps every file**, because the audio *is* the deliverable:

```
clip_audio/<session_id>.ogg                 downloaded SD sessions
clip_audio/<session_id>/…                   their raw packets + session.json
clip_audio/rtc/<session_id>/<utterance>.ogg RTC utterances
```

So `CLIP_TEMP_DIR` grows with use, and pruning is your job:

- size it from the device: `/api/clip/status` reports `bitrate` and
  `free_space_mb` once connected, and every event carries the exact `bytes`;
- both the Ogg files *and* the raw packet directories are retained — prune both;
- deleting files is safe at any time — the URL simply starts answering `404`, and
  the event stream is never rewound;
- a simple cron keeps it bounded:

  ```bash
  find /opt/respeaker-clip/clip_audio -type f -mtime +7 -delete
  find /opt/respeaker-clip/clip_audio -type d -empty -delete
  ```

Never delete `chat.db` for space: it holds the ingestion bookkeeping that keeps
retries idempotent.

## 8. Behind a reverse proxy

```nginx
location /api/ {
    proxy_pass         http://127.0.0.1:5000;
    proxy_http_version 1.1;
    proxy_set_header   Host $host;
    proxy_set_header   Connection "";
    proxy_buffering    off;            # required for /api/clip/events
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
```

The service already sends `X-Accel-Buffering: no` on the event stream, and pings
every 15 s. If you split locations, note that only `/api/clip/events` is
long-lived — the two audio routes are ordinary small file GETs.

## 9. Verify

```bash
respeaker-clip status --base-url http://127.0.0.1:5000
#   input mode    : clip
#   agent mode    : off (device gateway)      ← the mode check
#   connected     : yes · rtc phase : paused

curl -s http://127.0.0.1:5000/ | jq .reason   # "agent disabled: device gateway"
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5000/api/chat   # 404
curl -sN http://127.0.0.1:5000/api/clip/events | head -20
```

End-to-end: double-click the Clip (or `curl -XPOST .../api/clip/stream/resume`,
speak, then `.../stream/pause`) and watch for exactly one `utterance_audio`, then
fetch its URL and confirm the file plays.

`respeaker-clip status` exits `0` when the Clip is connected, `3` when the service
answers but the device is offline, `1` on transport errors — usable in a health
check.

## 10. Security

- `/api/clip/*` has **no authentication**, and CORS is open to all origins: any
  page a user visits can drive the device and download its audio. Keep it on your
  LAN; use `--host 127.0.0.1` when the consumer is local.
- **The audio is raw voice.** The default `0.0.0.0` bind plus retained files means
  anyone on the network can fetch recordings — restrict by firewall, and treat
  `clip_audio/` with the same care as any recording store.
- Run under a dedicated unprivileged user; `chmod 700` the working directory.
- For remote consumers, terminate TLS *and* add your own auth (mTLS, an auth
  proxy, WireGuard) rather than exposing the port.

## 11. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `/api/chat` works, `agent mode` says `on` | `--no-agent` was not passed and `AGENT_ENABLED=false` is not in the environment |
| Service exits: `--no-agent with --no-clip would serve nothing` | Drop `--no-clip` |
| `--no-agent cannot serve browser voice input` | `VOICE_INPUT_MODE`/`--input-mode` is `browser`; use `clip` |
| Nothing at all on the event stream | The stream only reports; utterances start on a device double-click or `stream/resume` |
| `utterance_audio` with `skipped: too short` | Below `RTC_MIN_UTTERANCE_FRAMES` (25 ≈ 0.5 s); lower it for very short commands |
| `409` from `/recordings/start` | An RTC session owns the frame channel; set `RTC_AUTO_ARM=false` to use SD recording |
| `404 no audio retained…` | The file was pruned, or the utterance was skipped — not an error state |
| Disk filling up | Expected without pruning: see §7 |
| `503 Clip runtime is not enabled` | Started with `--no-clip`; the gateway needs the runtime |
| BLE errors in `last_error` | §5 permissions / adapter on / pairing; the API stays up meanwhile |