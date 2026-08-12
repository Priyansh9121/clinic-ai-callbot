# Monitoring — not implemented

Metrics are currently in-process and exposed at `GET /health/metrics` as JSON:
counters, gauges and latency histograms, with label values restricted to closed
sets so no patient data can reach them.

A Prometheus exporter can be layered on without changing any call site — metric
names are constants on `app.core.metrics.M`, including the ones reserved for
speech and telephony.

Alerts worth defining when this is built: health endpoint failing (this is what
telephony routes on), emergency trigger rate, human transfer rate, booking
failure rate, and p95 conversation turn latency.
