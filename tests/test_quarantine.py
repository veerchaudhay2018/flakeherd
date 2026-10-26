"""Quarantine selection, manifest round-trip and matching tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.conftest import make_history

from flakeherd.config import Config
from flakeherd.detector import classify
from flakeherd.errors import ManifestError
from flakeherd.quarantine import (
    SCHEMA_VERSION,
    QuarantineEntry,
    find_match,
    load_manifest,
    pytest_id_parts,
    select,
    write_manifest,
)


def _entry(
    name: str = "test_flaky",
    classname: str | None = "test_mod.TestA",
    file: str | None = "tests/test_mod.py",
    cls: str = "confirmed-flaky",
    score: int = 80,
) -> QuarantineEntry:
    return QuarantineEntry(
        test_key=f"{classname}::{name}" if classname else name,
        name=name,
        classname=classname,
        file=file,
        cls=cls,
        score=score,
        failure_rate=0.4,
        samples=10,
        reason="in-run flakiness evidence",
    )


def _classification(pattern, cls_hint=None, **kw):

    hist = make_history(pattern=pattern, **kw)
    return classify(hist, Config(), in_current_run=True)


class TestSelection:
    def test_confirmed_and_flaky_selected(self):
        entries = select(
            [
                _classification("PF", shas=["s", "s"]),  # confirmed-flaky
                _classification("PFPF"),  # flaky
                _classification("PPPP"),  # stable
            ],
            Config(),
        )
        assert [e.cls for e in entries] == ["confirmed-flaky", "flaky"]  # score order

    def test_low_confidence_needs_opt_in(self):
        entries = select([_classification("PFP")], Config())
        assert entries == []
        cfg = Config(include_low_confidence=True)
        entries = select([_classification("PFP")], cfg)
        assert [e.cls for e in entries] == ["flaky-low"]

    def test_broken_and_regression_never_selected(self):
        entries = select(
            [
                _classification("PPFF"),  # regression
                _classification("FFFF"),  # broken
                _classification("PPPP"),
            ],
            Config(),
        )
        assert entries == []

    def test_min_score_filters(self):
        cfg = Config(quarantine_min_score=100)
        entries = select([_classification("PF", shas=["s", "s"])], cfg)
        assert entries == []


class TestManifest:
    def test_roundtrip(self, tmp_path: Path):
        path = tmp_path / "q.json"
        write_manifest(path, [_entry()], generator="flakeherd 0.1.0")
        loaded = load_manifest(path)
        assert loaded == [_entry()]
        raw = json.loads(path.read_text())
        assert raw["schema_version"] == SCHEMA_VERSION
        assert raw["quarantined"][0]["name"] == "test_flaky"

    def test_empty_manifest_is_valid(self, tmp_path):
        path = tmp_path / "q.json"
        write_manifest(path, [], generator="g")
        assert load_manifest(path) == []

    def test_missing_file_error(self, tmp_path):
        with pytest.raises(ManifestError, match="not found"):
            load_manifest(tmp_path / "nope.json")

    def test_bad_schema_version_error(self, tmp_path):
        path = tmp_path / "q.json"
        path.write_text(json.dumps({"schema_version": 99, "quarantined": []}))
        with pytest.raises(ManifestError, match="schema_version"):
            load_manifest(path)

    def test_entry_missing_required_field_error(self, tmp_path):
        path = tmp_path / "q.json"
        path.write_text(
            json.dumps({"schema_version": 1, "quarantined": [{"name": "x"}]})
        )
        with pytest.raises(ManifestError, match="missing"):
            load_manifest(path)

    def test_invalid_json_error(self, tmp_path):
        path = tmp_path / "q.json"
        path.write_text("{nope")
        with pytest.raises(ManifestError, match="invalid JSON"):
            load_manifest(path)


class TestPytestIdParts:
    def test_plain_function(self):
        assert pytest_id_parts("tests/test_x.py::test_a") == (
            "tests/test_x.py",
            "tests.test_x",
            "test_a",
        )

    def test_nested_path_mirrors_pytest_dotted_classname(self):
        # pytest derives junit classname from the rootdir-relative path:
        # tests/unit/test_x.py -> tests.unit.test_x
        file, classname, name = pytest_id_parts(
            "tests/unit/test_x.py::test_a"
        )
        assert file == "tests/unit/test_x.py"
        assert classname == "tests.unit.test_x"
        assert name == "test_a"

    def test_class_and_params(self):
        file, classname, name = pytest_id_parts(
            "tests/unit/test_x.py::TestFoo::test_a[param 1]"
        )
        assert file == "tests/unit/test_x.py"
        assert classname == "tests.unit.test_x.TestFoo"
        assert name == "test_a[param 1]"


class TestFindMatch:
    def test_name_and_classname_match(self):
        e = _entry(classname="test_x", file=None)
        assert find_match([e], name="test_flaky", classname="test_x", file="test_x.py")

    def test_name_and_file_match(self):
        e = _entry(classname=None, file="tests/test_x.py")
        assert find_match([e], name="test_flaky", classname="test_x", file="test_x.py")

    def test_parametrized_items_match_base_name(self):
        e = _entry(classname="test_x", file=None)
        assert find_match([e], name="test_flaky[a]", classname="test_x", file="test_x.py")
        e2 = _entry(name="test_flaky[a]", classname="test_x", file=None)
        assert find_match([e2], name="test_flaky", classname="test_x", file="test_x.py")

    def test_wrong_file_never_matches(self):
        e = _entry(classname="test_x", file="tests/test_x.py")
        assert (
            find_match([e], name="test_flaky", classname="test_x", file="tests/other.py")
            is None
        )

    def test_same_basename_in_different_directories_never_matches(self):
        e = _entry(name="test_flaky", classname=None, file="tests/a/test_x.py")
        assert (
            find_match(
                [e],
                name="test_flaky",
                classname="test_x",
                file="tests/b/test_x.py",
            )
            is None
        )

    def test_same_name_different_class_no_match(self):
        e = _entry(classname="test_x.TestA", file=None)
        assert (
            find_match([e], name="test_flaky", classname="test_x.TestB", file="tests/test_x.py")
            is None
        )

    def test_no_identity_fields_name_only_match(self):
        e = _entry(classname=None, file=None)
        assert find_match([e], name="test_flaky", classname="anything", file="x.py")

    def test_identity_less_entry_skipped_when_ambiguous(self):
        # an identity-less entry must never win when another entry with the
        # same name carries (possibly contradicting) identity
        plain = _entry(name="test_x", classname=None, file=None)
        named = _entry(name="test_x", classname="other_mod", file=None)
        # identity entry matches its own classname
        found = find_match(
            [plain, named], name="test_x", classname="other_mod", file="other.py"
        )
        assert found is not None and found.classname == "other_mod"
        # neither matches a third module
        assert (
            find_match([plain, named], name="test_x", classname="third_mod", file="third.py")
            is None
        )

    def test_two_identity_less_entries_with_same_name_never_match(self):
        a = _entry(name="dup", classname=None, file=None)
        b = _entry(name="dup", classname=None, file=None)
        assert find_match([a, b], name="dup", classname="x", file="x.py") is None

    def test_no_match_returns_none(self):
        e = _entry(classname="test_x", file=None)
        assert find_match([e], name="test_other", classname="test_x", file="test_x.py") is None

    def test_classname_suffix_match(self):
        # junit classname like "test_x.TestA" also matches a nodeid
        # classified under a nested class path
        e = _entry(classname="TestA", file=None)
        assert find_match([e], name="test_flaky", classname="test_x.TestA", file="test_x.py")


def test_load_manifest_rejects_invalid_numeric_fields(tmp_path):
    import json

    import pytest

    from flakeherd.errors import ManifestError
    from flakeherd.quarantine import SCHEMA_VERSION, load_manifest

    for field, value in [("score", "not-an-int"), ("failure_rate", "not-a-float")]:
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "quarantined": [{
                "test_key": "test_example",
                "name": "test_example",
                "cls": "flaky",
                "reason": "regression test",
                field: value,
            }],
        }
        path = tmp_path / f"{field}.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(ManifestError, match="invalid field value"):
            load_manifest(path)
