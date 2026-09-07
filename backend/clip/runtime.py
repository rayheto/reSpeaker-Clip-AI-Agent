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
from collections import deque
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Any, Callable

from clip import BleTransport, ClipClient
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
from backend.clip.ogg import OpusFormatError, convert_session_to_ogg
from backend.clip.transfer import download_session_compatible
from backend.services.audio_service import AudioService

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
)


@dataclass
class IngestRequest:
    session_id: str
    trigger: str = "physical"
    conversation_id: str | None = None
    force: bool = False


def _jittered(delay: float) -> float:
    return max(0.05, delay * (1.0 + JITTER_FRACTION * (2.0 * random.random() - 1.0)))


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

        # Injected collaborators (overridden in tests)
        self.audio_service = AudioService()
        self.ogg_converter = convert_session_to_ogg
        self.session_downloader = download_session_compatible

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        """Start the supervisor and ingestion loop (called by the worker)."""
        self._ingest_task = asyncio.create_task(self._ingest_loop(), name="clip-ingest")
        self._supervisor_task = asyncio.create_task(self._supervisor(), name="clip-supervisor")
        try:
            await self._stopping.wait()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        self._stopping.set()
        for task in (self._supervisor_task, self._ingest_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._supervisor_task, self._ingest_task):
            if task is not None and not task.done():
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        await self._teardown_transport()

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
        if event == "state":
            state = str(payload.get("state", "")).upper()
            session: str | None = payload.get("session") if isinstance(payload.get("session"), str) else None
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
        if sid:
            await self._request_ingest(sid, trigger=trigger)

    def _poll_recording_active(self, status: Any) -> bool:
        return str(getattr(status, "state", "")).upper() in ("RECORDING", "PAUSED")

    async def _apply_status(self, status: Any) -> None:
        """Reconcile polling against observed events (idempotent)."""
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
        elif not recording and sid and self._baseline_done:
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
        except (ClipUnavailableError, ClipTransferFailedError, OpusFormatError) as exc:
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
            except (CommandTimeoutError, ClipConnectionError, ProtocolError, CommandError) as exc:
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
