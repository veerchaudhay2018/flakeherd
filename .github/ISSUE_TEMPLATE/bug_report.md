---
name: Bug report
about: Something is misclassified, crashes, or behaves unexpectedly
title: "[bug] "
labels: bug
assignees: ''
---

**What happened**

A clear description of the wrong behavior. If a classification is wrong,
say what you expected and what you got (e.g. expected `regression`, got
`flaky`).

**How to reproduce**

1. Steps or a minimal repository/setup that triggers it.
2. The exact command(s), e.g. `flakeherd record -j 'junit.xml' --sha ...`
3. What happened (output, exit code).

**Environment**

- flakeherd version (`flakeherd --version`):
- Python version:
- OS / CI platform:
- Test framework + JUnit XML producer (pytest/jest/go/...):

**Report / evidence**

If a classification looks wrong, include the report table rows for the
test (redact anything sensitive), or attach a minimal sanitized JUnit XML
sample + `flakeherd report` output.

If the problem is in history sync, include the output of the failing
`ci-sync` command (tokens are auto-redacted, but please double-check).
