"""/api/clip API tests with an injected fake worker (no BLE, deterministic)."""

import pytest

from app import create_app
from config import settings
from backend.clip.exceptions import (
    ClipCommandFailedError,
    ClipConflictError,
    ClipTransferFailedError,
    ClipUnavailableError,
)


class FakeClipWorker:
    """Duck-type of the ClipWorker synchronous façade used by the routes."""

    def __init__(self):
        self.available = True
        self.status_payload = {
            "connected": True,
            "recording": False,
            "session": None,
            "device_id": "Clip",
            "device_name": "Clip",
            "input_mode": "both",
            "record_mode": "enhanced",
            "last_error": None,
            "battery_percent": 90,
            "state": "IDLE",
            "rtc_phase": "paused",
            "rtc_session": "20269900000001",
            "rtc_utterance_id": 3,
            "rtc_partial_transcript": "hello",
            "rtc_processing": False,
            "rtc_error": None,
        }
        self.events: list[dict] = []
        self.start_calls: list[tuple] = []
        self.stop_calls = 0
        self.ingest_calls: list[tuple] = []
        self.context_registered: list[str] = []
        self.start_error: Exception | None = None
        self.stop_error: Exception | None = None
        self.status_error: Exception | None = None
        self.ingest_error: Exception | None = None
        self.rtc_resume_calls = 0
        self.rtc_pause_calls = 0
        self.rtc_resume_error: Exception | None = None
        self.rtc_pause_error: Exception | None = None

    def get_status(self):
        if self.status_error:
            raise self.status_error
        return dict(self.status_payload)

    def start_recording(self, mode=None, conversation_id=None):
        self.start_calls.append((mode, conversation_id))
        if self.start_error:
            raise self.start_error
        return {"session": "20260821000010", "mode": mode, "conversation_id": conversation_id}

    def stop_recording(self):
        self.stop_calls += 1
        if self.stop_error:
            raise self.stop_error
        return {"accepted": True, "session": "20260821000010"}

    def rtc_resume(self):
        self.rtc_resume_calls += 1
        if self.rtc_resume_error:
            raise self.rtc_resume_error
        return {
            "accepted": True,
            "phase": "capturing",
            "session": "20269900000001",
            "utterance_id": self.status_payload["rtc_utterance_id"] + 1,
        }

    def rtc_pause(self):
        self.rtc_pause_calls += 1
        if self.rtc_pause_error:
            raise self.rtc_pause_error
        return {
            "accepted": True,
            "phase": "paused",
            "session": "20269900000001",
            "utterance_id": self.status_payload["rtc_utterance_id"],
        }

    def ingest(self, session_id, trigger="manual"):
        self.ingest_calls.append((session_id, trigger))
        if self.ingest_error:
            raise self.ingest_error
        return {"accepted": True, "session": session_id, "status": "queued"}

    def register_context(self, conversation_id):
        self.context_registered.append(conversation_id)

    def iter_events(self, after_id=None):
        for event in list(self.events):
            yield event
        while True:
            yield {"type": "ping"}  # keep the SSE stream alive


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "SUPABASE_URL", "")
    monkeypatch.setattr(settings, "SUPABASE_KEY", "")
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite:///{tmp_path}/api.db")
    monkeypatch.setattr(settings, "PINECONE_API_KEY", "")
    fake = FakeClipWorker()
    app = create_app(clip_enabled=True, clip_factory=lambda: fake)
    app.config["TESTING"] = True
    app.config["FAKE_CLIP_WORKER"] = fake
    return app.test_client(), fake


@pytest.fixture
def disabled_api(monkeypatch, tmp_path):
    """App with the Clip runtime disabled: all /api/clip calls are 503."""
    monkeypatch.setattr(settings, "SUPABASE_URL", "")
    monkeypatch.setattr(settings, "SUPABASE_KEY", "")
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite:///{tmp_path}/off.db")
    app = create_app(clip_enabled=False)
    app.config["TESTING"] = True
    return app.test_client()


# -- status ----------------------------------------------------------------

def test_status_ok(api):
    client, fake = api
    r = client.get("/api/clip/status")
    assert r.status_code == 200
    data = r.get_json()
    assert data["connected"] is True
    assert data["available"] is True
    assert data["device_id"] == "Clip"


def test_status_503_when_runtime_disabled(disabled_api):
    r = disabled_api.get("/api/clip/status")
    assert r.status_code == 503
    assert r.get_json()["connected"] is False


def test_status_503_when_reconnecting(api):
    client, fake = api
    fake.status_error = ClipUnavailableError("reconnecting")
    r = client.get("/api/clip/status")
    assert r.status_code == 503


# -- recordings ------------------------------------------------------------

def test_start_ok(api):
    client, fake = api
    r = client.post("/api/clip/recordings/start", json={"mode": "enhanced"})
    assert r.status_code == 200
    assert r.get_json()["session"]
    assert fake.start_calls[0][0] == "enhanced"


def test_start_bad_mode_400(api):
    client, _ = api
    r = client.post("/api/clip/recordings/start", json={"mode": "stereo"})
    assert r.status_code == 400


def test_start_conflict_409(api):
    client, fake = api
    fake.start_error = ClipConflictError("Clip is already recording")
    r = client.post("/api/clip/recordings/start", json={})
    assert r.status_code == 409


def test_start_unavailable_503(api):
    client, fake = api
    fake.start_error = ClipUnavailableError("disconnected")
    r = client.post("/api/clip/recordings/start", json={})
    assert r.status_code == 503


def test_start_command_failure_502(api):
    client, fake = api
    fake.start_error = ClipCommandFailedError("rejected")
    r = client.post("/api/clip/recordings/start", json={})
    assert r.status_code == 502


def test_stop_ok(api):
    client, fake = api
    r = client.post("/api/clip/recordings/stop", json={})
    assert r.status_code == 202
    assert r.get_json()["accepted"] is True
    assert fake.stop_calls == 1


def test_stop_conflict_409(api):
    client, fake = api
    fake.stop_error = ClipConflictError("Clip is not recording")
    r = client.post("/api/clip/recordings/stop", json={})
    assert r.status_code == 409


# -- ingest / context --------------------------------------------------------

def test_ingest_ok(api):
    client, fake = api
    r = client.post("/api/clip/sessions/20260821000020/ingest", json={})
    assert r.status_code == 200
    assert fake.ingest_calls == [("20260821000020", "manual")]


def test_ingest_invalid_session_400(api):
    client, _ = api
    r = client.post("/api/clip/sessions/../ingest", json={})
    assert r.status_code == 400


def test_ingest_transfer_failure_502(api):
    client, fake = api
    fake.ingest_error = ClipTransferFailedError("transfer timed out")
    r = client.post("/api/clip/sessions/20260821000021/ingest", json={})
    assert r.status_code == 502


def test_context_registers(api):
    client, fake = api
    r = client.post("/api/clip/context", json={"conversation_id": "conv-abc"})
    assert r.status_code == 200
    assert fake.context_registered == ["conv-abc"]


def test_context_missing_id_400(api):
    client, _ = api
    r = client.post("/api/clip/context", json={})
    assert r.status_code == 400
    r2 = client.post("/api/clip/context", json={"conversation_id": 5})
    assert r2.status_code == 400


def test_ingest_registers_requested_conversation(api):
    client, fake = api
    r = client.post(
        "/api/clip/sessions/20260821000022/ingest",
        json={"conversation_id": "conv-for-session", "trigger": "retry"},
    )
    assert r.status_code == 200
    assert "conv-for-session" in fake.context_registered
    assert fake.ingest_calls == [("20260821000022", "retry")]


# -- RTC stream control ------------------------------------------------------

def test_stream_resume_ok(api):
    client, fake = api
    r = client.post("/api/clip/stream/resume", json={})
    assert r.status_code == 200
    data = r.get_json()
    assert data["accepted"] is True
    assert data["phase"] == "capturing"
    assert fake.rtc_resume_calls == 1


def test_stream_pause_ok(api):
    client, fake = api
    r = client.post("/api/clip/stream/pause", json={})
    assert r.status_code == 202
    data = r.get_json()
    assert data["accepted"] is True
    assert data["phase"] == "paused"
    assert fake.rtc_pause_calls == 1


def test_stream_resume_conflict_409(api):
    client, fake = api
    fake.rtc_resume_error = ClipConflictError("RTC live stream is not armed")
    r = client.post("/api/clip/stream/resume", json={})
    assert r.status_code == 409


def test_stream_pause_conflict_409(api):
    client, fake = api
    fake.rtc_pause_error = ClipConflictError("RTC live stream is not armed")
    r = client.post("/api/clip/stream/pause", json={})
    assert r.status_code == 409


def test_stream_resume_unavailable_503(api):
    client, fake = api
    fake.rtc_resume_error = ClipUnavailableError("reconnecting")
    r = client.post("/api/clip/stream/resume", json={})
    assert r.status_code == 503


def test_stream_resume_command_failed_502(api):
    client, fake = api
    fake.rtc_resume_error = ClipCommandFailedError("AT+RESUME rejected")
    r = client.post("/api/clip/stream/resume", json={})
    assert r.status_code == 502


def test_stream_endpoints_disabled_503(disabled_api):
    client = disabled_api
    assert client.post("/api/clip/stream/resume", json={}).status_code == 503
    assert client.post("/api/clip/stream/pause", json={}).status_code == 503


def test_status_includes_rtc_fields(api):
    client, _ = api
    r = client.get("/api/clip/status")
    assert r.status_code == 200
    data = r.get_json()
    assert data["rtc_phase"] == "paused"
    assert data["rtc_session"] == "20269900000001"
    assert data["rtc_utterance_id"] == 3
    assert data["rtc_partial_transcript"] == "hello"
    assert data["rtc_processing"] is False


# -- SSE ---------------------------------------------------------------------

def test_events_stream_replays_history(api):
    client, fake = api
    fake.events = [
        {"type": "connection", "connected": True, "error": None},
        {"type": "recording", "action": "started", "session": "S", "trigger": "web"},
        {"type": "result", "session": "S", "conversation_id": "c1", "transcript": "hi", "response": "hello"},
    ]
    resp = client.get("/api/clip/events", buffered=False)
    stream = resp.response
    text = ""
    try:
        for _ in range(100):
            chunk = next(stream)
            text += chunk.decode("utf-8", "replace")
            if "event: result" in text:
                break
        assert "event: connection" in text
        assert "event: recording" in text
        assert "event: result" in text
        assert "hello" in text
        assert 'event: {"type":' not in text  # SSE data is JSON, event names bare
    finally:
        resp.close()


def test_events_stream_rtc_events(api):
    client, fake = api
    fake.events = [
        {"type": "connection", "connected": True, "error": None},
        {"type": "rtc_state", "phase": "capturing", "session": "20269900000001", "utterance_id": 1},
        {"type": "transcript", "utterance_id": 1, "text": "hello clip", "final": False},
        {"type": "transcript", "utterance_id": 1, "text": "hello clip", "final": True},
    ]
    resp = client.get("/api/clip/events", buffered=False)
    stream = resp.response
    text = ""
    try:
        for _ in range(100):
            chunk = next(stream)
            text += chunk.decode("utf-8", "replace")
            if "event: transcript" in text and '"final": true' in text:
                break
        assert "event: rtc_state" in text
        assert "event: transcript" in text
        assert '"phase": "capturing"' in text
        assert '"utterance_id": 1' in text
    finally:
        resp.close()


# -- frontend input modes -----------------------------------------------------

def test_index_embeds_clip_config_both(api):
    client, _ = api
    r = client.get("/")
    assert r.status_code == 200
    assert '"input_mode": "both"' in r.get_data(as_text=True)
    assert '"clip_enabled": true' in r.get_data(as_text=True)


def test_index_embeds_clip_config_browser(disabled_api):
    r = disabled_api.get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # default settings mode is "both" but the worker was disabled on purpose
    assert "clip-config" in body


def test_index_clip_only_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "VOICE_INPUT_MODE", "clip")
    monkeypatch.setattr(settings, "SUPABASE_URL", "")
    monkeypatch.setattr(settings, "SUPABASE_KEY", "")
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite:///{tmp_path}/cliponly.db")
    fake = FakeClipWorker()
    app = create_app(clip_enabled=True, clip_factory=lambda: fake)
    r = app.test_client().get("/")
    body = r.get_data(as_text=True)
    assert '"input_mode": "clip"' in body
