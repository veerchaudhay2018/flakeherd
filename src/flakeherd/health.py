"""Test Health metric: one honest number for the state of the herd.

The health score is a **heuristic indicator** (0-100) computed from the
classification counts in a ``flakeherd record --json-out`` report. It is
NOT a statistically valid probability, a reliability percentage, or a
prediction - it is a transparent, weighted blend of how many tests sit in
each classification, designed for at-a-glance tracking over time.

Formula (``metric_version`` 1)
------------------------------

Each *scored* test contributes a fixed weight:

===============  =====
class            weight
===============  =====
stable-pass      100
fixed            100
watch            50
suspected-flaky  25
flaky-low        25
confirmed-flaky  0
flaky            0
regression       0
broken           0
===============  =====

``new`` and ``skip-only`` tests carry no verdict yet and are excluded from
both the numerator and the denominator.

::

    health = round(100 * sum(weights) / scored_tests)

State:

* ``ok`` - at least one scored test and at least ``min_runs`` (default 5)
  recorded runs: the score is meaningful enough to display.
* ``insufficient-data`` - an empty history or fewer runs than the minimum:
  the score is ``None`` and the badge says so rather than inventing a
  number from a couple of observations.

Everything here is pure standard library; the SVG badge is rendered locally
(no external badge service, no invented public URL) and includes
``<title>``/``<desc>``/``aria-label`` for accessibility.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .errors import FlakeHerdError
from .util import atomic_write_bytes, utc_now_iso

METRIC_VERSION = 1
DEFAULT_MIN_RUNS = 5

# class -> per-test weight (see module docstring). Classes absent from the
# map (new, skip-only) are excluded from scoring.
_WEIGHTS: dict[str, int] = {
    "stable-pass": 100,
    "fixed": 100,
    "watch": 50,
    "suspected-flaky": 25,
    "flaky-low": 25,
    "confirmed-flaky": 0,
    "flaky": 0,
    "regression": 0,
    "broken": 0,
}

STATE_OK = "ok"
STATE_INSUFFICIENT = "insufficient-data"

_HEALTH_NOTE = (
    "Heuristic indicator computed from classification counts; not a "
    "statistically valid probability or a literal reliability percentage."
)


@dataclass(frozen=True)
class HealthReport:
    """Everything consumers need, in one serializable bundle."""

    state: str  # "ok" | "insufficient-data"
    health_score: int | None  # None when insufficient data
    scored_tests: int
    total_tests: int
    runs: int
    min_runs_required: int
    metric_version: int
    generated_at: str
    distribution: dict[str, int]
    source_report: str = ""

    def to_payload(self) -> dict:
        return {
            "metric_version": self.metric_version,
            "generated_at": self.generated_at,
            "state": self.state,
            "health_score": self.health_score,
            "sample_size": {
                "runs": self.runs,
                "tests_total": self.total_tests,
                "tests_scored": self.scored_tests,
            },
            "min_runs_required": self.min_runs_required,
            "distribution": self.distribution,
            "source_report": self.source_report,
            "notes": _HEALTH_NOTE,
        }


def compute_health(payload: dict, *, min_runs: int = DEFAULT_MIN_RUNS) -> HealthReport:
    """Compute the health metric from a ``record --json-out`` payload."""
    if min_runs < 1:
        raise FlakeHerdError("min_runs must be >= 1")
    if not isinstance(payload, dict) or "summary" not in payload or "run_count" not in payload:
        raise FlakeHerdError(
            "report payload does not look like a flakeherd JSON report "
            "(expected 'summary' and 'run_count' keys)"
        )
    distribution = {
        str(k): int(v) for k, v in payload.get("summary", {}).items() if isinstance(v, int)
    }
    total_tests = int(payload.get("test_count") or 0)
    runs = int(payload.get("run_count") or 0)
    scored = sum(n for cls, n in distribution.items() if cls in _WEIGHTS)
    weighted = sum(_WEIGHTS[cls] * n for cls, n in distribution.items() if cls in _WEIGHTS)
    if scored > 0 and runs >= min_runs:
        state = STATE_OK
        score: int | None = round(weighted / scored)
    else:
        state = STATE_INSUFFICIENT
        score = None
    return HealthReport(
        state=state,
        health_score=score,
        scored_tests=scored,
        total_tests=total_tests,
        runs=runs,
        min_runs_required=min_runs,
        metric_version=METRIC_VERSION,
        generated_at=utc_now_iso(),
        distribution=dict(sorted(distribution.items())),
    )


def load_report_payload(path: str | Path) -> dict:
    """Load and sanity-check a ``record --json-out`` report file."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FlakeHerdError(f"report not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise FlakeHerdError(f"cannot read report {path}: {exc}") from exc
    return raw


# ---------------------------------------------------------------------------
# SVG badge (rendered locally - no external service)
# ---------------------------------------------------------------------------

# Badge palette: solid fills with white text, contrast >= 4.5:1.
_BADGE_COLORS = {
    "green": "#1a7f37",  # score >= 80
    "yellow": "#9a6700",  # score >= 60
    "orange": "#bc4c00",  # score >= 40
    "red": "#cb2431",  # score < 40
    "gray": "#6e7781",  # insufficient data
}
_BADGE_TEXT = {
    "green": "healthy",
    "yellow": "fair",
    "orange": "degraded",
    "red": "at risk",
    "gray": "no data",
}

_BADGE_HEIGHT = 20
_CHAR_WIDTH = 6.5  # estimate for 11px Verdana/Helvetica/dejaVu sans
_LABEL_PAD = 5
_VALUE_PAD = 9


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _band(score: int | None) -> str:
    if score is None:
        return "gray"
    if score >= 80:
        return "green"
    if score >= 60:
        return "yellow"
    if score >= 40:
        return "orange"
    return "red"


def render_badge_svg(
    health: HealthReport,
    *,
    label: str = "tests",
    font_size: int = 11,
) -> str:
    """Render a small accessible SVG badge for *health*.

    The badge is two segments (label + value) in the shields.io style,
    generated locally. Widths are estimated from character counts; text
    elements carry ``textLength`` so the text never overflows its segment
    regardless of the renderer's font metrics.
    """
    band = _band(health.health_score)
    color = _BADGE_COLORS[band]
    if health.health_score is None:
        value = _BADGE_TEXT["gray"]
        aria = "Test health: insufficient data"
    else:
        value = f"{health.health_score}/100"
        aria = f"Test health: {health.health_score} of 100 (heuristic)"

    label_w = round(len(label) * _CHAR_WIDTH + 2 * _LABEL_PAD)
    value_w = round(len(value) * _CHAR_WIDTH + 2 * _VALUE_PAD)
    total_w = label_w + value_w
    height = _BADGE_HEIGHT
    radius = 3
    label_x = label_w / 2
    value_x = label_w + value_w / 2

    title = _esc(
        "Test health: "
        + ("insufficient data" if health.health_score is None else f"{health.health_score}/100")
        + " - heuristic indicator, not a statistical probability"
    )
    desc = _esc(
        f"flakeherd metric_version {health.metric_version}: "
        f"{health.runs} run(s), {health.scored_tests} scored test(s), "
        f"state {health.state}"
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_w}" height="{height}" '
        f'role="img" aria-label="{_esc(aria)}">\n'
        f"  <title>{title}</title>\n"
        f"  <desc>{desc}</desc>\n"
        f'  <rect width="{label_w}" height="{height}" rx="{radius}" fill="#555555"/>\n'
        f'<rect x="{label_w}" width="{value_w}" height="{height}" rx="{radius}" fill="{color}"/>\n'
        f'<rect x="{label_w - radius}" width="{radius}" height="{height}" fill="{color}"/>\n'
        f'  <text x="{label_x}" y="14" fill="#ffffff" font-family="Verdana,Helvetica,'
        f'DejaVu Sans,sans-serif" font-size="{font_size}" text-anchor="middle" '
        f'textLength="{max(1, label_w - 6)}" lengthAdjust="spacingAndGlyphs">{_esc(label)}</text>\n'
        f'  <text x="{value_x}" y="14" fill="#ffffff" font-family="Verdana,Helvetica,'
        f'DejaVu Sans,sans-serif" font-size="{font_size}" text-anchor="middle" '
        f'textLength="{max(1, value_w - 8)}" lengthAdjust="spacingAndGlyphs">{_esc(value)}</text>\n'
        "</svg>\n"
    )


def write_badge(path: str | Path, svg: str) -> None:
    try:
        atomic_write_bytes(path, svg.encode("utf-8"))
    except OSError as exc:
        raise FlakeHerdError(f"cannot write badge {path}: {exc}") from exc


__all__ = [
    "DEFAULT_MIN_RUNS",
    "METRIC_VERSION",
    "STATE_INSUFFICIENT",
    "STATE_OK",
    "HealthReport",
    "compute_health",
    "load_report_payload",
    "render_badge_svg",
    "write_badge",
]
