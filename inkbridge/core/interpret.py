"""Annotated page image -> strips -> Claude vision -> one feedback JSON per document."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from .overlay import PNG_SCALE, OverlayResult, page_text

log = logging.getLogger(__name__)

STRIP_HEIGHT = 1400
STRIP_OVERLAP = 0.15
MARK_KINDS = {"comment", "strike", "circle", "arrow", "question", "approve"}
PROMPTS_DIR = Path(os.environ.get("PROMPTS_DIR", Path(__file__).resolve().parents[2] / "prompts"))


@dataclass
class Strip:
    index: int
    top: int  # PNG pixels
    bottom: int
    png: bytes
    context: str


def load_prompt(name: str = "interpret.md") -> str:
    return (PROMPTS_DIR / name).read_text()


def strip_ranges(height: int, strip: int = STRIP_HEIGHT, overlap: float = STRIP_OVERLAP) -> list[tuple[int, int]]:
    if height <= strip:
        return [(0, height)]
    step = int(strip * (1 - overlap))
    ranges, top = [], 0
    while True:
        bottom = min(top + strip, height)
        ranges.append((top, bottom))
        if bottom >= height:
            return ranges
        top += step


def has_ink(top: int, bottom: int, ink_bands: list[tuple[int, int]]) -> bool:
    return any(a < bottom and b > top for a, b in ink_bands)


def slice_strips(result: OverlayResult, original_pdf: bytes) -> list[Strip]:
    image = Image.open(io.BytesIO(result.png))
    strips = []
    for i, (top, bottom) in enumerate(strip_ranges(image.height)):
        if not has_ink(top, bottom, result.ink_bands):
            continue
        buf = io.BytesIO()
        image.crop((0, top, image.width, bottom)).save(buf, "PNG", optimize=True)
        strips.append(Strip(i, top, bottom, buf.getvalue(), _context(result, original_pdf, top, bottom)))
    return strips


def _context(result: OverlayResult, original_pdf: bytes, top: int, bottom: int) -> str:
    parts = []
    for page_index, (ptop, ph) in enumerate(result.page_bands):
        a, b = max(top, ptop), min(bottom, ptop + ph)
        if a < b:
            parts.append(page_text(original_pdf, page_index, (a - ptop) / PNG_SCALE, (b - ptop) / PNG_SCALE))
    return "\n".join(p for p in parts if p)


def parse_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model reply (tolerates code fences and stray prose)."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text[text.find("{") : text.rfind("}") + 1]
    return json.loads(candidate)


def normalize_mark(m: dict[str, Any]) -> dict[str, Any]:
    kind = str(m.get("kind", "comment")).strip().lower()
    try:
        confidence = float(m.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    return {
        "kind": kind if kind in MARK_KINDS else "comment",
        "anchor_text": str(m.get("anchor_text") or "").strip(),
        "note": str(m.get("note") or "").strip(),
        "instruction": str(m.get("instruction") or "").strip(),
        "confidence": round(min(max(confidence, 0.0), 1.0), 2),
    }


def _key(s: str) -> str:
    return re.sub(r"\W+", " ", s.lower()).strip()


def merge_strips(results: list[dict[str, Any]]) -> tuple[list[dict], list[str], list[str]]:
    """Merge per-strip results; marks seen twice in the overlap keep the more confident copy."""
    marks: dict[tuple[str, str, str], dict] = {}
    notes: dict[str, str] = {}
    summaries = []
    for r in results:
        for raw in r.get("marks", []) or []:
            m = normalize_mark(raw)
            if not (m["anchor_text"] or m["note"] or m["instruction"]):
                continue
            key = (m["kind"], _key(m["anchor_text"]), _key(m["note"]))
            if key not in marks or m["confidence"] > marks[key]["confidence"]:
                marks[key] = m
        for n in r.get("free_notes", []) or []:
            n = str(n).strip()
            if n and _key(n) not in notes:
                notes[_key(n)] = n
        if (s := str(r.get("summary") or "").strip()):
            summaries.append(s)
    return list(marks.values()), list(notes.values()), summaries


class Interpreter:
    def __init__(self, api_key: str, model: str, client=None, concurrency: int = 4):
        if client is None:
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic(api_key=api_key or None)
        self.client = client
        self.model = model
        self.prompt = load_prompt()
        self._sem = asyncio.Semaphore(concurrency)

    async def _ask(self, content: list[dict], system: str, max_tokens: int = 4096) -> str:
        async with self._sem:
            resp = await self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": content}],
            )
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

    async def interpret_strip(self, strip: Strip, title: str, pen_colors: list[str]) -> dict[str, Any]:
        text = (
            f"Document: {title}\nStrip {strip.index + 1} (pixels {strip.top}-{strip.bottom}).\n"
            f"Pen colours used in this document: {', '.join(pen_colors) or 'unknown'}.\n\n"
            f"Typeset text in this strip:\n<context>\n{strip.context or '(none)'}\n</context>"
        )
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": base64.b64encode(strip.png).decode()}},
            {"type": "text", "text": text},
        ]
        reply = await self._ask(content, self.prompt)
        try:
            return parse_json(reply)
        except (ValueError, json.JSONDecodeError):
            log.warning("strip %s: unparseable reply %r", strip.index, reply[:200])
            return {"marks": [], "free_notes": [], "summary": ""}

    async def summarize(self, title: str, marks: list[dict], notes: list[str], summaries: list[str]) -> str:
        if not (marks or notes):
            return "No handwritten feedback found."
        if len(summaries) == 1:
            return summaries[0]
        payload = json.dumps({"marks": marks, "free_notes": notes, "strip_summaries": summaries}, indent=1)
        reply = await self._ask(
            [{"type": "text", "text": f"Document: {title}\n\n{payload}"}],
            "Summarize a reader's handwritten feedback on a document in at most two plain sentences: "
            "what they want changed overall. Reply with the sentences only.",
            max_tokens=300,
        )
        return reply.strip()

    async def interpret(self, result: OverlayResult, original_pdf: bytes, title: str) -> dict[str, Any]:
        strips = slice_strips(result, original_pdf)
        results = await asyncio.gather(*(self.interpret_strip(s, title, result.pen_colors) for s in strips))
        marks, notes, summaries = merge_strips(list(results))
        return {
            "highlights": result.highlights,
            "marks": marks,
            "free_notes": notes,
            "summary": await self.summarize(title, marks, notes, summaries),
            "strips": len(strips),
        }
