"""Tests for the pure-logic parts of the orchestrator: Docs API parsing,
diff validation, and OOXML tracked-changes construction.

None of these touch the network — they exercise the deterministic pieces the
design contract requires the orchestrator to get right on its own.
"""
import io
import re
import zipfile

import pytest
from lxml import etree

import orchestrator as orch

W = orch.W_NS


def w(tag):
    return f"{{{W}}}{tag}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def paragraphs():
    return [
        {"index": 0, "style": "Title", "text": "The Glass Orchard"},
        {"index": 1, "style": "Normal", "text": "She walked quickly into the room and sat down."},
        {"index": 2, "style": "Heading 1", "text": "Chapter One"},
        {"index": 3, "style": "Normal", "text": ""},
    ]


@pytest.fixture
def job(paragraphs):
    return {
        "doc_id": "fake-doc-id-123",
        "plain_text": "\n".join(p["text"] for p in paragraphs),
        "paragraphs": paragraphs,
    }


# ---------------------------------------------------------------------------
# paragraphs_from_document / build_job  (Docs API response -> job.json)
# ---------------------------------------------------------------------------

def _docs_api_paragraph(named_style, *contents):
    return {
        "paragraph": {
            "paragraphStyle": {"namedStyleType": named_style},
            "elements": [{"textRun": {"content": c}} for c in contents],
        }
    }


def test_paragraphs_from_document_extracts_text_and_maps_styles():
    document = {
        "body": {
            "content": [
                {"sectionBreak": {}},  # non-paragraph elements are skipped
                _docs_api_paragraph("TITLE", "The Glass Orchard\n"),
                _docs_api_paragraph("HEADING_1", "Chapter ", "One\n"),
                _docs_api_paragraph("NORMAL_TEXT", "Plain text.\n"),
            ]
        }
    }
    paragraphs = orch.paragraphs_from_document(document)
    assert [p["text"] for p in paragraphs] == ["The Glass Orchard", "Chapter One", "Plain text."]
    assert [p["style"] for p in paragraphs] == ["Title", "Heading 1", "Normal"]
    assert [p["index"] for p in paragraphs] == [0, 1, 2]


def test_paragraphs_from_document_unknown_style_passes_through():
    document = {"body": {"content": [_docs_api_paragraph("SOME_FUTURE_STYLE", "text\n")]}}
    paragraphs = orch.paragraphs_from_document(document)
    assert paragraphs[0]["style"] == "SOME_FUTURE_STYLE"


def test_build_job_joins_paragraph_text_and_omits_checksum():
    document = {
        "body": {
            "content": [
                _docs_api_paragraph("NORMAL_TEXT", "Hello\n"),
                _docs_api_paragraph("NORMAL_TEXT", "World\n"),
            ]
        }
    }
    job_data = orch.build_job("doc-abc", document)
    assert job_data["doc_id"] == "doc-abc"
    assert job_data["plain_text"] == "Hello\nWorld"
    assert "checksum" not in job_data
    assert len(job_data["paragraphs"]) == 2


# ---------------------------------------------------------------------------
# validate_edits
# ---------------------------------------------------------------------------

def test_accepts_exact_match(job):
    diff = {"edits": [{"para": 1, "start": 4, "end": 10, "old": "walked", "new": "drifted"}]}
    accepted, rejected = orch.validate_edits(job, diff)
    assert accepted == diff["edits"]
    assert rejected == []


def test_rejects_mismatched_old(job):
    diff = {"edits": [{"para": 1, "start": 11, "end": 18, "old": "rapidly", "new": "slowly"}]}
    accepted, rejected = orch.validate_edits(job, diff)
    assert accepted == []
    assert len(rejected) == 1
    assert "does not match source" in rejected[0]["reason"]
    assert rejected[0]["old"] == "rapidly"  # original edit fields preserved on rejection


def test_rejects_out_of_range_paragraph(job):
    diff = {"edits": [{"para": 99, "start": 0, "end": 3, "old": "abc", "new": "xyz"}]}
    accepted, rejected = orch.validate_edits(job, diff)
    assert accepted == []
    assert "out of range" in rejected[0]["reason"]


def test_rejects_negative_paragraph_index(job):
    diff = {"edits": [{"para": -1, "start": 0, "end": 3, "old": "abc", "new": "xyz"}]}
    accepted, rejected = orch.validate_edits(job, diff)
    assert accepted == []
    assert "out of range" in rejected[0]["reason"]


def test_rejects_overlapping_edits(job):
    diff = {
        "edits": [
            {"para": 1, "start": 4, "end": 10, "old": "walked", "new": "drifted"},
            {"para": 1, "start": 0, "end": 10, "old": "She walked", "new": "He ran"},
        ]
    }
    accepted, rejected = orch.validate_edits(job, diff)
    assert len(accepted) == 1 and accepted[0]["new"] == "drifted"
    assert len(rejected) == 1
    assert "overlaps" in rejected[0]["reason"]


def test_accepts_adjacent_non_overlapping_edits(job):
    # "walked" is text[4:10], "quickly" is text[11:18] — adjacent, not overlapping
    diff = {
        "edits": [
            {"para": 1, "start": 4, "end": 10, "old": "walked", "new": "drifted"},
            {"para": 1, "start": 11, "end": 18, "old": "quickly", "new": "softly"},
        ]
    }
    accepted, rejected = orch.validate_edits(job, diff)
    assert len(accepted) == 2
    assert rejected == []


def test_accepts_edits_across_different_paragraphs(job):
    diff = {
        "edits": [
            {"para": 0, "start": 4, "end": 9, "old": "Glass", "new": "Stone"},
            {"para": 1, "start": 4, "end": 10, "old": "walked", "new": "drifted"},
        ]
    }
    accepted, rejected = orch.validate_edits(job, diff)
    assert len(accepted) == 2
    assert rejected == []


def test_empty_diff_yields_nothing(job):
    accepted, rejected = orch.validate_edits(job, {"edits": []})
    assert accepted == [] and rejected == []


def test_missing_edits_key_yields_nothing(job):
    accepted, rejected = orch.validate_edits(job, {})
    assert accepted == [] and rejected == []


# ---------------------------------------------------------------------------
# .docx construction
# ---------------------------------------------------------------------------

def _parse_document_xml(docx_bytes):
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as archive:
        names = archive.namelist()
        for required in (
            "[Content_Types].xml",
            "_rels/.rels",
            "word/document.xml",
            "word/styles.xml",
            "word/_rels/document.xml.rels",
        ):
            assert required in names, f"missing {required} in {names}"
        return etree.fromstring(archive.read("word/document.xml"))


def test_docx_is_valid_zip_with_required_parts(job):
    docx_bytes = orch.build_docx_bytes(job, [])
    _parse_document_xml(docx_bytes)  # asserts required parts present + well-formed XML


def test_docx_paragraph_count_and_style_mapping(job):
    docx_bytes = orch.build_docx_bytes(job, [])
    tree = _parse_document_xml(docx_bytes)
    ps = tree.findall(f".//{w('p')}")
    assert len(ps) == len(job["paragraphs"])
    style_vals = [p.find(f".//{w('pStyle')}").get(w("val")) for p in ps]
    assert style_vals == ["Title", "Normal", "Heading1", "Normal"]


def test_docx_paragraph_with_no_edits_is_a_plain_run(job):
    docx_bytes = orch.build_docx_bytes(job, [])
    tree = _parse_document_xml(docx_bytes)
    title_p = tree.findall(f".//{w('p')}")[0]
    assert title_p.findall(f".//{w('del')}") == []
    assert title_p.findall(f".//{w('ins')}") == []
    assert title_p.find(f".//{w('r')}/{w('t')}").text == "The Glass Orchard"


def test_docx_tracked_change_structure(job):
    edits = [{"para": 1, "start": 4, "end": 10, "old": "walked", "new": "drifted"}]
    when = "2026-06-07T00:00:00Z"
    docx_bytes = orch.build_docx_bytes(job, edits, author="Claude", when=when)
    tree = _parse_document_xml(docx_bytes)

    dels = tree.findall(f".//{w('del')}")
    inss = tree.findall(f".//{w('ins')}")
    assert len(dels) == 1 and len(inss) == 1

    deletion, insertion = dels[0], inss[0]
    assert deletion.find(f".//{w('delText')}").text == "walked"
    assert insertion.find(f".//{w('t')}").text == "drifted"
    assert deletion.get(w("author")) == "Claude"
    assert deletion.get(w("date")) == when
    assert insertion.get(w("author")) == "Claude"
    assert insertion.get(w("date")) == when
    assert deletion.get(w("id")) != insertion.get(w("id"))  # ids must be unique


def test_docx_preserves_surrounding_text(job):
    edits = [{"para": 1, "start": 4, "end": 10, "old": "walked", "new": "drifted"}]
    docx_bytes = orch.build_docx_bytes(job, edits, when="2026-06-07T00:00:00Z")
    tree = _parse_document_xml(docx_bytes)
    edited_p = tree.findall(f".//{w('p')}")[1]

    children = [c for c in edited_p if c.tag in (w("r"), w("del"), w("ins"))]
    assert [c.tag for c in children] == [w("r"), w("del"), w("ins"), w("r")]
    assert children[0].find(w("t")).text == "She "
    assert children[3].find(w("t")).text == " quickly into the room and sat down."


def test_docx_orders_multiple_edits_in_one_paragraph_by_position(job):
    # Deliberately out of source order — orchestrator must apply them in
    # left-to-right order regardless of input order.
    edits = [
        {"para": 1, "start": 11, "end": 18, "old": "quickly", "new": "softly"},
        {"para": 1, "start": 4, "end": 10, "old": "walked", "new": "drifted"},
    ]
    docx_bytes = orch.build_docx_bytes(job, edits, when="2026-06-07T00:00:00Z")
    tree = _parse_document_xml(docx_bytes)
    edited_p = tree.findall(f".//{w('p')}")[1]

    dels = edited_p.findall(w("del"))
    inss = edited_p.findall(w("ins"))
    assert [d.find(f".//{w('delText')}").text for d in dels] == ["walked", "quickly"]
    assert [i.find(f".//{w('t')}").text for i in inss] == ["drifted", "softly"]


def test_docx_change_ids_unique_across_document(job):
    edits = [
        {"para": 0, "start": 4, "end": 9, "old": "Glass", "new": "Stone"},
        {"para": 1, "start": 4, "end": 10, "old": "walked", "new": "drifted"},
    ]
    docx_bytes = orch.build_docx_bytes(job, edits, when="2026-06-07T00:00:00Z")
    tree = _parse_document_xml(docx_bytes)
    ids = [el.get(w("id")) for el in tree.findall(f".//{w('del')}") + tree.findall(f".//{w('ins')}")]
    assert len(ids) == len(set(ids))


def test_docx_preserves_empty_paragraphs(job):
    docx_bytes = orch.build_docx_bytes(job, [])
    tree = _parse_document_xml(docx_bytes)
    empty_p = tree.findall(f".//{w('p')}")[3]
    run_text_el = empty_p.find(f".//{w('r')}/{w('t')}")
    assert run_text_el is not None
    assert run_text_el.text in (None, "")


def test_docx_default_timestamp_is_iso8601_utc(job):
    docx_bytes = orch.build_docx_bytes(job, [{"para": 0, "start": 0, "end": 3, "old": "The", "new": "A"}])
    tree = _parse_document_xml(docx_bytes)
    date = tree.find(f".//{w('del')}").get(w("date"))
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", date)


def test_docx_opens_in_python_docx(job, tmp_path):
    from docx import Document

    edits = [{"para": 1, "start": 4, "end": 10, "old": "walked", "new": "drifted"}]
    docx_bytes = orch.build_docx_bytes(job, edits, when="2026-06-07T00:00:00Z")
    path = tmp_path / "out.docx"
    path.write_bytes(docx_bytes)

    doc = Document(str(path))
    texts = [p.text for p in doc.paragraphs]
    assert texts[0] == "The Glass Orchard"
    assert texts[2] == "Chapter One"
    # python-docx's plain `.text` walks only direct <w:r> children of <w:p>, so
    # it skips <w:ins>/<w:del> entirely — neither "walked" nor "drifted" show.
    # That the file opens and surrounding text round-trips is the structural
    # OOXML sanity check; the lxml tests above cover the tracked-change content.
    assert "walked" not in texts[1] and "drifted" not in texts[1]
    assert texts[1].startswith("She ") and texts[1].endswith("quickly into the room and sat down.")
