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
            "created": "2026-09-28",
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
        # correspondents / document_types: id -> {"name", "owner"}; documents: id -> fields
        self.objects: dict[str, dict[int, dict[str, Any]]] = {
            "correspondents": {},
            "document_types": {},
        }
        self.documents: dict[int, dict[str, Any]] = {1: self.doc}
        self.bulk_edits: list[dict[str, Any]] = []
        self.next_id = 100

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
            for key in ("correspondent", "document_type", "title", "tags", "created"):
                if key in body:
                    self.doc[key] = body[key]
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
        m = re.fullmatch(r"/api/(correspondents|document_types)/(?:(\d+)/)?", path)
        if m:
            endpoint, object_id = m.group(1), m.group(2)
            store = self.objects[endpoint]
            if method == "GET":
                field = "correspondent" if endpoint == "correspondents" else "document_type"
                results = [
                    {
                        "id": i,
                        "name": o["name"],
                        "owner": o.get("owner"),
                        "document_count": sum(
                            1 for d in self.documents.values() if d.get(field) == i
                        ),
                    }
                    for i, o in sorted(store.items(), key=lambda x: x[1]["name"])
                ]
                return httpx.Response(200, json={"results": results, "next": None})
            if method == "POST":
                self.next_id += 1
                store[self.next_id] = {"name": json.loads(request.content)["name"], "owner": 3}
                return httpx.Response(201, json={"id": self.next_id})
            if method == "DELETE":
                del store[int(object_id)]
                return httpx.Response(204)
        if path == "/api/documents/" and method == "GET":
            params = request.url.params
            docs = list(self.documents.values())
            for key, field in (
                ("correspondent__id", "correspondent"),
                ("document_type__id", "document_type"),
            ):
                if key in params:
                    docs = [d for d in docs if d.get(field) == int(params[key])]
            if "tags__id__all" in params:
                docs = [d for d in docs if int(params["tags__id__all"]) in d.get("tags", [])]
            fields = params.get("fields")
            rows = (
                [{f: d.get(f) for f in fields.split(",")} for d in docs]
                if fields
                else [{"id": d["id"]} for d in docs]
            )
            return httpx.Response(200, json={"results": rows, "next": None})
        if path == "/api/documents/bulk_edit/":
            body = json.loads(request.content)
            self.bulk_edits.append(body)
            field = "correspondent" if body["method"] == "set_correspondent" else "document_type"
            for doc_id in body["documents"]:
                self.documents[doc_id][field] = body["parameters"][field]
            return httpx.Response(200, json={"result": "OK"})
        if path == "/api/tags/":
            tag = self.tags.get(request.url.params["name__iexact"])
            return httpx.Response(200, json={"results": [{"id": tag}] if tag else []})
        return httpx.Response(404, text=f"unexpected {method} {path}")
