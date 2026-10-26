"""Human-readable terminal report (plain text by default, optional ANSI)."""

from __future__ import annotations

import sys

from ..detector import SEVERITY_ORDER, AnalysisResult
from ..model import CLASS_LABELS, CLS_STABLE_PASS
from ..quarantine import QuarantineEntry
from ..util import fmt_pct, truncate

_COLOR_FOR = {
    "broken": "31",  # red
    "regression": "31",  # red
    "confirmed-flaky": "33",  # yellow
    "flaky": "33",  # yellow
    "suspected-flaky": "33",  # yellow
    "flaky-low": "33",  # yellow
    "watch": "36",  # cyan
    "fixed": "32",  # green
    "new": "36",  # cyan
    "skip-only": "2",  # dim
    "stable-pass": "2",  # dim
}

_LABEL_WIDTH = max(len(label) for label in CLASS_LABELS.values())
_TEST_WIDTH = 46


def _colorize(text: str, cls: str) -> str:
    code = _COLOR_FOR.get(cls)
    if not code:
        return text
    return f"\033[{code}m{text}\033[0m"


def render(
    result: AnalysisResult,
    *,
    db_path: str = "",
    use_color: bool = False,
    quarantine_suggestions: list[QuarantineEntry] | None = None,
) -> str:
    """Render the full report as a string (no trailing newline)."""
    lines: list[str] = []
    add = lines.append

    add("FlakeHerd report")
    add(f"  database : {db_path or '-'} ({result.run_count} runs, {result.test_count} tests)")
    add(f"  run      : {result.current_run_uid or '-'}")
    add("")

    counts = "  ".join(
        f"{CLASS_LABELS.get(cls, cls)}={result.counts[cls]}"
        for cls in SEVERITY_ORDER
        if result.counts.get(cls)
    )
    add(f"  classes  : {counts or 'no tests recorded'}")
    add("")

    notable = [
        c for c in result.classifications if c.cls != CLS_STABLE_PASS
    ]
    stable = result.counts.get(CLS_STABLE_PASS, 0)
    if not notable and stable:
        add(f"All {stable} test(s) are stable-pass. Nothing to report.")
    elif notable:
        header = (
            f"{'CLASS':<{_LABEL_WIDTH}}  {'TEST':<{_TEST_WIDTH}}  "
            f"{'N':>3}  {'FAIL%':>6}  {'SCORE':>5}  RECENT"
        )
        add(header)
        add("-" * len(header))
        for c in notable:
            label = CLASS_LABELS.get(c.cls, c.cls)
            test = truncate(c.test_key, _TEST_WIDTH)
            retry = ""
            if c.retry_attempts is not None and c.retry_attempts > 1:
                retry = f"  (retried: {c.retry_outcomes})"
            row = (
                f"{label:<{_LABEL_WIDTH}}  {test:<{_TEST_WIDTH}}  "
                f"{c.samples:>3}  {fmt_pct(c.failure_rate):>6}  {c.score:>5}  {c.recent}"
            )
            if use_color:
                row = row.replace(label, _colorize(label, c.cls), 1)
            add(row + retry)
        if stable:
            add("")
            add(f"  ({stable} stable-pass test(s) not shown)")

    gate = result.gate_failures()
    if gate:
        add("")
        add("GATE: unexplained failures in this run "
            f"({len(gate)} test(s) failed without flaky evidence):")
        for c in gate:
            marker = "!" if not use_color else _colorize("!", "31")
            add(f"  {marker} {c.test_key} [{CLASS_LABELS.get(c.cls, c.cls)}]")
        add("  These tests are NOT excused by flaky history; the job should fail.")

    flaky = result.flaky_family
    if flaky:
        add("")
        add("Quarantine candidates (proven flaky):")
        for c in flaky:
            add(f"  * {c.test_key} (score {c.score}) - {c.reason}")
        add("Run `flakeherd quarantine` to generate a skip manifest.")

    if quarantine_suggestions:
        add("")
        add("Quarantine policy (threshold enabled) - suggestions only, nothing written:")
        for e in quarantine_suggestions:
            add(f"  * {e.test_key} (score {e.score}, {e.samples} samples)")
            if e.audit_reason:
                add(f"      audit: {e.audit_reason}")
        add(
            "Suggestion mode: run `flakeherd quarantine` or pass "
            "--auto-quarantine to write the manifest (it is never committed "
            "or pushed automatically)."
        )
    return "\n".join(lines)


def should_use_color(stream=None) -> bool:
    """Color when writing to an interactive TTY and NO_COLOR is unset."""
    import os

    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if not hasattr(stream, "isatty"):
        return False
    try:
        return stream.isatty()
    except Exception:
        return False


__all__ = ["render", "should_use_color"]
