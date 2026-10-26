"""Structural validation of the composite action and CI workflows.

Plain ``yaml.safe_load`` (the ``action-schema`` CI job) proves the files
parse; it cannot prove they are *correct*. These tests pin down the
release-critical invariants that were either fixed or introduced in 0.1.1:

* user-controlled action inputs must never be interpolated into shell
  source (shell-injection fix - regression guard);
* the optional sync token must reach only the ``ci-sync`` steps, never the
  step that parses JUnit reports produced by test code;
* workflows must declare least-privilege permissions, and the one workflow
  holding ``contents: write`` must be manually triggered only;
* ``actions/checkout`` in the dogfood workflow must not persist credentials;
* referenced action versions must be exact pins, not floating major tags.

Everything here runs offline: behaviour on a real GitHub-hosted runner
remains a separate, documented manual verification step.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
ACTION = REPO_ROOT / "action.yml"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
DOGFOOD = REPO_ROOT / "docs" / "experimental" / "dogfood.yml"


def _load(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    assert isinstance(doc, dict), f"{path.name} did not parse to a mapping"
    return doc


def _on(doc: dict) -> dict:
    """Return the ``on:`` trigger block (PyYAML parses bare ``on`` as True)."""
    triggers = doc.get("on", doc.get(True))
    assert isinstance(triggers, dict), "workflow has no 'on:' block"
    return triggers


def _steps(doc: dict) -> list[dict]:
    steps = []
    for job in doc.get("jobs", {}).values():
        steps.extend(job.get("steps", []))
    return steps


class TestCompositeAction:
    def test_composite_structure(self):
        doc = _load(ACTION)
        assert doc["runs"]["using"] == "composite"
        assert isinstance(doc["runs"]["steps"], list)

    def test_inputs_never_interpolated_into_shell_source(self):
        """Regression test for the 0.1.1 shell-injection fix.

        Any ``${{ inputs.* }}`` inside a ``run:`` block becomes shell
        source at job time; inputs must flow through env + quoted vars.
        """
        for step in _load(ACTION)["runs"]["steps"]:
            run = step.get("run", "")
            assert "${{ inputs." not in run, (
                f"step {step.get('name')!r} interpolates inputs into shell source"
            )

    def test_shell_steps_declare_bash(self):
        for step in _load(ACTION)["runs"]["steps"]:
            if "run" in step:
                assert step.get("shell") == "bash", (
                    f"step {step.get('name')!r} runs without an explicit bash shell"
                )

    def test_sync_token_scoped_to_ci_sync_steps_only(self):
        """The token must never reach the step that parses test output."""
        doc = _load(ACTION)
        token_steps = []
        for step in doc["runs"]["steps"]:
            env = step.get("env", {})
            if "FLAKEHERD_GITHUB_TOKEN" in env:
                token_steps.append(step)
        assert token_steps, "no step consumes FLAKEHERD_GITHUB_TOKEN"
        for step in token_steps:
            run = step.get("run", "")
            assert "ci-sync" in run, (
                f"step {step.get('name')!r} receives the token but is not a sync step"
            )
        for step in doc["runs"]["steps"]:
            if "ci-sync" not in step.get("run", ""):
                env = step.get("env", {})
                assert "FLAKEHERD_GITHUB_TOKEN" not in env

    def test_github_token_input_is_optional(self):
        inputs = _load(ACTION)["inputs"]
        assert "github-token" in inputs
        assert inputs["github-token"].get("required") is not True


class TestCIWorkflow:
    def test_triggers(self):
        triggers = _on(_load(CI))
        assert triggers["push"]["branches"] == ["main"], "push trigger must cover main"
        assert "pull_request" in triggers

    def test_least_privilege_permissions(self):
        assert _load(CI)["permissions"] == {"contents": "read"}

    def test_action_references_are_pinned(self):
        """Major-version floating tags are mutable; pin at least major.minor."""
        for step in _steps(_load(CI)):
            ref = step.get("uses", "")
            if ref:
                version = ref.split("@", 1)[1]
                assert version.count(".") >= 1, f"unpinned action reference: {ref}"


class TestDogfoodWorkflow:
    def test_manual_trigger_only(self):
        """contents: write must never apply to automatic untrusted events."""
        triggers = _on(_load(DOGFOOD))
        assert list(triggers) == ["workflow_dispatch"], (
            f"dogfood must be manual-only, got: {list(triggers)}"
        )

    def test_permissions_are_write_for_history_push_only(self):
        assert _load(DOGFOOD)["permissions"] == {"contents": "write"}

    def test_checkout_does_not_persist_credentials(self):
        for step in _steps(_load(DOGFOOD)):
            if step.get("uses", "").startswith("actions/checkout"):
                assert step.get("with", {}).get("persist-credentials") is False

    def test_token_passed_to_action(self):
        uses = [s.get("uses", "") for s in _steps(_load(DOGFOOD))]
        assert any(u == "./" for u in uses), "dogfood must exercise the local action"
        for step in _steps(_load(DOGFOOD)):
            if step.get("uses") == "./":
                assert step.get("with", {}).get("github-token") == "${{ secrets.GITHUB_TOKEN }}"

    def test_action_references_are_pinned(self):
        for step in _steps(_load(DOGFOOD)):
            ref = step.get("uses", "")
            if ref and not ref.startswith("./"):
                version = ref.split("@", 1)[1]
                assert version.count(".") >= 1, f"unpinned action reference: {ref}"


class TestExampleWorkflow:
    """The user-facing example must demonstrate the safe pattern too."""

    def test_example_uses_ephemeral_credentials(self):
        path = REPO_ROOT / "examples" / "workflows" / "flakeherd-basic.yml"
        doc = _load(path)
        for step in _steps(doc):
            if step.get("uses", "").startswith("actions/checkout"):
                assert step.get("with", {}).get("persist-credentials") is False, (
                    "example must show persist-credentials: false"
                )

    def test_example_passes_sync_token(self):
        """Without persisted credentials the example must pass github-token,
        otherwise its history sync could never authenticate."""
        path = REPO_ROOT / "examples" / "workflows" / "flakeherd-basic.yml"
        uses = [s.get("uses", "") for s in _steps(_load(path))]
        assert any("flakeherd" in u for u in uses), "example must use the action"
        for step in _steps(_load(path)):
            if "flakeherd" in step.get("uses", ""):
                assert step.get("with", {}).get("github-token")


@pytest.mark.parametrize("path", [ACTION, CI, DOGFOOD])
def test_yaml_parses(path: Path):
    _load(path)


class TestHealthExampleWorkflow:
    """The badge example must demonstrate the safe pattern too.

    Invariants: least-privilege (read-only) permissions because nothing
    writes to the repository, ephemeral checkout credentials, no secrets
    referenced anywhere, and pinned action references.
    """

    HEALTH = REPO_ROOT / "examples" / "workflows" / "flakeherd-health.yml"

    def test_yaml_parses(self):
        _load(self.HEALTH)

    def test_read_only_permissions(self):
        assert _load(self.HEALTH)["permissions"] == {"contents": "read"}

    def test_checkout_does_not_persist_credentials(self):
        for step in _steps(_load(self.HEALTH)):
            if step.get("uses", "").startswith("actions/checkout"):
                assert step.get("with", {}).get("persist-credentials") is False

    def test_no_secrets_referenced(self):
        """The badge flow needs no tokens at all; keep it that way."""
        text = self.HEALTH.read_text(encoding="utf-8")
        assert "secrets." not in text, "health badge example must not use secrets"

    def test_no_git_write_commands(self):
        """Nothing in the example pushes to the repository."""
        text = self.HEALTH.read_text(encoding="utf-8")
        for forbidden in ("git push", "ci-sync --mode push"):
            assert forbidden not in text

    def test_action_references_are_pinned(self):
        for step in _steps(_load(self.HEALTH)):
            ref = step.get("uses", "")
            if ref:
                version = ref.split("@", 1)[1]
                assert version.count(".") >= 1, f"unpinned action reference: {ref}"

    def test_runs_tests_before_recording(self):
        """The pipeline mirrors the documented flow: junit -> record -> health."""
        names = [s.get("name") or s.get("uses", "") for s in _steps(_load(self.HEALTH))]
        run_idx = next(i for i, n in enumerate(names) if n and "Run tests" in n)
        record_idx = next(i for i, n in enumerate(names) if n and "Record and compute" in n)
        assert run_idx < record_idx


class TestExampleWorkflowRegression:
    """Guards the example workflow defects fixed in 0.2.0."""

    BASIC = REPO_ROOT / "examples" / "workflows" / "flakeherd-basic.yml"

    def test_push_trigger_is_well_formed(self):
        # 0.2.0 fixed 'branches: ain]' (a malformed trigger that would have
        # limited the workflow to a branch literally named "ain]")
        triggers = _on(_load(self.BASIC))
        assert triggers["push"]["branches"] == ["main"]

    def test_action_references_are_pinned(self):
        # 'you/flakeherd@main' is the documented placeholder for the
        # repository that does not exist yet (the example comments say to
        # replace it); everything else must be pinned.
        for step in _steps(_load(self.BASIC)):
            ref = step.get("uses", "")
            if ref and not ref.startswith("./") and not ref.startswith("you/flakeherd"):
                version = ref.split("@", 1)[1]
                assert version.count(".") >= 1, f"unpinned action reference: {ref}"
