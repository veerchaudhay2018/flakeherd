# Research, originality audit and design decisions

This file records the evidence behind FlakeHerd: the problem, the existing
solutions, why the open gap exists, and every significant design decision.
Research date: 2026-10-25. Sources were checked directly unless noted.

## 1. The problem is real and measured

| Evidence | Finding |
| --- | --- |
| Parry et al., ACM TOSEM survey ("Fix the tests") | 59% of developers report flaky tests at least monthly |
| Bitrise Mobile Insights 2025 (10M+ builds) | 26% of teams affected by flakiness, up from 10% in 2022 |
| arXiv 2602.02307 (Feb 2026), 1,960 Java OSS projects | 51.28% of projects show flaky re-runs; 15 root-cause categories |
| GitHub community discussion #163123 | users requesting native test-history/flaky dashboards in Actions |
| BuildPulse marketing (Aug 2026) | states outright: "GitHub Actions has no built-in flaky-test detection" |
| Semaphore "47% of manually restarted jobs pass on retry" | the cost is paid as human time, not just compute |

Interpretation: the pain is common, chronic, and growing; GitHub itself
provides no detection; teams improvise with reruns (`|| true`), deletion
of tests, or paid analytics.

## 2. Existing solutions (checked 2026-10-25)

### Commercial SaaS
- **BuildPulse** - flaky test detection as a service; GitHub Actions
  integration; data leaves your repo; paid.
- **Trunk (Flaky Tests)** - dev-productivity SaaS incl. flaky test
  quarantine; paid.
- **Datadog CI Visibility / Test Intelligence** - flakiness as part of a
  large observability platform; paid, agent-based.
- **Flakehouse** (flakehouse-style services) - watches repositories and
  "tells you which failure is real"; hosted product.
- **Launchable** - ML test selection/prediction incl. flaky insights; paid.

### Open source / free
- **smartcontractkit/flakeguard** (Go, MIT) - a *rerun-based* guard: re-runs
  failing Go tests and looks for pass/fail divergence; Chainlink's internal
  tool, released 2025, ~0 stars, Go ecosystem only. Same name, different
  mechanism (see 4.3).
- **ncbi/deflake** (Python, small) - runs a flaky command repeatedly until
  it fails; a debugging helper, no history, no CI integration.
- **oslabs-beta/flake-guard-alpha** - Jest-only flaky test manager; archived
  research-project shape.
- **Flake-doctor (ChiragPatankar)** - reads CI history for pytest flakes;
  pytest-specific, single-maintainer demo project.
- **flakelens** (PyPI) - separates "real LLM failures from flaky ones";
  ML-eval focused.
- **WithSecureOpenSource/flaky-tests-detection** - the closest prior OSS
  GitHub Action (analyze CI for flakes) - **dead since Dec 2022, ~26
  stars**. This is the concrete gap: demand existed, the maintainer moved
  on, nothing active replaced it.
- **flakeguard.com / npx flake-guard** - npm wrapper that re-executes E2E
  tests N times; rerun-based, JS E2E only.

**Conclusion:** every serious, maintained option is either a paid service
or rerun-based/single-ecosystem. The niche "free + cross-framework +
history-based + GitHub-native + data-stays-in-your-repo" is unoccupied.

## 3. Name audit (originality)

Checked GitHub and PyPI before committing to the name:

| Name | Status |
| --- | --- |
| `flakeguard` | **taken** - smartcontractkit/flakeguard (Go, MIT), thc1006/flakeguard, flakeguard.com npm tool |
| `flakelens` | **taken** - PyPI package (MIT), referenced in tooling discussions |
| `flakeaudit` | **taken** - Determinate Systems (Nix SBOM auditing, Rust) |
| `reflake` | **taken** - PyPI (data versioning engine, AGPL) |
| `deflake` | **taken** - ncbi/deflake |
| `flake-doctor` | taken (pytest-focused project) |
| **`flakeherd`** | **no GitHub or PyPI hit found** - selected |

"Herd" fits the product promise: watch the tests, separate the sheep from
the goats, and pen the flakes. It is short, spellable, and verb-able
(`flakeherd quarantine`).

## 4. Design decisions

### 4.1 History-based, not rerun-based
Rerun tools (smartcontractkit/flakeguard, flake-guard npm) *cause* extra CI
compute to manufacture evidence. FlakeHerd instead accumulates evidence
that already exists - every CI run produces results; recording them is
free. The one exception is the strongest signal in the taxonomy: when a
human re-runs a failed job, GitHub provides the same commit + different
outcome, and FlakeHerd's same-sha divergence check captures it without
spending any compute.

### 4.2 JUnit XML as the universal adapter
One parser covers pytest, jest, vitest, Go (via go-junit-report),
Java/Kotlin (Surefire/Gradle), cargo-nextest, Playwright, Cypress, .NET
(JUnitXml.TestLogger), and anything else that speaks the de-facto standard.
Language-agnostic input is the main axis on which the OSS predecessors
failed (Go-only, jest-only, pytest-only).

### 4.3 SQLite on a cache branch (the zero-server bet)
SaaS analytics is the natural design - and the one that makes test data
leave the repo. FlakeHerd's storage is a plain SQLite file, synced through
a branch (`flakeherd/history`) with git plumbing: inspect with `git log`,
revert with `git revert`, audit with `git diff`, access-control with branch
protection. Trade-offs accepted: last-writer-wins between concurrent CI
jobs (mitigated by merge-before-push + one retry + idempotent row merge;
see ARCHITECTURE.md 4), and the job needs `contents: write`.

### 4.4 An explicit taxonomy, not a flaky bit
"Flaky" is usually one boolean. FlakeHerd separates `confirmed-flaky`
(same commit diverged, or in-run retries), `flaky` (oscillation),
`flaky-low` (2 transitions - could be a fix!), `regression`, `broken`,
`fixed`, `watch`, `new`. The distinction matters operationally: quarantine
is safe for the first two, dangerous for broken/regression (hides bugs),
and premature for low-confidence cases. Defaults are conservative.

### 4.5 Gate on "unexplained failures", not on "any failure"
With `fail-on: unexplained`, currently-failing tests fail the job *unless*
their history carries flaky evidence. Rationale: tests are innocent until
proven flaky - a brand-new failure must still break the build, or the gate
becomes an open door for real regressions. This pairs with the user's
`|| true` on the test step: FlakeHerd becomes the arbiter of whether the
job should be red.

### 4.6 Quarantine that stays honest
Deleting flaky tests loses coverage silently. Skipping via
`pytest.mark.skip` keeps a visible record ("skipped: flakeherd quarantine
[confirmed-flaky]: commit X produced both a pass and a fail") in every
test run, and the manifest is stable, diff-able JSON meant to be committed
and code-reviewed.

### 4.7 Zero runtime dependencies
Supply-chain surface area is a feature decision: pure stdlib (sqlite3,
ElementTree, subprocess) means `pip install flakeherd` pulls nothing
else, the composite action installs with `--no-deps`, and the whole
attack surface is Python's own. XML parsing is streaming with size caps
(see SECURITY.md).

### 4.8 License: MIT
A CI tool that gets embedded in other people's pipelines must be
maximally permissive or it will not be embedded. GPL would deter exactly
the corporate adopters this problem needs; MIT matches the prior art
(promptfoo MIT, smartcontractkit/flakeguard MIT) and the ecosystem norm.

## 5. Positioning statement (for Show HN / README)

*Flaky tests waste 59% of teams' time. Rerun tools burn extra CI compute;
SaaS ships your test data elsewhere. FlakeHerd takes the third road: it
records the test history your CI already produces, keeps it on a branch in
your own repo, classifies every test from real evidence (proven flakes vs
regressions vs still-broken), and can gate your build so only unexplained
failures turn it red. Any JUnit XML, zero dependencies, MIT.*
