# Security patch notes — 0.1.1 review build

This 0.1.1 patch addresses two issues found during review of the 0.1.0 ZIP
and aligns the package metadata with the patched release. It then received
a second, independent final-engineering pass whose findings are folded in
below.

## Changes made

- Composite-action inputs used in shell commands are passed through
  step-scoped environment variables and quoted, rather than interpolated
  into shell source.
- Added an optional `github-token` input. It is exposed only to the
  history pull/push steps, not the record step that processes test
  reports.
- Git sync consumes `FLAKEHERD_GITHUB_TOKEN` and configures a temporary
  GitHub HTTPS authorization header for Git subprocesses; the raw token
  variable is removed from each Git child environment.
- Updated the README, security policy and architecture docs to require
  `actions/checkout` with `persist-credentials: false` and to restrict
  `contents: write` to trusted workflows.
- Updated the manual dogfood workflow to follow the safer credential
  pattern.
- Updated GitHub Actions dependencies to current compatible official
  releases; `setup-python` stays on the v6 line so the project can
  continue testing Python 3.9, which newer action versions may no longer
  bundle.
- Added a regression test for token handling.

## Findings from the final verification pass (2026-10-25)

- **Reproduced and fixed:** the pytest-plugin integration tests silently
  depended on flakeherd being pip-installed (the `pytest11` entry point
  active). With a bare `PYTHONPATH=src` run - the command recorded in the
  original validation notes - four plugin tests failed. Inner test runs
  are now hermetic (`PYTEST_DISABLE_PLUGIN_AUTOLOAD` + `PYTEST_PLUGINS`),
  which also prevents an installed environment from crashing with
  "Plugin already registered under a different name" when the plugin is
  loaded twice. Both modes now pass 164/164.
- **Reproduced and fixed:** `examples/workflows/flakeherd-basic.yml` still
  demonstrated the pre-patch pattern (persisted checkout credentials, no
  token), contradicting the README security note; with the documented
  `persist-credentials: false` guidance it could never authenticate a
  history push. It now shows the safe, functional pattern.
- **Added:** structural workflow tests (`tests/test_workflows.py`) pin the
  security invariants so a future edit cannot silently reintroduce input
  interpolation into shell source, widen the token scope, or auto-trigger
  the write-capable dogfood workflow.
- **Verified present:** the 0.1.0 review fixes (gzip decompressed-size
  cap, "unknown"-sha false-flaky exclusion, explicit current-run gating,
  atomic writes creating parent directories, streaming-parser element
  detach) are all in this build and covered by regression tests.
- **Not a defect:** the demo script itself terminates cleanly (exit 0,
  ~21 s). The earlier "timed out after the completion banner" report was
  an artifact of the previous execution harness, not of the script.

## Verification performed (final pass, network available)

- `PYTHONPATH=src python -m pytest -q`: **164 tests passed**, exit 0 - and
  the same 164 pass after `pip install -e .`.
- `ruff check src tests`: clean (ruff 0.17.2; the previous environment
  could not install ruff - that limitation no longer applies).
- `python -m compileall -q src tests`: passed.
- YAML parsing for `action.yml`, both workflows and the example workflow:
  passed; structural invariants additionally enforced by tests.
- Wheel build (`pip wheel . --no-deps --no-build-isolation`): succeeded;
  metadata reports `Version: 0.1.1`, console + `pytest11` entry points
  present. Fresh-venv install from the wheel passes an end-to-end smoke
  test (record, report, quarantine, plugin skip).
- `bash demo/run_demo.sh`: completes with **exit code 0**.
- `actions/checkout@v7.0.1` and `actions/setup-python@v6.3.0` verified to
  exist as published release tags.

## Not verified

- The action has not been executed on a real GitHub-hosted runner as part
  of this patch. See `RELEASE_VALIDATION.md` for the exact manual
  first-run procedure.
- Token authentication is configured for `https://github.com/`; GitHub
  Enterprise or SSH remotes need separate validation.

This is a security improvement, not a claim that the project is
production-ready or immune to all attacks. Keep write-capable tokens away
from workflows that execute untrusted pull-request code.

---

# Security patch notes — 0.2.0

## Retry-related fixes (found and fixed during this release)

1. **JUnit retry-marker masking (verified, fixed).** flakeherd <= 0.1.1
   parsed a `<testcase>` whose only failure evidence was a
   `<rerunFailure>` child as a *pass*. Combined with the duplicate merge,
   a test that failed **every** retry attempt (rerun attempt element +
   failing final element) was recorded as `pass` + flakiness hint - a
   genuine failure silently turned green, invisible to the
   `--fail-on unexplained` gate. Reproduced against real
   pytest-rerunfailures 16.7 output (which emits failed attempts as *bare*
   testcase elements); fixed by counting `rerunFailure` as a failed
   attempt and by the last-element-wins merge for divergent duplicates.
   Regression tests: `tests/test_junit.py` (rerunfailures-format
   fixtures), `tests/test_retry_plugin.py`.
2. **Gate bypass via rerun markers (verified, fixed).** flakeherd <= 0.1.1
   classified an all-fail history carrying rerun markers as
   `confirmed-flaky`, which *excused* a currently-failing test from the
   gate. Retries that never recover are now classified `broken`; the new
   `suspected-flaky` class (single in-run recovery) is deliberately not a
   gate-excusing class.
3. **Retry evidence is untrusted input.** The `flakeherd_retry_*` JUnit
   properties are sanitized on parse (outcome letters restricted to
   P/F/S, excerpt capped at 10 KiB, attempt counts must be positive
   integers) and are never used in shell commands, SQL or paths. No new
   attack surface was added: the retry feature runs inside pytest via
   documented plugin hooks and writes only to the JUnit XML path pytest
   already uses.

## Reviewed, no change needed

- `action.yml` and both workflows: unchanged in 0.2.0 (no new inputs, no
  new steps, no new token flows); structural tests extended to the new
  read-only, secret-free badge example workflow.
- Badge generation is a local file write to a user-specified path via the
  same atomic-write helper as all other outputs; no network, no service.
