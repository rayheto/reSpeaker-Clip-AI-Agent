"""RTC live-stream runtime tests (fake transport, deterministic, no BLE).

Covers the warm-pause utterance engine: auto-arm command order, physical
double-click STREAMING/PAUSED events, web resume/pause, >= 20 repeated cycles,
exactly-once finalization, stale lease cleanup, BLE loss/re-arm, SD-safety,
bounded frame admission, partial revision/stale suppression, exactly one
process_transcript, and decoupled next-capture-during-LLM.
"""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from config import settings
from backend.clip import store
from backend.clip.exceptions import ClipConflictError
from backend.clip.runtime import (
    RTC_PHASE_CAPTURING,
    RTC_PHASE_DISCONNECTED,
    RTC_PHASE_PAUSED,
    RTC_PHASE_STOPPED,
)
from backend.clip.runtime import ClipRuntime
from tests.test_clip_runtime import FakeTransport, make_runtime


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def clip_db(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "SUPABASE_URL", "")
    monkeypatch.setattr(settings, "SUPABASE_KEY", "")
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite:///{tmp_path}/rtc.db")
    monkeypatch.setattr(settings, "CLIP_TEMP_DIR", str(tmp_path / "clip_audio"))
    # Deterministic tests: notification settle is covered by its own test.
    monkeypatch.setattr(settings, "RTC_SETTLE_SECONDS", 0.0)
    store.init_clip_ingestions()


async def _until(predicate, timeout: float = 2.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition was not met before timeout")


def _frame(seed: int = 0) -> bytes:
    return b"\xf8\x01\x02\x03" + bytes([seed & 0xFF])


async def _arm(runtime, transport) -> str:
    await runtime._connect()
    await runtime._on_connected()
    assert runtime._rtc_phase == RTC_PHASE_PAUSED
    assert transport.rtc_session is not None
    return runtime._rtc_session


async def _next_job(runtime):
    job = await runtime._rtc_finalize_queue.get()
    runtime._rtc_finalize_pending = max(0, runtime._rtc_finalize_pending - 1)
    return job


# ---------------------------------------------------------------------------
# Arming
# ---------------------------------------------------------------------------

class TestArming:
    def test_auto_arm_command_order_and_armed_paused(self):
        async def body():
            transport = FakeTransport()
            runtime = make_runtime(transport, rtc_auto_arm=True)
            await runtime._connect()
            await runtime._on_connected()

            assert runtime._rtc_phase == RTC_PHASE_PAUSED
            assert runtime._rtc_session == transport.rtc_session
            assert runtime._rtc_receiver is not None
            assert runtime._rtc_lease_token is not None
            assert transport.connected
            # Order: status, baseline list, then start_rtc -> stream -> pause.
            commands = transport.commands
            assert commands[0] == "AT+GSTAT"
            assert "AT+START=rtc" in commands
            start_index = commands.index("AT+START=rtc")
            assert commands[start_index + 1].startswith("AT+DOWNLOAD=")
            assert commands[start_index + 2] == "AT+PAUSE"
            # Wrong direction is impossible: no RESUME during arming.
            assert "AT+RESUME" not in commands

            payload = await runtime.status_payload()
            assert payload["rtc_phase"] == RTC_PHASE_PAUSED
            assert payload["rtc_session"] == transport.rtc_session
            assert payload["rtc_processing"] is False

            types = [e["type"] for e in runtime.event_history()]
            assert "rtc_state" in types
            assert "result" not in types

        run(body())

    def test_mismatched_start_reply_is_not_accepted_as_download_ack(self):
        async def body():
            transport = FakeTransport()
            transport.rtc_stale_download_responses = 1
            runtime = make_runtime(transport, rtc_auto_arm=True)

            await runtime._connect()
            await runtime._on_connected()

            assert runtime._rtc_phase == RTC_PHASE_PAUSED
            downloads = [
                command
                for command in transport.commands
                if command.startswith("AT+DOWNLOAD=")
            ]
            assert len(downloads) == 2
            assert downloads[0] == downloads[1]
            assert runtime._rtc_arm_attempts == 0

        run(body())

    def test_empty_start_response_uses_streaming_event_session(self):
        async def body():
            transport = FakeTransport()
            transport.rtc_start_session_omitted = True
            runtime = make_runtime(transport, rtc_auto_arm=True)

            await runtime._connect()
            await runtime._on_connected()

            assert runtime._rtc_phase == RTC_PHASE_PAUSED
            assert runtime._rtc_session == transport.last_rtc_session
            assert transport.commands.count("AT+START=rtc") == 1
            assert any(command.startswith("AT+DOWNLOAD=") for command in transport.commands)

        run(body())

    def test_settle_window_ignores_stale_initial_notifications(self, monkeypatch):
        async def body():
            monkeypatch.setattr(settings, "RTC_SETTLE_SECONDS", 2.0)
            transport = FakeTransport()
            runtime = make_runtime(transport, rtc_auto_arm=True)
            await runtime._connect()
            await runtime._on_connected()
            assert runtime._rtc_phase == RTC_PHASE_PAUSED

            # STREAMING/PAUSED notifications right after arming are the lagging
            # initial flow: they must not start or finalize an utterance.
            transport.emit_state("STREAMING", transport.rtc_session)
            transport.emit_state("PAUSED", transport.rtc_session)
            await asyncio.sleep(0.05)

            assert runtime._rtc_phase == RTC_PHASE_PAUSED
            assert runtime._rtc_utterance_id is None
            assert runtime._rtc_finalize_pending == 0

        run(body())

    def test_arm_failure_keeps_connection_alive(self):
        async def body():
            transport = FakeTransport()
            transport.reject_commands["AT+START"] = "invalid mode"
            runtime = make_runtime(transport, rtc_auto_arm=True)
            await runtime._connect()
            await runtime._on_connected()

            assert runtime._rtc_phase == RTC_PHASE_DISCONNECTED
            assert runtime._rtc_arm_attempts == 1
            assert runtime._rtc_last_error
            assert transport.connected  # BLE stays up
            assert runtime._ready is True

        run(body())

    def test_partial_arm_failure_stops_session_and_releases_receiver(self):
        async def body():
            transport = FakeTransport()
            transport.reject_commands["AT+PAUSE"] = "codec failure"
            runtime = make_runtime(transport, rtc_auto_arm=True)
            await runtime._connect()
            await runtime._on_connected()

            assert runtime._rtc_phase == RTC_PHASE_DISCONNECTED
            assert runtime._rtc_session is None
            assert runtime._rtc_receiver is None
            assert runtime._rtc_lease_token is None
            assert transport.rtc_mode is False
            assert transport.commands[-1] == "AT+STOP"
            assert transport.connected

        run(body())

    def test_failed_arm_retained_rtc_session_is_never_downloaded(self, monkeypatch):
        async def body():
            monkeypatch.setattr(settings, "RTC_ARM_TIMEOUT", 0.01)
            transport = FakeTransport()
            # Match real firmware: no STREAM_START arrives before the RTC arm
            # deadline, and GSTAT retains the live-only session after STOP.
            transport.rtc_stream_start_count = 0
            transport.retain_rtc_session_on_stop = True
            runtime = make_runtime(transport, rtc_auto_arm=True)

            await runtime._connect()
            await runtime._on_connected()

            sid = transport.last_rtc_session
            assert sid is not None
            assert runtime._rtc_phase == RTC_PHASE_DISCONNECTED
            assert transport.status_state == "IDLE"
            assert transport.status_session == sid

            # A missing STREAM_START discards the suspect link before retry.
            assert runtime.is_connected is False
            transport.rtc_stream_start_count = 1
            runtime._rtc_next_arm_at = 0.0
            await runtime._connect()
            await runtime._on_connected()

            # Reconnect GSTAT retains the prior live-only SID. It must not be
            # misclassified as an SD recording or enter the download queue.
            assert sid not in runtime._queued_sessions
            assert store.get_ingestion(runtime.device_id, sid) is None
            assert runtime.is_connected is True
            assert runtime._rtc_phase == RTC_PHASE_PAUSED
            assert runtime._rtc_session != sid
            assert transport.connect_calls == 2

        run(body())

    def test_identical_duplicate_stream_start_before_data_is_tolerated(self):
        async def body():
            transport = FakeTransport()
            transport.rtc_stream_start_count = 2
            runtime = make_runtime(transport, rtc_auto_arm=True)

            await runtime._connect()
            await runtime._on_connected()

            assert runtime._rtc_phase == RTC_PHASE_PAUSED
            assert runtime._rtc_receiver is not None
            assert runtime._rtc_receiver.error is None
            assert transport.commands.count("AT+START=rtc") == 1

        run(body())

    def test_stale_idle_from_prior_rtc_session_does_not_stop_successor(self):
        async def body():
            transport = FakeTransport()
            runtime = make_runtime(transport, rtc_auto_arm=True)
            await runtime._connect()
            await runtime._on_connected()

            current = runtime._rtc_session
            assert current is not None
            await runtime._handle_event(
                {
                    "event": "state",
                    "state": "IDLE",
                    "session": "00000000000001",
                    "duration": 5,
                }
            )

            assert runtime._rtc_phase == RTC_PHASE_PAUSED
            assert runtime._rtc_session == current
            assert runtime._rtc_receiver is not None

        run(body())

    def test_idle_during_arming_is_retryable_failure_not_terminal_stop(self):
        async def body():
            transport = FakeTransport()
            transport.rtc_idle_on_download = True
            runtime = make_runtime(transport, rtc_auto_arm=True)

            await runtime._connect()
            await runtime._on_connected()

            assert runtime._rtc_phase == RTC_PHASE_DISCONNECTED
            assert runtime._rtc_arm_attempts == 1
            assert runtime._rtc_last_error.startswith(
                "RTC session ended before stream start"
            )
            assert "firmware_event=timeout" in runtime._rtc_last_error
            assert runtime.is_connected is True
            assert "AT+STOP" not in transport.commands

            # Keep the warm link so its RTC connection-parameter negotiation
            # can settle before attempt 2.
            transport.rtc_idle_on_download = False
            runtime._rtc_next_arm_at = 0.0
            await runtime._arm_rtc()
            assert runtime._rtc_phase == RTC_PHASE_PAUSED
            assert runtime._rtc_arm_attempts == 0
            assert runtime.is_connected is True
            assert transport.connect_calls == 1

        run(body())

    def test_firmware_watchdog_retries_on_settled_link_beyond_three_failures(self):
        async def body():
            transport = FakeTransport()
            transport.rtc_idle_on_download = True
            runtime = make_runtime(transport, rtc_auto_arm=True)

            for attempt in range(1, 4):
                runtime._rtc_next_arm_at = 0.0
                if attempt == 1:
                    await runtime._connect()
                    await runtime._on_connected()
                else:
                    await runtime._arm_rtc()
                assert runtime._rtc_arm_attempts == attempt
                assert runtime.is_connected is True

            assert transport.commands.count("AT+START=rtc") == 3
            assert runtime._rtc_phase == RTC_PHASE_DISCONNECTED

            # Retry remains available after three failures, with its delay
            # capped by the runtime instead of becoming permanently disabled.
            transport.rtc_idle_on_download = False
            runtime._rtc_next_arm_at = 0.0
            await runtime._arm_rtc()
            assert runtime._rtc_phase == RTC_PHASE_PAUSED
            assert runtime._rtc_arm_attempts == 0
            assert transport.commands.count("AT+START=rtc") == 4
            assert transport.connect_calls == 1

        run(body())

    def test_reconnect_during_download_retry_does_not_auto_arm_rtc(self):
        async def body():
            transport = FakeTransport()
            runtime = make_runtime(transport, rtc_auto_arm=True)
            runtime._download_retry_owner = True

            await runtime._connect()
            await runtime._on_connected()

            assert runtime._ready is True
            assert runtime._rtc_phase == RTC_PHASE_DISCONNECTED
            assert "AT+START=rtc" not in transport.commands

            runtime._download_retry_owner = False
            result = await runtime._arm_rtc()
            assert result["armed"] is True

        run(body())

    def test_restart_recovery_queue_prevents_rtc_auto_arm(self):
        async def body():
            store.mark_baseline_complete("Clip")
            store.upsert_ingestion(
                "Clip", "RECOVERED1", status="downloading", trigger="restart"
            )
            transport = FakeTransport()
            runtime = make_runtime(transport, rtc_auto_arm=True)

            await runtime._connect()
            await runtime._on_connected()

            assert "RECOVERED1" in runtime._queued_sessions
            assert runtime._rtc_phase == RTC_PHASE_DISCONNECTED
            assert "AT+START=rtc" not in transport.commands

            # The guard also lives inside _arm_rtc so a concurrent supervisor
            # cannot bypass the _on_connected call-site check.
            result = await runtime._arm_rtc()
            assert result == {"armed": False, "reason": "SD ingestion is pending"}
            assert "AT+START=rtc" not in transport.commands

        run(body())

    def test_arm_waits_for_operation_lock(self):
        async def body():
            transport = FakeTransport()
            runtime = make_runtime(transport, rtc_auto_arm=True)
            await runtime._connect()
            runtime._ready = True

            await runtime._operation_lock.acquire()
            task = asyncio.create_task(runtime._arm_rtc())
            await asyncio.sleep(0)
            assert "AT+START=rtc" not in transport.commands
            runtime._operation_lock.release()

            result = await task
            assert result["armed"] is True
            assert transport.max_concurrent == 1

        run(body())

    def test_no_auto_arm_when_legacy_disabled(self):
        async def body():
            transport = FakeTransport()
            runtime = make_runtime(transport, rtc_auto_arm=False)
            await runtime._connect()
            await runtime._on_connected()
            assert runtime._rtc_phase == RTC_PHASE_DISCONNECTED
            assert "AT+START=rtc" not in transport.commands

        run(body())


# ---------------------------------------------------------------------------
# Utterance cycles (physical + web)
# ---------------------------------------------------------------------------

class TestUtterances:
    def test_physical_double_click_cycles_utterances(self):
        async def body():
            transport = FakeTransport()
            runtime = make_runtime(transport, rtc_auto_arm=True)
            sid = await _arm(runtime, transport)

            transport.emit_state("STREAMING", sid)
            await _until(lambda: runtime._rtc_phase == RTC_PHASE_CAPTURING)
            uid1 = runtime._rtc_utterance_id
            assert uid1 == 1
            for index in range(20):
                transport.emit_stream_data(_frame(index))

            transport.emit_state("PAUSED", sid)
            await _until(lambda: runtime._rtc_phase == RTC_PHASE_PAUSED)
            assert runtime._rtc_finalize_pending == 1
            job = await _next_job(runtime)
            assert job["utterance_id"] == uid1
            assert len(job["frames"]) == 20

            # Next double-click starts a fresh utterance.
            transport.emit_state("STREAMING", sid)
            await _until(lambda: runtime._rtc_phase == RTC_PHASE_CAPTURING)
            assert runtime._rtc_utterance_id == uid1 + 1

        run(body())

    def test_web_resume_pause_20_cycles_no_disconnect(self, monkeypatch):
        async def body():
            monkeypatch.setattr(settings, "RTC_MAX_PENDING_FINALIZE", 64)
            transport = FakeTransport()
            runtime = make_runtime(transport, rtc_auto_arm=True)
            await _arm(runtime, transport)

            seen = []
            for _ in range(25):
                resume = await runtime.rtc_resume()
                assert resume["accepted"] is True
                assert runtime._rtc_phase == RTC_PHASE_CAPTURING
                seen.append(runtime._rtc_utterance_id)
                for _ in range(3):
                    transport.emit_stream_data(_frame())
                pause = await runtime.rtc_pause()
                assert pause["accepted"] is True
                assert runtime._rtc_phase == RTC_PHASE_PAUSED

            assert seen == list(range(1, 26))
            assert transport.connect_calls == 1
            assert transport.connected
            assert transport.commands.count("AT+RESUME") == 25
            assert transport.commands.count("AT+PAUSE") == 26  # 1 arm + 25 web
            assert runtime._rtc_finalize_pending == 25

        run(body())

    def test_duplicate_resume_is_idempotent(self):
        async def body():
            transport = FakeTransport()
            runtime = make_runtime(transport, rtc_auto_arm=True)
            await _arm(runtime, transport)

            first = await runtime.rtc_resume()
            uid = runtime._rtc_utterance_id
            second = await runtime.rtc_resume()  # already capturing
            assert first["accepted"] is True
            assert second["accepted"] is True
            assert runtime._rtc_utterance_id == uid
            assert transport.commands.count("AT+RESUME") == 1

        run(body())

    def test_pre_roll_seeds_next_utterance(self):
        async def body():
            transport = FakeTransport()
            runtime = make_runtime(transport, rtc_auto_arm=True)
            await _arm(runtime, transport)

            # Frames arriving just before/around the resume are tentatively
            # buffered and become the first frames of the next utterance.
            for index in range(6):
                transport.emit_stream_data(_frame(index))
            assert len(runtime._rtc_pre_roll) == 6

            await runtime.rtc_resume()
            assert len(runtime._rtc_utterance_frames) == 6
            assert runtime._rtc_partial_text == ""

        run(body())

    def test_bounded_frame_admission(self, monkeypatch):
        async def body():
            monkeypatch.setattr(settings, "RTC_MAX_UTTERANCE_FRAMES", 10)
            transport = FakeTransport()
            runtime = make_runtime(transport, rtc_auto_arm=True)
            await _arm(runtime, transport)
            await runtime.rtc_resume()

            for index in range(50):
                transport.emit_stream_data(_frame(index))
            assert len(runtime._rtc_utterance_frames) == 10
            assert runtime._rtc_utterance_truncated is True

            await runtime.rtc_pause()
            job = await _next_job(runtime)
            assert len(job["frames"]) == 10
            assert job["truncated"] is True

        run(body())


# ---------------------------------------------------------------------------
# Finalization: exactly once + LLM pipeline
# ---------------------------------------------------------------------------

class TestFinalize:
    def test_finalization_exactly_once_under_pause_races(self):
        async def body():
            transport = FakeTransport()
            runtime = make_runtime(transport, rtc_auto_arm=True)
            sid = await _arm(runtime, transport)
            await runtime.rtc_resume()
            for _ in range(10):
                transport.emit_stream_data(_frame())
            uid = runtime._rtc_utterance_id

            # Web pause + firmware PAUSED event + a duplicate event race.
            await runtime.rtc_pause()
            transport.emit_state("PAUSED", sid)
            transport.emit_state("PAUSED", sid)
            await runtime.rtc_pause()  # idempotent now
            await asyncio.sleep(0.05)

            assert runtime._rtc_phase == RTC_PHASE_PAUSED
            assert runtime._rtc_finalize_pending == 1
            job = await _next_job(runtime)
            assert job["utterance_id"] == uid
            assert len(job["frames"]) == 10

        run(body())

    def test_final_correction_then_exactly_one_process_transcript(self, monkeypatch):
        async def body():
            monkeypatch.setattr(settings, "RTC_MIN_UTTERANCE_FRAMES", 5)
            transport = FakeTransport()
            runtime = make_runtime(transport, rtc_auto_arm=True)
            runtime.rtc_stt_final = lambda frames: "final corrected text"
            calls = []

            def process(transcript, conversation_id=None):
                calls.append((transcript, conversation_id))
                return {
                    "transcript": transcript,
                    "response": "ok",
                    "conversation_id": "conv-rtc-1",
                }

            runtime.audio_service = SimpleNamespace(process_transcript=process)
            await _arm(runtime, transport)
            await runtime.rtc_resume()
            for _ in range(10):
                transport.emit_stream_data(_frame())
            await runtime.rtc_pause()

            job = await _next_job(runtime)
            await runtime._rtc_finalize_job(job)

            assert calls == [("final corrected text", None)]
            events = runtime.event_history()
            finals = [e for e in events if e["type"] == "transcript" and e.get("final")]
            assert finals and finals[-1]["text"] == "final corrected text"
            results = [e for e in events if e["type"] == "result"]
            assert len(results) == 1
            assert results[0]["response"] == "ok"
            assert results[0]["utterance_id"] == job["utterance_id"]
            assert runtime._rtc_finalize_pending == 0

        run(body())

    def test_finalize_queue_is_bounded_drops_oldest(self, monkeypatch):
        async def body():
            monkeypatch.setattr(settings, "RTC_MAX_PENDING_FINALIZE", 3)
            transport = FakeTransport()
            runtime = make_runtime(transport, rtc_auto_arm=True)
            await _arm(runtime, transport)

            for _ in range(5):
                await runtime.rtc_resume()
                for _ in range(5):
                    transport.emit_stream_data(_frame())
                await runtime.rtc_pause()

            assert runtime._rtc_finalize_pending == 3
            # Oldest utterances were dropped first; uid 1 is gone.
            job = await _next_job(runtime)
            assert job["utterance_id"] == 3

        run(body())

    def test_empty_utterance_skips_llm(self):
        async def body():
            transport = FakeTransport()
            runtime = make_runtime(transport, rtc_auto_arm=True)

            def forbidden(*_args, **_kwargs):
                raise AssertionError("STT must not run for an empty utterance")

            runtime.rtc_stt_final = forbidden
            runtime.audio_service = SimpleNamespace(
                process_transcript=forbidden
            )
            await _arm(runtime, transport)
            await runtime.rtc_resume()
            for _ in range(2):  # below RTC_MIN_UTTERANCE_FRAMES
                transport.emit_stream_data(_frame())
            await runtime.rtc_pause()

            job = await _next_job(runtime)
            await runtime._rtc_finalize_job(job)

            events = runtime.event_history()
            finals = [e for e in events if e["type"] == "transcript" and e.get("final")]
            assert finals and finals[-1].get("skipped") == "too short"
            assert not [e for e in events if e["type"] == "result"]

        run(body())

    def test_next_capture_starts_while_prior_llm_runs(self, monkeypatch):
        async def body():
            monkeypatch.setattr(settings, "RTC_MIN_UTTERANCE_FRAMES", 5)
            gate = threading.Event()
            started = threading.Event()

            def slow_process(transcript, conversation_id=None):
                started.set()
                gate.wait(timeout=10)
                return {
                    "transcript": transcript,
                    "response": "ok",
                    "conversation_id": "conv-rtc-2",
                }

            transport = FakeTransport()
            runtime = make_runtime(transport, rtc_auto_arm=True)
            runtime.rtc_stt_partial = lambda frames: None
            runtime.rtc_stt_final = lambda frames: "llm text"
            runtime.audio_service = SimpleNamespace(
                process_transcript=slow_process
            )
            finalizer = asyncio.create_task(runtime._rtc_finalize_loop())
            try:
                await _arm(runtime, transport)
                await runtime.rtc_resume()
                uid1 = runtime._rtc_utterance_id
                for _ in range(10):
                    transport.emit_stream_data(_frame())
                await runtime.rtc_pause()
                # Finalize worker picks the job up and blocks in the LLM step.
                await _until(lambda: runtime._rtc_finalize_queue.empty())
                await _until(lambda: started.is_set())

                # The next utterance must start without waiting for the LLM.
                resume = await runtime.rtc_resume()
                assert resume["accepted"] is True
                assert runtime._rtc_phase == RTC_PHASE_CAPTURING
                assert runtime._rtc_utterance_id == uid1 + 1

                gate.set()
                await _until(
                    lambda: any(e["type"] == "result" for e in runtime.event_history())
                )
                assert runtime._rtc_finalize_pending == 0
            finally:
                gate.set()
                finalizer.cancel()
                try:
                    await finalizer
                except (asyncio.CancelledError, Exception):
                    pass

        run(body())


# ---------------------------------------------------------------------------
# Partial transcriptions (rolling STT)
# ---------------------------------------------------------------------------

class TestPartialStt:
    def test_partial_revisions_update_latest(self, monkeypatch):
        async def body():
            monkeypatch.setattr(settings, "RTC_PARTIAL_INTERVAL", 0.02)
            monkeypatch.setattr(settings, "RTC_PARTIAL_MIN_FRAMES", 3)
            texts = iter(["hello", "hello world", "hello world clip"])

            transport = FakeTransport()
            runtime = make_runtime(transport, rtc_auto_arm=True)
            runtime.rtc_stt_partial = lambda frames: next(texts)
            await _arm(runtime, transport)
            await runtime.rtc_resume()
            for _ in range(60):
                transport.emit_stream_data(_frame())
            await _until(
                lambda: sum(
                    1
                    for e in runtime.event_history()
                    if e["type"] == "transcript" and not e.get("final")
                )
                >= 3
            )
            partials = [
                e["text"]
                for e in runtime.event_history()
                if e["type"] == "transcript" and not e.get("final")
            ]
            assert partials == ["hello", "hello world", "hello world clip"]
            assert runtime._rtc_partial_text == "hello world clip"

            await runtime.rtc_pause()
            assert runtime._rtc_partial_inflight is False

        run(body())

    def test_stale_partial_result_is_ignored(self, monkeypatch):
        async def body():
            monkeypatch.setattr(settings, "RTC_PARTIAL_INTERVAL", 0.02)
            monkeypatch.setattr(settings, "RTC_PARTIAL_MIN_FRAMES", 3)
            gate = threading.Event()
            calls = []

            def slow_partial(frames):
                calls.append(len(frames))
                gate.wait(timeout=10)
                return "stale text"

            transport = FakeTransport()
            runtime = make_runtime(transport, rtc_auto_arm=True)
            runtime.rtc_stt_partial = slow_partial
            await _arm(runtime, transport)
            await runtime.rtc_resume()
            for _ in range(5):
                transport.emit_stream_data(_frame())
            await _until(lambda: runtime._rtc_partial_inflight)

            # The utterance ends and a new one starts before the result lands.
            await runtime.rtc_pause()
            uid_old = runtime._rtc_utterance_id
            await runtime.rtc_resume()
            uid_new = runtime._rtc_utterance_id
            assert uid_new == uid_old + 1

            gate.set()
            await asyncio.sleep(0.1)

            stale = [
                e
                for e in runtime.event_history()
                if e["type"] == "transcript" and e.get("utterance_id") == uid_old
            ]
            assert stale == []
            assert runtime._rtc_partial_text == ""  # new utterance

        run(body())


# ---------------------------------------------------------------------------
# RTC vs legacy SD pipeline / transport safety
# ---------------------------------------------------------------------------

class TestSafety:
    def test_rtc_never_enters_sd_ingestion(self):
        async def body():
            transport = FakeTransport()
            runtime = make_runtime(transport, rtc_auto_arm=True)
            await _arm(runtime, transport)

            rejected = await runtime._request_ingest(
                "20260821000099", trigger="physical"
            )
            assert rejected["accepted"] is False
            manual = await runtime.manual_ingest("20260821000099")
            assert manual["accepted"] is False
            assert store.get_ingestion(runtime.device_id, "20260821000099") is None

            with pytest.raises(ClipConflictError):
                await runtime.start_recording(mode="enhanced")

            # A heartbeat reporting STREAMING must not create ingestion rows.
            transport.status_state = "STREAMING"
            await runtime._heartbeat_once()
            assert store.list_recent_ingestions(runtime.device_id, limit=100) == []

        run(body())

    def test_ble_loss_aborts_and_reatms_fresh_session(self):
        async def body():
            transport = FakeTransport()
            runtime = make_runtime(transport, rtc_auto_arm=True)
            session1 = await _arm(runtime, transport)
            await runtime.rtc_resume()
            for _ in range(5):
                transport.emit_stream_data(_frame())

            transport.connected = False
            await runtime._rtc_abort(reason="disconnected", finalize=True)

            assert runtime._rtc_phase == RTC_PHASE_DISCONNECTED
            assert runtime._rtc_lease_token is None
            assert runtime._rtc_receiver is None
            assert runtime._rtc_session is None
            # The in-flight utterance was finalized exactly once.
            assert runtime._rtc_finalize_pending == 1
            job = await _next_job(runtime)
            assert len(job["frames"]) == 5

            # Supervisor-style reconnect re-arms a fresh RTC session.
            transport.connected = True
            await runtime._connect()
            await runtime._on_connected()
            assert runtime._rtc_phase == RTC_PHASE_PAUSED
            assert runtime._rtc_session is not None
            assert runtime._rtc_session != session1
            assert transport.commands.count("AT+START=rtc") == 2

        run(body())

    def test_rtc_stop_is_terminal_then_reatms_on_reconnect(self):
        async def body():
            transport = FakeTransport()
            runtime = make_runtime(transport, rtc_auto_arm=True)
            session1 = await _arm(runtime, transport)

            result = await runtime.rtc_stop()
            assert result["accepted"] is True
            assert "AT+STOP" in transport.commands
            assert runtime._rtc_phase == RTC_PHASE_STOPPED
            assert runtime._rtc_lease_token is None
            assert runtime._rtc_receiver is None

            # STOP is terminal on this connection: no re-arm, no resume.
            rearm = await runtime._arm_rtc()
            assert rearm["armed"] is False
            with pytest.raises(ClipConflictError):
                await runtime.rtc_resume()

            # A fresh connection re-arms a brand-new session.
            await runtime._teardown_transport()
            await runtime._connect()
            await runtime._on_connected()
            assert runtime._rtc_phase == RTC_PHASE_PAUSED
            assert runtime._rtc_session is not None
            assert runtime._rtc_session != session1

        run(body())

    def test_shutdown_stops_stream_and_cleans(self):
        async def body():
            transport = FakeTransport()
            runtime = make_runtime(transport, rtc_auto_arm=True)
            await _arm(runtime, transport)
            await runtime.rtc_resume()
            for _ in range(3):
                transport.emit_stream_data(_frame())
            capture_task = runtime._rtc_capture_task

            await runtime.shutdown()

            assert "AT+STOP" in transport.commands
            # STOPPED when the terminal stop completed, DISCONNECTED otherwise.
            assert runtime._rtc_phase in (RTC_PHASE_STOPPED, RTC_PHASE_DISCONNECTED)
            assert runtime._rtc_lease_token is None
            assert runtime._rtc_receiver is None
            assert capture_task is None or capture_task.cancelled() or capture_task.done()

        run(body())

    def test_stale_lease_detach_does_not_clobber_successor(self):
        def body():
            transport = FakeTransport()
            got = []

            def sink_a(frame):
                got.append(("a", frame))

            def sink_b(frame):
                got.append(("b", frame))

            token_a = transport.set_file_frame_handler(sink_a)
            token_b = transport.set_file_frame_handler(sink_b)  # successor
            # Cleaning up the stale owner must not remove the successor.
            assert transport.detach_file_frame_handler(token_a) is False
            transport._emit_file_frame(b"\x01\x02")
            assert [name for name, _ in got] == ["b"]
            # The current owner can detach exactly once.
            assert transport.detach_file_frame_handler(token_b) is True
            assert transport.detach_file_frame_handler(token_b) is False
            assert transport._file_frame_handler is None

        body()

    def test_legacy_download_conflicts_while_rtc_armed(self):
        async def body():
            transport = FakeTransport()
            runtime = make_runtime(transport, rtc_auto_arm=True)
            await _arm(runtime, transport)

            from backend.clip.exceptions import ClipConflictError as Conflict

            with pytest.raises(Conflict):
                await runtime._download_session_once("20260821999999")

        run(body())
