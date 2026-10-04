from pathlib import Path

from starlette.testclient import TestClient

from paperless_bedrock.app import create_app
from paperless_bedrock.jobs import JobQueue


def client(tmp_path: Path, token: str | None = None) -> tuple[TestClient, JobQueue]:
    queue = JobQueue(tmp_path / "q.db")
    return TestClient(create_app(queue, token)), queue


def test_webhook_queues_document(tmp_path: Path) -> None:
    c, _ = client(tmp_path)
    r = c.post("/analyze", json={"document_id": 42})
    assert r.status_code == 202 and r.json() == {"document_id": 42, "queued": True}
    assert c.post("/analyze", json={"document_id": "42"}).json()["queued"] is False
    assert c.get("/health").json() == {"status": "ok", "jobs": {"queued": 1}}


def test_bad_body(tmp_path: Path) -> None:
    c, _ = client(tmp_path)
    assert c.post("/analyze", content=b"nope").status_code == 400
    assert c.post("/analyze", json={"id": 1}).status_code == 400


def test_token_required_when_configured(tmp_path: Path) -> None:
    c, _ = client(tmp_path, token="s3cret")
    assert c.post("/analyze", json={"document_id": 1}).status_code == 401
    ok = c.post("/analyze", json={"document_id": 1}, headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 202
