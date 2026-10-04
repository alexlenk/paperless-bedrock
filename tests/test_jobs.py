from pathlib import Path

import pytest

import paperless_bedrock.jobs as jobs
from paperless_bedrock.jobs import JobQueue, Worker
from paperless_bedrock.paperless import PaperlessError


def test_enqueue_deduplicates_open_jobs(tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "q.db")
    assert q.enqueue(1) and not q.enqueue(1) and q.enqueue(2)
    assert q.counts() == {"queued": 2}


def test_running_jobs_are_requeued_after_restart(tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "q.db")
    q.enqueue(1)
    assert q.claim() is not None
    assert JobQueue(tmp_path / "q.db").counts() == {"queued": 1}


def test_retry_then_give_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs, "RETRY_BASE_SECONDS", 0)
    q = JobQueue(tmp_path / "q.db")
    q.enqueue(1)
    failures: list[tuple[int, str]] = []

    def boom(doc_id: int) -> None:
        raise TimeoutError("bedrock slow")

    worker = Worker(q, boom, lambda d, e: failures.append((d, e)), max_attempts=2)
    assert worker.run_once()
    assert q.counts() == {"queued": 1} and failures == []
    assert worker.run_once()
    assert q.counts() == {"failed": 1}
    assert failures == [(1, "TimeoutError: bedrock slow")]


def test_paperless_errors_are_not_retried(tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "q.db")
    q.enqueue(1)
    failures: list[int] = []

    def forbidden(doc_id: int) -> None:
        raise PaperlessError("403")

    Worker(q, forbidden, lambda d, e: failures.append(d), max_attempts=3).run_once()
    assert q.counts() == {"failed": 1} and failures == [1]


def test_success(tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "q.db")
    q.enqueue(1)
    done: list[int] = []
    Worker(q, done.append, lambda d, e: None, max_attempts=3).run_once()
    assert done == [1] and q.counts() == {"done": 1}
    assert q.enqueue(1)  # a finished document can be queued again
