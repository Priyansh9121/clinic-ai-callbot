# Reverse proxy — not implemented

Placeholder for the production TLS-terminating reverse proxy.

The backend already sets its own security headers (`app/security/headers.py`),
including HSTS when `APP_ENV=production`. A proxy added here must not strip or
duplicate them.

Requirements when this is built: TLS 1.2+ only, request body size limits, a
connection-level rate limit in front of the application-level one, and access
logs that exclude query strings (they can carry caller-supplied values).
