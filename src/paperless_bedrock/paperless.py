"""Minimal paperless-ngx REST client (API version 10, paperless-ngx 3.x)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import httpx

API_VERSION = "10"


class PaperlessError(Exception):
    """Permanent error talking to paperless (bad request, missing permission, not found)."""


@dataclass(frozen=True)
class DocumentInfo:
    id: int
    title: str
    mime_type: str
    has_archive: bool
    checksum: str
    page_count: int | None
    added: dt.datetime | None
    tags: list[int]
    custom_fields: list[dict[str, Any]]


class PaperlessClient:
    def __init__(self, base_url: str, token: str, *, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Token {token}",
                "Accept": f"application/json; version={API_VERSION}",
            },
            timeout=httpx.Timeout(30.0, read=120.0),
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        response = self._http.request(method, url, **kwargs)
        if response.status_code >= 500:
            response.raise_for_status()  # transient, retried by the caller
        if response.status_code >= 400:
            raise PaperlessError(
                f"{method} {url}: HTTP {response.status_code} {response.text[:300]}"
            )
        return response

    # --- documents -----------------------------------------------------------------------

    def get_document(self, document_id: int) -> DocumentInfo:
        """Return the root document, resolving a version id to its root."""
        data = self._request("GET", f"/api/documents/{document_id}/").json()
        if data.get("root_document"):
            data = self._request("GET", f"/api/documents/{data['root_document']}/").json()
        root = next((v for v in data.get("versions") or [] if v.get("is_root")), None)
        if root is None or not root.get("checksum"):
            raise PaperlessError(f"document {data['id']}: no root version checksum in API response")
        added = data.get("added")
        return DocumentInfo(
            id=data["id"],
            title=data["title"],
            mime_type=data.get("mime_type") or "",
            has_archive=bool(data.get("archived_file_name")),
            checksum=root["checksum"],
            page_count=data.get("page_count"),
            added=dt.datetime.fromisoformat(added) if added else None,
            tags=list(data.get("tags") or []),
            custom_fields=list(data.get("custom_fields") or []),
        )

    def download(self, document_id: int, *, original: bool) -> bytes:
        """Download the root version's file (archive or original), never a later version."""
        params = {"version": str(document_id)}
        if original:
            params["original"] = "true"
        return self._request(
            "GET", f"/api/documents/{document_id}/download/", params=params
        ).content

    def update_document(self, document_id: int, fields: dict[str, Any]) -> None:
        self._request("PATCH", f"/api/documents/{document_id}/", json=fields)

    def upload_version(self, document_id: int, filename: str, pdf: bytes, label: str) -> str:
        response = self._request(
            "POST",
            f"/api/documents/{document_id}/update_version/",
            files={"document": (filename, pdf, "application/pdf")},
            data={"version_label": label},
        )
        return str(response.json())

    # --- notes ---------------------------------------------------------------------------

    def list_notes(self, document_id: int) -> list[str]:
        notes = self._request("GET", f"/api/documents/{document_id}/notes/").json()
        return [n["note"] for n in notes]

    def add_note(self, document_id: int, text: str) -> None:
        self._request("POST", f"/api/documents/{document_id}/notes/", json={"note": text})

    # --- lookups -------------------------------------------------------------------------

    def find_id(self, endpoint: str, name: str) -> int | None:
        results = self._request(
            "GET", f"/api/{endpoint}/", params={"name__iexact": name, "page_size": 2}
        ).json()["results"]
        return int(results[0]["id"]) if results else None

    def custom_field_types(self, names: list[str]) -> dict[str, tuple[int, str]]:
        """Map custom field name -> (id, data_type) for the names that exist."""
        found: dict[str, tuple[int, str]] = {}
        for name in names:
            results = self._request(
                "GET", "/api/custom_fields/", params={"name__iexact": name, "page_size": 2}
            ).json()["results"]
            if results:
                found[name] = (int(results[0]["id"]), str(results[0]["data_type"]))
        return found
