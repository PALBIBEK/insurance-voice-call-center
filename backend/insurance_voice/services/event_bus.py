"""Per-session pub/sub fan-out.

Everything interesting that happens inside a turn (tool activity,
handoffs, approval prompts) is published here; the frontend WebSocket
handler subscribes and relays. The Redis backend is what lets an
approval REST call landing on worker B reach a WebSocket held open on
worker A.
"""

import asyncio
import contextlib
import json
import typing as t


class EventBus(t.Protocol):
    async def publish(self, session_id: str, event: dict) -> None: ...
    def subscribe(self, session_id: str) -> t.AsyncContextManager["asyncio.Queue[dict]"]: ...


class InMemoryEventBus:
    def __init__(self):
        self._subscribers: dict[str, set[asyncio.Queue]] = {}

    async def publish(self, session_id: str, event: dict) -> None:
        for queue in self._subscribers.get(session_id, set()):
            queue.put_nowait(event)

    @contextlib.asynccontextmanager
    async def subscribe(self, session_id: str) -> t.AsyncIterator["asyncio.Queue[dict]"]:
        queue: asyncio.Queue[dict] = asyncio.Queue()
        self._subscribers.setdefault(session_id, set()).add(queue)
        try:
            yield queue
        finally:
            self._subscribers[session_id].discard(queue)
            if not self._subscribers[session_id]:
                del self._subscribers[session_id]


class RedisEventBus:
    def __init__(self, redis_url: str):
        import redis.asyncio as aioredis

        self._redis = aioredis.from_url(redis_url, decode_responses=True)

    @staticmethod
    def _channel(session_id: str) -> str:
        return f"session-events:{session_id}"

    async def publish(self, session_id: str, event: dict) -> None:
        await self._redis.publish(self._channel(session_id), json.dumps(event))

    @contextlib.asynccontextmanager
    async def subscribe(self, session_id: str) -> t.AsyncIterator["asyncio.Queue[dict]"]:
        queue: asyncio.Queue[dict] = asyncio.Queue()
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(self._channel(session_id))

        async def reader() -> None:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    queue.put_nowait(json.loads(message["data"]))

        reader_task = asyncio.create_task(reader())
        try:
            yield queue
        finally:
            reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader_task
            await pubsub.unsubscribe(self._channel(session_id))
            await pubsub.aclose()


def build_event_bus(redis_url: str) -> EventBus:
    if redis_url:
        return RedisEventBus(redis_url)
    return InMemoryEventBus()
