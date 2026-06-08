#!/usr/bin/env python3
"""Deterministic orchestrator for the fiction-editing pipeline.

Owns all I/O described in DESIGN-CONTRACT.md / README.md: Google API calls,
diff validation, and .docx construction. Contains no intelligence — it
always produces the same output for the same input.
"""

import argparse
import io
import itertools
import json
import sys
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from lxml import etree

CREDENTIALS_PATH = Path("credentials.json")
TOKEN_PATH = Path("token.json")

SCOPES = [
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/drive",
]

DOCX_MIMETYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
GOOGLE_DOC_MIMETYPE = "application/vnd.google-apps.document"

CHANGE_AUTHOR = "Claude"

# Google Docs `namedStyleType` -> the human-readable style name documented in
# the job.json schema. This mapping is also used in reverse (see
# STYLE_NAME_TO_ID) when rebuilding the .docx, so job.json's `style` field is
# the single source of truth for paragraph styling end to end.
STYLE_MAP = {
    "NORMAL_TEXT": "Normal",
    "TITLE": "Title",
    "SUBTITLE": "Subtitle",
    "HEADING_1": "Heading 1",
    "HEADING_2": "Heading 2",
    "HEADING_3": "Heading 3",
    "HEADING_4": "Heading 4",
    "HEADING_5": "Heading 5",
    "HEADING_6": "Heading 6",
}

STYLE_NAME_TO_ID = {
    "Normal": "Normal",
    "Title": "Title",
    "Subtitle": "Subtitle",
    "Heading 1": "Heading1",
    "Heading 2": "Heading2",
    "Heading 3": "Heading3",
    "Heading 4": "Heading4",
    "Heading 5": "Heading5",
    "Heading 6": "Heading6",
}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def authenticate():
    """Standard installed-app OAuth2 flow, caching the token on disk."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_PATH.exists():
                sys.exit(
                    f"Missing {CREDENTIALS_PATH}. Create an OAuth client ID "
                    "(Desktop app) in Google Cloud Console, enable the Docs "
                    "and Drive APIs, and download the client secret to "
                    f"{CREDENTIALS_PATH}."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_PATH), SCOPES
            )
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json())

    return creds


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------

def paragraphs_from_document(document):
    """Walk the Docs API body content and build the documented paragraph list."""
    paragraphs = []
    for element in document.get("body", {}).get("content", []):
        paragraph = element.get("paragraph")
        if paragraph is None:
            continue

        text = "".join(
            run.get("textRun", {}).get("content", "")
            for run in paragraph.get("elements", [])
        )
        # The Docs API includes the paragraph's terminating newline in the
        # last text run; job.json joins paragraphs with "\n" itself.
        text = text[:-1] if text.endswith("\n") else text

        named_style = paragraph.get("paragraphStyle", {}).get(
            "namedStyleType", "NORMAL_TEXT"
        )
        style = STYLE_MAP.get(named_style, named_style)

        paragraphs.append({"index": len(paragraphs), "style": style, "text": text})

    return paragraphs


def build_job(doc_id, document):
    paragraphs = paragraphs_from_document(document)
    return {
        "doc_id": doc_id,
        "plain_text": "\n".join(p["text"] for p in paragraphs),
        "paragraphs": paragraphs,
    }


def cmd_extract(args):
    from googleapiclient.discovery import build as build_service

    creds = authenticate()
    docs = build_service("docs", "v1", credentials=creds)
    document = docs.documents().get(documentId=args.doc_id).execute()

    job = build_job(args.doc_id, document)

    out_path = Path(args.out)
    out_path.write_text(json.dumps(job, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Wrote {out_path} — {len(job['paragraphs'])} paragraphs")


# ---------------------------------------------------------------------------
# diff validation
# ---------------------------------------------------------------------------

def validate_edits(job, diff):
    """Validate every edit's `old` field against the source.

    Returns (accepted, rejected) where each rejected entry is the original
    edit dict plus a `reason` key. Edits are validated and, within a
    paragraph, must not overlap a previously accepted edit — overlapping
    spans would make the tracked-change mapping ambiguous and break the
    "no original text silently modified without a tracked-change entry"
    guarantee.
    """
    paragraphs = job["paragraphs"]
    accepted = []
    rejected = []
    accepted_spans = defaultdict(list)  # para index -> [(start, end), ...]

    for edit in diff.get("edits", []):
        para_idx = edit.get("para")
        start = edit.get("start")
        end = edit.get("end")
        old = edit.get("old")

        reason = None
        if para_idx is None or not (0 <= para_idx < len(paragraphs)):
            reason = f"paragraph index {para_idx!r} out of range"
        else:
            actual = paragraphs[para_idx]["text"][start:end]
            if actual != old:
                reason = f"old field does not match source: expected {actual!r}, got {old!r}"
            elif any(start < e and s < end for s, e in accepted_spans[para_idx]):
                reason = f"edit span [{start}:{end}] overlaps another accepted edit in paragraph {para_idx}"

        if reason:
            rejected.append({**edit, "reason": reason})
        else:
            accepted_spans[para_idx].append((start, end))
            accepted.append(edit)

    return accepted, rejected


# ---------------------------------------------------------------------------
# .docx construction — hand-built OOXML with <w:ins>/<w:del> tracked changes
# ---------------------------------------------------------------------------

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"
NSMAP = {"w": W_NS}


def w(tag):
    return f"{{{W_NS}}}{tag}"


def xml_space_preserve(element):
    element.set(f"{{{XML_NS}}}space", "preserve")


# The Docs API encodes a soft line break (Shift+Enter within a paragraph) as
# U+000B (vertical tab) in textRun content. XML 1.0 forbids that control
# character outright, so it can't be written into <w:t>/<w:delText> as-is —
# it must become an explicit <w:br/> between text segments instead.
SOFT_LINE_BREAK = "\x0b"


def add_text_with_breaks(run, text, text_tag):
    segments = text.split(SOFT_LINE_BREAK)
    for i, segment in enumerate(segments):
        if i > 0:
            etree.SubElement(run, w("br"))
        if segment or len(segments) == 1:
            t = etree.SubElement(run, text_tag)
            xml_space_preserve(t)
            t.text = segment


def add_run(parent, text):
    run = etree.SubElement(parent, w("r"))
    add_text_with_breaks(run, text, w("t"))
    return run


def add_deletion(parent, text, change_id, author, when):
    d = etree.SubElement(parent, w("del"))
    d.set(w("id"), str(change_id))
    d.set(w("author"), author)
    d.set(w("date"), when)
    run = etree.SubElement(d, w("r"))
    add_text_with_breaks(run, text, w("delText"))
    return d


def add_insertion(parent, text, change_id, author, when):
    ins = etree.SubElement(parent, w("ins"))
    ins.set(w("id"), str(change_id))
    ins.set(w("author"), author)
    ins.set(w("date"), when)
    run = etree.SubElement(ins, w("r"))
    add_text_with_breaks(run, text, w("t"))
    return ins


def style_id_for(style_name):
    return STYLE_NAME_TO_ID.get(style_name, "Normal")


def build_document_xml(paragraphs, edits_by_para, author, when):
    document = etree.Element(w("document"), nsmap=NSMAP)
    body = etree.SubElement(document, w("body"))
    change_id = itertools.count(1)

    for para in paragraphs:
        p = etree.SubElement(body, w("p"))
        p_pr = etree.SubElement(p, w("pPr"))
        etree.SubElement(p_pr, w("pStyle")).set(w("val"), style_id_for(para["style"]))

        text = para["text"]
        edits = sorted(edits_by_para.get(para["index"], []), key=lambda e: e["start"])

        cursor = 0
        for edit in edits:
            if edit["start"] > cursor:
                add_run(p, text[cursor:edit["start"]])
            add_deletion(p, edit["old"], next(change_id), author, when)
            add_insertion(p, edit["new"], next(change_id), author, when)
            cursor = edit["end"]

        if cursor < len(text):
            add_run(p, text[cursor:])
        elif not text and not edits:
            # Preserve empty paragraphs — an empty <w:p> with no runs collapses.
            add_run(p, "")

    sect_pr = etree.SubElement(body, w("sectPr"))
    page_size = etree.SubElement(sect_pr, w("pgSz"))
    page_size.set(w("w"), "12240")
    page_size.set(w("h"), "15840")
    page_margin = etree.SubElement(sect_pr, w("pgMar"))
    for attr, value in (
        ("top", "1440"), ("right", "1440"), ("bottom", "1440"), ("left", "1440"),
        ("header", "720"), ("footer", "720"), ("gutter", "0"),
    ):
        page_margin.set(w(attr), value)

    return document


CONTENT_TYPES_XML = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>
"""

PACKAGE_RELS_XML = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""

DOCUMENT_RELS_XML = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>
"""

# Minimal static style definitions so the <w:pStyle> references in
# document.xml resolve to something with the documented display name.
_HEADING_SIZES = {1: "32", 2: "28", 3: "26", 4: "24", 5: "22", 6: "22"}
_HEADING_STYLES = "\n".join(
    f'  <w:style w:type="paragraph" w:styleId="Heading{n}">\n'
    f'    <w:name w:val="Heading {n}"/>\n'
    f'    <w:basedOn w:val="Normal"/>\n'
    f'    <w:rPr><w:b/><w:sz w:val="{size}"/></w:rPr>\n'
    f'  </w:style>'
    for n, size in _HEADING_SIZES.items()
)

STYLES_XML = (
    """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults>
    <w:rPrDefault><w:rPr><w:sz w:val="22"/></w:rPr></w:rPrDefault>
  </w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Title">
    <w:name w:val="Title"/>
    <w:basedOn w:val="Normal"/>
    <w:rPr><w:b/><w:sz w:val="56"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Subtitle">
    <w:name w:val="Subtitle"/>
    <w:basedOn w:val="Normal"/>
    <w:rPr><w:i/><w:sz w:val="32"/></w:rPr>
  </w:style>
"""
    + _HEADING_STYLES
    + "\n</w:styles>\n"
).encode("utf-8")


def package_docx(document_xml_bytes):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES_XML)
        archive.writestr("_rels/.rels", PACKAGE_RELS_XML)
        archive.writestr("word/_rels/document.xml.rels", DOCUMENT_RELS_XML)
        archive.writestr("word/document.xml", document_xml_bytes)
        archive.writestr("word/styles.xml", STYLES_XML)
    return buffer.getvalue()


def build_docx_bytes(job, accepted_edits, author=CHANGE_AUTHOR, when=None):
    when = when or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    edits_by_para = defaultdict(list)
    for edit in accepted_edits:
        edits_by_para[edit["para"]].append(edit)

    document = build_document_xml(job["paragraphs"], edits_by_para, author, when)
    document_xml = etree.tostring(
        document, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    return package_docx(document_xml)


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

def cmd_build(args):
    from googleapiclient.discovery import build as build_service
    from googleapiclient.http import MediaFileUpload

    job = json.loads(Path(args.job).read_text(encoding="utf-8"))
    diff = json.loads(Path(args.diff).read_text(encoding="utf-8"))

    # 1. Validate every edit's `old` field against the source.
    accepted, rejected = validate_edits(job, diff)
    for edit in rejected:
        print(
            f"REJECTED edit (para {edit.get('para')}, "
            f"{edit.get('start')}:{edit.get('end')}): {edit['reason']}",
            file=sys.stderr,
        )

    # 2. Build the .docx from validated edits only.
    docx_bytes = build_docx_bytes(job, accepted)
    out_path = Path(args.out) if args.out else Path(f"{job['doc_id']}.docx")
    out_path.write_bytes(docx_bytes)
    print(f"Wrote {out_path} ({len(accepted)} accepted, {len(rejected)} rejected)")

    if args.no_upload:
        return

    # 3. Upload to Drive, replacing the original file in place.
    creds = authenticate()
    drive = build_service("drive", "v3", credentials=creds)
    media = MediaFileUpload(str(out_path), mimetype=DOCX_MIMETYPE, resumable=True)
    updated = (
        drive.files()
        .update(
            fileId=job["doc_id"],
            body={"mimeType": GOOGLE_DOC_MIMETYPE},
            media_body=media,
            fields="id, webViewLink",
        )
        .execute()
    )

    print(f"Uploaded — {updated.get('webViewLink', updated['id'])}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="Fiction-editing pipeline orchestrator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract_parser = subparsers.add_parser("extract", help="Extract a Google Doc into job.json")
    extract_parser.add_argument("--doc-id", required=True, help="Google Doc file ID")
    extract_parser.add_argument("--out", default="job.json", help="Output path for job.json")
    extract_parser.set_defaults(func=cmd_extract)

    build_parser = subparsers.add_parser(
        "build", help="Validate diff.json, build the .docx, and upload it"
    )
    build_parser.add_argument("--job", default="job.json", help="Path to job.json")
    build_parser.add_argument("--diff", default="diff.json", help="Path to diff.json")
    build_parser.add_argument(
        "--out", default=None, help="Output path for the .docx (default: <doc_id>.docx)"
    )
    build_parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Build the .docx locally without uploading to Drive (useful for review/testing)",
    )
    build_parser.set_defaults(func=cmd_build)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
