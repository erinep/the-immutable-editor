---
name: google-docs-dispatch
description: Run a complete editing pass on a live Google Doc — confirm the document, let the user choose what kind of pass to run, fetch it, apply the chosen judgment, and upload the results as tracked-change suggestions, end to end via the extract_doc and build_and_upload MCP tools. Use when asked to edit, review, proofread, or run any kind of pass on a Google Doc, given a doc ID or a docs.google.com link.
---

# Google Docs Dispatch

Runs one complete editing pass on a live Google Doc: confirm which doc and
which kind of edit, fetch it, apply that judgment, upload the results as
tracked-change suggestions. There's no `job.json`/`diff.json` to read or
write, no offsets to compute, and no orchestrator CLI to run — the
`extract_doc` and `build_and_upload` MCP tools own all of that. Your job is
the two things that need judgment: picking the right kind of pass, and
applying it well.

## Steps

0. **Confirm the doc.** This pass needs a specific Google Doc. If you
   weren't given one — no ID and no `docs.google.com/document/d/...` link
   anywhere in the conversation — stop and ask the user for one rather than
   guessing at an ID or reusing one from a stale `job.json` on disk.

1. **Ask which kind of edit, before fetching anything.** Asking first
   means the user can step away while you fetch and review, instead of a
   short wait, a question, then a second short wait.

   Look at the other skills available in this project named `check-[domain]`
   (e.g. `check-grammar`) — each one is a self-contained judgment module
   for one kind of pass — and offer them by what they actually judge (read
   each one's description), not by file name. Don't hardcode a list here:
   the available `check-[domain]` skills are exactly the menu, and asking
   you to keep this list in sync with that directory is how the two would
   silently drift apart.

   **Exactly one kind per round.** If the user wants more than one, that's
   two separate passes, each with its own fetch → check → build/upload
   cycle — running two judgments over a single extraction risks their
   edits landing on overlapping spans, where the second would be silently
   rejected at upload time.

2. **Fetch.** Call `extract_doc` with the doc's ID — the long segment in
   `docs.google.com/document/d/<ID>/edit`. It returns the paragraph list
   directly in its result; read straight from that response, no separate
   file access needed (it also writes `job.json` to disk as a side effect,
   but you don't need to touch it).

3. **Apply the chosen judgment.** Read the **Scope** section of the
   matching `check-[domain]` skill — e.g. `.claude/skills/check-grammar/SKILL.md`
   for a grammar pass — and apply *exactly* that judgment to every
   paragraph's `text`. That file is the single source of truth for what's
   in scope and what to leave alone; don't restate, extend, or second-guess
   it here — if the judgment needs to change, it changes there, once, for
   every flow that uses it.

   For each real issue, note the three things you actually know:

   - `para` — the paragraph's `index`
   - `old` — the exact erroneous text, copied character-for-character,
     trimmed to the smallest span that still pins down the spot
   - `new` — the corrected replacement text

   If `old` genuinely occurs more than once in that paragraph and you mean
   a specific one, also note `occurrence` (0-based index, in reading order).

4. **Build and upload — in one call.** Call `build_and_upload` with the
   doc's ID and your list of `{para, old, new[, occurrence]}` edits, as-is
   — don't compute `start`/`end` yourself. The tool locates each `old` in
   the source by exact string search (precise, mechanical work — exactly
   the kind of thing that's slow and error-prone to do by hand-counting
   characters), validates it, builds the `.docx` with tracked changes, and
   uploads it, replacing the original in place.

   - On success it returns `{accepted, rejected, rejected_edits, doc_url}`.
   - If any edit's `old` couldn't be placed (not found, ambiguous,
     overlapping another edit), it returns `{error, problems}` instead —
     naming exactly which entries to fix — and *nothing* is built or
     uploaded. Fix those entries and call it again; don't work around it by
     computing an offset yourself.
   - If the call itself doesn't complete — denied, cancelled, or
     interrupted before any response comes back — don't sit on that
     silently or ask a vague "should I go ahead?". Say plainly what you
     were about to send (how many edits, to which doc) and what you
     actually observed, then ask one concrete question: *"Want me to
     retry those same N edits now, or has something changed?"* You're the
     only one who saw what happened at the tool-call level — the user is
     relying on you to characterize it accurately, not to hand the
     uncertainty back to them.

5. **Report.** Tell the user the doc URL and the accepted/rejected counts.
   They review and accept or reject each suggestion in Google Docs — that
   review step, backed by tracked changes and Google Docs' own revision
   history, is the safety net for this whole pipeline. There's no
   pre-upload checkpoint here by design; trust it.
