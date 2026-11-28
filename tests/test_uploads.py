import threading
import time

import pytest

from inkbridge import uploads
from inkbridge.core.store import SQLiteStore
from inkbridge.uploads import UploadError, Uploads, token_hash, upload_content


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


def test_default_kind_is_pdf(grants):
    token, _ = grants.create("Spec")
    assert grants.claim(token).kind == "pdf"


def test_upload_content():
    assert upload_content(b"%PDF-1.7 ...", "pdf") == {"pdf": b"%PDF-1.7 ..."}
    assert upload_content("# Tïtle".encode(), "markdown") == {"markdown": "# Tïtle"}
    assert upload_content(b"\xef\xbb\xbf<p>x</p>", "html") == {"html": "<p>x</p>"}  # BOM dropped
    assert upload_content(b"PK\x03\x04...", "rmdoc") == {"rmdoc": b"PK\x03\x04..."}
    for data, kind in ((b"# not a pdf", "pdf"), (b"\xff\xfe\x00", "markdown"), (b"%PDF-\xff", "html"),
                       (b"%PDF-1.7", "rmdoc")):
        with pytest.raises(UploadError) as e:
            upload_content(data, kind)
        assert e.value.status == 415
