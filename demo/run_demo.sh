#!/usr/bin/env bash
# FlakeHerd end-to-end demonstration.
#
# Part A - deterministic history simulation: eight recorded "CI runs" made
#           of JUnit XML; shows the full classification taxonomy.
# Part B - a real pytest suite with a genuinely flaky test: recorded across
#           eight real test executions, then quarantined and skipped.
# Part C - auto-retry with preserved evidence: a transient test recovers on
#           retry (suspected-flaky evidence in the XML), a genuinely broken
#           test stays red through every attempt; then the quarantine
#           policy (suggestion mode) and the Test Health badge.
# Part D - the cache-branch sync: two checkouts sharing one bare "origin",
#           exactly like two CI runners; history accumulates across pushes.
#
# Prerequisites: python3 (>= 3.9), git, pytest, and `pip install -e .`
# from the repository root. Everything runs locally; nothing leaves your
# machine except nothing at all.
set -euo pipefail

PYTHON="${PYTHON:-python3}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/flakeherd-demo-XXXXXX")"

banner() {
  echo
  echo "=============================================================="
  echo "== $*"
  echo "=============================================================="
}

note() { echo; echo "-- $* --"; }

run_quietly() { "$@" >/dev/null 2>&1 || true; }

banner "FlakeHerd demo - scratch dir: $SCRATCH"

# ---------------------------------------------------------------------------
banner "PART A - deterministic history simulation (the taxonomy)"
# ---------------------------------------------------------------------------

"$PYTHON" "$HERE/build_history.py" "$SCRATCH/part-a"
cd "$SCRATCH/part-a"

SHAS=("commit-0001" "commit-0002" "commit-0003" "commit-0003"
      "commit-0005" "commit-0006" "commit-0007" "commit-0008")

note "recording the 8 runs (like 8 CI jobs would)"
for i in 1 2 3 4 5 6 7; do
  "$PYTHON" -m flakeherd record \
    -j "run_$i.xml" \
    --sha "${SHAS[$((i-1))]}" \
    --run-uid "ci-run-$i" \
    --no-report
done

note "recording run 8 and printing the report"
"$PYTHON" -m flakeherd record \
  -j "run_8.xml" \
  --sha "${SHAS[7]}" \
  --run-uid "ci-run-8"

# ---------------------------------------------------------------------------
banner "PART B - real pytest suite with a genuinely flaky test"
# ---------------------------------------------------------------------------

mkdir -p "$SCRATCH/part-b/results"
cd "$SCRATCH/part-b"

note "running the real suite 8 times (DEMO_RUN seeds a deterministic flake; runs 3 and 4 share a commit)"
for i in 1 2 3 4 5 6 7 8; do
  if [ "$i" -eq 3 ] || [ "$i" -eq 4 ]; then SHA="feature-rerun"; else SHA="commit-$i"; fi
  DEMO_RUN="$i" "$PYTHON" -m pytest "$HERE/demo-project" \
    --junitxml="results/run_$i.xml" -q >/dev/null 2>&1 || true
  OUT=$("$PYTHON" -m flakeherd record \
    -j "results/run_$i.xml" \
    --sha "$SHA" \
    --run-uid "pytest-run-$i" \
    --no-report 2>&1)
  echo "$OUT" | grep -E "recorded|error" || true
done

note "what FlakeHerd knows after 8 real runs"
"$PYTHON" -m flakeherd report

note "writing the quarantine manifest"
"$PYTHON" -m flakeherd quarantine

note "running the suite once more - the quarantined test now SKIPS with a reason"
export FLAKEHERD_MANIFEST="$SCRATCH/part-b/.flakeherd/quarantine.json"
DEMO_RUN=9 "$PYTHON" -m pytest "$HERE/demo-project" -q
unset FLAKEHERD_MANIFEST

# ---------------------------------------------------------------------------
banner "PART C - auto-retry with preserved evidence + test health badge"
# ---------------------------------------------------------------------------

mkdir -p "$SCRATCH/part-c"
cd "$SCRATCH/part-c"

note "running the retry demo suite with --flakeherd-retries 1 (one extra attempt per failure)"
"$PYTHON" -m pytest "$HERE/retry-project" \
  --flakeherd-retries 1 \
  --junitxml="junit.xml" -q >/dev/null 2>&1 || true

note "what the JUnit XML recorded (retry evidence: per-attempt outcomes + first failure)"
"$PYTHON" - "$SCRATCH/part-c/junit.xml" <<'PY'
import sys
import xml.etree.ElementTree as ET

tree = ET.parse(sys.argv[1])
for elem in tree.iter("testcase"):
    props = {p.get("name"): p.get("value") for p in elem.iter("property")}
    if props:
        name = elem.get("classname") + "::" + elem.get("name")
        print(f"  {name}")
        print(f"    attempts : {props.get('flakeherd_retry_attempts')}")
        print(f"    outcomes : {props.get('flakeherd_retry_outcomes')}")
        excerpt = (props.get("flakeherd_retry_first_failure") or "")[:60]
        if excerpt:
            print(f"    1st fail : {excerpt}...")
PY

note "recording five runs with the same retry pattern (a recovered test never gates; a test failing every attempt stays a failure)"
# one recovered run alone is 'suspected flaky'; recovering in *every* run
# is a demonstrated 50% failure rate - the effective attempt sequence
# (F,P per run) accumulates transitions and escalates the class to 'flaky'.
for i in 1 2 3 4 5; do
  "$PYTHON" -m flakeherd record \
    -j "junit.xml" \
    --sha "retry-demo-sha-$i" \
    --run-uid "retry-run-$i" \
    --json-out report.json \
    --no-report
done
"$PYTHON" -m flakeherd report | grep -E "suspected|retried|classes" || true

note "enabling the quarantine policy (threshold 40, min 5 samples) - suggestion mode"
cat > .flakeherd.json <<'JSON'
{
  "window": 30,
  "quarantine_threshold_enabled": true,
  "quarantine_score_threshold": 40,
  "quarantine_min_samples": 5,
  "quarantine_expiry_days": 30
}
JSON
# The transient test qualifies: five demonstrated flakes, score 80 >= 40,
# 5 samples >= 5. The broken test never qualifies (quarantine must not
# hide bugs). --dry-run writes nothing.
"$PYTHON" -m flakeherd quarantine --dry-run

note "computing the Test Health metric and rendering the badge locally"
# weights: stable-pass 100, flaky 0, broken 0 -> (100+0+0)/3 = 33/100
"$PYTHON" -m flakeherd health --report report.json --badge test-health.svg --json-out health.json
echo "badge (first 2 lines):"
head -2 test-health.svg

note "insufficient-data handling: a fresh database with one run"
mkdir -p "$SCRATCH/part-c-empty" && cd "$SCRATCH/part-c-empty"
"$PYTHON" -m flakeherd record \
  -j "$SCRATCH/part-c/junit.xml" \
  --sha "empty-sha" --run-uid "only-run" --json-out report.json --no-report
"$PYTHON" -m flakeherd health --report report.json --badge no-data.svg
echo "badge (first 2 lines):"
head -2 no-data.svg
cd "$SCRATCH/part-c"
rm -f .flakeherd.json

# ---------------------------------------------------------------------------
banner "PART D - history sync over a cache branch (two CI runners)"
# ---------------------------------------------------------------------------

mkdir -p "$SCRATCH/part-d"
cd "$SCRATCH/part-d"
export GIT_AUTHOR_NAME=demo GIT_AUTHOR_EMAIL=demo@example.com
export GIT_COMMITTER_NAME=demo GIT_COMMITTER_EMAIL=demo@example.com

note "creating a bare origin and a first checkout (runner 1)"
git init --bare -b main origin.git
git clone origin.git runner1 2>/dev/null
cd runner1
echo "# project" > README.md
git add README.md
git commit -q -m "initial commit"

cat > junit-a.xml <<'XML'
<?xml version="1.0"?>
<testsuites><testsuite name="s">
  <testcase classname="app.CalcTest" name="test_add" file="app/test_calc.py" line="4"/>
  <testcase classname="app.CalcTest" name="test_div" file="app/test_calc.py" line="9"/>
</testsuite></testsuites>
XML

note "runner 1 records its results and pushes history to the cache branch"
"$PYTHON" -m flakeherd record -j junit-a.xml --sha run1-sha --run-uid ci-run-1 --no-report
"$PYTHON" -m flakeherd ci-sync --mode push

note "runner 2 = a completely fresh clone; it pulls the shared history first"
cd "$SCRATCH/part-d"
git clone origin.git runner2 2>/dev/null
cd runner2
"$PYTHON" -m flakeherd ci-sync --mode pull

cat > junit-b.xml <<'XML'
<?xml version="1.0"?>
<testsuites><testsuite name="s">
  <testcase classname="app.CalcTest" name="test_add" file="app/test_calc.py" line="4"/>
</testsuite></testsuites>
XML

note "runner 2 records its own results and pushes; both histories merge on the branch"
"$PYTHON" -m flakeherd record -j junit-b.xml --sha run2-sha --run-uid ci-run-2 --no-report
"$PYTHON" -m flakeherd ci-sync --mode push

note "the cache branch contains both runners' runs"
git ls-remote origin refs/heads/flakeherd/history
git fetch --quiet origin flakeherd/history
git log --oneline FETCH_HEAD

note "runner 2's report now sees the combined history of both runners"
"$PYTHON" -m flakeherd report

banner "Demo complete - scratch dir kept at: $SCRATCH (delete at will)"
