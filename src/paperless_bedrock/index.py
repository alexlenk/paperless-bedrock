"""Knowledge index: a rebuildable lookup table derived from paperless (SQLite).

paperless stays the source of truth. The index only maps normalised reference numbers to
documents and correspondents, keeps aliases (old spellings of merged objects), the objects this
tool created, and the merge log needed for undo.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from paperless_bedrock.identifiers import IDENTITY_KINDS, Reference

_SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS documents (
    document_id INTEGER PRIMARY KEY,
    correspondent_id INTEGER,
    document_type_id INTEGER,
    sender_name TEXT,
    sender_address TEXT,
    subject TEXT,
    document_date TEXT,
    summary_json TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS refs (
    document_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (document_id, kind, value)
);
CREATE INDEX IF NOT EXISTS refs_value ON refs (value);
CREATE TABLE IF NOT EXISTS aliases (
    kind TEXT NOT NULL,           -- correspondent | document_type
    alias_norm TEXT NOT NULL,
    alias TEXT NOT NULL,
    object_id INTEGER NOT NULL,
    PRIMARY KEY (kind, alias_norm)
);
CREATE TABLE IF NOT EXISTS created (
    kind TEXT NOT NULL,
    object_id INTEGER NOT NULL,
    PRIMARY KEY (kind, object_id)
);
CREATE TABLE IF NOT EXISTS merges (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    from_id INTEGER NOT NULL,
    from_name TEXT NOT NULL,
    into_id INTEGER NOT NULL,
    into_name TEXT NOT NULL,
    document_ids TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at REAL NOT NULL,
    undone_at REAL
);
CREATE TABLE IF NOT EXISTS blocked_merges (
    kind TEXT NOT NULL,
    name_a TEXT NOT NULL,
    name_b TEXT NOT NULL,
    PRIMARY KEY (kind, name_a, name_b)
);
"""


def norm_name(name: str) -> str:
    return " ".join(name.casefold().split())


@dataclass(frozen=True)
class Merge:
    id: int
    kind: str
    from_id: int
    from_name: str
    into_id: int
    into_name: str
    document_ids: list[int]
    reason: str
    created_at: float
    undone_at: float | None


class KnowledgeIndex:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA busy_timeout = 10000")
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)

    def _exec(self, sql: str, params: Iterable[object] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, tuple(params))

    # --- documents and references -----------------------------------------------------

    def record_document(
        self,
        document_id: int,
        *,
        correspondent_id: int | None,
        document_type_id: int | None,
        sender_name: str,
        sender_address: str | None,
        subject: str,
        document_date: str | None,
        summary: dict[str, object],
        refs: list[Reference],
    ) -> None:
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._db.execute(
                    "INSERT OR REPLACE INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        document_id,
                        correspondent_id,
                        document_type_id,
                        sender_name,
                        sender_address,
                        subject,
                        document_date,
                        json.dumps(summary, ensure_ascii=False),
                        time.time(),
                    ),
                )
                self._db.execute("DELETE FROM refs WHERE document_id = ?", (document_id,))
                self._db.executemany(
                    "INSERT OR IGNORE INTO refs VALUES (?, ?, ?)",
                    [(document_id, r.kind, r.value) for r in refs],
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def related_documents(
        self, refs: list[Reference], exclude: int, limit: int = 3
    ) -> list[dict[str, object]]:
        """Summaries of the most recent other documents sharing a reference number."""
        values = sorted({r.value for r in refs})
        if not values:
            return []
        marks = ",".join("?" * len(values))
        rows = self._exec(
            f"SELECT d.summary_json, GROUP_CONCAT(DISTINCT r.kind || ':' || r.value)"
            f" FROM refs r JOIN documents d ON d.document_id = r.document_id"
            f" WHERE r.value IN ({marks}) AND r.document_id != ?"
            f" GROUP BY d.document_id ORDER BY d.document_date DESC, d.document_id DESC LIMIT ?",
            [*values, exclude, limit],
        ).fetchall()
        result = []
        for summary_json, shared in rows:
            summary = json.loads(summary_json)
            summary["shared_references"] = shared.split(",")
            result.append(summary)
        return result

    def correspondents_for_identity(self, refs: list[Reference]) -> set[int]:
        values = sorted({r.value for r in refs if r.kind in IDENTITY_KINDS})
        if not values:
            return set()
        marks = ",".join("?" * len(values))
        rows = self._exec(
            f"SELECT DISTINCT d.correspondent_id FROM refs r"
            f" JOIN documents d ON d.document_id = r.document_id"
            f" WHERE r.value IN ({marks})"
            f" AND r.kind IN ('vat_id','creditor_id','commercial_register')"
            f" AND d.correspondent_id IS NOT NULL",
            values,
        ).fetchall()
        return {row[0] for row in rows}

    def identity_groups(self) -> list[tuple[str, set[int]]]:
        """Identity reference values that point to more than one correspondent."""
        rows = self._exec(
            "SELECT r.value, GROUP_CONCAT(DISTINCT d.correspondent_id) FROM refs r"
            " JOIN documents d ON d.document_id = r.document_id"
            " WHERE r.kind IN ('vat_id','creditor_id','commercial_register')"
            " AND d.correspondent_id IS NOT NULL GROUP BY r.value"
            " HAVING COUNT(DISTINCT d.correspondent_id) > 1"
        ).fetchall()
        return [(value, {int(x) for x in ids.split(",")}) for value, ids in rows]

    def profile(self, field: str, object_id: int, limit: int = 5) -> dict[str, object]:
        """Names, addresses, identity references and subjects seen for a correspondent/type."""
        column = "correspondent_id" if field == "correspondent" else "document_type_id"
        docs = self._exec(
            f"SELECT document_id, sender_name, sender_address, subject FROM documents"
            f" WHERE {column} = ? ORDER BY document_date DESC LIMIT ?",
            (object_id, limit),
        ).fetchall()
        ids = [d[0] for d in docs]
        identity: list[str] = []
        if ids and field == "correspondent":
            marks = ",".join("?" * len(ids))
            identity = [
                f"{k}:{v}"
                for k, v in self._exec(
                    f"SELECT DISTINCT kind, value FROM refs WHERE document_id IN ({marks})"
                    f" AND kind IN ('vat_id','creditor_id','commercial_register')",
                    ids,
                ).fetchall()
            ]
        return {
            "sender_names": sorted({d[1] for d in docs if d[1]}),
            "addresses": sorted({d[2] for d in docs if d[2]})[:3],
            "identity_references": identity,
            "recent_subjects": [d[3] for d in docs if d[3]],
        }

    def move_documents(self, field: str, from_id: int, into_id: int) -> None:
        column = "correspondent_id" if field == "correspondent" else "document_type_id"
        self._exec(f"UPDATE documents SET {column} = ? WHERE {column} = ?", (into_id, from_id))

    def set_document_field(self, field: str, document_ids: list[int], object_id: int) -> None:
        column = "correspondent_id" if field == "correspondent" else "document_type_id"
        marks = ",".join("?" * len(document_ids))
        if document_ids:
            self._exec(
                f"UPDATE documents SET {column} = ? WHERE document_id IN ({marks})",
                [object_id, *document_ids],
            )

    def document_count(self) -> int:
        return int(self._exec("SELECT COUNT(*) FROM documents").fetchone()[0])

    # --- aliases, created objects, blocklist --------------------------------------------

    def add_alias(self, kind: str, alias: str, object_id: int) -> None:
        self._exec(
            "INSERT OR REPLACE INTO aliases VALUES (?, ?, ?, ?)",
            (kind, norm_name(alias), alias, object_id),
        )

    def remove_alias(self, kind: str, alias: str) -> None:
        self._exec(
            "DELETE FROM aliases WHERE kind = ? AND alias_norm = ?", (kind, norm_name(alias))
        )

    def aliases(self, kind: str) -> dict[int, list[str]]:
        result: dict[int, list[str]] = {}
        for alias, object_id in self._exec(
            "SELECT alias, object_id FROM aliases WHERE kind = ? ORDER BY alias", (kind,)
        ):
            result.setdefault(object_id, []).append(alias)
        return result

    def alias_target(self, kind: str, name: str) -> int | None:
        row = self._exec(
            "SELECT object_id FROM aliases WHERE kind = ? AND alias_norm = ?",
            (kind, norm_name(name)),
        ).fetchone()
        return int(row[0]) if row else None

    def retarget_aliases(self, kind: str, from_id: int, into_id: int) -> None:
        self._exec(
            "UPDATE aliases SET object_id = ? WHERE kind = ? AND object_id = ?",
            (into_id, kind, from_id),
        )

    def mark_created(self, kind: str, object_id: int) -> None:
        self._exec("INSERT OR IGNORE INTO created VALUES (?, ?)", (kind, object_id))

    def created_by_us(self, kind: str) -> set[int]:
        return {r[0] for r in self._exec("SELECT object_id FROM created WHERE kind = ?", (kind,))}

    def block(self, kind: str, name_a: str, name_b: str) -> None:
        a, b = sorted((norm_name(name_a), norm_name(name_b)))
        self._exec("INSERT OR IGNORE INTO blocked_merges VALUES (?, ?, ?)", (kind, a, b))

    def is_blocked(self, kind: str, name_a: str, name_b: str) -> bool:
        a, b = sorted((norm_name(name_a), norm_name(name_b)))
        return (
            self._exec(
                "SELECT 1 FROM blocked_merges WHERE kind = ? AND name_a = ? AND name_b = ?",
                (kind, a, b),
            ).fetchone()
            is not None
        )

    # --- merge log ---------------------------------------------------------------------

    def log_merge(
        self,
        kind: str,
        from_id: int,
        from_name: str,
        into_id: int,
        into_name: str,
        document_ids: list[int],
        reason: str,
    ) -> int:
        cur = self._exec(
            "INSERT INTO merges (kind, from_id, from_name, into_id, into_name, document_ids,"
            " reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kind,
                from_id,
                from_name,
                into_id,
                into_name,
                json.dumps(document_ids),
                reason,
                time.time(),
            ),
        )
        return int(cur.lastrowid or 0)

    def merges(self, limit: int = 50) -> list[Merge]:
        rows = self._exec(
            "SELECT id, kind, from_id, from_name, into_id, into_name, document_ids, reason,"
            " created_at, undone_at FROM merges ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            Merge(
                id=r[0],
                kind=r[1],
                from_id=r[2],
                from_name=r[3],
                into_id=r[4],
                into_name=r[5],
                document_ids=json.loads(r[6]),
                reason=r[7],
                created_at=r[8],
                undone_at=r[9],
            )
            for r in rows
        ]

    def get_merge(self, merge_id: int) -> Merge | None:
        return next((m for m in self.merges(limit=1_000_000) if m.id == merge_id), None)

    def mark_undone(self, merge_id: int) -> None:
        self._exec("UPDATE merges SET undone_at = ? WHERE id = ?", (time.time(), merge_id))

    def clear_documents(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM refs")
            self._db.execute("DELETE FROM documents")
