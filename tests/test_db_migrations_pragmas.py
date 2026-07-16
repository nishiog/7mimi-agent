"""Issue #29: scheduler Deployment Pod and runner Job Pods share one SQLite
file over a PVC and may write concurrently. connect() must set a busy
timeout and WAL journal mode so concurrent writers block-and-retry instead
of immediately raising "database is locked"."""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from shichimimi_agent.db.migrations import connect, migrate


class ConnectPragmaTest(unittest.TestCase):
    def test_busy_timeout_is_5000ms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "app.sqlite"
            conn = connect(db_path)
            try:
                (value,) = conn.execute("PRAGMA busy_timeout").fetchone()
            finally:
                conn.close()
        self.assertEqual(value, 5000)

    def test_journal_mode_is_wal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "app.sqlite"
            conn = connect(db_path)
            try:
                (mode,) = conn.execute("PRAGMA journal_mode").fetchone()
            finally:
                conn.close()
        self.assertEqual(mode.lower(), "wal")

    def test_foreign_keys_still_enabled(self) -> None:
        """Regression guard: adding the two new PRAGMAs must not have
        displaced the pre-existing foreign_keys=ON call."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "app.sqlite"
            conn = connect(db_path)
            try:
                (value,) = conn.execute("PRAGMA foreign_keys").fetchone()
            finally:
                conn.close()
        self.assertEqual(value, 1)


class ConcurrentWriterBlocksInsteadOfFailingImmediatelyTest(unittest.TestCase):
    """A behavioral check, not just a PRAGMA-value check: with WAL +
    busy_timeout, a writer that starts while another connection holds an
    open write transaction blocks until the timeout/commit, rather than
    raising sqlite3.OperationalError("database is locked") immediately."""

    def test_second_writer_blocks_and_then_succeeds_after_first_commits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "app.sqlite"
            migrate(db_path)

            insert_sql = (
                "INSERT INTO sessions (id, source, role, workspace_path, status, created_at, updated_at) "
                "VALUES (?, 'cli', 'ai_it_topic_runner', '', 'running', '2026-07-16T00:00:00Z', '2026-07-16T00:00:00Z')"
            )

            holder = connect(db_path)
            holder.execute("BEGIN IMMEDIATE")
            holder.execute(insert_sql, ("s1",))

            result: dict[str, object] = {}

            def second_writer() -> None:
                conn = connect(db_path)
                try:
                    start = time.monotonic()
                    conn.execute(insert_sql, ("s2",))
                    conn.commit()
                    result["elapsed"] = time.monotonic() - start
                    result["error"] = None
                except sqlite3.OperationalError as exc:
                    result["error"] = exc
                finally:
                    conn.close()

            thread = threading.Thread(target=second_writer)
            thread.start()
            # Give the second writer time to actually hit the lock and
            # start waiting before the first connection releases it.
            time.sleep(0.2)
            holder.commit()
            holder.close()
            thread.join(timeout=10)

        self.assertIsNone(result.get("error"), f"second writer should not fail immediately: {result.get('error')}")
        self.assertIn("elapsed", result)


if __name__ == "__main__":
    unittest.main()
