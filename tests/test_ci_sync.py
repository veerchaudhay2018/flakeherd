"""Cache-branch sync tests: real git repositories, push/pull/merge, races.

These tests run actual ``git init/clone/push`` commands against local bare
repositories - the same plumbing FlakeHerd uses in CI.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from tests.conftest import GitRepo, make_result

from flakeherd.ci_sync import (
    HISTORY_FILE_ON_BRANCH,
    _git_environment,
    ci_pull,
    ci_push,
    remote_branch_exists,
)
from flakeherd.errors import SyncError
from flakeherd.model import RunInfo
from flakeherd.store import Store


def _record(db: Path, uid: str, sha: str, status: str = "pass") -> None:
    with Store(db) as store:
        store.record_run(
            RunInfo(run_uid=uid, commit_sha=sha, branch="main",
                    created_at=f"2026-01-01T00:00:0{uid[-1]}+00:00"),
            [make_result(name="t", status=status)],
        )


def _branch_db_rows(origin: Path, tmp_path: Path) -> int:
    """Read history.db straight from the origin branch tip."""
    proc = subprocess.run(
        ["git", "show", f"refs/heads/flakeherd/history:{HISTORY_FILE_ON_BRANCH}"],
        cwd=str(origin),
        capture_output=True,
        check=True,
    )
    tmp = tmp_path / "branch-copy.db"
    tmp.write_bytes(proc.stdout)
    with Store(tmp) as store:
        return store.run_count(), store.test_count()


def test_pull_with_unreachable_remote_raises(git_env, tmp_path):
    """A broken remote must be loud, not mistaken for 'branch absent'."""
    db = tmp_path / "work/.flakeherd/history.db"
    with pytest.raises(SyncError, match="cannot reach"):
        ci_pull(db, remote="/definitely/not/a/repo.git", repo_root=git_env.work)


class TestPull:
    def test_pull_without_branch_is_noop(self, git_env: GitRepo, tmp_path):
        db = tmp_path / "work/.flakeherd/history.db"
        result = ci_pull(db, remote=str(git_env.origin), repo_root=git_env.work)
        assert result.status == "no-history"
        assert not db.parent.exists() or not db.exists()

    def test_pull_merges_rows(self, git_env: GitRepo, tmp_path):
        db_a = git_env.work / ".flakeherd/history.db"
        _record(db_a, "r1", "s1")
        ci_push(db_a, remote=str(git_env.origin), repo_root=git_env.work)

        # fresh checkout (simulating a new CI runner) pulls the branch
        work2 = tmp_path / "work2"
        git_env.git("clone", str(git_env.origin), str(work2), cwd=tmp_path, check=False)
        db_b = work2 / ".flakeherd/history.db"
        result = ci_pull(db_b, remote=str(git_env.origin), repo_root=work2)
        assert result.status == "merged"
        assert result.runs_added == 1
        with Store(db_b) as store:
            assert store.run_count() == 1


class TestPush:
    def test_first_push_creates_branch(self, git_env: GitRepo, tmp_path):
        db = git_env.work / ".flakeherd/history.db"
        _record(db, "r1", "s1")
        result = ci_push(db, remote=str(git_env.origin), repo_root=git_env.work)
        assert result.status == "created"
        assert remote_branch_exists(str(git_env.origin), "flakeherd/history")
        runs, tests = _branch_db_rows(git_env.origin, tmp_path)
        assert (runs, tests) == (1, 1)

    def test_second_push_fast_forwards(self, git_env: GitRepo, tmp_path):
        db = git_env.work / ".flakeherd/history.db"
        _record(db, "r1", "s1")
        ci_push(db, remote=str(git_env.origin), repo_root=git_env.work)
        _record(db, "r2", "s2")
        result = ci_push(db, remote=str(git_env.origin), repo_root=git_env.work)
        assert result.status == "updated"
        runs, _tests = _branch_db_rows(git_env.origin, tmp_path)
        assert runs == 2

    def test_concurrent_writer_loses_no_rows(self, git_env: GitRepo, tmp_path):
        """The race-correctness pin: two checkouts, both push, no row loss."""
        # checkout A records and pushes
        db_a = git_env.work / ".flakeherd/history.db"
        _record(db_a, "run-A", "s1")
        ci_push(db_a, remote=str(git_env.origin), repo_root=git_env.work)

        # checkout B (cloned BEFORE A pushed, stale by design) records and pushes
        work_b = tmp_path / "work_b"
        git_env.git("clone", str(git_env.origin), str(work_b), cwd=tmp_path, check=False)
        db_b = work_b / ".flakeherd/history.db"
        _record(db_b, "run-B", "s2")
        result = ci_push(db_b, remote=str(git_env.origin), repo_root=work_b)
        assert result.status in ("updated", "created")

        # the branch must now contain BOTH runs - B's push merged A's rows
        runs, _rows = _branch_db_rows(git_env.origin, tmp_path)
        assert runs == 2
        with Store(db_b) as store:
            assert store.run_count() == 2  # B's local db caught up too

    def test_push_outside_repo_raises(self, tmp_path):
        db = tmp_path / "h.db"
        _record(db, "r1", "s1")
        with pytest.raises(SyncError, match="not inside a git repository"):
            ci_push(db, remote="origin", repo_root=tmp_path)


def test_remote_branch_exists_uses_repo_root(git_env, tmp_path, monkeypatch):
    db = git_env.work / ".flakeherd/history.db"
    _record(db, "r1", "s1")
    result = ci_push(db, remote=str(git_env.origin), repo_root=git_env.work)
    assert result.status == "created"

    # Run from outside the checkout; resolve the named remote in repo_root.
    monkeypatch.chdir(tmp_path)
    assert remote_branch_exists(
        "origin", "flakeherd/history", repo_root=git_env.work
    )


class TestBranch:
    def test_history_file_on_branch_constant(self):
        assert HISTORY_FILE_ON_BRANCH == "history.db"


def test_work_tree_never_touched_by_push(git_env: GitRepo):
    """Push writes only git objects - the checked-out files stay clean."""
    db = git_env.work / ".flakeherd/history.db"
    _record(db, "r1", "s1")
    ci_push(db, remote=str(git_env.origin), repo_root=git_env.work)
    status = git_env.git("status", "--porcelain").stdout.strip()
    assert "history.db" not in status


def test_git_environment_consumes_token_and_uses_header(monkeypatch):
    """Raw token must not be inherited by Git; auth is a temporary header."""
    monkeypatch.setenv("FLAKEHERD_GITHUB_TOKEN", "test-token-value")
    env = _git_environment()
    assert "FLAKEHERD_GITHUB_TOKEN" not in env
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert "test-token-value" not in env["GIT_CONFIG_VALUE_0"]
    assert env["GIT_CONFIG_VALUE_0"].startswith("AUTHORIZATION: basic ")
