import pytest

from inkbridge.config import Settings
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

    pulled = await bridge.poll_outbox()
    assert [p.doc_id for p in pulled] == [pushed.doc_id]
    assert not pulled[0].external
    assert await t.list_folder("Outbox") == []
    assert len(await t.list_folder("Archive")) == 1

    stored = bridge.store.get(pushed.doc_id)
    assert stored.status == "pulled"
    assert stored.feedback["source"] == {"kind": "pdf", "ref": "https://claude.ai/artifact/x"}
    assert (stored.path / "annotated.pdf").exists() and list(stored.path.glob("strip-*.png"))

    assert await bridge.poll_outbox() == []
    feedback, images = bridge.get_annotations(pushed.doc_id)
    assert feedback[0]["marks"][0]["instruction"] == "Explain blast radius" and images
    assert bridge.list_returned()[0]["doc_id"] == pushed.doc_id


async def test_doc_moved_back_to_outbox_is_pulled_again(bridge):
    t = bridge.transport
    pushed = await bridge.push_document("Spec", pdf=sample_pdf())
    uuid = bridge.store.get(pushed.doc_id).rm_uuid
    t.annotate(uuid, rm_page([line([(0, 300), (10, 310)])]))
    await bridge.poll_outbox()
    t.annotate(uuid, rm_page([line([(0, 300), (10, 310)]), line([(0, 900), (50, 950)])]))
    assert len(await bridge.poll_outbox()) == 1
    assert bridge._interpreter.calls == 2


async def test_unknown_docs_are_processed_as_external(bridge):
    t = bridge.transport
    u = await t.upload_pdf(sample_pdf(), "Some paper", "Outbox")
    t.annotate(u, rm_page([line([(0, 300), (10, 310)])]))
    pulled = await bridge.poll_outbox()
    assert pulled[0].external and pulled[0].doc_id == f"ext-{u[:8]}"
    assert bridge.store.get(pulled[0].doc_id).feedback["tags"] == ["external"]


async def test_failed_archive_move_is_retried_without_reprocessing(bridge):
    t = bridge.transport
    pushed = await bridge.push_document("Spec", pdf=sample_pdf())
    uuid = bridge.store.get(pushed.doc_id).rm_uuid
    t.annotate(uuid, rm_page([line([(0, 300), (10, 310)])]))
    real_move = t.move

    async def broken(remote, folder):
        raise RuntimeError("network")

    t.move = broken
    assert await bridge.poll_outbox() == []
    assert bridge.store.get(pushed.doc_id).status == "pulled"
    t.move = real_move
    assert await bridge.poll_outbox() == []
    assert len(await t.list_folder("Archive")) == 1
    assert bridge._interpreter.calls == 1
