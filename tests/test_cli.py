from pathlib import Path

import pytest
from conftest import make_pdf
from fake_paperless import FakePaperless

import paperless_bedrock.paperless as paperless_module
from paperless_bedrock.cli import main
from paperless_bedrock.jobs import JobQueue


def test_enqueue_queues_tagged_documents_as_light_batch_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakePaperless(make_pdf([["x"]]))
    fake.tags["fileee"] = 40
    fake.documents = {
        1: {"id": 1, "tags": [40]},
        2: {"id": 2, "tags": [40]},
        3: {"id": 3, "tags": []},
    }
    original = paperless_module.PaperlessClient

    def client(base_url: str, token: str) -> paperless_module.PaperlessClient:
        return original(base_url, token, transport=fake.transport())

    monkeypatch.setattr(paperless_module, "PaperlessClient", client)
    for key, value in {
        "PAPERLESS_URL": "http://paperless",
        "PAPERLESS_TOKEN": "secret",
        "DATA_DIR": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)

    assert main(["enqueue", "--tag", "fileee", "--light"]) == 0
    assert "2 documents matched, 2 queued" in capsys.readouterr().out
    queue = JobQueue(tmp_path / "jobs.sqlite3")
    jobs = [queue.claim(), queue.claim(), queue.claim()]
    assert [(j.document_id, j.light) for j in jobs if j] == [(1, True), (2, True)]
