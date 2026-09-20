"""On-disk locations of Clip audio the HTTP API serves.

The runtime writes here and ``backend/routes/clip.py`` reads here, so both sides
must agree on the layout. Session ids are validated separately (14 digits), and
the helpers below stay inside ``CLIP_TEMP_DIR`` by construction — a route never
joins a caller-supplied path.

Layout::

    <CLIP_TEMP_DIR>/<session_id>.ogg                  re-containerized SD session
    <CLIP_TEMP_DIR>/<session_id>/...                  raw downloaded packets
    <CLIP_TEMP_DIR>/rtc/<session_id>/<utterance>.ogg   RTC warm-pause utterance
"""

from __future__ import annotations

import re
from pathlib import Path

from config import settings

SESSION_RE = re.compile(r"^\d{14}$")


def temp_dir() -> Path:
    return Path(settings.CLIP_TEMP_DIR)


def session_audio_path(session_id: str) -> Path:
    """Ogg produced by ``convert_session_to_ogg`` for a downloaded session."""
    return temp_dir() / f"{session_id}.ogg"


def session_dir(session_id: str) -> Path:
    """Directory holding the raw ``NNNN.opus`` packets of a session."""
    return temp_dir() / session_id


def utterance_dir(session_id: str) -> Path:
    return temp_dir() / "rtc" / session_id


def utterance_audio_path(session_id: str, utterance_id: int) -> Path:
    """Ogg snapshot of one RTC utterance (exchange mode)."""
    return utterance_dir(session_id) / f"{utterance_id}.ogg"


def session_audio_url(session_id: str) -> str:
    return f"/api/clip/sessions/{session_id}/audio"


def utterance_audio_url(session_id: str, utterance_id: int) -> str:
    return f"/api/clip/utterances/{session_id}/{utterance_id}/audio"