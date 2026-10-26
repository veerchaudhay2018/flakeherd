# FlakeHerd architecture

This document is the source of truth for design decisions. The taxonomy and
score are pinned by `tests/test_detector.py`; the storage and sync behavior
by `tests/test_store.py` and `tests/test_ci_sync.py`. If this file and the
code disagree, the code wins and this file must be updated.

## 1. Components

```
src/flakeherd/
  junit.py          parser: JUnit XML -> TestCaseResult (streaming, size-capped)
  store.py          SQLite history: runs + outcomes (WAL, FK cascade, upsert)
  detector.py       classification taxonomy + score + gate
  quarantine.py     manifest selection/write/load + pytest id matching
  pytest_plugin.py  pytest11 plugin: skip quarantined tests with a reason;
                    opt-in auto-retry (--flakeherd-retries) with preserved evidence
  report/           terminal table, GitHub annotations + step summary, JSON
  ci_sync.py        cache-branch pull/push via git plumbing
  health.py         Test Health metric + local SVG badge (from the JSON report)
  cli.py            argparse CLI: record, report, quarantine, ci-sync,
                    health, init
```

Dependencies: **none at runtime**. Python standard library only
(`sqlite3`, `xml.etree`, `gzip`, `glob`, `subprocess`, `argparse`, ...).
`pytest` is a consumer of the optional plugin, not a dependency of the CLI.

## 2. Data model

Two tables (SQLite, WAL journaling, foreign keys on):

```
runs(run_uid PK, commit_sha, branch, created_at)
outcomes(run_uid FK->runs ON DELETE CASCADE, test_key,
         name, classname, file, line, status, flaky_hint, duration_sec,
         retry_attempts NULL, retry_outcomes NULL,
         PK(run_uid, test_key))
```

The two `retry_*` columns were added in 0.2.0. Databases recorded by 0.1.x
are migrated in place (`ALTER TABLE ADD COLUMN`, nullable); merges from an
old cache-branch database select NULL for the missing columns, so history
sync works across versions in both directions.

- **run_uid**: `$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT` in CI (so a re-run of the
  same job records *distinct* outcomes for the *same* commit - the strongest
  flakiness signal), a generated id locally.
- **test_key**: `classname::name`, or `name` when a report has no classname.
- **status**: `pass | fail | skip`. **flaky_hint**: boolean in-run flakiness
  evidence (rerun markers like `<rerunFailure>`, or a test that both passed
  and failed inside one run's merged files).
- Re-recording the same `run_uid` upserts rows instead of duplicating them.
- `prune(max_runs)` keeps the newest runs (default 500) and cascades deletes
  to outcomes, bounding database growth.

### Duplicate elements inside one run (retries, shards)

Retry producers emit one `<testcase>` per attempt; shards emit one per
shard. FlakeHerd merges duplicates under two rules (0.2.0):

* **Divergent pass/fail duplicates keep the LAST element's status** plus
  `flaky_hint=True` for the divergence. Retry output is temporal - the
  final attempt is the last element - so a test that failed its last
  attempt is recorded as `fail` even when an earlier (or bare rerun)
  element looks like a pass. This is the fix for the retry-masking defect:
  before 0.2.0 the merge always said "recovered", letting a genuinely
  failing test be recorded as green.
* fail+skip -> fail; pass+skip -> pass (unchanged since 0.1.0).

Because the final status is honest, a recovered sample (pass + hint)
genuinely contains a failure, and the detector expands it to a fail-then-
pass *attempt pair* when computing failure rate, transitions and trailing
streaks (the "effective sequence"). Recorded samples stay run-level:
`samples` counts runs, while `pass_count`/`fail_count` count attempts.

## 3. The classification taxonomy

Given one test's window (last N outcomes, oldest first), skips removed for
pass/fail reasoning, rules in priority order:

1. 1 sample with in-run recovery (pass + `flaky_hint`) ->
   `suspected-flaky` (the evidence exists even in a first run)
2. 1 sample otherwise -> `new`
3. no pass/fail sample -> `skip-only`
4. any commit sha with both a pass and a fail **across runs** ->
   `confirmed-flaky` (identical code, different outcome). Sentinel shas
   ("unknown", empty) are excluded, and the check runs on *recorded run
   outcomes* only - a recovered sample's hidden in-run failure must never
   fabricate cross-run divergence.
5. all attempts failed -> `broken` (reruns that never recover included)
6. all attempts passed -> `stable-pass`
7. mixed: let T = pass<->fail transitions, S = trailing failure streak,
   both over the effective attempt sequence
   - S >= `min_regression_streak` AND T <= 2 -> `regression`
   - T >= 3 -> `flaky`
   - some run recovered in-run (fail-then-pass) -> `suspected-flaky`
   - T == 2 -> `flaky-low` (one-off flake P F P, or break-then-fix P F F P)
   - T == 1 and currently passing -> `fixed`
   - otherwise -> `watch` (first failure after a passing streak)

Score: `base[class] + 20*failure_rate + 10*transition_density`, clamped to
0-100. Bases: confirmed-flaky 70, flaky 60, suspected-flaky 45, flaky-low
35, fixed 15, watch 15. Transition density = T / (attempt samples - 1).
Everything else scores 0.

`suspected-flaky` is deliberately **not** a member of the flaky family: an
auto-retry that recovered once is suspicion, not proof, and must never
unconditionally excuse a current failure.

Gate (`--fail-on unexplained`): a currently-failing test that is NOT in the
flaky family (confirmed-flaky, flaky, flaky-low) fails the job. A test that
did not run in the current run never gates. A test that failed every retry
attempt is recorded as `fail` (eventually `broken`) and gates normally:
retries can never silently turn a genuine failure into a green gate.

## 4. Storage sync: the cache branch

The history database is synced through a branch inside the user's own
repository (default `flakeherd/history`), one commit per CI run:

- **pull**: `git ls-remote` (branch present?) -> `git fetch` -> `git show
  FETCH_HEAD:history.db` (binary-safe) -> merge rows with
  `INSERT OR IGNORE` (idempotent, order-independent).
- **push**: `git hash-object -w` the db -> build a one-file tree with a
  throwaway `GIT_INDEX_FILE` (never touches the working tree) ->
  `git commit-tree` with the branch tip as parent -> `git push`.

**Correctness rule**: the pushed database must be a superset of the branch
tip. Push therefore always merges the tip's rows into the local database
*before* committing (pinned by
`test_concurrent_writer_loses_no_rows`). If the push is rejected anyway
(two writers raced), the loser re-fetches the tip, merges again, and
retries once. Losing the same race twice is possible but vanishingly rare;
the result is a missing sample set in shared history, which the detector
tolerates by design - classification is statistical, and the push failure
is reported as a warning without failing the build.

Threat model and safety notes: see `SECURITY.md`.

## 5. Parsing

`xml.etree.iterparse` with per-element clearing *and* detaching from the
parent keeps memory bounded on large reports; files over 64 MiB
(configurable) are rejected up front; `.gz` reports are decompressed under
the same cap applied to the expanded content. Namespaced documents are
supported. `status` attribute fallbacks cover TestNG-style reports.
Retry evidence is read from two sources:

* **The property contract** (first-party, extensible): a `<testcase>` may
  carry `<properties>` with `flakeherd_retry_attempts`,
  `flakeherd_retry_outcomes` (`"F,P"` - one letter per attempt) and a
  capped (10 KiB) `flakeherd_retry_first_failure` excerpt. FlakeHerd's own
  pytest auto-retry writes this contract; any other framework adapter can
  emit the same properties and be read identically. A fail-then-pass
  outcome string sets `flaky_hint` on the merged result.
* **Legacy child elements**: `<rerunFailure>` (pytest-rerunfailures) marks
  a *failed attempt* (fail + hint); `<flakyFailure>` (Maven Surefire)
  marks a *recovered flake* (pass + hint); other `rerun`-ish tags set the
  hint only. Empirically (rerunfailures 16.7 + pytest 9), failed rerun
  attempts are written as *bare* testcase elements - the last-element-wins
  merge rule above is what keeps those tests red.

Cases without a `name` attribute are skipped with a warning (never
silently).

## 6. The composite action

`action.yml` installs the package from the action's own checkout (no PyPI
dependency), then runs pull -> record (the only step that can fail the job,
and only under `fail-on: unexplained`) -> optional quarantine -> push.
Pull/push are `continue-on-error: true`: history sync is best-effort and
must never break a build. For GitHub Actions, configure checkout with
`persist-credentials: false` and pass `github-token` to FlakeHerd; the action
exposes that token only to the pull/push steps. Never expose a write-capable
token to test processes. Grant `contents: write` only to trusted workflows;
untrusted pull-request workflows should be read-only and should not push history.

## 7. The pytest plugin

Registered via the `pytest11` entry point. No manifest -> zero behavior
change. Manifest found (`.flakeherd/quarantine.json` or
`$FLAKEHERD_MANIFEST`) -> matching tests get `pytest.mark.skip` with the
recorded reason, and the run header states how many tests are quarantined.

Matching is deliberately conservative: the name must match after stripping
parameter suffixes on both sides, the derived classname must equal the
manifest's classname or be its suffix (the manifest's classname comes from
the same JUnit XML path derivation, so real flows match exactly), and a
recorded file must agree too. An entry carrying no classname and no file
matches by unique name only. Two identity signals must never contradict.

## 8. Concurrency and performance

- One SQLite file, WAL mode: concurrent processes on one machine are safe.
- CI jobs share state through the cache branch (above), not the file.
- Window slicing happens while loading (`ORDER BY created_at, rowid`),
  memory is bounded by `window * distinct tests` samples.
- The store is intentionally not a time-series database: 500 runs x a few
  thousand tests is single-digit megabytes, inspectable with `sqlite3`.

## 9. Auto-retry: the evidence pipeline (0.2.0)

`--flakeherd-retries N` (default 0 = off) is implemented as a firstresult
`pytest_runtest_protocol` hook: with retries off it returns `None` and
pytest's default protocol runs untouched; with retries on, each attempt is
a full `runtestprotocol(item, log=True)` pass, so console output, the
terminal summary and JUnit XML all see every attempt.

Evidence flows through three pinned mechanisms (all covered by
`tests/test_retry_plugin.py` against *real* generated XML):

1. **Per-attempt JUnit elements.** pytest's junitxml reporter finalizes one
   `<testcase>` per nodeid per attempt; failed attempts keep their
   `<failure>` tracebacks verbatim - the original failure is never
   discarded. The last element carries the final outcome.
2. **The property contract.** A `pytest_runtest_makereport` hookwrapper
   tracks per-attempt outcomes on the item and appends the cumulative
   `flakeherd_retry_*` properties to each attempt's *teardown report*
   (reports snapshot `item.user_properties` at creation, so the teardown
   report - the last one created for that attempt's element - is the only
   reliable carrier). Only tests that actually retried (>= 2 attempts)
   carry properties; single-attempt tests stay byte-identical to vanilla
   pytest output.
3. **Last-element-wins merge + suspected-flaky classification** (sections
   3 and 5) turn the XML back into history: fail-then-pass -> `pass` +
   hint + retry metadata -> `suspected-flaky`; fail-fail -> `fail` with
   retry metadata -> `broken` (gates).

Interruptions: `KeyboardInterrupt` propagates out of the retry loop (a
`finally` only removes the per-item state); pytest aborts the session with
exit code 2 and junitxml still writes the attempts that ran. Quarantined
(skipped) tests never enter the loop; teardown failures are not retried.

Do not combine with pytest-rerunfailures' `--reruns`: both implement
`pytest_runtest_protocol` and only one can drive the loop.

## 10. Quarantine policy and manifest evolution (0.2.0)

The policy layer is additive and opt-in (see `config.py`). Selection order:
eligible class (confirmed-flaky/flaky by default; suspected-flaky and
flaky-low behind `include_low_confidence`) -> effective score threshold
(`max(quarantine_min_score, policy floor, --min-score)` - the policy floor
can only be raised, never bypassed) -> minimum samples (policy on only) ->
entry with `audit_reason` and, when configured, `expires_at`.

Expiry is consumed at the *plugin*: `is_expired()` compares calendar dates
(expires today = still active; malformed dates never expire - a corrupt
manifest must not silently un-quarantine a test). `record` suggests;
`record --auto-quarantine` (explicit opt-in, requires the policy) or
`flakeherd quarantine` writes. Nothing is ever committed or pushed.

Manifest compatibility: `schema_version` stays 1. New fields are optional
with defaults on load (`expires_at=None`, `audit_reason=""`); 0.1.x readers
ignore unknown keys. Pinned by `tests/test_quarantine_policy.py`.

## 11. Test Health metric (0.2.0)

`flakeherd/health.py` computes the metric *from the JSON report* (single
analysis path - no second detector). Weights are fixed constants pinned by
tests; `metric_version` exists so downstream trend charts can detect a
formula change. The `insufficient-data` state (fewer than `min_runs`
recorded runs, or no scored tests) is a first-class outcome, not an error:
the badge renders "no data" rather than inventing a number. The badge is a
hand-rolled SVG (no dependencies) with `role="img"`, `aria-label`,
`<title>`/`<desc>` and `textLength`-constrained text; it is generated
locally and never pushed anywhere (see `docs/BADGE_WORKFLOW.md`).
