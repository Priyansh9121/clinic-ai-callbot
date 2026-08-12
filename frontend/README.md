# Admin dashboard — not implemented

Placeholder for the React + TypeScript + Vite staff dashboard (Milestone 5).

Nothing has been built here yet. The directory exists so the intended structure
is visible, not to imply a partial implementation.

Planned views (see the project brief for detail):

- **Dashboard** — active calls, outcomes, transfer rate, booking completion rate
- **Practitioners / Appointment types / Clinic information** — management
- **Calls** — minimal structured metadata only; no transcripts, no patient detail
- **Safety review** — emergency triggers, low-confidence turns, failed workflows
- **System health** — database, Redis, LLM, STT, TTS, telephony

The backend already provides what the first and last of these need:
`GET /health` reports per-component status and `GET /health/metrics` exposes
counters and latency histograms with no patient data in them.

Authentication will use the existing RBAC boundary (`app/security/auth.py`).
The development API-key mechanism must be replaced with a real identity
provider before this ships.
