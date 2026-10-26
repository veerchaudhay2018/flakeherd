# Publishing the Test Health badge (optional, and how to do it safely)

`flakeherd health --badge test-health.svg` renders the badge **locally**.
There is no public badge URL and no badge service: FlakeHerd never uploads
anything anywhere. This document describes the deliberate design and a
secure recipe if you want the badge visible in your README.

## Why there is no automatic push

Automatically pushing generated files from CI is a well-known foot-gun:

* it needs a **write-capable token** in the workflow, which must never be
  exposed to untrusted code (test code, PR-authored plugins, Makefiles);
* it creates commits nobody reviewed, on branches that may be protected;
* a compromised PR could otherwise use the write path to tamper with your
  repository content.

So FlakeHerd generates the badge and stops there. What happens next is
your decision.

## Option 1 (simplest): commit the badge from your machine

```bash
python -m pytest --junitxml=junit.xml -q
flakeherd record --junit junit.xml --json-out report.json --no-report
flakeherd health --report report.json --badge test-health.svg
git add test-health.svg
git commit -m "chore: refresh test-health badge"
```

Then reference it in your README:

```markdown
![Test Health](test-health.svg)
```

The badge is a diff-able SVG in your repository - code review applies to
it like any other file.

## Option 2: CI artifact (no repository writes at all)

`examples/workflows/flakeherd-health.yml` uploads `test-health.svg` as a
workflow artifact with `permissions: contents: read` and no secrets. This
is the right default: nothing is written to the repository, and anyone can
download the latest badge from the run's artifact page.

## Option 3 (advanced): push to a dedicated branch from a trusted workflow

If you want the README badge to update automatically without committing to
`main`, use a **separate, manually-audited workflow** with these
properties:

1. **Dedicated fine-grained PAT**, *not* `secrets.GITHUB_TOKEN`:
   repository access limited to **one branch** (e.g. `badges/test-health`)
   with *Contents: Read and write* only. Store it as
   `secrets.BADGE_PUSH_TOKEN`.
2. **Never run this workflow on `pull_request`** - fork PRs must never get
   the token. Trigger on `push` to protected branches or
   `workflow_dispatch` only.
3. **Least-privilege everything**: `permissions: contents: read` at the
   workflow level (the PAT carries its own scope), `persist-credentials:
   false` on checkout, and never pass the token to your test steps.
4. **Do not run untrusted code in the same job.** Build the badge in a job
   that runs *your* released test suite, not PR-authored code; or split
   "run tests" (unprivileged) and "push badge" (privileged) into two jobs
   and pass the artifact between them.

Sketch (pushes to `badges/test-health` only):

```yaml
      - name: Push badge branch
        env:
          BADGE_TOKEN: ${{ secrets.BADGE_PUSH_TOKEN }}
        shell: bash
        run: |
          set -euo pipefail
          set +x

          if [ -z "${BADGE_TOKEN:-}" ]; then
            echo "BADGE_PUSH_TOKEN is not configured"
            exit 1
          fi

          askpass="$(mktemp)"
          trap 'rm -f "$askpass"; unset BADGE_TOKEN' EXIT

          cat > "$askpass" <<'EOF'
          #!/bin/sh
          case "$1" in
            *Username*) printf '%s\n' 'x-access-token' ;;
            *Password*) printf '%s\n' "$BADGE_TOKEN" ;;
            *) exit 1 ;;
          esac
          EOF

          chmod 700 "$askpass"
          export GIT_ASKPASS="$askpass"
          export GIT_TERMINAL_PROMPT=0

          git config user.name "flakeherd-badge"
          git config user.email "flakeherd-badge@users.noreply.github.com"

          git checkout --orphan badges/test-health
          git rm -rq --cached . 2>/dev/null || true
          git add test-health.svg
          git commit -qm "Update test health badge"
          git push -q \
            "https://github.com/${GITHUB_REPOSITORY}.git" \
            HEAD:badges/test-health --force
```

README reference (raw content URL of the dedicated branch):

```markdown
![Test Health](https://raw.githubusercontent.com/<owner>/<repo>/badges/test-health/test-health.svg)
```

## Honest limits

* The health score is a **heuristic** (see the formula in the README);
  the badge label says "of 100", never "reliable" or a probability.
* `insufficient-data` is a first-class state: with fewer than
  `--min-runs` recorded runs (default 5) the badge shows "no data" rather
  than a made-up number.
* Badge rendering is local and dependency-free; if you prefer a badge
  service, feed it `health.json` yourself - but then you are sending your
  (aggregated, non-identifying) metrics to a third party, which is exactly
  what FlakeHerd avoids elsewhere.
