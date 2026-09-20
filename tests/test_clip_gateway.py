"""Device-gateway mode: the Clip service with the agent switched off.

Contract under test (``AGENT_ENABLED=false`` / ``--no-agent``):
  * the agent stack is never imported (no LangGraph, Groq, Mem0, Pinecone,
    conversation store) and no API key is required;
  * only the health and Clip blueprints exist — no chat/voice/tts/composio;
  * a finalized utterance is re-containerized to Ogg, kept on disk and
    announced with an ``utterance_audio`` event instead of being transcribed;
  * a downloaded SD session behaves the same way and its audio is retained
    (the agent path deletes it after transcribing).
"""

import asyncio
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from config import settings
from backend.clip import store
from backend.clip.audio_paths import session_audio_path, session_dir, utterance_audio_path
from backend.clip.runtime import ClipRuntime, IngestRequest
from tests.test_clip_ogg import packet
from tests.test_clip_runtime import FakeTransport

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION = "20260920101234"
OPUS_FRAME = b"\xf8\x01\x02\x03\x04"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def clip_db(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "SUPABASE_URL", "")
    monkeypatch.setattr(settings, "SUPABASE_KEY", "")
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite:///{tmp_path}/gateway.db")
    monkeypatch.setattr(settings, "CLIP_TEMP_DIR", str(tmp_path / "clip_audio"))
    store.init_clip_ingestions()


class FakeWorker:
    """Stand-in for ClipWorker so app tests never touch BLE."""

    def get_status(self):
        return {"connected": False, "agent_enabled": False}

    def iter_events(self, after_id=None):
        return iter(())

    def register_context(self, conversation_id):
        return None


def make_gateway_runtime(transport=None, **kwargs):
    return ClipRuntime(
        transport=transport or FakeTransport(),
        device_id="Clip",
        rtc_auto_arm=False,
        agent_enabled=False,
        **kwargs,
    )


def make_gateway_app():
    from app import create_app

    return create_app(clip_enabled=True, agent_enabled=False, clip_factory=FakeWorker)


def frames(count: int) -> list[bytes]:
    return [OPUS_FRAME] * count


def write_downloaded_session(session_id: str = SESSION) -> Path:
    """Lay out what a completed BLE download leaves behind: metadata + packets."""
    raw_dir = session_dir(session_id)
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "session.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "sample_rate_hz": 16000,
                "channels": 1,
                "mode": "enhanced",
            }
        ),
        encoding="utf-8",
    )
    for index in range(3):
        (raw_dir / f"{index:04d}.opus").write_bytes(b"".join(packet() for _ in range(2)))
    return raw_dir


# ---------------------------------------------------------------------------
# Import isolation
# ---------------------------------------------------------------------------

AGENT_MODULE_PREFIXES = (
    "backend.services",
    "backend.graph",
    "backend.llm",
    "backend.vector",
    "langgraph",
    "langchain",
    "langchain_groq",
    "groq",
    "mem0",
    "composio",
    "pinecone",
)
# Note: the Clip store keeps its own state through
# backend.database.supabase_client, so importing that module (and, as a side
# effect of the package __init__, backend.database.chat) is expected in gateway
# mode. Neither calls init_db() nor touches Pinecone.


def test_gateway_startup_imports_no_agent_modules():
    """Run in a fresh interpreter: import state is otherwise order-dependent."""
    script = textwrap.dedent(
        """
        import sys
        from app import create_app

        class Worker:
            def get_status(self):
                return {"connected": False}

            def iter_events(self, after_id=None):
                return iter(())

            def register_context(self, conversation_id):
                return None

        create_app(clip_enabled=True, agent_enabled=False, clip_factory=Worker)
        banned = %r
        loaded = sorted(m for m in sys.modules if m.startswith(banned))
        print("LOADED=" + ",".join(loaded))
        """
    ) % (AGENT_MODULE_PREFIXES,)

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    line = next(
        (ln for ln in result.stdout.splitlines() if ln.startswith("LOADED=")), None
    )
    assert line is not None, result.stdout
    assert line == "LOADED=", f"agent stack was imported in gateway mode: {line[7:]}"


def test_agent_mode_still_loads_the_agent_stack():
    """The inverse guard: disabling the agent must not disable it by accident."""
    script = textwrap.dedent(
        """
        import sys
        from app import create_app

        class Worker:
            def get_status(self):
                return {"connected": False}

            def iter_events(self, after_id=None):
                return iter(())

            def register_context(self, conversation_id):
                return None

        create_app(clip_enabled=True, agent_enabled=True, clip_factory=Worker)
        print("LOADED=" + ",".join(m for m in sys.modules if m.startswith("backend.graph")))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "LOADED=backend.graph" in result.stdout


def test_gateway_runtime_does_not_build_an_audio_service():
    runtime = make_gateway_runtime()
    assert runtime.agent_enabled is False
    assert runtime.audio_service is None

    agent_runtime = ClipRuntime(transport=FakeTransport(), device_id="Clip", rtc_auto_arm=False)
    assert agent_runtime.agent_enabled is True
    assert agent_runtime.audio_service is not None


def test_gateway_status_reports_the_mode():
    runtime = make_gateway_runtime()
    assert run(runtime.status_payload())["agent_enabled"] is False


# ---------------------------------------------------------------------------
# RTC utterances become audio, not transcripts
# ---------------------------------------------------------------------------

def test_utterance_is_written_and_announced_as_audio():
    async def body():
        runtime = make_gateway_runtime()
        await runtime._rtc_finalize_job(
            {
                "utterance_id": 7,
                "session": SESSION,
                "frames": frames(settings.RTC_MIN_UTTERANCE_FRAMES),
                "reason": "device",
            }
        )

        events = runtime.event_history()
        audio = [event for event in events if event["type"] == "utterance_audio"]
        assert len(audio) == 1
        assert audio[0]["utterance_id"] == 7
        assert audio[0]["url"] == f"/api/clip/utterances/{SESSION}/7/audio"
        assert audio[0]["bytes"] > 0
        assert audio[0]["content_type"] == "audio/ogg"
        assert audio[0]["trigger"] == "device"

        path = utterance_audio_path(SESSION, 7)
        assert path.is_file()
        assert path.read_bytes().startswith(b"OggS")

        # No STT, no agent: none of the agent-path events may appear.
        agentish = {"transcript", "thinking", "token", "result"}
        assert not [event for event in events if event["type"] in agentish]

    run(body())


def test_short_utterance_is_skipped_without_stt_or_audio():
    async def body():
        runtime = make_gateway_runtime()
        await runtime._rtc_finalize_job(
            {
                "utterance_id": 8,
                "session": SESSION,
                "frames": frames(settings.RTC_MIN_UTTERANCE_FRAMES - 1),
                "reason": "web",
            }
        )

        audio = [e for e in runtime.event_history() if e["type"] == "utterance_audio"]
        assert len(audio) == 1
        assert audio[0]["skipped"] == "too short"
        assert "url" not in audio[0]
        assert not utterance_audio_path(SESSION, 8).exists()

    run(body())


def test_utterance_audio_is_served_over_http():
    path = utterance_audio_path(SESSION, 3)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"OggS-fake-utterance")

    client = make_gateway_app().test_client()

    response = client.get(f"/api/clip/utterances/{SESSION}/3/audio")
    assert response.status_code == 200
    assert response.mimetype == "audio/ogg"
    assert response.data == b"OggS-fake-utterance"

    assert client.get(f"/api/clip/utterances/{SESSION}/4/audio").status_code == 404
    assert client.get("/api/clip/utterances/not-a-session/3/audio").status_code == 400


# ---------------------------------------------------------------------------
# SD sessions are exchanged, not transcribed
# ---------------------------------------------------------------------------

def test_session_audio_is_retained_and_announced():
    async def body():
        runtime = make_gateway_runtime()
        raw_dir = write_downloaded_session()

        await runtime._exchange_session(
            IngestRequest(session_id=SESSION, trigger="physical"),
            SimpleNamespace(output_dir=str(raw_dir)),
        )

        ogg = session_audio_path(SESSION)
        assert ogg.is_file()
        assert ogg.read_bytes().startswith(b"OggS")
        # The audio is the deliverable: unlike the agent path it is not deleted.
        assert raw_dir.is_dir()

        events = runtime.event_history()
        audio = [event for event in events if event["type"] == "session_audio"]
        assert len(audio) == 1
        assert audio[0]["url"] == f"/api/clip/sessions/{SESSION}/audio"
        assert audio[0]["bytes"] == ogg.stat().st_size
        assert audio[0]["trigger"] == "physical"

        completed = [
            event
            for event in events
            if event["type"] == "workflow" and event.get("status") == "completed"
        ]
        assert completed and completed[0]["mode"] == "exchange"

        row = store.get_ingestion("Clip", SESSION)
        assert row["status"] == "completed"
        assert row["transcript"] == ""

    run(body())


def test_session_audio_is_served_over_http():
    path = session_audio_path(SESSION)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"OggS-fake-session")

    client = make_gateway_app().test_client()

    response = client.get(f"/api/clip/sessions/{SESSION}/audio")
    assert response.status_code == 200
    assert response.mimetype == "audio/ogg"
    assert response.data == b"OggS-fake-session"

    assert client.get("/api/clip/sessions/20260920999999/audio").status_code == 404
    assert client.get("/api/clip/sessions/nope/audio").status_code == 400


# ---------------------------------------------------------------------------
# App surface
# ---------------------------------------------------------------------------

def test_gateway_app_exposes_only_health_and_clip_routes():
    app = make_gateway_app()
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert "/api/clip/status" in rules
    assert "/api/health" in rules
    assert not [rule for rule in rules if rule.startswith("/api/chat")]
    assert not [rule for rule in rules if rule.startswith("/api/voice")]
    assert not [rule for rule in rules if rule.startswith("/api/tts")]
    assert not [rule for rule in rules if rule.startswith("/api/composio")]

    client = app.test_client()
    assert client.get("/api/clip/status").status_code == 200
    assert client.get("/api/chat").status_code == 404


def test_gateway_root_points_at_the_api_instead_of_the_chat_ui():
    client = make_gateway_app().test_client()
    response = client.get("/")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["web_ui"] is False
    assert "device gateway" in payload["reason"]
    assert "/api/clip/utterances/<session_id>/<utterance_id>/audio" in payload["endpoints"]