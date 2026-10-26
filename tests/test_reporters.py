"""Reporter tests: annotation escaping, caps, ordering, markdown, JSON."""

from __future__ import annotations

import json

from tests.conftest import make_history

from flakeherd.config import Config
from flakeherd.detector import AnalysisResult, classify
from flakeherd.report.github import (
    annotations,
    append_step_summary,
    format_annotation,
    in_github_actions,
    summary_markdown,
)
from flakeherd.report.json_report import build_payload
from flakeherd.report.terminal import render


def _classification(pattern, key="test_mod.TestA::test_x", **kw):
    hist = make_history(key=key, pattern=pattern, **kw)
    return classify(hist, Config(), in_current_run=True)


def _result(classifications):
    counts: dict[str, int] = {}
    for c in classifications:
        counts[c.cls] = counts.get(c.cls, 0) + 1
    return AnalysisResult(
        classifications=classifications,
        counts=counts,
        current_run_uid="r9",
        run_count=9,
        test_count=len(classifications),
    )


class TestFormatAnnotation:
    def test_escaping_of_percent_and_newline(self):
        line = format_annotation("warning", "100% flaky\nsecond line", title="A: title")
        assert "::warning title=A%3A title::100%25 flaky%0Asecond line" == line

    def test_file_and_line_properties(self):
        line = format_annotation(
            "error", "boom", title="t", file="tests/a, b.py", line=42
        )
        assert "file=tests/a%2C b.py" in line
        assert "line=42" in line

    def test_comma_separated_properties(self):
        line = format_annotation("notice", "x", title="t", file="f.py")
        assert line.startswith("::notice title=t, file=f.py::x")


class TestAnnotationEmission:
    def test_severity_order_and_capping(self):
        classes = []
        for i in range(15):
            classes.append(_classification("PPFF", key=f"r::t{i}"))  # regression
        for i in range(15):
            classes.append(_classification("PFPF", key=f"f::t{i}"))  # flaky
        lines = annotations(_result(classes), max_annotations=10)
        errors = [line for line in lines if line.startswith("::error")]
        warnings = [line for line in lines if line.startswith("::warning")]
        assert len(errors) == 10  # GitHub's own cap
        assert len(warnings) == 10
        assert lines[:10] == errors  # most actionable first

    def test_stable_tests_are_not_annotated(self):
        lines = annotations(_result([_classification("PPPP")]), max_annotations=10)
        assert lines == []

    def test_flaky_annotation_contains_reason(self):
        lines = annotations(
            _result([_classification("PF", shas=["s", "s"])]), max_annotations=10
        )
        assert len(lines) == 1
        assert "commit" in lines[0] and "::warning" in lines[0]

    def test_annotations_only_for_current_run_tests(self):
        not_now = classify(
            make_history(pattern="FFPP", key="old::t"), Config(), in_current_run=False
        )
        lines = annotations(_result([not_now]), max_annotations=10)
        assert lines == []


class TestSummaryMarkdown:
    def test_contains_tables_and_gate(self):
        result = _result(
            [
                _classification("PF", shas=["s", "s"], key="a::flaky"),
                _classification("PPFF", key="b::reg"),
            ]
        )
        md = summary_markdown(result)
        assert "## FlakeHerd" in md
        assert "`a::flaky`" in md
        assert "Gate: unexplained failures" in md
        assert "b::reg" in md

    def test_step_summary_append(self, tmp_path):
        target = tmp_path / "summary.md"
        env = {"GITHUB_STEP_SUMMARY": str(target)}
        assert append_step_summary("hello", env=env)
        assert "hello" in target.read_text()
        # appends rather than overwrites
        append_step_summary("again", env=env)
        assert "hello" in target.read_text() and "again" in target.read_text()

    def test_step_summary_noop_without_env(self):
        assert append_step_summary("x", env={}) is False

    def test_in_github_actions(self):
        assert in_github_actions({"GITHUB_ACTIONS": "true"})
        assert not in_github_actions({"GITHUB_ACTIONS": "false"})
        assert not in_github_actions({})


class TestTerminalRender:
    def test_notable_rows_and_recent_pattern(self):
        result = _result(
            [
                _classification("PF", shas=["s", "s"], key="a::flaky"),
                _classification("PPPP", key="b::stable"),
                _classification("PPPFF", key="c::reg"),
            ]
        )
        text = render(result, db_path="h.db")
        assert "a::flaky" in text
        assert "b::stable" not in text  # stable rows are collapsed
        assert "1 stable-pass test(s) not shown" in text
        assert "PF" in text
        assert "confirmed flaky" in text

    def test_all_stable_message(self):
        result = _result([_classification("PPPP")])
        assert "Nothing to report" in render(result)

    def test_gate_section_lists_failures(self):
        result = _result([_classification("PPFF", key="reg::t")])
        text = render(result)
        assert "GATE: unexplained failures" in text
        assert "reg::t" in text

    def test_quarantine_candidates_section(self):
        result = _result([_classification("PF", shas=["s", "s"], key="f::t")])
        assert "Quarantine candidates" in render(result)


class TestJsonReport:
    def test_payload_structure(self):
        result = _result([_classification("PF", shas=["s", "s"], key="a::b")])
        payload = build_payload(result, window=30)
        assert payload["schema_version"] == 1
        assert payload["window"] == 30
        assert payload["tests"][0]["test_key"] == "a::b"
        assert payload["tests"][0]["cls"] == "confirmed-flaky"
        json.dumps(payload)  # serializable
