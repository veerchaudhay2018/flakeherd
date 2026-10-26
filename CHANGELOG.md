# Changelog

All notable changes to this project are documented here. The format is
based on Keep a Changelog, and this project adheres to Semantic Versioning.

## [0.2.0] - 2026-10-25

### Security
- Fixed a retry-masking defect in the JUnit parser (verified against real
  pytest-rerunfailures 16.7 output): a `<testcase>` whose only failure
  evidence is a `<rerunFailure>` child was parsed as a *pass*, so a test
  that failed **every** retry attempt could be merged with its failing
  final outcome and silently recorded as green - masking a genuine failure
  from the gate. Rerun markers now count as failed attempts, and divergent
  pass/fail duplicates inside one run resolve to the **last** element's
  status (the final attempt), which is how retry producers order their
  output. A test that fails all retries stays red.
- The gate no longer excuses a currently-failing test from
  `confirmed-flaky` merely because rerun markers exist in its history: a
  test that failed every recorded run (with or without retries) is
  classified `broken` and gates the build.

### Added
- **Auto-retry with preserved evidence** (pytest): `--flakeherd-retries N`
  option and `flakeherd_retries` ini setting (default 0 = off, negative
  rejected). Setup/call failures are retried up to N extra times; every
  attempt is reported as its own JUnit `<testcase>` with the original
  failure tracebacks preserved, and the final attempt carries
  `flakeherd_retry_attempts` / `flakeherd_retry_outcomes` /
  `flakeherd_retry_first_failure` properties (the documented, extensible
  evidence contract for future framework adapters). `record --json-out`
  exposes the same per-test evidence under `current_run`. Fail-then-pass is
  classified **suspected-flaky** (new class, score base 45) - never
  automatically confirmed-flaky, never quarantined without the
  low-confidence opt-in, and never an excuse for a current failure.
  Interruptions propagate (never swallowed); skipped and quarantined tests
  are never retried; teardown failures are not retried.
- **Quarantine score-threshold policy** (all opt-in):
  `quarantine_threshold_enabled` (default false) plus
  `quarantine_score_threshold` (default 40, only with the policy on),
  `quarantine_min_samples` (default 5, validated >= 1) and
  `quarantine_expiry_days` (default 0 = never). Only eligible flaky classes
  can ever qualify; `broken` and `regression` tests are never quarantined.
  New manifest entries carry optional `expires_at` (expired entries stop
  being skipped and the test runs again) and a machine-generated
  `audit_reason`; schema_version stays 1 and manifests remain loadable by
  0.1.x readers (and vice versa). With the policy enabled `record` reports
  quarantine **suggestions only** (also in the JSON report); writing the
  manifest requires `flakeherd quarantine` or the new explicit
  `record --auto-quarantine` flag. FlakeHerd never commits or pushes the
  manifest.
- **Test Health metric + local SVG badge**: `flakeherd health --report
  <record --json-out file> [--badge PATH] [--json-out PATH]
  [--min-runs N] [--label TEXT]` computes a transparent, documented
  heuristic (metric_version 1; weighted blend of classification counts,
  `new`/`skip-only` excluded), exposes score, sample sizes, an explicit
  `insufficient-data` state, generation timestamp and metric version, and
  renders an accessible (title/desc/aria-label) SVG badge locally with an
  "no data" state. No public badge URL is invented; badges are never pushed
  anywhere automatically. Docs: README, `docs/BADGE_WORKFLOW.md`,
  `examples/workflows/flakeherd-health.yml` (artifact upload only).
- `quarantine` CLI: `--min-samples` and `--expiry-days` flags;
  dry-run output shows audit reasons and expiry dates.
- SQLite store: `retry_attempts` / `retry_outcomes` columns with in-place
  migration for 0.1.1 databases; cache-branch merges handle databases from
  either version.
- Tests: 164 -> 287 (real pytester runs of the retry plugin against actual
  generated JUnit XML, exact score boundaries 39/40/41, min-sample guards,
  expiry semantics, manifest compatibility in both directions, store
  migration, health metric + badge, policy CLI).

### Changed
- Reclassified in-run fail-then-pass evidence: `confirmed-flaky` is now
  reserved for same-commit divergence **across runs**; a recovered retry
  inside a single run yields `suspected-flaky` instead. A recovered sample
  now also counts its hidden failure in `FAIL%`, transition and regression
  statistics (the "effective attempt sequence"), so flakiness evidence is
  no longer discarded by rate calculations.
- Score formula: base 45 added for `suspected-flaky` (between `flaky-low`
  35 and `flaky` 60). All other bases unchanged.
- `examples/workflows/flakeherd-basic.yml`: fixed a malformed push trigger
  (`branches: ain]` -> `branches: [main]`) and pinned action references
  (`actions/checkout@v7.0.1`, `actions/setup-python@v6.3.0`) like the CI
  workflows.

## [0.1.1] - 2026-10-25

### Security
- Pass composite-action inputs to shell commands through quoted environment variables rather than interpolating values into shell source.
- Add optional step-scoped GitHub token handling for cache-branch history sync; recommend `persist-credentials: false` and least-privilege workflow permissions.
- Add a regression test for token handling and document the limits of the security change.

### Fixed
- Align the package version with the 0.1.1 security patch release.
- Remove premature project URLs that pointed to a GitHub repository not yet created.
- pytest-plugin integration tests no longer depend on flakeherd being
  pip-installed in the test environment: inner runs are now hermetic
  (`PYTEST_DISABLE_PLUGIN_AUTOLOAD` + `PYTEST_PLUGINS`), so a bare
  `PYTHONPATH=src python -m pytest -q` run passes identically with and
  without the package installed.
- `examples/workflows/flakeherd-basic.yml` now demonstrates the documented
  safe pattern (`persist-credentials: false` plus `github-token`); the
  previous example contradicted the security guidance and, with ephemeral
  credentials, could never have authenticated a history push.

### Added
- `tests/test_workflows.py`: structural validation of `action.yml` and the
  CI workflows - inputs never interpolated into shell source, sync token
  scoped to `ci-sync` steps only, least-privilege permissions, manual-only
  trigger for the write-capable dogfood workflow, pinned action references,
  and the safe pattern in the example workflow (164 tests total).

### Changed
- Downgraded the trove classifier to `Development Status :: 3 - Alpha` to
  reflect the project's actual maturity ahead of any real-runner validation.

## [0.1.0] - 2026-10-25

### Added
- Initial public release.
- `flakeherd record`: parse JUnit XML (pytest, jest, vitest, Go, Maven,
  TestNG-style flavors; `.xml` / `.xml.gz`), merge duplicate results within
  a run, record outcomes to a local SQLite history with commit/branch/run
  metadata.
- `flakeherd report`: taxonomy-driven analysis with per-test classes
  (confirmed-flaky, flaky, flaky-low, regression, broken, fixed, watch,
  new, skip-only, stable-pass), 0-100 flakiness scores, and unexplained-
  failure gating.
- `flakeherd quarantine`: JSON skip-manifest for proven flakes, with
  score/class configuration.
- pytest plugin (entry-point auto-registration): skips quarantined tests
  with the recorded reason; header line; `FLAKEHERD_MANIFEST` /
  `FLAKEHERD_DISABLE` environment controls.
- `flakeherd ci-sync`: history persistence through a cache branch in the
  user's repository with merge-before-push correctness, binary-safe blob
  extraction, and a retry on push races.
- Reporters: terminal table (color-capable, NO_COLOR-aware), GitHub
  Actions annotations (file/line, escaping, per-severity caps), job
  summary Markdown, and JSON output.
- Composite GitHub Action (`action.yml`) with pull/record/quarantine/push
  steps; history sync is best-effort and never breaks builds.
- Optional `.flakeherd.json` configuration, `.flakeherd.example.json`
  template, `flakeherd init`.
- Test suite (145 tests at initial release; 146 after the security regression test) covering parser flavors, taxonomy rules, storage,
  quarantine matching, reporters, CLI exit codes, cache-branch sync
  against real git repositories, and the pytest plugin via real
  collection runs.
- Reproducible three-part demo (`demo/run_demo.sh`), recorded transcript
  in `docs/DEMO_OUTPUT.txt`.
- Documentation: README, ARCHITECTURE, RESEARCH (evidence + competitor +
  name audit), SECURITY, CONTRIBUTING, ROADMAP.
