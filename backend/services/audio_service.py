"""Shared audio -> transcript -> LangGraph -> persistence pipeline.

Extracted from the browser voice route so the Clip ingestion workflow and the
legacy browser microphone path run through identical processing (STT, routing,
conversation persistence, memory, vector indexing).  The service accepts raw
bytes or a local file path and returns
``{"transcript", "response", "conversation_id", "route"}``.
"""

from __future__ import annotations

import logging
from typing import Callable

from backend.database import (
    create_conversation,
    save_turn,
    get_recent_messages,
)
from backend.graph import build_graph, AgentState
from backend.llm.stt import transcribe_bytes
from backend.memory import recall, save_exchange
from backend.services import index_conversation_async

logger = logging.getLogger(__name__)


class AudioService:
    """Single-user audio chat pipeline used by browser and Clip inputs."""

    def __init__(self) -> None:
        self._graph = None

    def _get_graph(self):
        if self._graph is None:
            self._graph = build_graph()
        return self._graph

    def process_transcript(
        self,
        transcript: str,
        conversation_id: str | None = None,
    ) -> dict:
        """Run the transcript through LangGraph and persist the exchange."""
        cid = conversation_id or create_conversation()

        state: AgentState = {
            "messages": [],
            "transcript": transcript,
            "route": "",
            "response": "",
            "error": None,
            "memories": recall(transcript),
            "history": get_recent_messages(cid, 10),
        }
        result = self._get_graph().invoke(state)

        save_turn(cid, "user", transcript)
        save_turn(cid, "assistant", result["response"])
        save_exchange(transcript, result["response"])
        index_conversation_async(cid)

        return {
            "transcript": transcript,
            "response": result["response"],
            "conversation_id": cid,
            "route": result.get("route", ""),
        }

    def process_transcript_stream(
        self,
        transcript: str,
        conversation_id: str | None = None,
        on_event: Callable[[dict], None] | None = None,
    ) -> dict:
        """streaming variant of :meth:`process_transcript`.

        Same router, same persistence, same result shape — but when
        ``on_event`` is supplied it emits feedback as the reply is produced:
          {"type":"thinking","tool":""}       generation started
          {"type":"thinking","tool":"<name>"} agent about to call a tool
          {"type":"token","text":"<piece>"}   streamed answer text
        The plain non-streaming path is unchanged (used by browser mic).
        """
        cid = conversation_id or create_conversation()

        state: AgentState = {
            "messages": [],
            "transcript": transcript,
            "route": "",
            "response": "",
            "error": None,
            "memories": recall(transcript),
            "history": get_recent_messages(cid, 10),
        }

        from backend.graph.router import router_node
        from backend.graph.nodes.agentic import _get_agent, MAX_AGENT_ITERATIONS
        from backend.graph.nodes.simple import SYSTEM_PROMPT as SIMPLE_SYSTEM
        from backend.graph.nodes.persona import SYSTEM_PROMPT as PERSONA_SYSTEM
        from backend.llm.client import llm
        from backend.memory import format_memories
        from backend.utils.text import extract_answer, strip_thinking

        def emit(event: dict) -> None:
            if on_event is not None:
                try:
                    on_event(event)
                except Exception:
                    logger.debug("clip RTC stream on_event failed", exc_info=True)

        try:
            routed = router_node(state)
        except Exception:
            routed = {"route": "simple"}
        route = routed.get("route") if isinstance(routed, dict) else "simple"
        if route not in ("simple", "context", "persona"):
            route = "simple"

        emit({"type": "thinking", "tool": ""})
        user_content = format_memories(state["memories"]) + transcript

        if route == "context":
            # Tool-capable agent: surface each tool call as a thinking event
            # during the run, then stream the (cleaned) answer afterwards.
            messages = [*state.get("history", []), ("user", user_content)]
            config = {"recursion_limit": MAX_AGENT_ITERATIONS}
            full = ""
            for step in _get_agent().stream(
                {"messages": messages}, config=config, stream_mode="values"
            ):
                msgs = step.get("messages", [])
                if not msgs:
                    continue
                last = msgs[-1]
                if getattr(last, "type", "") == "ai":
                    for tc in getattr(last, "tool_calls", []) or []:
                        emit({"type": "thinking", "tool": tc.get("name", "") or "a tool"})
                    content = getattr(last, "content", "") or ""
                    if content:
                        full = content
            response = extract_answer(full)
            step = 40
            for i in range(0, len(response), step):
                emit({"type": "token", "text": response[i:i + step]})
        else:
            # simple / persona: single streaming LLM call.
            system_prompt = PERSONA_SYSTEM if route == "persona" else SIMPLE_SYSTEM
            messages = [
                {"role": "system", "content": system_prompt},
                *state.get("history", []),
                {"role": "user", "content": user_content},
            ]
            full = ""
            last_len = 0
            for chunk in llm.stream(messages):
                content = getattr(chunk, "content", "") or ""
                if not content:
                    continue
                full += content
                clean = strip_thinking(full)
                if len(clean) > last_len:
                    emit({"type": "token", "text": clean[last_len:]})
                    last_len = len(clean)
            response = extract_answer(full)

        save_turn(cid, "user", transcript)
        save_turn(cid, "assistant", response)
        save_exchange(transcript, response)
        index_conversation_async(cid)

        return {
            "transcript": transcript,
            "response": response,
            "conversation_id": cid,
            "route": route,
        }

    def process_audio(
        self,
        audio_bytes: bytes,
        filename: str = "audio.wav",
        conversation_id: str | None = None,
    ) -> dict:
        """Transcribe raw audio bytes, then run the shared pipeline."""
        transcript = transcribe_bytes(audio_bytes, filename)
        return self.process_transcript(transcript, conversation_id)

    def process_audio_file(
        self,
        file_path: str,
        filename: str | None = None,
        conversation_id: str | None = None,
    ) -> dict:
        """Read a local audio file (e.g. a Clip Ogg) and process it."""
        import os

        path = file_path
        with open(path, "rb") as handle:
            audio_bytes = handle.read()
        return self.process_audio(
            audio_bytes,
            filename or os.path.basename(path),
            conversation_id,
        )
