"""Telephony transport (Milestone 4).

A SIP call is made to look exactly like the local microphone session: the same
``VoiceSession``, VAD, endpointing, STT, ConversationEngine and TTS run
unchanged, behind the same ``AudioInput``/``AudioOutput`` protocols.

Asterisk owns all SIP. This package owns the adapter, and nothing else:

    app/telephony/
    ├── states.py            call lifecycle — the third state domain
    ├── metadata.py          caller ID, normalised and never authentication
    ├── destinations.py      the transfer allowlist
    ├── control.py           CallController protocol (signalling only)
    ├── audiosocket.py       AudioSocket wire protocol
    ├── transport.py         AudioInput/AudioOutput over a call, with pacing
    ├── server.py            media server: accept, correlate, hand off
    ├── ari.py               Asterisk REST interface client
    ├── asterisk_control.py  CallController backed by ARI
    ├── manager.py           one call = one isolated session
    ├── service.py           call setup, wiring control to media
    └── factory.py           construction from Settings
"""
