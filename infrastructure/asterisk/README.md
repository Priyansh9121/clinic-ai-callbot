# Asterisk — not implemented (Milestone 4)

No configuration exists yet. See `docs/telephony/design.md` for the intended
call path, the process-isolation requirement and the AI-failure fallback.

When this is built it will contain `pjsip.conf`, `extensions.conf` and ARI
configuration. Two rules for whoever writes them:

1. **No credentials in this repository.** Trunk and ARI credentials come from
   the deployment's secret store; `.env.example` carries `CHANGE_ME`
   placeholders.
2. **The dialplan health-checks the AI before routing to it**, and treats any
   uncertainty — non-200, malformed response, timeout — as failure, falling
   back to reception and then to voicemail. AI failure must never make the
   clinic unreachable.
