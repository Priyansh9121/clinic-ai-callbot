# Redis

Holds transient conversation sessions only, with a TTL
(`SESSION_TTL_SECONDS`, default 30 minutes). It is never the system of record —
anything that must outlive a call is written to PostgreSQL.

Configured with persistence **off** (`--save "" --appendonly no`): session state
is disposable by design, and not writing it to disk is also the privacy-correct
default. Memory is capped at 256 MB with `allkeys-lru`.

Redis is optional locally — with `REDIS_URL` empty the backend falls back to an
in-process store — but it is **required** in production, and `Settings` refuses
to boot without it. The in-process store is per-worker, so with more than one
worker a caller's session would vanish between turns.
