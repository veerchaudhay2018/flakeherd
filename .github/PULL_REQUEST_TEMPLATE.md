## Summary

What this PR changes and why. One or two sentences for humans.

## Type of change

- [ ] Bug fix (behavior change for existing functionality)
- [ ] New feature
- [ ] Detection/classification change (see extra requirements below)
- [ ] Documentation
- [ ] CI / tooling

## Checklist

- [ ] `python -m pytest -q` passes locally
- [ ] `ruff check src tests` is clean
- [ ] `bash demo/run_demo.sh` completes (if parser/store/detector changed)
- [ ] Tests added or updated for the behavior change
- [ ] `docs/ARCHITECTURE.md` updated if taxonomy, score, gate or storage
      semantics changed (same PR, please)
- [ ] README updated for user-visible changes
- [ ] No new runtime dependencies (or an explicit, argued exception)

## Detection/classification changes only

- Which rule(s) changed, and why the old behavior was wrong or incomplete
- Example outcome sequences (`PFPF`-style) that must now classify
  differently, and the test that pins it
- Why the new behavior is conservative-by-default (or what opted in)
