"""Quarantine policy tests: threshold boundaries, min samples, expiry, audit.

The score-threshold policy is opt-in (``quarantine_threshold_enabled``).
These tests pin the 39/40/41 boundary behavior, the sufficient-evidence
guard, class eligibility (broken/regression never qualify), expiry
metadata + consumption, audit reasons, and manifest compatibility in both
directions with flakeherd 0.1.1 manifests.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from tests.conftest import make_history

from flakeherd.config import (
    DEFAULT_QUARANTINE_MIN_SAMPLES,
    DEFAULT_QUARANTINE_SCORE_THRESHOLD,
    Config,
)
from flakeherd.detector import classify
from flakeherd.model import CLS_SUSPECTED_FLAKY
from flakeherd.quarantine import (
    QuarantineEntry,
    is_expired,
    load_manifest,
    select,
    write_manifest,
)

# flaky-low patterns whose computed scores are exactly 39, 40 and 41
# (score = 35 + 20*failure_rate + 10*transition_density; verified against
# the detector - see tests/test_detector.py for the formula pins).
PATTERN_39 = "PPPPPPPFPP"
PATTERN_40 = "PPPPPFPP"
PATTERN_41 = "PPPPFPP"


def _cls(pattern, **kw):
    return classify(make_history(pattern=pattern, **kw), Config(), in_current_run=True)


def _policy(**kw) -> Config:
    defaults = dict(quarantine_threshold_enabled=True)
    defaults.update(kw)
    return Config(**defaults)


class TestThresholdBoundaries:
    def test_boundary_scores_are_exactly_39_40_41(self):
        # Guard the fixtures themselves: if the score formula ever changes,
        # these tests must be re-derived rather than silently drift.
        assert _cls(PATTERN_39).score == 39
        assert _cls(PATTERN_40).score == 40
        assert _cls(PATTERN_41).score == 41

    def test_policy_default_threshold_is_40_when_enabled(self):
        assert DEFAULT_QUARANTINE_SCORE_THRESHOLD == 40
        cfg = _policy()
        assert cfg.quarantine_score_threshold == 40

    def test_score_39_is_excluded_at_default_threshold(self):
        # flaky-low needs the low-confidence opt-in to be eligible at all
        cfg = _policy(include_low_confidence=True)
        entries = select([_cls(PATTERN_39)], cfg)
        assert entries == []

    def test_score_40_qualifies_at_default_threshold(self):
        cfg = _policy(include_low_confidence=True)
        entries = select([_cls(PATTERN_40)], cfg)
        assert [e.test_key for e in entries] == [_cls(PATTERN_40).test_key]

    def test_score_41_qualifies_at_default_threshold(self):
        cfg = _policy(include_low_confidence=True)
        entries = select([_cls(PATTERN_41)], cfg)
        assert len(entries) == 1

    def test_custom_threshold_respected(self):
        cfg = _policy(quarantine_score_threshold=70)
        # confirmed-flaky (5 samples, same-sha divergence) scores >= 70;
        # this flaky pattern scores 68 and must not qualify.
        confirmed = _cls("PFPPP", shas=["s", "s", "a", "b", "c"])
        flaky = _cls("PFPPFPPPPP")  # flaky, score 68 < 70
        assert confirmed.cls == "confirmed-flaky"
        assert confirmed.score >= 70 and flaky.score < 70
        entries = select([confirmed, flaky], cfg)
        assert [e.cls for e in entries] == ["confirmed-flaky"]

    def test_policy_off_keeps_legacy_behavior(self):
        # Without the opt-in nothing changes: every eligible class qualifies.
        cfg = Config()  # threshold policy disabled
        _cls(PATTERN_39)  # flaky-low, score 39 < 40 but legacy...
        # ...still not selected because flaky-low needs include_low_confidence
        # even in legacy mode; confirmed-flaky with 2 samples IS selected:
        two_sample = _cls("PF", shas=["s", "s"])
        assert two_sample.samples == 2
        entries = select([two_sample], cfg)
        assert len(entries) == 1

    def test_min_score_flag_can_only_raise_not_lower(self):
        # --min-score 10 must NOT lower the effective threshold below the
        # policy floor of 40: the threshold is a safety floor, not a ceiling.
        cfg = _policy(include_low_confidence=True)
        entries = select([_cls(PATTERN_39)], cfg, cli_min_score=10)
        assert entries == []


class TestMinimumSamples:
    def test_default_min_samples_is_five(self):
        assert DEFAULT_QUARANTINE_MIN_SAMPLES == 5

    def test_insufficient_history_is_excluded_under_policy(self):
        cfg = _policy()
        # confirmed-flaky but only 2 recorded samples
        short = _cls("PF", shas=["s", "s"])
        assert short.cls == "confirmed-flaky"
        assert short.samples == 2
        assert select([short], cfg) == []

    def test_sufficient_history_qualifies_under_policy(self):
        cfg = _policy()
        # 5+ samples, flaky (3 transitions)
        long_flaky = _cls("PFPFPF")
        assert long_flaky.cls == "flaky" and long_flaky.samples == 6
        entries = select([long_flaky], cfg)
        assert len(entries) == 1

    def test_min_samples_not_enforced_without_policy(self):
        # Legacy mode preserves 0.1.1 behavior exactly.
        cfg = Config()
        short = _cls("PF", shas=["s", "s"])
        assert len(select([short], cfg)) == 1

    def test_min_samples_validation(self):
        with pytest.raises(Exception, match="quarantine_min_samples"):
            Config(quarantine_min_samples=0).validated()

    def test_threshold_validation(self):
        with pytest.raises(Exception, match="quarantine_score_threshold"):
            Config(quarantine_score_threshold=101).validated()

    def test_expiry_days_validation(self):
        with pytest.raises(Exception, match="quarantine_expiry_days"):
            Config(quarantine_expiry_days=-1).validated()

    def test_config_roundtrip_new_keys(self, tmp_path: Path):
        path = tmp_path / ".flakeherd.json"
        path.write_text(json.dumps({
            "quarantine_threshold_enabled": True,
            "quarantine_score_threshold": 55,
            "quarantine_min_samples": 8,
            "quarantine_expiry_days": 14,
        }))
        cfg = Config.load(path)
        assert cfg.quarantine_threshold_enabled is True
        assert cfg.quarantine_score_threshold == 55
        assert cfg.quarantine_min_samples == 8
        assert cfg.quarantine_expiry_days == 14

    def test_wrong_type_rejected(self, tmp_path: Path):
        path = tmp_path / ".flakeherd.json"
        path.write_text(json.dumps({"quarantine_min_samples": "five"}))
        with pytest.raises(Exception, match="integer"):
            Config.load(path)


class TestEligibility:
    def test_broken_never_quarantined(self):
        cfg = _policy()
        broken = _cls("FFFFFFF")
        assert broken.cls == "broken"
        assert select([broken], cfg) == []

    def test_regression_never_quarantined(self):
        cfg = _policy()
        regression = _cls("PPPPFFF")
        assert regression.cls == "regression"
        assert select([regression], cfg) == []

    def test_stable_pass_never_quarantined(self):
        cfg = _policy()
        stable = _cls("PPPPPPP")
        assert select([stable], cfg) == []

    def test_suspected_flaky_requires_low_confidence_opt_in(self):
        cfg = _policy()
        suspected = _cls("P", hints="1")  # single recovered run
        assert suspected.cls == CLS_SUSPECTED_FLAKY
        assert select([suspected], cfg) == []
        cfg_low = _policy(include_low_confidence=True, quarantine_min_samples=1)
        entries = select([suspected], cfg_low)
        assert [e.cls for e in entries] == [CLS_SUSPECTED_FLAKY]


class TestExpiryAndAudit:
    def test_entries_carry_expiry_date_when_configured(self):
        cfg = _policy(quarantine_expiry_days=7)
        today = date(2026, 10, 25)
        entries = select([_cls("PFPFPF")], cfg, today=today)
        assert entries[0].expires_at == (today + timedelta(days=7)).isoformat()

    def test_no_expiry_by_default(self):
        cfg = _policy()
        entries = select([_cls("PFPFPF")], cfg)
        assert entries[0].expires_at is None

    def test_entries_carry_audit_reason(self):
        cfg = _policy()
        c = _cls("PFPFPF")
        entries = select([c], cfg)
        audit = entries[0].audit_reason
        assert f"score {c.score} >= 40" in audit
        assert f"samples {c.samples} >= 5" in audit
        assert "policy=threshold" in audit

    def test_legacy_mode_audit_reason(self):
        cfg = Config()
        entries = select([_cls("PFPFPF")], cfg)
        assert "policy=legacy" in entries[0].audit_reason

    def test_is_expired_semantics(self):
        def entry(expires_at):
            return QuarantineEntry(
                test_key="k", name="t", classname=None, file=None,
                cls="flaky", score=60, failure_rate=0.4, samples=8,
                reason="r", expires_at=expires_at,
            )

        assert is_expired(entry(None)) is False
        assert is_expired(entry("2026-10-24"), today=date(2026, 10, 25)) is True
        assert is_expired(entry("2026-10-25"), today=date(2026, 10, 25)) is False
        assert is_expired(entry("2026-10-26"), today=date(2026, 10, 25)) is False
        # malformed dates must never silently un-quarantine
        assert is_expired(entry("not-a-date"), today=date(2026, 10, 25)) is False


class TestManifestCompatibility:
    def test_new_manifest_roundtrip_keeps_new_fields(self, tmp_path: Path):
        cfg = _policy(quarantine_expiry_days=3)
        entries = select([_cls("PFPFPF")], cfg, today=date(2026, 10, 25))
        path = tmp_path / "q.json"
        write_manifest(path, entries, generator="flakeherd 0.2.0")
        loaded = load_manifest(path)
        assert loaded == entries
        assert loaded[0].expires_at == "2026-10-28"
        assert "policy=threshold" in loaded[0].audit_reason

    def test_old_011_manifest_still_loads(self, tmp_path: Path):
        # A manifest exactly as flakeherd 0.1.1 wrote it: no expires_at,
        # no audit_reason anywhere.
        legacy = {
            "schema_version": 1,
            "generator": "flakeherd 0.1.1",
            "generated_at": "2026-01-01T00:00:00+00:00",
            "quarantined": [
                {
                    "test_key": "c::t",
                    "name": "t",
                    "classname": "c",
                    "file": "tests/test_x.py",
                    "cls": "confirmed-flaky",
                    "score": 80,
                    "failure_rate": 0.4,
                    "samples": 10,
                    "reason": "same commit produced both a pass and a fail",
                }
            ],
        }
        path = tmp_path / "legacy.json"
        path.write_text(json.dumps(legacy))
        loaded = load_manifest(path)
        assert len(loaded) == 1
        assert loaded[0].expires_at is None
        assert loaded[0].audit_reason == ""
        assert loaded[0].cls == "confirmed-flaky"

    def test_new_manifest_is_json_compatible_with_011_readers(self, tmp_path: Path):
        # 0.1.x load_manifest ignores unknown entry keys; verify the new
        # manifest keeps every legacy field intact so old readers work.
        cfg = _policy(quarantine_expiry_days=5)
        entries = select([_cls("PFPFPF")], cfg, today=date(2026, 10, 25))
        path = tmp_path / "q.json"
        write_manifest(path, entries, generator="flakeherd 0.2.0")
        raw = json.loads(path.read_text())
        assert raw["schema_version"] == 1
        row = raw["quarantined"][0]
        for legacy_field in (
            "test_key", "name", "classname", "file", "cls",
            "score", "failure_rate", "samples", "reason",
        ):
            assert legacy_field in row

    def test_schema_version_stays_one(self):
        from flakeherd.quarantine import SCHEMA_VERSION

        assert SCHEMA_VERSION == 1
