"""Typed application settings.

Single Settings class, everything overridable via IVCC_-prefixed env vars
or a git-ignored .env file. Secrets never get hardcoded defaults.
"""

import typing as t

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="IVCC_", env_file=".env", extra="ignore")

    ENV: t.Literal["development", "production", "test"] = "development"
    LOG_LEVEL: str = "INFO"
    SECRET_KEY: str = "dev-only-not-a-secret"
    API_KEY: str = ""  # static key guarding /api; empty disables the guard (dev/demo)

    # --- Inference (OpenRouter) ---
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    # Two-tier model choice: cheap/fast for triage + routine turns,
    # stronger tier reserved for claim-probability reasoning.
    OPENROUTER_MODEL_TRIAGE: str = "openai/gpt-4o-mini"
    OPENROUTER_MODEL_SPECIALIST: str = "openai/gpt-4o"

    # --- Voice (ElevenLabs) ---
    ELEVENLABS_API_KEY: str = ""
    ELEVENLABS_AGENT_ID: str = ""
    # Premade voice usable on the free tier ("George"); library voices 402 there.
    ELEVENLABS_VOICE_ID: str = "JBFqnCBsd6RMkjVDRZzb"
    ELEVENLABS_TTS_MODEL: str = "eleven_flash_v2_5"  # 0.5 credits/char
    VOICE_WEBHOOK_SECRET: str = ""  # shared secret validated on /api/voice/completions

    # --- Demo user (seeded into user_info at startup) ---
    DEMO_USER_ID: str = "Bibek"
    DEMO_USER_PASSWORD: str = "Bibek"

    # --- Stores ---
    DATABASE_URL: str = "sqlite+aiosqlite:///./ivcc.db"
    REDIS_URL: str = ""  # empty -> in-memory session store + event bus (single process)

    # --- Mock tool behavior ---
    TOOL_LATENCY_MIN_S: float = 2.0
    TOOL_LATENCY_MAX_S: float = 3.0
    TOOL_FAILURE_RATE: float = 0.0
    TOOL_MAX_RETRIES: int = 3
    TOOL_RETRY_BACKOFF_BASE_S: float = 0.5

    # --- Drift detection ---
    DRIFT_MAX_STAGNANT_TURNS: int = 3
    DRIFT_MAX_OFFDOMAIN_TURNS: int = 2

    # --- Server ---
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000

    # --- Frontend (separate process; the backend only exposes APIs) ---
    # Origins allowed to call the API from the browser (the standalone UI server).
    CORS_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000"
