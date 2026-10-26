"""Shared fixtures and tiny builders used across the test suite."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from flakeherd.config import Config
from flakeherd.model import RunInfo, Sample, TestCaseResult, TestHistory
from flakeherd.store import Store

_LETTER_TO_STATUS = {"P": "pass", "F": "fail", ".": "skip"}


def make_history(
    key: str = "test_mod.TestA::test_x",
    pattern: str = "PF",
    shas: list[str] | str | None = None,
    hints: str = "",
    name: str = "test_x",
    classname: str | None = "test_mod.TestA",
    file: str | None = "tests/test_mod.py",
    line: int | None = 1,
) -> TestHistory:
    """Build a TestHistory from a compact pattern: P=pass F=fail .=skip.

    ``shas`` is one sha for all samples or one per sample; ``hints`` marks
    samples carrying in-run flaky evidence (e.g. "01" -> sample 0 and 1).
    """
    n = len(pattern)
    if shas is None:
        shas = [f"sha{i}" for i in range(n)]
    elif isinstance(shas, str):
        shas = [shas] * n
    assert len(shas) == n, f"got {len(shas)} shas for {n} samples"
    hinted = {i for i, part in enumerate(_chunk(hints)) if part == "1"}
    samples = [
        Sample(
            status=_LETTER_TO_STATUS[letter],
            commit_sha=shas[i],
            run_uid=f"run-{i}",
            created_at=f"2026-01-0{1 + i % 9}T00:00:00+00:00",
            flaky_hint=i in hinted,
        )
        for i, letter in enumerate(pattern)
    ]
    return TestHistory(
        test_key=key,
        samples=samples,
        name=name,
        classname=classname,
        file=file,
        line=line,
    )


def _chunk(hints: str) -> list[str]:
    return [hints[i] for i in range(len(hints))]


def make_result(
    name: str = "test_x",
    classname: str | None = "test_mod.TestA",
    status: str = "pass",
    file: str | None = "tests/test_mod.py",
    line: int | None = 3,
) -> TestCaseResult:
    return TestCaseResult(name=name, classname=classname, status=status, file=file, line=line)


def junit_xml(
    cases: list[dict],
    *,
    root: str = "testsuites",
    suite_name: str = "pytest",
) -> str:
    """Build a small JUnit document from case dicts.

    Each case dict: name, classname, status (pass/fail/skip), file, line,
    time, plus optional 'extra_children' raw XML and 'flaky_child' bool.
    """
    rows = []
    for case in cases:
        attrs = [f'name="{case["name"]}"']
        if case.get("classname"):
            attrs.append(f'classname="{case["classname"]}"')
        if case.get("file"):
            attrs.append(f'file="{case["file"]}"')
        if case.get("line"):
            attrs.append(f'line="{case["line"]}"')
        if case.get("time") is not None:
            attrs.append(f'time="{case["time"]}"')
        inner = ""
        status = case.get("status", "pass")
        if status == "fail":
            msg = case.get("message", "boom")
            inner += f'<failure message="{msg}" type="AssertionError">tb</failure>'
        elif status == "skip":
            inner += "<skipped/>"
        if case.get("flaky_child"):
            inner += '<rerunFailure message="flaked once">tb</rerunFailure>'
        inner += case.get("extra_children", "")
        rows.append(f"<testcase {' '.join(attrs)}>{inner}</testcase>")
    body = f'<testsuite name="{suite_name}">' + "".join(rows) + "</testsuite>"
    if root == "testsuites":
        return f'<?xml version="1.0"?><testsuites>{body}</testsuites>'
    return f'<?xml version="1.0"?>{body}' if root == "testsuite" else body


@pytest.fixture
def config() -> Config:
    return Config()


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "history.db")
    yield s
    s.close()


@dataclass
class GitRepo:
    origin: Path  # bare repository acting as the remote
    work: Path  # a cloned working repository

    def git(self, *args: str, cwd: Path | None = None, check: bool = True):
        import subprocess

        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.work),
            capture_output=True,
            text=True,
            env={
                **__import__("os").environ,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_AUTHOR_NAME": "tester",
                "GIT_AUTHOR_EMAIL": "tester@example.com",
                "GIT_COMMITTER_NAME": "tester",
                "GIT_COMMITTER_EMAIL": "tester@example.com",
            },
        )
        if check and proc.returncode != 0:
            raise AssertionError(f"git {args} failed: {proc.stderr}")
        return proc


@pytest.fixture
def git_env(tmp_path: Path) -> GitRepo:
    """A bare 'origin' plus a cloned working repo with one commit."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    repo = GitRepo(origin, work)
    repo.git("init", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    repo.git("clone", str(origin), str(work), cwd=tmp_path, check=False)
    (work / "README.md").write_text("# demo\n", encoding="utf-8")
    repo.git("add", "README.md")
    repo.git("commit", "-m", "initial")
    repo.git("push", "-u", "origin", "main", check=False)
    return repo


def record_run(
    store: Store,
    run_uid: str,
    sha: str,
    results: list[TestCaseResult],
    *,
    created_at: str = "",
) -> None:
    store.record_run(
        RunInfo(run_uid=run_uid, commit_sha=sha, branch="main", created_at=created_at),
        results,
    )
