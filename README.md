# Fiction Editor — Design Contract

## Purpose

This document defines the architecture, separation of concerns, and development phases for an AI-powered fiction editing pipeline. It is the source of truth for how components interact and what each component is responsible for.

---

## Architecture Overview

The system has two distinct layers that never overlap:

**Orchestrator** — a deterministic Python script. Owns all I/O: Google API calls, file operations, checksum logic, diff validation, and .docx construction. Contains no intelligence. Always produces the same output for the same input.

**Claude Code session** — the intelligence layer. Reads document content from a file, reasons about fiction editing, and writes a structured diff to a file. Has no awareness of Google APIs, checksums, or .docx internals.

The handoff between layers is a pair of JSON files on disk. This is the only interface between them.

---

## Separation of Concerns

### Orchestrator owns

- Authenticating with Google APIs (OAuth2)
- Calling the Docs API to extract document content
- Serialising extracted content to `job.json`
- Validating every `old` field in the diff against the source
- Rejecting any edit whose `old` field does not match exactly
- Constructing the `.docx` with `<w:ins>`/`<w:del>` OOXML tracked changes
- Uploading the `.docx` to Drive, replacing the original file in place

### Claude Code session owns

- Reading `job.json`
- Reasoning about prose quality, pacing, clarity, and consistency
- Producing `diff.json` conforming to the diff schema
- Ensuring every `old` field is copied character-for-character from the source

### Neither layer owns

- Validating whether Claude's `new` text is good — that is the human reviewer's job in Google Docs suggestion mode

---

## File Schema

### job.json

Produced by the orchestrator, consumed by Claude.

```json
{
  "doc_id": "string — Google Doc file ID",
  "plain_text": "string — full document text, paragraphs joined by newline",
  "paragraphs": [
    {
      "index": 0,
      "style": "Normal | Heading 1 | Heading 2 | ...",
      "text": "string — exact paragraph text"
    }
  ]
}
```

### diff.json

Produced by Claude, consumed by the orchestrator.

```json
{
  "edits": [
    {
      "para": 12,
      "start": 4,
      "end": 17,
      "old": "string — exact characters from source at [start:end]",
      "new": "string — replacement text"
    }
  ]
}
```

`start` and `end` are character offsets within the paragraph text. `old` must equal `paragraphs[para].text[start:end]` exactly, or the edit is rejected.

---

## Validation Rules

Enforced by the orchestrator at build time, in this order:

1. For each edit, slice `paragraphs[para].text[start:end]` and compare to `old`. If they do not match character-for-character, reject that edit and log it. Continue processing remaining edits.
2. Build the `.docx` only from edits that passed validation.

---

## Phase A — Manual Handoff

The two layers are run separately by the developer. No integration code required.

**Step 1 — Extract**

Run the orchestrator's extract command, passing a Google Doc ID:

```bash
python orchestrator.py extract --doc-id <DOC_ID> --out job.json
```

This authenticates, calls the Docs API, builds the paragraph list, and writes `job.json`.

**Step 2 — Edit**

Open a Claude Code session in the project directory. Provide a task prompt along these lines:

```
Read job.json. Edit the manuscript for grammar, splling and malapropism
Output your edits to diff.json following the schema in DESIGN-CONTRACT.md.
The `old` field in every edit must be copied character-for-character from the source.
```

Claude reads `job.json`, produces `diff.json`.

**Step 3 — Build and upload**

Run the orchestrator's build command:

```bash
python orchestrator.py build --job job.json --diff diff.json
```

This validates each `old` field, constructs the `.docx` with tracked changes, and uploads it to Drive using the `doc_id` from `job.json`.

**Step 4 — Review**

Open the Google Doc. All of Claude's edits appear as suggestions. Accept or reject each one.

---

## Phase B — MCP Tools

The orchestrator logic is wrapped in an MCP server, exposing two tools that Claude Code can call directly during its session. The developer runs a single Claude Code session; no manual steps.

### Tools

**extract_doc**

```
Input:  { doc_id: string }
Output: { job: <job.json contents> }
Side effect: writes job.json to disk
```

Performs the same logic as `orchestrator.py extract`. Returns the job payload so Claude can begin editing immediately without a separate read step.

**build_and_upload**

```
Input:  { doc_id: string, diff: <diff.json contents> }
Output: { accepted: number, rejected: number, doc_url: string }
Side effect: builds .docx, uploads to Drive, logs rejected edits
```

Performs `old` field validation, .docx construction, and Drive upload. Returns a summary of accepted and rejected edits.

### Session flow

Claude Code calls `extract_doc`, receives the document, reasons about edits, then calls `build_and_upload` with the diff. The human opens the doc and reviews suggestions. Claude never calls any Google API directly — it only calls the two MCP tools.

### What does not change in Phase B

- The JSON schemas for job and diff are identical
- The validation logic in the orchestrator is identical
- The `.docx` construction is identical
- The human review step is identical

Phase B is purely a convenience wrapper around Phase A. The correctness guarantees are the same.

---

## What the Human Reviewer Is Responsible For

The pipeline makes no attempt to validate whether Claude's suggested text is an improvement. That judgment is entirely the reviewer's. The pipeline only guarantees:

- Every tracked change in the `.docx` is anchored to the correct span of original text, because the `old` field is checked character-for-character against `paragraphs[para].text[start:end]` before any edit is applied
- No original text was silently modified without a corresponding tracked change entry

Note: the pipeline does not detect whether the live Google Doc changed between `extract` and `build` — `job.json` is a frozen snapshot, and nothing re-fetches the document to compare. If a human edits the doc in that window, `build` will still validate and upload against the stale snapshot, overwriting their changes. An earlier draft of this contract proposed a stored-checksum re-check to guard against this, but a checksum recomputed from a field stored in the same file it's compared against can only ever detect corruption of `job.json` itself — it cannot detect drift in the live source. Catching that would require re-fetching the document at build time and comparing a freshly computed digest, which this pipeline does not do.