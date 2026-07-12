"""Config layer: typed settings, env override with IVCC_ prefix, test profile."""

from insurance_voice.config.settings import Settings


def test_default_settings_are_dev_safe():
    s = Settings()
    assert s.ENV == "development"
    assert s.DATABASE_URL.startswith("sqlite+aiosqlite")
    assert s.REDIS_URL == ""  # empty -> in-memory store/bus fallback
    assert 2.0 <= s.TOOL_LATENCY_MIN_S <= s.TOOL_LATENCY_MAX_S <= 3.0
    assert s.TOOL_MAX_RETRIES >= 1
    assert s.DRIFT_MAX_STAGNANT_TURNS >= 2


def test_env_override_with_prefix(monkeypatch):
    monkeypatch.setenv("IVCC_ENV", "production")
    monkeypatch.setenv("IVCC_TOOL_FAILURE_RATE", "0.5")
    monkeypatch.setenv("IVCC_OPENROUTER_API_KEY", "sk-test")
    s = Settings()
    assert s.ENV == "production"
    assert s.TOOL_FAILURE_RATE == 0.5
    assert s.OPENROUTER_API_KEY == "sk-test"


def test_two_tier_model_choice_exists():
    s = Settings()
    assert s.OPENROUTER_MODEL_TRIAGE  # cheap/fast tier
    assert s.OPENROUTER_MODEL_SPECIALIST  # reasoning tier
    assert s.OPENROUTER_BASE_URL == "https://openrouter.ai/api/v1"


def test_settings_singleton_accessor():
    from insurance_voice.config import get_settings

    a = get_settings()
    b = get_settings()
    assert a is b
