"""Session storage.

Conversation state is transient and expires: it exists for the duration of a
call plus a short grace period, and is never the system of record. Anything
that must outlive the call (the appointment, the call outcome, audit events)
is written to PostgreSQL by the relevant service.

Two backends:

* ``RedisSessionStore``  — production. Survives a backend restart and is shared
  across workers, which matters because a telephony gateway and an API worker
  may touch the same call.
* ``InMemorySessionStore`` — tests and single-process development only. It is
  process-local, so it silently breaks with more than one worker; ``Settings``
  refuses to boot a production instance without ``REDIS_URL`` for that reason.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

from app.conversation.session import ConversationSession
from app.core.logging import get_logger

logger = get_logger(__name__)

_KEY_PREFIX = "clinic:session:"


@runtime_checkable
class SessionStore(Protocol):
    async def get(self, session_id: str) -> ConversationSession | None: ...
    async def save(self, session: ConversationSession) -> None: ...
    async def delete(self, session_id: str) -> None: ...
    async def ping(self) -> bool: ...
    async def close(self) -> None: ...


class InMemorySessionStore:
    """Process-local store with TTL semantics matching the Redis backend."""

    def __init__(self, ttl_seconds: int = 1800) -> None:
        self._ttl = ttl_seconds
        self._data: dict[str, tuple[float, str]] = {}

    async def get(self, session_id: str) -> ConversationSession | None:
        entry = self._data.get(session_id)
        if entry is None:
            return None
        expires_at, payload = entry
        if expires_at < time.monotonic():
            self._data.pop(session_id, None)
            return None
        return ConversationSession.model_validate_json(payload)

    async def save(self, session: ConversationSession) -> None:
        session.touch()
        self._data[session.session_id] = (
            time.monotonic() + self._ttl,
            session.model_dump_json(),
        )

    async def delete(self, session_id: str) -> None:
        self._data.pop(session_id, None)

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        self._data.clear()


class RedisSessionStore:
    """Redis-backed store. Values are JSON with a sliding TTL."""

    def __init__(self, url: str, ttl_seconds: int = 1800) -> None:
        from redis.asyncio import Redis

        self._redis: Redis = Redis.from_url(url, decode_responses=True)
        self._ttl = ttl_seconds

    @staticmethod
    def _key(session_id: str) -> str:
        return f"{_KEY_PREFIX}{session_id}"

    async def get(self, session_id: str) -> ConversationSession | None:
        payload = await self._redis.get(self._key(session_id))
        if payload is None:
            return None
        return ConversationSession.model_validate_json(payload)

    async def save(self, session: ConversationSession) -> None:
        session.touch()
        # TTL is refreshed on every write, so a long call never expires
        # mid-conversation while an abandoned one still ages out.
        await self._redis.set(
            self._key(session.session_id), session.model_dump_json(), ex=self._ttl
        )

    async def delete(self, session_id: str) -> None:
        await self._redis.delete(self._key(session_id))

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception as exc:
            logger.warning("session_store.ping_failed", error=type(exc).__name__)
            return False

    async def close(self) -> None:
        await self._redis.aclose()


def build_session_store(redis_url: str, ttl_seconds: int) -> SessionStore:
    """Pick a backend from configuration. Redis when a URL is configured."""
    if redis_url:
        logger.info("session_store.backend", backend="redis")
        return RedisSessionStore(redis_url, ttl_seconds)
    logger.warning(
        "session_store.backend",
        backend="memory",
        detail="in-process store; not valid for multi-worker deployments",
    )
    return InMemorySessionStore(ttl_seconds)
