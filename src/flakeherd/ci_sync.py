"""History sync over a "cache branch" - the zero-server persistence layer.

How it works (all inside your own repository - nothing leaves it):

* ``ci-pull``: fetch the history branch, extract ``history.db`` from the
  branch tip as a blob, and merge its rows into the local database with
  ``INSERT OR IGNORE`` (idempotent, order-independent).
* ``ci-push``: write the local database as a blob, build a one-file tree via
  git plumbing (no working-tree checkout, safe inside a CI checkout), commit
  it as a child of the current branch tip, and push.

The history branch (default ``flakeherd/history``) accumulates one commit per
CI run and can be inspected, diffed and reverted with plain git.

Concurrency: two jobs pushing at the same time produce a non-fast-forward
rejection for the loser. FlakeHerd then pulls, merges and retries once; if
the retry also loses the race, it leaves the local analysis untouched and
reports a warning instead of failing your build. Flakiness detection is
statistical, so an occasionally lost row set is acceptable - and vastly
preferable to data-locking complexity.

In GitHub Actions, pass a token only to the pull/push steps and set
``actions/checkout`` to ``persist-credentials: false``. The token is converted
to a temporary Git HTTP authorization header for Git subprocesses and removed
from their child environment. Do not expose a write-capable token to test code.
"""

from __future__ import annotations

import base64
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import SyncError
from .store import Store
from .util import redact, utc_now_iso

HISTORY_FILE_ON_BRANCH = "history.db"
_GIT_IDENTITY = ("flakeherd", "flakeherd@users.noreply.github.com")
_TIMEOUT = 180


def _git_environment(env_extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a Git subprocess environment with step-scoped token auth.

    ``FLAKEHERD_GITHUB_TOKEN`` is consumed here and removed before spawning Git,
    so the raw token is not passed to Git or printed in diagnostics. The action
    sets this variable only on history pull/push steps, never on test execution.
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if env_extra:
        env.update(env_extra)
    token = env.pop("FLAKEHERD_GITHUB_TOKEN", "")
    if token:
        try:
            count = int(env.get("GIT_CONFIG_COUNT", "0"))
        except ValueError:
            count = 0
        auth = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
        env[f"GIT_CONFIG_KEY_{count}"] = "http.https://github.com/.extraheader"
        env[f"GIT_CONFIG_VALUE_{count}"] = f"AUTHORIZATION: basic {auth}"
        env["GIT_CONFIG_COUNT"] = str(count + 1)
    return env


def _git_rc(
    *args: str,
    cwd: str | Path | None = None,
    env_extra: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run git; return ``(returncode, stdout, stderr)`` without raising."""
    env = _git_environment(env_extra)
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=os.fspath(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            env=env,
        )
    except FileNotFoundError as exc:  # git not installed
        raise SyncError("git executable not found - is git installed?") from exc
    except subprocess.TimeoutExpired as exc:
        raise SyncError(f"git {args[0]} timed out after {_TIMEOUT}s") from exc
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def _git(
    *args: str,
    cwd: str | Path | None = None,
    env_extra: dict[str, str] | None = None,
) -> str:
    """Run git, return stdout; raises SyncError on failure."""
    rc, out, err = _git_rc(*args, cwd=cwd, env_extra=env_extra)
    if rc != 0:
        detail = redact((err or out) or f"exit {rc}")
        raise SyncError(f"git {' '.join(args[:2])} failed: {detail}")
    return out


def _git_blob(
    *args: str,
    cwd: str | Path | None = None,
) -> tuple[int, bytes]:
    """Run git expecting binary stdout (e.g. ``show <rev>:<file>``).

    Returns ``(returncode, stdout_bytes)``. Text mode must never be used
    here: the history database is a binary SQLite file and any
    decode/re-encode round-trip would corrupt it.
    """
    env = _git_environment()
    proc = subprocess.run(
        ["git", *args],
        cwd=os.fspath(cwd) if cwd else None,
        capture_output=True,
        timeout=_TIMEOUT,
        env=env,
    )
    return proc.returncode, proc.stdout or b""


def _repo_root(cwd: str | Path | None = None) -> Path:
    try:
        top = _git("rev-parse", "--show-toplevel", cwd=cwd)
    except SyncError as exc:
        raise SyncError("not inside a git repository (history sync needs one)") from exc
    return Path(top)


def remote_branch_exists(
    remote: str,
    branch: str,
    repo_root: str | Path | None = None,
) -> bool:
    """True when *remote* has *branch*. Empty ls-remote output = absent.

    A failed ``ls-remote`` (auth, network) raises instead of masquerading
    as "branch absent" - a broken remote should be loud, not silent.
    """
    rc, out, err = _git_rc(
        "ls-remote", "--heads", remote, f"refs/heads/{branch}",
        cwd=repo_root,
    )
    if rc != 0:
        raise SyncError(
            f"cannot reach remote {remote!r}: " + redact(err or f"git exited {rc}")
        )
    return bool(out.strip())


def _fetch(remote: str, branch: str, root: Path) -> None:
    _git(
        "fetch", "--quiet", remote, f"refs/heads/{branch}",
        cwd=root,
    )


def _fetch_tip(remote: str, branch: str, root: Path) -> str:
    """Fetch *branch* and return the sha of its tip (FETCH_HEAD)."""
    _fetch(remote, branch, root)
    return _git("rev-parse", "FETCH_HEAD", cwd=root)


@dataclass
class PullResult:
    status: str  # "merged" | "no-history" | "empty-history"
    runs_added: int = 0
    outcomes_added: int = 0


@dataclass
class PushResult:
    status: str  # "created" | "updated" | "rejected"
    commit: str = ""
    retried: bool = False
    detail: str = ""  # redacted stderr tail when rejected, for diagnostics


def ci_pull(
    db_path: str | Path,
    remote: str = "origin",
    branch: str = "flakeherd/history",
    repo_root: str | Path | None = None,
) -> PullResult:
    """Merge the remote history branch into the local database."""
    root = _repo_root(repo_root) if repo_root else _repo_root()
    if not remote_branch_exists(remote, branch, repo_root=root):
        return PullResult("no-history")
    _fetch(remote, branch, root)
    tmp: Path | None = None
    try:
        # A hand-made history branch without the db file is reported, not
        # fatal: the local database simply stays as-is.
        rc, blob = _git_blob("show", f"FETCH_HEAD:{HISTORY_FILE_ON_BRANCH}", cwd=root)
        if rc != 0 or not blob:
            return PullResult("empty-history")
        fd, name = tempfile.mkstemp(prefix="flakeherd-remote-", suffix=".db")
        os.close(fd)
        tmp = Path(name)
        tmp.write_bytes(blob)
        with Store(db_path) as store:
            stats = store.merge_from(tmp)
        return PullResult("merged", stats.runs_added, stats.outcomes_added)
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def _commit_tree(root: Path, tree: str, parent: str | None) -> str:
    args = [
        "-c", f"user.name={_GIT_IDENTITY[0]}",
        "-c", f"user.email={_GIT_IDENTITY[1]}",
        "commit-tree", tree,
    ]
    if parent:
        args += ["-p", parent]
    args += ["-m", f"flakeherd: update test history ({utc_now_iso()})"]
    return _git(*args, cwd=root)


def _build_tree(root: Path, db_path: str | Path) -> tuple[str, str]:
    """Store the db as a blob and build a one-file index + tree.

    Returns ``(blob_sha, tree_sha)``. Uses a throwaway GIT_INDEX_FILE so the
    caller's checkout is never touched.
    """
    blob = _git("hash-object", "-w", os.fspath(db_path), cwd=root)
    fd, index_name = tempfile.mkstemp(prefix="flakeherd-index-")
    os.close(fd)
    index_env = {"GIT_INDEX_FILE": index_name}
    try:
        _git("read-tree", "--empty", cwd=root, env_extra=index_env)
        _git(
            "update-index", "--add", "--cacheinfo",
            f"100644,{blob},{HISTORY_FILE_ON_BRANCH}",
            cwd=root,
            env_extra=index_env,
        )
        tree = _git("write-tree", cwd=root, env_extra=index_env)
    finally:
        try:
            os.unlink(index_name)
        except OSError:
            pass
    return blob, tree


def ci_push(
    db_path: str | Path,
    remote: str = "origin",
    branch: str = "flakeherd/history",
    repo_root: str | Path | None = None,
) -> PushResult:
    """Commit the local database to the history branch and push it.

    Correctness rule: the pushed database must be a *superset* of the
    branch tip. Without the merge below, a job that raced with another
    writer would fast-forward the branch with its own (stale) database and
    silently drop the other job's rows.
    """
    root = _repo_root(repo_root) if repo_root else _repo_root()
    target = f"refs/heads/{branch}"

    def _attempt(parent: str | None) -> PushResult:
        _blob, tree = _build_tree(root, db_path)
        commit = _commit_tree(root, tree, parent)
        rc, _out, err = _git_rc("push", remote, f"{commit}:{target}", cwd=root)
        if rc != 0:
            return PushResult("rejected", commit, detail=redact(err[-400:]))
        return PushResult("created" if parent is None else "updated", commit)

    if not remote_branch_exists(remote, branch, repo_root=root):
        result = _attempt(None)
        if result.status == "rejected":
            # Another job created the branch between our check and the push:
            # merge its rows in, then retry with it as the parent.
            ci_pull(db_path, remote, branch, repo_root=root)
            parent = _fetch_tip(remote, branch, root)
            result = _attempt(parent)
            result.retried = True
        return result

    parent = _fetch_tip(remote, branch, root)
    # Always merge the tip's rows into the local db before pushing, so the
    # pushed database is a superset of the branch content.
    pull = ci_pull(db_path, remote, branch, repo_root=root)
    if pull.status not in ("merged", "empty-history"):
        raise SyncError(f"unexpected pull status {pull.status!r} before push")
    result = _attempt(parent)
    if result.status == "rejected":
        # Another job won the push race: merge its rows, retry once.
        parent = _fetch_tip(remote, branch, root)
        ci_pull(db_path, remote, branch, repo_root=root)
        result = _attempt(parent)
        result.retried = True
    return result


__all__ = [
    "HISTORY_FILE_ON_BRANCH",
    "PullResult",
    "PushResult",
    "ci_pull",
    "ci_push",
    "remote_branch_exists",
]
