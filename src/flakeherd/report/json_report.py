"""Machine-readable JSON report for scripting and further automation."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from ..detector import AnalysisResult
from ..errors import FlakeHerdError
from ..model import TestCaseResult
from ..quarantine import QuarantineEntry
from ..util import atomic_write_bytes, utc_now_iso

SCHEMA_VERSION = 1

# Longest first-failure excerpt embedded per test in the JSON report; the
# full traceback lives in the JUnit XML artifact and the console output.
MAX_EXCERPT_IN_REPORT = 1000


def build_payload(
    result: AnalysisResult,
    *,
    window: int,
    current_run_results: dict[str, TestCaseResult] | None = None,
    quarantine_suggestions: list[QuarantineEntry] | None = None,
) -> dict:
    """Stable JSON schema (schema_version 1) covering every classification.

    ``current_run_results`` (the parsed JUnit results of the run just
    recorded, keyed by test key) adds a ``current_run`` object with retry
    evidence to each matching test entry - additive fields, so schema
    version 1 consumers that ignore unknown keys stay compatible.
    """
    tests: list[dict] = []
    for c in result.classifications:
        entry = asdict(c)
        current = (current_run_results or {}).get(c.test_key)
        if current is not None:
            current_run: dict = {}
            if current.retry_attempts is not None:
                current_run["retry_attempts"] = current.retry_attempts
            if current.retry_outcomes:
                current_run["retry_outcomes"] = current.retry_outcomes
            if current.retry_first_failure:
                excerpt = current.retry_first_failure
                if len(excerpt) > MAX_EXCERPT_IN_REPORT:
                    excerpt = excerpt[: MAX_EXCERPT_IN_REPORT - 3] + "..."
                current_run["retry_first_failure_excerpt"] = excerpt
                current_run["retry_full_trace"] = "junit-xml"
            if current_run:
                entry["current_run"] = current_run
        tests.append(entry)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generator": "flakeherd",
        "generated_at": utc_now_iso(),
        "current_run_uid": result.current_run_uid,
        "run_count": result.run_count,
        "test_count": result.test_count,
        "window": window,
        "summary": dict(sorted(result.counts.items())),
        "gate_failures": [
            {
                "test_key": c.test_key,
                "cls": c.cls,
                "reason": c.reason,
            }
            for c in result.gate_failures()
        ],
        "tests": tests,
    }
    if quarantine_suggestions is not None:
        payload["quarantine_suggestions"] = [asdict(e) for e in quarantine_suggestions]
    return payload


def write_json(path: str | Path, payload: dict) -> None:
    body = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    try:
        atomic_write_bytes(path, (body + "\n").encode("utf-8"))
    except OSError as exc:
        raise FlakeHerdError(f"cannot write JSON report {path}: {exc}") from exc


__all__ = ["SCHEMA_VERSION", "build_payload", "write_json"]
