"""Store tests: schema, upserts, window loading, pruning, merging."""

from __future__ import annotations

import sqlite3

import pytest
from tests.conftest import make_result, record_run

from flakeherd.errors import StoreError
from flakeherd.model import RunInfo
from flakeherd.store import Store


class TestSchema:
    def test_creates_tables_and_wal(self, store):
        tables = {
            r["name"]
            for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"runs", "outcomes"} <= tables
        mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"

    def test_open_creates_parent_dirs(self, tmp_path):
        s = Store(tmp_path / "a" / "b" / "h.db")
        s.close()
        assert (tmp_path / "a" / "b" / "h.db").exists()


class TestRecording:
    def test_record_and_load_roundtrip(self, store):
        record_run(
            store, "r1", "sha1",
            [make_result(name="test_a"), make_result(name="test_b", status="fail")],
        )
        histories = store.load_history(window=30)
        assert set(histories) == {"test_mod.TestA::test_a", "test_mod.TestA::test_b"}
        hist = histories["test_mod.TestA::test_b"]
        assert hist.current_status == "fail"
        assert hist.samples[0].commit_sha == "sha1"
        assert hist.samples[0].run_uid == "r1"
        assert hist.file == "tests/test_mod.py"

    def test_rerun_same_uid_upserts(self, store):
        record_run(store, "r1", "sha1", [make_result(name="test_a", status="fail")])
        record_run(store, "r1", "sha1", [make_result(name="test_a", status="pass")])
        hist = store.load_history(30)["test_mod.TestA::test_a"]
        assert len(hist.samples) == 1
        assert hist.current_status == "pass"

    def test_invalid_status_rejected_by_schema(self, store):
        record_run(store, "r1", "s", [make_result(name="t")])
        with pytest.raises(sqlite3.IntegrityError):
            with store.conn:
                store.conn.execute(
                    "INSERT INTO outcomes (run_uid, test_key, name, classname, file, "
                    "line, status, flaky_hint, duration_sec) "
                    "VALUES (?, ?, 'n', NULL, NULL, NULL, 'weird', 0, NULL)",
                    ("r1", "k"),
                )

    def test_stats_counts(self, store):
        stats = store.record_run(
            RunInfo(run_uid="r1", commit_sha="s"),
            [make_result(name="t1"), make_result(name="t2")],
        )
        assert stats.tests_recorded == 2
        assert store.run_count() == 1
        assert store.test_count() == 2


class TestWindow:
    def test_window_trims_to_newest(self, store):
        for i in range(6):
            record_run(
                store,
                f"r{i}",
                f"sha{i}",
                [make_result(name="t", status="pass" if i % 2 else "fail")],
                created_at=f"2026-01-01T00:00:{i:02d}+00:00",
            )
        hist = store.load_history(window=3)["test_mod.TestA::t"]
        assert [s.run_uid for s in hist.samples] == ["r3", "r4", "r5"]

    def test_ordering_oldest_first(self, store):
        record_run(
            store, "late", "s", [make_result(name="t")],
            created_at="2026-05-01T00:00:00+00:00",
        )
        record_run(
            store, "early", "s", [make_result(name="t", status="fail")],
            created_at="2026-01-01T00:00:00+00:00",
        )
        hist = store.load_history(30)["test_mod.TestA::t"]
        assert hist.recent_pattern() == "FP"


class TestPrune:
    def test_prune_keeps_newest_runs(self, store):
        for i in range(5):
            record_run(
                store,
                f"r{i}",
                f"sha{i}",
                [make_result(name="t")],
                created_at=f"2026-01-01T00:00:{i:02d}+00:00",
            )
        deleted = store.prune(max_runs=2)
        assert deleted == 3
        assert store.run_count() == 2
        assert store.current_run_uid() == "r4"


class TestMerge:
    def test_merge_is_idempotent_and_additive(self, tmp_path):
        main = Store(tmp_path / "main.db")
        other = Store(tmp_path / "other.db")
        record_run(main, "r1", "sha1", [make_result(name="a")])
        record_run(main, "r2", "sha2", [make_result(name="b")])
        record_run(other, "r2", "sha2", [make_result(name="b"), make_result(name="c")])
        record_run(other, "r3", "sha3", [make_result(name="d")])
        stats = main.merge_from(tmp_path / "other.db")
        assert stats.runs_added == 1  # r3
        assert stats.outcomes_added == 2  # c and d (b already present)
        assert main.test_count() == 4
        # second merge is a no-op
        stats2 = main.merge_from(tmp_path / "other.db")
        assert stats2.runs_added == 0 and stats2.outcomes_added == 0
        main.close()
        other.close()

    def test_merge_rejects_missing_file(self, tmp_path):
        s = Store(tmp_path / "x.db")
        with pytest.raises(StoreError, match="not a file"):
            s.merge_from(tmp_path / "missing.db")
        s.close()


class TestRerecord:
    def test_rerecord_replaces_run_metadata(self, store):
        record_run(store, "r1", "old", [make_result(name="t")])
        record_run(store, "r1", "new-sha", [make_result(name="t")])
        assert store.run_for("r1").commit_sha == "new-sha"

    def test_rerecord_drops_tests_absent_from_new_report(self, store):
        record_run(store, "r1", "s", [make_result(name="a"), make_result(name="b")])
        record_run(store, "r1", "s", [make_result(name="a")])
        keys = set(store.load_history(30))
        assert "test_mod.TestA::a" in keys
        assert "test_mod.TestA::b" not in keys

    def test_merge_failure_leaves_store_usable(self, tmp_path):
        s = Store(tmp_path / "m.db")
        bad = tmp_path / "bad.db"
        bad.write_bytes(b"this is not a database" * 10)
        with pytest.raises(StoreError):
            s.merge_from(bad)
        # the store must survive a corrupt merge and keep working
        record_run(s, "r1", "s", [make_result(name="t")])
        assert s.run_count() == 1
        s.close()


class TestHelpers:
    def test_run_for_returns_runinfo(self, store):
        record_run(store, "r1", "shaX", [make_result(name="t")])
        info = store.run_for("r1")
        assert info.commit_sha == "shaX"
        assert info.branch == "main"
        assert store.run_for("nope") is None

    def test_context_manager_closes(self, tmp_path):
        with Store(tmp_path / "cm.db") as s:
            s.run_count()
        with pytest.raises(sqlite3.ProgrammingError):
            s.conn.execute("SELECT 1")


class TestSchemaMigration:
    def test_011_database_is_migrated_in_place(self, tmp_path):
        """A database written by flakeherd 0.1.1 (no retry columns) opens,
        migrates and records retry evidence without data loss."""
        import sqlite3

        from flakeherd.model import RunInfo, TestCaseResult

        db = tmp_path / "old.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            CREATE TABLE runs (
                run_uid     TEXT PRIMARY KEY,
                commit_sha  TEXT NOT NULL,
                branch      TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );
            CREATE TABLE outcomes (
                run_uid      TEXT NOT NULL REFERENCES runs(run_uid) ON DELETE CASCADE,
                test_key     TEXT NOT NULL,
                name         TEXT NOT NULL,
                classname    TEXT,
                file         TEXT,
                line         INTEGER,
                status       TEXT NOT NULL CHECK (status IN ('pass','fail','skip')),
                flaky_hint   INTEGER NOT NULL DEFAULT 0,
                duration_sec REAL,
                PRIMARY KEY (run_uid, test_key)
            );
            INSERT INTO runs VALUES ('old-run', 'sha1', 'main', '2026-01-01T00:00:00+00:00');
            INSERT INTO outcomes
                (run_uid, test_key, name, classname, file, line, status, flaky_hint)
            VALUES ('old-run', 'c::t', 't', 'c', NULL, NULL, 'pass', 0);
            """
        )
        conn.commit()
        conn.close()

        with Store(db) as store:
            # old rows survive the migration
            history = store.load_history(30)
            assert "c::t" in history
            assert history["c::t"].samples[0].retry_attempts is None
            # new writes carry retry evidence
            store.record_run(
                RunInfo(run_uid="new-run", commit_sha="sha2", branch="main"),
                [
                    TestCaseResult(
                        name="t", classname="c", status="pass", flaky_hint=True,
                        retry_attempts=2, retry_outcomes="F,P",
                    )
                ],
            )
            history = store.load_history(30)
            samples = history["c::t"].samples
            assert [s.run_uid for s in samples] == ["old-run", "new-run"]
            assert samples[1].retry_attempts == 2
            assert samples[1].retry_outcomes == "F,P"

        # migration is idempotent
        with Store(db) as store:
            assert store.run_count() == 2

    def test_new_database_has_columns_from_the_start(self, tmp_path):
        import sqlite3

        from flakeherd.model import RunInfo, TestCaseResult

        db = tmp_path / "new.db"
        with Store(db) as store:
            store.record_run(
                RunInfo(run_uid="r", commit_sha="s", branch="main"),
                [TestCaseResult(name="t", status="pass")],
            )
        conn = sqlite3.connect(db)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(outcomes)")}
        conn.close()
        assert {"retry_attempts", "retry_outcomes"} <= cols

    def test_merge_from_011_database(self, tmp_path):
        """merge_from handles a remote 0.1.1 database without retry columns."""
        import sqlite3

        from flakeherd.model import RunInfo, TestCaseResult

        old = tmp_path / "old-remote.db"
        conn = sqlite3.connect(old)
        conn.executescript(
            """
            CREATE TABLE runs (
                run_uid     TEXT PRIMARY KEY,
                commit_sha  TEXT NOT NULL,
                branch      TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );
            CREATE TABLE outcomes (
                run_uid      TEXT NOT NULL REFERENCES runs(run_uid) ON DELETE CASCADE,
                test_key     TEXT NOT NULL,
                name         TEXT NOT NULL,
                classname    TEXT,
                file         TEXT,
                line         INTEGER,
                status       TEXT NOT NULL CHECK (status IN ('pass','fail','skip')),
                flaky_hint   INTEGER NOT NULL DEFAULT 0,
                duration_sec REAL,
                PRIMARY KEY (run_uid, test_key)
            );
            INSERT INTO runs VALUES ('remote-run', 'sha9', 'main', '2026-02-01T00:00:00+00:00');
            INSERT INTO outcomes
                (run_uid, test_key, name, classname, file, line, status, flaky_hint)
            VALUES ('remote-run', 'r::x', 'x', 'r', NULL, NULL, 'fail', 0);
            """
        )
        conn.commit()
        conn.close()

        with Store(tmp_path / "local.db") as store:
            store.record_run(
                RunInfo(run_uid="local-run", commit_sha="sha1", branch="main"),
                [TestCaseResult(name="t", status="pass")],
            )
            stats = store.merge_from(old)
            assert stats.runs_added == 1
            assert stats.outcomes_added == 1
            history = store.load_history(30)
            remote = history["r::x"].samples[0]
            assert remote.status == "fail"
            assert remote.retry_attempts is None
