import functools

from insurance_voice.config.settings import Settings


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
