"""Bounded in-memory cache for approved, non-patient-specific phrases.

Wraps any ``TextToSpeechProvider``, so the policy is engine-independent.

The policy is an **allowlist, not a general text→audio cache**. Only phrases
that appear in the application's own response catalogue are cacheable. That is
a privacy control, not an optimisation detail:

* A general cache keyed on arbitrary text would end up holding
  "Booked. Dr Sarah Patel, tomorrow at 12 pm" — a specific patient's
  appointment — in memory for the next caller's benefit.
* Keys are a hash of the phrase, so even the allowlisted keys are not readable
  text in a heap dump.

Cached entries are held in memory only. Nothing is written to disk, so there is
no audio artefact to leak, delete or forget about.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from collections.abc import AsyncIterator

from app.core.logging import get_logger
from app.core.metrics import M, metrics
from app.speech.providers import TextToSpeechProvider

logger = get_logger(__name__)

#: Default ceiling. Each catalogue phrase is ~1-3 s of 16 kHz PCM (~32-96 KB),
#: so 64 entries is a few megabytes at most.
DEFAULT_MAX_ENTRIES = 64


def cacheable_phrases() -> frozenset[str]:
    """Exactly the phrases the application is allowed to cache.

    Built from the response catalogue, and deliberately excludes anything with
    a format placeholder: a template like ``"Booked. {practitioner}, {when}."``
    only becomes speech once it has been filled with patient-specific detail,
    and that filled text must never be cached.
    """
    from app.conversation import responses

    approved: set[str] = set(responses.PREGENERATED)
    for name in dir(responses):
        if name.startswith("_") or not name.isupper():
            continue
        value = getattr(responses, name)
        if isinstance(value, str) and "{" not in value:
            approved.add(value)
        elif isinstance(value, dict):
            approved.update(v for v in value.values() if isinstance(v, str) and "{" not in v)
    return frozenset(p for p in approved if p.strip())


class CachingTTS:
    """``TextToSpeechProvider`` decorator with an allowlisted LRU phrase cache."""

    def __init__(
        self,
        inner: TextToSpeechProvider,
        *,
        allowlist: frozenset[str] | None = None,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._inner = inner
        self._allowlist = allowlist if allowlist is not None else cacheable_phrases()
        self._max_entries = max_entries
        self._entries: OrderedDict[str, bytes] = OrderedDict()

    @property
    def inner(self) -> TextToSpeechProvider:
        return self._inner

    @property
    def size(self) -> int:
        return len(self._entries)

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha256(text.strip().encode()).hexdigest()[:32]

    def is_cacheable(self, text: str) -> bool:
        return text.strip() in self._allowlist

    async def synthesize(self, text: str) -> bytes:
        cleaned = (text or "").strip()
        if not cleaned:
            return b""
        if not self.is_cacheable(cleaned):
            metrics.increment(M.TTS_CACHE_MISS, labels={"reason": "not_cacheable"})
            return await self._inner.synthesize(cleaned)

        key = self._key(cleaned)
        cached = self._entries.get(key)
        if cached is not None:
            self._entries.move_to_end(key)
            metrics.increment(M.TTS_CACHE_HIT)
            return cached

        metrics.increment(M.TTS_CACHE_MISS, labels={"reason": "cold"})
        audio = await self._inner.synthesize(cleaned)
        self._store(key, audio)
        return audio

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        """Stream, serving a cache hit as a single immediate chunk.

        A cache hit is already complete audio, so there is nothing to stream —
        emitting it whole is both correct and the lowest possible latency.
        """
        cleaned = (text or "").strip()
        if not cleaned:
            return
        if self.is_cacheable(cleaned):
            key = self._key(cleaned)
            cached = self._entries.get(key)
            if cached is not None:
                self._entries.move_to_end(key)
                metrics.increment(M.TTS_CACHE_HIT)
                yield cached
                return
            metrics.increment(M.TTS_CACHE_MISS, labels={"reason": "cold"})
            collected = bytearray()
            async for chunk in self._inner.synthesize_stream(cleaned):
                collected.extend(chunk)
                yield chunk
            self._store(key, bytes(collected))
            return

        metrics.increment(M.TTS_CACHE_MISS, labels={"reason": "not_cacheable"})
        async for chunk in self._inner.synthesize_stream(cleaned):
            yield chunk

    def _store(self, key: str, audio: bytes) -> None:
        if not audio:
            return
        self._entries[key] = audio
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    async def warm_cache(self, phrases: tuple[str, ...] = ()) -> None:
        """Pre-synthesise approved phrases so common turns need no synthesis.

        Anything not on the allowlist is skipped rather than synthesised, so a
        caller-specific string passed here cannot end up cached by accident.
        """
        await self._inner.warm_cache(phrases)
        targets = phrases or tuple(sorted(self._allowlist))
        warmed = 0
        for phrase in targets:
            if not self.is_cacheable(phrase):
                continue
            if self._key(phrase) in self._entries:
                continue
            await self.synthesize(phrase)
            warmed += 1
        logger.info("tts.cache_warmed", entries=self.size, synthesised=warmed)

    async def health(self) -> bool:
        return await self._inner.health()

    async def aclose(self) -> None:
        self._entries.clear()
        closer = getattr(self._inner, "aclose", None)
        if closer is not None:
            await closer()
