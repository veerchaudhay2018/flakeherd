# Roadmap

## Implemented and tested (v0.2.0)

- Everything from 0.1.1 (JUnit ingestion, SQLite history, taxonomy, gate,
  reporters, quarantine manifest + plugin, cache-branch sync, composite
  action, three-part demo) plus:
- Auto-retry for pytest (`--flakeherd-retries` / `flakeherd_retries` ini;
  off by default) with fully preserved evidence: per-attempt JUnit
  elements, original tracebacks, the `flakeherd_retry_*` property contract,
  and the `suspected-flaky` classification for fail-then-pass recoveries
- Retry-evidence parser hardening: `<rerunFailure>` = failed attempt,
  `<flakyFailure>` = recovered flake (Surefire), last-element-wins merge
  for divergent duplicates (fixes the rerunfailures fail-then-fail masking
  defect, verified against real plugin output), gate no longer excused by
  rerun markers on all-fail histories
- Quarantine score-threshold policy (opt-in): threshold (default 40),
  minimum samples (default 5), expiry dates + audit reasons on manifest
  entries, suggestion mode in `record`, explicit `--auto-quarantine`
  opt-in; manifests stay compatible with 0.1.x in both directions
- Test Health metric + local accessible SVG badge (`flakeherd health`) with
  an honest `insufficient-data` state; secure optional publication recipes
  documented (`docs/BADGE_WORKFLOW.md`)
- 287 automated tests (up from 164)

## Implemented and tested (v0.1.1)

- JUnit XML ingestion: pytest / jest / vitest / Go / Maven flavors,
  namespaced documents, TestNG-style `status` attributes, `.gz` support,
  streaming parse with size caps, duplicate-merging within a run
- SQLite history: WAL, upsert-safe recording, windowed loading, pruning,
  idempotent row merges
- Classification taxonomy: confirmed-flaky, flaky, flaky (low confidence),
  regression, broken, fixed, watch, new, skip-only, stable-pass
- Score (0-100) and the `--fail-on unexplained` gate
- Reporters: terminal table (ANSI, NO_COLOR-aware), GitHub annotations
  with file/line and per-severity caps, job-summary Markdown, JSON report
- Quarantine manifest (schema_version 1) + pytest plugin with conservative
  matching, reason-carrying skips, `FLAKEHERD_MANIFEST` /
  `FLAKEHERD_DISABLE` controls
- Cache-branch sync (`flakeherd ci-sync`) with merge-before-push
  correctness and one retry on push races
- Composite GitHub Action (`action.yml`), self-CI workflow, dogfood
  workflow (experimental), 164 automated tests, reproducible three-part demo

## Known gaps (honest)

- `action.yml` has not been executed on real GitHub-hosted runners yet
  (written, YAML-validated, and its underlying commands are fully tested
  locally; the `dogfood.yml` workflow exists to verify on first push).
- Not yet published to PyPI (no maintainer credentials at time of
  writing); install from source or via the action.
- The Python 3.9-3.13 CI matrix is configured but has not run on GitHub.
- Jest/Go/Java flows are covered by parser fixtures, not by end-to-end
  demo runs (the demo exercises pytest end-to-end).

## Planned next (in rough priority)

1. **First real GitHub run** of the dogfood workflow; fix what reality
   breaks; tag `v1`.
2. **PyPI publication** + release workflow (trusted publishing via CI).
3. **Quarantine auto-application**: materialize the manifest on the cache
   branch at pull time, so quarantine requires zero commits by the user
   (currently deliberate: manifests are meant to be code-reviewed).
4. **Retry adapters beyond pytest**: the `flakeherd_retry_*` JUnit property
   contract is documented; adapters for jest/vitest/Go/Java can emit it
   without any FlakeHerd changes.
5. **pytest 7.x/8.x CI legs** for the retry plugin (currently exercised on
   pytest 9.0.2 only).
4. **PR comment reporter** (stdlib `urllib` + `$GITHUB_TOKEN`): a short
   flakiness note on changed-test failures.
5. **Jest/vitest e2e demo**: mirror the pytest demo with `jest-junit`.
6. **SARIF output** for editor/CI integration.
7. **Failure-mode heuristics** (research): cluster failure messages
   reported by the same test across runs to suggest *why* it flakes.
8. **Optional in-run re-roll**: for jobs that explicitly opt in, re-run
   only currently-failing tests once, converting one-off flakes into
   same-sha evidence inside a single job.

## Non-goals

- Being a test-runner, a test-selection service, or a CI platform.
- Storing test data outside the user's repository, ever.
- ML-based flakiness prediction before the evidence-based core is proven
  in the field.
