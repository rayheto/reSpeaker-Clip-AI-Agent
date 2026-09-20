# Deploying the Clip service (`respeaker-clip serve`)

How to run the reSpeaker Clip service as a long-lived process on a host with
Bluetooth — the thing `npx respeaker-clip serve` wraps.

[中文](./DEPLOYMENT.zh-cn.md)

**What actually runs:** a Flask process holding **one** BLE connection to one
Clip plus one `/api/clip` HTTP API (REST + SSE). The Node CLI is only the
installer/launcher: it creates a virtualenv, installs the Python service into
it, and `exec`s `python -m backend.service_cli`. Once the service is up you can
stop thinking about Node.

---

## 1. Prerequisites

| Requirement | Why | Check |
| --- | --- | --- |
| Node ≥ 18.17 | runs the CLI | `node --version` |
| Python ≥ 3.10 **with `venv`** | the service itself (Debian/Ubuntu: `sudo apt install python3-venv`) | `python3 -m venv --help` |
| Bluetooth host (Linux + BlueZ, or Windows) | the Clip talks BLE | `bluetoothctl power on` |
| A Clip, powered and paired | voice input | `bluetoothctl devices` |
| `GROQ_API_KEY` | LLM + Whisper STT + Orpheus TTS | see §3 |
| Free TCP port (default 5000) | the HTTP API | `ss -ltnp \| grep 5000` |

The service **starts and serves the API even when BLE is unavailable** — it
retries with backoff and reports `connected: false`. So you can deploy it before
the hardware is on site, and watch it come up with `respeaker-clip status`.

---

## 2. Deploy

```bash
# 1. install the CLI (or use npx for a one-off)
npm install -g respeaker-clip

# 2. check the host
respeaker-clip doctor

# 3. run it
sudo mkdir -p /opt/respeaker-clip            # state: venv, .env, chat.db, clip_audio/
sudo chown "$USER" /opt/respeaker-clip
cd /opt/respeaker-clip
respeaker-clip serve --source /opt/reSpeaker-Clip-AI-Agent
```

`serve` resolves what to run and where to keep state:

| | Default | Override |
| --- | --- | --- |
| Service source | pip spec `respeaker-clip-service` | `--source <checkout>` (recommended today — see §7) |
| Virtualenv | `$RESPEAKER_CLIP_HOME/venv`, else `~/.cache/respeaker-clip/venv` | `--venv <dir>` |
| Working directory | the directory you run the CLI from | — |

The working directory matters: the SQLite fallback (`chat.db`), downloaded
audio (`clip_audio/`) and `.env` are all resolved from it.

First run creates the venv and pip-installs (takes a few minutes for
`sentence-transformers`). Later runs reuse it; add `--skip-install` to never
touch pip again.

### Flags

| Flag | Default | Meaning |
| --- | --- | --- |
| `--host` / `--port` | `0.0.0.0` / `5000` | Bind address — see §6, the API has **no auth** |
| `--input-mode` | `clip` | `clip` (device only) \| `browser` (system mic) \| `both` |
| `--ble-address` / `--ble-name` | from env | Pin the device, or scan by name |
| `--no-clip` | off | Serve the API without the BLE runtime (no voice input) |
| `--env-file` | `./.env` | Configuration file (see §3) |
| `--venv` / `--pip-spec` / `--skip-install` | see above | Install behaviour |
| `-- <args…>` | — | Anything else forwarded to the service verbatim |

CLI environment variables: `RESPEAKER_CLIP_HOME`, `RESPEAKER_CLIP_SERVICE_ROOT`,
`RESPEAKER_CLIP_PIP_SPEC`, `RESPEAKER_CLIP_PYTHON`, `RESPEAKER_CLIP_ENV_FILE`,
`RESPEAKER_CLIP_BASE_URL` (for `status`).

---

## 3. Configuration

The service reads a `.env` from the **working directory** (or `--env-file`).
Real environment variables always win, so systemd `EnvironmentFile=` and
container env override the file.

```bash
cp /opt/reSpeaker-Clip-AI-Agent/.env.example .env
chmod 600 .env                     # it holds API keys
# edit: GROQ_API_KEY, CLIP_BLE_ADDRESS or CLIP_BLE_NAME
```

Minimum to be useful:

```ini
GROQ_API_KEY=gsk_...
CLIP_BLE_ADDRESS=F0:FB:BF:05:FD:EB     # or CLIP_BLE_NAME=Clip to scan
VOICE_INPUT_MODE=clip
```

Everything installed default that matters in production:

| Variable | Default | Notes |
| --- | --- | --- |
| `RTC_AUTO_ARM` | `true` | Arms the warm-pause RTC session on connect — keep on for the live voice path |
| `RTC_ARM_TIMEOUT` | `15` | Seconds to wait for the stream before re-arming |
| `RTC_PARTIAL_INTERVAL` | `2.0` | Rolling partial-transcript cadence |
| `GROQ_RTC_PARTIAL_MODEL` / `GROQ_RTC_FINAL_MODEL` | `whisper-large-v3-turbo` / `whisper-large-v3` | Cheap rolling partials, accurate final pass |
| `DATABASE_URL` | `sqlite:///chat.db` | Supabase URL if you use it (falls back to SQLite when its tables are missing) |
| `CLIP_TEMP_DIR` | `clip_audio` | Downloaded session audio; size it, or clean it |
| `CLIP_DOWNLOAD_TIMEOUT` | `300` | Per-session download timeout |

Confirm what the service actually resolved:

```bash
python3 -m backend.service_cli --print-config     # from a checkout
respeaker-clip serve --dry-run                    # what the CLI will execute
```

---

## 4. Run it under systemd (Linux)

`/etc/systemd/system/respeaker-clip.service`:

```ini
[Unit]
Description=reSpeaker Clip voice service
After=network-online.target bluetooth.target
Wants=network-online.target bluetooth.target

[Service]
Type=simple
User=clip                                    # a user that can reach BlueZ (see below)
WorkingDirectory=/opt/respeaker-clip
Environment=RESPEAKER_CLIP_HOME=/opt/respeaker-clip
EnvironmentFile=/opt/respeaker-clip/.env     # optional; real env beats .env anyway
ExecStart=/usr/local/bin/respeaker-clip serve --source /opt/reSpeaker-Clip-AI-Agent
Restart=always
RestartSec=10                                # the runtime reconnects on its own; this covers a crash
TimeoutStopSec=20                            # lets the runtime close BLE + finish writes
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

**BLE permissions.** The service needs D-Bus access to BlueZ, so the service
user must be allowed to talk to it — a bare system account usually is not:

- simplest: run it as the desktop user that already paired the Clip, or
- add the service user to the `bluetooth` group and add a polkit rule for
  `org.bluez`, or
- on a headless box, `bluetoothctl power on` once as that user and confirm
  `respeaker-clip status` shows the device.

If BLE is refused you will still get a healthy HTTP API and `connected: false`
with the error in `last_error` — an easy thing to misread as "service is up".

**One process, always.** The runtime owns exactly one BLE connection per
process and the reloader is intentionally off. Never run two instances
(including a stray `python app.py` alongside the unit), and never give the
service multiple workers.

---

## 5. Behind a reverse proxy

The API is plain HTTP + SSE, so nginx works — but SSE must not be buffered:

```nginx
location /api/ {
    proxy_pass         http://127.0.0.1:5000;
    proxy_http_version 1.1;
    proxy_set_header   Host $host;
    proxy_set_header   Connection "";
    proxy_buffering    off;            # the service also sends X-Accel-Buffering: no
    proxy_read_timeout 3600s;          # /api/clip/events is long-lived
    proxy_send_timeout 3600s;
    chunked_transfer_encoding on;
}
```

The event stream sends a `: ping` comment every 15 s, which keeps idle proxies
and load balancers from dropping it. A client that reconnects gets missed
events replayed through `Last-Event-ID`.

TLS terminates at the proxy — the service serves plain HTTP by design.

---

## 6. Security

Be deliberate about exposure:

- **There is no authentication** on `/api/clip/*`: anyone who can reach the port
  can start/stop recordings, stream transcripts and read replies.
- `flask_cors` is enabled for **all** origins, so a browser page on any site can
  call the API. Treat it as a LAN-only service.
- Default `--host 0.0.0.0` binds every interface. Use `--host 127.0.0.1` when the
  client is on the same host, or firewall the port to your subnet.
- Run as a dedicated unprivileged user; keep `.env` `chmod 600`.
- For remote access, put it behind the proxy with TLS **and** your own auth
  (mTLS, an auth proxy, WireGuard) rather than exposing it directly.

---

## 7. Which install mode?

| | `--source <checkout>` | pip spec (default) |
| --- | --- | --- |
| Installs | `requirements.txt` — the repo's exact pins, including the BLE SDK from the RTC-capable git fork | `respeaker-clip-service` plus its `[clip]` extra |
| Works today | yes | only once a wheel exists on PyPI or via a git spec you provide |
| Best for | production today, and anything that needs the pinned SDK | distributions that publish the Python package themselves |

Until the Python package has a PyPI release, deploy with `--source`. Equivalent
manual install:

```bash
pip install "respeaker-clip-service[clip] @ git+https://github.com/KasunThushara/reSpeaker-Clip-AI-Agent.git"
```

Running from a pip install serves the **API only** — `frontend/` is not in the
wheel, and `GET /` then returns a JSON index of endpoints instead of the web UI.

---

## 8. Verify the deployment

```bash
respeaker-clip status                                  # 0 connected · 3 Clip offline · 1 unreachable
curl -s http://127.0.0.1:5000/api/clip/status | jq
curl -sN http://127.0.0.1:5000/api/clip/events | head -20
```

A healthy API with a live Clip looks like:

```json
{ "connected": true, "device_id": "F0:FB:BF:05:FD:EB", "recording": false,
  "rtc_phase": "paused", "rtc_session": "20260920101234",
  "rtc_utterance_id": 4, "rtc_processing": false, "input_mode": "clip" }
```

The event stream opens with a snapshot event and then delivers
`rtc_state`, `transcript` (rolling partials then one `final`), `thinking`,
`token` and `result` frames:

```
event: connection
data: {"type": "connection", "connected": true, "status": {...}}

id: 12
event: rtc_state
data: {"phase": "capturing", "utterance_id": 4, "trigger": "device"}
```

End-to-end smoke test of the voice path: double-click the Clip (or
`curl -XPOST .../api/clip/stream/resume` then `.../stream/pause`) and watch for
one `final` transcript followed by `result`.

---

## 9. Upgrading and state

```bash
sudo systemctl stop respeaker-clip
git -C /opt/reSpeaker-Clip-AI-Agent pull            # or: respeaker-clip serve --pip-spec '<new spec>'
sudo systemctl start respeaker-clip                 # re-runs pip install for --source mode
```

- Pip is re-run on every `serve`; `--skip-install` skips it. To rebuild from
  scratch: `rm -rf /opt/respeaker-clip/venv`.
- State to back up or clean: `.env`, `chat.db` (SQLite fallback),
  `clip_audio/` (downloaded sessions), and the venv (disposable).
- `systemctl restart` is safe: on shutdown the runtime cancels its jobs, sends a
  best-effort STOP so the device stops streaming, aborts the RTC session and
  closes BLE. Note it does **not** finalize an utterance that is mid-flight — if
  you need that transcript, let it finish (or `POST /api/clip/stream/pause`)
  before restarting.

---

## 10. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `503 {"error": "Clip runtime is not enabled on this server"}` | Started with `--no-clip`, or `--input-mode browser`. Restart with `--input-mode clip` |
| `503`, `last_error: BLE discovery connect failed` | Bleak can't reach BlueZ: adapter off (`bluetoothctl power on`), wrong `CLIP_BLE_ADDRESS`, unpaired device, or the service user lacks D-Bus/polkit access (§4) |
| `connected: false` after the host slept | Expected until the supervisor reconnects (backoff caps at 30 s); the API stays up meanwhile |
| `env file not found: …` | `--env-file` points at a missing path; omit it to use `./.env` |
| Keys "not picked up" from `.env` | The file must be in the **working directory** (`WorkingDirectory=` under systemd) — or pass `--env-file`, or put the variables in real env |
| `python3 -m venv` fails | `python3-venv` not installed (Debian/Ubuntu) |
| pip hangs / fails behind a proxy | Set `HTTP_PROXY`/`HTTPS_PROXY` for the service user; pip in a venv does not inherit your shell |
| SSE arrives all at once or times out | A proxy is buffering: `proxy_buffering off` + long `proxy_read_timeout` (§5) |
| Port already in use | Another instance is running — check `ss -ltnp \| grep 5000`; running two is not supported (§4) |
| Voice works but the answer never arrives | `GROQ_API_KEY` missing/quota: check `last_error` and `journalctl -u respeaker-clip` |

---

## 11. Docker (sketch, not validated here)

BLE is the hard part: the container needs the host D-Bus socket and Bluetooth
access, which usually means `--network host`, mounting
`/var/run/dbus/system_bus_socket`, and granting access to the adapter. Mount a
volume for the working directory (`.env`, `chat.db`, `clip_audio/`) and use
`--source` with the checkout mounted read-only. Given the coupling to host
Bluetooth, running the CLI under systemd on the host (§4) is the better default;
containerise only if you already have host-Bluetooth patterns in place.