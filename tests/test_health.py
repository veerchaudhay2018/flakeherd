"""Test Health metric tests: computation, states, badge rendering, CLI."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from flakeherd.cli import main
from flakeherd.errors import FlakeHerdError
from flakeherd.health import (
    DEFAULT_MIN_RUNS,
    METRIC_VERSION,
    STATE_INSUFFICIENT,
    STATE_OK,
    compute_health,
    load_report_payload,
    render_badge_svg,
)


def _payload(runs=10, counts=None) -> dict:
    counts = counts or {}
    return {
        "schema_version": 1,
        "generator": "flakeherd",
        "generated_at": "2026-10-25T00:00:00+00:00",
        "current_run_uid": "r1",
        "run_count": runs,
        "test_count": sum(counts.values()),
        "window": 30,
        "summary": counts,
        "gate_failures": [],
        "tests": [],
    }


def _counts(**kw) -> dict:
    """Map python-ish names to real class ids: stable_pass=4 -> 'stable-pass': 4."""
    return {k.replace("_", "-"): v for k, v in kw.items()}


class TestComputeHealth:
    def test_all_stable_is_100(self):
        health = compute_health(_payload(counts=_counts(stable_pass=8)))
        assert health.state == STATE_OK
        assert health.health_score == 100
        assert health.scored_tests == 8
        assert health.runs == 10

    def test_all_broken_is_0(self):
        health = compute_health(_payload(runs=6, counts=_counts(broken=4)))
        assert health.state == STATE_OK
        assert health.health_score == 0

    def test_mixed_weights(self):
        # 3 stable (3*100) + 1 flaky (0) = 300/4 = 75
        health = compute_health(_payload(counts=_counts(stable_pass=3, flaky=1)))
        assert health.health_score == 75

    def test_watch_and_suspected_weights(self):
        # 1 stable (100) + 1 watch (50) + 1 suspected-flaky (25) + 1 broken (0)
        # = 175/4 = 43.75 -> 44
        health = compute_health(
            _payload(counts=_counts(stable_pass=1, watch=1, suspected_flaky=1, broken=1))
        )
        assert health.health_score == 44

    def test_new_and_skip_only_are_excluded(self):
        # new/skip-only carry no verdict: excluded from numerator AND denominator
        health = compute_health(_payload(counts=_counts(stable_pass=2, new=5, skip_only=3)))
        assert health.scored_tests == 2
        assert health.health_score == 100

    def test_fixed_counts_as_healthy(self):
        health = compute_health(_payload(runs=7, counts=_counts(fixed=4)))
        assert health.health_score == 100

    def test_metric_version_and_timestamp_exposed(self):
        health = compute_health(_payload(counts=_counts(stable_pass=1)))
        assert health.metric_version == METRIC_VERSION == 1
        assert health.generated_at  # ISO timestamp always present

    def test_sample_sizes_exposed(self):
        health = compute_health(_payload(runs=12, counts=_counts(stable_pass=4, new=2)))
        assert health.runs == 12
        assert health.total_tests == 6
        assert health.scored_tests == 4


class TestInsufficientData:
    def test_empty_history_is_insufficient(self):
        health = compute_health(_payload(runs=0, counts=_counts(stable_pass=0)))
        assert health.state == STATE_INSUFFICIENT
        assert health.health_score is None

    def test_fewer_runs_than_minimum_is_insufficient(self):
        health = compute_health(_payload(runs=DEFAULT_MIN_RUNS - 1, counts=_counts(stable_pass=3)))
        assert health.state == STATE_INSUFFICIENT
        assert health.health_score is None

    def test_min_runs_boundary_is_ok(self):
        health = compute_health(_payload(runs=DEFAULT_MIN_RUNS, counts=_counts(stable_pass=3)))
        assert health.state == STATE_OK

    def test_custom_min_runs(self):
        health = compute_health(_payload(runs=2, counts=_counts(stable_pass=3)), min_runs=2)
        assert health.state == STATE_OK
        with pytest.raises(FlakeHerdError):
            compute_health(_payload(), min_runs=0)

    def test_only_unscored_tests_is_insufficient(self):
        health = compute_health(_payload(runs=50, counts=_counts(new=5)))
        assert health.state == STATE_INSUFFICIENT
        assert health.health_score is None


class TestPayload:
    def test_to_payload_shape(self):
        health = compute_health(_payload(counts=_counts(stable_pass=4, flaky=1)))
        payload = health.to_payload()
        assert payload["metric_version"] == 1
        assert payload["state"] == STATE_OK
        assert payload["health_score"] == 80
        assert payload["sample_size"] == {
            "runs": 10, "tests_total": 5, "tests_scored": 5,
        }
        assert payload["min_runs_required"] == DEFAULT_MIN_RUNS
        assert "probability" in payload["notes"].lower()

    def test_distribution_included(self):
        health = compute_health(_payload(counts=_counts(stable_pass=3, broken=2)))
        assert health.distribution == {"broken": 2, "stable-pass": 3}

    def test_rejects_non_report_payloads(self):
        with pytest.raises(FlakeHerdError):
            compute_health({"nope": 1})
        with pytest.raises(FlakeHerdError):
            compute_health([1, 2, 3])


class TestBadge:
    def test_ok_badge_has_score_and_accessibility(self):
        health = compute_health(_payload(counts=_counts(stable_pass=4, flaky=1)))  # 80
        svg = render_badge_svg(health)
        root = ET.fromstring(svg)  # valid XML
        ns = "{http://www.w3.org/2000/svg}"
        assert root.tag == ns + "svg"
        assert root.get("role") == "img"
        assert root.get("aria-label")
        assert root.find(ns + "title") is not None
        assert root.find(ns + "desc") is not None
        assert "80/100" in svg
        # accessible title repeats the value and the honesty caveat
        assert "heuristic" in (root.find(ns + "title").text or "")

    def test_insufficient_data_badge_says_so(self):
        health = compute_health(_payload(runs=1, counts=_counts(stable_pass=2)))
        assert health.state == STATE_INSUFFICIENT
        svg = render_badge_svg(health)
        assert "no data" in svg
        assert "/100" not in svg

    def test_band_colors(self):
        def band_for(**score_counts):
            health = compute_health(_payload(counts=_counts(**score_counts)))
            svg = render_badge_svg(health)
            for color in ("#1a7f37", "#9a6700", "#bc4c00", "#cb2431", "#6e7781"):
                if color in svg:
                    return color
            return None

        # green >= 80: 4 stable + 1 watch = 90
        assert band_for(stable_pass=4, watch=1) == "#1a7f37"
        # yellow: 3 stable + 1 flaky + 1 watch = 70
        assert band_for(stable_pass=3, flaky=1, watch=1) == "#9a6700"
        # orange: 1 stable + 1 watch + 1 suspected = 58.3 -> 58
        assert band_for(stable_pass=1, watch=1, suspected_flaky=1) == "#bc4c00"
        # red: 1 flaky + 1 broken = 0
        assert band_for(flaky=1, broken=1) == "#cb2431"
        # gray: insufficient data
        health = compute_health(_payload(runs=1))
        assert "#6e7781" in render_badge_svg(health)

    def test_label_and_value_escaped(self):
        health = compute_health(_payload(counts=_counts(stable_pass=1)))
        svg = render_badge_svg(health, label="a<b>&c")
        assert "a&lt;b&gt;&amp;c" in svg

    def test_badge_is_small(self):
        health = compute_health(_payload(counts=_counts(stable_pass=1)))
        svg = render_badge_svg(health)
        root = ET.fromstring(svg)
        assert int(root.get("height")) == 20
        assert 0 < int(root.get("width")) < 400


class TestLoadReport:
    def test_loads_real_report(self, tmp_path: Path):
        path = tmp_path / "report.json"
        path.write_text(json.dumps(_payload(counts=_counts(stable_pass=2))))
        payload = load_report_payload(path)
        assert payload["summary"] == {"stable-pass": 2}

    def test_missing_file_is_error(self, tmp_path: Path):
        with pytest.raises(FlakeHerdError, match="not found"):
            load_report_payload(tmp_path / "nope.json")

    def test_invalid_json_is_error(self, tmp_path: Path):
        path = tmp_path / "report.json"
        path.write_text("{nope")
        with pytest.raises(FlakeHerdError):
            load_report_payload(path)


class TestHealthCLI:
    @pytest.fixture(autouse=True)
    def _cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        yield tmp_path

    def _write_report(self, tmp_path, runs=10, **counts):
        path = tmp_path / "report.json"
        path.write_text(json.dumps(_payload(runs=runs, counts=_counts(**counts))))
        return "report.json"

    def test_health_prints_summary(self, tmp_path, capsys):
        self._write_report(tmp_path, stable_pass=4, flaky=1)
        assert main(["health", "--report", "report.json"]) == 0
        out = capsys.readouterr().out
        assert "80/100" in out
        assert "heuristic" in out
        assert "version 1" in out

    def test_health_writes_badge_and_json(self, tmp_path):
        self._write_report(tmp_path, stable_pass=4, flaky=1)
        rc = main([
            "health", "--report", "report.json",
            "--badge", "badge.svg", "--json-out", "health.json",
        ])
        assert rc == 0
        svg = (tmp_path / "badge.svg").read_text()
        assert "80/100" in svg
        payload = json.loads((tmp_path / "health.json").read_text())
        assert payload["health_score"] == 80
        assert payload["metric_version"] == 1
        assert payload["sample_size"]["runs"] == 10

    def test_health_badge_default_path(self, tmp_path):
        self._write_report(tmp_path, stable_pass=1)
        assert main(["health", "--report", "report.json", "--badge"]) == 0
        assert (tmp_path / "test-health.svg").exists()

    def test_insufficient_data_flow(self, tmp_path, capsys):
        self._write_report(tmp_path, runs=2, stable_pass=3)
        assert main([
            "health", "--report", "report.json",
            "--badge", "b.svg", "--json-out", "h.json",
        ]) == 0
        out = capsys.readouterr().out
        assert "insufficient-data" in out
        assert "n/a" in out
        svg = (tmp_path / "b.svg").read_text()
        assert "no data" in svg
        payload = json.loads((tmp_path / "h.json").read_text())
        assert payload["health_score"] is None
        assert payload["state"] == "insufficient-data"

    def test_missing_report_is_usage_error(self, tmp_path):
        assert main(["health", "--report", "missing.json"]) == 2

    def test_min_runs_option(self, tmp_path, capsys):
        self._write_report(tmp_path, runs=3, stable_pass=2)
        assert main(["health", "--report", "report.json"]) == 0
        assert "insufficient-data" in capsys.readouterr().out
        assert main([
            "health", "--report", "report.json", "--min-runs", "2",
        ]) == 0
        assert "100/100" in capsys.readouterr().out

    def test_custom_label(self, tmp_path):
        self._write_report(tmp_path, stable_pass=1)
        assert main([
            "health", "--report", "report.json",
            "--badge", "b.svg", "--label", "mytests",
        ]) == 0
        assert "mytests" in (tmp_path / "b.svg").read_text()
