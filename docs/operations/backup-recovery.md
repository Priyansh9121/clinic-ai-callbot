# Backup and recovery

PostgreSQL is the only thing here that cannot be rebuilt. Everything else —
model weights, the Python environment, Asterisk's own packages — is
re-downloadable, and backing it up buys nothing but gigabytes.

## What is in the database

Patient records, appointments, availability, call metadata and the audit log.
Treat a dump as a health record: it contains names, dates of birth and mobile
numbers. Encrypt it at rest, restrict who can read it, and give it the same
retention period as the data itself (`CALL_DATA_RETENTION_DAYS`).

Audio and transcripts are **not** in the database — `RETAIN_AUDIO` and
`RETAIN_TRANSCRIPTS` default to false and nothing writes them elsewhere. A
dump therefore holds no recordings, which is a property worth preserving.

## Version note

Use a `pg_dump` at least as new as the server. The development cluster runs
PostgreSQL 18; Homebrew's `postgresql@17` formula ships a 17 client that
refuses with `aborting because of server version mismatch`. The `libpq` keg
carries a matching pair:

```bash
PG=/opt/homebrew/Cellar/libpq/18.4/bin
```

## Backup

```bash
PGPASSWORD=<db password> "$PG/pg_dump" \
  -h 127.0.0.1 -p 5433 -U clinic_app -d clinic_callbot \
  -Fc -f clinic-$(date +%Y%m%d-%H%M).dump
```

`-Fc` is the custom format: compressed, and restorable table-by-table. The
development database dumps to about 93 KB.

Do not put the password on the command line in anything shared — use
`PGPASSWORD` from the environment, a `.pgpass` file, or a role with peer
authentication.

## Restore

**Always restore into a new database first.** Restoring over a live one is how
a recovery drill becomes an outage.

```bash
TMPDB=clinic_restore_verify
"$PG/psql"  -h 127.0.0.1 -p 5433 -U postgres -d postgres \
            -c "CREATE DATABASE $TMPDB OWNER clinic_app"
"$PG/pg_restore" -h 127.0.0.1 -p 5433 -U postgres -d "$TMPDB" --no-owner clinic-....dump
```

`clinic_app` deliberately cannot create databases, so the create and the
restore run as the cluster superuser (`postgres`). `--no-owner` lets the dump
land under whatever role owns the target.

## Verifying a restore

A dump that restores without error is not yet a backup you can rely on. Check
that the schema and a representative row count survived:

```bash
for q in \
  "select count(*) from information_schema.tables where table_schema='public'" \
  "select count(*) from practitioners" \
  "select count(*) from availability_slots" \
  "select version_num from alembic_version" ; do
  "$PG/psql" -h 127.0.0.1 -p 5433 -U postgres -d "$TMPDB" -tAc "$q"
done
```

Compare against the same queries on the source. **Verified on the development
database:** 11 tables, 3 practitioners, 1683 availability slots and
`alembic_version = 0001_initial_schema` in both, after which the temporary
database was dropped and the development database was confirmed unchanged.

Do not print patient rows to check a restore. Counts and the schema are enough,
and they leave nothing in your terminal scrollback.

Then clean up:

```bash
"$PG/psql" -h 127.0.0.1 -p 5433 -U postgres -d postgres -c "DROP DATABASE $TMPDB"
```

## Migrations

`alembic_version` travels in the dump, so a restored database remembers which
migration it is on. Restoring an old dump into a newer deployment means running
`alembic upgrade head` afterwards. Restoring a *newer* dump into an older
release does not work and is not supported — roll the code forward first.

## What is not backed up, and why

| Not backed up | Recovery |
|---|---|
| Piper / Whisper / Silero weights (`models/`, ~922 MB) | re-download; see `docs/architecture/voice-pipeline.md` |
| Ollama models | `ollama pull <model>` |
| Python environment | `uv sync` |
| Asterisk packages | package manager |
| Redis contents | transient conversation state, TTL-bounded; a restart loses in-flight calls and nothing else |

## Secrets

`.env`, `infrastructure/asterisk/*.conf` and any TLS material are **not** in
git and must **not** be in the database dump either. Back them up separately in
whatever secret store the deployment uses, encrypted, with their own access
control. A database dump and the credentials to reach the database should never
travel together.

## Disaster recovery, in order

1. Restore PostgreSQL from the most recent dump into a fresh database.
2. Run `alembic upgrade head`.
3. Restore `.env` and `infrastructure/asterisk/*.conf` from the secret store.
4. Re-download model weights and pull the Ollama model.
5. Start the dev services, then the backend, then confirm
   `uv run python scripts/telephony_status.py` reports ARI reachable.
6. Place a test call to extension 600, then 700.

Steps 1–4 are recoverable from backups and the network. Step 6 is the only one
that needs a person.
