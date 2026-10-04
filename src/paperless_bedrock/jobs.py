"""Persistent job queue (SQLite) and a single worker thread."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from paperless_bedrock.paperless import PaperlessError

log = logging.getLogger(__name__)

RETRY_BASE_SECONDS = 30.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    not_before REAL NOT NULL,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_job_per_document
    ON jobs (document_id) WHERE status IN ('queued', 'running');
"""


@dataclass(frozen=True)
class Job:
    id: int
    document_id: int
    attempts: int
    light: bool = False


PRIORITY_NEW = 0  # webhook: new letters first
PRIORITY_BATCH = 1  # bulk imports run when nothing new is waiting


class JobQueue:
    def __init__(self, path: Path, *, recover: bool = False) -> None:
        """`recover`: requeue jobs left 'running' by a stopped server (only the server sets it)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA busy_timeout = 10000")
            self._db.executescript(_SCHEMA)
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(jobs)")}
            if "light" not in columns:
                self._db.execute("ALTER TABLE jobs ADD COLUMN light INTEGER NOT NULL DEFAULT 0")
            if "priority" not in columns:
                self._db.execute("ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
            if recover:
                self._db.execute("UPDATE jobs SET status = 'queued' WHERE status = 'running'")

    def enqueue(
        self, document_id: int, *, light: bool = False, priority: int = PRIORITY_NEW
    ) -> bool:
        """Queue a document; returns False if an open job for it already exists."""
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO jobs"
                " (document_id, status, not_before, created_at, updated_at, light, priority)"
                " VALUES (?, 'queued', ?, ?, ?, ?, ?)",
                (document_id, now, now, now, int(light), priority),
            )
            return cur.rowcount == 1

    def claim(self) -> Job | None:
        now = time.time()
        with self._lock:
            row = self._db.execute(
                "SELECT id, document_id, attempts, light FROM jobs"
                " WHERE status = 'queued' AND not_before <= ?"
                " ORDER BY priority, not_before, id LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            self._db.execute(
                "UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = ?"
                " WHERE id = ?",
                (now, row[0]),
            )
            return Job(id=row[0], document_id=row[1], attempts=row[2] + 1, light=bool(row[3]))

    def finish(self, job: Job) -> None:
        self._set(job, "done", None, time.time())

    def retry(self, job: Job, error: str) -> None:
        delay = RETRY_BASE_SECONDS * 2 ** (job.attempts - 1)
        self._set(job, "queued", error, time.time() + delay)

    def fail(self, job: Job, error: str) -> None:
        self._set(job, "failed", error, time.time())

    def _set(self, job: Job, status: str, error: str | None, not_before: float) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE jobs SET status = ?, last_error = ?, not_before = ?, updated_at = ?"
                " WHERE id = ?",
                (status, error, not_before, time.time(), job.id),
            )

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall()
        return {status: count for status, count in rows}


class Worker:
    """Processes jobs one at a time. Paperless 4xx errors are permanent; everything else retries."""

    def __init__(
        self,
        queue: JobQueue,
        process: Callable[[Job], object],
        on_failure: Callable[[int, str], None],
        max_attempts: int,
        poll_seconds: float = 2.0,
        idle: Callable[[], object] | None = None,
    ) -> None:
        self.queue = queue
        self.process = process
        self.idle = idle
        self.on_failure = on_failure
        self.max_attempts = max_attempts
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="analysis-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=30)

    def run_once(self) -> bool:
        if self.idle is not None:
            # e.g. the nightly consolidation: checked between jobs, never runs in parallel to one
            try:
                self.idle()
            except Exception:
                log.exception("maintenance task failed")
        job = self.queue.claim()
        if job is None:
            return False
        try:
            self.process(job)
        except PaperlessError as e:
            self._give_up(job, str(e))
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            if job.attempts >= self.max_attempts:
                self._give_up(job, error)
            else:
                log.warning(
                    "document %s: attempt %d failed: %s", job.document_id, job.attempts, error
                )
                self.queue.retry(job, error)
        else:
            self.queue.finish(job)
        return True

    def _give_up(self, job: Job, error: str) -> None:
        self.queue.fail(job, error)
        self.on_failure(job.document_id, error)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                busy = self.run_once()
            except Exception:  # never let the worker die
                log.exception("worker error")
                busy = False
            if not busy:
                self._stop.wait(self.poll_seconds)
