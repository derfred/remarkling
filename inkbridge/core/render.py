"""HTML / Markdown / URL -> a PDF of tall pages, each exactly as tall as its content, stamped with a doc ID.

Content up to HEIGHT_CAP_PX is one page. Longer content, and content with page breaks in it, becomes
several tall pages of one PDF (one document on the tablet). Where to cut is planned by plan_pages from
the break marks the page carries (BREAKS_JS): CSS page breaks, data-break="prefer", headings, and
boxes that must not be cut.
"""

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

# Page margins added above and below the content of every page after a cut.
CUT_MARGIN_PX = round(12 * PX_PER_MM)
PT_PER_PX = 72 / 96

# Where the content may or must be cut, in CSS px from the top of the document (see plan_pages).
BREAKS_JS = """
() => {
  const y = el => el.getBoundingClientRect().top + window.scrollY;
  const bottom = el => el.getBoundingClientRect().bottom + window.scrollY;
  const PAGE = new Set(['page', 'always', 'left', 'right', 'recto', 'verso']);
  const hard = [], prefer = [], avoid = [], gap = [];
  for (const el of document.body.querySelectorAll('*')) {
    const s = getComputedStyle(el);
    if (s.display === 'none' || el.getClientRects().length === 0) continue;
    if (PAGE.has(s.breakBefore) || el.dataset.break === 'page') hard.push(y(el));
    if (PAGE.has(s.breakAfter)) hard.push(bottom(el));
    if (el.dataset.break === 'prefer') prefer.push(y(el));
    if (s.breakInside === 'avoid' || s.breakInside === 'avoid-page'
        || ['IMG', 'SVG', 'CANVAS', 'VIDEO', 'FIGURE', 'TR', 'PRE'].includes(el.tagName.toUpperCase()))
      avoid.push([y(el), bottom(el)]);
  }
  // Gaps between blocks: the children of the body, or of the wrapper the content sits in.
  let box = document.body;
  while (box.children.length === 1 && box.firstElementChild.children.length) box = box.firstElementChild;
  const kids = Array.from(box.children).filter(el => el.getClientRects().length);
  kids.slice(1).forEach(el => gap.push(y(el)));
  const tops = sel => Array.from(document.querySelectorAll(sel)).map(y);
  return {height: document.documentElement.scrollHeight, hard, prefer, avoid, gap,
          h2: tops('h2'), h3: tops('h3')};
}
"""

# Kinds of cut, best first, when content must be cut to fit under the cap.
CUT_RANKS = ("prefer", "h2", "h3", "gap")


@dataclass
class Cut:
    at: float
    by: str  # "page" (a page break in the content), one of CUT_RANKS, or "cap" (nothing better fit)


def plan_pages(height: float, marks: dict, cap: float = HEIGHT_CAP_PX) -> list[Cut]:
    """Where to cut [0, height] into pages: at every page break, and wherever a page would be taller than `cap`.

    A page that is too tall is cut at the best mark in its second half (prefer, then h2, then h3, then a gap
    between blocks), the lowest one of that kind; failing that, at the best mark anywhere on the page. A cut
    never falls inside an `avoid` box (an image, a figure, a break-inside: avoid block) unless the box itself is
    taller than a page. With no mark at all the page is cut at the cap, moved up to the top of an avoid box it
    would cut through.
    """
    eps = 1.0
    avoid = [(a, b) for a, b in marks.get("avoid", []) if b - a < cap]

    def inside(o: float) -> bool:
        return any(a + eps < o < b - eps for a, b in avoid)

    def fits(o: float, start: float, end: float) -> bool:
        return start + eps < o < end - eps and not inside(o)

    hard = sorted({o for o in marks.get("hard", []) if eps < o < height - eps})
    cuts: list[Cut] = []
    start = 0.0
    for end in [*hard, height]:
        while end - start > cap:
            limit = start + cap
            pick = None
            for lo in (start + cap / 2, start):
                for kind in CUT_RANKS:
                    found = [o for o in marks.get(kind, []) if fits(o, max(lo, start), limit + eps)]
                    if found:
                        pick = Cut(max(found), kind)
                        break
                if pick:
                    break
            if pick is None:
                tops = [a for a, b in avoid if a < limit < b and a > start + eps]
                pick = Cut(min(tops) if tops else limit, "cap")
            cuts.append(pick)
            start = pick.at
        if end < height:
            cuts.append(Cut(end, "page"))
            start = end
    return cuts


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


def stack_windows(windows: list[bytes], margin_pt: float = CUT_MARGIN_PX * PT_PER_PX) -> bytes:
    """One-page PDFs of consecutive windows -> one PDF with a page each, with a margin on the side of every cut.

    The margin is added here rather than in the browser, so no content outside a window can end up hidden
    (and still in the text layer) on a page.
    """
    out = pymupdf.open()
    for i, w in enumerate(windows):
        with pymupdf.open(stream=w, filetype="pdf") as one:
            r = one[0].rect
            top = margin_pt if i else 0.0
            below = margin_pt if i < len(windows) - 1 else 0.0
            page = out.new_page(width=r.width, height=r.height + top + below)
            page.show_pdf_page(pymupdf.Rect(0, top, r.width, top + r.height), one, 0)
    data = out.tobytes(garbage=3, deflate=True)
    out.close()
    return data


async def render_tall_pdf(
    doc_id: str,
    title: str,
    *,
    html: str | None = None,
    url: str | None = None,
    ready_selector: str | None = None,
    timeout_ms: int = 30_000,
) -> tuple[bytes, list[Cut]]:
    """Render HTML (or a public URL) to one PDF of tall pages, cut where plan_pages says; returns it and the cuts."""
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
            marks = await page.evaluate(BREAKS_JS)
            height = marks["height"]
            # Pages after a cut get a margin above and below, so they need that much less content.
            cuts = plan_pages(height, marks, cap=HEIGHT_CAP_PX - 2 * CUT_MARGIN_PX)
            bounds = [0.0, *(c.at for c in cuts), float(height)]

            windows = []
            for start, end in zip(bounds, bounds[1:]):
                if len(bounds) > 2:
                    # Shift the content up and clip it to this window.
                    await page.evaluate(
                        """([start, h]) => {
                          document.documentElement.style.cssText = `height:${h}px;overflow:hidden`;
                          document.body.style.transform = `translateY(${-start}px)`;
                        }""",
                        [start, end - start],
                    )
                windows.append(await page.pdf(
                    width=f"{VIEWPORT_WIDTH_PX}px",
                    height=f"{int(end - start) + 1}px",
                    print_background=True,
                    page_ranges="1",
                ))
            data = windows[0] if len(windows) == 1 else stack_windows(windows)
            return stamp_id(data, doc_id, title), cuts
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
