"""clip_ingestions persistence tests (SQLite backend)."""

import pytest

from config import settings
from backend.clip import store


@pytest.fixture(autouse=True)
def sqlite_backend(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "SUPABASE_URL", "")
    monkeypatch.setattr(settings, "SUPABASE_KEY", "")
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite:///{tmp_path}/clip.db")
    store.init_clip_ingestions()


def test_create_and_get(tmp_path):
    store.upsert_ingestion(
        "Clip", "20260101000000", status="stopped", trigger="physical"
    )
    row = store.get_ingestion("Clip", "20260101000000")
    assert row["status"] == "stopped"
    assert row["trigger"] == "physical"
    assert row["device_id"] == "Clip"


def test_update_keeps_row(tmp_path):
    store.upsert_ingestion("Clip", "S1", status="stopped")
    store.mark_status("Clip", "S1", "downloading")
    store.mark_failed("Clip", "S1", "transfer timeout")
    row = store.get_ingestion("Clip", "S1")
    assert row["status"] == "failed"
    assert "timeout" in row["error"]


def test_mark_completed(tmp_path):
    store.upsert_ingestion("Clip", "S1", status="stopped")
    store.mark_completed(
        "Clip", "S1", transcript="hi there", response="hello!", conversation_id="c1"
    )
    row = store.get_ingestion("Clip", "S1")
    assert row["status"] == "completed"
    assert row["transcript"] == "hi there"
    assert row["conversation_id"] == "c1"


def test_baseline_marks_ignored(tmp_path):
    store.upsert_ingestion("Clip", "S0", status="stopped")  # already tracked
    count = store.mark_ignored_existing("Clip", ["S0", "S1", "S2", "S3"])
    assert count == 3  # S0 already existed
    assert store.get_ingestion("Clip", "S1")["status"] == "ignored_existing"
    assert store.get_ingestion("Clip", "S0")["status"] == "stopped"  # untouched


def test_baseline_marker_persists_even_when_device_has_no_sessions():
    assert store.is_baseline_complete("Clip") is False
    store.mark_baseline_complete("Clip")
    assert store.is_baseline_complete("Clip") is True
    # Reinitializing the store simulates an application restart.
    store.init_clip_ingestions()
    assert store.is_baseline_complete("Clip") is True


def test_keyed_by_device(tmp_path):
    store.upsert_ingestion("ClipA", "S1", status="stopped")
    store.upsert_ingestion("ClipB", "S1", status="completed")
    assert store.get_ingestion("ClipA", "S1")["status"] == "stopped"
    assert store.get_ingestion("ClipB", "S1")["status"] == "completed"


def test_invalid_status_rejected(tmp_path):
    with pytest.raises(ValueError):
        store.upsert_ingestion("Clip", "S1", status="bogus")


def test_list_recent_orders_by_update(tmp_path):
    for i in range(3):
        store.upsert_ingestion("Clip", f"S{i}", status="stopped")
    store.mark_status("Clip", "S2", "downloading")
    rows = store.list_recent_ingestions("Clip", limit=10)
    assert [r["session_id"] for r in rows][0] == "S2"
    assert len(rows) == 3


def test_missing_supabase_clip_tables_fall_back_to_sqlite(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setattr(settings, "SUPABASE_KEY", "configured")
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite:///{tmp_path}/fallback.db")

    def missing_tables():
        raise RuntimeError("PGRST205: clip_device_state is missing")

    monkeypatch.setattr(store, "_supabase_init", missing_tables)
    store.init_clip_ingestions()

    # Supabase remains configured, but every subsequent Clip operation must
    # consistently use the initialized SQLite fallback.
    assert store._use_supabase() is False
    assert store.is_baseline_complete("Clip") is False
    store.mark_baseline_complete("Clip")
    store.upsert_ingestion("Clip", "S1", status="stopped")
    store.mark_status("Clip", "S1", "processing")
    assert store.is_baseline_complete("Clip") is True
    assert store.get_ingestion("Clip", "S1")["status"] == "processing"
    assert store.list_recent_ingestions("Clip", 10)[0]["session_id"] == "S1"
