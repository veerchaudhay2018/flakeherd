# Contributing to FlakeHerd

Thanks for wanting to help. FlakeHerd is intentionally small - the whole
core is ~1,600 lines of dependency-free Python - and it should stay
reviewable in an afternoon.

## Development setup

```bash
git clone <your-fork-url> && cd flakeherd
python3 -m venv .venv && source .venv/bin/activate
pip install -e . -r requirements-dev.txt

python -m pytest -q      # everything must pass (287 tests in this release)
ruff check src tests      # must be clean
bash demo/run_demo.sh    # must complete; read the output
```

## Project layout

```
src/flakeherd/     the package (core modules, reporters, pytest plugin)
tests/             pytest suite - one file per module under test
demo/              the reproducible three-part demo
examples/          sample JUnit files + a full user workflow
docs/              ARCHITECTURE.md, RESEARCH.md, flow diagram, demo transcript
action.yml         the GitHub Actions composite action
```

Read `docs/ARCHITECTURE.md` before changing detection or storage
behavior - the taxonomy rules and the sync correctness rule are specified
there and pinned by tests.

## Design principles (what we trade off in favor of what)

1. **History over reruns.** Never burn CI compute to manufacture evidence
   the user's own history already contains.
2. **Conservative by default.** Nothing quarantines, gates, or deletes
   without explicit opt-in; low-confidence candidates are reported, not
   acted on; a new failure still fails the build.
3. **Data stays in the repo.** No network except the user's own git remote;
   no accounts, no telemetry, no SaaS.
4. **Zero runtime dependencies.** If a change needs a third-party library,
   the bar is very high and must be argued in the PR.
5. **Honest output.** Warnings are surfaced, trade-offs are documented,
   untested claims are labeled (see the README's limitations section).

## Making changes

- Keep the taxonomy decisions in `docs/ARCHITECTURE.md` in sync with
  `detector.py`; update both in the same PR.
- New behavior needs tests first or with the change. Detector rules:
  add the scenario to `tests/test_detector.py` with a `PFPF`-style pattern.
- Parser changes must cover at least one real-world XML flavor
  (`tests/test_junit.py` fixtures show the flavors).
- CI runs `ruff`, the full test matrix (Python 3.9-3.13) and the demo.

## Submitting

1. Fork, branch (`feat/...` or `fix/...`), commit with clear messages.
2. Ensure `pytest`, `ruff`, and the demo pass locally.
3. Open a PR describing the user-visible change and the reasoning,
   especially for classification or storage changes.
4. For taxonomy/score changes, expect discussion - false negatives and
   false positives both have real costs.

## Reporting bugs

Use the issue templates. For security matters, follow `SECURITY.md`
(please do not open public issues for vulnerabilities).

## Releasing (maintainers)

1. Update `src/flakeherd/__init__.py` (`__version__`) and `CHANGELOG.md`.
2. Tag `vX.Y.Z`; CI publishes the package (once configured).
3. Mark the action tag (`v1`, `v1.1`) after verifying a real run.
