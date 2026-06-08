---
name: grammar-check
description: Check a manuscript for grammar, spelling, and malapropisms and write the corrections to diff.json in the immutable-editor's diff schema. Use when asked to grammar-check, proofread, or run the editing pass on job.json.
---

# Grammar Check

Find grammar, spelling, and malapropism errors in `job.json`'s manuscript and
write them to `diff.json`.

You supply *what's wrong* and *what it should say* — `para`, `old`, `new`. A
bundled script, `build_diff.py`, supplies *where*: it reads your draft, locates
each `old` in the source paragraph by exact string search, and fills in
`start`/`end` — overwriting `diff.json` with the complete schema in place.
Don't compute offsets yourself — finding a substring's position is exact,
mechanical work that a script does perfectly and for free, where hand-counting
characters is slow and the kind of thing LLMs get subtly wrong (a wrong offset
is a silently rejected edit and a wasted round trip).

## Scope

Flag only:

- **Grammar** — subject/verb agreement, tense, punctuation, sentence structure
- **Spelling** — misspelled words, typos
- **Malapropisms** — a word that sounds like the intended one but means
  something else (e.g. "for all intensive purposes", "could care less")

**Leave intentional authorial choices alone.** Fiction routinely contains
"errors" that are deliberate: dialect and accent spellings, fragmented or
non-standard dialogue ("ain't", "gonna", "y'know"), invented names and
terminology (especially in genre fiction), and stylistic fragments used for
effect. When in doubt, don't propose an edit — this pass should catch the
writer's mistakes, not overwrite their voice.

Do not propose edits for prose quality, pacing, clarity, tone, or style —
those belong to the human reviewer's judgment (see `README.md`, "What the
Human Reviewer Is Responsible For").

## Steps

1. **Read `job.json`** and review each paragraph's `text` for issues.

2. **Clear out any stale `diff.json` first:** `rm -f diff.json`. It's a
   regenerated pipeline artifact (see `.gitignore`), not something to
   preserve — and removing it up front means the `Write` in the next step
   always lands on a clean file instead of erroring because a leftover from
   a previous run exists on disk and hasn't been read into this session.

3. **For each issue, write one draft entry** directly to `diff.json` — no
   `start`/`end` yet, just the three things you actually know:

   ```json
   { "para": 3, "old": "an bird", "new": "a bird" }
   ```

   - `para` — the paragraph's `index`
   - `old` — the exact erroneous text, copied character-for-character,
     trimmed to the smallest span that still pins down the spot (usually
     just the wrong word or phrase — not the whole sentence)
   - `new` — the corrected replacement text

   If `old` genuinely occurs more than once in that paragraph and you mean a
   specific one, add `"occurrence": <0-based index, in reading order>`.

   Write `{"edits": [...]}` to `diff.json` — `{"edits": []}` if you found
   nothing. Same shape either way; the next step handles both.

4. **Run the offset script** to resolve the draft into the final form:

   ```
   python3 .claude/skills/grammar-check/build_diff.py job.json diff.json
   ```

   It reads your draft, locates each `old` by exact search, fills in
   `start`/`end`, checks for overlaps, and **overwrites `diff.json` in place**
   with the complete schema — one file, no scratch artifact to track or clean
   up.

   If it exits with a problem instead (text not found, ambiguous repeat,
   overlap), `diff.json` is left untouched and the message tells you which
   entry and why — fix that entry (e.g. widen `old` for uniqueness, or add
   `occurrence`) and rerun. Don't compute the offset by hand to work around it.
