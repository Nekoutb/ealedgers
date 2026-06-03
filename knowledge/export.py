"""Rule-review export (Step 27 enablement).

Produces a reviewer-friendly CSV of the encoded rules for an accounting /
tax expert to sign off — the gate that flips rules from ``needs_review`` to
``verified``. Each row carries the citation + verbatim source so the expert
can check our encoding against the authoritative text, plus blank
``Verdict`` / ``Reviewer notes`` columns to fill in.

CSV is semicolon-delimited with a UTF-8 BOM so it opens cleanly (accents and
columns intact) in a French Excel — the reviewer's likely tool.
"""

import csv
import io
import json


HEADER = [
    "Framework", "Slice", "Slug", "Title", "Citation",
    "Source text (verbatim)", "Encoded effect", "Current status",
    "Verdict (OK / Change)", "Reviewer notes",
]


def build_rules_review_csv(rules):
    """Return the review CSV (with BOM) as a string for an iterable of
    Rule instances."""
    buf = io.StringIO()
    buf.write("﻿")  # BOM → Excel reads UTF-8 + accents correctly
    writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    writer.writerow(HEADER)
    for r in rules:
        writer.writerow([
            r.framework,
            r.knowledge_slice,
            r.slug,
            r.title,
            r.source_ref,
            r.source_text,
            json.dumps(r.effects, ensure_ascii=False),
            r.get_review_status_display(),
            "",  # Verdict — reviewer fills
            "",  # Reviewer notes — reviewer fills
        ])
    return buf.getvalue()
