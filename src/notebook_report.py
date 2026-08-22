# src/notebook_report.py
"""Thin adapter: render a notebook artifact through the same editorial
visual-report pipeline research reports use (src/visual_report.py), so the
studio panel's generated artifacts (study guide, briefing, FAQ, quiz,
mindmap) get the same hero/TOC/typography/print-toolbar treatment.

Deliberately not a fork of visual_report.py's ~1900-line template: this
module only maps notebook-artifact fields onto generate_visual_report's
existing parameters (plus the small report_type_label/generated_by_label
knobs added there for this reuse).

English labels: this surface (the "Open Visual Report" page) uses English
kind labels, distinct from src.notebook_artifacts._KIND_LABELS (Dutch,
used for Document titles / the studio UI). Exported here so both this
module and future callers (e.g. other backend surfaces) can reach the same
mapping without re-deriving it.
"""
from datetime import datetime
from typing import Optional

from src.visual_report import generate_visual_report

ENGLISH_KIND_LABELS = {
    "study_guide": "Study guide",
    "briefing": "Briefing",
    "faq": "FAQ",
    "quiz": "Quiz",
    "mindmap": "Mindmap",
    "infographic": "Infographic",
    "flashcards": "Flashcards",
    "data_table": "Data table",
    "slide_deck": "Slide deck",
}


def generate_notebook_artifact_report(
    notebook_name: str,
    kind: str,
    document_title: Optional[str],
    document_content: str,
) -> str:
    """Render a notebook artifact's markdown as a self-contained HTML report.

    `document_title` is passed through as generate_visual_report's `question`
    — used only as a title fallback: if the artifact markdown itself starts
    with a heading (the generation prompts in notebook_artifacts.py instruct
    the model to emit one), that heading wins as the page title, matching
    the same behavior research reports already have.

    No `sources`: a notebook's indexed sources are uploaded files, not URLs.
    The shared template renders each source as an `<a href="...">` — with no
    URL that degrades to `href=""` (a self-referencing, misleading link), so
    this adapter passes `sources=[]` rather than surface that. See the task
    report for the rendered HTML that led to this call.

    No `session_id`: the "Discuss"/hide-image affordances tied to it don't
    apply to notebook artifacts, and the template already guards on it being
    falsy (both button blocks render empty).

    No `category`: the per-category palettes/structural CSS (product,
    comparison, howto, landscape) are tuned for research-report shapes
    (e.g. howto's numbered-step H2 treatment) and don't fit a FAQ or quiz.
    Default (no category) gives the neutral long-form serif treatment.
    """
    kind_label = ENGLISH_KIND_LABELS.get(kind, kind)
    title = (document_title or "").strip() or kind_label
    stats = {
        "Notebook": notebook_name,
        "Type": kind_label,
        "Generated": datetime.now().strftime("%B %d, %Y"),
    }
    return generate_visual_report(
        question=title,
        report_markdown=document_content,
        sources=[],
        stats=stats,
        category=None,
        session_id=None,
        report_type_label=f"Notebook {kind_label}",
        generated_by_label="Ithaka Notebooks",
    )
