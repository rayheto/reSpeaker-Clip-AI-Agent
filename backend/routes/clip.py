"""/api/clip endpoints for the reSpeaker Clip integration.

Error mapping (from the approved plan):
400 bad input, 409 state conflict, 502 command/transfer failure,
503 unavailable / reconnecting.  The Clip runtime is injected through
``app.extensions["clip_worker"]`` so tests can substitute a fake worker and
keep everything deterministic (no real BLE in tests).
"""

from __future__ import annotations

import json
import re

from flask import Blueprint, Response, current_app, jsonify, request

from backend.clip.exceptions import (
    ClipCommandFailedError,
    ClipConflictError,
    ClipInputError,
    ClipTransferFailedError,
    ClipUnavailableError,
)

clip_bp = Blueprint("clip", __name__)

_SESSION_RE = re.compile(r"^\d{14}$")


def _worker():
    return current_app.extensions.get("clip_worker")


def _require_worker():
    worker = _worker()
    if worker is None:
        raise ClipUnavailableError("Clip runtime is not enabled on this server")
    return worker


def _validate_session_id(session_id: str) -> str:
    if not isinstance(session_id, str) or not _SESSION_RE.match(session_id):
        raise ClipInputError("invalid session_id")
    return session_id


# -- error mapping -----------------------------------------------------------

@clip_bp.errorhandler(ClipInputError)
def _bad_input(exc: ClipInputError):
    return jsonify({"error": str(exc)}), 400


@clip_bp.errorhandler(ClipConflictError)
def _conflict(exc: ClipConflictError):
    return jsonify({"error": str(exc)}), 409


@clip_bp.errorhandler(ClipCommandFailedError)
def _command_failed(exc: ClipCommandFailedError):
    return jsonify({"error": str(exc)}), 502


@clip_bp.errorhandler(ClipTransferFailedError)
def _transfer_failed(exc: ClipTransferFailedError):
    return jsonify({"error": str(exc)}), 502


@clip_bp.errorhandler(ClipUnavailableError)
def _unavailable(exc: ClipUnavailableError):
    return jsonify({"error": str(exc), "connected": False}), 503


# -- endpoints ---------------------------------------------------------------

@clip_bp.route("/clip/status", methods=["GET"])
def clip_status():
    worker = _require_worker()
    payload = worker.get_status()
    payload.setdefault("available", True)
    return jsonify(payload)


@clip_bp.route("/clip/events", methods=["GET"])
def clip_events():
    worker = _require_worker()
    last_event_id = request.headers.get("Last-Event-ID")
    try:
        after_id = int(last_event_id) if last_event_id is not None else None
    except ValueError:
        after_id = None

    def generate():
        try:
            for event in worker.iter_events(after_id=after_id):
                etype = event.get("type") if isinstance(event, dict) else None
                if etype == "ping" or not etype:
                    yield ": ping\n\n"
                    continue
                payload = dict(event)
                event_id = payload.pop("_event_id", None)
                id_line = f"id: {event_id}\n" if event_id is not None else ""
                yield (
                    f"{id_line}event: {etype}\n"
                    f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                )
        except GeneratorExit:
            pass

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@clip_bp.route("/clip/recordings/start", methods=["POST"])
def clip_start():
    worker = _require_worker()
    data = request.get_json(silent=True) or {}
    mode = data.get("mode")
    if mode is not None and mode not in ("normal", "enhanced"):
        raise ClipInputError("mode must be 'normal' or 'enhanced'")
    conversation_id = data.get("conversation_id")
    if conversation_id is not None and not isinstance(conversation_id, str):
        raise ClipInputError("conversation_id must be a string")
    result = worker.start_recording(mode=mode, conversation_id=conversation_id)
    return jsonify(result), 200


@clip_bp.route("/clip/recordings/stop", methods=["POST"])
def clip_stop():
    worker = _require_worker()
    result = worker.stop_recording()
    return jsonify(result), 202


@clip_bp.route("/clip/stream/resume", methods=["POST"])
def clip_stream_resume():
    """Resume the armed RTC session (start the next logical utterance)."""
    worker = _require_worker()
    result = worker.rtc_resume()
    return jsonify(result), 200


@clip_bp.route("/clip/stream/pause", methods=["POST"])
def clip_stream_pause():
    """Warm-pause the RTC session (finalize the current utterance)."""
    worker = _require_worker()
    result = worker.rtc_pause()
    return jsonify(result), 202


@clip_bp.route("/clip/sessions/<session_id>/ingest", methods=["POST"])
def clip_ingest(session_id: str):
    worker = _require_worker()
    _validate_session_id(session_id)
    data = request.get_json(silent=True) or {}
    trigger = data.get("trigger", "manual")
    if not isinstance(trigger, str) or len(trigger) > 64:
        raise ClipInputError("trigger must be a short string")
    conversation_id = data.get("conversation_id")
    if conversation_id is not None and not isinstance(conversation_id, str):
        raise ClipInputError("conversation_id must be a string")

    # Register the requested conversation so the session is attached to it.
    if conversation_id:
        worker.register_context(conversation_id)
    result = worker.ingest(session_id, trigger=trigger)
    return jsonify(result), 200


@clip_bp.route("/clip/context", methods=["POST"])
def clip_context():
    worker = _require_worker()
    data = request.get_json(silent=True)
    if not data or "conversation_id" not in data:
        raise ClipInputError("Missing 'conversation_id' field")
    conversation_id = data["conversation_id"]
    if not isinstance(conversation_id, str):
        raise ClipInputError("conversation_id must be a string")
    worker.register_context(conversation_id)
    return jsonify({"accepted": True, "conversation_id": conversation_id}), 200
