# Asterisk configuration templates

Seven `*.conf.example` templates. Copy each to `*.conf` and substitute the
`${...}` placeholders — [`docs/development/local-sip.md`](../../docs/development/local-sip.md)
is the authority for what each one means and carries the validated values.

```bash
cd infrastructure/asterisk
for f in *.conf.example; do cp "$f" "${f%.example}"; done
```

| Template | What it configures |
|---|---|
| `pjsip.conf.example` | transport, softphone `1001`, `reception`, commented trunk |
| `extensions.conf.example` | `[ai-internal]` (600 proof-of-life, 700 the AI), `[clinic-ai-entry]` health gate, `[clinic-transfer]` |
| `ari.conf.example` | the `[ai-callbot]` ARI user |
| `http.conf.example` | Asterisk's HTTP server, which serves ARI |
| `modules.conf.example` | AudioSocket + ARI modules, and three deliberate `noload`s |
| `rtp.conf.example` | RTP port range |
| `pjsip-trunk.conf.example` | Phase B SIP trunk — not enabled, never exercised against a provider |

The populated `*.conf` files are **gitignored**: they hold real SIP and ARI
passwords, and `*.conf` does not match `*.conf.example`, so the templates stay
tracked. `tests/telephony/test_dialplan_templates.py` pins that, and also
checks the templates still agree with each other and with the application
defaults.

Two rules for anyone editing these:

1. **No credentials in this repository.** Every secret is a `${...}` placeholder
   here and a `CHANGE_ME` in `.env.example`. Generate them —
   `openssl rand -base64 24` — do not choose them.
2. **The dialplan health-checks the AI before routing to it**, and treats any
   uncertainty — non-200, malformed response, timeout — as failure, falling back
   to reception and then to voicemail. AI failure must never make the clinic
   unreachable.

Two values must agree with the backend's `.env`, and both have cost real
debugging time:

| Here | `.env` | Symptom when they disagree |
|---|---|---|
| `ari.conf` **section name** | `ASTERISK_ARI_USER` | `401 Authentication required` — indistinguishable from a wrong password |
| `Stasis(...)` argument | `ASTERISK_APP_NAME` | the call reaches Stasis, nothing is listening, the caller falls through to reception |

`docs/telephony/design.md` is the pre-implementation design note, kept for the
reasoning behind the call path and the failure fallback. It predates these
templates; where the two differ, the templates are what runs.
