"""Hot per-session state, keyed by session id.

Two interchangeable backends behind one interface:
- InMemorySessionStore: dev/tests/single-process.
- RedisSessionStore: multi-worker deployments (docker-compose).

Only coordination state lives here (status, current agent, drift counters,
rolling LLM context window). The turn table in Postgres stays the source
of truth for the transcript.
"""

import json
import typing as t


class SessionStore(t.Protocol):
    async def ping(self) -> None: ...
    async def set_state(self, session_id: str, state: dict) -> None: ...
    async def get_state(self, session_id: str) -> dict: ...
    async def update_state(self, session_id: str, **fields: t.Any) -> None: ...
    async def append_history(self, session_id: str, message: dict) -> None: ...
    async def get_history(self, session_id: str) -> list[dict]: ...


DEFAULT_HISTORY_LIMIT = 30


class InMemorySessionStore:
    def __init__(self, history_limit: int = DEFAULT_HISTORY_LIMIT):
        self._history_limit = history_limit
        self._state: dict[str, dict] = {}
        self._history: dict[str, list[dict]] = {}

    async def ping(self) -> None:
        return None

    async def set_state(self, session_id: str, state: dict) -> None:
        self._state[session_id] = dict(state)

    async def get_state(self, session_id: str) -> dict:
        return dict(self._state.get(session_id, {}))

    async def update_state(self, session_id: str, **fields: t.Any) -> None:
        self._state.setdefault(session_id, {}).update(fields)

    async def append_history(self, session_id: str, message: dict) -> None:
        history = self._history.setdefault(session_id, [])
        history.append(dict(message))
        del history[:-self._history_limit]

    async def get_history(self, session_id: str) -> list[dict]:
        return [dict(m) for m in self._history.get(session_id, [])]


class RedisSessionStore:
    def __init__(self, redis_url: str, history_limit: int = DEFAULT_HISTORY_LIMIT):
        import redis.asyncio as aioredis

        self._history_limit = history_limit
        self._redis = aioredis.from_url(redis_url, decode_responses=True)

    async def ping(self) -> None:
        await self._redis.ping()

    @staticmethod
    def _state_key(session_id: str) -> str:
        return f"session:{session_id}"

    @staticmethod
    def _history_key(session_id: str) -> str:
        return f"session:{session_id}:history"

    async def set_state(self, session_id: str, state: dict) -> None:
        key = self._state_key(session_id)
        pipe = self._redis.pipeline()
        pipe.delete(key)
        if state:
            pipe.hset(key, mapping={k: json.dumps(v) for k, v in state.items()})
        await pipe.execute()

    async def get_state(self, session_id: str) -> dict:
        raw = await self._redis.hgetall(self._state_key(session_id))
        return {k: json.loads(v) for k, v in raw.items()}

    async def update_state(self, session_id: str, **fields: t.Any) -> None:
        await self._redis.hset(
            self._state_key(session_id), mapping={k: json.dumps(v) for k, v in fields.items()}
        )

    async def append_history(self, session_id: str, message: dict) -> None:
        key = self._history_key(session_id)
        pipe = self._redis.pipeline()
        pipe.rpush(key, json.dumps(message))
        pipe.ltrim(key, -self._history_limit, -1)
        await pipe.execute()

    async def get_history(self, session_id: str) -> list[dict]:
        raw = await self._redis.lrange(self._history_key(session_id), 0, -1)
        return [json.loads(m) for m in raw]


def build_session_store(redis_url: str, history_limit: int = DEFAULT_HISTORY_LIMIT) -> SessionStore:
    if redis_url:
        return RedisSessionStore(redis_url, history_limit=history_limit)
    return InMemorySessionStore(history_limit=history_limit)
