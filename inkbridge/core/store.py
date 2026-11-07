"""SQLite state: doc ID -> source, reMarkable UUID, status, Trilium note ID, feedback."""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BASE32 = "abcdefghijklmnopqrstuvwxyz234567"

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
  doc_id          TEXT PRIMARY KEY,   -- rm-xxxxxx, rm-xxxxxx.2 for split parts, ext-... for unknown docs
  base_id         TEXT NOT NULL,      -- rm-xxxxxx shared by all parts
  title           TEXT NOT NULL,
  source_kind     TEXT NOT NULL,      -- html | markdown | pdf | url | artifact | external
  source_ref      TEXT,
  rm_uuid         TEXT,
  rm_hash         TEXT,               -- transport-specific version of the last processed download
  status          TEXT NOT NULL,      -- pushed | pulled | external
  trilium_note_id TEXT,
  trilium_url     TEXT,
  pushed_at       REAL,
  pulled_at       REAL,
  feedback        TEXT,               -- feedback JSON
  dir             TEXT                -- working dir with original.pdf, annotated.pdf, annotated.png
);
CREATE INDEX IF NOT EXISTS docs_rm_uuid ON docs(rm_uuid);
CREATE INDEX IF NOT EXISTS docs_base_id ON docs(base_id);
"""


@dataclass
class Doc:
    doc_id: str
    base_id: str
    title: str
    source_kind: str
    source_ref: str | None = None
    rm_uuid: str | None = None
    rm_hash: str | None = None
    status: str = "pushed"
    trilium_note_id: str | None = None
    trilium_url: str | None = None
    pushed_at: float | None = None
    pulled_at: float | None = None
    feedback: dict[str, Any] | None = None
    dir: str | None = None
    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def path(self) -> Path | None:
        return Path(self.dir) if self.dir else None


_COLUMNS = [
    "doc_id", "base_id", "title", "source_kind", "source_ref", "rm_uuid", "rm_hash", "status",
    "trilium_note_id", "trilium_url", "pushed_at", "pulled_at", "feedback", "dir",
]


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def mint_id(self) -> str:
        while True:
            doc_id = "rm-" + "".join(secrets.choice(BASE32) for _ in range(6))
            if not self._conn.execute("SELECT 1 FROM docs WHERE base_id = ?", (doc_id,)).fetchone():
                return doc_id

    def upsert(self, doc: Doc) -> Doc:
        values = [getattr(doc, c) for c in _COLUMNS]
        values[_COLUMNS.index("feedback")] = json.dumps(doc.feedback) if doc.feedback is not None else None
        placeholders = ", ".join("?" for _ in _COLUMNS)
        updates = ", ".join(f"{c} = excluded.{c}" for c in _COLUMNS[1:])
        with self._conn:
            self._conn.execute(
                f"INSERT INTO docs ({', '.join(_COLUMNS)}) VALUES ({placeholders}) "
                f"ON CONFLICT(doc_id) DO UPDATE SET {updates}",
                values,
            )
        return doc

    def get(self, doc_id: str) -> Doc | None:
        row = self._conn.execute("SELECT * FROM docs WHERE doc_id = ?", (doc_id,)).fetchone()
        return _row_to_doc(row) if row else None

    def parts(self, base_id: str) -> list[Doc]:
        rows = self._conn.execute("SELECT * FROM docs WHERE base_id = ? ORDER BY doc_id", (base_id,)).fetchall()
        return [_row_to_doc(r) for r in rows]

    def by_rm_uuid(self, rm_uuid: str) -> Doc | None:
        row = self._conn.execute("SELECT * FROM docs WHERE rm_uuid = ?", (rm_uuid,)).fetchone()
        return _row_to_doc(row) if row else None

    def returned(self, since: float | None = None, limit: int = 20) -> list[Doc]:
        rows = self._conn.execute(
            "SELECT * FROM docs WHERE pulled_at IS NOT NULL AND pulled_at >= ? ORDER BY pulled_at DESC LIMIT ?",
            (since or 0, limit),
        ).fetchall()
        return [_row_to_doc(r) for r in rows]

    def older_than(self, cutoff: float) -> list[Doc]:
        rows = self._conn.execute(
            "SELECT * FROM docs WHERE dir IS NOT NULL AND COALESCE(pulled_at, pushed_at, 0) < ?", (cutoff,)
        ).fetchall()
        return [_row_to_doc(r) for r in rows]


def _row_to_doc(row: sqlite3.Row) -> Doc:
    data = dict(row)
    if data.get("feedback"):
        data["feedback"] = json.loads(data["feedback"])
    return Doc(**data)


def now() -> float:
    return time.time()
