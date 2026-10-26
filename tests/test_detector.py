"""Detector tests: the classification taxonomy, score and gate are all
pinned here. Every rule in docs/ARCHITECTURE.md must have a matching test.
"""

from __future__ import annotations

from tests.conftest import make_history, make_result, record_run

from flakeherd.config import Config
from flakeherd.detector import analyze, classify
from flakeherd.model import (
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
)


def _cls(pattern, shas=None, hints="", cfg=None, key="k::t"):
    hist = make_history(key=key, pattern=pattern, shas=shas, hints=hints)
    return classify(hist, cfg or Config(), in_current_run=True)


class TestSentinelSha:
    """"unknown"/empty shas must never count as divergence evidence."""

    def test_unknown_sha_flip_is_not_confirmed_flaky(self):
        c = _cls("PF", shas=["unknown", "unknown"])
        assert c.cls == CLS_WATCH
        assert c.same_sha_divergence is False

    def test_unknown_and_real_sha_do_not_pair_up(self):
        assert _cls("PF", shas=["unknown", "s"]).cls == CLS_WATCH

    def test_empty_sha_ignored(self):
        c = _cls("PF", shas=["", ""])
        assert c.cls == CLS_WATCH

    def test_real_same_sha_divergence_still_detected(self):
        # the fix must not break the actual signal
        assert _cls("PF", shas=["abc", "abc"]).cls == CLS_CONFIRMED_FLAKY


class TestBasicClasses:
    def test_single_sample_is_new(self):
        assert _cls("P").cls == CLS_NEW
        assert _cls("F").cls == CLS_NEW

    def test_all_skips_is_skip_only(self):
        assert _cls("...").cls == CLS_SKIP_ONLY

    def test_all_pass_is_stable_pass(self):
        assert _cls("PPPPP").cls == CLS_STABLE_PASS

    def test_all_fail_is_broken(self):
        assert _cls("FFFF").cls == CLS_BROKEN


class TestConfirmedFlaky:
    def test_same_sha_divergence(self):
        assert _cls("PF", shas=["s", "s"]).cls == CLS_CONFIRMED_FLAKY

    def test_same_sha_among_other_runs(self):
        assert _cls("PPFFP", shas=["a", "b", "s", "s", "s"]).cls == CLS_CONFIRMED_FLAKY

    def test_rerun_hint_on_all_fail_history_is_broken(self):
        # 0.2.0 fix: a rerun marker on a FAILING sample means "retried and
        # still failed" - never recovery evidence. Classifying such a test
        # as confirmed-flaky excused a consistently failing test from the
        # gate (the retry-masking defect).
        c = _cls("FFF", hints="001")
        assert c.cls == CLS_BROKEN
        assert c.flaky_hint_seen

    def test_reason_mentions_sha(self):
        c = _cls("FP", shas=["deadbeef99", "deadbeef99"])
        assert "deadbeef99" in c.reason

    def test_same_sha_but_same_status_not_divergent(self):
        # same sha, both fail: a genuine failure, not flakiness
        assert _cls("FF", shas=["s", "s"]).cls == CLS_BROKEN


class TestMixedOutcomes:
    def test_three_transitions_is_flaky(self):
        c = _cls("PFPF")
        assert c.cls == CLS_FLAKY
        assert c.transitions == 3

    def test_two_transitions_is_low_confidence(self):
        # both shapes of T==2: one-off flake and break-then-fix
        assert _cls("PFP").cls == CLS_FLAKY_LOW
        assert _cls("PPFFP").cls == CLS_FLAKY_LOW

    def test_single_transition_ending_pass_is_fixed(self):
        assert _cls("FFPP").cls == CLS_FIXED

    def test_single_transition_short_streak_is_watch(self):
        assert _cls("PPPF").cls == CLS_WATCH

    def test_trailing_streak_is_regression(self):
        assert _cls("PPPFF").cls == CLS_REGRESSION
        assert _cls("PPFFF").cls == CLS_REGRESSION

    def test_regression_threshold_configurable(self):
        cfg = Config(min_regression_streak=1)
        assert classify(
            make_history(pattern="PPF"), cfg, in_current_run=True
        ).cls == CLS_REGRESSION

    def test_long_history_oscillation_stays_flaky(self):
        # T=3 with a trailing streak: strong oscillation wins
        assert _cls("PFPFFF").cls == CLS_FLAKY

    def test_skips_do_not_break_transitions(self):
        # skips are ignored for pass/fail reasoning
        assert _cls("P.F.P").cls == CLS_FLAKY_LOW


class TestScores:
    def test_confirmed_scores_higher_than_flaky(self):
        confirmed = _cls("PF", shas=["s", "s"])
        flaky = _cls("PFPF", shas=["a", "b", "c", "d"])
        assert confirmed.score > flaky.score

    def test_score_bounds_and_monotonic_with_failure_rate(self):
        # same class (confirmed-flaky), different failure rates
        low = _cls("PFPPPPPP", shas=["s", "s"] + [f"x{i}" for i in range(6)])
        high = _cls("PFFPPPPP", shas=["s", "s"] + [f"x{i}" for i in range(6)])
        assert low.cls == CLS_CONFIRMED_FLAKY and high.cls == CLS_CONFIRMED_FLAKY
        assert 0 <= low.score <= 100
        assert 0 <= high.score <= 100
        assert high.score >= low.score

    def test_stable_pass_scores_zero(self):
        assert _cls("PPPP").score == 0


class TestAnalyze:
    def test_full_flow_classification_and_counts(self, store, config):
        # one stable, one confirmed (re-run), one regression, one broken
        for i, uid in enumerate(["r1", "r2", "r3", "r4"]):
            sha = "same" if uid in ("r1", "r2") else f"s{i}"
            record_run(
                store,
                uid,
                sha,
                [
                    make_result(name="stable"),
                    make_result(name="flaky", status="fail" if uid == "r2" else "pass"),
                    make_result(name="reg", status="pass" if i < 2 else "fail"),
                    make_result(name="broken", status="fail"),
                ],
                created_at=f"2026-01-01T00:0{i}:00+00:00",
            )
        result = analyze(store, config)
        by_key = {c.test_key: c for c in result.classifications}
        assert by_key["test_mod.TestA::stable"].cls == CLS_STABLE_PASS
        assert by_key["test_mod.TestA::flaky"].cls == CLS_CONFIRMED_FLAKY
        assert by_key["test_mod.TestA::reg"].cls == CLS_REGRESSION
        assert by_key["test_mod.TestA::broken"].cls == CLS_BROKEN
        assert result.run_count == 4
        assert result.test_count == 4
        assert result.counts[CLS_BROKEN] == 1

    def test_in_current_run_flag(self, store, config):
        record_run(
            store, "r1", "s1", [make_result(name="a")],
            created_at="2026-01-01T00:00:00+00:00",
        )
        record_run(
            store, "r2", "s2", [make_result(name="b")],
            created_at="2026-02-01T00:00:00+00:00",
        )
        result = analyze(store, config)
        by_key = {c.test_key: c for c in result.classifications}
        assert by_key["test_mod.TestA::a"].in_current_run is False
        assert by_key["test_mod.TestA::b"].in_current_run is True

    def test_ignore_patterns_drop_tests(self, store):
        cfg = Config(ignore=["*Ignore*"])
        record_run(store, "r1", "s", [make_result(name="Keep"), make_result(name="IgnoreMe")])
        result = analyze(store, cfg)
        keys = {c.test_key for c in result.classifications}
        assert keys == {"test_mod.TestA::Keep"}

    def test_gate_failures_exclude_flaky(self, store):
        # "proven": fails in the newest run but has same-sha pass/fail
        # divergence -> confirmed-flaky -> excused by the gate.
        # "real": always fails -> broken -> gated.
        for uid, statuses in [
            ("r1", ("pass", "fail")),
            ("r2", ("fail", "fail")),
            ("r3", ("fail", "fail")),
        ]:
            record_run(
                store,
                uid,
                "same",
                [
                    make_result(name="proven", status=statuses[0]),
                    make_result(name="real", status=statuses[1]),
                ],
                created_at=f"2026-01-0{1 + int(uid[1:]) - 1}T00:00:00+00:00",
            )
        result = analyze(store, Config())
        gate_keys = {c.test_key for c in result.gate_failures()}
        assert gate_keys == {"test_mod.TestA::real"}
        by_key = {c.test_key: c for c in result.classifications}
        assert by_key["test_mod.TestA::proven"].cls == CLS_CONFIRMED_FLAKY
        assert by_key["test_mod.TestA::proven"].current_status == "fail"


class TestGateScenarios:
    def test_current_failure_with_flaky_evidence_is_excused(self):
        c = _cls("PFPF")  # cls=flaky, current status fail
        assert c.cls == CLS_FLAKY
        assert c.current_status == "fail"
        assert c.is_flaky_family

    def test_new_failure_gates(self):
        c = _cls("F")
        assert c.cls == CLS_NEW
        assert not c.is_flaky_family

    def test_broken_gates(self):
        c = _cls("FF")
        assert c.cls == CLS_BROKEN
        assert not c.is_flaky_family


class TestSuspectedFlaky:
    """0.2.0: in-run fail-then-pass evidence is *suspected*, not confirmed."""

    def test_single_recovered_run_is_suspected(self):
        c = _cls("P", hints="1")
        assert c.cls == CLS_SUSPECTED_FLAKY
        assert c.flaky_hint_seen

    def test_recovered_among_passes_is_suspected(self):
        c = _cls("PPP", hints="010")
        assert c.cls == CLS_SUSPECTED_FLAKY

    def test_same_sha_divergence_still_confirms(self):
        # cross-run same-commit divergence remains the gold standard
        c = _cls("PFP", shas=["s", "s", "s"])
        assert c.cls == CLS_CONFIRMED_FLAKY

    def test_recovered_with_same_sha_is_not_fabricated_divergence(self):
        # the hidden in-run failure must NOT count as cross-run divergence
        c = _cls("PP", shas=["s", "s"], hints="10")
        assert c.cls == CLS_SUSPECTED_FLAKY
        assert c.same_sha_divergence is False

    def test_hint_on_fail_is_not_recovery(self):
        # retried but never recovered: not suspected-flaky evidence
        c = _cls("PF", hints="01")
        assert c.cls != CLS_SUSPECTED_FLAKY
        assert c.cls == CLS_WATCH  # P then F, single transition, currently failing

    def test_all_fail_with_hints_is_broken(self):
        assert _cls("FFF", hints="111").cls == CLS_BROKEN

    def test_oscillation_outranks_hint(self):
        # 3+ transitions is stronger evidence than a single recovery
        c = _cls("PFPFP", hints="10000")
        assert c.cls == CLS_FLAKY

    def test_regression_wins_over_old_hint(self):
        # recovered once long ago, now failing persistently -> regression
        c = _cls("PPPPFF", hints="100000")
        assert c.cls == CLS_REGRESSION

    def test_currently_passing_with_hint_is_suspected(self):
        c = _cls("FP", hints="01")
        assert c.cls == CLS_SUSPECTED_FLAKY

    def test_score_base_is_45(self):
        # 45 + 20 * failure_rate + 10 * transition_density
        c = _cls("PPPP", hints="1000")
        # effective F,P,P,P,P: fr=0.2, td=1/4 -> 45 + 4 + 2.5 = 51.5 -> 52
        assert c.cls == CLS_SUSPECTED_FLAKY
        assert c.score == 52

    def test_fail_count_counts_recovered_failures(self):
        c = _cls("PP", hints="10")
        # effective sequence F,P,P: one real failure inside the recovered run
        assert c.fail_count == 1
        assert c.pass_count == 2
        assert c.samples == 2  # recorded samples stay run-level

    def test_failure_rate_includes_recovered_failures(self):
        c = _cls("PP", hints="10")
        assert abs(c.failure_rate - 1 / 3) < 1e-9

    def test_suspected_flaky_does_not_excuse_gate(self):
        # suspected-flaky is NOT in the flaky family: a current failure with
        # only recovery history still gates (innocent until proven flaky)
        from flakeherd.model import FLAKY_CLASSES

        assert CLS_SUSPECTED_FLAKY not in FLAKY_CLASSES

    def test_hint_not_on_first_run_new_classification(self):
        # first recorded run, plain pass: new, no verdict
        c = _cls("P")
        assert c.cls == CLS_NEW
        assert c.cls != CLS_SUSPECTED_FLAKY
