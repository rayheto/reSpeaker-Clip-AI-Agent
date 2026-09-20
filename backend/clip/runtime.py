"""Application-level reSpeaker Clip runtime.

Owns exactly one ``clip.ClipClient`` per physical device and runs inside the
dedicated asyncio event loop of :mod:`backend.clip.worker`.  Design notes:

* **One connection owner** — the background supervisor is the only component
  that connects/reconnects, so the OS/Flask never races it.
* **Serialized commands** — the SDK serializes AT commands internally and the
  runtime adds a manager-level operation lock so a status heartbeat can never
  issue a command while a file transfer is active.
* **Reconnect discipline** — the protocol has no request id, so a timeout or
  protocol/connection failure disconnects the transport and the supervisor
  recreates it before the next command, with 1/2/4/8/16/30s jittered backoff.
* **State reconciliation** — firmware ``state`` events and GSTAT polling
  converge on a single current recording state; every transition (started /
  stopped) is idempotent with respect to the ``clip_ingestions`` table.
* **Offline safe** — Clip unavailability never blocks Flask from serving.
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Any, Callable

from clip import BleTransport, ClipClient
from clip.stream import StreamReceiver
from clip.exceptions import (
    ClipError,
    CommandError,
    CommandTimeoutError,
    ConnectionError as ClipConnectionError,
    ProtocolError,
    TransferError,
    TransferTimeoutError,
)

from config import settings
from backend.clip import store
from backend.clip.exceptions import (
    ClipCommandFailedError,
    ClipConflictError,
    ClipInputError,
    ClipTransferFailedError,
    ClipUnavailableError,
)
from backend.clip.audio_paths import session_audio_url, utterance_audio_path, utterance_audio_url
from backend.clip.ogg import (
    OpusFormatError,
    convert_frames_to_ogg_bytes,
    convert_session_to_ogg,
)
from backend.clip.transfer import download_session_compatible

logger = logging.getLogger(__name__)

RECONNECT_DELAYS = (1, 2, 4, 8, 16, 30)
JITTER_FRACTION = 0.2  # +/-20%
MAX_EVENT_HISTORY = 300
DOWNLOAD_MAX_ATTEMPTS = 3
DOWNLOAD_RECONNECT_TIMEOUT = 45.0

# CommandError messages that indicate a state conflict rather than a failure.
_CONFLICT_HINTS = (
    "already recording",
    "not recording",
    "no active session",
    "invalid state",
    "cannot delete active session",
    "already paused",
    "not paused",
)

# --- RTC live-stream phases (warm pause: BLE frames flow only while capturing) --
RTC_PHASE_DISCONNECTED = "disconnected"
RTC_PHASE_ARMING = "arming"
RTC_PHASE_PAUSED = "paused"          # armed, waiting for the next utterance
RTC_PHASE_CAPTURING = "capturing"    # listening (resumed)
RTC_PHASE_FINALIZING = "finalizing"  # transient: utterance being handed to STT
RTC_PHASE_STOPPED = "stopped"        # terminal until a fresh connection
RTC_ACTIVE_PHASES = (
    RTC_PHASE_ARMING,
    RTC_PHASE_PAUSED,
    RTC_PHASE_CAPTURING,
    RTC_PHASE_FINALIZING,
)
# Re-arm forever with bounded backoff. A transient radio/firmware timing miss
# must not leave voice input permanently disconnected.
RTC_ARM_RETRY_DELAYS = (2.0, 5.0, 15.0, 30.0)
RTC_DOWNLOAD_RESPONSE_RETRIES = 3
RTC_START_RESPONSE_SETTLE_SECONDS = 0.25


class RtcStreamReceiver(StreamReceiver):
    """Accept only a harmless duplicate initial STREAM_START notification."""

    def _on_start(self, frame: Any) -> None:
        if (
            self.started.is_set()
            and self.frames_received == 0
            and self.session_id == frame.session_id
        ):
            return
        super()._on_start(frame)

    def fail_start(self, message: str) -> None:
        """Wake an arm waiter when firmware ends before STREAM_START."""
        if self.started.is_set():
            return
        self._fail(TransferError(message))
        # StreamReceiver.wait_start() waits on ``started`` only.  Set it after
        # recording the error so the waiter wakes and raises immediately.
        self.started.set()


def _rtc_partial_stt_default(frames: list[bytes]) -> str:
    """Rolling partial transcription of a cumulative Ogg snapshot."""
    from backend.clip.ogg import convert_frames_to_ogg_bytes
    from backend.llm.stt import transcribe_bytes

    audio = convert_frames_to_ogg_bytes(frames)
    return transcribe_bytes(
        audio, filename="clip-rtc-partial.ogg", model=settings.GROQ_RTC_PARTIAL_MODEL
    )


def _rtc_final_stt_default(frames: list[bytes]) -> str:
    """Authoritative final transcription of one utterance."""
    from backend.clip.ogg import convert_frames_to_ogg_bytes
    from backend.llm.stt import transcribe_bytes

    audio = convert_frames_to_ogg_bytes(frames)
    return transcribe_bytes(
        audio, filename="clip-rtc-final.ogg", model=settings.GROQ_RTC_FINAL_MODEL
    )


@dataclass
class IngestRequest:
    session_id: str
    trigger: str = "physical"
    conversation_id: str | None = None
    force: bool = False


def _jittered(delay: float) -> float:
    return max(0.05, delay * (1.0 + JITTER_FRACTION * (2.0 * random.random() - 1.0)))


def _swallow_task_result(task: asyncio.Task) -> None:
    """Silence a cancelled worker's result so it cannot spam warnings."""
    if not task.cancelled():
        task.exception()


def reconnect_delay_seconds(failures: int) -> float:
    """Deterministic (un-jittered) delay for a given consecutive-failure count."""
    index = max(0, min(failures, len(RECONNECT_DELAYS) - 1))
    return float(RECONNECT_DELAYS[index])


class ClipRuntime:
    """Async core: connection supervision, recording, ingestion, events."""

    def __init__(
        self,
        *,
        transport: Any | None = None,
        device_id: str | None = None,
        rtc_auto_arm: bool | None = None,
        agent_enabled: bool | None = None,
        audio_service: Any | None = None,
    ) -> None:
        ble_address = settings.CLIP_BLE_ADDRESS.strip()
        ble_name = settings.CLIP_BLE_NAME.strip() or "Clip"
        explicit_device_id = (device_id or "").strip()
        self._transport = transport or BleTransport(
            address=ble_address or None,
            name=ble_name,
        )
        self._client = ClipClient(self._transport)
        self._client.on_event(self._on_event_callback)
        self.device_id = explicit_device_id or ble_address or ble_name

        self._temp_dir = Path(settings.CLIP_TEMP_DIR)

        # Locks / ownership
        self._operation_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()

        # Supervisor state
        self._stopping = asyncio.Event()
        self._connection_ready = asyncio.Event()
        self._transport_lost = asyncio.Event()
        self._baseline_done = False
        self._recovery_done = False
        self._ready = False
        self._reconnect_failures = 0

        # Current device state
        self._recording = False
        self._recording_session: str | None = None
        self._last_status_payload: dict[str, Any] | None = None
        self._last_error: str | None = None
        self._transfer_active = False
        self._download_retry_owner = False
        self._download_reconnect_requested = False
        self._web_sessions: set[str] = set()
        self._web_start_pending = False

        # Active conversation (registered by the web page)
        self._active_conversation: str | None = None

        # Ingestion queue
        self._ingest_queue: asyncio.Queue[IngestRequest] = asyncio.Queue()
        self._queued_sessions: set[str] = set()
        self._ingest_task: asyncio.Task | None = None
        self._supervisor_task: asyncio.Task | None = None

        # Event hub (thread-safe; read by Flask threads for SSE)
        self._event_history: deque[tuple[int, dict[str, Any]]] = deque(
            maxlen=MAX_EVENT_HISTORY
        )
        self._event_cond = threading.Condition(threading.Lock())
        self._event_seq = count()

        # RTC live-stream state (warm-pause utterance engine)
        self.rtc_auto_arm = (
            bool(settings.RTC_AUTO_ARM) if rtc_auto_arm is None else bool(rtc_auto_arm)
        )
        self._rtc_phase = RTC_PHASE_DISCONNECTED
        self._rtc_session: str | None = None
        self._rtc_start_accepted = False
        self._rtc_session_ready = asyncio.Event()
        self._rtc_event_session: str | None = None
        # RTC sessions are live-only and never exist in SD storage. Preserve
        # their ids across failed arms/reconnects so a stale IDLE GSTAT cannot
        # route one into the legacy ingestion/download workflow.
        self._rtc_session_ids: set[str] = set()
        self._rtc_receiver: StreamReceiver | None = None
        self._rtc_lease_token: int | None = None
        self._rtc_arm_attempts = 0
        self._rtc_reconnect_for_arm_failure = False
        self._rtc_next_arm_at = 0.0
        self._rtc_arm_started_at = 0.0
        self._rtc_download_accepted_at: float | None = None
        self._rtc_device_event_status: str | None = None
        self._rtc_device_event_at = 0.0
        self._rtc_device_event_logged_status: str | None = None
        self._rtc_device_event_logged_at = 0.0
        self._rtc_last_error: str | None = None
        self._rtc_generation = 0
        self._rtc_utterance_id: int | None = None
        self._rtc_utterance_frames: list[bytes] = []
        self._rtc_utterance_truncated = False
        self._rtc_pre_roll: deque[tuple[float, bytes]] = deque(
            maxlen=max(1, int(settings.RTC_PRE_ROLL_FRAMES))
        )
        self._rtc_event_ignore_until = 0.0
        self._rtc_partial_text = ""
        self._rtc_partial_inflight = False
        self._rtc_capture_task: asyncio.Task | None = None
        self._rtc_finalize_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._rtc_finalize_task: asyncio.Task | None = None
        self._rtc_finalize_pending = 0

        # Injected collaborators (overridden in tests)
        self.agent_enabled = (
            settings.AGENT_ENABLED if agent_enabled is None else bool(agent_enabled)
        )
        self.audio_service = (
            audio_service if audio_service is not None else self._build_audio_service()
        )
        self.ogg_converter = convert_session_to_ogg
        self.session_downloader = download_session_compatible
        self.rtc_stt_partial = _rtc_partial_stt_default
        self.rtc_stt_final = _rtc_final_stt_default

    def _build_audio_service(self) -> Any | None:
        """Build the agent pipeline, but only when the agent is enabled.

        Imported here rather than at module scope so that a device-gateway
        deployment (``AGENT_ENABLED=false``) never loads LangGraph, Groq, Mem0,
        Pinecone or the conversation store: the Clip runtime stands alone.
        """
        if not self.agent_enabled:
            return None
        from backend.services.audio_service import AudioService

        return AudioService()

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        """Start the supervisor and ingestion loop (called by the worker)."""
        self._ingest_task = asyncio.create_task(self._ingest_loop(), name="clip-ingest")
        self._supervisor_task = asyncio.create_task(self._supervisor(), name="clip-supervisor")
        self._rtc_finalize_task = asyncio.create_task(
            self._rtc_finalize_loop(), name="clip-rtc-finalize"
        )
        try:
            await self._stopping.wait()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        self._stopping.set()
        for task in (self._supervisor_task, self._ingest_task, self._rtc_finalize_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._supervisor_task, self._ingest_task, self._rtc_finalize_task):
            if task is not None and not task.done():
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        # Best-effort terminal STOP so the device does not keep streaming.
        if self._rtc_active and self.is_connected:
            try:
                await self._client.stop_recording()
            except Exception:
                pass
        await self._rtc_abort(reason="shutdown", finalize=False)
        await self._teardown_transport()

    @property
    def _rtc_active(self) -> bool:
        return self._rtc_phase in RTC_ACTIVE_PHASES

    # ------------------------------------------------------------------
    # Event hub
    # ------------------------------------------------------------------

    def _push_event(self, event: dict[str, Any]) -> None:
        with self._event_cond:
            sequence = next(self._event_seq)
            self._event_history.append((sequence, dict(event)))
            self._event_cond.notify_all()

    def event_history(self) -> list[dict[str, Any]]:
        with self._event_cond:
            return [dict(event) for _, event in self._event_history]

    def iter_events(self, after_id: int | None = None) -> Any:
        """Blocking generator of events for an SSE subscriber.

        Runs on the Flask (synchronous) thread while the runtime pushes events
        from the worker thread; a 15s timeout yields an empty ping event.
        """
        # A new subscriber starts at the current tail. EventSource reconnects
        # provide Last-Event-ID so events missed during a short network outage
        # can be replayed even after deque rotation.
        with self._event_cond:
            if after_id is None:
                cursor = self._event_history[-1][0] if self._event_history else -1
            else:
                cursor = after_id
        yield {
            "type": "connection",
            "connected": self._ready,
            "error": self._last_error,
        }
        while True:
            new: list[tuple[int, dict[str, Any]]] = []
            with self._event_cond:
                new = [item for item in self._event_history if item[0] > cursor]
                if new:
                    cursor = new[-1][0]
                elif self._stopping.is_set():
                    return
                elif not self._event_cond.wait(timeout=15.0):
                    pass  # keep-alive ping below
            for sequence, event in new:
                payload = dict(event)
                payload["_event_id"] = sequence
                yield payload
            if not new:
                yield {"type": "ping"}

    # ------------------------------------------------------------------
    # Firmware events
    # ------------------------------------------------------------------

    def _on_event_callback(self, payload: dict[str, Any]) -> None:
        """Invoked synchronously by the transport; schedule async handling."""
        if not isinstance(payload, dict) or not payload.get("event"):
            return
        try:
            asyncio.create_task(self._handle_event(dict(payload)))
        except RuntimeError:
            logger.debug("clip event callback outside running loop: %s", payload)

    async def _handle_event(self, payload: dict[str, Any]) -> None:
        event = str(payload.get("event", ""))
        if event == "rtc":
            status = str(payload.get("status", "unknown"))
            now = time.monotonic()
            self._rtc_device_event_status = status
            self._rtc_device_event_at = now
            # BlueZ can replay one response notification while a CCC lease is
            # being torn down.  Keep the authoritative timestamp above, but do
            # not publish/log an indistinguishable immediate duplicate.
            if (
                status == self._rtc_device_event_logged_status
                and now - self._rtc_device_event_logged_at < 0.5
            ):
                return
            self._rtc_device_event_logged_status = status
            self._rtc_device_event_logged_at = now
            logger.warning("clip RTC firmware event: %s", status)
            self._push_event(
                {
                    "type": "rtc_device",
                    "status": status,
                    "session": self._rtc_session,
                }
            )
            return
        if event == "state":
            state = str(payload.get("state", "")).upper()
            session: str | None = payload.get("session") if isinstance(payload.get("session"), str) else None
            if (
                self._rtc_phase == RTC_PHASE_ARMING
                and state == "STREAMING"
                and session
            ):
                # Firmware may acknowledge AT+START=rtc with an empty data
                # object while the audio thread is still publishing its
                # session id.  The state event is authoritative in that
                # startup window and normally arrives before the response.
                if session not in self._rtc_session_ids:
                    self._rtc_event_session = session
                    self._rtc_session = session
                    self._rtc_session_ids.add(session)
                    self._rtc_session_ready.set()
                else:
                    logger.debug(
                        "ignoring replayed RTC STREAMING event for %s", session
                    )
            if self._rtc_active:
                # While an RTC session is armed the firmware state machine is
                # RTC-owned: STREAMING = resumed/capturing, PAUSED = warm pause,
                # IDLE = terminal stop. Never mixes with SD-record ingestion.
                if state == "STREAMING":
                    await self._rtc_handle_streaming()
                elif state == "PAUSED":
                    await self._rtc_handle_paused()
                elif state == "IDLE":
                    await self._rtc_handle_idle(session)
                else:
                    logger.debug("clip state event %s ignored while RTC armed", state)
                return
            if state in ("RECORDING", "PAUSED"):
                await self._observe_recording_started(session or self._recording_session)
            elif state == "IDLE":
                await self._observe_recording_stopped(session)
        # Other events (ble/wifi/usb/storage/mark) are intentionally ignored;
        # the status heartbeat reports connection and battery state.

    # ------------------------------------------------------------------
    # Recording control (web)
    # ------------------------------------------------------------------

    async def start_recording(
        self,
        mode: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        mode = mode or settings.CLIP_RECORD_MODE
        if mode not in ("normal", "enhanced"):
            raise ClipInputError("mode must be 'normal' or 'enhanced'")
        if self._recording:
            raise ClipConflictError("Clip is already recording")
        if self._rtc_active:
            raise ClipConflictError(
                "RTC live stream is armed; it owns the file-frame channel"
            )
        if self._transfer_active:
            raise ClipConflictError("a download is in progress; stop or wait for it first")

        if conversation_id:
            self._active_conversation = conversation_id

        self._web_start_pending = True
        try:
            session = await self._call(lambda: self._client.start_recording(mode))
        except Exception:
            self._web_start_pending = False
            raise
        if session:
            self._web_sessions.add(session)
        await self._observe_recording_started(session or self._recording_session)
        return {
            "session": self._recording_session,
            "mode": mode,
            "conversation_id": self._active_conversation,
        }

    async def stop_recording(self) -> dict[str, Any]:
        if self._rtc_active:
            # STOP is terminal for RTC: finalize the live utterance, end the
            # stream and detach the receiver lease.
            return await self.rtc_stop()
        if not self._recording and not self._recording_session:
            raise ClipConflictError("Clip is not recording")
        if self._transfer_active:
            raise ClipConflictError("a download is in progress; wait for it first")
        requested_sid = self._recording_session
        try:
            result = await self._call(lambda: self._client.stop_recording())
        except ClipConflictError:
            # Nothing active on the device; reconcile locally and report.
            self._recording = False
            self._recording_session = None
            raise
        sid = result.get("session") if isinstance(result, dict) else None
        sid = sid or requested_sid
        await self._observe_recording_stopped(sid)
        return {"accepted": True, "session": sid, "data": result}

    async def set_active_conversation(self, conversation_id: str | None) -> None:
        if conversation_id and not isinstance(conversation_id, str):
            raise ClipInputError("conversation_id must be a string")
        self._active_conversation = conversation_id or None

    # ------------------------------------------------------------------
    # Observed state transitions (events + GSTAT reconciliation)
    # ------------------------------------------------------------------

    async def _observe_recording_started(self, session: str | None) -> None:
        trigger = (
            "web"
            if self._web_start_pending or (session and session in self._web_sessions)
            else "physical"
        )
        self._web_start_pending = False
        if session:
            self._recording_session = session
            if trigger == "web":
                self._web_sessions.add(session)
        elif self._recording_session is None:
            # Refuse to mark recording without any session to track.
            self._recording = True
            return
        already = self._recording
        self._recording = True
        if self._recording_session:
            store.upsert_ingestion(
                self.device_id,
                self._recording_session,
                status="recording",
                trigger=trigger,
                conversation_id=self._active_conversation,
            )
        if not already:
            self._push_event(
                {
                    "type": "recording",
                    "action": "started",
                    "session": self._recording_session,
                    "trigger": trigger,
                }
            )

    async def _observe_recording_stopped(self, session: str | None) -> None:
        sid = session or self._recording_session
        was_recording = self._recording or self._recording_session is not None
        trigger = "web" if (sid and sid in self._web_sessions) else "physical"
        self._recording = False
        self._recording_session = None
        if sid:
            self._web_sessions.discard(sid)
        if was_recording:
            self._push_event(
                {
                    "type": "recording",
                    "action": "stopped",
                    "session": sid,
                    "trigger": trigger,
                }
            )
        # Enqueue ingestion only on the first observation of the stop (a
        # firmware IDLE notification may race the direct web call; the second
        # call sees was_recording=False and must not overwrite the trigger).
        # Untracked stops are recovered by the heartbeat's reconnect branch.
        if sid and was_recording:
            await self._request_ingest(sid, trigger=trigger)

    def _poll_recording_active(self, status: Any) -> bool:
        return str(getattr(status, "state", "")).upper() in ("RECORDING", "PAUSED")

    async def _apply_status(self, status: Any) -> None:
        """Reconcile polling against observed events (idempotent)."""
        if self._rtc_active:
            # RTC owns the session: GSTAT reports STREAMING/PAUSED and RTC
            # sessions are never SD recordings, so legacy reconciliation
            # (ingest rows, download enqueue) must not run here.
            return
        recording = self._poll_recording_active(status)
        sid = getattr(status, "session_id", None)
        if recording and sid and sid != self._recording_session:
            # A new session started while we were not looking (e.g. physical
            # button during a disconnect). Stop tracking any stale session.
            if self._recording_session:
                await self._observe_recording_stopped(self._recording_session)
            await self._observe_recording_started(sid)
        elif not recording and self._recording_session:
            await self._observe_recording_stopped(sid)
        elif recording and not self._recording_session and sid:
            await self._observe_recording_started(sid)
        elif (
            not recording
            and sid
            and sid not in self._rtc_session_ids
            and self._baseline_done
        ):
            # Firmware keeps the most recently stopped session in GSTAT while
            # IDLE.  This recovers a physical-button stop missed during a BLE
            # outage without an unstable paginated AT+LIST history scan.
            await self._request_ingest(sid, trigger="reconnect")

    # ------------------------------------------------------------------
    # Connection supervision
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        try:
            return bool(self._client.is_connected)
        except Exception:
            return False

    @property
    def _legacy_ingest_pending(self) -> bool:
        """Whether an SD ingestion owns priority over RTC auto-arm.

        Recovery queues are populated during ``_on_connected`` before the
        ingestion coroutine has a chance to enter ``_download_session``.  The
        queued-session check closes that scheduling window; the retry-owner
        check covers the transfer/reconnect window after dequeue.
        """
        return bool(self._queued_sessions) or self._download_retry_owner

    async def _connect(self) -> None:
        async with self._connect_lock:
            if self._client.is_connected:
                return
            await self._client.connect()
            self._last_error = None

    async def _teardown_transport(self) -> None:
        """Disconnect so the supervisor can recreate the connection."""
        self._ready = False
        self._connection_ready.clear()
        self._transport_lost.set()
        await self._rtc_abort(reason="disconnected", finalize=True)
        try:
            await self._client.disconnect()
        except Exception:
            pass

    async def _supervisor(self) -> None:
        self._last_conn_payload: dict[str, Any] | None = None
        while not self._stopping.is_set():
            if not self.is_connected:
                # A BLE link can disappear while an SDK file receiver is
                # waiting for frames.  Publish the real state immediately and
                # wake the transfer watchdog instead of waiting for the SDK's
                # full download timeout.
                self._ready = False
                self._connection_ready.clear()
                self._transport_lost.set()
                try:
                    await self._rtc_abort(reason="disconnected", finalize=True)
                except Exception as exc:
                    logger.debug("clip RTC abort on link loss: %s", exc)
                conn_payload: dict[str, Any] = {
                    "type": "connection",
                    "connected": False,
                    "error": self._last_error or "disconnected",
                }
                if conn_payload != self._last_conn_payload:
                    self._last_conn_payload = conn_payload
                    self._push_event(conn_payload)
                # During a transfer failure the download coroutine must first
                # detach its old frame receiver.  Reconnecting before that
                # cleanup completes can route notifications from the new link
                # into stale receiver state and produce false CRC failures.
                if (
                    self._download_retry_owner
                    and not self._download_reconnect_requested
                ):
                    try:
                        await asyncio.wait_for(self._stopping.wait(), timeout=0.1)
                    except asyncio.TimeoutError:
                        pass
                    continue
                try:
                    await self._connect()
                except ClipConnectionError as exc:
                    self._last_error = str(exc)
                except Exception as exc:
                    self._last_error = str(exc)
                if self.is_connected:
                    self._reconnect_failures = 0
                    try:
                        await self._on_connected()
                    except Exception as exc:
                        logger.exception("clip post-connect failed: %s", exc)
                        await self._teardown_transport()
                else:
                    self._reconnect_failures += 1
                    delay = _jittered(
                        reconnect_delay_seconds(self._reconnect_failures - 1)
                    )
                    try:
                        await asyncio.wait_for(self._stopping.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        pass
            else:
                try:
                    await self._heartbeat_once()
                except ClipUnavailableError as exc:
                    self._last_error = str(exc)
                    await self._teardown_transport()
                except Exception as exc:
                    logger.warning("clip heartbeat failed: %s", exc)
                    self._last_error = str(exc)
                    await self._teardown_transport()
                if (
                    self.rtc_auto_arm
                    and not self._legacy_ingest_pending
                    and self._rtc_phase == RTC_PHASE_DISCONNECTED
                    and time.monotonic() >= self._rtc_next_arm_at
                    and self._ready
                    and self.is_connected
                    and not self._stopping.is_set()
                ):
                    try:
                        await self._arm_rtc()
                    except Exception as exc:
                        logger.warning("clip RTC re-arm attempt failed: %s", exc)
                interval = max(0.5, settings.CLIP_STATUS_INTERVAL)
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass

    async def _on_connected(self) -> None:
        async with self._operation_lock:
            status = await self._client.status()
            self._baseline_done = store.is_baseline_complete(self.device_id)
            await self._apply_status(status)
            self._last_status_payload = self._status_payload_from(status)

            if not self._baseline_done:
                if not await self._baseline_existing():
                    raise ClipUnavailableError("could not establish Clip session baseline")
                store.mark_baseline_complete(self.device_id)
                self._baseline_done = True
            else:
                if not self._recovery_done:
                    await self._recover_interrupted_ingestions()
                    self._recovery_done = True
            self._ready = True
            self._last_error = None
            self._transport_lost.clear()
            self._connection_ready.set()
            if self._rtc_reconnect_for_arm_failure:
                # Preserve the bounded arm-attempt count across a reconnect
                # requested specifically to discard a suspect command stream.
                self._rtc_reconnect_for_arm_failure = False
            else:
                self._rtc_arm_attempts = 0
                self._rtc_next_arm_at = 0.0
            if self._rtc_phase == RTC_PHASE_STOPPED:
                # A fresh connection re-arms a new RTC session.
                self._rtc_phase = RTC_PHASE_DISCONNECTED

        # Auto-arm the live RTC session after every baseline/recovery.  A
        # failure is non-fatal: the supervisor retries with bounded backoff.
        if (
            self.rtc_auto_arm
            and not self._legacy_ingest_pending
            and time.monotonic() >= self._rtc_next_arm_at
        ):
            try:
                await self._arm_rtc()
            except Exception as exc:
                logger.exception("clip RTC auto-arm failed: %s", exc)

        # A failed arm may deliberately close a response stream that can no
        # longer be trusted.  Do not publish a contradictory connected event;
        # the supervisor will establish the replacement link next iteration.
        if not self._ready or not self.is_connected:
            return

        connected_event = {
            "type": "connection",
            "connected": True,
            "error": None,
            "status": self._last_status_payload,
        }
        self._last_conn_payload = connected_event
        self._push_event(connected_event)

    async def _baseline_existing(self) -> bool:
        try:
            sessions = await self._list_device_sessions()
        except Exception as exc:
            logger.warning("clip baseline session list failed: %s", exc)
            return False
        store.mark_ignored_existing(
            self.device_id, [s.id for s in sessions if s.id != self._recording_session]
        )
        return True

    async def _recover_interrupted_ingestions(self) -> None:
        for row in store.list_recent_ingestions(self.device_id, limit=1000):
            if row.get("status") not in ("downloading", "processing"):
                continue
            store.mark_status(
                self.device_id,
                row["session_id"],
                "stopped",
                error="resuming after service restart",
            )
            await self._request_ingest(
                row["session_id"],
                trigger=row.get("trigger") or "restart",
                conversation_id=row.get("conversation_id"),
            )

    async def _discover_sessions_locked(self) -> None:
        sessions = await self._list_device_sessions()
        for session in sessions:
            if session.id != self._recording_session:
                await self._request_ingest(session.id, trigger="reconnect")

    async def _list_device_sessions(self) -> tuple[Any, ...]:
        """List every session without requiring firmware's optional ``total``.

        Clip firmware 0.0.8+1 can omit ``total`` from ``AT+LIST`` responses.
        The SDK's ``list_all_sessions`` rejects that otherwise valid response,
        so paginate using the public ``list_sessions`` API and stop on a short
        page instead.
        """
        # Page 1 with the SDK default page size is encoded as plain AT+LIST,
        # which is reliable on firmware 0.0.8+1.  AT+LIST?1&50 has shown
        # intermittent timeouts on the same unit.
        per_page = 10
        return await self._client.list_sessions(page_number=1, per_page=per_page)

    async def _discovery_scan(self) -> None:
        if self._transfer_active or self._operation_lock.locked() or not self._ready:
            return
        async with self._operation_lock:
            try:
                await self._discover_sessions_locked()
            except Exception as exc:
                logger.warning("clip discovery scan failed: %s", exc)
                # A timed-out command leaves the request/response protocol
                # desynchronized by design.  Reconnect before another command.
                await self._teardown_transport()

    async def _heartbeat_once(self) -> None:
        # Status stays allowed while recording; it is skipped while a download
        # holds the manager-level operation lock.
        if self._operation_lock.locked():
            return
        async with self._operation_lock:
            if not self.is_connected or not self._ready:
                raise ClipUnavailableError("Clip is not connected")
            try:
                status = await self._client.status()
            except (CommandTimeoutError, ClipConnectionError, ProtocolError) as exc:
                raise ClipUnavailableError(str(exc)) from exc
            except CommandError as exc:
                logger.warning("clip GSTAT rejected: %s", exc)
                return
            self._reconnect_failures = 0
            self._last_error = None
            await self._apply_status(status)
            self._last_status_payload = self._status_payload_from(status)

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------

    async def _call(self, operation: Callable[[], Any]) -> Any:
        """Run one serialized command; maps SDK errors to runtime errors.

        On timeout/protocol/connection failure the transport is disconnected so
        the supervisor performs a clean reconnect before any next command.
        """
        async with self._operation_lock:
            return await self._run_command(operation)

    async def _run_command(self, operation: Callable[[], Any]) -> Any:
        """Run a command assuming the manager lock is held (mapping errors).

        Used by ``_call`` and by the RTC arming sequence, which runs inside
        ``_on_connected``'s lock and therefore cannot re-acquire it.
        """
        if not self.is_connected:
            raise ClipUnavailableError("Clip is not connected or is reconnecting")
        try:
            value = await operation()
        except CommandError as exc:
            message = str(exc).lower()
            if any(hint in message for hint in _CONFLICT_HINTS):
                raise ClipConflictError(str(exc)) from exc
            raise ClipCommandFailedError(str(exc)) from exc
        except (CommandTimeoutError, ClipConnectionError, ProtocolError) as exc:
            await self._teardown_transport()
            raise ClipUnavailableError(str(exc)) from exc
        except ClipError as exc:
            raise ClipCommandFailedError(str(exc)) from exc
        self._reconnect_failures = 0
        return value

    # ------------------------------------------------------------------
    # RTC live streaming (warm-pause utterances)
    # ------------------------------------------------------------------

    def _rtc_on_frame(self, payload: bytes) -> None:
        """Synchronous receive-path callback: O(1) append + ring buffer only.

        Never performs file I/O, network, decoding or locking on the BLE
        receive path. Frames captured while CAPTURING append to the current
        utterance buffer (hard-bounded); frames around transitions land in a
        small pre-roll ring that seeds the next utterance so the first words
        after a physical double-click are not lost.
        """
        if self._rtc_phase == RTC_PHASE_CAPTURING:
            if len(self._rtc_utterance_frames) < settings.RTC_MAX_UTTERANCE_FRAMES:
                self._rtc_utterance_frames.append(payload)
            else:
                self._rtc_utterance_truncated = True
        else:
            self._rtc_pre_roll.append((time.monotonic(), payload))

    def _consume_pre_roll(self) -> list[bytes]:
        """Seed a new utterance with tentative first frames (bounded ring)."""
        frames = [payload for _, payload in self._rtc_pre_roll]
        self._rtc_pre_roll.clear()
        return frames

    async def _rtc_begin_capture(self, trigger: str) -> None:
        """Transition to CAPTURING and start the next logical utterance."""
        if self._rtc_phase == RTC_PHASE_CAPTURING:
            return  # duplicate STREAMING / double-click: already listening
        self._rtc_generation += 1
        uid = self._rtc_generation
        self._rtc_utterance_id = uid
        self._rtc_utterance_frames = self._consume_pre_roll()
        self._rtc_utterance_truncated = False
        self._rtc_partial_text = ""
        self._rtc_partial_inflight = False
        self._rtc_phase = RTC_PHASE_CAPTURING
        self._push_event(
            {
                "type": "rtc_state",
                "phase": RTC_PHASE_CAPTURING,
                "session": self._rtc_session,
                "utterance_id": uid,
                "trigger": trigger,
            }
        )
        if self._rtc_capture_task is not None and not self._rtc_capture_task.done():
            self._rtc_capture_task.cancel()
            self._rtc_capture_task.add_done_callback(_swallow_task_result)
        self._rtc_capture_task = asyncio.create_task(
            self._rtc_capture_worker(uid), name=f"clip-rtc-capture-{uid}"
        )

    async def _rtc_capture_worker(self, uid: int) -> None:
        """Rolling partial STT: latest-wins, at most one request in flight."""
        interval = max(0.2, float(settings.RTC_PARTIAL_INTERVAL))
        while not self._stopping.is_set():
            if (
                self._rtc_utterance_id != uid
                or self._rtc_phase != RTC_PHASE_CAPTURING
            ):
                return
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            if self._stopping.is_set():
                return
            if (
                self._rtc_utterance_id != uid
                or self._rtc_phase != RTC_PHASE_CAPTURING
            ):
                return
            if self._rtc_partial_inflight:
                continue
            if len(self._rtc_utterance_frames) < settings.RTC_PARTIAL_MIN_FRAMES:
                continue
            snapshot = list(self._rtc_utterance_frames)
            self._rtc_partial_inflight = True
            try:
                text = await asyncio.to_thread(self.rtc_stt_partial, snapshot)
            except Exception as exc:
                logger.warning("clip RTC partial STT failed: %s", exc)
                continue
            finally:
                self._rtc_partial_inflight = False
            text = (text or "").strip()
            # latest-wins: apply only while this utterance is still live.
            if (
                text
                and self._rtc_utterance_id == uid
                and self._rtc_phase == RTC_PHASE_CAPTURING
                and text != self._rtc_partial_text
            ):
                self._rtc_partial_text = text
                self._push_event(
                    {
                        "type": "transcript",
                        "utterance_id": uid,
                        "session": self._rtc_session,
                        "text": text,
                        "final": False,
                    }
                )

    async def _rtc_finalize_utterance(self, reason: str) -> bool:
        """Finalize the live utterance exactly once (idempotent).

        Decoupled from the LLM: the job is queued (FIFO) and processed by the
        background finalize worker, so the next utterance can start while the
        previous one is still transcribing/thinking.
        """
        if self._rtc_phase != RTC_PHASE_CAPTURING:
            return False
        uid = self._rtc_utterance_id
        frames = list(self._rtc_utterance_frames)
        truncated = self._rtc_utterance_truncated
        # Claim the FINALIZING phase before any await: a second concurrent
        # finalize (late firmware PAUSED vs web pause) sees non-CAPTURING and
        # returns False, so finalization is exactly once.
        self._rtc_phase = RTC_PHASE_FINALIZING
        self._rtc_utterance_frames = []
        self._rtc_utterance_truncated = False
        self._rtc_partial_inflight = False
        if self._rtc_capture_task is not None and not self._rtc_capture_task.done():
            # Cancel without awaiting: the worker only touches the same fields
            # the next capture re-initializes, and a stale result is discarded
            # by its uid/phase guards. No await keeps finalization atomic.
            self._rtc_capture_task.cancel()
            self._rtc_capture_task.add_done_callback(_swallow_task_result)
        self._rtc_capture_task = None
        self._push_event(
            {
                "type": "rtc_state",
                "phase": RTC_PHASE_FINALIZING,
                "session": self._rtc_session,
                "utterance_id": uid,
                "reason": reason,
            }
        )
        job = {
            "utterance_id": uid,
            "session": self._rtc_session,
            "frames": frames,
            "reason": reason,
            "truncated": truncated,
            "conversation_id": self._active_conversation,
        }
        # Bounded FIFO: only on a pathological backlog drop the oldest job.
        if self._rtc_finalize_pending >= settings.RTC_MAX_PENDING_FINALIZE:
            try:
                self._rtc_finalize_queue.get_nowait()
                self._rtc_finalize_pending -= 1
            except asyncio.QueueEmpty:
                pass
        self._rtc_finalize_queue.put_nowait(job)
        self._rtc_finalize_pending += 1
        self._rtc_phase = RTC_PHASE_PAUSED
        self._push_event(
            {
                "type": "rtc_state",
                "phase": RTC_PHASE_PAUSED,
                "session": self._rtc_session,
                "utterance_id": uid,
            }
        )
        return True

    async def _rtc_finalize_loop(self) -> None:
        """FIFO worker: final STT then exactly one process_transcript."""
        while True:
            job = await self._rtc_finalize_queue.get()
            try:
                await self._rtc_finalize_job(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("clip RTC finalize failed: %s", exc)
                self._push_event(
                    {
                        "type": "rtc_state",
                        "phase": RTC_PHASE_PAUSED,
                        "session": job.get("session"),
                        "utterance_id": job.get("utterance_id"),
                        "error": str(exc),
                    }
                )
            finally:
                self._rtc_finalize_pending = max(0, self._rtc_finalize_pending - 1)

    async def _exchange_utterance(self, job: dict[str, Any]) -> None:
        """Device-gateway path for one utterance: keep the audio, skip the AI.

        No transcription and no agent: the frames are re-containerized to Ogg,
        written under ``CLIP_TEMP_DIR/rtc/<session>/`` and announced with an
        ``utterance_audio`` event carrying a URL the client can fetch. ASR and
        the reply are the consumer's business.
        """
        uid = job["utterance_id"]
        frames = job["frames"]
        session = job.get("session") or "unknown"
        payload: dict[str, Any] = {
            "type": "utterance_audio",
            "utterance_id": uid,
            "session": session,
        }
        if len(frames) < settings.RTC_MIN_UTTERANCE_FRAMES:
            payload["skipped"] = "too short"
            self._push_event(payload)
            return
        try:
            audio = await asyncio.to_thread(convert_frames_to_ogg_bytes, frames)
            path = utterance_audio_path(session, uid)
            path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(path.write_bytes, audio)
        except Exception as exc:
            logger.exception("clip utterance audio exchange failed for %s: %s", uid, exc)
            payload["error"] = str(exc)
            self._push_event(payload)
            return
        payload["url"] = utterance_audio_url(session, uid)
        payload["bytes"] = len(audio)
        payload["content_type"] = "audio/ogg"
        payload["trigger"] = job.get("reason")
        self._push_event(payload)

    async def _rtc_finalize_job(self, job: dict[str, Any]) -> None:
        if not self.agent_enabled:
            await self._exchange_utterance(job)
            return
        uid = job["utterance_id"]
        frames = job["frames"]
        session = job.get("session")
        if len(frames) < settings.RTC_MIN_UTTERANCE_FRAMES:
            # Empty / too-short utterance: never invoke the LLM.
            self._push_event(
                {
                    "type": "transcript",
                    "utterance_id": uid,
                    "session": session,
                    "text": "",
                    "final": True,
                    "skipped": "too short",
                }
            )
            return
        try:
            text = await asyncio.to_thread(self.rtc_stt_final, frames)
        except Exception as exc:
            logger.warning("clip RTC final STT failed for %s: %s", uid, exc)
            self._push_event(
                {
                    "type": "rtc_state",
                    "phase": RTC_PHASE_PAUSED,
                    "session": session,
                    "utterance_id": uid,
                    "error": f"final transcription failed: {exc}",
                }
            )
            return
        text = (text or "").strip()
        self._push_event(
            {
                "type": "transcript",
                "utterance_id": uid,
                "session": session,
                "text": text,
                "final": True,
            }
        )
        if not text:
            return

        # Streamed reply: emit tool-call ("thinking") and token events so the
        # frontend shows LLM progress instead of a silent "Armed" wait. Falls
        # back to the blocking pipeline when the injected audio_service only
        # provides the legacy method (keeps existing tests/stubs intact).
        def _stream_emit(event: dict[str, Any]) -> None:
            ev: dict[str, Any] = {
                "type": event.get("type"),
                "session": session,
                "utterance_id": uid,
            }
            if event.get("tool"):
                ev["tool"] = event["tool"]
            elif event.get("text") is not None:
                ev["text"] = event["text"]
            self._push_event(ev)

        stream_proc = getattr(self.audio_service, "process_transcript_stream", None)
        try:
            if stream_proc is not None:
                outcome = await asyncio.to_thread(
                    stream_proc, text, job.get("conversation_id"), _stream_emit
                )
            else:
                outcome = await asyncio.to_thread(
                    self.audio_service.process_transcript,
                    text,
                    job.get("conversation_id"),
                )
        except Exception as exc:
            logger.exception(
                "clip RTC process_transcript failed for %s: %s", uid, exc
            )
            self._push_event(
                {
                    "type": "rtc_state",
                    "phase": RTC_PHASE_PAUSED,
                    "session": session,
                    "utterance_id": uid,
                    "error": f"processing failed: {exc}",
                }
            )
            return
        self._push_event(
            {
                "type": "result",
                "session": session,
                "utterance_id": uid,
                "conversation_id": outcome.get("conversation_id"),
                "transcript": outcome.get("transcript") or text,
                "response": outcome.get("response", ""),
                "trigger": "rtc",
            }
        )

    # -- firmware event entry points --------------------------------------

    async def _rtc_handle_streaming(self) -> None:
        """Firmware STREAMING: RTC active (resumed / initial stream flow)."""
        if time.monotonic() < self._rtc_event_ignore_until:
            # Initial-stream notification lagging behind the arm sequence.
            return
        if self._rtc_phase == RTC_PHASE_CAPTURING:
            return
        if self._rtc_phase in (RTC_PHASE_PAUSED, RTC_PHASE_FINALIZING):
            await self._rtc_begin_capture("device")
        # ARMING: initial stream start before our own AT+PAUSE; nothing to do.

    async def _rtc_handle_paused(self) -> None:
        """Firmware PAUSED: warm pause ends the current utterance."""
        if time.monotonic() < self._rtc_event_ignore_until:
            # The arm sequence's own PAUSED arriving after the arm finished.
            if self._rtc_phase == RTC_PHASE_ARMING:
                self._rtc_phase = RTC_PHASE_PAUSED
            return
        if self._rtc_phase == RTC_PHASE_ARMING:
            # The arm sequence's own AT+PAUSE; arming moves to PAUSED itself.
            self._rtc_phase = RTC_PHASE_PAUSED
            return
        await self._rtc_finalize_utterance("paused")

    async def _rtc_handle_idle(self, session: str | None) -> None:
        """Firmware IDLE while RTC armed: terminal stop (STOP/power-off)."""
        if not self._rtc_active:
            return
        if session and self._rtc_session and session != self._rtc_session:
            # STOP/timeout notifications are scheduled independently by the
            # firmware.  A delayed IDLE from the prior RTC session must never
            # tear down the receiver lease installed for its successor.
            logger.debug(
                "ignoring stale RTC IDLE for %s while %s is active",
                session,
                self._rtc_session,
            )
            return
        if self._rtc_phase == RTC_PHASE_ARMING:
            # A firmware watchdog can end the session before STREAM_START.
            # This is a retryable arm failure, not the user's terminal STOP.
            receiver = self._rtc_receiver
            if isinstance(receiver, RtcStreamReceiver):
                now = time.monotonic()
                age = max(0.0, now - self._rtc_arm_started_at)
                download_age = (
                    f"{max(0.0, now - self._rtc_download_accepted_at):.2f}s"
                    if self._rtc_download_accepted_at is not None
                    else "not-acknowledged"
                )
                firmware_status = (
                    self._rtc_device_event_status
                    if self._rtc_device_event_at >= self._rtc_arm_started_at
                    else "none"
                )
                receiver.fail_start(
                    "RTC session ended before stream start "
                    f"(session={self._rtc_session or session}, age={age:.2f}s, "
                    f"download_ack_age={download_age}, firmware_event={firmware_status})"
                )
            return
        await self._rtc_finalize_utterance("stopped")
        await self._rtc_mark_stopped()

    # -- web control -------------------------------------------------------

    async def rtc_resume(self) -> dict[str, Any]:
        """Resume the armed RTC session: start the next logical utterance."""
        if not self._rtc_active:
            raise ClipConflictError("RTC live stream is not armed")
        if self._rtc_phase == RTC_PHASE_CAPTURING:
            # Already listening (e.g. a physical double-click won the race).
            return {
                "accepted": True,
                "phase": self._rtc_phase,
                "session": self._rtc_session,
                "utterance_id": self._rtc_utterance_id,
            }
        try:
            await self._call(lambda: self._client.resume_recording())
        except ClipConflictError:
            pass  # firmware already resumed; the STREAMING event drives it
        await self._rtc_begin_capture("web")
        return {
            "accepted": True,
            "phase": self._rtc_phase,
            "session": self._rtc_session,
            "utterance_id": self._rtc_utterance_id,
        }

    async def rtc_pause(self) -> dict[str, Any]:
        """Pause the RTC session and finalize the current utterance."""
        if not self._rtc_active:
            raise ClipConflictError("RTC live stream is not armed")
        if self._rtc_phase in (RTC_PHASE_PAUSED, RTC_PHASE_ARMING):
            # Already paused: idempotent, no duplicate finalization.
            return {
                "accepted": True,
                "phase": RTC_PHASE_PAUSED,
                "session": self._rtc_session,
                "utterance_id": self._rtc_utterance_id,
            }
        try:
            await self._call(lambda: self._client.pause_recording())
        except ClipConflictError:
            pass  # a physical double-click may have paused first
        await self._rtc_handle_paused()
        return {
            "accepted": True,
            "phase": self._rtc_phase,
            "session": self._rtc_session,
            "utterance_id": self._rtc_utterance_id,
        }

    async def rtc_stop(self) -> dict[str, Any]:
        """Terminal RTC STOP: end the stream, detach, no re-arm until reconnect."""
        if not self._rtc_active:
            raise ClipConflictError("RTC live stream is not armed")
        session = self._rtc_session
        await self._rtc_finalize_utterance("stopped")
        try:
            result = await self._call(lambda: self._client.stop_recording())
        except (ClipConflictError, ClipCommandFailedError) as exc:
            result = {"error": str(exc)}
        await self._rtc_mark_stopped()
        return {"accepted": True, "session": session, "data": result}

    # -- arming / teardown -------------------------------------------------

    async def _arm_rtc(self) -> dict[str, Any]:
        """Auto-arm RTC after connect: start_rtc -> stream -> warm pause."""
        if self._rtc_active or self._rtc_phase == RTC_PHASE_STOPPED:
            return {"armed": False, "reason": f"rtc phase is {self._rtc_phase}"}
        if self._legacy_ingest_pending:
            return {"armed": False, "reason": "SD ingestion is pending"}
        async with self._operation_lock:
            # An ingestion can be queued while this coroutine is waiting for
            # another command to release the operation lock.
            if self._legacy_ingest_pending:
                return {"armed": False, "reason": "SD ingestion is pending"}
            return await self._arm_rtc_locked()

    async def _arm_rtc_locked(self) -> dict[str, Any]:
        if not self.is_connected or not self._ready:
            return {"armed": False, "reason": "not connected"}
        self._rtc_pre_roll.clear()
        self._rtc_start_accepted = False
        self._rtc_session_ready.clear()
        self._rtc_event_session = None
        self._rtc_arm_started_at = time.monotonic()
        self._rtc_download_accepted_at = None
        self._rtc_device_event_status = None
        self._rtc_device_event_at = 0.0
        self._rtc_phase = RTC_PHASE_ARMING
        self._rtc_last_error = None
        self._push_event(
            {
                "type": "rtc_state",
                "phase": RTC_PHASE_ARMING,
                "session": None,
                "utterance_id": self._rtc_utterance_id,
            }
        )
        try:
            session = await self._run_command(self._start_rtc_session)
        except Exception as exc:
            reconnect = self._rtc_start_accepted
            if reconnect:
                await self._rtc_cleanup_failed_arm()
            return await self._rtc_arm_failed(exc, reconnect=reconnect)
        self._rtc_session_ids.add(session)
        self._rtc_session = session
        # The response characteristic can replay START once on a freshly
        # subscribed BlueZ link.  Let that duplicate reach the queue before
        # send_command() performs its pre-DOWNLOAD drain; otherwise the replay
        # can arrive just after the drain and masquerade as DOWNLOAD's reply.
        await asyncio.sleep(RTC_START_RESPONSE_SETTLE_SECONDS)
        receiver = RtcStreamReceiver(on_frame=self._rtc_on_frame)
        self._rtc_receiver = receiver
        try:
            token = await self._run_command(
                lambda: self._start_rtc_stream_checked(session, receiver)
            )
        except Exception as exc:
            await self._rtc_cleanup_failed_arm()
            return await self._rtc_arm_failed(exc, reconnect=True)
        self._rtc_lease_token = token
        self._rtc_download_accepted_at = time.monotonic()
        try:
            await asyncio.wait_for(
                receiver.wait_start(timeout=settings.RTC_ARM_TIMEOUT),
                timeout=settings.RTC_ARM_TIMEOUT + 1.0,
            )
        except Exception as exc:
            # The stream never started; release both the receiver lease and
            # the device-side RTC session before a later retry.
            await self._rtc_cleanup_failed_arm()
            return await self._rtc_arm_failed(exc, reconnect=True)
        try:
            await self._run_command(lambda: self._client.pause_recording())
        except ClipConflictError:
            pass  # firmware already warm-paused
        except Exception as exc:
            await self._rtc_cleanup_failed_arm()
            return await self._rtc_arm_failed(exc)
        self._rtc_pre_roll.clear()  # discard initial stream frames
        self._rtc_event_ignore_until = (
            time.monotonic() + float(settings.RTC_SETTLE_SECONDS)
        )
        self._rtc_phase = RTC_PHASE_PAUSED
        self._rtc_arm_attempts = 0
        self._rtc_next_arm_at = 0.0
        self._push_event(
            {
                "type": "rtc_state",
                "phase": RTC_PHASE_PAUSED,
                "session": session,
                "utterance_id": self._rtc_utterance_id,
            }
        )
        return {"armed": True, "session": session, "phase": RTC_PHASE_PAUSED}

    async def _start_rtc_stream_checked(
        self, session: str, receiver: StreamReceiver
    ) -> int | None:
        """Attach the frame sink and accept only this DOWNLOAD's response.

        The wire protocol has no request id and BLE commands use Write Without
        Response.  After reconnect, a delayed duplicate START response can
        otherwise be consumed as the DOWNLOAD reply while the actual write is
        lost.  Retry a shape-mismatched reply inside the firmware's five-second
        watchdog; a real STREAM_START notification is independently
        authoritative even if its JSON acknowledgement was malformed.
        """
        transport = self._client.transport
        token = transport.set_file_frame_handler(receiver.feed)
        command = f"AT+DOWNLOAD={session}"
        last_response: dict[str, Any] | None = None
        try:
            for attempt in range(1, RTC_DOWNLOAD_RESPONSE_RETRIES + 1):
                response = await self._client.request(command)
                last_response = response
                data = response.get("data")
                response_matches = (
                    isinstance(data, dict)
                    and str(data.get("state", "")).lower() == "streaming"
                    and data.get("session") == session
                )
                if response_matches or (
                    receiver.started.is_set() and receiver.error is None
                ):
                    return token
                logger.warning(
                    "ignoring mismatched RTC DOWNLOAD response (%d/%d): %s",
                    attempt,
                    RTC_DOWNLOAD_RESPONSE_RETRIES,
                    response,
                )
                if attempt < RTC_DOWNLOAD_RESPONSE_RETRIES:
                    # Give an already-sent STREAM_START notification one event
                    # loop turn to arrive before retransmitting DOWNLOAD.
                    await asyncio.sleep(RTC_START_RESPONSE_SETTLE_SECONDS)
                    if receiver.error is not None:
                        raise receiver.error
                    if receiver.started.is_set():
                        return token
            raise ProtocolError(
                f"RTC DOWNLOAD response did not match session {session}: "
                f"{last_response}"
            )
        except Exception:
            if token is not None:
                transport.detach_file_frame_handler(token)
            else:
                transport.set_file_frame_handler(None)
            raise

    async def _start_rtc_session(self) -> str:
        """Start RTC and take its SID from the authoritative state event."""
        response = await self._client.request("AT+START=rtc")
        self._rtc_start_accepted = True
        data = response.get("data") if isinstance(response, dict) else None
        response_session = data.get("session") if isinstance(data, dict) else None

        # Firmware publishes STREAMING from the state machine before the
        # START handler returns JSON.  Require that fresh event because the
        # response characteristic can replay a plausible response belonging
        # to a prior, already-ended RTC session after reconnect.
        try:
            await asyncio.wait_for(self._rtc_session_ready.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        if self._rtc_event_session:
            if (
                isinstance(response_session, str)
                and response_session
                and response_session != self._rtc_event_session
            ):
                logger.warning(
                    "ignoring stale RTC START response SID %s; "
                    "state event reports %s",
                    response_session,
                    self._rtc_event_session,
                )
            return self._rtc_event_session
        raise ClipCommandFailedError(
            "RTC START response was not accompanied by a fresh STREAMING event "
            f"(response_session={response_session!r})"
        )

    async def _rtc_cleanup_failed_arm(self) -> None:
        """Roll back a partially-created RTC session without dropping BLE."""
        session_started = self._rtc_start_accepted or self._rtc_session is not None
        firmware_already_ended = (
            self._rtc_device_event_status == "timeout"
            and self._rtc_device_event_at >= self._rtc_arm_started_at
        )
        transport = getattr(self._client, "transport", None)
        token = self._rtc_lease_token
        # Mark inactive before AT+STOP can emit IDLE asynchronously; otherwise
        # that event races the failed-arm bookkeeping and can leave STOPPED.
        self._rtc_phase = RTC_PHASE_DISCONNECTED
        if transport is not None and token is not None:
            try:
                transport.detach_file_frame_handler(token)
            except Exception:
                pass
        self._rtc_lease_token = None
        self._rtc_receiver = None
        self._rtc_session = None
        self._rtc_start_accepted = False
        self._rtc_session_ready.clear()
        self._rtc_event_session = None
        self._rtc_pre_roll.clear()
        if session_started and self.is_connected and not firmware_already_ended:
            try:
                # The caller already owns _operation_lock. Use the SDK call
                # directly so rollback remains serialized without re-locking.
                await self._client.stop_recording()
            except Exception as stop_exc:
                logger.debug("clip RTC failed-arm STOP rejected: %s", stop_exc)

    async def _rtc_arm_failed(
        self, exc: Exception, *, reconnect: bool = False
    ) -> dict[str, Any]:
        """Record an arming failure and optionally retry on a fresh link."""
        self._rtc_last_error = str(exc)
        self._rtc_arm_attempts += 1
        retry_delay = RTC_ARM_RETRY_DELAYS[
            min(self._rtc_arm_attempts - 1, len(RTC_ARM_RETRY_DELAYS) - 1)
        ]
        self._rtc_next_arm_at = time.monotonic() + retry_delay
        self._rtc_phase = RTC_PHASE_DISCONNECTED
        logger.warning(
            "clip RTC arm failed (attempt %d; retry in %.0fs): %s",
            self._rtc_arm_attempts,
            retry_delay,
            exc,
        )
        self._push_event(
            {
                "type": "rtc_state",
                "phase": RTC_PHASE_DISCONNECTED,
                "session": None,
                "utterance_id": self._rtc_utterance_id,
                "error": str(exc),
                "retry_seconds": retry_delay,
            }
        )
        firmware_watchdog_after_download = (
            self._rtc_download_accepted_at is not None
            and self._rtc_device_event_status == "timeout"
            and self._rtc_device_event_at >= self._rtc_arm_started_at
        )
        if reconnect and self.is_connected and not firmware_watchdog_after_download:
            # An early IDLE or missing stream frame can race a late response
            # from the failed cleanup STOP.  Never issue the next GSTAT/START
            # on that response stream; reconnect gives the protocol a clean
            # queue and a fresh file-notification subscription.
            self._rtc_reconnect_for_arm_failure = True
            logger.warning(
                "discarding BLE connection after RTC arm failure; "
                "retrying on a fresh link after %.0fs",
                retry_delay,
            )
            await self._teardown_transport()
        elif reconnect and self.is_connected:
            # The firmware waits for a tight BLE connection interval before
            # emitting STREAM_START.  On a cold Linux/BlueZ link that update
            # can collide with its five-second START watchdog.  Reconnecting
            # here makes every attempt cold and can reproduce the collision
            # forever.  DOWNLOAD was acknowledged and the later STOP cleanup
            # was serialized, so this command stream is still trustworthy;
            # retain it and retry after the negotiated parameters settle.
            logger.warning(
                "retaining BLE connection after firmware RTC watchdog; "
                "retrying on the settled link after %.0fs",
                retry_delay,
            )
        return {"armed": False, "error": str(exc)}

    async def _rtc_mark_stopped(self) -> None:
        """Detach the lease and move to the terminal STOPPED phase."""
        transport = getattr(self._client, "transport", None)
        token = self._rtc_lease_token
        if transport is not None and token is not None:
            try:
                transport.detach_file_frame_handler(token)
            except Exception:
                pass
        self._rtc_lease_token = None
        self._rtc_receiver = None
        self._rtc_session = None
        self._rtc_start_accepted = False
        self._rtc_session_ready.clear()
        self._rtc_event_session = None
        self._rtc_utterance_frames = []
        self._rtc_utterance_truncated = False
        self._rtc_pre_roll.clear()
        self._rtc_phase = RTC_PHASE_STOPPED
        self._push_event(
            {
                "type": "rtc_state",
                "phase": RTC_PHASE_STOPPED,
                "session": None,
                "utterance_id": self._rtc_utterance_id,
            }
        )

    async def _rtc_abort(self, *, reason: str, finalize: bool) -> None:
        """Idempotent cleanup of an armed RTC session (BLE loss/shutdown)."""
        was_active = self._rtc_active
        if not was_active and self._rtc_lease_token is None:
            return
        if finalize and self._rtc_phase == RTC_PHASE_CAPTURING:
            await self._rtc_finalize_utterance(reason)
        if self._rtc_capture_task is not None and not self._rtc_capture_task.done():
            self._rtc_capture_task.cancel()
            self._rtc_capture_task.add_done_callback(_swallow_task_result)
        self._rtc_capture_task = None
        transport = getattr(self._client, "transport", None)
        token = self._rtc_lease_token
        if transport is not None and token is not None:
            try:
                transport.detach_file_frame_handler(token)
            except Exception:
                pass
        self._rtc_lease_token = None
        self._rtc_receiver = None
        self._rtc_session = None
        self._rtc_start_accepted = False
        self._rtc_session_ready.clear()
        self._rtc_event_session = None
        self._rtc_utterance_frames = []
        self._rtc_utterance_truncated = False
        self._rtc_pre_roll.clear()
        self._rtc_partial_inflight = False
        self._rtc_phase = RTC_PHASE_DISCONNECTED
        if was_active:
            self._push_event(
                {
                    "type": "rtc_state",
                    "phase": RTC_PHASE_DISCONNECTED,
                    "session": None,
                    "utterance_id": self._rtc_utterance_id,
                    "reason": reason,
                }
            )

    # ------------------------------------------------------------------
    # Status payload
    # ------------------------------------------------------------------

    @staticmethod
    def _status_payload_from(status: Any) -> dict[str, Any]:
        return {
            "state": getattr(status, "state", "UNKNOWN"),
            "recording": bool(getattr(status, "recording", False)),
            "session": getattr(status, "session_id", None),
            "duration_seconds": int(getattr(status, "duration_seconds", 0)),
            "battery_percent": int(getattr(status, "battery_percent", 0)),
            "charging": bool(getattr(status, "charging", False)),
            "temperature_c": getattr(status, "temperature_c", None),
            "mode": getattr(status, "mode", ""),
            "bitrate": int(getattr(status, "bitrate", 0)),
            "free_space_mb": int(getattr(status, "free_space_mb", 0)),
            "device_name": getattr(status, "device_name", ""),
        }

    async def status_payload(self) -> dict[str, Any]:
        payload = dict(self._last_status_payload or {})
        payload.update(
            {
                "connected": self._ready,
                "device_id": self.device_id,
                "recording": self._recording,
                "session": self._recording_session,
                "transfer_active": self._transfer_active,
                "last_error": self._last_error,
                "input_mode": settings.VOICE_INPUT_MODE,
                "record_mode": settings.CLIP_RECORD_MODE,
                "agent_enabled": self.agent_enabled,
                "rtc_phase": self._rtc_phase,
                "rtc_session": self._rtc_session,
                "rtc_utterance_id": self._rtc_utterance_id,
                "rtc_partial_transcript": self._rtc_partial_text,
                "rtc_processing": self._rtc_finalize_pending > 0,
                "rtc_error": self._rtc_last_error,
            }
        )
        return payload

    # ------------------------------------------------------------------
    # Ingestion workflow
    # ------------------------------------------------------------------

    async def _request_ingest(
        self,
        session_id: str,
        *,
        trigger: str,
        conversation_id: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Idempotent enqueue: repeated events / retries never duplicate work."""
        if not session_id:
            return {"accepted": False, "reason": "missing session id"}
        if self._rtc_active:
            # RTC owns the single file-frame channel; SD ingestion would steal
            # stream frames. Never route RTC sessions into the SD pipeline.
            return {
                "accepted": False,
                "session": session_id,
                "reason": "RTC live stream is armed; SD ingestion disabled",
            }
        row = store.get_ingestion(self.device_id, session_id)
        if row is None:
            # A stopped session immediately enters the ingestion workflow:
            # persist the row (status=stopped) so a dashboard/reconnect can see it.
            store.upsert_ingestion(
                self.device_id,
                session_id,
                status="stopped",
                trigger=trigger,
                conversation_id=conversation_id or self._active_conversation,
            )
        else:
            status = row.get("status")
            if status in (
                "completed",
                "downloading",
                "processing",
            ):
                return {
                    "accepted": False,
                    "session": session_id,
                    "status": status,
                    "reason": "already handled",
                }
            if not force and status in ("failed", "ignored_existing"):
                return {
                    "accepted": False,
                    "session": session_id,
                    "status": status,
                    "reason": "retry via POST ingest",
                }
            store.upsert_ingestion(
                self.device_id,
                session_id,
                status="stopped",
                trigger=trigger,
                conversation_id=(
                    conversation_id
                    or row.get("conversation_id")
                    or self._active_conversation
                ),
            )
        if session_id in self._queued_sessions:
            return {
                "accepted": True,
                "session": session_id,
                "status": "queued",
                "reason": "already queued",
            }
        request = IngestRequest(
            session_id=session_id,
            trigger=trigger,
            conversation_id=conversation_id or self._active_conversation,
            force=force,
        )
        self._queued_sessions.add(session_id)
        self._ingest_queue.put_nowait(request)
        return {"accepted": True, "session": session_id, "status": "queued"}

    async def manual_ingest(
        self, session_id: str, *, trigger: str = "manual"
    ) -> dict[str, Any]:
        conversation_id = self._active_conversation
        row = store.get_ingestion(self.device_id, session_id)
        if row is not None and row.get("conversation_id") and not conversation_id:
            conversation_id = row["conversation_id"]
        return await self._request_ingest(
            session_id, trigger=trigger, conversation_id=conversation_id, force=True
        )

    async def _ingest_loop(self) -> None:
        while True:
            request = await self._ingest_queue.get()
            self._queued_sessions.discard(request.session_id)
            try:
                await self._process_ingest(request)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("clip ingest crashed for %s: %s", request.session_id, exc)
                try:
                    store.mark_failed(self.device_id, request.session_id, str(exc))
                    self._push_event(
                        {
                            "type": "workflow",
                            "session": request.session_id,
                            "status": "failed",
                            "error": str(exc),
                        }
                    )
                except Exception:
                    pass

    async def _exchange_session(self, request: IngestRequest, result: Any) -> None:
        """Device-gateway path for a stopped session: keep the Ogg, skip the AI.

        The re-containerized session audio is the deliverable, so it is left in
        place (unlike the agent path, which deletes it after transcribing) and
        announced with a ``session_audio`` event pointing at its URL.
        """
        sid = request.session_id
        device = self.device_id
        try:
            ogg_path = await asyncio.to_thread(self.ogg_converter, result.output_dir)
            size = await asyncio.to_thread(lambda path=Path(ogg_path): path.stat().st_size)
        except Exception as exc:
            logger.exception("clip session audio exchange failed for %s: %s", sid, exc)
            store.mark_failed(device, sid, str(exc))
            self._push_event(
                {"type": "workflow", "session": sid, "status": "failed", "error": str(exc)}
            )
            self._retain_failed_artifacts()
            return
        store.mark_completed(
            device,
            sid,
            transcript="",
            response="",
            conversation_id=request.conversation_id,
            trigger=request.trigger,
        )
        self._push_event(
            {"type": "workflow", "session": sid, "status": "completed", "mode": "exchange"}
        )
        self._push_event(
            {
                "type": "session_audio",
                "session": sid,
                "url": session_audio_url(sid),
                "bytes": size,
                "content_type": "audio/ogg",
                "trigger": request.trigger,
            }
        )

    async def _process_ingest(self, request: IngestRequest) -> None:
        sid = request.session_id
        device = self.device_id
        row = store.get_ingestion(device, sid)
        if row is None:
            store.upsert_ingestion(
                device,
                sid,
                status="stopped",
                trigger=request.trigger,
                conversation_id=request.conversation_id,
            )
        else:
            status = row.get("status")
            if not request.force and status in (
                "completed",
                "ignored_existing",
                "downloading",
                "processing",
            ):
                return
            if not request.force and status == "failed":
                return
            store.upsert_ingestion(
                device,
                sid,
                status="stopped",
                trigger=request.trigger,
                conversation_id=request.conversation_id or row.get("conversation_id"),
            )
        self._push_event(
            {
                "type": "workflow",
                "session": sid,
                "status": "stopped",
                "trigger": request.trigger,
            }
        )

        store.mark_status(device, sid, "downloading")
        self._push_event({"type": "workflow", "session": sid, "status": "downloading"})
        try:
            result = await self._download_session(sid)
        except (
            ClipUnavailableError,
            ClipCommandFailedError,
            ClipTransferFailedError,
            OpusFormatError,
        ) as exc:
            store.mark_failed(device, sid, str(exc))
            self._push_event(
                {
                    "type": "workflow",
                    "session": sid,
                    "status": "failed",
                    "error": str(exc),
                }
            )
            self._retain_failed_artifacts()
            return
        except Exception as exc:
            logger.exception("clip download failed for %s: %s", sid, exc)
            store.mark_failed(device, sid, str(exc))
            self._push_event(
                {
                    "type": "workflow",
                    "session": sid,
                    "status": "failed",
                    "error": str(exc),
                }
            )
            self._retain_failed_artifacts()
            return

        store.mark_status(device, sid, "processing")
        self._push_event({"type": "workflow", "session": sid, "status": "processing"})
        if not self.agent_enabled:
            await self._exchange_session(request, result)
            return
        try:
            ogg_path = self.ogg_converter(result.output_dir)
            conversation_id = request.conversation_id or self._active_conversation
            outcome = await asyncio.to_thread(
                self.audio_service.process_audio_file,
                ogg_path,
                filename="clip.ogg",
                conversation_id=conversation_id,
            )
            transcript = outcome["transcript"]
            response = outcome["response"]
            conversation_id = outcome["conversation_id"]
            store.mark_completed(
                device,
                sid,
                transcript=transcript,
                response=response,
                conversation_id=conversation_id,
                trigger=request.trigger,
            )
            self._push_event(
                {
                    "type": "workflow",
                    "session": sid,
                    "status": "completed",
                    "conversation_id": conversation_id,
                }
            )
            self._push_event(
                {
                    "type": "result",
                    "session": sid,
                    "conversation_id": conversation_id,
                    "transcript": transcript,
                    "response": response,
                    "trigger": request.trigger,
                }
            )
            self._cleanup_session_audio(Path(result.output_dir), ogg_path=ogg_path)
        except Exception as exc:
            logger.exception("clip processing failed for %s: %s", sid, exc)
            store.mark_failed(device, sid, str(exc))
            self._push_event(
                {
                    "type": "workflow",
                    "session": sid,
                    "status": "failed",
                    "error": str(exc),
                }
            )
            self._retain_failed_artifacts()

    async def _download_session(self, session_id: str) -> Any:
        """Download a session through the manager-level operation lock.

        Transfer interference is prevented because heartbeat/status try-locks
        the same lock and skips while a transfer holds it.  BLE can briefly
        drop as the firmware leaves recording mode; cancel a receiver that was
        attached to the old link, wait for the supervisor to reconnect, and
        retry from the device instead of appearing stuck until the SDK's
        download timeout expires.
        """
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        last_error: Exception | None = None
        self._download_retry_owner = True
        try:
            for attempt in range(1, DOWNLOAD_MAX_ATTEMPTS + 1):
                if not self._ready or not self.is_connected:
                    self._download_reconnect_requested = True
                    try:
                        await asyncio.wait_for(
                            self._connection_ready.wait(),
                            timeout=DOWNLOAD_RECONNECT_TIMEOUT,
                        )
                    except asyncio.TimeoutError as exc:
                        raise ClipUnavailableError(
                            "Clip did not reconnect in time for download"
                        ) from exc
                    finally:
                        self._download_reconnect_requested = False

                try:
                    return await self._download_session_once(session_id)
                except (ClipUnavailableError, ClipTransferFailedError) as exc:
                    last_error = exc
                    if attempt >= DOWNLOAD_MAX_ATTEMPTS or self._stopping.is_set():
                        raise
                    logger.warning(
                        "clip download attempt %d/%d failed for %s: %s; waiting for reconnect",
                        attempt,
                        DOWNLOAD_MAX_ATTEMPTS,
                        session_id,
                        exc,
                    )
                    # _download_session_once has already detached and closed
                    # the old receiver before control reaches this block.
                    await self._teardown_transport()
                    self._download_reconnect_requested = True
                    try:
                        await asyncio.wait_for(
                            self._connection_ready.wait(),
                            timeout=DOWNLOAD_RECONNECT_TIMEOUT,
                        )
                    except asyncio.TimeoutError as wait_exc:
                        raise ClipUnavailableError(
                            "Clip did not reconnect in time for download retry"
                        ) from wait_exc
                    finally:
                        self._download_reconnect_requested = False

            assert last_error is not None
            raise last_error
        finally:
            self._download_reconnect_requested = False
            self._download_retry_owner = False

    async def _download_session_once(self, session_id: str) -> Any:
        """Run one transfer and abort promptly if its BLE link disappears."""
        async with self._operation_lock:
            if not self._ready or not self.is_connected:
                raise ClipUnavailableError("Clip is not connected or is reconnecting")
            if self._rtc_active:
                raise ClipConflictError(
                    "RTC live stream holds the file-frame channel"
                )
            self._transfer_active = True
            self._transport_lost.clear()
            download_task = asyncio.create_task(
                self.session_downloader(
                    self._client,
                    session_id,
                    self._temp_dir,
                    timeout=settings.CLIP_DOWNLOAD_TIMEOUT,
                )
            )
            lost_task = asyncio.create_task(self._transport_lost.wait())
            try:
                done, _ = await asyncio.wait(
                    (download_task, lost_task),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if lost_task in done and not download_task.done():
                    download_task.cancel()
                    await asyncio.gather(download_task, return_exceptions=True)
                    raise ClipUnavailableError("BLE connection lost during download")
                return await download_task
            except (TransferError, TransferTimeoutError) as exc:
                raise ClipTransferFailedError(str(exc)) from exc
            except CommandError as exc:
                # A rejected session command (most importantly "Session not
                # found" for a stale/live-only RTC id) says nothing about BLE
                # health.  Do not turn it into a reconnect loop.
                raise ClipCommandFailedError(str(exc)) from exc
            except (CommandTimeoutError, ClipConnectionError, ProtocolError) as exc:
                raise ClipUnavailableError(f"transfer interrupted: {exc}") from exc
            except (ClipUnavailableError, ClipTransferFailedError):
                raise
            except Exception as exc:
                raise ClipTransferFailedError(str(exc)) from exc
            finally:
                lost_task.cancel()
                await asyncio.gather(lost_task, return_exceptions=True)
                if not download_task.done():
                    download_task.cancel()
                    await asyncio.gather(download_task, return_exceptions=True)
                self._transfer_active = False

    # ------------------------------------------------------------------
    # Artifact management
    # ------------------------------------------------------------------

    def _cleanup_session_audio(self, session_dir: Path, ogg_path: Path | None = None) -> None:
        """Delete successful local temporary audio after ingestion."""
        try:
            if ogg_path is not None:
                ogg_path = Path(ogg_path)
                ogg_path.unlink(missing_ok=True)
            if session_dir.is_dir():
                for child in list(session_dir.iterdir()):
                    if child.is_file():
                        child.unlink(missing_ok=True)
                session_dir.rmdir()
        except OSError as exc:
            logger.warning("clip temp cleanup failed for %s: %s", session_dir, exc)

    def _retain_failed_artifacts(self) -> None:
        """Bound failed-artifact retention to a recent window (best effort)."""
        try:
            if not self._temp_dir.is_dir():
                return
            failed_dirs = sorted(
                (
                    p
                    for p in self._temp_dir.iterdir()
                    if p.is_dir() and (p / "session.json").exists()
                ),
                key=lambda p: p.stat().st_mtime,
            )
            max_keep = max(1, int(settings.CLIP_MAX_FAILED_ARTIFACTS))
            for old in failed_dirs[:-max_keep] if len(failed_dirs) > max_keep else []:
                import shutil

                (self._temp_dir / f"{old.name}.ogg").unlink(missing_ok=True)
                shutil.rmtree(old, ignore_errors=True)
        except Exception as exc:
            logger.warning("clip failed-artifact cleanup error: %s", exc)
