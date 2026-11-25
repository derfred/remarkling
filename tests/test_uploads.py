import threading
import time

import pytest

from inkbridge import uploads
from inkbridge.core.store import SQLiteStore
from inkbridge.uploads import UploadError, Uploads, sniff_kind, token_hash


@pytest.fixture
def grants(tmp_path):
    return Uploads(SQLiteStore(tmp_path / "s.db"))


def test_grant_round_trip_single_use_and_release(grants):
    token, grant = grants.create("Spec", "markdown", "#ready")
    claimed = grants.claim(token)
    assert claimed == grant
    assert (claimed.title, claimed.kind, claimed.ready_selector) == ("Spec", "markdown", "#ready")
    with pytest.raises(UploadError) as e:
        grants.claim(token)
    assert e.value.status == 410 and "already used" in str(e.value)
    grants.release(claimed)  # a failed upload frees it again
    assert grants.claim(token) == grant
    grants.complete(grant, "rm-aaaaaa")
    grants.release(grant)  # no effect once it pushed something
    with pytest.raises(UploadError):
        grants.claim(token)


def test_only_the_token_hash_is_stored(grants):
    token, _ = grants.create("Spec")
    [row] = grants.store._fetch("SELECT * FROM uploads")
    assert row["token_hash"] == token_hash(token) and token not in str(row)


def test_survives_a_restart(tmp_path):
    token, _ = Uploads(SQLiteStore(tmp_path / "s.db")).create("Spec")
    after_restart = Uploads(SQLiteStore(tmp_path / "s.db"))
    assert after_restart.claim(token).title == "Spec"
    with pytest.raises(UploadError):
        Uploads(SQLiteStore(tmp_path / "s.db")).claim(token)


def test_concurrent_claims_admit_one(tmp_path):
    token, _ = Uploads(SQLiteStore(tmp_path / "s.db")).create("Spec")
    results = []

    def claim():
        try:
            Uploads(SQLiteStore(tmp_path / "s.db")).claim(token)
            results.append("ok")
        except UploadError:
            results.append("refused")

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["ok"] + ["refused"] * 7


def test_unknown_and_expired_tokens_are_refused(grants, monkeypatch):
    with pytest.raises(UploadError) as e:
        grants.claim("nonsense")
    assert e.value.status == 403
    grants.ttl_s = 60
    token, _ = grants.create("Spec")
    later = time.time() + 61
    monkeypatch.setattr(uploads.time, "time", lambda: later)
    with pytest.raises(UploadError) as e:
        grants.claim(token)
    assert e.value.status == 410 and "expired" in str(e.value)


def test_old_grants_are_pruned(grants, monkeypatch):
    grants.create("Old")
    later = time.time() + grants.ttl_s + uploads.KEEP_S + 1
    monkeypatch.setattr(uploads.time, "time", lambda: later)
    grants.create("New")
    assert [r["title"] for r in grants.store._fetch("SELECT title FROM uploads")] == ["New"]


def test_unknown_kind_is_refused(grants):
    with pytest.raises(ValueError):
        grants.create("x", "docx")


@pytest.mark.parametrize("data,ctype,kind,expected", [
    (b"%PDF-1.7 ...", "", None, "pdf"),
    (b"%PDF-1.7 ...", "text/html", "html", "pdf"),
    (b"# Title", "", "markdown", "markdown"),
    (b"<p>x</p>", "text/html; charset=utf-8", None, "html"),
    (b"# Title", "text/markdown", None, "markdown"),
    (b"  <!DOCTYPE html><html>", "application/octet-stream", None, "html"),
])
def test_sniff_kind(data, ctype, kind, expected):
    assert sniff_kind(data, ctype, kind) == expected


def test_sniff_kind_refuses():
    with pytest.raises(UploadError):
        sniff_kind(b"# not a pdf", "", "pdf")
    with pytest.raises(UploadError):
        sniff_kind(b"\x00\x01", "application/octet-stream", None)
