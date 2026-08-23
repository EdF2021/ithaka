# src/notebook_mindmap.py
"""Mindmap artifact helpers: generation-time format validation.

The mindmap generation prompt (src/notebook_artifacts.py) asks for exactly
one ```mermaid fence whose first line is "mindmap", a root((Topic)) node and
4-8 indented main branches. Two 2026-08-20 production artifacts showed a
model ignoring all of that and answering with free prose — which the preview
then displayed as unrendered markdown. `validate_mindmap_markdown` below is
registered in src/notebook_artifacts.py's _KIND_VALIDATORS so
generate_artifact retries (with the error fed back) on such a format miss —
same recovery shape as the slide-deck, infographic and flashcards validators.
"""

from __future__ import annotations

import re

_MERMAID_FENCE_RE = re.compile(r"```mermaid[ \t]*\n(.*?)```", re.S)
_ROOT_RE = re.compile(r"^\s*root\(\((.+?)\)\)\s*$")
_MIN_BRANCHES = 2


def validate_mindmap_markdown(content: str) -> None:
    """Raise ValueError (Dutch, fed back to the model on retry) on a format miss."""
    m = _MERMAID_FENCE_RE.search(content or "")
    if not m:
        raise ValueError(
            "geen ```mermaid-codefence gevonden. Lever exact één codefence met "
            "taalaanduiding \"mermaid\" waarvan de eerste regel \"mindmap\" is."
        )
    lines = [l for l in m.group(1).splitlines() if l.strip()]
    if not lines or lines[0].strip() != "mindmap":
        raise ValueError(
            "de eerste regel in de mermaid-fence moet \"mindmap\" zijn."
        )
    root_idx = None
    for i, line in enumerate(lines):
        if _ROOT_RE.match(line):
            root_idx = i
            break
    if root_idx is None:
        raise ValueError(
            "geen wortel gevonden: gebruik \"root((Onderwerp))\" als eerste knoop."
        )
    root_indent = len(lines[root_idx]) - len(lines[root_idx].lstrip())
    below = [l for l in lines[root_idx + 1:]
             if (len(l) - len(l.lstrip())) > root_indent]
    if below:
        branch_indent = min(len(l) - len(l.lstrip()) for l in below)
        branches = sum(1 for l in below if (len(l) - len(l.lstrip())) == branch_indent)
    else:
        branches = 0
    if branches < _MIN_BRANCHES:
        raise ValueError(
            f"slechts {branches} hoofdtakken onder de wortel; maak er 4 tot 8, "
            "elk met 2 tot 5 subtakken, uitsluitend via inspringing met spaties."
        )
