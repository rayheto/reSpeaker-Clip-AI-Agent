"""Persistence for Clip ingestion workflow state.

Mirrors the project's ``backend/database/chat.py`` pattern: SQLite is the
embedded default/tests backend and Supabase is used when configured.  Rows are
keyed by ``(device_id, session_id)`` so a repeated event, an HTTP retry, or a
reconnect discovery scan can never duplicate processing.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Literal

from config import settings
from backend.database import supabase_client

logger = logging.getLogger(__name__)

# Selected once by ``init_clip_ingestions`` so every Clip operation in a
# process uses the same backend.  In particular, having general Supabase
# credentials does not imply that the optional Clip tables were installed.
_backend: Literal["supabase", "sqlite"] | None = None

# Suggested lifecycle: recording -> stopped -> downloading -> processing ->
# completed | failed.  ignored_existing marks the first-start integration
# baseline so old device history is never ingested unexpectedly.
STATUSES = (
    "recording",
    "stopped",
    "downloading",
    "processing",
    "completed",
    "failed",
    "ignored_existing",
)

TABLE_COLUMNS = (
    "device_id",
    "session_id",
    "trigger",
    "source",
    "conversation_id",
    "status",
    "transcript",
    "response",
    "error",
    "metadata",
    "created_at",
    "updated_at",
)


def _use_supabase() -> bool:
    if _backend is not None:
        return _backend == "supabase"
    return bool(settings.SUPABASE_URL and settings.SUPABASE_KEY)


# ---------------------------------------------------------------------------
# SQLite backend
# ---------------------------------------------------------------------------

def _db_path() -> str:
    return settings.DATABASE_URL.replace("sqlite:///", "")


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(_db_path())


def _sqlite_init() -> None:
    conn = _connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS clip_ingestions (
            device_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            trigger TEXT,
            source TEXT,
            conversation_id TEXT,
            status TEXT NOT NULL DEFAULT 'stopped',
            transcript TEXT,
            response TEXT,
            error TEXT,
            metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (device_id, session_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS clip_device_state (
            device_id TEXT PRIMARY KEY,
            baseline_completed INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


def _sqlite_upsert(device_id: str, session_id: str, fields: dict[str, Any]) -> None:
    conn = _connect()
    keys = list(fields.keys())
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    if keys:
        insert_cols = ", ".join(["device_id", "session_id", *keys])
        placeholders = ", ".join(["?", "?", *(["?"] * len(keys))])
        update_cols = ", ".join(f"{k} = excluded.{k}" for k in keys)
        conn.execute(
            f"""
            INSERT INTO clip_ingestions ({insert_cols})
            VALUES ({placeholders})
            ON CONFLICT(device_id, session_id) DO UPDATE SET {update_cols}
            """,
            (device_id, session_id, *list(fields.values())),
        )
        conn.execute(
            "UPDATE clip_ingestions SET updated_at = ? "
            "WHERE device_id = ? AND session_id = ?",
            (now, device_id, session_id),
        )
    else:
        conn.execute(
            "INSERT OR IGNORE INTO clip_ingestions (device_id, session_id) VALUES (?, ?)",
            (device_id, session_id),
        )
    conn.commit()
    conn.close()


def _sqlite_get(device_id: str, session_id: str) -> dict[str, Any] | None:
    conn = _connect()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM clip_ingestions WHERE device_id = ? AND session_id = ?",
        (device_id, session_id),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _sqlite_mark_ignored(device_id: str, session_ids: Iterable[str]) -> int:
    conn = _connect()
    count = 0
    for sid in session_ids:
        if not sid:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO clip_ingestions "
            "(device_id, session_id, status, trigger, source) "
            "VALUES (?, ?, 'ignored_existing', 'baseline', 'device')",
            (device_id, sid),
        )
        count += cur.rowcount
    conn.commit()
    conn.close()
    return count


def _sqlite_list_recent(device_id: str | None, limit: int) -> list[dict[str, Any]]:
    conn = _connect()
    conn.row_factory = sqlite3.Row
    if device_id:
        rows = conn.execute(
            "SELECT * FROM clip_ingestions WHERE device_id = ? "
            "ORDER BY updated_at DESC, created_at DESC LIMIT ?",
            (device_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM clip_ingestions ORDER BY updated_at DESC, created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _sqlite_is_baseline_complete(device_id: str) -> bool:
    conn = _connect()
    row = conn.execute(
        "SELECT baseline_completed FROM clip_device_state WHERE device_id = ?",
        (device_id,),
    ).fetchone()
    conn.close()
    return bool(row and row[0])


def _sqlite_mark_baseline_complete(device_id: str) -> None:
    conn = _connect()
    conn.execute(
        """
        INSERT INTO clip_device_state (device_id, baseline_completed, updated_at)
        VALUES (?, 1, CURRENT_TIMESTAMP)
        ON CONFLICT(device_id) DO UPDATE SET
            baseline_completed = 1,
            updated_at = CURRENT_TIMESTAMP
        """,
        (device_id,),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Supabase backend
# ---------------------------------------------------------------------------

def _supabase_init() -> None:
    supabase_client.get_client().from_("clip_ingestions").select(
        "device_id, session_id"
    ).limit(1).execute()
    supabase_client.get_client().from_("clip_device_state").select(
        "device_id, baseline_completed"
    ).limit(1).execute()


def _supabase_upsert(device_id: str, session_id: str, fields: dict[str, Any]) -> None:
    row = {
        "device_id": device_id,
        "session_id": session_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **fields,
    }
    supabase_client.get_client().table("clip_ingestions").upsert(
        row, on_conflict="device_id,session_id"
    ).execute()


def _supabase_get(device_id: str, session_id: str) -> dict[str, Any] | None:
    res = (
        supabase_client.get_client()
        .table("clip_ingestions")
        .select("*")
        .eq("device_id", device_id)
        .eq("session_id", session_id)
        .execute()
    )
    return res.data[0] if res.data else None


def _supabase_mark_ignored(device_id: str, session_ids: Iterable[str]) -> int:
    count = 0
    for sid in session_ids:
        if not sid:
            continue
        if _supabase_get(device_id, sid) is not None:
            continue
        res = (
            supabase_client.get_client()
            .table("clip_ingestions")
            .upsert(
                {
                    "device_id": device_id,
                    "session_id": sid,
                    "status": "ignored_existing",
                    "trigger": "baseline",
                    "source": "device",
                },
                on_conflict="device_id,session_id",
            )
            .execute()
        )
        if res.data:
            count += 1
    return count


def _supabase_is_baseline_complete(device_id: str) -> bool:
    res = (
        supabase_client.get_client()
        .table("clip_device_state")
        .select("baseline_completed")
        .eq("device_id", device_id)
        .execute()
    )
    return bool(res.data and res.data[0].get("baseline_completed"))


def _supabase_mark_baseline_complete(device_id: str) -> None:
    supabase_client.get_client().table("clip_device_state").upsert(
        {
            "device_id": device_id,
            "baseline_completed": True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="device_id",
    ).execute()


def _supabase_list_recent(device_id: str | None, limit: int) -> list[dict[str, Any]]:
    query = supabase_client.get_client().table("clip_ingestions").select("*")
    if device_id:
        query = query.eq("device_id", device_id)
    res = query.order("updated_at", desc=True).limit(limit).execute()
    return res.data


# ---------------------------------------------------------------------------
# Facade
# ---------------------------------------------------------------------------

def init_clip_ingestions() -> None:
    global _backend

    if settings.SUPABASE_URL and settings.SUPABASE_KEY:
        try:
            _supabase_init()
        except Exception as exc:
            _sqlite_init()
            _backend = "sqlite"
            logger.warning(
                "Supabase Clip tables unavailable; using SQLite for all Clip "
                "state (run supabase_schema.sql to enable Supabase): %s",
                exc,
            )
        else:
            _backend = "supabase"
    else:
        _sqlite_init()
        _backend = "sqlite"


def upsert_ingestion(
    device_id: str,
    session_id: str,
    **fields: Any,
) -> None:
    allowed = {
        "trigger",
        "source",
        "conversation_id",
        "status",
        "transcript",
        "response",
        "error",
        "metadata",
    }
    cleaned = {k: v for k, v in fields.items() if k in allowed}
    if "status" in cleaned and cleaned["status"] not in STATUSES:
        raise ValueError(f"invalid clip ingestion status: {cleaned['status']}")
    if _use_supabase():
        _supabase_upsert(device_id, session_id, cleaned)
    else:
        _sqlite_upsert(device_id, session_id, cleaned)


def get_ingestion(device_id: str, session_id: str) -> dict[str, Any] | None:
    if _use_supabase():
        return _supabase_get(device_id, session_id)
    return _sqlite_get(device_id, session_id)


def mark_ignored_existing(device_id: str, session_ids: Iterable[str]) -> int:
    if _use_supabase():
        return _supabase_mark_ignored(device_id, session_ids)
    return _sqlite_mark_ignored(device_id, session_ids)


def is_baseline_complete(device_id: str) -> bool:
    if _use_supabase():
        return _supabase_is_baseline_complete(device_id)
    return _sqlite_is_baseline_complete(device_id)


def mark_baseline_complete(device_id: str) -> None:
    if _use_supabase():
        _supabase_mark_baseline_complete(device_id)
    else:
        _sqlite_mark_baseline_complete(device_id)


def mark_status(
    device_id: str,
    session_id: str,
    status: str,
    **fields: Any,
) -> None:
    upsert_ingestion(device_id, session_id, status=status, **fields)


def mark_failed(device_id: str, session_id: str, error: str, **fields: Any) -> None:
    upsert_ingestion(
        device_id, session_id, status="failed", error=str(error)[:2000], **fields
    )


def mark_completed(
    device_id: str,
    session_id: str,
    *,
    transcript: str,
    response: str,
    conversation_id: str | None,
    **fields: Any,
) -> None:
    upsert_ingestion(
        device_id,
        session_id,
        status="completed",
        transcript=transcript,
        response=response,
        conversation_id=conversation_id,
        error=None,
        **fields,
    )


def list_recent_ingestions(
    device_id: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    if _use_supabase():
        return _supabase_list_recent(device_id, limit)
    return _sqlite_list_recent(device_id, limit)
