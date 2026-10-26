"""The detector: turns raw history into human-meaningful classifications.

The taxonomy (and the exact rule order) is the intellectual core of FlakeHerd,
so it is specified here once and pinned by ``tests/test_detector.py``.

For each test key, we look at the last ``window`` recorded outcomes
("samples", oldest first). Skips are kept for reporting but excluded from
pass/fail reasoning. Because a sample marked with in-run flakiness evidence
(a retry that recovered) genuinely *contains* a failure, such a sample is
counted as a fail-then-pass attempt pair when computing pass/fail counts,
failure rate, transitions and trailing streaks - the "effective" sequence.
Rules, in priority order:

1.  **new** - only one recorded sample and no in-run recovery. No verdict
    yet (report shows it). A single sample that failed and then recovered
    inside that same run is classified *suspected-flaky* immediately: the
    evidence exists even though the history is short.
2.  **skip-only** - every sample is a skip; nothing to say about flakiness.
3.  **confirmed-flaky** - the *same commit sha* has both a pass and a fail
    (computed on recorded run outcomes) - e.g. a GitHub "Re-run failed
    jobs" that goes green. This is the gold-standard signal: identical
    code, different outcome *across runs*.
4.  **broken** - every attempt failed. Real bug or environment problem, not
    flakiness; quarantining would hide a bug. (A test that failed every
    retry attempt lands here too - reruns that never recover are not
    flakiness evidence.)
5.  **stable-pass** - every attempt passed.
6.  Remaining tests have both passes and failures in the window. Let
    ``T`` be the number of pass<->fail transitions and ``S`` the current
    trailing streak of consecutive failures:
    - **regression** - ``S >= min_regression_streak`` AND ``T <= 2``: the
      test used to pass, now keeps failing; little historical flip-flopping.
    - **flaky** - ``T >= 3``: clear oscillation.
    - **suspected-flaky** - some run recorded an in-run recovery (failed
      then passed inside a single run - an auto-retry or rerun that
      recovered). Direct evidence of nondeterminism, but a single
      mechanism: suspicion, not proof. It never unconditionally excuses a
      current failure and is quarantined only behind the low-confidence
      opt-in.
    - **flaky-low** - ``T == 2``: either a one-off flake (P F P) or a
      break-then-fix (P F F P); report, do not quarantine by default.
    - **fixed** - ``T == 1`` and currently passing: failed earlier, recovered.
    - **watch** - ``T == 1`` and currently failing with a short streak:
      could be the first flake or the start of a regression; needs evidence.

Score (0-100), for ranking and quarantine thresholds::

    score = base[class] + 20 * failure_rate + 10 * transition_density
            base: confirmed-flaky 70, flaky 60, suspected-flaky 45,
                  flaky-low 35, fixed 15, watch 15
            transition_density = T / (attempt samples - 1)

Gating ("unexplained failures"): a currently-failing test that does NOT
carry *proven* flaky evidence (class outside the flaky family - which
excludes suspected-flaky) is considered an unexplained failure and can fail
the CI job. In other words: tests are innocent until *proven* flaky, so a
brand-new failure still breaks the build - and an auto-retry that recovered
once does not buy a test a permanent free pass. A test that fails *every*
retry attempt stays red: retries must never silently turn a genuine failure
into a green gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config
from .junit import is_ignored
from .model import (
    CLS_BROKEN,
    CLS_CONFIRMED_FLAKY,
    CLS_FIXED,
    CLS_FLAKY,
    CLS_FLAKY_LOW,
    CLS_NEW,
    CLS_REGRESSION,
    CLS_SKIP_ONLY,
    CLS_STABLE_PASS,
    CLS_SUSPECTED_FLAKY,
    CLS_WATCH,
    FLAKY_CLASSES,
    STATUS_FAIL,
    STATUS_PASS,
    Classification,
    TestHistory,
)
from .store import Store

# Report ordering: worst/most-interesting first.
SEVERITY_ORDER = [
    CLS_BROKEN,
    CLS_REGRESSION,
    CLS_CONFIRMED_FLAKY,
    CLS_FLAKY,
    CLS_SUSPECTED_FLAKY,
    CLS_FLAKY_LOW,
    CLS_WATCH,
    CLS_FIXED,
    CLS_NEW,
    CLS_SKIP_ONLY,
    CLS_STABLE_PASS,
]

_BASE_SCORE = {
    CLS_CONFIRMED_FLAKY: 70,
    CLS_FLAKY: 60,
    CLS_SUSPECTED_FLAKY: 45,
    CLS_FLAKY_LOW: 35,
    CLS_FIXED: 15,
    CLS_WATCH: 15,
}


@dataclass
class AnalysisResult:
    """Everything the reporters need, in one serializable bundle."""

    classifications: list[Classification] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    current_run_uid: str | None = None
    run_count: int = 0
    test_count: int = 0

    @property
    def flaky_family(self) -> list[Classification]:
        return sorted(
            (c for c in self.classifications if c.is_flaky_family),
            key=lambda c: (-c.score, c.test_key),
        )

    def gate_failures(self) -> list[Classification]:
        """Currently-failing tests without flaky evidence (see module docs)."""
        return [
            c
            for c in self.classifications
            if c.in_current_run
            and c.current_status == STATUS_FAIL
            and c.cls not in FLAKY_CLASSES
        ]


def classify(hist: TestHistory, cfg: Config, in_current_run: bool) -> Classification:
    """Apply the taxonomy to one test's history window."""
    samples = hist.samples
    current = hist.current_status

    # Raw run outcomes (skips excluded): used for the same-sha divergence
    # check. A recovered sample's hidden in-run failure must NOT fabricate
    # cross-run divergence - in-run recovery is *suspected* evidence.
    raw_pf = [s for s in samples if s.status != "skip"]

    # Effective attempt sequence: a sample that recovered in-run (a pass
    # carrying flakiness evidence) really contained a failure, so it expands
    # to fail-then-pass. A fail carrying rerun markers (retried, never
    # recovered) stays a single fail - retries that never recovered are not
    # recovery evidence.
    pf: list[str] = []
    for s in raw_pf:
        if s.status == STATUS_PASS and s.flaky_hint:
            pf.append(STATUS_FAIL)
            pf.append(STATUS_PASS)
        else:
            pf.append(s.status)
    passes = pf.count(STATUS_PASS)
    fails = pf.count(STATUS_FAIL)
    p, f = passes, fails
    n = len(samples)
    transitions = sum(1 for a, b in zip(pf, pf[1:]) if a != b)
    failure_rate = f / (p + f) if (p + f) else 0.0

    same_sha_divergence = False
    by_sha: dict[str, set[str]] = {}
    for s in raw_pf:
        # Sentinel shas ("unknown", empty) mean "we do not know the commit" -
        # they must never count as divergence evidence, or any two unrelated
        # local runs with pass/fail flips would be misread as proven flaky.
        if not s.commit_sha or s.commit_sha == "unknown":
            continue
        by_sha.setdefault(s.commit_sha, set()).add(s.status)
    divergent_sha = next((sha for sha, seen in by_sha.items() if len(seen) > 1), None)
    if divergent_sha is not None:
        same_sha_divergence = True
    flaky_hint_seen = any(s.flaky_hint for s in samples)
    # "Recovery" = a run that failed at least once and ended green (an
    # auto-retry or rerun that succeeded, or a same-run duplicate pass+fail).
    recovery_seen = any(
        s.status == STATUS_PASS and s.flaky_hint for s in samples
    )

    # ---- rule order (see module docstring) --------------------------------
    if n == 1 and recovery_seen:
        cls = CLS_SUSPECTED_FLAKY
        reason = "failed then recovered inside its first recorded run"
    elif n == 1:
        cls = CLS_NEW
        reason = f"first recorded outcome ({current})"
    elif p == 0 and f == 0:
        cls = CLS_SKIP_ONLY
        reason = "skipped in every recorded run"
    elif same_sha_divergence:
        cls = CLS_CONFIRMED_FLAKY
        short = divergent_sha[:10] if divergent_sha else "?"
        reason = f"commit {short} produced both a pass and a fail (re-run divergence)"
    elif p == 0:
        cls = CLS_BROKEN
        reason = (
            f"failed every time it ran ({n} recorded outcome(s)) - "
            "likely a real bug, not flakiness"
        )
    elif f == 0:
        cls = CLS_STABLE_PASS
        reason = f"passed in all {n} recorded runs"
    else:
        trailing = 0
        for status in reversed(pf):
            if status == STATUS_FAIL:
                trailing += 1
            else:
                break
        if trailing >= cfg.min_regression_streak and transitions <= 2:
            cls = CLS_REGRESSION
            reason = f"was passing, now failing for {trailing} consecutive run(s)"
        elif transitions >= 3:
            cls = CLS_FLAKY
            reason = f"{transitions} pass/fail transitions in the last {n} runs"
        elif recovery_seen:
            cls = CLS_SUSPECTED_FLAKY
            reason = (
                "recovered within a single run (fail-then-pass retry evidence) - "
                "suspected, not proven"
            )
        elif transitions == 2:
            cls = CLS_FLAKY_LOW
            reason = "2 transitions - one-off flake or break-then-fix (needs more evidence)"
        elif pf[-1] == STATUS_PASS:
            cls = CLS_FIXED
            reason = "failed earlier, passing again in the latest runs"
        else:
            cls = CLS_WATCH
            reason = "first failure after a passing streak - new regression or first flake"

    score = round(
        min(
            100,
            max(
                0,
                _BASE_SCORE.get(cls, 0)
                + 20 * failure_rate
                + 10 * (transitions / max(1, len(pf) - 1)),
            ),
        )
    )
    return Classification(
        test_key=hist.test_key,
        name=hist.name,
        classname=hist.classname,
        file=hist.file,
        line=hist.line,
        cls=cls,
        score=score,
        samples=n,
        pass_count=p,
        fail_count=f,
        skip_count=n - len(raw_pf),
        transitions=transitions,
        failure_rate=failure_rate,
        current_status=current,
        recent=hist.recent_pattern(),
        in_current_run=in_current_run,
        same_sha_divergence=same_sha_divergence,
        flaky_hint_seen=flaky_hint_seen,
        reason=reason,
    )


def analyze(store: Store, cfg: Config, current_run_uid: str | None = None) -> AnalysisResult:
    """Classify every known test, sorted for reporting.

    *current_run_uid* should be the run that was just recorded (the CLI
    passes it explicitly). Falling back to "newest by created_at" only
    makes sense when no run was recorded in this process - a merged remote
    run with a skewed timestamp must never hijack gate decisions.
    """
    histories = store.load_history(cfg.window)
    current_run_uid = current_run_uid or store.current_run_uid()
    classifications: list[Classification] = []
    for key, hist in histories.items():
        if is_ignored(key, hist.name, hist.classname, cfg.ignore):
            continue
        in_current = any(s.run_uid == current_run_uid for s in hist.samples)
        classification = classify(hist, cfg, in_current)
        # Surface the current run's retry evidence in machine-readable form.
        for s in hist.samples:
            if s.run_uid == current_run_uid:
                classification.retry_attempts = s.retry_attempts
                classification.retry_outcomes = s.retry_outcomes
                break
        classifications.append(classification)
    order = {name: i for i, name in enumerate(SEVERITY_ORDER)}
    classifications.sort(
        key=lambda c: (order.get(c.cls, 99), -c.score, c.test_key)
    )
    counts: dict[str, int] = {}
    for c in classifications:
        counts[c.cls] = counts.get(c.cls, 0) + 1
    return AnalysisResult(
        classifications=classifications,
        counts=counts,
        current_run_uid=current_run_uid,
        run_count=store.run_count(),
        test_count=store.test_count(),
    )
