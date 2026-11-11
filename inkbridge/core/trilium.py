"""Trilium ETAPI: one child note per pulled doc under a configured parent note."""

from __future__ import annotations

import html
from datetime import date
from typing import Any

import httpx

LOW_CONFIDENCE = 0.6


def note_html(feedback: dict[str, Any]) -> str:
    e = html.escape
    out = [f"<p><strong>Summary:</strong> {e(feedback.get('summary') or '')}</p>"]
    marks = feedback.get("marks") or []
    if marks:
        out.append("<h3>Marks</h3><ul>")
        for m in marks:
            bits = [f"<em>{e(m['kind'])}</em>"]
            if m.get("anchor_text"):
                bits.append(f"“{e(m['anchor_text'])}”")
            if m.get("note"):
                bits.append(e(m["note"]))
            if m.get("instruction"):
                bits.append(f"<strong>{e(m['instruction'])}</strong>")
            flag = " <span style='color:#b00'>[low confidence]</span>" if m.get("confidence", 1) < LOW_CONFIDENCE else ""
            out.append(f"<li>{' → '.join(bits)}{flag}</li>")
        out.append("</ul>")
    notes = feedback.get("free_notes") or []
    if notes:
        out.append("<h3>Notes</h3><ul>" + "".join(f"<li>{e(n)}</li>" for n in notes) + "</ul>")
    highlights = feedback.get("highlights") or []
    if highlights:
        out.append(
            "<h3>Highlights</h3><ul>"
            + "".join(f"<li>{e(h['text'])} <small>({e(h.get('color', ''))})</small></li>" for h in highlights)
            + "</ul>"
        )
    src = feedback.get("source") or {}
    if src.get("ref"):
        ref = e(src["ref"])
        link = f"<a href='{ref}'>{ref}</a>" if src["ref"].startswith("http") else ref
        out.append(f"<p><small>Source ({e(src.get('kind', ''))}): {link}</small></p>")
    return "\n".join(out)


class Trilium:
    def __init__(self, url: str, token: str, parent_note: str, public_url: str = "", client: httpx.AsyncClient | None = None):
        self.base = url.rstrip("/") + "/etapi"
        self.public_url = (public_url or url).rstrip("/")
        self.parent_note = parent_note
        self.client = client or httpx.AsyncClient(timeout=60)
        self.headers = {"Authorization": token}

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _req(self, method: str, path: str, **kw) -> httpx.Response:
        headers = {**self.headers, **kw.pop("headers", {})}
        r = await self.client.request(method, self.base + path, headers=headers, **kw)
        r.raise_for_status()
        return r

    def note_url(self, note_id: str) -> str:
        return f"{self.public_url}/#root/{self.parent_note}/{note_id}"

    async def find_note(self, doc_id: str) -> str | None:
        r = await self._req("GET", "/notes", params={"search": f'#inkbridge="{doc_id}"', "limit": 1})
        results = r.json().get("results", [])
        return results[0]["noteId"] if results else None

    async def note_exists(self, note_id: str) -> bool:
        r = await self.client.get(f"{self.base}/notes/{note_id}", headers=self.headers)
        return r.status_code == 200 and not r.json().get("isDeleted")

    async def upsert(
        self,
        doc_id: str,
        title: str,
        feedback: dict[str, Any],
        annotated_pdf: bytes,
        original_pdf: bytes | None,
        note_id: str | None = None,
    ) -> str:
        """Create or update the note for `doc_id`; returns the note ID."""
        if note_id and not await self.note_exists(note_id):
            note_id = None
        note_id = note_id or await self.find_note(doc_id)
        content = note_html(feedback)
        if note_id is None:
            r = await self._req(
                "POST", "/create-note",
                json={"parentNoteId": self.parent_note, "title": title, "type": "text", "content": content},
            )
            note_id = r.json()["note"]["noteId"]
        else:
            await self._req("PATCH", f"/notes/{note_id}", json={"title": title})
            await self._req("PUT", f"/notes/{note_id}/content", content=content.encode(),
                            headers={"Content-Type": "text/plain"})

        source = feedback.get("source") or {}
        await self._set_labels(note_id, {
            "inkbridge": doc_id,
            "source": source.get("ref") or source.get("kind") or "",
            "pulled": date.today().isoformat(),
        })
        await self._replace_attachments(note_id, {
            "annotated.pdf": annotated_pdf,
            **({"original.pdf": original_pdf} if original_pdf else {}),
        })
        return note_id

    async def _set_labels(self, note_id: str, labels: dict[str, str]) -> None:
        note = (await self._req("GET", f"/notes/{note_id}")).json()
        existing = {a["name"]: a for a in note.get("attributes", []) if a.get("type") == "label"}
        for name, value in labels.items():
            if name in existing:
                if existing[name].get("value") != value:
                    await self._req("PATCH", f"/attributes/{existing[name]['attributeId']}", json={"value": value})
            else:
                await self._req("POST", "/attributes",
                                json={"noteId": note_id, "type": "label", "name": name, "value": value})

    async def _replace_attachments(self, note_id: str, files: dict[str, bytes]) -> None:
        r = await self._req("GET", f"/notes/{note_id}/attachments")
        for att in r.json() if isinstance(r.json(), list) else r.json().get("results", []):
            if att.get("title") in files:
                await self._req("DELETE", f"/attachments/{att['attachmentId']}")
        for pos, (title, data) in enumerate(files.items()):
            r = await self._req(
                "POST", "/attachments",
                json={"ownerId": note_id, "role": "file", "mime": "application/pdf", "title": title,
                      "content": "", "position": (pos + 1) * 10},
            )
            att_id = r.json()["attachmentId"]
            await self._req("PUT", f"/attachments/{att_id}/content", content=data,
                            headers={"Content-Type": "application/octet-stream"})
