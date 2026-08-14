# Operations runbook

Startup, shutdown and the admin-interface decision. Telephony-specific setup is
in [local-sip.md](../development/local-sip.md); backups are in
[backup-recovery.md](backup-recovery.md).

## Startup

Order matters: each layer needs the one below it.

```bash
# 1. Data stores
./scripts/dev_services.sh start          # PostgreSQL :5433, Redis :6380

# 2. Local models
ollama list                              # the configured LLM_MODEL must be present

# 3. Check configuration before starting anything
uv run python scripts/telephony_status.py

# 4. Backend — bind an address the PBX can reach
cd backend && uv run uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
```

`APP_HOST`/`APP_PORT` are **not** read by the uvicorn CLI; pass `--host`
explicitly. Loopback is unreachable from the Asterisk VM, which makes the
dialplan's health check fail and sends every caller to reception.

### What a good startup looks like

```
session_store.backend        backend=redis
app.startup                  environment=development llm_provider=ollama
telephony.media_listening    host=0.0.0.0 port=8090 wire_format=slin
telephony.started            media_bind=0.0.0.0:8090 media_advertised=192.168.65.1:8090
                             stasis_app=clinic-ai max_calls=2
```

Then:

```bash
curl -s localhost:8000/health/live          # {"status":"ok"}
curl -s localhost:8000/health | python3 -m json.tool
```

`telephony.ari_disconnected error=InvalidStatus` repeating means the ARI
credential is rejected. The rest of the application is unaffected — telephony
reports down and the dialplan routes callers to reception.

## Shutdown

`Ctrl-C`, or `SIGTERM` to the uvicorn process. The FastAPI lifespan stops
telephony, which cancels the ARI reconnect loop, closes the media server and
releases the channels.

```
Shutting down
Waiting for application shutdown.
app.shutdown
Application shutdown complete.
Finished server process
```

Then services, then the VM. Backend first, so calls end cleanly.

## Verified restart drill

Run on the current HEAD, twice in succession:

| Step | Result |
|---|---|
| Ports 8000 / 8090 free before start | PASS |
| PostgreSQL, Redis, Ollama reachable | PASS |
| Backend starts, `app.startup` logged | PASS |
| `telephony.media_listening` on `0.0.0.0:8090` | PASS |
| `telephony.started` with bind and advertised addresses | PASS |
| `/health/live` returns `{"status":"ok"}` | PASS |
| Ports 8000 and 8090 both listening | PASS |
| `SIGTERM` → `app.shutdown` → `Application shutdown complete` | PASS |
| Port 8000 released | PASS |
| Port 8090 released | PASS |
| No zombie process | PASS |
| Second start/stop cycle identical | PASS |

ARI authentication fails throughout because the credential is still a
placeholder. That is the expected state until the PBX is configured, and it does
not affect any of the above — which is the point: the process starts, serves and
stops cleanly whether or not the PBX is reachable.

`llm.warmup_failed error=LLMTimeoutError` may appear on a cold Ollama. The
warmup is best-effort and the first real turn pays the load instead.

## Administration: how a clinic changes things today

**Decision: a web admin UI is POST-MVP.** Recorded here rather than left
implicit.

The data a clinic changes lives in PostgreSQL, and there is no UI for it. The
question is whether that is acceptable for a first deployment, and the answer
depends on how often each thing changes:

| Data | Changes | How, today |
|---|---|---|
| Practitioners | rarely — hiring, departure | SQL, or extend `scripts/seed_dev_data.py` |
| Appointment types | rarely | SQL — set `active = false` to retire |
| Locations | almost never | SQL |
| Availability slots | **often — weekly rosters** | seed script or SQL |
| Clinic public wording | occasionally, with sign-off | edit `CLINIC_KNOWLEDGE_FILE`, redeploy |
| Greeting, safety wording | rarely, needs review | code change |

Only one row of that table is a real problem. Practitioners and appointment
types change a few times a year, and a UI for them would be ceremony. **Slot
generation is different**: a clinic cannot be expected to write SQL every week
to publish next week's roster.

So the honest position is that this system is deployable for a pilot where
someone technical maintains the slot grid, and is **not** yet self-service for a
clinic. The smallest thing that would change that is a roster import or a
recurring-availability generator — not a CRM, not a dashboard. That is the next
piece of operational work, and it is deliberately not a full admin portal.

Until then, staff-facing reads and writes go through the authenticated
appointments API (`require_role(Role.STAFF)`), and operational visibility comes
from `/health` and `/health/metrics`, neither of which exposes patient data.
