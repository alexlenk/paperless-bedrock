"""In-memory paperless API (only the endpoints we use), served via httpx.MockTransport."""

import hashlib
import json
import re
from typing import Any

import httpx


class FakePaperless:
    def __init__(self, pdf: bytes, *, has_archive: bool = True) -> None:
        self.pdf = pdf
        self.doc: dict[str, Any] = {
            "id": 1,
            "title": "scan_0001",
            "mime_type": "application/pdf",
            "archived_file_name": "1.pdf" if has_archive else None,
            "page_count": 1,
            "added": "2026-09-28T15:35:00+00:00",
            "tags": [5],
            "custom_fields": [{"field": 99, "value": "keep me"}],
            "root_document": None,
            "versions": [{"id": 1, "is_root": True, "checksum": hashlib.sha256(pdf).hexdigest()}],
        }
        self.notes: list[str] = []
        self.patches: list[dict[str, Any]] = []
        self.versions: list[tuple[str, bytes, str]] = []
        self.downloads: list[dict[str, str]] = []
        self.custom_fields = {
            "Payment needed": (11, "boolean"),
            "Amount": (12, "monetary"),
            "Due date": (13, "date"),
            "Payee IBAN": (14, "string"),
        }
        self.tags = {"analysis-failed": 7}
        self.fail_notes_with: int | None = None

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        assert request.headers["authorization"] == "Token secret"
        assert "version=10" in request.headers["accept"]
        if path == "/api/documents/1/" and method == "GET":
            return httpx.Response(200, json=self.doc)
        if path == "/api/documents/1/" and method == "PATCH":
            body = json.loads(request.content)
            self.patches.append(body)
            return httpx.Response(200, json=self.doc)
        if path == "/api/documents/1/download/":
            self.downloads.append(dict(request.url.params))
            return httpx.Response(200, content=self.pdf)
        if path == "/api/documents/1/notes/":
            if self.fail_notes_with:
                return httpx.Response(self.fail_notes_with, text="nope")
            if method == "POST":
                self.notes.append(json.loads(request.content)["note"])
            return httpx.Response(
                200, json=[{"id": i, "note": n} for i, n in enumerate(self.notes)]
            )
        if path == "/api/documents/1/update_version/":
            body = request.content
            label = re.search(rb'name="version_label"\r\n\r\n(.*?)\r\n', body)
            assert label is not None
            filename = re.search(rb'filename="([^"]+)"', body)
            assert filename is not None
            pdf = body[body.index(b"%PDF") : body.rindex(b"%%EOF") + 5]
            self.versions.append((filename.group(1).decode(), pdf, label.group(1).decode()))
            return httpx.Response(200, json="task-123")
        if path == "/api/custom_fields/":
            found = self.custom_fields.get(request.url.params["name__iexact"])
            results = [{"id": found[0], "data_type": found[1]}] if found else []
            return httpx.Response(200, json={"results": results})
        if path == "/api/tags/":
            tag = self.tags.get(request.url.params["name__iexact"])
            return httpx.Response(200, json={"results": [{"id": tag}] if tag else []})
        return httpx.Response(404, text=f"unexpected {method} {path}")
