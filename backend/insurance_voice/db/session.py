"""Async engine + session factory.

One Database object per process. Sessions are handed out explicitly
(`async with db.session() as s`) and passed down through service calls -
no task-local "current session" global, so concurrent call sessions can
never share ORM state by accident.
"""

import contextlib
import typing as t

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from insurance_voice.db.base import Base


class Database:
    def __init__(self, database_url: str, *, echo: bool = False):
        kwargs: dict = {"echo": echo}
        if database_url.endswith(":memory:"):
            # Every pooled connection to a :memory: SQLite is a *different*
            # empty database - share one connection so concurrent tasks in
            # tests all see the same schema/data.
            from sqlalchemy.pool import StaticPool

            kwargs["poolclass"] = StaticPool
        self.engine = create_async_engine(database_url, **kwargs)
        self._session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    @contextlib.asynccontextmanager
    async def session(self) -> t.AsyncIterator[AsyncSession]:
        async with self._session_factory() as session:
            yield session

    async def create_all(self) -> None:
        # Import for side effect: registers all models on Base.metadata
        import insurance_voice.db.models  # noqa: F401

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()
