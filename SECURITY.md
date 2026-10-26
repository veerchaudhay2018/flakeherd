# Security policy

## Supported versions

The initial release line (0.x) is supported on a best-effort basis.

## Threat model

FlakeHerd's attack surface is deliberately tiny:

| Surface | Handling |
| --- | --- |
| JUnit XML input (untrusted-ish: produced by your own test run, could be tampered in transit or by a dependency) | streamed via `xml.etree.iterparse` with per-element clearing; files > 64 MiB rejected before parsing; `.gz` decompressed under the same cap; malformed XML fails the run loudly, never silently. Python 3.9+'s ElementTree does not resolve external entities; DoS-style quadratic expansions are bounded by the size cap. Retry-evidence properties (`flakeherd_retry_*`) are treated like any other untrusted input: outcome strings are sanitized to `P`/`F`/`S` letters, the first-failure excerpt is capped (10 KiB) and never executed, and attempt counts must parse as positive integers. |
| SQLite database | local file, parameterized queries only; created with `PRAGMA foreign_keys=ON`; merged rows come from the cache branch you control. |
| Git operations | `GIT_TERMINAL_PROMPT=0` everywhere (no credential prompts in CI); all error output passes through a redaction step (`util.redact`) masking GitHub token shapes (`ghp_...`, `github_pat_...`, `Bearer ...`); commits use a fixed `flakeherd@users.noreply.github.com` identity, never the runner's config. |
| Network | none beyond `git fetch`/`git push` to your own remote. No telemetry, no API calls, no downloads. The `report`/`record` steps never touch the network. |
| Secrets | never stored in the database, manifests or reports; the composite action scopes its optional token to history sync steps and converts it to a Git HTTP header for Git subprocesses. |
| The composite action | installs only `actions/setup-python` (official) plus this repository itself, with `--no-deps`; use immutable action releases and, for the strongest supply-chain control, pin each action to a verified full commit SHA. |

## Concurrency and integrity

- Concurrent CI jobs share history through the cache branch; the push path
  merges the branch tip before pushing so rows are never silently dropped
  (see `docs/ARCHITECTURE.md` section 4). A pathological double-lost race
  can drop the loser's sample rows - detection is statistical and tolerates
  this; job correctness (the gate) only depends on the local database.
- The history branch contains test names and outcomes - the same
  information your CI logs already contain. If your test *names* are
  sensitive, use the `ignore` config patterns or do not enable the sync.

## Reporting a vulnerability

Please report via GitHub's **private vulnerability reporting** on this
repository (Security tab -> Report a vulnerability). Do not open a public
issue. Include reproduction steps and, if possible, a minimal XML or git
scenario. Expect an acknowledgement within a week.


## GitHub Actions token handling

When using the composite action, configure `actions/checkout` with
`persist-credentials: false` and pass `github-token: ${{ secrets.GITHUB_TOKEN }}`
to the action. The token is set only in the history pull/push steps and is not
passed to the record/test step. The action treats input values as environment
variables rather than interpolating them into shell source. Do not grant
`contents: write` to workflows that execute untrusted pull-request code.
