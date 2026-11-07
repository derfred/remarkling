from types import SimpleNamespace

from inkbridge.core.interpret import Interpreter, has_ink, merge_strips, parse_json, strip_ranges
from inkbridge.core.overlay import overlay
from rmfixtures import line, rm_page, rmdoc, sample_pdf


def test_strip_ranges_overlap_and_cover():
    assert strip_ranges(1000) == [(0, 1000)]
    ranges = strip_ranges(4000)
    assert ranges[0] == (0, 1400) and ranges[1][0] == 1190
    assert ranges[-1][1] == 4000
    assert all(a < b for a, b in ranges)


def test_has_ink():
    assert has_ink(0, 100, [(90, 120)])
    assert not has_ink(0, 100, [(100, 120)])


def test_parse_json_tolerates_fences_and_prose():
    assert parse_json('Here:\n```json\n{"marks": []}\n```') == {"marks": []}
    assert parse_json('sure {"a": {"b": 1}} done') == {"a": {"b": 1}}


def test_merge_dedupes_overlap_keeping_higher_confidence():
    a = {"marks": [{"kind": "strike", "anchor_text": "blast radius", "note": "", "instruction": "Remove", "confidence": 0.4}],
         "free_notes": ["Looks good"], "summary": "s1"}
    b = {"marks": [{"kind": "STRIKE", "anchor_text": "Blast radius.", "note": "", "instruction": "Delete it", "confidence": 0.9},
                   {"kind": "weird", "anchor_text": "x", "confidence": "high"}],
         "free_notes": ["looks good"], "summary": ""}
    marks, notes, summaries = merge_strips([a, b])
    assert len(marks) == 2
    strike = next(m for m in marks if m["kind"] == "strike")
    assert strike["instruction"] == "Delete it" and strike["confidence"] == 0.9
    assert next(m for m in marks if m["anchor_text"] == "x")["kind"] == "comment"
    assert notes == ["Looks good"] and summaries == ["s1"]


class FakeMessages:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.replies.pop(0))])


async def test_interpreter_sends_only_inked_strips_with_context():
    pdf = sample_pdf(height_pt=3000)
    doc = rmdoc(pdf, [rm_page([line([(-300, 300), (100, 320)])])])
    result = overlay(doc)
    fake = FakeMessages(['{"marks": [{"kind": "comment", "anchor_text": "blast radius", "note": "define", '
                         '"instruction": "Explain blast radius", "confidence": 0.8}], "free_notes": [], '
                         '"summary": "Wants blast radius defined."}'])
    interp = Interpreter("", "claude-sonnet-5-5", client=SimpleNamespace(messages=fake))
    feedback = await interp.interpret(result, pdf, "Map viewer spec")
    assert len(fake.calls) == 1  # one inked strip out of ~5
    call = fake.calls[0]
    assert call["model"] == "claude-sonnet-5-5"
    text = call["messages"][0]["content"][1]["text"]
    assert "blast radius" in text
    assert feedback["marks"][0]["instruction"] == "Explain blast radius"
    assert feedback["summary"] == "Wants blast radius defined."
