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
    assert JobQueue(tmp_path / "q.db").counts() == {"running": 1}  # e.g. CLI: leaves it alone
    assert JobQueue(tmp_path / "q.db", recover=True).counts() == {"queued": 1}


def test_retry_then_give_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs, "RETRY_BASE_SECONDS", 0)
    q = JobQueue(tmp_path / "q.db")
    q.enqueue(1)
    failures: list[tuple[int, str]] = []

    def boom(job: object) -> None:
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

    def forbidden(job: object) -> None:
        raise PaperlessError("403")

    Worker(q, forbidden, lambda d, e: failures.append(d), max_attempts=3).run_once()
    assert q.counts() == {"failed": 1} and failures == [1]


def test_success(tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "q.db")
    q.enqueue(1)
    done: list[int] = []
    Worker(
        q, lambda job: done.append(job.document_id), lambda d, e: None, max_attempts=3
    ).run_once()
    assert done == [1] and q.counts() == {"done": 1}
    assert q.enqueue(1)  # a finished document can be queued again


def test_batch_jobs_wait_for_new_letters(tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "q.db")
    q.enqueue(10, light=True, priority=jobs.PRIORITY_BATCH)
    q.enqueue(11)
    first = q.claim()
    second = q.claim()
    assert first is not None and first.document_id == 11 and not first.light
    assert second is not None and second.document_id == 10 and second.light


def test_maintenance_runs_between_jobs(tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "q.db")
    calls: list[str] = []

    def idle() -> None:
        calls.append("maintenance")
        raise RuntimeError("must not stop the worker")

    worker = Worker(q, lambda job: calls.append("job"), lambda d, e: None, 3, idle=idle)
    q.enqueue(1)
    assert worker.run_once() and not worker.run_once()
    assert calls == ["maintenance", "job", "maintenance"]
