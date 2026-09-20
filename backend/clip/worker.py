"""Dedicated daemon-thread asyncio host for the Clip runtime.

Flask is synchronous, so the Clip ``ClipRuntime`` lives on one persistent
asyncio event loop in a background daemon thread.  Flask request handlers
submit coroutines to that loop via :meth:`ClipWorker.call` and block on the
result.  Clip offline/unavailable never prevents Flask from serving.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Coroutine

from backend.clip.exceptions import ClipUnavailableError
from backend.clip.runtime import ClipRuntime

logger = logging.getLogger(__name__)


class ClipWorker:
    """Thread-safe façade over a runtime running on a daemon thread."""

    def __init__(self, runtime_factory=None) -> None:
        self._factory = runtime_factory or (lambda: ClipRuntime())
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._runtime: ClipRuntime | None = None
        self._ready = threading.Event()

    # -- lifecycle --------------------------------------------------------

    @property
    def running(self) -> bool:
        return bool(
            self._thread is not None
            and self._thread.is_alive()
            and self._loop is not None
            and not self._loop.is_closed()
        )

    @property
    def runtime(self) -> ClipRuntime | None:
        return self._runtime

    def start(self) -> None:
        if self.running:
            return
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run, name="clip-worker", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=15.0):
            raise RuntimeError("Clip worker failed to start within 15s")

    def stop(self) -> None:
        loop = self._loop
        runtime = self._runtime
        if loop is None or runtime is None:
            self._thread = None
            return
        try:
            future = asyncio.run_coroutine_threadsafe(runtime.shutdown(), loop)
            future.result(timeout=10)
        except Exception as exc:
            logger.debug("clip worker shutdown error: %s", exc)
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._loop = None
        self._thread = None
        self._runtime = None

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        runtime = self._factory()
        self._runtime = runtime
        loop.create_task(runtime.run_forever())
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                pending = [
                    task
                    for task in asyncio.all_tasks(loop)
                    if not task.done()
                ]
                if pending:
                    for task in pending:
                        task.cancel()
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:
                pass
            self._loop = None
            self._runtime = None
            loop.close()

    # -- coroutine dispatch ----------------------------------------------

    def call(self, coro: Coroutine, timeout: float | None = None) -> Any:
        if not self.running or self._loop is None or self._runtime is None:
            raise ClipUnavailableError("Clip runtime is not running")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    # -- synchronous façade used by Flask routes ---------------------------

    def _require_runtime(self) -> ClipRuntime:
        if not self.running or self._runtime is None:
            raise ClipUnavailableError("Clip runtime is not running")
        return self._runtime

    def get_status(self) -> dict[str, Any]:
        runtime = self._require_runtime()
        return self.call(runtime.status_payload())

    def start_recording(
        self, mode: str | None = None, conversation_id: str | None = None
    ) -> dict[str, Any]:
        runtime = self._require_runtime()
        return self.call(
            runtime.start_recording(mode=mode, conversation_id=conversation_id)
        )

    def stop_recording(self) -> dict[str, Any]:
        runtime = self._require_runtime()
        return self.call(runtime.stop_recording())

    def rtc_resume(self) -> dict[str, Any]:
        runtime = self._require_runtime()
        return self.call(runtime.rtc_resume())

    def rtc_pause(self) -> dict[str, Any]:
        runtime = self._require_runtime()
        return self.call(runtime.rtc_pause())

    def ingest(self, session_id: str, trigger: str = "manual") -> dict[str, Any]:
        runtime = self._require_runtime()
        return self.call(runtime.manual_ingest(session_id, trigger=trigger))

    def register_context(self, conversation_id: str | None) -> None:
        runtime = self._require_runtime()
        self.call(runtime.set_active_conversation(conversation_id))

    def iter_events(self, after_id: int | None = None) -> Any:
        if self._runtime is None:
            return iter(())
        return self._runtime.iter_events(after_id=after_id)
