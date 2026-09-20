import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    GROQ_LLM_MODEL: str = os.getenv("GROQ_LLM_MODEL", "qwen/qwen3.6-27b")
    GROQ_AGENT_MODEL: str = os.getenv("GROQ_AGENT_MODEL", "openai/gpt-oss-20b")
    GROQ_STT_MODEL: str = os.getenv("GROQ_STT_MODEL", "whisper-large-v3")
    # RTC live-stream STT: rolling partials use a fast model; the final
    # authoritative transcription of each utterance uses the final model.
    GROQ_RTC_PARTIAL_MODEL: str = os.getenv("GROQ_RTC_PARTIAL_MODEL", "whisper-large-v3-turbo")
    GROQ_RTC_FINAL_MODEL: str = os.getenv("GROQ_RTC_FINAL_MODEL", "").strip()
    GROQ_TTS_MODEL: str = os.getenv("GROQ_TTS_MODEL", "canopylabs/orpheus-v1-english")
    TTS_VOICE: str = os.getenv("TTS_VOICE", "autumn")
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///chat.db")

    LLM_TEMPERATURE: float = 0.7
    LLM_MAX_TOKENS: int = 2048
    LLM_TOP_P: float = 1.0

    STT_TEMPERATURE: float = 0.0
    STT_PROMPT: str = os.getenv("STT_PROMPT", "")
    STT_LANGUAGE: str = os.getenv("STT_LANGUAGE", "en")


    # --- reSpeaker Clip input ---
    VOICE_INPUT_MODE: str = os.getenv("VOICE_INPUT_MODE", "both")
    CLIP_BLE_ADDRESS: str = os.getenv("CLIP_BLE_ADDRESS", "")
    CLIP_BLE_NAME: str = os.getenv("CLIP_BLE_NAME", "Clip")
    CLIP_RECORD_MODE: str = os.getenv("CLIP_RECORD_MODE", "enhanced")
    CLIP_STATUS_INTERVAL: int = int(os.getenv("CLIP_STATUS_INTERVAL", "5"))
    CLIP_DOWNLOAD_TIMEOUT: int = int(os.getenv("CLIP_DOWNLOAD_TIMEOUT", "300"))
    CLIP_TEMP_DIR: str = os.getenv("CLIP_TEMP_DIR", "clip_audio")
    CLIP_MAX_FAILED_ARTIFACTS: int = int(os.getenv("CLIP_MAX_FAILED_ARTIFACTS", "5"))

    # --- Deployment scope ---
    # When false the service runs as a device gateway only: the Clip runtime,
    # its HTTP API and the audio-exchange path load, and the agent stack
    # (LangGraph/Groq/Mem0/Pinecone/Supabase conversations) is neither imported
    # nor started. Utterances and downloaded sessions are re-containerized to
    # Ogg, kept on disk and announced over SSE instead of being transcribed
    # and answered; no GROQ_API_KEY is required.
    AGENT_ENABLED: bool = os.getenv("AGENT_ENABLED", "true").lower() in ("1", "true", "yes", "on")

    # --- reSpeaker Clip RTC live streaming (AT+START=rtc) ---
    # One RTC session stays armed for the whole process: the firmware mic
    # pipeline runs warm while it emits nothing over BLE during warm pauses.
    # Each RESUME->PAUSE interval is one logical utterance; rolling partial
    # transcripts use cumulative in-memory Ogg snapshots, and the final
    # transcript goes through AudioService.process_transcript exactly once.
    RTC_AUTO_ARM: bool = os.getenv("RTC_AUTO_ARM", "true").lower() in ("1", "true", "yes", "on")
    # Ignore RTC state notifications arriving shortly after arming: the
    # initial STREAMING/PAUSED pair can lag behind the arm commands in the
    # BLE notification stream and must not start/finalize an utterance.
    RTC_SETTLE_SECONDS: float = float(os.getenv("RTC_SETTLE_SECONDS", "1.0"))
    # Bounded time to wait for the RTC stream (STREAM_START) while arming.
    RTC_ARM_TIMEOUT: float = float(os.getenv("RTC_ARM_TIMEOUT", "15"))
    # Rolling partial transcription cadence (seconds between snapshots).
    RTC_PARTIAL_INTERVAL: float = float(os.getenv("RTC_PARTIAL_INTERVAL", "2.0"))
    # A snapshot must contain at least this many Opus frames (~20 ms each at
    # 50 fps) before a partial STT request is worth issuing.
    RTC_PARTIAL_MIN_FRAMES: int = int(os.getenv("RTC_PARTIAL_MIN_FRAMES", "25"))
    # Utterances shorter than this many frames never reach the LLM.
    RTC_MIN_UTTERANCE_FRAMES: int = int(os.getenv("RTC_MIN_UTTERANCE_FRAMES", "25"))
    # Hard bound on buffered frames per utterance (50 fps * 3600 s ~ 180k).
    RTC_MAX_UTTERANCE_FRAMES: int = int(os.getenv("RTC_MAX_UTTERANCE_FRAMES", "180000"))
    # Tentative pre-roll ring: frames arriving right around a RESUME/STREAMING
    # transition are seeded into the next utterance so the first words are
    # never lost when event and frame characteristics race.
    RTC_PRE_ROLL_FRAMES: int = int(os.getenv("RTC_PRE_ROLL_FRAMES", "15"))
    # Upper bound on finalize jobs waiting for their STT/LLM turn (FIFO).
    RTC_MAX_PENDING_FINALIZE: int = int(os.getenv("RTC_MAX_PENDING_FINALIZE", "16"))
    TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "")

    # Composio: API key + the toolkits exposed to the agent through
    # Composio sessions (empty string disables the Composio integration).
    COMPOSIO_API_KEY: str = os.getenv("COMPOSIO_API_KEY", "")
    COMPOSIO_TOOLKITS: list[str] = [
        t.strip()
        for t in os.getenv("COMPOSIO_TOOLKITS", "github").split(",")
        if t.strip()
    ]

    FMP_API_KEY: str = os.getenv("FMP_API_KEY", "")

    # Legacy direct SaaS modules. These settings remain for compatibility,
    # but the hybrid registry routes these apps through Composio.
    GMAIL_CREDENTIALS_FILE: str = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    GMAIL_TOKEN_FILE: str = os.getenv("GMAIL_TOKEN_FILE", "token.json")

    SLACK_BOT_TOKEN: str = os.getenv("SLACK_BOT_TOKEN", "")
    SLACK_USER_TOKEN: str = os.getenv("SLACK_USER_TOKEN", "")

    LINEAR_API_KEY: str = os.getenv("LINEAR_API_KEY", "")

    SHOPIFY_ACCESS_TOKEN: str = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
    SHOPIFY_AGENT_PROFILE: str = os.getenv(
        "SHOPIFY_AGENT_PROFILE",
        "https://shopify.dev/ucp/agent-profiles/examples/2026-04-08/valid-with-capabilities.json",
    )
    SHOPIFY_CLIENT_ID: str = os.getenv("SHOPIFY_CLIENT_ID", "")
    SHOPIFY_CLIENT_SECRET: str = os.getenv("SHOPIFY_CLIENT_SECRET", "")

    MEM0_API_KEY: str = os.getenv("MEM0_API_KEY", "")
    MEM0_USER_ID: str = os.getenv("MEM0_USER_ID", "user-1")

    NOTION_API_KEY: str = os.getenv("NOTION_API_KEY", "")
    NOTION_DATABASE_ID: str = os.getenv("NOTION_DATABASE_ID", "")

    USER_ID: str = os.getenv("USER_ID", "user-1")

    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

    PINECONE_API_KEY: str = os.getenv("PINECONE_API_KEY", "")
    PINECONE_INDEX_NAME: str = os.getenv("PINECONE_INDEX_NAME", "conversations")
    PINECONE_CLOUD: str = os.getenv("PINECONE_CLOUD", "aws")
    PINECONE_REGION: str = os.getenv("PINECONE_REGION", "us-east-1")

    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
    EMBEDDING_DIM: int = 384


settings = Settings()
