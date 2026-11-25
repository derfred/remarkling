"""Document state: doc ID -> source, reMarkable UUID, status, Trilium note ID, feedback; plus a
key-value cache the transports use for immutable data (see `Store.cache_get_many`).

`Store` is the interface the pipeline uses. Two backends implement it with the same SQL:
`SQLiteStore` (a file under DATA_DIR; the default, and what tests use) and `PostgresStore`
(selected by a postgres:// DATABASE_URL). `open_store()` picks one from the settings.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Sequence

if TYPE_CHECKING:
    from ..config import Settings

BASE32 = "abcdefghijklmnopqrstuvwxyz234567"

# {float} is REAL on SQLite (8 bytes) and DOUBLE PRECISION on Postgres (whose REAL is 4 bytes,
# too coarse for epoch timestamps).
SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
  doc_id          TEXT PRIMARY KEY,   -- rm-xxxxxx, rm-xxxxxx.2 for split parts, ext-... for unknown docs
  base_id         TEXT NOT NULL,      -- rm-xxxxxx shared by all parts
  title           TEXT NOT NULL,
  source_kind     TEXT NOT NULL,      -- html | markdown | pdf | url | artifact | external
  source_ref      TEXT,
  rm_uuid         TEXT,
  rm_hash         TEXT,               -- transport-specific version of the last processed download
  status          TEXT NOT NULL,      -- pushed | pulled | failed | external
  trilium_note_id TEXT,
  trilium_url     TEXT,
  pushed_at       {float},
  pulled_at       {float},
  feedback        TEXT,               -- feedback JSON
  dir             TEXT                -- working dir with original.pdf, annotated.pdf, annotated.png
);
CREATE INDEX IF NOT EXISTS docs_rm_uuid ON docs(rm_uuid);
CREATE INDEX IF NOT EXISTS docs_base_id ON docs(base_id);
CREATE TABLE IF NOT EXISTS cache (
  key        TEXT PRIMARY KEY,         -- "<namespace>:<id>", e.g. cloud.doc:<doc index hash>
  value      TEXT NOT NULL,            -- JSON
  created_at {float} NOT NULL
);
"""

# Columns added after the first release. `migrations()` adds any that an existing table lacks, so
# they are not in SCHEMA. A pull failure is kept on the doc until a later pull of it succeeds;
# interpretation (handwriting -> marks) runs after the pull and keeps its own status.
ADDED_COLUMNS = {
    "error": "TEXT",                           # last pull failure, "<ExceptionType>: <message>"
    "error_stage": "TEXT",                     # download | render | save
    "error_version": "TEXT",                   # the rm_hash (remote version) that failed
    "attempts": "INTEGER NOT NULL DEFAULT 0",  # consecutive failures of that version
    "failed_at": "{float}",
    "interpret_status": "TEXT",                # pending | done | failed | off; NULL before this column
    "interpret_error": "TEXT",
    "interpret_attempts": "INTEGER NOT NULL DEFAULT 0",
    "interpret_failed_at": "{float}",
}

# Rows per IN (...) list; far below SQLite's and Postgres's parameter limits.
_CHUNK = 500


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
    error: str | None = None
    error_stage: str | None = None
    error_version: str | None = None
    attempts: int = 0
    failed_at: float | None = None
    interpret_status: str | None = None
    interpret_error: str | None = None
    interpret_attempts: int = 0
    interpret_failed_at: float | None = None
    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def path(self) -> Path | None:
        return Path(self.dir) if self.dir else None


_COLUMNS = [
    "doc_id", "base_id", "title", "source_kind", "source_ref", "rm_uuid", "rm_hash", "status",
    "trilium_note_id", "trilium_url", "pushed_at", "pulled_at", "feedback", "dir", *ADDED_COLUMNS,
]


class Store(ABC):
    """Persistence for `Doc`s. Queries are written once, with `?` placeholders; a backend
    supplies `_fetch` and `_execute` (and translates placeholders if its driver needs to)."""

    @abstractmethod
    def _fetch(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]: ...

    @abstractmethod
    def _execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        """Run one write statement in its own transaction."""

    @abstractmethod
    def _execute_many(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        """Run one write statement per row, all in one transaction."""

    @abstractmethod
    def close(self) -> None: ...

    def mint_id(self) -> str:
        while True:
            doc_id = "rm-" + "".join(secrets.choice(BASE32) for _ in range(6))
            if not self._fetch("SELECT 1 FROM docs WHERE base_id = ?", (doc_id,)):
                return doc_id

    def upsert(self, doc: Doc) -> Doc:
        values = [getattr(doc, c) for c in _COLUMNS]
        values[_COLUMNS.index("feedback")] = json.dumps(doc.feedback) if doc.feedback is not None else None
        placeholders = ", ".join("?" for _ in _COLUMNS)
        updates = ", ".join(f"{c} = excluded.{c}" for c in _COLUMNS[1:])
        self._execute(
            f"INSERT INTO docs ({', '.join(_COLUMNS)}) VALUES ({placeholders}) "
            f"ON CONFLICT(doc_id) DO UPDATE SET {updates}",
            values,
        )
        return doc

    def get(self, doc_id: str) -> Doc | None:
        return self._one("SELECT * FROM docs WHERE doc_id = ?", (doc_id,))

    def parts(self, base_id: str) -> list[Doc]:
        return self._many("SELECT * FROM docs WHERE base_id = ? ORDER BY doc_id", (base_id,))

    def by_rm_uuid(self, rm_uuid: str) -> Doc | None:
        return self._one("SELECT * FROM docs WHERE rm_uuid = ?", (rm_uuid,))

    def delete(self, doc_id: str) -> None:
        self._execute("DELETE FROM docs WHERE doc_id = ?", (doc_id,))

    def returned(self, since: float | None = None, limit: int = 20) -> list[Doc]:
        """Docs pulled, or failed to pull, since `since`; the most recent event first."""
        return self._many(
            "SELECT * FROM docs WHERE (pulled_at IS NOT NULL AND pulled_at >= ?) "
            "OR (failed_at IS NOT NULL AND failed_at >= ?) "
            "ORDER BY COALESCE(failed_at, pulled_at) DESC LIMIT ?",
            (since or 0, since or 0, limit),
        )

    def interpret_due(self) -> list[Doc]:
        """Pulled docs whose interpretation has not run or failed, and whose files are still there.

        The caller applies the retry backoff to the failed ones."""
        return self._many(
            "SELECT * FROM docs WHERE dir IS NOT NULL AND interpret_status IN ('pending', 'failed') "
            "ORDER BY pulled_at",
            (),
        )

    def older_than(self, cutoff: float) -> list[Doc]:
        return self._many(
            "SELECT * FROM docs WHERE dir IS NOT NULL AND COALESCE(pulled_at, pushed_at, 0) < ?", (cutoff,)
        )

    # --- cache ------------------------------------------------------------------------------
    # For values that never change once written (content-addressed blobs), so there is no
    # invalidation: entries are only added, and dropped by `cache_retain` once unused.

    def cache_get_many(self, keys: Sequence[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        keys = list(dict.fromkeys(keys))
        for i in range(0, len(keys), _CHUNK):
            chunk = keys[i:i + _CHUNK]
            marks = ", ".join("?" for _ in chunk)
            for row in self._fetch(f"SELECT key, value FROM cache WHERE key IN ({marks})", chunk):
                out[row["key"]] = json.loads(row["value"])
        return out

    def cache_put_many(self, items: dict[str, Any]) -> None:
        if items:
            self._execute_many(
                "INSERT INTO cache (key, value, created_at) VALUES (?, ?, ?) ON CONFLICT (key) DO NOTHING",
                [(k, json.dumps(v), now()) for k, v in items.items()],
            )

    def cache_retain(self, prefix: str, keep: Iterable[str]) -> int:
        """Delete the entries under `prefix` that are not in `keep`; return how many."""
        keep = set(keep)
        stale = [r["key"] for r in self._fetch("SELECT key FROM cache WHERE key LIKE ?", (prefix + "%",))
                 if r["key"] not in keep]
        for i in range(0, len(stale), _CHUNK):
            chunk = stale[i:i + _CHUNK]
            self._execute(f"DELETE FROM cache WHERE key IN ({', '.join('?' for _ in chunk)})", chunk)
        return len(stale)

    def cache_rows(self, prefix: str = "") -> list[dict[str, Any]]:
        """Every entry under `prefix` as {key, value (raw JSON text), created_at}, for inspection."""
        return self._fetch("SELECT key, value, created_at FROM cache WHERE key LIKE ? ORDER BY key", (prefix + "%",))

    def with_rm_uuid(self) -> list[Doc]:
        return self._many("SELECT * FROM docs WHERE rm_uuid IS NOT NULL ORDER BY doc_id", ())

    def _one(self, sql: str, params: Sequence[Any]) -> Doc | None:
        rows = self._fetch(sql, params)
        return _row_to_doc(rows[0]) if rows else None

    def _many(self, sql: str, params: Sequence[Any]) -> list[Doc]:
        return [_row_to_doc(r) for r in self._fetch(sql, params)]


class SQLiteStore(Store):
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA.format(float="REAL"))
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(docs)").fetchall()}
        with self._conn:
            for stmt in migrations(have, "REAL"):
                self._conn.execute(stmt)

    def _fetch(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self._conn:
            self._conn.execute(sql, params)

    def _execute_many(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        with self._conn:
            self._conn.executemany(sql, rows)

    def close(self) -> None:
        self._conn.close()


class PostgresStore(Store):
    """One connection per process (server, poller), reconnected if Postgres drops it."""

    def __init__(self, url: str):
        import psycopg
        from psycopg.rows import dict_row

        self._psycopg = psycopg
        self._url = url
        self._row_factory = dict_row
        self._lock = threading.Lock()
        self._conn = self._connect()
        with self._lock, self._conn.transaction():
            # Serialise schema creation between containers starting at the same time.
            self._conn.execute("SELECT pg_advisory_xact_lock(hashtext('inkbridge.schema'))")
            for stmt in _statements(SCHEMA.format(float="DOUBLE PRECISION")):
                self._conn.execute(stmt)
            have = {r["column_name"] for r in self._conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'docs' "
                "AND table_schema = current_schema()").fetchall()}
            for stmt in migrations(have, "DOUBLE PRECISION"):
                self._conn.execute(stmt)

    def _connect(self):
        return self._psycopg.connect(self._url, autocommit=True, row_factory=self._row_factory)

    def _run(self, op):
        with self._lock:
            if self._conn.closed:
                self._conn = self._connect()
            try:
                return op(self._conn)
            except self._psycopg.OperationalError:
                # Stale connection (Postgres restarted, idle timeout): retry once on a fresh one.
                # Each op is one autocommit statement or one transaction, so a retry cannot apply
                # half a write.
                self._conn.close()
                self._conn = self._connect()
                return op(self._conn)

    def _fetch(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        sql = sql.replace("?", "%s")
        return self._run(lambda conn: conn.execute(sql, params).fetchall())

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        sql = sql.replace("?", "%s")
        self._run(lambda conn: conn.execute(sql, params))

    def _execute_many(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        sql = sql.replace("?", "%s")

        def op(conn):
            with conn.transaction(), conn.cursor() as cur:
                cur.executemany(sql, rows)

        self._run(op)

    def close(self) -> None:
        self._conn.close()


def open_store(settings: Settings) -> Store:
    """The store DATABASE_URL names; unset or empty means SQLite at DATA_DIR/inkbridge.sqlite3.

    postgres://... or postgresql://...  Postgres (any libpq URL)
    sqlite:///relative/path.db           SQLite, relative to the working directory
    sqlite:////absolute/path.db          SQLite, absolute path (four slashes)
    sqlite:// or sqlite:///:memory:      SQLite in memory (lost on exit; for tests)
    """
    url = settings.database_url
    if not url:
        return SQLiteStore(settings.db_path)
    if url.startswith(("postgres://", "postgresql://")):
        return PostgresStore(url)
    if url.startswith("sqlite://"):
        return SQLiteStore(sqlite_path(url))
    raise ValueError(
        f"DATABASE_URL must be a postgres://, postgresql:// or sqlite:// URL, got {url.split(':')[0]}:"
    )


def sqlite_path(url: str) -> str:
    """The file path in a sqlite:// URL (SQLAlchemy's convention), or ":memory:"."""
    rest = url.removeprefix("sqlite://")
    if rest in ("", "/:memory:"):
        return ":memory:"
    if not rest.startswith("/") or rest == "/":
        raise ValueError("a sqlite URL is sqlite:///relative/path or sqlite:////absolute/path")
    if "?" in rest:
        raise ValueError("sqlite URLs take no query parameters")
    return rest[1:]


def migrations(have: set[str], float_type: str) -> list[str]:
    """ALTER statements that add the ADDED_COLUMNS missing from a docs table with columns `have`."""
    return [f"ALTER TABLE docs ADD COLUMN {name} {decl.format(float=float_type)}"
            for name, decl in ADDED_COLUMNS.items() if name not in have]


def _statements(script: str) -> list[str]:
    lines = [line.split("--")[0] for line in script.splitlines()]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def _row_to_doc(row: dict[str, Any]) -> Doc:
    data = dict(row)
    if data.get("feedback"):
        data["feedback"] = json.loads(data["feedback"])
    return Doc(**data)


def now() -> float:
    return time.time()
