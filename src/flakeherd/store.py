"""SQLite-backed history store.

Everything FlakeHerd knows about your tests lives in a single SQLite file
(usually ``.flakeherd/history.db``). The schema is intentionally tiny - two
tables - and the file is fully mergeable: when history is synced through the
cache branch (:mod:`flakeherd.ci_sync`), remote rows are folded in with
``INSERT OR IGNORE`` so the same run recorded twice never duplicates.

Concurrency note: WAL journaling plus SQLite's own locking make concurrent
*processes* on one machine safe. Concurrent CI jobs share state through the
cache branch instead, where a last-writer may occasionally win; flakiness
detection is statistical and tolerates rare lost rows. This trade-off is
documented in ``docs/ARCHITECTURE.md``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .errors import StoreError
from .junit import _merge_into
from .model import RunInfo, Sample, TestCaseResult, TestHistory
from .util import utc_now_iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_uid     TEXT PRIMARY KEY,
    commit_sha  TEXT NOT NULL,
    branch      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outcomes (
    run_uid      TEXT NOT NULL REFERENCES runs(run_uid) ON DELETE CASCADE,
    test_key     TEXT NOT NULL,
    name         TEXT NOT NULL,
    classname    TEXT,
    file         TEXT,
    line         INTEGER,
    status       TEXT NOT NULL CHECK (status IN ('pass','fail','skip')),
    flaky_hint   INTEGER NOT NULL DEFAULT 0,
    duration_sec REAL,
    retry_attempts INTEGER,
    retry_outcomes TEXT,
    PRIMARY KEY (run_uid, test_key)
);
CREATE INDEX IF NOT EXISTS idx_outcomes_key ON outcomes (test_key);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs (created_at);
"""

# Columns added after 0.1.1; existing databases are migrated in place with
# ALTER TABLE (nullable, so every old row and every old reader stays valid).
_MIGRATION_COLUMNS = {
    "retry_attempts": "INTEGER",
    "retry_outcomes": "TEXT",
}


@dataclass
class RecordStats:
    run_uid: str
    tests_recorded: int


@dataclass
class MergeStats:
    runs_added: int
    outcomes_added: int


class Store:
    """A tiny append-first history database. One instance per process."""

    def __init__(self, path: str | Path, *, create_parent: bool = True) -> None:
        self.path = Path(path)
        if create_parent:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise StoreError(f"cannot create directory {self.path.parent}: {exc}") from exc
        try:
            self.conn = sqlite3.connect(str(self.path))
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(_SCHEMA)
            self._migrate()
        except sqlite3.Error as exc:
            raise StoreError(f"cannot open history database {self.path}: {exc}") from exc

    def _migrate(self) -> None:
        """Add columns introduced after the initial schema (0.1.1 -> 0.2.0).

        ``CREATE TABLE IF NOT EXISTS`` cannot evolve an existing table, so
        databases recorded by older versions gain the new nullable columns
        here. Old rows keep NULL retry metadata ("not reported"), and old
        FlakeHerd versions reading a new database simply never select these
        columns - both directions stay compatible.
        """
        existing = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(outcomes)")
        }
        for column, decl in _MIGRATION_COLUMNS.items():
            if column not in existing:
                self.conn.execute(
                    f"ALTER TABLE outcomes ADD COLUMN {column} {decl}"
                )
        self.conn.commit()

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- writes ------------------------------------------------------------

    def record_run(
        self,
        run: RunInfo,
        results: Iterable[TestCaseResult],
    ) -> RecordStats:
        """Insert one run and its (deduplicated) outcomes.

        Recording the same ``run_uid`` again upserts outcomes instead of
        duplicating them, which makes local re-races and CI retries safe.
        """
        merged: dict[str, TestCaseResult] = {}
        for result in results:
            _merge_into(merged, result)
        created_at = run.created_at or utc_now_iso()
        try:
            with self.conn:  # single transaction
                # Re-recording the same run_uid replaces the run's metadata
                # (a corrected --sha must not be silently ignored) and drops
                # outcomes for tests absent from the new report, so a
                # re-record is authoritative.
                self.conn.execute(
                    "INSERT INTO runs (run_uid, commit_sha, branch, created_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(run_uid) DO UPDATE SET "
                    "commit_sha=excluded.commit_sha, branch=excluded.branch, "
                    "created_at=excluded.created_at",
                    (run.run_uid, run.commit_sha, run.branch, created_at),
                )
                self.conn.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS _current_run_keys (key TEXT)"
                )
                self.conn.execute("DELETE FROM _current_run_keys")
                self.conn.executemany(
                    "INSERT INTO _current_run_keys VALUES (?)",
                    [(key,) for key in merged],
                )
                self.conn.execute(
                    "DELETE FROM outcomes WHERE run_uid = ? AND test_key NOT IN "
                    "(SELECT key FROM _current_run_keys)",
                    (run.run_uid,),
                )
                self.conn.executemany(
                    "INSERT OR REPLACE INTO outcomes "
                    "(run_uid, test_key, name, classname, file, line, "
                    "status, flaky_hint, duration_sec, retry_attempts, retry_outcomes) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            run.run_uid,
                            r.test_key,
                            r.name,
                            r.classname,
                            r.file,
                            r.line,
                            r.status,
                            int(r.flaky_hint),
                            r.duration,
                            r.retry_attempts,
                            r.retry_outcomes or None,
                        )
                        for r in merged.values()
                    ],
                )
        except sqlite3.Error as exc:
            raise StoreError(f"failed to record run {run.run_uid}: {exc}") from exc
        return RecordStats(run_uid=run.run_uid, tests_recorded=len(merged))

    def prune(self, max_runs: int) -> int:
        """Keep only the newest *max_runs* runs; returns runs deleted."""
        try:
            cur = self.conn.execute(
                "DELETE FROM runs WHERE run_uid NOT IN "
                "(SELECT run_uid FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?)",
                (max_runs,),
            )
            self.conn.commit()
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        except sqlite3.Error as exc:
            raise StoreError(f"prune failed: {exc}") from exc

    def merge_from(self, other_db: str | Path) -> MergeStats:
        """Merge rows from *other_db* into this store (idempotent)."""
        other = Path(other_db)
        if not other.is_file():
            raise StoreError(f"nothing to merge: {other} is not a file")
        try:
            self.conn.execute("ATTACH DATABASE ? AS remote_merge", (str(other),))
        except sqlite3.Error as exc:
            raise StoreError(f"cannot attach {other}: {exc}") from exc
        try:
            runs_before = self.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            outcomes_before = self.conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
            self.conn.execute(
                "INSERT OR IGNORE INTO runs "
                "SELECT run_uid, commit_sha, branch, created_at FROM remote_merge.runs"
            )
            # The remote database may predate the retry columns (recorded by
            # flakeherd <= 0.1.1): select NULLs for the missing columns so the
            # merge works in both directions.
            remote_cols = {
                row["name"]
                for row in self.conn.execute("PRAGMA remote_merge.table_info(outcomes)")
            }
            retry_attempts = "retry_attempts" if "retry_attempts" in remote_cols else "NULL"
            retry_outcomes = "retry_outcomes" if "retry_outcomes" in remote_cols else "NULL"
            self.conn.execute(
                "INSERT OR IGNORE INTO outcomes "
                "SELECT run_uid, test_key, name, classname, file, line, "
                f"status, flaky_hint, duration_sec, {retry_attempts}, {retry_outcomes} "
                "FROM remote_merge.outcomes"
            )
            self.conn.commit()
            runs_after = self.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            outcomes_after = self.conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
        except sqlite3.Error as exc:
            # Roll back before detaching: a DETACH with an open transaction
            # fails and would leave the remote db attached and the store
            # unusable for further merges.
            try:
                self.conn.rollback()
            except sqlite3.Error:
                pass
            raise StoreError(f"merge from {other} failed: {exc}") from exc
        finally:
            try:
                self.conn.execute("DETACH DATABASE remote_merge")
            except sqlite3.Error:
                pass
        return MergeStats(runs_after - runs_before, outcomes_after - outcomes_before)

    # -- reads -------------------------------------------------------------

    def load_history(self, window: int) -> dict[str, TestHistory]:
        """Load the newest *window* samples per test key, oldest first."""
        histories: dict[str, list[Sample]] = {}
        meta: dict[str, tuple] = {}
        try:
            cur = self.conn.execute(
                "SELECT o.test_key, o.name, o.classname, o.file, o.line, o.status, "
                "       o.flaky_hint, o.retry_attempts, o.retry_outcomes, "
                "       r.commit_sha, r.run_uid, r.created_at "
                "FROM outcomes o JOIN runs r ON r.run_uid = o.run_uid "
                "ORDER BY r.created_at ASC, r.rowid ASC, o.rowid ASC"
            )
        except sqlite3.Error as exc:
            raise StoreError(f"cannot load history: {exc}") from exc
        for row in cur:
            key = row["test_key"]
            sample = Sample(
                status=row["status"],
                commit_sha=row["commit_sha"],
                run_uid=row["run_uid"],
                created_at=row["created_at"],
                flaky_hint=bool(row["flaky_hint"]),
                retry_attempts=row["retry_attempts"],
                retry_outcomes=row["retry_outcomes"] or "",
            )
            bucket = histories.setdefault(key, [])
            bucket.append(sample)
            if len(bucket) > window:
                bucket.pop(0)  # deque-like sliding window, keeps memory bounded
            meta[key] = (row["name"], row["classname"], row["file"], row["line"])
        return {
            key: TestHistory(
                test_key=key,
                samples=samples,
                name=meta[key][0],
                classname=meta[key][1],
                file=meta[key][2],
                line=meta[key][3],
            )
            for key, samples in histories.items()
        }

    def current_run_uid(self) -> str | None:
        row = self.conn.execute(
            "SELECT run_uid FROM runs ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return row["run_uid"] if row else None

    def run_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    def test_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(DISTINCT test_key) FROM outcomes"
        ).fetchone()[0]

    def run_for(self, run_uid: str) -> RunInfo | None:
        row = self.conn.execute(
            "SELECT run_uid, commit_sha, branch, created_at FROM runs WHERE run_uid = ?",
            (run_uid,),
        ).fetchone()
        if row is None:
            return None
        return RunInfo(
            run_uid=row["run_uid"],
            commit_sha=row["commit_sha"],
            branch=row["branch"],
            created_at=row["created_at"],
        )
