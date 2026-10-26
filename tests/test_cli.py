"""CLI end-to-end tests: record, report, quarantine, init, exit codes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.conftest import junit_xml, make_result

from flakeherd.cli import main


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.delenv("GITHUB_REF_NAME", raising=False)
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    yield tmp_path


def _write_junit(tmp_path: Path, name: str, cases: list[dict]) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(junit_xml(cases), encoding="utf-8")
    return path


class TestRecord:
    def test_record_reports_and_creates_db(self, tmp_path, capsys):
        _write_junit(tmp_path, "results/junit.xml", [
            {"name": "test_a", "classname": "test_x", "status": "pass"},
            {"name": "test_b", "classname": "test_x", "status": "fail",
             "file": "tests/test_x.py", "line": 9},
        ])
        rc = main(["record", "-j", "results/junit.xml", "--sha", "s1", "--run-uid", "r1"])
        assert rc == 0
        assert (tmp_path / ".flakeherd/history.db").exists()
        out = capsys.readouterr().out
        assert "FlakeHerd report" in out
        assert "test_x::test_b" in out

    def test_two_runs_same_sha_yields_confirmed_flaky(self, tmp_path, capsys):
        _write_junit(tmp_path, "results/r1.xml", [
            {"name": "t", "classname": "c", "status": "fail"},
        ])
        _write_junit(tmp_path, "results/r2.xml", [
            {"name": "t", "classname": "c", "status": "pass"},
        ])
        assert main([
            "record", "-j", "results/r1.xml", "--sha", "same", "--run-uid", "u1", "--no-report",
        ]) == 0
        assert main([
            "record", "-j", "results/r2.xml", "--sha", "same", "--run-uid", "u2",
        ]) == 0
        out = capsys.readouterr().out
        assert "confirmed flaky" in out

    def test_no_matches_is_usage_error(self, tmp_path):
        rc = main(["record", "-j", "nothing-here/*.xml"])
        assert rc == 2

    def test_malformed_xml_is_usage_error(self, tmp_path):
        (tmp_path / "junit.xml").write_text("<unclosed>")
        assert main(["record", "-j", "junit.xml"]) == 2

    def test_json_out_written(self, tmp_path):
        _write_junit(tmp_path, "junit.xml", [
            {"name": "t", "classname": "c", "status": "pass"},
        ])
        rc = main([
            "record", "-j", "junit.xml", "--sha", "s", "--run-uid", "r",
            "--json-out", "report.json", "--no-report",
        ])
        assert rc == 0
        payload = json.loads((tmp_path / "report.json").read_text())
        assert payload["schema_version"] == 1
        assert payload["tests"][0]["test_key"] == "c::t"

    def test_fail_on_unexplained_exits_1_for_broken(self, tmp_path):
        _write_junit(tmp_path, "junit.xml", [
            {"name": "t", "classname": "c", "status": "fail"},
        ])
        rc = main([
            "record", "-j", "junit.xml", "--sha", "s", "--run-uid", "r",
            "--fail-on", "unexplained",
        ])
        assert rc == 1

    def test_fail_on_unexplained_excuses_proven_flaky(self, tmp_path):
        _write_junit(tmp_path, "junit.xml", [
            {"name": "t", "classname": "c", "status": "fail"},
        ])
        # earlier run, same sha, passed -> failure is a re-run divergence
        _seed_history(tmp_path, [("u1", "s", "pass")], name="t", classname="c")
        rc = main([
            "record", "-j", "junit.xml", "--sha", "s", "--run-uid", "u2",
            "--fail-on", "unexplained",
        ])
        assert rc == 0

    def test_no_prune_flag(self, tmp_path):
        _write_junit(tmp_path, "junit.xml", [
            {"name": "t", "classname": "c", "status": "pass"},
        ])
        rc = main(["record", "-j", "junit.xml", "--sha", "s", "--run-uid", "r", "--no-prune"])
        assert rc == 0


def _seed_history(
    tmp_path: Path,
    runs: list[tuple[str, str, str]],
    *,
    name: str = "test_x",
    classname: str | None = "test_mod.TestA",
) -> None:
    """Seed a history db directly (bypassing the CLI) with earlier runs."""
    from flakeherd.model import RunInfo
    from flakeherd.store import Store

    with Store(tmp_path / ".flakeherd/history.db") as store:
        for uid, sha, status in runs:
            store.record_run(
                RunInfo(run_uid=uid, commit_sha=sha, branch="main",
                        created_at=f"2026-01-01T00:00:{len(uid)}+00:00"),
                [make_result(name=name, classname=classname, status=status)],
            )


    def test_gate_judges_recorded_run_not_future_pulled_run(self, tmp_path):
        # a pulled/merged run with a skewed future timestamp must not hijack
        # the gate: the just-recorded failure still fails the job.
        from flakeherd.model import RunInfo
        from flakeherd.store import Store

        with Store(tmp_path / ".flakeherd/history.db") as store:
            store.record_run(
                RunInfo(run_uid="future", commit_sha="zzz", branch="main",
                        created_at="2030-01-01T00:00:00+00:00"),
                [make_result(name="t", classname="c", status="pass")],
            )
        _write_junit(tmp_path, "junit.xml", [
            {"name": "t", "classname": "c", "status": "fail"},
        ])
        rc = main([
            "record", "-j", "junit.xml", "--sha", "sha-now", "--run-uid", "now",
            "--fail-on", "unexplained",
        ])
        assert rc == 1

    def test_unknown_sha_flip_does_not_excuse_failure(self, tmp_path):
        # two local runs with unknown shas: pass then fail is NOT proven
        # flakiness; the gate must still fail.
        _seed_history(tmp_path, [("u1", "unknown", "pass")], name="t", classname="c")
        _write_junit(tmp_path, "junit.xml", [
            {"name": "t", "classname": "c", "status": "fail"},
        ])
        rc = main([
            "record", "-j", "junit.xml", "--sha", "unknown", "--run-uid", "u2",
            "--fail-on", "unexplained",
        ])
        assert rc == 1

    def test_json_out_to_nested_directory(self, tmp_path):
        _write_junit(tmp_path, "junit.xml", [
            {"name": "t", "classname": "c", "status": "pass"},
        ])
        rc = main([
            "record", "-j", "junit.xml", "--sha", "s", "--run-uid", "r",
            "--json-out", "nested/dir/report.json", "--no-report",
        ])
        assert rc == 0
        assert (tmp_path / "nested" / "dir" / "report.json").exists()


class TestReport:
    def test_report_after_record(self, tmp_path, capsys):
        _write_junit(tmp_path, "junit.xml", [
            {"name": "t", "classname": "c", "status": "pass"},
        ])
        main(["record", "-j", "junit.xml", "--sha", "s", "--run-uid", "r", "--no-report"])
        capsys.readouterr()
        assert main(["report"]) == 0
        assert "FlakeHerd report" in capsys.readouterr().out

    def test_report_without_db_fails(self, tmp_path):
        assert main(["report"]) == 2

    def test_config_window_override(self, tmp_path, capsys):
        (tmp_path / ".flakeherd.json").write_text(json.dumps({"window": 2}))
        for i in range(4):
            _seed_history(tmp_path, [(f"u{i}", f"s{i}", "pass")])
        assert main(["report"]) == 0


class TestQuarantineCmd:
    def test_writes_manifest_and_roundtrips(self, tmp_path, capsys):
        # two runs, same sha -> confirmed-flaky
        _seed_history(tmp_path, [("u1", "s", "fail")])
        _seed_history(tmp_path, [("u2", "s", "pass")])
        assert main(["quarantine"]) == 0
        manifest = tmp_path / ".flakeherd/quarantine.json"
        data = json.loads(manifest.read_text())
        assert data["quarantined"][0]["name"] == "test_x"
        out = capsys.readouterr().out
        assert "quarantined test(s)" in out

    def test_dry_run_writes_nothing(self, tmp_path, capsys):
        _seed_history(tmp_path, [("u1", "s", "fail"), ("u2", "s", "pass")])
        assert main(["quarantine", "--dry-run"]) == 0
        assert not (tmp_path / ".flakeherd/quarantine.json").exists()

    def test_min_score_can_exclude_everything(self, tmp_path):
        _seed_history(tmp_path, [("u1", "s", "fail"), ("u2", "s", "pass")])
        rc = main(["quarantine", "--min-score", "100"])
        assert rc == 0
        data = json.loads((tmp_path / ".flakeherd/quarantine.json").read_text())
        assert data["quarantined"] == []


class TestInit:
    def test_writes_example_config(self, tmp_path):
        assert main(["init"]) == 0
        cfg = json.loads((tmp_path / ".flakeherd.json").read_text())
        assert cfg["window"] == 30

    def test_refuses_overwrite_without_force(self, tmp_path):
        assert main(["init"]) == 0
        assert main(["init"]) == 2
        assert main(["init", "--force"]) == 0


class TestMisc:
    def test_version_string(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0
        assert "flakeherd" in capsys.readouterr().out

    def test_parser_requires_subcommand(self):
        with pytest.raises(SystemExit):
            main([])

    def test_db_env_var_respected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLAKEHERD_DB", str(tmp_path / "custom/h.db"))
        _write_junit(tmp_path, "junit.xml", [
            {"name": "t", "classname": "c", "status": "pass"},
        ])
        rc = main(["record", "-j", "junit.xml", "--sha", "s", "--run-uid", "r", "--no-report"])
        assert rc == 0
        assert (tmp_path / "custom/h.db").exists()


class TestQuarantinePolicyCLI:
    """record --auto-quarantine + suggestion mode + policy enforcement."""

    def _seed_flaky(self, tmp_path, runs=6):
        # 6 runs: alternating P/F -> flaky with 6 samples
        for i in range(runs):
            status = "pass" if i % 2 == 0 else "fail"
            _seed_history(tmp_path, [(f"u{i}", f"s{i}", status)])

    def _write_junit_pass(self, tmp_path):
        _write_junit(tmp_path, "junit.xml", [
            {"name": "test_x", "classname": "test_mod.TestA", "status": "pass"},
        ])

    def test_policy_suggestions_in_record_report(self, tmp_path, capsys):
        (tmp_path / ".flakeherd.json").write_text(json.dumps({
            "quarantine_threshold_enabled": True,
        }))
        self._seed_flaky(tmp_path)
        self._write_junit_pass(tmp_path)
        rc = main([
            "record", "-j", "junit.xml", "--sha", "sx", "--run-uid", "now",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "suggestions only" in out
        assert "audit:" in out
        # suggestion mode writes no manifest
        assert not (tmp_path / ".flakeherd/quarantine.json").exists()

    def test_policy_suggestions_in_json_report(self, tmp_path):
        (tmp_path / ".flakeherd.json").write_text(json.dumps({
            "quarantine_threshold_enabled": True,
        }))
        self._seed_flaky(tmp_path)
        self._write_junit_pass(tmp_path)
        rc = main([
            "record", "-j", "junit.xml", "--sha", "sx", "--run-uid", "now",
            "--json-out", "report.json", "--no-report",
        ])
        assert rc == 0
        payload = json.loads((tmp_path / "report.json").read_text())
        suggestions = payload.get("quarantine_suggestions", [])
        assert any(s["cls"] == "flaky" for s in suggestions)
        assert all("audit_reason" in s for s in suggestions)

    def test_auto_quarantine_requires_policy(self, tmp_path):
        self._seed_flaky(tmp_path)
        self._write_junit_pass(tmp_path)
        rc = main([
            "record", "-j", "junit.xml", "--sha", "sx", "--run-uid", "now",
            "--auto-quarantine", "--no-report",
        ])
        assert rc == 2  # usage error: policy not enabled

    def test_auto_quarantine_writes_manifest(self, tmp_path, capsys):
        (tmp_path / ".flakeherd.json").write_text(json.dumps({
            "quarantine_threshold_enabled": True,
        }))
        self._seed_flaky(tmp_path)
        self._write_junit_pass(tmp_path)
        rc = main([
            "record", "-j", "junit.xml", "--sha", "sx", "--run-uid", "now",
            "--auto-quarantine", "--no-report",
        ])
        assert rc == 0
        manifest = json.loads((tmp_path / ".flakeherd/quarantine.json").read_text())
        assert manifest["quarantined"]
        assert "policy=threshold" in manifest["quarantined"][0]["audit_reason"]
        err = capsys.readouterr().err
        assert "NOT committed or pushed" in err

    def test_auto_quarantine_enforces_min_samples(self, tmp_path):
        (tmp_path / ".flakeherd.json").write_text(json.dumps({
            "quarantine_threshold_enabled": True,
        }))
        # only 2 runs of history: confirmed-flaky but insufficient evidence
        _seed_history(tmp_path, [("u1", "s", "fail")])
        _seed_history(tmp_path, [("u2", "s", "pass")])
        self._write_junit_pass(tmp_path)
        rc = main([
            "record", "-j", "junit.xml", "--sha", "sx", "--run-uid", "now",
            "--auto-quarantine", "--no-report",
        ])
        assert rc == 0
        manifest = json.loads((tmp_path / ".flakeherd/quarantine.json").read_text())
        assert manifest["quarantined"] == []  # not enough history

    def test_quarantine_cmd_respects_policy_threshold(self, tmp_path):
        (tmp_path / ".flakeherd.json").write_text(json.dumps({
            "quarantine_threshold_enabled": True,
        }))
        self._seed_flaky(tmp_path)
        assert main(["quarantine"]) == 0
        manifest = json.loads((tmp_path / ".flakeherd/quarantine.json").read_text())
        # flaky scores 80 >= 40 with 6 samples >= 5 -> quarantined
        assert len(manifest["quarantined"]) == 1

    def test_quarantine_cmd_expiry_days(self, tmp_path):
        from datetime import date, timedelta

        self._seed_flaky(tmp_path)
        rc = main(["quarantine", "--expiry-days", "7"])
        assert rc == 0
        manifest = json.loads((tmp_path / ".flakeherd/quarantine.json").read_text())
        expected = (date.today() + timedelta(days=7)).isoformat()
        assert manifest["quarantined"][0]["expires_at"] == expected

    def test_quarantine_cmd_expiry_zero_means_never(self, tmp_path):
        self._seed_flaky(tmp_path)
        assert main(["quarantine", "--expiry-days", "0"]) == 0
        manifest = json.loads((tmp_path / ".flakeherd/quarantine.json").read_text())
        assert manifest["quarantined"][0]["expires_at"] is None

    def test_quarantine_dry_run_shows_audit_and_expiry(self, tmp_path, capsys):
        self._seed_flaky(tmp_path)
        assert main(["quarantine", "--dry-run", "--expiry-days", "3"]) == 0
        out = capsys.readouterr().out
        assert "audit:" in out
        assert "expires:" in out
        assert not (tmp_path / ".flakeherd/quarantine.json").exists()

    def test_invalid_min_samples_config_rejected(self, tmp_path):
        (tmp_path / ".flakeherd.json").write_text(json.dumps({
            "quarantine_min_samples": 0,
        }))
        self._write_junit_pass(tmp_path)
        rc = main(["record", "-j", "junit.xml", "--sha", "s", "--run-uid", "r"])
        assert rc == 2

    def test_retry_evidence_in_record_json(self, tmp_path):
        # JUnit XML carrying the flakeherd retry property contract
        xml = junit_xml([{"name": "t", "classname": "c", "status": "pass"}])
        xml = xml.replace(
            "</testcase>",
            '<properties><property name="flakeherd_retry_attempts" value="2"/>'
            '<property name="flakeherd_retry_outcomes" value="F,P"/>'
            '<property name="flakeherd_retry_first_failure" '
            'value="AssertionError: transient"/></properties></testcase>',
        )
        (tmp_path / "junit.xml").write_text(xml)
        rc = main([
            "record", "-j", "junit.xml", "--sha", "s", "--run-uid", "r",
            "--json-out", "report.json", "--no-report",
        ])
        assert rc == 0
        payload = json.loads((tmp_path / "report.json").read_text())
        test = payload["tests"][0]
        assert test["current_run"]["retry_attempts"] == 2
        assert test["current_run"]["retry_outcomes"] == "F,P"
        assert "transient" in test["current_run"]["retry_first_failure_excerpt"]
        assert test["cls"] == "suspected-flaky"
