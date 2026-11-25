import time

import pytest

from inkbridge import uploads
from inkbridge.uploads import UploadError, Uploads, load_key, sniff_kind


def test_grant_round_trip_single_use_and_release():
    u = Uploads(b"k" * 32)
    token, grant = u.create("Spec", "markdown", "#ready")
    assert u.check(token) == grant
    claimed = u.claim(token)
    assert claimed.title == "Spec" and claimed.kind == "markdown" and claimed.ready_selector == "#ready"
    with pytest.raises(UploadError) as e:
        u.claim(token)
    assert e.value.status == 410
    u.release(claimed)  # a failed upload frees it again
    assert u.claim(token) == grant


def test_tampered_foreign_and_expired_tokens_are_refused(monkeypatch):
    u = Uploads(b"k" * 32, ttl_s=60)
    token, _ = u.create("Spec")
    payload, sig = token.split(".")
    for bad in (payload + "x." + sig, payload, "nonsense", payload + "." + sig[:-2] + "AA"):
        with pytest.raises(UploadError) as e:
            u.check(bad)
        assert e.value.status == 403
    with pytest.raises(UploadError):
        Uploads(b"j" * 32).check(token)
    later = time.time() + 61
    monkeypatch.setattr(uploads.time, "time", lambda: later)
    with pytest.raises(UploadError) as e:
        u.check(token)
    assert e.value.status == 410


def test_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        Uploads(b"k" * 32).create("x", "docx")


def test_key_is_created_once(tmp_path):
    key = load_key(tmp_path / "d" / "upload.key")
    assert len(key) == 32 and load_key(tmp_path / "d" / "upload.key") == key
    assert (tmp_path / "d" / "upload.key").stat().st_mode & 0o777 == 0o600


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
