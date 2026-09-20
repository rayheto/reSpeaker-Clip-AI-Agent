"""ClipRuntime lifecycle tests using a fake transport (deterministic, no BLE)."""

import asyncio
import struct
import threading
from types import SimpleNamespace

import pytest

from clip.transports.base import BaseTransport
from clip.exceptions import (
    CommandError,
    CommandTimeoutError,
    ConnectionError as ClipConnectionError,
)

from config import settings
from backend.clip import store
from backend.clip.exceptions import (
    ClipCommandFailedError,
    ClipConflictError,
    ClipUnavailableError,
)
from backend.clip.runtime import (
    ClipRuntime,
    IngestRequest,
    reconnect_delay_seconds,
)


def run(coro):
    """Synchronous helper that runs an async test body."""
    return asyncio.run(coro)


def stream_start_frame(sid: str) -> bytes:
    """Encode a STREAM_START frame (type 0x13) for an RTC session."""
    return bytes([0x13, len(sid)]) + sid.encode("ascii")


def stream_data_frame(sequence: int, payload: bytes) -> bytes:
    """Encode a STREAM_DATA frame (type 0x14)."""
    return struct.pack("<BHH", 0x14, sequence & 0xFFFF, len(payload)) + payload


def stream_end_frame(reason: int = 0) -> bytes:
    """Encode a STREAM_END frame (type 0x15)."""
    return bytes([0x15, reason])


class FakeTransport(BaseTransport):
    """In-memory BaseTransport mimicking the Clip BLE/AT protocol.

    ``rtc_mode`` models the RTC live-stream path: ``AT+START=rtc`` arms a
    session, ``AT+DOWNLOAD=<sid>`` emits STREAM_START, ``AT+PAUSE`` /
    ``AT+RESUME`` move between PAUSED and STREAMING with state notifications,
    and ``AT+STOP`` ends the stream (IDLE + STREAM_END). Legacy SD recording
    keeps the original behavior.
    """

    def __init__(self):
        super().__init__()
        self.connected = False
        self.commands: list[str] = []
        self.connect_calls = 0
        self.fail_connects = 0
        self.timeout_commands: set[str] = set()
        self.reject_commands: dict[str, str] = {}
        self.status_state = "IDLE"
        self.status_recording = False
        self.status_session: str | None = None
        self.session_items: list[dict] = []
        self._session_counter = 0
        self.max_concurrent = 0
        self._concurrent = 0
        # RTC live-stream model
        self.rtc_mode = False
        self.rtc_session: str | None = None
        self.last_rtc_session: str | None = None
        self._rtc_counter = 0
        self.rtc_seq = 0
        self.rtc_stream_start_count = 1
        self.rtc_start_session_omitted = False
        self.retain_rtc_session_on_stop = False
        self.missing_session_error = False
        self.rtc_idle_on_download = False
        self.rtc_stale_download_responses = 0

    def _next_rtc_session(self) -> str:
        self._rtc_counter += 1
        return f"2026990{self._rtc_counter:07d}"

    def emit_stream_data(self, payload: bytes, sequence: int | None = None) -> int:
        """Deliver one STREAM_DATA frame through the file-frame handler."""
        seq = self.rtc_seq if sequence is None else sequence
        self.rtc_seq = seq + 1
        self._emit_file_frame(stream_data_frame(seq, payload))
        return seq

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def connect(self):
        self.connect_calls += 1
        if self.fail_connects > 0:
            self.fail_connects -= 1
            raise ClipConnectionError("no BLE device found (fake)")
        self.connected = True
        self._emit_event({"event": "ble", "status": "connected"})

    async def disconnect(self):
        self.connected = False

    # -- helpers ---------------------------------------------------------

    def _next_session(self) -> str:
        self._session_counter += 1
        return f"2026082{self._session_counter:07d}"

    def emit_state(self, state: str, session: str | None, duration: int | None = None):
        payload = {"event": "state", "state": state, "session": session}
        if duration is not None:
            payload["duration"] = duration
        self._emit_event(payload)

    # -- protocol ----------------------------------------------------------

    async def send_command(self, command: str, *, timeout: float) -> dict:
        self._concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self._concurrent)
        try:
            self.commands.append(command)
            if command in self.timeout_commands:
                raise CommandTimeoutError(f"no response to {command}")
            reject_key = next(
                (key for key in self.reject_commands if command.startswith(key)),
                None,
            )
            if reject_key is not None:
                raise CommandError(
                    self.reject_commands[reject_key],
                    command=command,
                    response={
                        "ok": False,
                        "msg": self.reject_commands[reject_key],
                    },
                )
            if command == "AT+GSTAT":
                return {
                    "ok": True,
                    "data": {
                        "state": self.status_state,
                        "recording": self.status_recording,
                        "session": self.status_session,
                        "duration": 12,
                        "battery": 90,
                        "charging": False,
                        "temp": 30,
                        "voltage": 3800,
                        "mode": "enhanced",
                        "bitrate": 64000,
                        "free_space": 5000,
                        "device": "Clip",
                    },
                }
            if command == "AT+START=rtc":
                sid = self._next_rtc_session()
                self.rtc_mode = True
                self.rtc_session = sid
                self.last_rtc_session = sid
                self.status_state = "STREAMING"
                self.status_session = sid
                self.emit_state("STREAMING", sid)
                await asyncio.sleep(0)  # let the notification task run
                return {
                    "ok": True,
                    "data": {} if self.rtc_start_session_omitted else {"session": sid},
                }
            if command.startswith("AT+START"):
                sid = self._next_session()
                self.status_state = "RECORDING"
                self.status_recording = True
                self.status_session = sid
                self.emit_state("RECORDING", sid)
                await asyncio.sleep(0)
                return {"ok": True, "data": {"session": sid}}
            if command == "AT+STOP":
                if self.rtc_mode:
                    sid = self.rtc_session
                    self.rtc_mode = False
                    self.rtc_session = None
                    self.status_state = "IDLE"
                    self.status_recording = False
                    self.status_session = (
                        sid if self.retain_rtc_session_on_stop else None
                    )
                    self.emit_state("IDLE", sid, duration=0)
                    self._emit_file_frame(stream_end_frame(0))
                    await asyncio.sleep(0)
                    return {"ok": True, "data": {"session": sid, "duration": 0}}
                sid = self.status_session
                self.status_state = "IDLE"
                self.status_recording = False
                self.status_session = None
                self.emit_state("IDLE", sid, duration=12)
                await asyncio.sleep(0)
                return {"ok": True, "data": {"session": sid, "duration": 12}}
            if command == "AT+DEVICE?":
                # AT+DEVICE? returns the name at the top level (not in data).
                return {"ok": True, "device": "Clip"}
            if command == "AT+BATT?":
                return {
                    "ok": True,
                    "data": {
                        "battery": 90,
                        "charging": False,
                        "temp": 30,
                        "voltage": 3800,
                    },
                }
            if command.startswith("AT+LIST") and "=" not in command:
                return {
                    "ok": True,
                    "data": {
                        "sessions": self.session_items,
                        "total": len(self.session_items),
                    },
                }
            if command.startswith("AT+LIST=") and self.missing_session_error:
                raise CommandError(
                    "Session not found",
                    command=command,
                    response={"ok": False, "msg": "Session not found"},
                )
            if command.startswith("AT+DOWNLOAD=") and self.rtc_mode:
                target = command.split("=", 1)[1].split(":", 1)[0]
                if target == self.rtc_session:
                    if self.rtc_stale_download_responses > 0:
                        self.rtc_stale_download_responses -= 1
                        # A delayed duplicate START response consumed while
                        # the current Write Without Response was lost.
                        return {
                            "ok": True,
                            "data": {"session": target, "mode": "rtc"},
                        }
                    if self.rtc_idle_on_download:
                        self.rtc_mode = False
                        self.status_state = "IDLE"
                        self.status_recording = False
                        self._emit_event({"event": "rtc", "status": "timeout"})
                        self.emit_state("IDLE", target, duration=5)
                        await asyncio.sleep(0)
                        return {
                            "ok": True,
                            "data": {"state": "streaming", "session": target},
                        }
                    for _ in range(self.rtc_stream_start_count):
                        self._emit_file_frame(stream_start_frame(target))
                    await asyncio.sleep(0)
                    return {
                        "ok": True,
                        "data": {"state": "streaming", "session": target},
                    }
            if command == "AT+PAUSE":
                if self.rtc_mode and self.status_state == "STREAMING":
                    self.status_state = "PAUSED"
                    self.emit_state("PAUSED", self.rtc_session)
                    await asyncio.sleep(0)
                    return {"ok": True, "data": {}}
                raise CommandError(
                    "not recording",
                    command=command,
                    response={"ok": False, "msg": "not recording"},
                )
            if command == "AT+RESUME":
                if self.rtc_mode and self.status_state == "PAUSED":
                    self.status_state = "STREAMING"
                    self.emit_state("STREAMING", self.rtc_session)
                    await asyncio.sleep(0)
                    return {"ok": True, "data": {}}
                raise CommandError(
                    "not paused",
                    command=command,
                    response={"ok": False, "msg": "not paused"},
                )
            return {"ok": True, "data": {}}
        finally:
            self._concurrent -= 1


@pytest.fixture(autouse=True)
def clip_db(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "SUPABASE_URL", "")
    monkeypatch.setattr(settings, "SUPABASE_KEY", "")
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite:///{tmp_path}/clip.db")
    monkeypatch.setattr(settings, "CLIP_TEMP_DIR", str(tmp_path / "clip_audio"))
    store.init_clip_ingestions()


def make_runtime(
    transport: FakeTransport | None = None,
    device_id: str = "Clip",
    rtc_auto_arm: bool = False,
) -> ClipRuntime:
    """Build a runtime; RTC auto-arm is opt-in so legacy tests are untouched."""
    return ClipRuntime(
        transport=transport or FakeTransport(),
        device_id=device_id,
        rtc_auto_arm=rtc_auto_arm,
    )


# ---------------------------------------------------------------------------
# Backoff / serialization / timeout-reconnect
# ---------------------------------------------------------------------------

class TestSupervisor:
    def test_ble_settings_are_trimmed(self, monkeypatch):
        transport = FakeTransport()
        captured = {}

        def make_transport(*, address, name):
            captured.update(address=address, name=name)
            return transport

        monkeypatch.setattr(settings, "CLIP_BLE_ADDRESS", "  C4:F1:79:A4:09:A0  ")
        monkeypatch.setattr(settings, "CLIP_BLE_NAME", "  reSpeaker Clip  ")
        monkeypatch.setattr("backend.clip.runtime.BleTransport", make_transport)

        runtime = ClipRuntime()

        assert captured == {
            "address": "C4:F1:79:A4:09:A0",
            "name": "reSpeaker Clip",
        }
        assert runtime.device_id == "C4:F1:79:A4:09:A0"

        named_runtime = ClipRuntime(transport=FakeTransport(), device_id="  local Clip  ")
        assert named_runtime.device_id == "local Clip"

    def test_reconnect_delay_sequence(self):
        assert [reconnect_delay_seconds(i) for i in range(6)] == [1, 2, 4, 8, 16, 30]
        assert reconnect_delay_seconds(99) == 30.0

    def test_session_listing_uses_stable_plain_first_page(self):
        async def body():
            runtime = make_runtime()
            calls = []
            first_page = tuple(SimpleNamespace(id=f"S{i}") for i in range(10))

            async def list_sessions(*, page_number, per_page):
                calls.append((page_number, per_page))
                return first_page

            runtime._client.list_sessions = list_sessions
            sessions = await runtime._list_device_sessions()

            assert len(sessions) == 10
            assert calls == [(1, 10)]

        run(body())

    def test_reconnect_does_not_run_paginated_history_scan(self):
        async def body():
            transport = FakeTransport()
            runtime = make_runtime(transport)
            await runtime._connect()
            store.mark_baseline_complete(runtime.device_id)
            scans = 0

            async def failed_scan():
                nonlocal scans
                scans += 1
                raise CommandTimeoutError("no response to AT+LIST")

            runtime._discover_sessions_locked = failed_scan
            await runtime._on_connected()

            assert runtime._ready is True
            assert runtime._connection_ready.is_set()
            assert runtime.is_connected is True
            assert scans == 0

        run(body())

    def test_jittered_delay_bounds(self):
        from backend.clip.runtime import _jittered

        for _ in range(200):
            value = _jittered(1.0)
            assert 0.6 <= value <= 1.4
        for _ in range(200):
            value = _jittered(30.0)
            assert 18.0 <= value <= 42.0

    def test_serialized_lifecycle_single_command_at_a_time(self):
        async def body():
            transport = FakeTransport()
            runtime = make_runtime(transport)
            await runtime._connect()
            await asyncio.gather(
                runtime._call(lambda: runtime._client.device_name()),
                runtime._call(lambda: runtime._client.status()),
                runtime._call(lambda: runtime._client.battery()),
            )
            assert transport.max_concurrent == 1
            assert transport.commands.count("AT+GSTAT") == 1

        run(body())

    def test_timeout_tears_down_transport_then_reconnects(self):
        async def body():
            transport = FakeTransport()
            transport.timeout_commands = {"AT+DEVICE?"}
            runtime = make_runtime(transport)
            await runtime._connect()
            assert runtime.is_connected

            with pytest.raises(ClipUnavailableError):
                await runtime._call(lambda: runtime._client.device_name())
            assert not runtime.is_connected  # transport torn down

            await runtime._connect()  # supervisor-style clean reconnect
            assert runtime.is_connected
            assert "AT+DEVICE?" in transport.commands

        run(body())

    def test_rejected_command_maps_to_command_failed(self):
        async def body():
            transport = FakeTransport()
            transport.reject_commands["AT+DEVICE?"] = "Unknown command"
            runtime = make_runtime(transport)
            await runtime._connect()
            with pytest.raises(ClipCommandFailedError):
                await runtime._call(lambda: runtime._client.device_name())

        run(body())

    def test_conflict_hint_maps_to_conflict(self):
        async def body():
            transport = FakeTransport()
            transport.reject_commands["AT+START"] = "Already recording or invalid state"
            runtime = make_runtime(transport)
            await runtime._connect()
            with pytest.raises(ClipConflictError):
                await runtime.start_recording(mode="enhanced")

        run(body())

    def test_no_heartbeat_during_download(self):
        async def body():
            transport = FakeTransport()
            runtime = make_runtime(transport)
            await runtime._connect()
            started = asyncio.Event()
            release = asyncio.Event()

            async def held_download(sid):
                async with runtime._operation_lock:
                    started.set()
                    await release.wait()
                return SimpleNamespace(output_dir="/tmp/fake")

            runtime._download_session = held_download
            task = asyncio.create_task(runtime._download_session("S1"))
            await started.wait()

            commands_before = len(transport.commands)
            await runtime._heartbeat_once()
            assert len(transport.commands) == commands_before  # skipped, no GSTAT

            release.set()
            await task

        run(body())

    def test_link_loss_cancels_stale_download_receiver(self):
        async def body():
            transport = FakeTransport()
            runtime = make_runtime(transport)
            await runtime._connect()
            runtime._ready = True
            runtime._connection_ready.set()
            started = asyncio.Event()
            cancelled = asyncio.Event()

            async def hanging_download(*_args, **_kwargs):
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

            runtime.session_downloader = hanging_download
            task = asyncio.create_task(runtime._download_session_once("S-LOST"))
            await started.wait()

            transport.connected = False
            runtime._ready = False
            runtime._transport_lost.set()

            with pytest.raises(ClipUnavailableError, match="lost during download"):
                await asyncio.wait_for(task, timeout=1)
            assert cancelled.is_set()
            assert runtime._transfer_active is False

        run(body())

    def test_interrupted_download_waits_for_reconnect_and_retries(self, monkeypatch):
        async def body():
            transport = FakeTransport()
            runtime = make_runtime(transport)
            await runtime._connect()
            runtime._ready = True
            runtime._connection_ready.set()
            attempts = 0
            expected = SimpleNamespace(output_dir="/tmp/retried")

            async def flaky_download(*_args, **_kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise ClipConnectionError("test link drop")
                return expected

            async def reconnect_after_teardown():
                while transport.connected:
                    await asyncio.sleep(0)
                transport.connected = True
                runtime._ready = True
                runtime._transport_lost.clear()
                runtime._connection_ready.set()

            runtime.session_downloader = flaky_download
            reconnect_task = asyncio.create_task(reconnect_after_teardown())
            result = await runtime._download_session("S-RETRY")
            await reconnect_task

            assert result is expected
            assert attempts == 2
            assert runtime._transfer_active is False

        run(body())

    def test_session_not_found_is_terminal_without_ble_reconnect(self):
        async def body():
            transport = FakeTransport()
            transport.missing_session_error = True
            runtime = make_runtime(transport)
            await runtime._connect()
            runtime._ready = True
            runtime._connection_ready.set()

            with pytest.raises(ClipCommandFailedError, match="Session not found"):
                await runtime._download_session("00000000000388")

            assert runtime.is_connected is True
            assert transport.connect_calls == 1
            assert transport.commands.count("AT+LIST=00000000000388") == 1

        run(body())


# ---------------------------------------------------------------------------
# Web START / STOP and events
# ---------------------------------------------------------------------------

class TestRecording:
    def test_web_start_stop_records_and_enqueues(self):
        async def body():
            runtime = make_runtime()
            await runtime._connect()

            start = await runtime.start_recording(mode="enhanced")
            assert start["session"]
            assert runtime._recording
            assert runtime._recording_session == start["session"]

            stop = await runtime.stop_recording()
            assert stop["accepted"]
            assert stop["session"] == start["session"]
            assert not runtime._recording

            row = store.get_ingestion(runtime.device_id, start["session"])
            assert row is not None
            assert row["status"] == "stopped"
            assert row["trigger"] == "web"

        run(body())

    def test_start_while_recording_is_conflict(self):
        async def body():
            runtime = make_runtime()
            await runtime._connect()
            await runtime.start_recording()
            with pytest.raises(ClipConflictError):
                await runtime.start_recording()
            # stop while recording is the normal path, not a conflict
            stop = await runtime.stop_recording()
            assert stop["accepted"] is True

        run(body())

    def test_stop_when_idle_is_conflict(self):
        async def body():
            runtime = make_runtime()
            await runtime._connect()
            with pytest.raises(ClipConflictError):
                await runtime.stop_recording()

        run(body())

    def test_start_while_offline_unavailable(self):
        async def body():
            runtime = make_runtime()
            with pytest.raises(ClipUnavailableError):
                await runtime.start_recording()

        run(body())

    def test_physical_button_events(self):
        async def body():
            transport = FakeTransport()
            runtime = make_runtime(transport)
            await runtime._connect()

            transport.emit_state("RECORDING", "202608210000001")
            await _await_until(lambda: runtime._recording_session == "202608210000001")
            assert runtime._recording
            assert runtime._recording_session == "202608210000001"
            recording_row = store.get_ingestion(runtime.device_id, "202608210000001")
            assert recording_row is not None
            assert recording_row["status"] == "recording"
            assert recording_row["trigger"] == "physical"

            transport.emit_state("IDLE", "202608210000001", duration=7)
            await _await_until(lambda: not runtime._recording)
            row = store.get_ingestion(runtime.device_id, "202608210000001")
            assert row is not None and row["status"] == "stopped"

        run(body())

    def test_repeated_idle_event_does_not_duplicate(self):
        async def body():
            transport = FakeTransport()
            runtime = make_runtime(transport)
            await runtime._connect()

            transport.emit_state("RECORDING", "202608210000002")
            await _await_until(lambda: runtime._recording)
            transport.emit_state("IDLE", "202608210000002")
            await _await_until(lambda: not runtime._recording)
            # The stop happened once: only one ingestion request may be queued.
            assert len(runtime._queued_sessions) == 1

            transport.emit_state("IDLE", "202608210000002")  # repeated event
            await asyncio.sleep(0.02)
            # Still exactly one request queued — repeated events never duplicate.
            assert runtime._queued_sessions == {"202608210000002"}
            assert len(runtime._event_history) <= 6

        run(body())

    def test_missed_event_reconciled_on_reconnect(self):
        async def body():
            transport = FakeTransport()
            # While we were offline the user pressed the physical button.
            transport.status_state = "RECORDING"
            transport.status_recording = True
            transport.status_session = "202608210000003"
            runtime = make_runtime(transport)
            await runtime._connect()
            await runtime._on_connected()

            assert runtime._recording
            assert runtime._recording_session == "202608210000003"

            # Device stopped while connected: polling convergence.
            transport.status_state = "IDLE"
            transport.status_recording = False
            transport.status_session = None
            status = await runtime._client.status()
            await runtime._apply_status(status)
            await _await_until(lambda: not runtime._recording)
            row = store.get_ingestion(runtime.device_id, "202608210000003")
            assert row is not None and row["status"] == "stopped"

        run(body())


# ---------------------------------------------------------------------------
# Baseline / discovery / ingestion
# ---------------------------------------------------------------------------

class TestIngestion:
    def test_first_start_baseline_marks_ignored(self):
        async def body():
            transport = FakeTransport()
            transport.session_items = [
                {"id": "OLD1", "files": 2, "size": 100, "bookmarks": 0},
                {"id": "OLD2", "files": 1, "size": 50, "bookmarks": 1},
            ]
            runtime = make_runtime(transport)
            await runtime._connect()
            await runtime._on_connected()
            rows = store.list_recent_ingestions(runtime.device_id, limit=10)
            ignored = {r["session_id"] for r in rows if r["status"] == "ignored_existing"}
            assert ignored == {"OLD1", "OLD2"}

        run(body())

    def test_reconnect_discovery_queues_untracked_sessions(self):
        async def body():
            transport = FakeTransport()
            runtime = make_runtime(transport)
            await runtime._connect()
            await runtime._on_connected()  # first baseline, empty

            transport.session_items = [
                {"id": "NEW1", "files": 2, "size": 100, "bookmarks": 0}
            ]
            result = await runtime._discovery_scan()
            assert "NEW1" in runtime._queued_sessions

        run(body())

    def test_baseline_is_not_repeated_after_process_restart(self):
        async def body():
            first_transport = FakeTransport()
            first_transport.session_items = [
                {"id": "OLD1", "files": 1, "size": 10, "bookmarks": 0}
            ]
            first = make_runtime(first_transport, device_id="PersistentClip")
            await first._connect()
            await first._on_connected()
            assert store.is_baseline_complete("PersistentClip")
            await first._teardown_transport()

            second_transport = FakeTransport()
            second_transport.session_items = [
                {"id": "OLD1", "files": 1, "size": 10, "bookmarks": 0},
                {"id": "NEW1", "files": 1, "size": 10, "bookmarks": 0},
            ]
            second_transport.status_session = "NEW1"
            second = make_runtime(second_transport, device_id="PersistentClip")
            await second._connect()
            await second._on_connected()

            assert "NEW1" in second._queued_sessions
            assert "OLD1" not in second._queued_sessions
            assert store.get_ingestion("PersistentClip", "NEW1")["status"] == "stopped"

        run(body())

    def test_restart_recovers_interrupted_ingestion(self):
        async def body():
            store.mark_baseline_complete("Clip")
            store.upsert_ingestion(
                "Clip", "INTERRUPTED1", status="processing", trigger="physical"
            )
            runtime = make_runtime()
            await runtime._connect()
            await runtime._on_connected()
            assert "INTERRUPTED1" in runtime._queued_sessions
            assert store.get_ingestion("Clip", "INTERRUPTED1")["status"] == "stopped"

        run(body())

    def test_idempotent_enqueue_rejects_duplicates(self):
        async def body():
            runtime = make_runtime()
            first = await runtime._request_ingest("S1", trigger="physical")
            second = await runtime._request_ingest("S1", trigger="physical")
            assert first["accepted"] is True
            assert second["accepted"] is True  # queued once
            assert second.get("reason") == "already queued"
            assert len(runtime._queued_sessions) == 1

        run(body())

    def test_already_completed_is_not_reprocessed(self):
        async def body():
            runtime = make_runtime()
            store.upsert_ingestion(
                runtime.device_id, "DONE1", status="completed", trigger="web"
            )
            result = await runtime._request_ingest("DONE1", trigger="reconnect")
            assert result["accepted"] is False
            assert result["status"] == "completed"
            manual = await runtime.manual_ingest("DONE1")
            assert manual["accepted"] is False
            assert manual["status"] == "completed"

        run(body())

    def test_failed_requires_manual_retry(self):
        async def body():
            runtime = make_runtime()
            store.mark_failed(runtime.device_id, "FAIL1", "transfer timeout")
            denied = await runtime._request_ingest("FAIL1", trigger="reconnect")
            assert denied["accepted"] is False
            assert denied["status"] == "failed"
            accepted = await runtime.manual_ingest("FAIL1", trigger="manual")
            assert accepted["accepted"] is True

        run(body())

    def test_full_ingestion_pipeline_completes(self):
        async def body():
            runtime = make_runtime()
            await runtime._connect()

            async def fake_download(sid):
                return SimpleNamespace(output_dir=f"/tmp/{sid}")

            runtime._download_session = fake_download
            runtime.ogg_converter = lambda session_dir: f"{session_dir}.ogg"
            runtime.audio_service = SimpleNamespace(
                process_audio_file=lambda *a, **k: {
                    "transcript": "hello clip",
                    "response": "Hi there!",
                    "conversation_id": "conv-1",
                }
            )

            await runtime._request_ingest("S100", trigger="web")
            # hand-run the queued request
            await runtime._process_ingest(
                IngestRequest(session_id="S100", trigger="web")
            )

            row = store.get_ingestion(runtime.device_id, "S100")
            assert row["status"] == "completed"
            assert row["transcript"] == "hello clip"
            assert row["response"] == "Hi there!"
            assert row["conversation_id"] == "conv-1"

            events = runtime.event_history()
            types = [e["type"] for e in events]
            assert "result" in types
            result_event = next(e for e in events if e["type"] == "result")
            assert result_event["transcript"] == "hello clip"

        run(body())

    def test_processing_failure_marks_failed_and_keeps_artifacts(self, tmp_path):
        async def body():
            runtime = make_runtime()
            await runtime._connect()

            async def fake_download(sid):
                return SimpleNamespace(output_dir=str(tmp_path / "S200"))

            def bad_ogg(session_dir):
                raise ValueError("conversion exploded")

            runtime._download_session = fake_download
            runtime.ogg_converter = bad_ogg
            await runtime._process_ingest(
                IngestRequest(session_id="S200", trigger="physical")
            )
            row = store.get_ingestion(runtime.device_id, "S200")
            assert row["status"] == "failed"
            assert "conversion exploded" in row["error"]

        run(body())

    def test_download_failure_marks_failed(self):
        async def body():
            from backend.clip.exceptions import ClipTransferFailedError

            runtime = make_runtime()
            await runtime._connect()

            async def failing_download(sid):
                raise ClipTransferFailedError("transfer timed out")

            runtime._download_session = failing_download
            await runtime._process_ingest(
                IngestRequest(session_id="S300", trigger="physical")
            )
            row = store.get_ingestion(runtime.device_id, "S300")
            assert row["status"] == "failed"
            assert "transfer" in row["error"]

        run(body())

    def test_active_conversation_is_used(self):
        async def body():
            runtime = make_runtime()
            await runtime.set_active_conversation("active-1")
            assert runtime._active_conversation == "active-1"
            await runtime.set_active_conversation(None)
            assert runtime._active_conversation is None

        run(body())

    def test_cloud_processing_runs_off_the_ble_event_loop(self, tmp_path):
        async def body():
            runtime = make_runtime()
            await runtime._connect()
            event_loop_thread = threading.get_ident()
            worker_threads = []

            session_dir = tmp_path / "S400"
            session_dir.mkdir()
            ogg_path = tmp_path / "S400.ogg"
            ogg_path.write_bytes(b"ogg")

            async def fake_download(_sid):
                return SimpleNamespace(output_dir=str(session_dir))

            def process(*_args, **_kwargs):
                worker_threads.append(threading.get_ident())
                return {
                    "transcript": "off loop",
                    "response": "ok",
                    "conversation_id": "conv-off-loop",
                }

            runtime._download_session = fake_download
            runtime.ogg_converter = lambda _path: ogg_path
            runtime.audio_service = SimpleNamespace(process_audio_file=process)
            await runtime._process_ingest(
                IngestRequest(session_id="S400", trigger="physical")
            )
            assert worker_threads and worker_threads[0] != event_loop_thread

        run(body())

    def test_failed_artifact_retention_removes_matching_ogg(self, monkeypatch, tmp_path):
        runtime = make_runtime()
        runtime._temp_dir = tmp_path
        monkeypatch.setattr(settings, "CLIP_MAX_FAILED_ARTIFACTS", 1)
        for sid in ("OLD", "NEW"):
            directory = tmp_path / sid
            directory.mkdir()
            (directory / "session.json").write_text("{}", encoding="utf-8")
            (tmp_path / f"{sid}.ogg").write_bytes(b"partial")
        # Ensure deterministic age order.
        (tmp_path / "OLD" / "session.json").touch()
        import os
        os.utime(tmp_path / "OLD", (1, 1))
        os.utime(tmp_path / "NEW", (2, 2))

        runtime._retain_failed_artifacts()
        assert not (tmp_path / "OLD").exists()
        assert not (tmp_path / "OLD.ogg").exists()
        assert (tmp_path / "NEW").exists()
        assert (tmp_path / "NEW.ogg").exists()


def test_event_replay_survives_bounded_history_rotation():
    runtime = make_runtime()
    for index in range(305):
        runtime._push_event({"type": "workflow", "index": index})
    stream = runtime.iter_events(after_id=299)
    snapshot = next(stream)
    assert snapshot["type"] == "connection"
    replayed = [next(stream) for _ in range(5)]
    assert [item["index"] for item in replayed] == [300, 301, 302, 303, 304]


async def _await_until(predicate, timeout: float = 1.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met before timeout")
