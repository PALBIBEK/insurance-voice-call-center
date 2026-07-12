"""Server entrypoint: python -m insurance_voice.asgi"""

import uvicorn

from insurance_voice.config import get_settings


def run_server() -> None:
    settings = get_settings()
    uvicorn.run(
        "insurance_voice.asgi:app",
        host=settings.APP_HOST,
        port=settings.APP_PORT,
        log_level=settings.LOG_LEVEL.lower(),
        server_header=False,
    )


def _build_app():
    from insurance_voice.factory import create_app

    return create_app()


app = _build_app()

if __name__ == "__main__":
    run_server()
