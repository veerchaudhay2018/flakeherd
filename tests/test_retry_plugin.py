"""Auto-retry plugin tests - real pytest runs via pytester.

Every scenario asserted here runs a real inner pytest session with
``--junitxml`` and inspects the actual generated XML, so the evidence
contract (properties + per-attempt testcase elements) is pinned against
reality, not against what we *hope* the junitxml plugin writes.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest

pytest_plugins = ["pytester"]


@pytest.fixture(autouse=True)
def _hermetic_retry_plugin(monkeypatch):
    # Same hermetic pattern as test_pytest_plugin.py: no plugin autoload,
    # plugin loaded explicitly, so results are identical with and without
    # flakeherd being pip-installed in the outer environment.
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PYTEST_PLUGINS", "flakeherd.pytest_plugin")


def _testcases(junit_xml_path):
    """Parse a junit file, return {nodeid-ish key: [elements]}."""
    tree = ET.parse(junit_xml_path)
    cases = {}
    for elem in tree.iter("testcase"):
        key = f"{elem.get('classname')}::{elem.get('name')}"
        cases.setdefault(key, []).append(elem)
    return cases


def _props(elem):
    return {
        p.get("name"): p.get("value")
        for p in elem.iter("property")
    }


class TestPassFirst:
    def test_no_retry_metadata_when_passing_first(self, pytester):
        pytester.makepyfile(test_x="def test_ok():\n    assert True\n")
        result = pytester.runpytest("--flakeherd-retries", "2", "--junitxml=junit.xml")
        result.assert_outcomes(passed=1)
        cases = _testcases(pytester.path / "junit.xml")
        assert len(cases["test_x::test_ok"]) == 1
        assert _props(cases["test_x::test_ok"][0]) == {}


class TestFailThenPass:
    def test_recovered_test_reports_attempts_and_outcomes(self, pytester):
        pytester.makepyfile(
            test_x="""
            calls = []
            def test_flaky():
                calls.append(1)
                assert len(calls) >= 2, "first attempt fails"
            """
        )
        result = pytester.runpytest("--flakeherd-retries", "1", "--junitxml=junit.xml")
        result.assert_outcomes(passed=1, failed=1)
        cases = _testcases(pytester.path / "junit.xml")
        elements = cases["test_x::test_flaky"]
        assert len(elements) == 2  # one element per attempt
        # The original failure trace is preserved on the first element.
        first = elements[0]
        assert first.find("failure") is not None
        assert "first attempt fails" in (first.find("failure").get("message") or "")
        # The final element carries the retry evidence contract.
        props = _props(elements[-1])
        assert props["flakeherd_retry_attempts"] == "2"
        assert props["flakeherd_retry_outcomes"] == "F,P"
        assert "first attempt fails" in props["flakeherd_retry_first_failure"]

    def test_recovered_test_parses_to_suspected_flaky(self, pytester, tmp_path):
        """End-to-end: XML -> parser -> classification = suspected-flaky."""
        from flakeherd.config import Config
        from flakeherd.detector import analyze
        from flakeherd.junit import parse_files
        from flakeherd.model import RunInfo
        from flakeherd.store import Store

        pytester.makepyfile(
            test_x="""
            calls = []
            def test_flaky():
                calls.append(1)
                assert len(calls) >= 2, "first attempt fails"
            """
        )
        pytester.runpytest("--flakeherd-retries", "1", "--junitxml=junit.xml")
        report = parse_files([pytester.path / "junit.xml"])
        parsed = report.results["test_x::test_flaky"]
        assert parsed.status == "pass"
        assert parsed.flaky_hint is True
        assert parsed.retry_attempts == 2
        assert parsed.retry_outcomes == "F,P"
        assert parsed.retry_first_failure

        db = tmp_path / "hist.db"
        with Store(db) as store:
            store.record_run(
                RunInfo(run_uid="r1", commit_sha="s1", branch="main"),
                report.results.values(),
            )
            result = analyze(store, Config(), current_run_uid="r1")
        c = next(c for c in result.classifications if c.test_key == "test_x::test_flaky")
        assert c.cls == "suspected-flaky"
        assert c.cls != "confirmed-flaky"  # suspicion, not proof
        assert c.retry_attempts == 2
        assert c.retry_outcomes == "F,P"


class TestRepeatedFailures:
    def test_exhausted_retries_stay_failed(self, pytester):
        pytester.makepyfile(test_x="def test_broken():\n    assert False, 'genuine bug'\n")
        result = pytester.runpytest("--flakeherd-retries", "2", "--junitxml=junit.xml")
        result.assert_outcomes(failed=3)  # initial + 2 retries, all logged
        cases = _testcases(pytester.path / "junit.xml")
        elements = cases["test_x::test_broken"]
        assert len(elements) == 3
        final = elements[-1]
        assert final.find("failure") is not None  # final outcome: FAIL
        props = _props(final)
        assert props["flakeherd_retry_attempts"] == "3"
        assert props["flakeherd_retry_outcomes"] == "F,F,F"
        assert "genuine bug" in props["flakeherd_retry_first_failure"]

    def test_exhausted_retries_gate_on_second_run(self, pytester, tmp_path):
        """A test that failed all attempts must not be excused by the gate."""
        from flakeherd.config import Config
        from flakeherd.detector import analyze
        from flakeherd.junit import parse_files
        from flakeherd.model import RunInfo
        from flakeherd.store import Store

        pytester.makepyfile(test_x="def test_broken():\n    assert False\n")
        pytester.runpytest("--flakeherd-retries", "1", "--junitxml=junit.xml")
        report = parse_files([pytester.path / "junit.xml"])
        parsed = report.results["test_x::test_broken"]
        assert parsed.status == "fail"  # never masked as a pass
        assert parsed.flaky_hint is False  # no recovery -> no flaky evidence
        assert parsed.retry_outcomes == "F,F"

        db = tmp_path / "hist.db"
        with Store(db) as store:
            store.record_run(
                RunInfo(run_uid="r1", commit_sha="s1", branch="main"),
                report.results.values(),
            )
            result = analyze(store, Config(), current_run_uid="r1")
        c = next(c for c in result.classifications if c.test_key == "test_x::test_broken")
        assert [c.test_key] == [g.test_key for g in result.gate_failures()]

    def test_retry_cap_is_respected(self, pytester):
        pytester.makepyfile(
            test_x="""
            calls = []
            def test_never_passes():
                calls.append(1)
                assert False
            """
        )
        result = pytester.runpytest("--flakeherd-retries", "3", "--junitxml=junit.xml")
        result.assert_outcomes(failed=4)
        cases = _testcases(pytester.path / "junit.xml")
        assert len(cases["test_x::test_never_passes"]) == 4
        assert _props(cases["test_x::test_never_passes"][-1])[
            "flakeherd_retry_attempts"
        ] == "4"


class TestInterruption:
    def test_keyboard_interrupt_aborts_and_is_not_swallowed(self, pytester):
        pytester.makepyfile(
            test_x="""
            calls = []
            def test_interrupts_on_second_attempt():
                calls.append(1)
                if len(calls) == 2:
                    raise KeyboardInterrupt("stop")
                assert False, "first attempt fails"
            """
        )
        result = pytester.runpytest_subprocess(
            "--flakeherd-retries", "1", "--junitxml=junit.xml"
        )
        # The interrupt aborts the session (pytest exit code 2 = interrupted);
        # our retry loop must never swallow it into a pass/fail outcome.
        assert result.ret == 2

    def test_interrupted_run_keeps_first_attempt_evidence(self, pytester):
        pytester.makepyfile(
            test_x="""
            calls = []
            def test_interrupts():
                calls.append(1)
                if len(calls) == 2:
                    raise KeyboardInterrupt("stop")
                assert False, "first attempt fails"
            """
        )
        pytester.runpytest_subprocess(
            "--flakeherd-retries", "1", "--junitxml=junit.xml"
        )
        junit = pytester.path / "junit.xml"
        assert junit.exists()
        cases = _testcases(junit)
        elements = cases.get("test_x::test_interrupts", [])
        # At least the first attempt's failure trace survived.
        assert any(e.find("failure") is not None for e in elements)


class TestMissingEvidence:
    def test_plain_run_has_no_retry_metadata(self, pytester):
        # No --flakeherd-retries: identical to vanilla pytest, nothing extra.
        pytester.makepyfile(test_x="def test_ok():\n    assert True\n")
        result = pytester.runpytest("--junitxml=junit.xml")
        result.assert_outcomes(passed=1)
        cases = _testcases(pytester.path / "junit.xml")
        assert _props(cases["test_x::test_ok"][0]) == {}

    def test_default_off_via_ini_zero(self, pytester):
        pytester.makeini(
            """
            [pytest]
            flakeherd_retries = 0
            """
        )
        pytester.makepyfile(test_x="def test_broken():\n    assert False\n")
        result = pytester.runpytest("--junitxml=junit.xml")
        result.assert_outcomes(failed=1)  # no retries
        cases = _testcases(pytester.path / "junit.xml")
        assert len(cases["test_x::test_broken"]) == 1

    def test_zero_flag_disables_retries(self, pytester):
        pytester.makepyfile(test_x="def test_broken():\n    assert False\n")
        result = pytester.runpytest("--flakeherd-retries", "0", "--junitxml=junit.xml")
        result.assert_outcomes(failed=1)

    def test_quarantined_test_is_not_retried(self, pytester):
        # A quarantined (skipped) test must never enter the retry loop.
        manifest = {
            "schema_version": 1,
            "generator": "t",
            "generated_at": "t",
            "quarantined": [
                {
                    "test_key": "test_x::test_quarantined",
                    "name": "test_quarantined",
                    "classname": "test_x",
                    "file": None,
                    "cls": "confirmed-flaky",
                    "score": 80,
                    "failure_rate": 0.4,
                    "samples": 10,
                    "reason": "proven flaky",
                }
            ],
        }
        (pytester.path / ".flakeherd").mkdir()
        (pytester.path / ".flakeherd" / "quarantine.json").write_text(
            json.dumps(manifest)
        )
        pytester.makepyfile(
            test_x="""
            calls = []
            def test_quarantined():
                calls.append(1)
                assert False
            """
        )
        result = pytester.runpytest("--flakeherd-retries", "3", "--junitxml=junit.xml")
        result.assert_outcomes(skipped=1)
        cases = _testcases(pytester.path / "junit.xml")
        assert len(cases["test_x::test_quarantined"]) == 1


class TestConfiguration:
    def test_ini_setting_configures_retries(self, pytester):
        pytester.makeini(
            """
            [pytest]
            flakeherd_retries = 1
            """
        )
        pytester.makepyfile(
            test_x="""
            calls = []
            def test_recovers():
                calls.append(1)
                assert len(calls) >= 2
            """
        )
        result = pytester.runpytest("--junitxml=junit.xml")
        result.assert_outcomes(passed=1, failed=1)
        cases = _testcases(pytester.path / "junit.xml")
        assert _props(cases["test_x::test_recovers"][-1])[
            "flakeherd_retry_outcomes"
        ] == "F,P"

    def test_cli_flag_overrides_ini(self, pytester):
        pytester.makeini(
            """
            [pytest]
            flakeherd_retries = 3
            """
        )
        pytester.makepyfile(test_x="def test_broken():\n    assert False\n")
        result = pytester.runpytest("--flakeherd-retries", "0", "--junitxml=junit.xml")
        result.assert_outcomes(failed=1)

    def test_negative_retries_is_usage_error(self, pytester):
        pytester.makepyfile(test_x="def test_ok():\n    assert True\n")
        result = pytester.runpytest_subprocess("--flakeherd-retries", "-1")
        assert result.ret == 4  # pytest usage error exit code
        result.stderr.fnmatch_lines(["*flakeherd-retries*>= 0*"])

    def test_negative_ini_value_is_usage_error(self, pytester):
        pytester.makeini(
            """
            [pytest]
            flakeherd_retries = -2
            """
        )
        pytester.makepyfile(test_x="def test_ok():\n    assert True\n")
        result = pytester.runpytest_subprocess()
        assert result.ret == 4


class TestTeardownAndSetup:
    def test_setup_failure_is_retried(self, pytester):
        pytester.makepyfile(
            test_x="""
            import pytest
            calls = []

            @pytest.fixture
            def flaky_fixture():
                calls.append(1)
                if len(calls) == 1:
                    raise RuntimeError("fixture exploded")
                return 42

            def test_with_flaky_fixture(flaky_fixture):
                assert flaky_fixture == 42
            """
        )
        result = pytester.runpytest("--flakeherd-retries", "1", "--junitxml=junit.xml")
        # attempt 1: setup error; attempt 2: pass
        result.assert_outcomes(passed=1, errors=1)
        cases = _testcases(pytester.path / "junit.xml")
        props = _props(cases["test_x::test_with_flaky_fixture"][-1])
        assert props["flakeherd_retry_attempts"] == "2"
        assert props["flakeherd_retry_outcomes"] == "F,P"

    def test_teardown_failure_is_not_retried(self, pytester):
        pytester.makepyfile(
            test_x="""
            import pytest

            @pytest.fixture
            def bad_teardown():
                yield
                raise RuntimeError("teardown bug")

            def test_body_ok(bad_teardown):
                assert True
            """
        )
        result = pytester.runpytest("--flakeherd-retries", "2", "--junitxml=junit.xml")
        # the test body passed; the teardown error is reported once, no retry
        result.assert_outcomes(passed=1, errors=1)
        cases = _testcases(pytester.path / "junit.xml")
        assert len(cases["test_x::test_body_ok"]) >= 1
        final_props = _props(cases["test_x::test_body_ok"][-1])
        # outcomes record exactly one attempt (teardown failure does not retry)
        if "flakeherd_retry_attempts" in final_props:
            assert final_props["flakeherd_retry_attempts"] == "1"
