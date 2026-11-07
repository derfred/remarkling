"""HTML / Markdown / URL -> one PDF page exactly as tall as the content, stamped with a doc ID."""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pikepdf
import pymupdf
from markdown_it import MarkdownIt

VIEWPORT_WIDTH_PX = 680  # ~180 mm
PX_PER_MM = 96 / 25.4
# PDF pages max out at 200 in (~5,080 mm); stay under it.
HEIGHT_CAP_PX = int(5000 * PX_PER_MM)
ID_KEY = "/InkBridgeId"
ID_PATTERN = re.compile(r"\brm-[a-z2-7]{6}(?:\.\d+)?\b")

PRINT_CSS = """
@page { margin: 0; }
html { background: #fff; }
body {
  margin: 0 !important;
  /* 14 mm left, 24 mm right for writing room, 40 mm bottom for a summary */
  padding: 12mm 24mm 40mm 14mm !important;
  box-sizing: border-box;
  max-width: none !important;
  width: 680px !important;
  background: #fff !important;
  color: #000;
}
img, svg, video, canvas { max-width: 100%; height: auto; }
pre { white-space: pre-wrap; word-break: break-word; }
.inkbridge-footer {
  margin-top: 10mm; padding-top: 2mm; border-top: 0.3mm solid #bbb;
  font: 8pt/1.3 system-ui, sans-serif; color: #888;
}
"""

MARKDOWN_CSS = """
body { font: 11pt/1.5 Georgia, 'Times New Roman', serif; }
h1, h2, h3, h4 { font-family: system-ui, sans-serif; line-height: 1.25; margin: 1.2em 0 0.4em; }
h1 { font-size: 20pt; } h2 { font-size: 15pt; } h3 { font-size: 12.5pt; }
code, pre { font: 9.5pt/1.4 'DejaVu Sans Mono', monospace; background: #f3f3f3; }
code { padding: 0 0.2em; } pre { padding: 0.6em 0.8em; }
pre code { background: none; padding: 0; }
table { border-collapse: collapse; width: 100%; font-size: 10pt; }
th, td { border: 0.3mm solid #999; padding: 0.3em 0.5em; text-align: left; vertical-align: top; }
blockquote { margin: 0; padding-left: 1em; border-left: 1mm solid #ccc; color: #444; }
"""

# Open <details>, reveal hidden tab panels and similar collapsed content before measuring.
EXPAND_JS = """
() => {
  document.querySelectorAll('details').forEach(d => d.open = true);
  document.querySelectorAll('[role=tabpanel], .tab-pane, .tab-content > *').forEach(el => {
    el.hidden = false;
    el.removeAttribute('aria-hidden');
    const s = getComputedStyle(el);
    if (s.display === 'none') el.style.setProperty('display', 'block', 'important');
  });
  document.querySelectorAll('[aria-expanded=false]').forEach(el => el.setAttribute('aria-expanded', 'true'));
}
"""

# Top offsets (CSS px) of every h2, used to pick split points for very tall content.
H2_OFFSETS_JS = """
() => Array.from(document.querySelectorAll('h2'))
  .map(h => h.getBoundingClientRect().top + window.scrollY)
"""


@dataclass
class RenderedPart:
    doc_id: str  # rm-xxxxxx or rm-xxxxxx.N
    pdf: bytes
    part: int  # 0 when not split
    parts: int


def markdown_to_html(markdown: str, title: str) -> str:
    md = MarkdownIt("commonmark", {"html": True, "linkify": False, "typographer": True}).enable(
        ["table", "strikethrough"]
    )
    body = md.render(markdown)
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{html_lib.escape(title)}</title>"
        f"<style>{MARKDOWN_CSS}</style></head><body>{body}</body></html>"
    )


def footer_html(doc_id: str, title: str) -> str:
    text = f"Ink Bridge · {doc_id} · {title} · {date.today().isoformat()}"
    return f"<div class='inkbridge-footer'>{html_lib.escape(text)}</div>"


def plan_splits(height: float, h2_offsets: list[float], cap: float = HEIGHT_CAP_PX) -> list[tuple[float, float]]:
    """Cut [0, height] into ranges no taller than `cap`, cutting at the nearest h2 above each cap.

    Falls back to a hard cut at the cap when a single section is taller than the cap.
    """
    ranges: list[tuple[float, float]] = []
    start = 0.0
    offsets = sorted(o for o in h2_offsets if o > 0)
    while height - start > cap:
        limit = start + cap
        candidates = [o for o in offsets if start + 1 < o <= limit]
        cut = candidates[-1] if candidates else limit
        ranges.append((start, cut))
        start = cut
    ranges.append((start, height))
    return ranges


async def render_tall_pdfs(
    doc_id: str,
    title: str,
    *,
    html: str | None = None,
    url: str | None = None,
    ready_selector: str | None = None,
    timeout_ms: int = 30_000,
) -> list[RenderedPart]:
    """Render HTML (or a public URL) to one or more tall single-page PDFs."""
    from playwright.async_api import async_playwright

    if (html is None) == (url is None):
        raise ValueError("pass exactly one of html or url")

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": VIEWPORT_WIDTH_PX, "height": 900})
            await page.emulate_media(media="print", color_scheme="light")
            if url is not None:
                await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            else:
                await page.set_content(html, wait_until="networkidle", timeout=timeout_ms)
            if ready_selector:
                await page.wait_for_selector(ready_selector, timeout=timeout_ms)
            await page.add_style_tag(content=PRINT_CSS)
            await page.evaluate("document.fonts.ready")
            await page.evaluate(EXPAND_JS)
            await page.evaluate(
                "(f) => document.body.insertAdjacentHTML('beforeend', f)", footer_html(doc_id, title)
            )
            height = await page.evaluate("document.documentElement.scrollHeight")
            offsets = await page.evaluate(H2_OFFSETS_JS)
            ranges = plan_splits(height, offsets)

            parts: list[RenderedPart] = []
            for i, (start, end) in enumerate(ranges, start=1):
                part_id = doc_id if len(ranges) == 1 else f"{doc_id}.{i}"
                if len(ranges) > 1:
                    # Shift the content up and clip it to this window.
                    await page.evaluate(
                        """([start, h]) => {
                          document.documentElement.style.cssText = `height:${h}px;overflow:hidden`;
                          document.body.style.transform = `translateY(${-start}px)`;
                        }""",
                        [start, end - start],
                    )
                pdf = await page.pdf(
                    width=f"{VIEWPORT_WIDTH_PX}px",
                    height=f"{int(end - start) + 1}px",
                    print_background=True,
                    page_ranges="1",
                )
                parts.append(
                    RenderedPart(part_id, stamp_id(pdf, part_id, title), i if len(ranges) > 1 else 0, len(ranges))
                )
            return parts
        finally:
            await browser.close()


def stamp_id(pdf: bytes, doc_id: str, title: str | None = None) -> bytes:
    """Write the doc ID into the PDF document info as /InkBridgeId."""
    import io

    with pikepdf.open(io.BytesIO(pdf)) as doc:
        doc.docinfo[ID_KEY] = doc_id
        if title:
            doc.docinfo["/Title"] = title
        out = io.BytesIO()
        doc.save(out)
        return out.getvalue()


def read_id(pdf: bytes) -> str | None:
    import io

    try:
        with pikepdf.open(io.BytesIO(pdf)) as doc:
            value = doc.docinfo.get(ID_KEY)
            return str(value) if value is not None else None
    except pikepdf.PdfError:
        return None


def passthrough_pdf(pdf: bytes, doc_id: str, title: str) -> bytes:
    """Existing PDFs keep their pages; add the footer line on the last page and stamp the ID."""
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    try:
        page = doc[-1]
        r = page.rect
        page.insert_text(
            (r.x0 + 12, r.y1 - 6), f"Ink Bridge · {doc_id} · {title}", fontsize=6, color=(0.55, 0.55, 0.55)
        )
        data = doc.tobytes(garbage=3, deflate=True)
    finally:
        doc.close()
    return stamp_id(data, doc_id, title)


def tablet_title(title: str, doc_id: str, part: int = 0, parts: int = 1) -> str:
    suffix = f" ({part}/{parts})" if parts > 1 else ""
    return f"{title}{suffix} · {doc_id}"


def id_from_title(name: str) -> str | None:
    m = ID_PATTERN.search(name)
    return m.group(0) if m else None


def write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path
