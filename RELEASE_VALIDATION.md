# FlakeHerd 0.2.0 release-candidate validation

This document records the checks performed on the 0.2.0 release candidate
in the build environment described below. It is not a claim of a completed
public release, and it is not a live GitHub Actions validation.

Build environment: Python 3.12.15, pytest 9.0.2, ruff 0.17.2, PyYAML 6.0.3,
git 2.47.3, Linux x86-64, network access available. pytest-rerunfailures
16.7 was installed temporarily in a sandbox to capture its real JUnit XML
output (used to pin the parser's rerun semantics in tests; it is NOT a
runtime or dev dependency of FlakeHerd).

## Passed locally (with exit codes)

| Check | Command | Result |
| --- | --- | --- |
| Unit + integration suite (editable install) | `python -m pytest -q` | 287 passed, exit 0 |
| Byte-compilation | `python -m compileall -q src tests` | exit 0 |
| Lint | `ruff check src tests` | clean, exit 0 |
| YAML parsing | PyYAML `safe_load` of `action.yml`, `.github/workflows/ci.yml`, `.github/workflows/dogfood.yml`, `examples/workflows/flakeherd-basic.yml`, `examples/workflows/flakeherd-health.yml` | all parse, exit 0 |
| Workflow structure invariants | `tests/test_workflows.py` (27 tests: no input interpolation into shell source, token scoped to sync steps, least-privilege permissions, manual-only dogfood trigger, pinned action refs, read-only+secret-free badge example, well-formed example trigger) | pass |
| Wheel build | `python -m pip wheel . --no-deps --no-build-isolation` | builds `flakeherd-0.2.0-py3-none-any.whl`, exit 0 |
| Wheel metadata | inspect `METADATA` / `entry_points.txt` / archive listing | `Version: 0.2.0`, `Requires-Python: >=3.9`, console script + `pytest11` entry points present, `py.typed` and `health.py` packaged, exit 0 |
| Source CLI version | `PYTHONPATH=src python -m flakeherd --version` | `flakeherd 0.2.0`, exit 0 |
| Install smoke test (fresh venv, from wheel) | `flakeherd --version`, `python -m flakeherd --version`, `flakeherd health --help` | all `flakeherd 0.2.0` / help text, exit 0 each |
| Functional smoke (fresh venv + pytest from PyPI) | real `pytest --flakeherd-retries 1 --junitxml` run; `record --json-out`; `health --badge` | retry evidence parsed (`F,P`), recovered test classified `suspected-flaky`, still-failing test recorded `fail`, insufficient-data badge rendered; exit 0 |
| Retry evidence (in-suite) | `tests/test_retry_plugin.py` (18 tests via real pytester runs against actual generated JUnit XML: pass-first, fail-then-pass, repeated failures, interruption exit code 2, missing evidence, option/ini validation, setup vs teardown semantics, quarantine interplay) | pass |
| Threshold boundaries + expiry | `tests/test_quarantine_policy.py` (30 tests; exact score 39/40/41 fixtures verified against the detector, min-sample guard, broken/regression never quarantined, expiry semantics incl. today-boundary and malformed dates, manifest compatibility in both directions with 0.1.1) | pass |
| Health metric + badge | `tests/test_health.py` (31 tests: formula weights, exclusion rules, insufficient-data, payload shape, SVG validity/accessibility/band colors/escaping, CLI) | pass |
| End-to-end demo | `bash demo/run_demo.sh` | all four parts complete (taxonomy, live flaky suite + quarantine skip, auto-retry + policy + badge, cache-branch sync); **clean exit 0** (~25 s) |
| Secret scan of the project tree | scan for token/private-key shapes (`ghp_`, `github_pat_`, `AKIA`, `-----BEGIN`, `Bearer `) | no findings, exit 0 |

## Not verified here (BLOCKED / manual)

- Execution of `action.yml` or the CI matrix on a real GitHub-hosted
  runner. The composite action and both workflows are structurally
  validated offline only. Follow the manual first-run procedure below
  before trusting them.
- Python 3.9-3.11 on real interpreters (the CI matrix is configured but
  unexecuted; only 3.12.15 was available locally). The code avoids 3.10+
  syntax at runtime (ruff target-version py39) and the wheel declares
  `>=3.9`; the retry plugin was exercised on pytest 9.0.2 only (the
  declared support range is pytest >= 7.4 - the `runtestprotocol` and
  `user_properties` mechanisms it relies on are stable across those
  versions, but only 9.0.2 was actually run).
- pytest-rerunfailures interop was verified against real 16.7 output in a
  sandbox (parser fixtures pin it); the plugin itself was not run under
  pytest 7.x/8.x.
- Authenticated history sync against a real GitHub remote, GitHub
  Enterprise, or SSH remotes. Token handling is unit-tested
  (`test_ci_sync.py`) and the header pattern is the standard
  `x-access-token` basic-auth form, but no live push was performed.
- The optional badge-push recipe in `docs/BADGE_WORKFLOW.md` is a documented
  pattern, not an executed workflow.
- Public repository creation, PyPI publication, release tagging.

## Manual first-run procedure for the repository owner

These steps perform the first real GitHub Actions validation with the
least risk. Estimated time: ~15 minutes.

1. Create the repository on GitHub (private is fine) and push this exact
   tree to `main`. Do not add secrets, do not enable fork PR workflows yet.
2. Confirm the repository's Settings -> Actions -> General -> Workflow
   permissions are set to "Read repository contents" (read-only default).
3. Open the **Actions** tab and confirm the `CI` workflow appears. Trigger
   it manually is not possible for `push`/`pull_request` events - open any
   trivial pull request so the `pull_request` event fires, or push a small
   commit to `main`.
4. Verify each CI job: `Lint (ruff)` green, `Test (Python 3.9 ... 3.13)`
   green on all five interpreters, `End-to-end demo` green, `Validate
   action + workflow YAML` green. Record the run URL in this file.
5. Only after CI is green: run the **Dogfood** workflow once via
   `workflow_dispatch` (it needs `contents: write` to push the history
   branch; it is manual-only by design). Check the run: annotations appear
   on failing lines, the step summary renders, `flakeherd/history` gains a
   commit, and a second run records a second sample set.
6. Still not verified on runners: the badge example
   (`examples/workflows/flakeherd-health.yml`) - run it on a branch first
   and confirm the artifact contains a valid `test-health.svg`.
7. Record the first real run URLs here, then consider tagging `v0.2.0`.
