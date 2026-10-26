"""Core data model: parsed test results, recorded history, classifications.

The vocabulary used across the codebase:

* A *run* is one execution of a CI job (or one local invocation). It has a
  unique ``run_uid``, a commit sha, a branch and a timestamp.
* A *test case result* is one row parsed from a JUnit XML file: name,
  classname, status (pass/fail/skip), optional file/line, duration, a
  ``flaky_hint`` flag (evidence of flakiness *within* a single run, such as
  a retry that recovered - pytest-rerunfailures ``<rerunFailure>`` children,
  a test that both passed and failed inside the same run's merged report
  files, or ``flakeherd_retry_*`` JUnit properties written by FlakeHerd's
  own auto-retry), and optional retry metadata (attempt count, per-attempt
  outcomes, and an excerpt of the first failure) emitted by retry adapters.
* A *test key* is the stable identity of a test across runs:
  ``classname::name`` when a classname exists, otherwise ``name``.
* A *classification* is the verdict the detector assigns to a test key based
  on its history window.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Status values stored for every recorded outcome.
STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"
ALL_STATUSES = (STATUS_PASS, STATUS_FAIL, STATUS_SKIP)

# ---------------------------------------------------------------------------
# Classification identifiers (stable strings, used in reports/manifests)
# ---------------------------------------------------------------------------

CLS_NEW = "new"
CLS_SKIP_ONLY = "skip-only"
CLS_STABLE_PASS = "stable-pass"
CLS_BROKEN = "broken"
CLS_CONFIRMED_FLAKY = "confirmed-flaky"
CLS_FLAKY = "flaky"
CLS_SUSPECTED_FLAKY = "suspected-flaky"
CLS_FLAKY_LOW = "flaky-low"
CLS_FIXED = "fixed"
CLS_REGRESSION = "regression"
CLS_WATCH = "watch"

# Display labels for terminal / Markdown output.
CLASS_LABELS: dict[str, str] = {
    CLS_NEW: "new",
    CLS_SKIP_ONLY: "skip-only",
    CLS_STABLE_PASS: "stable-pass",
    CLS_BROKEN: "broken",
    CLS_CONFIRMED_FLAKY: "confirmed flaky",
    CLS_FLAKY: "flaky",
    CLS_SUSPECTED_FLAKY: "suspected flaky",
    CLS_FLAKY_LOW: "flaky (low conf.)",
    CLS_FIXED: "fixed",
    CLS_REGRESSION: "regression",
    CLS_WATCH: "watch",
}

# Classes that count as "flaky evidence" when gating CI (the inverse set:
# currently-failing tests that are NOT in this set fail the gate).
# NOTE: ``suspected-flaky`` is deliberately NOT a member - in-run recovery
# evidence is suspicion, not proof, and must never unconditionally excuse a
# current failure ("innocent until *proven* flaky").
FLAKY_CLASSES = frozenset({CLS_CONFIRMED_FLAKY, CLS_FLAKY, CLS_FLAKY_LOW})

# Classes quarantined by default (unless --include-low-confidence is used).
DEFAULT_QUARANTINE_CLASSES = frozenset({CLS_CONFIRMED_FLAKY, CLS_FLAKY})

# Low-confidence classes: quarantined only with an explicit opt-in
# (--include-low-confidence / include_low_confidence).
LOW_CONFIDENCE_CLASSES = frozenset({CLS_FLAKY_LOW, CLS_SUSPECTED_FLAKY})


# Retry-evidence property names written by FlakeHerd's pytest auto-retry
# adapter into JUnit XML <properties> nodes. Any framework adapter may emit
# the same contract (see docs/ARCHITECTURE.md) - FlakeHerd's parser reads
# these properties from any JUnit XML file.
RETRY_PROP_ATTEMPTS = "flakeherd_retry_attempts"
RETRY_PROP_OUTCOMES = "flakeherd_retry_outcomes"
RETRY_PROP_FIRST_FAILURE = "flakeherd_retry_first_failure"


@dataclass(frozen=True)
class TestCaseResult:
    """One parsed ``<testcase>`` from a JUnit XML report."""

    name: str
    classname: str | None = None
    status: str = STATUS_PASS
    file: str | None = None
    line: int | None = None
    duration: float | None = None
    flaky_hint: bool = False
    # Retry evidence (None = the report carried no retry metadata).
    retry_attempts: int | None = None
    retry_outcomes: str = ""  # e.g. "F,P" - one letter per attempt
    retry_first_failure: str = ""  # capped excerpt of the first failure

    @property
    def test_key(self) -> str:
        if self.classname:
            return f"{self.classname}::{self.name}"
        return self.name

    def __post_init__(self) -> None:
        if self.status not in ALL_STATUSES:
            raise ValueError(f"invalid status {self.status!r} for {self.test_key!r}")


TestCaseResult.__test__ = False


@dataclass(frozen=True)
class RunInfo:
    """Metadata for one recorded run."""

    run_uid: str
    commit_sha: str = "unknown"
    branch: str = "unknown"
    created_at: str = ""  # ISO-8601 UTC; Store fills it if empty


@dataclass(frozen=True)
class Sample:
    """One historical outcome of a test, as loaded from the store.

    Ordering within a ``TestHistory`` is oldest first.
    """

    status: str
    commit_sha: str = "unknown"
    run_uid: str = ""
    created_at: str = ""
    flaky_hint: bool = False
    retry_attempts: int | None = None  # None = not reported by the adapter
    retry_outcomes: str = ""


@dataclass
class TestHistory:
    """All recorded outcomes for one test key, oldest first."""

    test_key: str
    samples: list[Sample] = field(default_factory=list)
    # Latest known identity metadata (from the most recent outcome).
    name: str = ""
    classname: str | None = None
    file: str | None = None
    line: int | None = None

    @property
    def current_status(self) -> str:
        return self.samples[-1].status if self.samples else STATUS_SKIP

    @staticmethod
    def outcome_letter(sample: Sample) -> str:
        return {"pass": "P", "fail": "F", "skip": "."}[sample.status]

    def recent_pattern(self, count: int = 10) -> str:
        """Compact ``PFPF...`` view of the most recent samples."""
        return "".join(self.outcome_letter(s) for s in self.samples[-count:])


@dataclass
class Classification:
    """The detector's verdict for one test key."""

    test_key: str
    name: str
    classname: str | None
    file: str | None
    line: int | None
    cls: str
    score: int  # 0..100, higher = more confidently flaky
    samples: int
    pass_count: int
    fail_count: int
    skip_count: int
    transitions: int
    failure_rate: float  # fail / (pass + fail); 0.0 when no pass/fail samples
    current_status: str
    recent: str  # e.g. "PFPF..PF"
    in_current_run: bool = False  # did this test appear in the newest run?
    same_sha_divergence: bool = False
    flaky_hint_seen: bool = False
    reason: str = ""
    # Retry evidence from the current run's sample (None = none reported).
    retry_attempts: int | None = None
    retry_outcomes: str = ""

    @property
    def label(self) -> str:
        return CLASS_LABELS.get(self.cls, self.cls)

    @property
    def is_flaky_family(self) -> bool:
        return self.cls in FLAKY_CLASSES
