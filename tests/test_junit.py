"""JUnit XML parser tests: formats, edge cases, safety, duplicate merging."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest
from tests.conftest import junit_xml, make_result

from flakeherd.errors import ParseError
from flakeherd.junit import (
    MAX_XML_BYTES,
    _merge_into,
    discover,
    is_ignored,
    parse_file,
    parse_files,
)


def _parse_one(xml: str, tmp_path: Path):
    path = tmp_path / "junit.xml"
    path.write_text(xml, encoding="utf-8")
    report = parse_files([path])
    return report


class TestFormats:
    def test_pytest_style_testsuites(self, tmp_path):
        xml = junit_xml([
            {
                "name": "test_a", "classname": "test_x", "status": "pass",
                "file": "tests/test_x.py", "line": 5,
            },
            {
                "name": "test_b", "classname": "test_x", "status": "fail",
                "file": "tests/test_x.py", "line": 9,
            },
        ])
        report = _parse_one(xml, tmp_path)
        assert set(report.results) == {
            "test_x::test_a",
            "test_x::test_b",
        }
        assert report.results["test_x::test_b"].status == "fail"
        assert report.results["test_x::test_b"].line == 9

    def test_bare_testsuite_root(self, tmp_path):
        xml = junit_xml([{"name": "test_a", "status": "pass"}], root="testsuite")
        report = _parse_one(xml, tmp_path)
        assert "test_a" in report.results

    def test_jest_style_classpath(self, tmp_path):
        # jest-junit emits classname="path/to/file.ts" with no children on pass
        xml = junit_xml(
            [
                {
                    "name": "sums two numbers", "classname": "src/math.test.ts",
                    "status": "pass", "time": 0.004,
                },
                {"name": "divides", "classname": "src/math.test.ts", "status": "fail"},
            ]
        )
        report = _parse_one(xml, tmp_path)
        assert report.results["src/math.test.ts::divides"].status == "fail"

    def test_go_style_package_classname(self, tmp_path):
        xml = junit_xml(
            [{"name": "TestRetry", "classname": "github.com/acme/pkg/retry", "status": "pass"}]
        )
        report = _parse_one(xml, tmp_path)
        assert "github.com/acme/pkg/retry::TestRetry" in report.results

    def test_namespaced_document(self, tmp_path):
        xml = (
            '<?xml version="1.0"?>'
            '<testsuites xmlns="http://junit.org/schema">'
            '<testsuite><testcase classname="c" name="n"/></testsuite></testsuites>'
        )
        report = _parse_one(xml, tmp_path)
        assert report.results["c::n"].status == "pass"

    def test_status_attribute_variants(self, tmp_path):
        xml = junit_xml([{"name": "t1", "status": "pass"}, {"name": "t2", "status": "skip"}])
        xml = xml.replace(
            '<testcase name="t1">', '<testcase name="t1" status="failed">'
        ).replace(
            '<testcase name="t2">', '<testcase name="t2" status="skipped">'
        )
        report = _parse_one(xml, tmp_path)
        assert report.results["t1"].status == "fail"
        assert report.results["t2"].status == "skip"

    def test_error_child_means_fail(self, tmp_path):
        xml = junit_xml([{"name": "t", "status": "pass"}]).replace(
            "</testcase>", '<error message="segv"/></testcase>'
        )
        report = _parse_one(xml, tmp_path)
        assert report.results["t"].status == "fail"

    def test_rerun_failure_child_is_a_failed_attempt(self, tmp_path):
        # 0.2.0 fix (verified against real pytest-rerunfailures output): a
        # <rerunFailure>-only element records a FAILED attempt that was
        # retried. Parsing it as a pass let a test that failed every retry
        # be merged with its failing final outcome and recorded as green.
        xml = junit_xml([{"name": "t", "status": "pass", "flaky_child": True}])
        report = _parse_one(xml, tmp_path)
        assert report.results["t"].flaky_hint is True
        assert report.results["t"].status == "fail"

    def test_flaky_failure_child_is_a_recovered_flake(self, tmp_path):
        # Maven Surefire semantics: <flakyFailure> marks a test that failed
        # an earlier attempt but ultimately passed - the build stays green.
        xml = junit_xml([{"name": "t", "status": "pass"}]).replace(
            "</testcase>",
            '<flakyFailure message="assert">tb</flakyFailure></testcase>',
        )
        report = _parse_one(xml, tmp_path)
        assert report.results["t"].status == "pass"
        assert report.results["t"].flaky_hint is True

    def test_real_rerunfailures_fail_then_fail_stays_failed(self, tmp_path):
        # Reproduces the exact XML pytest-rerunfailures 16.x emits for a test
        # that failed both its attempt and its final run: the rerun attempt
        # is a BARE testcase element (indistinguishable from a pass) followed
        # by the final failing element. The last element must win.
        xml = (
            '<?xml version="1.0"?><testsuites><testsuite name="pytest">'
            '<testcase classname="c" name="t" time="0.000"/>'
            '<testcase classname="c" name="t" time="0.000">'
            '<failure message="AssertionError">boom</failure>'
            "</testcase>"
            "</testsuite></testsuites>"
        )
        report = _parse_one(xml, tmp_path)
        merged = report.results["c::t"]
        assert merged.status == "fail"
        assert merged.flaky_hint is True  # divergence recorded as evidence

    def test_real_rerunfailures_fail_then_pass_is_pass(self, tmp_path):
        # A bare failed attempt followed by a bare final pass: outcome pass;
        # without explicit retry properties no hint can be derived (the two
        # elements look identical) - documented limitation, outcome correct.
        xml = (
            '<?xml version="1.0"?><testsuites><testsuite name="pytest">'
            '<testcase classname="c" name="t" time="0.045"/>'
            '<testcase classname="c" name="t" time="0.000"/>'
            "</testsuite></testsuites>"
        )
        report = _parse_one(xml, tmp_path)
        assert report.results["c::t"].status == "pass"
        assert report.results["c::t"].flaky_hint is False


class TestSafety:
    def test_oversized_file_rejected(self, tmp_path, monkeypatch):
        path = tmp_path / "huge.xml"
        path.write_text("<testsuites/>", encoding="utf-8")
        monkeypatch.setattr("flakeherd.junit.MAX_XML_BYTES", 4)
        with pytest.raises(ParseError, match="exceeds"):
            list(parse_file(path, max_bytes=4))

    def test_malformed_xml_rejected(self, tmp_path):
        with pytest.raises(ParseError, match="malformed"):
            _parse_one("<unclosed>", tmp_path)

    def test_missing_file_rejected(self, tmp_path):
        with pytest.raises(ParseError, match="not found"):
            parse_files([tmp_path / "nope.xml"])

    def test_gzip_reports_supported(self, tmp_path):
        import gzip as gz

        raw = junit_xml([{"name": "t", "status": "pass"}]).encode()
        path = tmp_path / "junit.xml.gz"
        with gz.open(path, "wb") as fh:
            fh.write(raw)
        report = parse_files([path])
        assert "t" in report.results

    def test_default_cap_is_sane(self):
        assert MAX_XML_BYTES == 64 * 1024 * 1024

    def test_gzip_bomb_rejected_by_decompressed_cap(self, tmp_path):
        # a tiny compressed file expanding far past the cap must be rejected
        big = b"<testsuite>" + b"<testcase classname='c' name='t'/>" * 5000 + b"</testsuite>"
        path = tmp_path / "bomb.xml.gz"
        with gzip.open(path, "wb") as fh:
            fh.write(big)
        assert path.stat().st_size < 1024  # compressed size is small...
        with pytest.raises(ParseError, match="decompressed"):
            parse_files([path], max_bytes=1024)  # ...but the cap sees through it

    def test_case_without_name_is_skipped_with_warning(self, tmp_path):
        xml = (
            '<testsuites><testsuite>'
            '<testcase classname="c"/><testcase name="ok"/>'
            '</testsuite></testsuites>'
        )
        report = _parse_one(xml, tmp_path)
        assert list(report.results) == ["ok"]
        assert any("without a 'name'" in w for w in report.warnings)


class TestDuplicates:
    def test_same_run_pass_and_fail_becomes_pass_with_hint(self, tmp_path):
        xml = junit_xml(
            [
                {"name": "t", "classname": "c", "status": "fail", "line": 9},
                {"name": "t", "classname": "c", "status": "pass", "line": 9},
            ]
        )
        report = _parse_one(xml, tmp_path)
        merged = report.results["c::t"]
        assert merged.status == "pass"
        assert merged.flaky_hint is True

    def test_duplicates_fail_wins_without_hint(self, tmp_path):
        xml = junit_xml(
            [
                {"name": "t", "classname": "c", "status": "fail"},
                {"name": "t", "classname": "c", "status": "skip"},
            ]
        )
        report = _parse_one(xml, tmp_path)
        assert report.results["c::t"].status == "fail"
        assert report.results["c::t"].flaky_hint is False

    def test_merge_into_direct(self):
        results: dict = {}
        _merge_into(results, make_result(status="fail"))
        _merge_into(results, make_result(status="pass"))
        assert results["test_mod.TestA::test_x"].status == "pass"
        assert results["test_mod.TestA::test_x"].flaky_hint is True


class TestDiscover:
    def test_glob_recursion(self, tmp_path):
        (tmp_path / "a" / "b").mkdir(parents=True)
        (tmp_path / "junit1.xml").write_text("<x/>")
        (tmp_path / "a" / "junit2.xml").write_text("<x/>")
        (tmp_path / "a" / "b" / "junit3.xml").write_text("<x/>")
        (tmp_path / "a" / "b" / "other.txt").write_text("no")
        found = discover(["**/junit*.xml"], base_dir=tmp_path)
        assert len(found) == 3

    def test_no_match_is_empty_not_error(self, tmp_path):
        assert discover(["**/nope*.xml"], base_dir=tmp_path) == []


class TestIgnorePatterns:
    def test_ignore_matches_key_name_classname(self):
        assert is_ignored("Slow::test_big", "test_big", "Slow", ["Slow::*"])
        assert is_ignored("X::test_big", "test_big", None, ["test_big"])
        assert is_ignored("X::test_big", "test_big", "Y", ["y*"])
        assert not is_ignored("X::test_big", "test_big", "Y", ["zzz*"])


def test_gzip_bad_file(tmp_path):
    path = tmp_path / "bad.xml.gz"
    path.write_bytes(b"not gzip at all")
    with pytest.raises(ParseError):
        parse_files([path])


def test_gzip_module_importable():
    # guard the unused-import warning: junit.py legitimately needs gzip
    import flakeherd.junit as j

    assert hasattr(j, "parse_file")
