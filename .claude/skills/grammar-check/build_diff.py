#!/usr/bin/env python3
"""Resolve diff.json's start/end offsets in place, by exact string search.

Claude writes diff.json in draft form — {para, old, new[, occurrence]} per
edit, i.e. *what's wrong* and *what it should say*. Locating *where* that text
sits in the source is exact, mechanical string search: a job for code, not for
an LLM counting characters. This script reads that draft, computes start/end
for each entry, checks for overlaps, and overwrites diff.json with the
complete schema the orchestrator expects. On any problem it leaves the file
untouched and reports exactly what to fix, so Claude can adjust and rerun
rather than guess at an offset.
"""
import json
import sys
from collections import defaultdict


def find_occurrences(text, needle):
    starts = []
    idx = text.find(needle)
    while idx != -1:
        starts.append(idx)
        idx = text.find(needle, idx + 1)
    return starts


def main(job_path, diff_path):
    job = json.load(open(job_path, encoding="utf-8"))
    paragraphs = job["paragraphs"]

    try:
        draft = json.load(open(diff_path, encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"{diff_path} is not valid JSON: {e}")

    placed = defaultdict(list)  # para index -> [(start, end), ...]
    result = []
    problems = []

    for i, edit in enumerate(draft.get("edits", [])):
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
        starts = find_occurrences(text, old)

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
                f"occurrence(s) of {old!r}, but occurrence={occurrence} "
                f"was requested"
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
        result.append({"para": para, "start": start, "end": end, "old": old, "new": new})

    if problems:
        print("\n".join(problems), file=sys.stderr)
        sys.exit(1)

    with open(diff_path, "w", encoding="utf-8") as f:
        json.dump({"edits": result}, f, indent=2, ensure_ascii=False)
    print(f"Resolved offsets for {len(result)} edit(s) in {diff_path}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
