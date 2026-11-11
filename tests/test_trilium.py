import json

import httpx

from inkbridge.core.trilium import Trilium, note_html

FEEDBACK = {
    "summary": "Define terms.",
    "marks": [{"kind": "question", "anchor_text": "blast radius", "note": "?", "instruction": "Define it", "confidence": 0.3}],
    "free_notes": ["Ship it"],
    "highlights": [{"text": "small", "color": "yellow"}],
    "source": {"kind": "artifact", "ref": "https://claude.ai/artifact/x"},
}


def test_note_html_flags_low_confidence_and_escapes():
    html = note_html({**FEEDBACK, "summary": "<b>x</b>"})
    assert "&lt;b&gt;" in html and "[low confidence]" in html and "Ship it" in html


class FakeEtapi:
    def __init__(self):
        self.notes, self.attrs, self.atts, self.n = {}, {}, {}, 0

    def _id(self):
        self.n += 1
        return f"id{self.n}"

    def handler(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "tok"
        p, m = req.url.path.removeprefix("/etapi"), req.method
        body = json.loads(req.content) if req.headers.get("content-type") == "application/json" else None
        if p == "/create-note":
            nid = self._id()
            self.notes[nid] = {"noteId": nid, "title": body["title"], "content": body["content"]}
            return httpx.Response(201, json={"note": {"noteId": nid}})
        if p == "/notes" and m == "GET":
            want = req.url.params["search"].split('"')[1]
            hits = [{"noteId": a["noteId"]} for a in self.attrs.values() if a["name"] == "inkbridge" and a["value"] == want]
            return httpx.Response(200, json={"results": hits})
        if p.startswith("/notes/") and p.endswith("/content"):
            self.notes[p.split("/")[2]]["content"] = req.content.decode()
            return httpx.Response(204)
        if p.startswith("/notes/") and p.endswith("/attachments"):
            nid = p.split("/")[2]
            return httpx.Response(200, json=[a for a in self.atts.values() if a["ownerId"] == nid])
        if p.startswith("/notes/"):
            nid = p.split("/")[2]
            if m == "PATCH":
                self.notes[nid].update(body)
                return httpx.Response(200, json=self.notes[nid])
            if nid not in self.notes:
                return httpx.Response(404)
            attrs = [{"attributeId": k, "type": "label", **a} for k, a in self.attrs.items() if a["noteId"] == nid]
            return httpx.Response(200, json={**self.notes[nid], "attributes": attrs})
        if p == "/attributes":
            self.attrs[self._id()] = {"noteId": body["noteId"], "name": body["name"], "value": body["value"]}
            return httpx.Response(201, json={})
        if p.startswith("/attributes/"):
            self.attrs[p.split("/")[2]]["value"] = body["value"]
            return httpx.Response(200, json={})
        if p == "/attachments":
            aid = self._id()
            self.atts[aid] = {"attachmentId": aid, "ownerId": body["ownerId"], "title": body["title"]}
            return httpx.Response(201, json={"attachmentId": aid})
        if p.startswith("/attachments/") and p.endswith("/content"):
            self.atts[p.split("/")[2]]["size"] = len(req.content)
            return httpx.Response(204)
        if p.startswith("/attachments/") and m == "DELETE":
            del self.atts[p.split("/")[2]]
            return httpx.Response(204)
        return httpx.Response(404, text=p)


async def test_upsert_creates_then_updates_in_place():
    fake = FakeEtapi()
    t = Trilium("https://notes.test", "tok", "parent", client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    nid = await t.upsert("rm-abc234", "Spec · rm-abc234", FEEDBACK, b"%PDF-annotated", b"%PDF-orig")
    assert fake.notes[nid]["title"] == "Spec · rm-abc234"
    assert sorted(a["title"] for a in fake.atts.values()) == ["annotated.pdf", "original.pdf"]

    again = await t.upsert("rm-abc234", "Spec v2 · rm-abc234", {**FEEDBACK, "summary": "New"}, b"%PDF-2", None)
    assert again == nid and len(fake.notes) == 1
    assert "New" in fake.notes[nid]["content"] and fake.notes[nid]["title"] == "Spec v2 · rm-abc234"
    assert [a["title"] for a in fake.atts.values()] == ["original.pdf", "annotated.pdf"]
    assert sum(1 for a in fake.attrs.values() if a["name"] == "inkbridge") == 1
    assert t.note_url(nid) == f"https://notes.test/#root/parent/{nid}"
