"""pytest plugin integration tests (via pytester) - real collection runs."""

from __future__ import annotations

import json

import pytest

pytest_plugins = ["pytester"]

# The quarantine plugin is normally registered through the ``pytest11``
# entry point whenever flakeherd happens to be pip-installed in the test
# environment - which an explicit ``-p``/conftest load would then crash
# with "Plugin already registered under a different name". Make every
# inner run hermetic instead: autoload off, plugin loaded via
# PYTEST_PLUGINS. The suite then behaves identically with a bare
# ``PYTHONPATH=src python -m pytest`` run and inside an environment where
# flakeherd is installed (entry point active).
@pytest.fixture(autouse=True)
def _hermetic_quarantine_plugin(monkeypatch):
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PYTEST_PLUGINS", "flakeherd.pytest_plugin")


def _manifest(path, entries):
    (path / ".flakeherd").mkdir(parents=True, exist_ok=True)
    (path / ".flakeherd/quarantine.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generator": "test",
                "generated_at": "2026-01-01T00:00:00+00:00",
                "quarantined": entries,
            }
        )
    )


def _entry(name, cls="confirmed-flaky", score=80, classname="test_x"):
    return {
        "test_key": f"{classname}::{name}",
        "name": name,
        "classname": classname,
        "file": None,
        "cls": cls,
        "score": score,
        "failure_rate": 0.4,
        "samples": 10,
        "reason": "same commit produced both a pass and a fail",
    }


class TestQuarantineSkipping:
    def test_matching_test_is_skipped_with_reason(self, pytester, tmp_path):
        _manifest(pytester.path, [_entry("test_flaky")])
        pytester.makepyfile(
            test_x="""
            def test_flaky():
                pass

            def test_ok():
                assert True
            """
        )
        result = pytester.runpytest()
        result.assert_outcomes(passed=1, skipped=1)
        result.stdout.fnmatch_lines(["*flakeherd: 1 test(s) quarantined*"])

    def test_parametrized_variants_are_skipped(self, pytester):
        _manifest(pytester.path, [_entry("test_flaky")])
        pytester.makepyfile(
            test_x="""
            import pytest

            @pytest.mark.parametrize("v", [1, 2])
            def test_flaky(v):
                assert v > 0
            """
        )
        result = pytester.runpytest()
        result.assert_outcomes(skipped=2)

    def test_class_methods_skipped(self, pytester):
        # real junit manifests carry the full classname for class methods
        _manifest(pytester.path, [_entry("test_flaky", classname="test_x.TestWidget")])
        pytester.makepyfile(
            test_x="""
            class TestWidget:
                def test_flaky(self):
                    assert True
            """
        )
        result = pytester.runpytest()
        result.assert_outcomes(skipped=1)

    def test_module_classname_never_skips_class_method(self, pytester):
        # strict matching: a module-level entry must not shadow a same-named
        # class method in the same file
        _manifest(pytester.path, [_entry("test_flaky", classname="test_x")])
        pytester.makepyfile(
            test_x="""
            class TestWidget:
                def test_flaky(self):
                    assert True
            """
        )
        result = pytester.runpytest()
        result.assert_outcomes(passed=1)


class TestNoManifest:
    def test_no_manifest_no_behavior_change(self, pytester):
        pytester.makepyfile(test_x="def test_ok():\n    assert True\n")
        result = pytester.runpytest()
        result.assert_outcomes(passed=1)

    def test_invalid_manifest_warns_but_runs(self, pytester):
        (pytester.path / ".flakeherd").mkdir()
        (pytester.path / ".flakeherd/quarantine.json").write_text('{"schema_version": 9}')
        pytester.makepyfile(test_x="def test_ok():\n    assert True\n")
        result = pytester.runpytest()
        result.assert_outcomes(passed=1)


class TestDisable:
    def test_env_var_disables_quarantine(self, pytester, monkeypatch):
        _manifest(pytester.path, [_entry("test_flaky")])
        pytester.makepyfile(test_x="def test_flaky():\n    pass\n")
        monkeypatch.setenv("FLAKEHERD_DISABLE", "1")
        result = pytester.runpytest()
        result.assert_outcomes(passed=1)

    def test_env_var_manifest_path(self, pytester, monkeypatch, tmp_path):
        custom = tmp_path / "custom-quarantine.json"
        custom.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generator": "t",
                    "generated_at": "t",
                    "quarantined": [_entry("test_flaky")],
                }
            )
        )
        monkeypatch.setenv("FLAKEHERD_MANIFEST", str(custom))
        pytester.makepyfile(test_x="def test_flaky():\n    pass\n")
        result = pytester.runpytest()
        result.assert_outcomes(skipped=1)


class TestQuarantineExpiry:
    def _manifest_with_expiry(self, pytester, expires_at):
        (pytester.path / ".flakeherd").mkdir(parents=True, exist_ok=True)
        (pytester.path / ".flakeherd/quarantine.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generator": "test",
                    "generated_at": "2026-01-01T00:00:00+00:00",
                    "quarantined": [
                        dict(
                            _entry("test_flaky"),
                            expires_at=expires_at,
                            audit_reason="score 80 >= 40; policy=threshold",
                        )
                    ],
                }
            )
        )

    def test_expired_entry_runs_again(self, pytester, monkeypatch):
        from datetime import date, timedelta

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        self._manifest_with_expiry(pytester, yesterday)
        pytester.makepyfile(test_x="def test_flaky():\n    assert True\n")
        result = pytester.runpytest()
        # the quarantine lapsed: the test runs and passes
        result.assert_outcomes(passed=1)

    def test_active_entry_still_skips(self, pytester):
        from datetime import date, timedelta

        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self._manifest_with_expiry(pytester, tomorrow)
        pytester.makepyfile(test_x="def test_flaky():\n    assert True\n")
        result = pytester.runpytest()
        result.assert_outcomes(skipped=1)
        result.stdout.fnmatch_lines(["*flakeherd: 1 test(s) quarantined*"])

    def test_today_expiry_is_still_active(self, pytester):
        # lapses the day AFTER the expiry date, not on it
        from datetime import date

        self._manifest_with_expiry(pytester, date.today().isoformat())
        pytester.makepyfile(test_x="def test_flaky():\n    assert True\n")
        result = pytester.runpytest()
        result.assert_outcomes(skipped=1)

    def test_header_reports_expired_entries(self, pytester):
        from datetime import date, timedelta

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        self._manifest_with_expiry(pytester, yesterday)
        pytester.makepyfile(test_x="def test_flaky():\n    assert True\n")
        result = pytester.runpytest()
        result.assert_outcomes(passed=1)
        result.stdout.fnmatch_lines(["*expired - running again*"])
