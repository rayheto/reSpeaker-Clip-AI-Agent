# respeaker-clip

Client SDK and service runner for the **reSpeaker Clip** voice service: the BLE
runtime, RTC live streaming ("warm pause" utterances) and the `/api/clip` HTTP
API exposed by the [reSpeaker Clip AI Agent](https://github.com/KasunThushara/reSpeaker-Clip-AI-Agent).

Two halves, one package:

| Entry point | What it is | Runs on |
| --- | --- | --- |
| `respeaker-clip` (default) | Typed client SDK — REST calls, SSE event stream, RTC utterance state machine | Browser, Node ≥ 18.17, edge workers |
| `respeaker-clip/service` + `respeaker-clip` CLI | Service runner — creates a virtualenv, installs the Python service, launches it | Node ≥ 18.17 + Python ≥ 3.10 |

The SDK has **no runtime dependencies**, works with the native `fetch`, and
parses SSE itself (no `EventSource` global needed).

## Install

```bash
npm install respeaker-clip

# run the service without cloning anything
npx respeaker-clip serve --source /path/to/reSpeaker-Clip-AI-Agent
```

## Quick start (SDK)

```js
import { ClipClient, RtcSessionController } from 'respeaker-clip';

const clip = new ClipClient({ baseUrl: 'http://localhost:5000' });
const rtc = new RtcSessionController(clip, {
  // A new utterance started: stop in-flight TTS so the user isn't drowned out.
  onUtteranceStart: () => stopSpeaking(),
});

const subscription = clip.subscribe({
  onConnection: (e) => setOnline(e.connected, e.error),
  onTranscript: (t) => showBubble(t.utterance_id, t.text, t.final),
  onThinking: (e) => setStatus(e.tool ? `using ${e.tool}…` : 'Thinking…'),
  onToken: (e) => appendToAnswer(e.text),
  onResult: (r) => finishAnswer(r.response, r.conversation_id),
  onError: (err) => console.warn('clip stream:', err.code, err.message),
});

await rtc.resume();  // start the next utterance (device double-click does the same)
await rtc.pause();   // warm-pause: finalize the utterance and answer it

subscription.close();
```

### The RTC warm-pause model

The service arms one RTC session when the device connects and keeps it armed.
While paused the device sends **no BLE audio frames** but its mic pipeline stays
warm, so resuming is low-latency. Every `RESUME → PAUSE` interval is one
**logical utterance**: `RtcSessionController` folds the event stream into a
renderable state machine — rolling partial transcripts, the authoritative final
transcript, the streamed answer — and hands you one `Utterance` object per id.

```js
rtc.snapshot;
// { phase, session, utteranceId, processing, streaming, streamingText,
//   tool, error, statusLine }

rtc.utterances;   // every utterance seen, oldest first
rtc.current;      // the one being captured/finalized
rtc.lastResult;   // result of an SD-session (non-utterance) reply
```

`phase` is one of `disconnected | arming | paused | capturing | finalizing | stopped`.
`describeRtcPhase(snapshot)` renders the same status lines the reference web UI uses.

### Device gateway mode (no agent)

If you only want the Clip as a voice input — and your own backend, ASR or
pipeline does the thinking — run the service with the agent switched off:

```bash
npx respeaker-clip serve --source /path/to/reSpeaker-Clip-AI-Agent --no-agent
```

In this mode the service is a **device gateway**:

- the agent stack (LangGraph, Groq, Mem0, Pinecone, the conversation store) is
  never imported or started, and **no `GROQ_API_KEY` is needed**;
- only `/api/health` and `/api/clip/*` are registered — no `/api/chat`,
  `/api/voice`, `/api/tts`, `/api/composio`;
- there is **no transcription and no LLM**: each finalized utterance and each
  downloaded session is re-containerized to Ogg, kept on disk and announced
  over SSE.

```js
import { ClipClient } from 'respeaker-clip';

const clip = new ClipClient({ baseUrl: 'http://localhost:5000' });

const sub = clip.subscribe({
  onUtteranceAudio: async (event) => {
    if (!event.url) return;                 // skipped: too short to keep
    const ogg = await clip.utteranceAudio(event.session, event.utterance_id);
    myOwnStt(ogg).then(myOwnAgent);
  },
  onSessionAudio: async (event) => {
    // A stopped SD recording finished downloading and is on disk.
    const ogg = await clip.sessionAudio(event.session);
    myOwnStt(ogg).then(myOwnAgent);
  },
});

await clip.streamResume();   // or double-click the device
// …speak…
await clip.streamPause();    // → one utterance_audio event
```

Events in this mode:

| Event | Payload |
| --- | --- |
| `utterance_audio` | `{ utterance_id, session, url, bytes, content_type, trigger }`, or `{ skipped: "too short" }` when nothing was kept |
| `session_audio` | `{ session, url, bytes, content_type, trigger }` |

Audio is fetched from the URLs in those events:

| Endpoint | Returns |
| --- | --- |
| `GET /api/clip/utterances/<session_id>/<utterance_id>/audio` | One utterance's Ogg |
| `GET /api/clip/sessions/<session_id>/audio` | A downloaded session's Ogg (404 once cleaned up) |

`clip.utteranceAudio(session, id)` / `clip.sessionAudio(session)` fetch the bytes
for you; `clip.utteranceAudioUrl(...)` / `clip.sessionAudioUrl(...)` return the
paths. `RtcSessionController` folds `utterance_audio` the same way it folds a
transcript result — the utterance gets `final: true` plus an `audioUrl`.

The agent path deletes session audio after transcribing; gateway mode **keeps
every file**, so `clip_audio/` (`CLIP_TEMP_DIR`) grows with use — clean it up on
your own schedule or point it at a volume you manage.

`--no-agent` refuses to combine with `--no-clip` (nothing would be served) and
with `--input-mode browser` (browser voice needs the agent).

Deploying this mode (systemd, retention, proxy, troubleshooting):
**[DEPLOYMENT-GATEWAY.md](./DEPLOYMENT-GATEWAY.md)** —
中文版 **[DEPLOYMENT-GATEWAY.zh-cn.md](./DEPLOYMENT-GATEWAY.zh-cn.md)**.

### REST methods

| Method | Endpoint |
| --- | --- |
| `getStatus()` | `GET /api/clip/status` |
| `startRecording({ mode, conversationId })` | `POST /api/clip/recordings/start` |
| `stopRecording()` | `POST /api/clip/recordings/stop` |
| `streamResume()` / `streamPause()` | `POST /api/clip/stream/resume` \| `/pause` |
| `ingest(sessionId, { trigger, conversationId })` | `POST /api/clip/sessions/<id>/ingest` |
| `registerContext(conversationId)` | `POST /api/clip/context` |
| `subscribe(handlers, options)` | `GET /api/clip/events` (SSE) |

Events: `connection`, `recording`, `workflow`, `result`, `rtc_state`,
`rtc_device`, `transcript`, `thinking`, `token`. Handler payloads are typed
(`ClipEventMap`); `onEvent` receives every event in order.

### Errors

Every failure is a `ClipApiError` carrying a `code`:

| `code` | Meaning |
| --- | --- |
| `bad_input` | 400 — malformed request (e.g. a session id that is not 14 digits) |
| `conflict` | 409 — state conflict (stopping when not recording, starting while RTC owns the stream) |
| `command_failed` | 502 — the device rejected or timed out on a command |
| `unavailable` | 503 — the Clip link is down/reconnecting (`error.isOffline`) |
| `network` / `timeout` / `aborted` | the request never got an HTTP answer |

`subscribe()` reconnects on its own with jittered exponential backoff, resuming
from `Last-Event-ID` so events missed during a blip are replayed. Pass
`{ reconnect: false }` to opt out, or `{ since: 12 }` to start from a known id.

## Running the service (CLI)

The service is Python. The CLI sets it up for you in a dedicated virtualenv,
so nothing leaks into your project:

```bash
# 1. from a local checkout (installs requirements.txt — the exact repo pins)
npx respeaker-clip serve --source /path/to/reSpeaker-Clip-AI-Agent

# 2. from a pip-installable distribution
npx respeaker-clip serve --pip-spec "respeaker-clip-service[clip]"

# 3. check prerequisites first
npx respeaker-clip doctor
npx respeaker-clip status --base-url http://localhost:5000
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--source <dir>` | `$RESPEAKER_CLIP_SERVICE_ROOT` | Run from a checkout (`pip install -r requirements.txt`) |
| `--venv <dir>` | `$RESPEAKER_CLIP_HOME/venv` | Virtualenv location |
| `--pip-spec <spec>` | `respeaker-clip-service` | pip requirement installed in package mode |
| `--skip-install` | — | Run what the venv already has |
| `--host` / `--port` | `0.0.0.0` / `5000` | Bind address |
| `--input-mode` | `clip` | `browser` \| `clip` \| `both` |
| `--ble-address` / `--ble-name` | from env | Pin or discover the device |
| `--no-clip` | — | Serve the HTTP API without the BLE runtime |
| `--no-agent` | — | Device gateway: no agent stack, no API key, audio over HTTP (see above) |
| `--env-file <path>` | `./.env` | Configuration file to load (real env vars win) |
| `--dry-run` / `--json` | — | Print the resolved plan and exit |
| `-- <args…>` | — | Extra arguments forwarded to the service |

Environment: `RESPEAKER_CLIP_HOME`, `RESPEAKER_CLIP_PIP_SPEC`,
`RESPEAKER_CLIP_PYTHON`, `RESPEAKER_CLIP_SERVICE_ROOT`,
`RESPEAKER_CLIP_ENV_FILE`, `RESPEAKER_CLIP_BASE_URL`.

The service reads a `.env` from the **directory you run it in** (or `--env-file`);
variables already in the environment always take precedence, so systemd
`EnvironmentFile=` and container env override the file. `.env` carries
`GROQ_API_KEY`, `CLIP_BLE_ADDRESS`, `VOICE_INPUT_MODE`, the `RTC_*` tuning and
`DATABASE_URL` — see `.env.example` in the repository.

`respeaker-clip status` exits `0` when the Clip is connected, `3` when the
service answers but the device is offline, and `1` on transport errors.

Deploying it as a service (systemd unit, BLE permissions, reverse proxy for SSE,
troubleshooting) is covered in **[DEPLOYMENT.md](./DEPLOYMENT.md)** —
中文版见 **[DEPLOYMENT.zh-cn.md](./DEPLOYMENT.zh-cn.md)**.

### Prerequisites for BLE voice input

- Linux or Windows host with Bluetooth (BlueZ on Linux: `bluetoothctl power on`)
- `python3 -m venv` available (Debian/Ubuntu: `sudo apt install python3-venv`)
- The Clip SDK is a git dependency (`respeaker-clip-sdk[ble]` from the
  RTC-capable fork). `--source` mode installs it via `requirements.txt`;
  in package mode install it explicitly:

```bash
pip install "respeaker-clip-sdk[ble] @ git+https://github.com/rayheto/reSpeaker_Clip.git@a146061b3820473f119dfaa7e8ac6791a48b9edb#subdirectory=sdk"
```

## Notes for maintainers

- `npm run build` compiles `src/` to `dist/esm` + `dist/cjs` (dual publish, both
  with type declarations); `npm test` builds and runs the `node:test` suite.
- `src/version.ts` is checked against `package.json` at build time.
- The Python distribution metadata lives in the repository root
  (`pyproject.toml`, console script `respeaker-clip-service`). Publishing that
  wheel to PyPI requires dropping the `clip` extra's direct git reference, which
  PyPI rejects — see the comment in `pyproject.toml`.
- No license has been chosen for this package yet (`package.json` says
  `UNLICENSED`); pick one before publishing publicly.

## License

UNLICENSED — see the repository for details.