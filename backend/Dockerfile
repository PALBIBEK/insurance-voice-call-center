FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY insurance_voice ./insurance_voice
COPY scripts ./scripts

RUN pip install --no-cache-dir ".[postgres]"

EXPOSE 8000
CMD ["python", "-m", "insurance_voice.asgi"]
