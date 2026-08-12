# PostgreSQL

## Development

Two interchangeable options, both on port **5433** so `.env` works either way:

| | Command |
|---|---|
| Docker | `docker compose up -d postgres` |
| No Docker (macOS/Homebrew) | `./scripts/dev_services.sh init` |

`dev_services.sh` creates an isolated cluster under `.devdata/` (gitignored),
owned by your user and bound to `127.0.0.1`. Any PostgreSQL already installed
on the machine is left untouched.

## Roles and least privilege

| Role | Purpose | Privileges |
|---|---|---|
| `postgres` | cluster owner, migrations bootstrap | superuser |
| `clinic_app` | the application | owns `public`; **no** CREATEDB / CREATEROLE / SUPERUSER |

The application never connects as a superuser. It also has no ability to create
databases, which is why `scripts/dev_services.sh` creates the test database
rather than the test suite doing it.

## Production notes

Not covered by this repository — these are deployment decisions that need to be
made with the clinic's IT governance:

- `scram-sha-256` authentication and TLS-only connections (`hostssl` in `pg_hba.conf`).
- Encryption at rest (full-disk or volume level).
- Point-in-time recovery, with restores actually tested.
- A separate read-only role for reporting; the application role is not it.
- Retention and deletion jobs for `calls`, `call_events` and `audit_events`
  (see `docs/privacy/data-inventory.md`).
