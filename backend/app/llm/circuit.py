"""A minimal circuit breaker.

Without one, a stopped Ollama costs every caller the full connection timeout on
every turn — turning a degraded feature into a degraded *call*. After a few
consecutive failures the breaker opens and subsequent turns go straight to the
deterministic analyser at zero cost, retrying periodically.

Deliberately simple: consecutive-failure count, one cooldown, one trial call on
recovery. No half-open request quotas, no rolling windows.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class CircuitBreaker:
    failure_threshold: int = 3
    recovery_seconds: float = 30.0

    _consecutive_failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)

    @property
    def is_open(self) -> bool:
        """True when calls should be skipped.

        Reading this also closes the breaker once the cooldown has elapsed, so
        the next call is a trial: success closes it fully, failure re-opens it.
        """
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.recovery_seconds:
            self._opened_at = None
            self._consecutive_failures = 0
            return False
        return True

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._opened_at = time.monotonic()

    def reset(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures
