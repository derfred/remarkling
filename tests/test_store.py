"""Both Store backends against the same expectations. Postgres runs when TEST_DATABASE_URL is set
(it drops and recreates the `docs` and `cache` tables there, so point it at a throwaway database)."""

import os

import pytest

from inkbridge.config import Settings
from inkbridge.core.store import Doc, PostgresStore, SQLiteStore, open_store, sqlite_path

PG_URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest.fixture(params=["sqlite", "postgres"])
def store(request):
    if request.param == "sqlite":
        s = SQLiteStore(":memory:")
    else:
        if not PG_URL:
            pytest.skip("TEST_DATABASE_URL not set")
        import psycopg

        with psycopg.connect(PG_URL, autocommit=True) as conn:
            conn.execute("DROP TABLE IF EXISTS docs, cache")
        s = PostgresStore(PG_URL)
    yield s
    s.close()


def test_round_trip_and_upsert(store):
    doc = Doc("rm-aaaaaa", "rm-aaaaaa", "Title", "html", pushed_at=1_700_000_000.123456, feedback={"a": [1]})
    store.upsert(doc)
    assert store.get("rm-aaaaaa") == doc  # float timestamps keep full precision

    doc.status, doc.rm_uuid, doc.pulled_at = "pulled", "uuid-1", 1_700_000_100.5
    store.upsert(doc)
    assert store.get("rm-aaaaaa").status == "pulled"
    assert store.by_rm_uuid("uuid-1").doc_id == "rm-aaaaaa"
    assert store.get("missing") is None


def test_queries(store):
    store.upsert(Doc("rm-bbbbbb.2", "rm-bbbbbb", "B2", "html", pushed_at=10, dir="/d/2"))
    store.upsert(Doc("rm-bbbbbb.1", "rm-bbbbbb", "B1", "html", pushed_at=10, pulled_at=50, dir="/d/1"))
    store.upsert(Doc("rm-cccccc", "rm-cccccc", "C", "pdf", pushed_at=10, pulled_at=60))

    assert [d.doc_id for d in store.parts("rm-bbbbbb")] == ["rm-bbbbbb.1", "rm-bbbbbb.2"]
    assert [d.doc_id for d in store.returned()] == ["rm-cccccc", "rm-bbbbbb.1"]
    assert [d.doc_id for d in store.returned(since=55)] == ["rm-cccccc"]
    assert [d.doc_id for d in store.returned(limit=1)] == ["rm-cccccc"]
    assert {d.doc_id for d in store.older_than(20)} == {"rm-bbbbbb.2"}
    assert store.mint_id() not in {"rm-bbbbbb", "rm-cccccc"}


def test_cache(store):
    assert store.cache_get_many(["x:1"]) == {}
    store.cache_put_many({"x:1": {"a": 1}, "x:2": [2], "y:1": "other"})
    store.cache_put_many({"x:1": {"a": "ignored"}})  # immutable values: first write wins
    assert store.cache_get_many(["x:1", "x:2", "x:3", "x:1"]) == {"x:1": {"a": 1}, "x:2": [2]}

    many = {f"x:{i}": i for i in range(1200)}  # more than one IN (...) chunk
    store.cache_put_many(many)
    assert len(store.cache_get_many(list(many))) == 1200

    assert store.cache_retain("x:", {"x:5"}) == 1199
    assert store.cache_get_many(["x:5", "x:6", "y:1"]) == {"x:5": 5, "y:1": "other"}


def test_schema_creation_is_idempotent(store):
    if isinstance(store, SQLiteStore):
        pytest.skip("in-memory SQLite has nothing to reopen")
    store.upsert(Doc("rm-dddddd", "rm-dddddd", "D", "html"))
    again = PostgresStore(PG_URL)
    assert again.get("rm-dddddd").title == "D"
    again.close()


def test_open_store_selects_backend(tmp_path):
    s = open_store(Settings(data_dir=tmp_path, database_url=""))
    assert isinstance(s, SQLiteStore) and s.path == tmp_path / "inkbridge.sqlite3"
    s.close()
    with pytest.raises(ValueError, match="mysql:") as err:
        open_store(Settings(data_dir=tmp_path, database_url="mysql://u:secret@h/db"))
    assert "secret" not in str(err.value)

    s = open_store(Settings(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path}/sub/x.db"))
    assert isinstance(s, SQLiteStore) and s.path == tmp_path / "sub" / "x.db"
    s.close()


@pytest.mark.parametrize("url, path", [
    ("sqlite:///data/x.db", "data/x.db"),
    ("sqlite:////data/x.db", "/data/x.db"),
    ("sqlite://", ":memory:"),
    ("sqlite:///:memory:", ":memory:"),
])
def test_sqlite_path(url, path):
    assert sqlite_path(url) == path


@pytest.mark.parametrize("url", ["sqlite:/x.db", "sqlite://x.db", "sqlite:///", "sqlite:///x.db?mode=ro"])
def test_sqlite_path_rejects(url):
    with pytest.raises(ValueError):
        open_store(Settings(database_url=url))
