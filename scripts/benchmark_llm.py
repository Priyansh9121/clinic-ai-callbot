#!/usr/bin/env python
"""Measure the local LLM analyser against the deterministic baseline.

    uv run python scripts/benchmark_llm.py
    uv run python scripts/benchmark_llm.py --model llama3.2:3b-instruct-q4_K_M
    uv run python scripts/benchmark_llm.py --compare qwen2.5:1.5b-instruct llama3.2:3b-instruct-q4_K_M
    uv run python scripts/benchmark_llm.py --baseline-only

Both analysers run over the same labelled corpus
(``backend/tests/fixtures/nlu_corpus.py``), so the comparison is like for like.
Hybrid routing is disabled during the run: the point is to measure the model,
not how often it is skipped.

Every number printed is measured. Nothing here is estimated.

The corpus is synthetic. No caller text is used, stored or printed.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.conversation.nlu.baseline import (  # noqa: E402
    AppointmentTypeTerm,
    BaselineAnalyser,
    PractitionerTerm,
    Vocabulary,
)
from app.conversation.nlu.llm_analyser import LLMBackedAnalyser  # noqa: E402
from app.conversation.states import Intent  # noqa: E402
from app.llm.errors import failure_reason  # noqa: E402
from app.llm.ollama import OllamaLLMProvider  # noqa: E402
from tests.fixtures.nlu_corpus import (  # noqa: E402
    ALL_CASES,
    ENTITY_CASES,
    INTENT_CASES,
    Case,
)

BOLD, DIM, GREEN, RED, YELLOW, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m"
)

TODAY = date(2026, 8, 12)  # a Wednesday; fixed so weekday arithmetic is stable

PATEL, WILSON, CHEN = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
STANDARD, LONG, NEW_PATIENT, TELEHEALTH = (uuid.uuid4() for _ in range(4))

VOCABULARY = Vocabulary(
    practitioners=(
        PractitionerTerm(PATEL, "Dr Sarah Patel",
                         ("dr sarah patel", "doctor patel", "sarah patel", "dr patel", "patel")),
        PractitionerTerm(WILSON, "Dr James Wilson",
                         ("dr james wilson", "doctor wilson", "james wilson", "dr wilson",
                          "wilson")),
        PractitionerTerm(CHEN, "Dr Emily Chen",
                         ("dr emily chen", "doctor chen", "emily chen", "dr chen", "chen")),
    ),
    appointment_types=(
        AppointmentTypeTerm(STANDARD, "standard_gp", "Standard GP",
                            ("standard appointment", "standard gp", "standard", "normal")),
        AppointmentTypeTerm(LONG, "long_gp", "Long GP",
                            ("long appointment", "longer appointment", "long gp", "long")),
        AppointmentTypeTerm(NEW_PATIENT, "new_patient", "New Patient",
                            ("new patient", "first appointment")),
        AppointmentTypeTerm(TELEHEALTH, "telehealth", "Telehealth",
                            ("phone appointment", "telehealth", "over the phone", "phone consult")),
    ),
)
NAME_BY_ID = {PATEL: "Dr Sarah Patel", WILSON: "Dr James Wilson", CHEN: "Dr Emily Chen"}
TYPE_BY_ID = {STANDARD: "Standard GP", LONG: "Long GP", NEW_PATIENT: "New Patient",
              TELEHEALTH: "Telehealth"}


@dataclass
class EntityScore:
    correct: int = 0
    total: int = 0

    def record(self, ok: bool) -> None:
        self.total += 1
        self.correct += int(ok)

    @property
    def pct(self) -> float:
        return 100.0 * self.correct / self.total if self.total else 0.0


@dataclass
class Result:
    label: str
    intent_correct: int = 0
    intent_total: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    failures: dict[str, int] = field(default_factory=dict)
    entity: dict[str, EntityScore] = field(default_factory=dict)
    wrong: list[tuple[str, str, str]] = field(default_factory=list)
    #: (entity name, utterance, expected, actual) — so an entity regression can
    #: be diagnosed from the report rather than re-derived by hand.
    entity_misses: list[tuple[str, str, str, str]] = field(default_factory=list)

    @property
    def intent_pct(self) -> float:
        return 100.0 * self.intent_correct / self.intent_total if self.intent_total else 0.0

    def score(
        self,
        name: str,
        ok: bool,
        *,
        text: str = "",
        expected: object = "",
        actual: object = "",
    ) -> None:
        self.entity.setdefault(name, EntityScore()).record(ok)
        if not ok:
            self.entity_misses.append((name, text, str(expected), str(actual)))


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(round((pct / 100.0) * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[index]


def _check_entities(case: Case, analysis, result: Result) -> None:
    """Score only the entities this case actually asserts."""
    entities = analysis.entities

    def check(name: str, expected: object, actual: object) -> None:
        result.score(name, expected == actual, text=case.text, expected=expected, actual=actual)

    if case.practitioner:
        check("practitioner", case.practitioner, NAME_BY_ID.get(entities.practitioner_id))
    if case.appointment_type:
        check("appointment_type", case.appointment_type,
              TYPE_BY_ID.get(entities.appointment_type_id))
    if case.date_offset_days is not None:
        check("date", TODAY + timedelta(days=case.date_offset_days), entities.preferred_date)
    if case.period:
        check("period", case.period, entities.preferred_period.value)
    if case.patient_type:
        check("patient_type", case.patient_type, entities.patient_type)
    if case.selection_ordinal is not None:
        check("selection", case.selection_ordinal, entities.selection_ordinal)

    # Hallucination cases: the correct outcome is *no* entity.
    if "hallucination" in case.tags:
        check("rejects_unknown", None, entities.practitioner_id)


async def run_analyser(analyser, label: str, *, cases: list[Case]) -> Result:
    result = Result(label=label)
    intent_ids = {id(c) for c in INTENT_CASES}
    entity_ids = {id(c) for c in ENTITY_CASES}

    for case in cases:
        started = time.perf_counter()
        analysis = await analyser.analyse(
            case.text, vocabulary=VOCABULARY, today=TODAY, state_hint=case.state
        )
        result.latencies_ms.append((time.perf_counter() - started) * 1000)

        if id(case) in intent_ids:
            result.intent_total += 1
            if analysis.intent is case.intent:
                result.intent_correct += 1
            else:
                result.wrong.append((case.text, case.intent.value, analysis.intent.value))

        if id(case) in entity_ids or "hallucination" in case.tags:
            _check_entities(case, analysis, result)

    return result


def print_result(result: Result, *, baseline: Result | None = None) -> None:
    print(f"\n{BOLD}{result.label}{RESET}")
    delta = ""
    if baseline is not None:
        diff = result.intent_pct - baseline.intent_pct
        colour = GREEN if diff > 0 else (RED if diff < 0 else DIM)
        delta = f"  {colour}({diff:+.1f} pts vs baseline){RESET}"
    print(f"  intent accuracy : {result.intent_pct:5.1f}%  "
          f"({result.intent_correct}/{result.intent_total}){delta}")

    if result.entity:
        print("  entities:")
        for name in sorted(result.entity):
            score = result.entity[name]
            line = f"    {name:<16} {score.pct:5.1f}%  ({score.correct}/{score.total})"
            if baseline is not None and name in baseline.entity:
                diff = score.pct - baseline.entity[name].pct
                colour = GREEN if diff > 0 else (RED if diff < 0 else DIM)
                line += f"  {colour}{diff:+.1f}{RESET}"
            print(line)

    if result.latencies_ms:
        values = result.latencies_ms
        print(f"  latency         : median {statistics.median(values):7.1f} ms | "
              f"p95 {_percentile(values, 95):7.1f} ms | max {max(values):7.1f} ms")

    if result.failures:
        total = sum(result.failures.values())
        rate = 100.0 * total / len(result.latencies_ms) if result.latencies_ms else 0.0
        detail = ", ".join(f"{k}={v}" for k, v in sorted(result.failures.items()))
        print(f"  fallbacks       : {total} ({rate:.1f}%)  [{detail}]")


def print_misses(result: Result, limit: int = 12) -> None:
    if result.wrong:
        print(f"\n{DIM}  misclassified ({len(result.wrong)}), first {limit}:{RESET}")
        for text, expected, got in result.wrong[:limit]:
            print(f"{DIM}    {text[:58]:<58} {expected} -> {got}{RESET}")
    if result.entity_misses:
        print(f"\n{DIM}  entity misses ({len(result.entity_misses)}):{RESET}")
        for name, text, expected, got in result.entity_misses[:limit]:
            print(f"{DIM}    {name:<16} {text[:40]:<40} {expected} -> {got}{RESET}")


class _CountingProvider:
    """Wraps a provider to count failures by reason for the report."""

    def __init__(self, inner, result: Result) -> None:
        self._inner = inner
        self._result = result

    async def analyse_message(self, message, **kwargs):  # noqa: ANN001, ANN003
        try:
            return await self._inner.analyse_message(message, **kwargs)
        except Exception as exc:
            reason = failure_reason(exc)
            self._result.failures[reason] = self._result.failures.get(reason, 0) + 1
            raise

    async def health(self) -> bool:
        return await self._inner.health()


async def benchmark_model(model: str, args) -> Result | None:  # noqa: ANN001
    provider = OllamaLLMProvider(
        base_url=args.base_url,
        model=model,
        timeout_seconds=args.timeout,
        max_concurrency=1,
        # The circuit breaker is a production feature; leaving it on made a
        # model that emitted a few malformed responses look *accurate*,
        # because the breaker opened and the rest of the corpus was silently
        # answered by the baseline. Every case must reach the model here.
        failure_threshold=10**9,
    )
    if not await provider.health():
        print(f"{RED}model {model!r} is not available on {args.base_url}{RESET}")
        await provider.aclose()
        return None

    print(f"{DIM}warming {model}…{RESET}")
    cold_started = time.perf_counter()
    await provider.warmup()
    cold_ms = (time.perf_counter() - cold_started) * 1000

    result = Result(label=f"LLM  ({model})")
    analyser = LLMBackedAnalyser(
        provider=_CountingProvider(provider, result),
        baseline=BaselineAnalyser(),
        min_confidence=args.min_confidence,
        # Off: measure the model on every case, not how often it is skipped.
        hybrid_routing=False,
        provider_name="ollama",
        model_name=model,
    )
    measured = await run_analyser(analyser, result.label, cases=ALL_CASES)
    measured.failures = result.failures
    measured.label = result.label
    print(f"{DIM}  warm-up (cold first call): {cold_ms:.0f} ms{RESET}")
    await provider.aclose()
    return measured


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default=None, help="single model to benchmark")
    parser.add_argument("--compare", nargs="*", default=None, help="several models")
    parser.add_argument("--timeout", type=float, default=20.0,
                        help="generous for benchmarking; production uses LLM_TIMEOUT_SECONDS")
    parser.add_argument("--min-confidence", type=float, default=0.6)
    parser.add_argument("--baseline-only", action="store_true")
    args = parser.parse_args()

    print(f"{BOLD}NLU benchmark{RESET}")
    print(f"{DIM}corpus: {len(ALL_CASES)} cases "
          f"({len(INTENT_CASES)} intent-labelled, {len(ENTITY_CASES)} entity-labelled)")
    print(f"reference date: {TODAY} (Wednesday) · synthetic utterances only{RESET}")

    baseline = await run_analyser(BaselineAnalyser(), "BASELINE (deterministic)",
                                  cases=ALL_CASES)
    print_result(baseline)
    print_misses(baseline)

    if args.baseline_only:
        return 0

    models = args.compare or ([args.model] if args.model else ["qwen2.5:1.5b-instruct"])
    results: list[Result] = []
    for model in models:
        measured = await benchmark_model(model, args)
        if measured is not None:
            print_result(measured, baseline=baseline)
            print_misses(measured)
            results.append(measured)

    if results:
        print(f"\n{BOLD}Summary{RESET}")
        print(f"  {'analyser':<40} {'intent':>8} {'median':>10} {'p95':>10}")
        for row in [baseline, *results]:
            print(f"  {row.label:<40} {row.intent_pct:7.1f}% "
                  f"{statistics.median(row.latencies_ms):9.1f}ms "
                  f"{_percentile(row.latencies_ms, 95):9.1f}ms")
        best = max(results, key=lambda r: r.intent_pct)
        if best.intent_pct <= baseline.intent_pct:
            print(f"\n{YELLOW}  No model beat the deterministic baseline on this corpus.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
