"""Blueprint registration.

``agent_enabled=False`` registers only the health and Clip blueprints and never
imports the agent modules, so a device-gateway process does not pull in
LangGraph, Groq, Composio or the vector store.
"""

from backend.routes.clip import clip_bp
from backend.routes.health import health_bp


def register_routes(app, *, agent_enabled: bool = True) -> None:
    app.register_blueprint(health_bp, url_prefix="/api")
    app.register_blueprint(clip_bp, url_prefix="/api")
    if not agent_enabled:
        return

    # Imported late: these modules construct LLM clients at import time.
    from backend.routes.chat import chat_bp
    from backend.routes.composio import composio_bp
    from backend.routes.tts import tts_bp
    from backend.routes.voice import voice_bp

    for blueprint in (chat_bp, voice_bp, tts_bp, composio_bp):
        app.register_blueprint(blueprint, url_prefix="/api")