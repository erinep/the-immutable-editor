#!/usr/bin/env python3
"""MCP server wrapping orchestrator.py — Phase B of the design contract.

Exposes `extract_doc` and `build_and_upload` as MCP tools so a Claude Code
session can run a whole Google Docs suggestion pass in-process — no
shelling out to the CLI, no job.json/diff.json round trip by hand. The
underlying logic — authentication, extraction, validation, .docx
construction, and upload — is unchanged from orchestrator.py; this wraps
and extends it per README.md's Phase B section.
"""
import json
import tempfile
from collections import defaultdict
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import orchestrator as orch

mcp = FastMCP("immutable-editor")

JOB_PATH = Path("job.json")


@mcp.tool()
def extract_doc(doc_id: str) -> dict:
    """Extract a Google Doc into job.json and return its contents.

    Authenticates, fetches the document via the Docs API, builds the
    paragraph list, and writes job.json to disk — then returns the job
    payload directly so editing can begin without a separate read.
    """
    from googleapiclient.discovery import build as build_service

    creds = orch.authenticate()
    docs = build_service("docs", "v1", credentials=creds)
    document = docs.documents().get(documentId=doc_id).execute()

    job = orch.build_job(doc_id, document)
    JOB_PATH.write_text(json.dumps(job, indent=2, ensure_ascii=False), encoding="utf-8")

    return {"job": job}


def _resolve_offsets(paragraphs, draft_edits):
    """Place draft {para, old, new[, occurrence]} edits by exact string search.

    Finding a substring's position is exact, mechanical work, so the tool
    does it rather than asking the model to count characters (slow, and the
    kind of thing LLMs get subtly wrong — a wrong offset is a silently
    rejected edit).
    Returns (resolved, problems); on any problem `resolved` is None and
    `problems` names exactly which entries to fix.
    """
    placed = defaultdict(list)
    resolved = []
    problems = []

    for i, edit in enumerate(draft_edits):
        missing = [k for k in ("para", "old", "new") if k not in edit]
        if missing:
            problems.append(f"edit {i}: missing required field(s): {', '.join(missing)}")
            continue

        para, old, new = edit["para"], edit["old"], edit["new"]
        if not isinstance(para, int) or not (0 <= para < len(paragraphs)):
            problems.append(
                f"edit {i}: paragraph index {para!r} is out of range "
                f"(must be 0..{len(paragraphs) - 1})"
            )
            continue

        text = paragraphs[para]["text"]
        starts = []
        idx = text.find(old)
        while idx != -1:
            starts.append(idx)
            idx = text.find(old, idx + 1)

        if not starts:
            problems.append(f"edit {i}: {old!r} not found in paragraph {para}")
            continue
        if len(starts) > 1 and "occurrence" not in edit:
            problems.append(
                f"edit {i}: {old!r} appears {len(starts)} times in paragraph "
                f"{para} — add \"occurrence\": <0-based index> to pick one"
            )
            continue

        occurrence = edit.get("occurrence", 0)
        if occurrence >= len(starts):
            problems.append(
                f"edit {i}: paragraph {para} has only {len(starts)} "
                f"occurrence(s) of {old!r}, but occurrence={occurrence} was requested"
            )
            continue

        start = starts[occurrence]
        end = start + len(old)
        if any(start < pe and ps < end for ps, pe in placed[para]):
            problems.append(
                f"edit {i}: [{start}:{end}] overlaps another edit "
                f"already placed in paragraph {para}"
            )
            continue

        placed[para].append((start, end))
        resolved.append({"para": para, "start": start, "end": end, "old": old, "new": new})

    if problems:
        return None, problems
    return resolved, []


@mcp.tool()
def build_and_upload(doc_id: str, edits: list[dict]) -> dict:
    """Place, validate, build, and upload a suggestion pass in one call.

    Takes draft edits in the form you actually reason in — `para`, `old`,
    `new`, and optionally `occurrence` for repeated text — and does the
    rest: locates each `old` in job.json by exact string search, validates
    it against the source, builds the .docx with <w:ins>/<w:del> tracked
    changes from the edits that pass, and uploads it to Drive, replacing
    the original file in place.

    Returns `{accepted, rejected, rejected_edits, doc_url}` on success. If
    any edit's `old` can't be placed (not found, ambiguous, overlapping),
    returns `{error, problems}` instead — naming exactly which entries to
    fix (e.g. widen `old` for uniqueness, or add `occurrence`) — and
    nothing is built or uploaded. Don't compute start/end yourself; this
    placement step is exact, mechanical work the tool does for free.
    """
    from googleapiclient.discovery import build as build_service
    from googleapiclient.http import MediaFileUpload

    job = json.loads(JOB_PATH.read_text(encoding="utf-8"))

    resolved, problems = _resolve_offsets(job["paragraphs"], edits)
    if problems:
        return {"error": "could not place every edit — fix and retry", "problems": problems}

    accepted, rejected = orch.validate_edits(job, {"edits": resolved})
    docx_bytes = orch.build_docx_bytes(job, accepted)

    with tempfile.NamedTemporaryFile(suffix=".docx") as tmp:
        tmp.write(docx_bytes)
        tmp.flush()

        creds = orch.authenticate()
        drive = build_service("drive", "v3", credentials=creds)
        media = MediaFileUpload(tmp.name, mimetype=orch.DOCX_MIMETYPE, resumable=True)
        updated = (
            drive.files()
            .update(
                fileId=doc_id,
                body={"mimeType": orch.GOOGLE_DOC_MIMETYPE},
                media_body=media,
                fields="id, webViewLink",
            )
            .execute()
        )

    return {
        "accepted": len(accepted),
        "rejected": len(rejected),
        "rejected_edits": rejected,
        "doc_url": updated.get("webViewLink", updated["id"]),
    }


if __name__ == "__main__":
    mcp.run()
