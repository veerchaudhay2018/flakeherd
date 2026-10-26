"""GitHub Actions integration: workflow-command annotations + step summary.

Annotations are plain ``::level ...::message`` commands printed to stdout -
no API calls, no tokens. A failed test with a known ``file``/``line`` gets a
clickable annotation in the GitHub UI at the exact source location.

GitHub enforces per-step annotation caps (10 errors, 10 warnings, 5
notices); FlakeHerd respects those caps and orders annotations so the most
actionable findings (broken/regression) appear first.
"""

from __future__ import annotations

import os
import sys

from ..detector import SEVERITY_ORDER, AnalysisResult
from ..model import CLASS_LABELS
from ..util import truncate

# GitHub hard caps per step.
_ANNOTATION_CAPS = {"error": 10, "warning": 10, "notice": 5}

_LEVEL_FOR = {
    "broken": "error",
    "regression": "error",
    "confirmed-flaky": "warning",
    "flaky": "warning",
    "suspected-flaky": "warning",
    "flaky-low": "warning",
    "watch": "warning",
    "fixed": "notice",
    "new": "notice",
}


def _esc_property(value: str) -> str:
    """Escape a workflow-command property value (title, file, ...)."""
    value = value.replace("%", "%25")
    value = value.replace("\r", "%0D").replace("\n", "%0A")
    value = value.replace(":", "%3A").replace(",", "%2C")
    return value


def _esc_message(value: str) -> str:
    value = value.replace("%", "%25")
    value = value.replace("\r", "%0D").replace("\n", "%0A")
    return value


def format_annotation(
    level: str,
    message: str,
    *,
    title: str,
    file: str | None = None,
    line: int | None = None,
) -> str:
    """Build one ``::level title=...,file=...::message`` command string."""
    props = [f"title={_esc_property(title)}"]
    if file:
        props.append(f"file={_esc_property(file)}")
    if line is not None and line > 0:
        props.append(f"line={line}")
    return f"::{level} {', '.join(props)}::{_esc_message(message)}"


def annotations(result: AnalysisResult, max_annotations: int = 10) -> list[str]:
    """Render annotation commands, most actionable first, capped per level."""
    max_annotations = max(0, min(max_annotations, 10))
    emitted: dict[str, list[str]] = {"error": [], "warning": [], "notice": []}
    order = {cls: i for i, cls in enumerate(SEVERITY_ORDER)}
    candidates = sorted(
        (c for c in result.classifications if c.cls in _LEVEL_FOR and c.in_current_run),
        key=lambda c: order.get(c.cls, 99),
    )
    for c in candidates:
        level = _LEVEL_FOR[c.cls]
        cap = min(_ANNOTATION_CAPS[level], max_annotations)
        bucket = emitted[level]
        if len(bucket) >= cap:
            continue
        title = f"FlakeHerd: {CLASS_LABELS.get(c.cls, c.cls)} test"
        detail = (
            f"{c.test_key}: {c.reason} "
            f"(score {c.score}, {c.fail_count}/{c.samples} recent failure rate "
            f"{c.failure_rate * 100:.1f}%)"
        )
        bucket.append(
            format_annotation(level, detail, title=title, file=c.file, line=c.line)
        )
    return emitted["error"] + emitted["warning"] + emitted["notice"]


def emit_annotations(
    result: AnalysisResult,
    max_annotations: int = 10,
    stream=None,
) -> int:
    """Print annotations to *stream* (default stdout); returns count."""
    stream = stream or sys.stdout
    lines = annotations(result, max_annotations)
    for line in lines:
        stream.write(line + "\n")
    return len(lines)


def summary_markdown(result: AnalysisResult, *, max_rows: int = 50) -> str:
    """Markdown block for ``$GITHUB_STEP_SUMMARY``."""
    lines: list[str] = ["## FlakeHerd: flaky-test report", ""]
    lines.append(f"Runs analyzed: **{result.run_count}** - tests: **{result.test_count}**")
    lines.append("")
    counts = [
        (CLASS_LABELS.get(cls, cls), n)
        for cls in SEVERITY_ORDER
        if (n := result.counts.get(cls))
    ]
    if counts:
        lines.append("| Class | Tests |")
        lines.append("| --- | --- |")
        for label, n in counts:
            lines.append(f"| {label} | {n} |")
        lines.append("")
    notable = [c for c in result.classifications if c.cls != "stable-pass"][:max_rows]
    if notable:
        lines.append("| Class | Test | Recent | Score |")
        lines.append("| --- | --- | --- | --- |")
        for c in notable:
            test = truncate(c.test_key, 80)
            label = CLASS_LABELS.get(c.cls, c.cls)
            lines.append(f"| {label} | `{test}` | {c.recent} | {c.score} |")
        lines.append("")
    gate = result.gate_failures()
    if gate:
        lines.append("### Gate: unexplained failures")
        lines.append("")
        for c in gate:
            lines.append(f"- `{c.test_key}` - {c.reason}")
        lines.append("")
    flaky = result.flaky_family
    if flaky:
        lines.append("Quarantine candidates (proven flaky) - run `flakeherd quarantine`.")
    return "\n".join(lines)


def append_step_summary(
    markdown: str,
    env: dict | None = None,
) -> bool:
    """Append *markdown* to ``$GITHUB_STEP_SUMMARY`` if that file is set."""
    env = env if env is not None else os.environ
    path = env.get("GITHUB_STEP_SUMMARY")
    if not path:
        return False
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(markdown.rstrip() + "\n\n")
    return True


def in_github_actions(env: dict | None = None) -> bool:
    env = env if env is not None else os.environ
    return env.get("GITHUB_ACTIONS") == "true"


__all__ = [
    "annotations",
    "append_step_summary",
    "emit_annotations",
    "format_annotation",
    "in_github_actions",
    "summary_markdown",
]
