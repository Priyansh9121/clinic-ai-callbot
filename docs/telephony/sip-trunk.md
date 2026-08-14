# SIP trunk readiness

How a real telephone number would reach this system. **Nothing here has been
exercised against a provider** — no trunk has been bought, configured or
called. This is the design and the checklist, written while the decisions are
fresh, not a report of something that works.

Local SIP (extension 700 from a softphone) is the proven path; see
[local-sip.md](../development/local-sip.md).

## The path

```
caller on the PSTN
      ↓
SIP provider  (owns the DID, does the PSTN interconnect)
      ↓  SIP/RTP over the internet
Asterisk PJSIP trunk         [transport-trunk] + [trunk]
      ↓
[from-trunk]                  inbound context — strangers, not staff
      ↓
health gate                   CURL(AI_HEALTH_URL); empty ⇒ reception
      ↓
Stasis(clinic-ai)             the application answers
      ↓  on any failure
reception                     [clinic-transfer] extension 100
```

The security property worth stating: **`[from-trunk]` is not `[ai-internal]`.**
An inbound caller can reach the clinic AI and reception and nothing else.
Pointing the trunk at the internal context would let anyone who dials the
number reach every extension a staff softphone can.

## Template

`infrastructure/asterisk/pjsip-trunk.conf.example`, with placeholders for the
provider host, auth username and password, the DID, the bind address and the
public/private addresses for NAT. Populated `*.conf` files are gitignored and a
test pins that they cannot be committed.

## Codecs

**G.711 only — `ulaw` and `alaw`.** Not a bandwidth decision. The media path
resamples to 16 kHz for Whisper, and a compressed codec spends transcription
accuracy to save bandwidth on a link carrying one call at a time. Accuracy here
decides whether "numb" is heard as "numb" or "down", which the STT benchmark
already showed is the difference between catching a stroke symptom and missing
it.

## Ports and firewall

| Port | Protocol | Direction | Notes |
|---|---|---|---|
| 5060 | UDP/TCP | inbound from provider **only** | source-restrict to the provider's signalling IPs |
| 5061 | TCP | inbound from provider | TLS, if used |
| 10000–20000 | UDP | inbound from provider | RTP; set `rtp.conf` to match and open no more |
| 8088 | TCP | **private only** | ARI. Never internet-facing |
| 8090 | TCP | **private only** | AudioSocket. Unauthenticated — see below |

An exposed SIP port is found by scanners within minutes and brute-forced for
toll fraud. Restrict by source address; `type=identify` on the trunk rejects
signalling from anywhere else, which is the second layer.

**AudioSocket has no authentication of any kind.** Anything that can reach
:8090 can inject audio into a live call. It belongs on the private network
between the PBX and the application, and nowhere else.

## NAT

If the PBX is behind NAT, set `external_signaling_address`,
`external_media_address` and `local_net` on the transport. Without them
Asterisk advertises its private address in SDP, the provider sends media to an
unroutable address, and the call connects with no audio — the same symptom as a
wrong `TELEPHONY_MEDIA_ADVERTISE_HOST`, from the other side.

## Caller ID

`trust_id_inbound=no`: the presented number is unauthenticated and must not be
treated as identity. It is not used to look up a patient — the conversation
identifies callers by surname and date of birth regardless of what the trunk
says. The number is salted and hashed before it is stored
(`TELEPHONY_CALLER_SALT`) and never logged raw.

## TLS and SRTP

Signalling over TLS is a transport option and is straightforward. **Media
encryption (SRTP) is not implemented or tested here**, and TLS on signalling
does not encrypt audio. For a service carrying health information over the
public internet, plan for both, and note that SRTP interacts with the
AudioSocket path — that leg is plain TCP on the private network and its
security is the network boundary, not encryption.

## Failover

Every failure routes to reception rather than to silence, and that ordering is
already implemented:

1. Health gate fails (backend down or unreachable) → reception, AI never
   consulted.
2. Stasis app not registered → the call returns from `Stasis()` → reception.
3. Application at capacity → declines to answer → dialplan failover.
4. Call fails mid-conversation → `HangupCause.FAILURE`, channel released.

The one thing to verify on a real trunk that local SIP cannot prove is what the
*provider* does when Asterisk is unreachable entirely. Most offer a failover
number; point it at the clinic's existing line.

## Before enabling

- [ ] Provider chosen, DID allocated, credentials in a secret store — not git
- [ ] `type=identify` restricted to the provider's signalling addresses
- [ ] Firewall: 5060/5061 and the RTP range source-restricted; 8088 and 8090 private
- [ ] `rtp.conf` range matches the firewall
- [ ] NAT addresses set if applicable
- [ ] Provider-side failover number configured
- [ ] `[from-trunk]` confirmed **not** to reach `[ai-internal]`
- [ ] Call recording confirmed off at the provider as well as in Asterisk
- [ ] Emergency calls: confirm what the provider does with `000`/`911`. **This
      system must never be the path to emergency services** — the safety engine
      tells callers to hang up and dial directly, and that assumption needs to
      hold at the trunk too.
