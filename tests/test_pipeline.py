import pytest

from inkbridge.config import Settings
from inkbridge.core import pipeline
from inkbridge.core.pipeline import InkBridge
from inkbridge.core.store import SQLiteStore
from fakes import FakeInterpreter, MemoryTransport
from rmfixtures import line, rm_page, sample_pdf


@pytest.fixture
def bridge(tmp_path):
    settings = Settings(data_dir=tmp_path, anthropic_api_key="x", trilium_url="")
    return InkBridge(settings, SQLiteStore(":memory:"), MemoryTransport(), FakeInterpreter())


async def test_push_pdf_uploads_to_inbox(bridge):
    result = await bridge.push_document("Spec", pdf=sample_pdf())
    assert result.doc_id.startswith("rm-") and result.tablet_paths == [f"/Inbox/Spec · {result.doc_id}"]
    inbox = await bridge.list_inbox()
    assert inbox[0]["doc_id"] == result.doc_id
    assert bridge.store.get(result.doc_id).status == "pushed"


async def test_push_requires_exactly_one_input(bridge):
    with pytest.raises(ValueError):
        await bridge.push_document("x")
    with pytest.raises(ValueError):
        await bridge.push_document("x", pdf=b"%PDF", url="https://e.com")


async def test_poll_processes_outbox_archives_and_is_idempotent(bridge):
    t = bridge.transport
    pushed = await bridge.push_document("Spec", pdf=sample_pdf(), source_ref="https://claude.ai/artifact/x")
    doc = bridge.store.get(pushed.doc_id)
    t.annotate(doc.rm_uuid, rm_page([line([(-200, 300), (200, 320)])]))

    result = await bridge.poll()
    assert [p.doc_id for p in result.pulled] == [pushed.doc_id] and result.failed == []
    pulled = result.pulled[0]
    assert not pulled.external and pulled.interpretation == "done" and pulled.marks == 1 and pulled.strips
    assert await t.list_folder("Outbox") == []
    assert len(await t.list_folder("Archive")) == 1

    stored = bridge.store.get(pushed.doc_id)
    assert stored.status == "pulled" and stored.interpret_status == "done"
    assert stored.feedback["source"] == {"kind": "pdf", "ref": "https://claude.ai/artifact/x"}
    assert (stored.path / "annotated.pdf").exists() and list(stored.path.glob("strip-*.png"))
    assert (stored.path / "ink.rmdoc").exists()

    assert (await bridge.poll()).pulled == []
    feedback, images = bridge.get_annotations(pushed.doc_id)
    assert feedback[0]["marks"][0]["instruction"] == "Explain blast radius" and images
    assert feedback[0]["interpretation"] == "done"
    assert bridge.list_returned()[0]["doc_id"] == pushed.doc_id


async def test_pull_works_without_an_interpreter(tmp_path):
    settings = Settings(data_dir=tmp_path, anthropic_api_key="", trilium_url="")
    bridge = InkBridge(settings, SQLiteStore(":memory:"), MemoryTransport())
    assert bridge.interpreter is None
    pushed = await bridge.push_document("Spec", pdf=sample_pdf())
    bridge.transport.annotate(bridge.store.get(pushed.doc_id).rm_uuid, rm_page([line([(0, 300), (10, 310)])]))

    result = await bridge.poll()
    assert [p.interpretation for p in result.pulled] == ["off"] and result.pulled[0].marks is None
    feedback, images = bridge.get_annotations(pushed.doc_id)
    assert images and feedback[0]["status"] == "pulled" and feedback[0]["interpretation"] == "off"
    assert "marks" not in feedback[0]
    assert await bridge.interpret_pending() == []


async def test_failed_pull_is_recorded_and_backed_off(bridge, monkeypatch):
    t = bridge.transport
    pushed = await bridge.push_document("Spec", pdf=sample_pdf())
    uuid = bridge.store.get(pushed.doc_id).rm_uuid
    t.annotate(uuid, rm_page([line([(0, 300), (10, 310)])]))
    real_download, downloads = t.download, []

    async def broken(remote):
        downloads.append(remote.version)
        raise ConnectionError("cloud down")

    t.download = broken
    result = await bridge.poll()
    assert result.pulled == []
    [failed] = result.failed
    assert (failed.doc_id, failed.stage, failed.attempts) == (pushed.doc_id, "download", 1)
    assert failed.error == "ConnectionError: cloud down"
    stored = bridge.store.get(pushed.doc_id)
    assert stored.status == "failed" and stored.error_version == t.docs[uuid][0].version
    assert bridge.list_returned()[0]["failure"]["stage"] == "download"

    # Same version, inside the backoff: reported, not retried.
    assert (await bridge.poll()).failed[0].attempts == 1 and len(downloads) == 1

    # Backoff over: retried, and the attempt count grows.
    monkeypatch.setattr(pipeline, "now", lambda: failed.retry_at + 1)
    assert (await bridge.poll()).failed[0].attempts == 2 and len(downloads) == 2

    # A new version (more ink) is retried at once, and success clears the failure.
    monkeypatch.undo()
    t.download = real_download
    t.annotate(uuid, rm_page([line([(0, 300), (10, 310)]), line([(0, 900), (50, 950)])]))
    result = await bridge.poll()
    assert [p.doc_id for p in result.pulled] == [pushed.doc_id] and result.failed == []
    stored = bridge.store.get(pushed.doc_id)
    assert stored.status == "pulled" and stored.error is None and stored.attempts == 0


async def test_failed_interpretation_keeps_the_pull_and_is_retried(bridge, monkeypatch):
    t = bridge.transport
    pushed = await bridge.push_document("Spec", pdf=sample_pdf())
    t.annotate(bridge.store.get(pushed.doc_id).rm_uuid, rm_page([line([(0, 300), (10, 310)])]))
    real = bridge._interpreter.interpret

    async def unauthorized(*args):
        raise PermissionError("401 invalid bearer token")

    bridge._interpreter.interpret = unauthorized
    result = await bridge.poll()
    [pulled] = result.pulled
    assert pulled.interpretation == "failed" and "401" in pulled.interpretation_error
    assert len(await t.list_folder("Archive")) == 1  # the pull finished regardless
    feedback, images = bridge.get_annotations(pushed.doc_id)
    assert images and feedback[0]["interpretation_error"] == "PermissionError: 401 invalid bearer token"

    bridge._interpreter.interpret = real
    assert await bridge.interpret_pending() == []  # backing off
    stored = bridge.store.get(pushed.doc_id)
    monkeypatch.setattr(pipeline, "now", lambda: pipeline.retry_at(stored.interpret_failed_at, 1) + 1)
    assert await bridge.interpret_pending() == [pushed.doc_id]
    assert bridge.store.get(pushed.doc_id).feedback["marks"]


async def test_doc_moved_back_to_outbox_is_pulled_again(bridge):
    t = bridge.transport
    pushed = await bridge.push_document("Spec", pdf=sample_pdf())
    uuid = bridge.store.get(pushed.doc_id).rm_uuid
    t.annotate(uuid, rm_page([line([(0, 300), (10, 310)])]))
    await bridge.poll()
    t.annotate(uuid, rm_page([line([(0, 300), (10, 310)]), line([(0, 900), (50, 950)])]))
    assert len((await bridge.poll()).pulled) == 1
    assert bridge._interpreter.calls == 2


async def test_unknown_docs_are_processed_as_external(bridge):
    t = bridge.transport
    u = await t.upload_pdf(sample_pdf(), "Some paper", "Outbox")
    t.annotate(u, rm_page([line([(0, 300), (10, 310)])]))
    pulled = (await bridge.poll()).pulled
    assert pulled[0].external and pulled[0].doc_id == f"ext-{u[:8]}"
    assert bridge.store.get(pulled[0].doc_id).feedback["tags"] == ["external"]


async def test_placeholder_for_an_unidentified_failure_is_dropped(bridge):
    """A renamed pushed doc fails before its PDF is read: the failure goes on an ext- placeholder,
    which the successful pull replaces with the real doc (found by the ID stamped in the PDF)."""
    t = bridge.transport
    pushed = await bridge.push_document("Spec", pdf=sample_pdf())
    uuid = bridge.store.get(pushed.doc_id).rm_uuid
    doc = bridge.store.get(pushed.doc_id)
    doc.rm_uuid = None  # as if the push's record of the UUID were lost
    bridge.store.upsert(doc)
    r, rmdoc = t.docs[uuid]
    t.docs[uuid] = (type(r)(r.uuid, "Renamed on the tablet", r.parent, r.version), rmdoc)
    t.annotate(uuid, rm_page([line([(0, 300), (10, 310)])]))
    real_download = t.download

    async def broken(remote):
        raise ConnectionError("cloud down")

    t.download = broken
    [failed] = (await bridge.poll()).failed
    assert failed.doc_id == f"ext-{uuid[:8]}"

    t.download = real_download
    t.annotate(uuid, rm_page([line([(0, 300), (10, 310)]), line([(0, 900), (50, 950)])]))
    assert [p.doc_id for p in (await bridge.poll()).pulled] == [pushed.doc_id]
    assert bridge.store.get(f"ext-{uuid[:8]}") is None


async def test_failed_archive_move_is_retried_without_reprocessing(bridge):
    t = bridge.transport
    pushed = await bridge.push_document("Spec", pdf=sample_pdf())
    uuid = bridge.store.get(pushed.doc_id).rm_uuid
    t.annotate(uuid, rm_page([line([(0, 300), (10, 310)])]))
    real_move = t.move

    async def broken(remote, folder):
        raise RuntimeError("network")

    t.move = broken
    assert [p.doc_id for p in (await bridge.poll()).pulled] == [pushed.doc_id]
    assert bridge.store.get(pushed.doc_id).status == "pulled"
    t.move = real_move
    assert (await bridge.poll()).pulled == []
    assert len(await t.list_folder("Archive")) == 1
    assert bridge._interpreter.calls == 1


async def test_notebooks_in_outbox_are_processed(bridge):
    from rmscene import scene_items as si

    from inkbridge.core.cloud import RemoteDoc
    from rmfixtures import notebook, page_text

    t = bridge.transport
    doc = notebook([rm_page([line([(0, 300), (10, 310)])],
                            text=page_text([(si.ParagraphStyle.PLAIN, "Typed on the tablet")]))])
    t.docs[doc.uuid] = (RemoteDoc(doc.uuid, "Notes", "Outbox", version="v1"), doc)
    pulled = (await bridge.poll()).pulled
    assert len(pulled) == 1 and pulled[0].external
    stored = bridge.store.get(pulled[0].doc_id)
    assert stored.status == "pulled" and t.docs[doc.uuid][0].parent == "Archive"
    assert (stored.path / "original.pdf").exists() and list(stored.path.glob("strip-*.png"))


async def test_failure_while_storing_is_recorded_and_others_still_pulled(bridge):
    t = bridge.transport
    first = await bridge.push_document("First", pdf=sample_pdf())
    second = await bridge.push_document("Second", pdf=sample_pdf())
    for p in (first, second):
        t.annotate(bridge.store.get(p.doc_id).rm_uuid, rm_page([line([(0, 300), (10, 310)])]))
    real_upsert = bridge.store.upsert

    def flaky(doc):
        if doc.doc_id == first.doc_id and doc.status == "pulled":
            raise OSError("disk full")
        return real_upsert(doc)

    bridge.store.upsert = flaky
    result = await bridge.poll_outbox()
    assert [p.doc_id for p in result.pulled] == [second.doc_id]
    assert [(f.doc_id, f.stage, f.error) for f in result.failed] == [(first.doc_id, "save", "OSError: disk full")]
