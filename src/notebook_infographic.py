# src/notebook_infographic.py
"""Render a notebook "infographic" artifact as a self-contained poster HTML page.

Unlike the other text artifacts (study guide/briefing/FAQ/quiz/mindmap), which
reuse the editorial long-form template in src/visual_report.py via
src/notebook_report.py, the infographic gets its own compact poster-style
renderer: hero title, a grid of stat cards ("key numbers"), 2-column section
cards, and a closing takeaway band.

The generation prompt in src/notebook_artifacts.py instructs the model to
emit a fixed markdown structure:

    # <title>

    ## Key numbers
    - **<number>** — <label>
    ...

    ## <section heading>
    - <bullet>
    ...

    > <one-sentence takeaway>

`_parse_infographic_markdown` below parses that structure but never assumes
it is complete or well-formed: any markdown that lacks part of it (missing
title, missing key numbers, prose instead of bullets, no takeaway, ...)
still renders — whatever *was* recognized, plus a fallback "Content" card
holding whatever text couldn't be matched to the expected shape. The page
is never empty and generation never raises on malformed input.

All interpolated text goes through html.escape (no raw interpolation, no
markdown-to-HTML pass-through) and the page carries no external resources
(no CDN fonts, no remote images, no script tags) — same constraints as
src/visual_report.py, just with a much smaller, purpose-built template
instead of reusing its ~1900-line one.
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from typing import List, Optional, Tuple

_H1_RE = re.compile(r'^#\s+(.+)$')
_H2_RE = re.compile(r'^##\s+(.+)$')
_BULLET_RE = re.compile(r'^[-*]\s+(.+)$')
_BLOCKQUOTE_RE = re.compile(r'^>\s?(.*)$')
# "- **<number>** — <label>" (or an en/em dash, or a plain hyphen, as the
# separator — the model doesn't always pick the exact glyph asked for).
_STAT_BULLET_RE = re.compile(r'^\*\*(.+?)\*\*\s*(?:—|–|-)\s*(.+)$')
# Fallback when a "key numbers" bullet has bold text but no dash separator:
# use the bold span as the number, everything else (if any) as the label.
_BOLD_RE = re.compile(r'\*\*(.+?)\*\*')

_KEY_NUMBERS_HEADING = "key numbers"


def _parse_stat_bullet(text: str) -> Tuple[str, str]:
    """Best-effort split of one "key numbers" bullet into (number, label).

    Tries the documented "**number** — label" shape first, then falls back
    to using a bold span as the number with the remainder as label, then
    finally treats the whole bullet as the "number" with an empty label
    rather than dropping it.
    """
    m = _STAT_BULLET_RE.match(text)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = _BOLD_RE.search(text)
    if m:
        number = m.group(1).strip()
        label = (text[:m.start()] + text[m.end():]).strip(" -—–:")
        return number, label
    return text.strip(), ""


def _parse_infographic_markdown(markdown_text: str) -> dict:
    """Parse the infographic markdown structure, tolerating partial input.

    Returns a dict with keys: title, stats (list[(number, label)]),
    sections (list[(heading, bullets, extra_paragraphs)]), takeaway,
    leftover_bullets, leftover_paragraphs. Every field degrades to an
    empty/None value rather than raising when the expected structure is
    missing.
    """
    lines = (markdown_text or "").replace("\r\n", "\n").split("\n")
    n = len(lines)
    consumed = [False] * n

    title: Optional[str] = None
    for i, line in enumerate(lines):
        m = _H1_RE.match(line)
        if m:
            title = m.group(1).strip()
            consumed[i] = True
            break

    h2_positions: List[Tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = _H2_RE.match(line)
        if m:
            h2_positions.append((i, m.group(1).strip()))

    # Blockquote: take the LAST contiguous run of "> " lines in the
    # document (the closing takeaway, per the documented structure) so an
    # unrelated quoted line earlier in a section body doesn't get mistaken
    # for it.
    bq_start: Optional[int] = None
    bq_end: Optional[int] = None
    i = 0
    while i < n:
        if _BLOCKQUOTE_RE.match(lines[i]):
            start = i
            while i < n and _BLOCKQUOTE_RE.match(lines[i]):
                i += 1
            bq_start, bq_end = start, i  # run is [start, i)
        else:
            i += 1

    boundaries = sorted(
        {idx for idx, _ in h2_positions}
        | ({bq_start} if bq_start is not None else set())
        | {n}
    )

    def _next_boundary(after: int) -> int:
        for b in boundaries:
            if b > after:
                return b
        return n

    stats: List[Tuple[str, str]] = []
    sections: List[Tuple[str, List[str], List[str]]] = []

    for i, heading in h2_positions:
        consumed[i] = True
        end = _next_boundary(i)
        body_bullets: List[str] = []
        body_extra: List[str] = []
        for j in range(i + 1, end):
            consumed[j] = True
            line = lines[j]
            if not line.strip():
                continue
            bm = _BULLET_RE.match(line)
            if bm:
                body_bullets.append(bm.group(1).strip())
            else:
                body_extra.append(line.strip())

        if heading.strip().lower() == _KEY_NUMBERS_HEADING:
            for b in body_bullets:
                stats.append(_parse_stat_bullet(b))
            # Prose that landed in the Key numbers section (no bullet
            # syntax) has no number/label shape to fall back to safely —
            # surface it as an ordinary section instead of guessing.
            if body_extra:
                sections.append((heading, [], body_extra))
        else:
            sections.append((heading, body_bullets, body_extra))

    # The generation prompt orders the model to write in the *sources'*
    # language (see _BASE_RULES in src/notebook_artifacts.py), so a Dutch
    # (or other non-English) source set can plausibly come back with
    # "## Kerncijfers" instead of the literal "## Key numbers" heading the
    # strict match above requires. Rather than hardcode translations, fall
    # back to structural detection: the first ordinary section whose every
    # bullet fits the strict "**number** — label" stat shape (and has no
    # stray prose) is almost certainly the key-numbers section under a
    # different name — promote it instead of silently losing the headline
    # feature.
    if not stats:
        for idx, (heading, bullets, extra) in enumerate(sections):
            if bullets and not extra and all(_STAT_BULLET_RE.match(b) for b in bullets):
                stats = [_parse_stat_bullet(b) for b in bullets]
                del sections[idx]
                break

    takeaway: Optional[str] = None
    if bq_start is not None:
        parts = [
            (_BLOCKQUOTE_RE.match(lines[k]).group(1) or "").strip()
            for k in range(bq_start, bq_end)
        ]
        consumed[bq_start:bq_end] = [True] * (bq_end - bq_start)
        joined = " ".join(p for p in parts if p).strip()
        takeaway = joined or None

    leftover_bullets: List[str] = []
    leftover_paragraphs: List[str] = []
    for i, line in enumerate(lines):
        if consumed[i] or not line.strip():
            continue
        bm = _BULLET_RE.match(line)
        if bm:
            leftover_bullets.append(bm.group(1).strip())
        else:
            leftover_paragraphs.append(line.strip())

    return {
        "title": title,
        "stats": stats,
        "sections": sections,
        "takeaway": takeaway,
        "leftover_bullets": leftover_bullets,
        "leftover_paragraphs": leftover_paragraphs,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

:root {{
  --font-display: 'Charter', 'Iowan Old Style', Georgia, serif;
  --font-body: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --bg: #fbf9f4;
  --bg-surface: #ffffff;
  --border: rgba(0,0,0,0.08);
  --text: #1a1817;
  --text-dim: #5a5651;
  --text-muted: #8a8580;
  --accent: #b8543a;
  --gold: #c9952e;
  --gold-bg: rgba(201,149,46,0.09);
  --radius: 12px;
  --shadow-sm: 0 1px 3px rgba(0,0,0,0.05);
}}

@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #131214; --bg-surface: #1c1a1e;
    --border: rgba(255,255,255,0.07);
    --text: #ece8e2; --text-dim: #a8a39c; --text-muted: #6f6b66;
    --accent: #e88f73;
    --gold: #e8c05a; --gold-bg: rgba(232,192,90,0.09);
    --shadow-sm: 0 1px 3px rgba(0,0,0,0.4);
  }}
}}

body {{
  font-family: var(--font-body);
  background: var(--bg);
  color: var(--text);
  line-height: 1.55;
  font-size: 16px;
  -webkit-font-smoothing: antialiased;
}}

.ig-wrap {{ max-width: 900px; margin: 0 auto; padding: 2.5rem 1.5rem 3rem; }}

.ig-hero {{ text-align: center; padding: 1.5rem 0 2rem; }}
.ig-hero-label {{
  text-transform: uppercase; letter-spacing: 0.28em; font-size: 0.68rem;
  font-weight: 600; color: var(--accent); margin-bottom: 1rem;
}}
.ig-hero h1 {{
  font-family: var(--font-display);
  font-size: clamp(1.7rem, 4.5vw, 2.5rem);
  font-weight: 700; line-height: 1.15; color: var(--text);
  letter-spacing: -0.02em;
}}

.ig-eyebrow {{
  text-transform: uppercase; letter-spacing: 0.18em; font-size: 0.72rem;
  font-weight: 600; color: var(--text-muted); margin: 0 0 0.9rem;
  text-align: center;
}}

.ig-stats-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 1rem;
  margin-bottom: 2.25rem;
}}
.ig-stat-card {{
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 1.25rem 1rem;
  text-align: center;
  box-shadow: var(--shadow-sm);
}}
.ig-stat-value {{
  font-family: var(--font-display);
  font-size: clamp(1.5rem, 4vw, 2.1rem);
  font-weight: 700; color: var(--accent); line-height: 1.1;
  word-break: break-word;
}}
.ig-stat-label {{ margin-top: 0.4rem; font-size: 0.8rem; color: var(--text-dim); }}

.ig-sections-grid {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1.25rem;
  margin-bottom: 1rem;
}}
@media (max-width: 700px) {{
  .ig-sections-grid {{ grid-template-columns: 1fr; }}
}}
.ig-card {{
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 1.4rem 1.5rem;
  box-shadow: var(--shadow-sm);
}}
.ig-card h2 {{
  font-family: var(--font-display);
  font-size: 1.1rem; font-weight: 700; margin-bottom: 0.7rem; color: var(--text);
}}
.ig-card ul {{ margin: 0; padding-left: 1.1rem; }}
.ig-card li {{ margin-bottom: 0.4rem; color: var(--text-dim); }}
.ig-card li::marker {{ color: var(--accent); }}
.ig-card p {{ margin-bottom: 0.5rem; color: var(--text-dim); }}
.ig-card p:last-child {{ margin-bottom: 0; }}
.ig-card strong {{ color: var(--text); }}
/* A single card (e.g. the fallback "Content" card with nothing else
   recognized) would otherwise sit half-width in the 2-column grid. */
.ig-sections-grid.ig-single-card {{ grid-template-columns: 1fr; }}

.ig-takeaway {{
  margin: 2rem 0 1.5rem;
  padding: 1.1rem 1.6rem;
  border-left: 3px solid var(--gold);
  background: var(--gold-bg);
  border-radius: 0 var(--radius) var(--radius) 0;
  font-family: var(--font-display);
  font-style: italic;
  font-size: 1.05rem;
  text-align: center;
  color: var(--text);
}}

.ig-meta {{
  text-align: center; font-size: 0.75rem; color: var(--text-muted);
  margin-top: 2rem; padding-top: 1rem; border-top: 1px solid var(--border);
}}

@media print {{
  /* Force the light palette on paper regardless of the viewer's dark
     preference — printing whatever prefers-color-scheme picked would risk
     light text on a white page. */
  body {{ background: #fff !important; color: #1a1817 !important; }}
  .ig-card, .ig-stat-card {{
    box-shadow: none !important; background: #fff !important;
    border-color: rgba(0,0,0,0.16) !important;
  }}
  .ig-card h2, .ig-card li, .ig-card p {{ color: #1a1817 !important; }}
  .ig-hero-label, .ig-stat-value {{ color: #b8543a !important; }}
  .ig-stat-label {{ color: #5a5651 !important; }}
  .ig-takeaway {{
    background: rgba(201,149,46,0.09) !important;
    border-left-color: #c9952e !important; color: #1a1817 !important;
  }}
  .ig-meta {{ color: #8a8580 !important; }}
}}

@media (max-width: 360px) {{
  .ig-wrap {{ padding: 1.5rem 1rem 2rem; }}
}}
</style>
</head>
<body>
<div class="ig-wrap">
  <div class="ig-hero">
    <div class="ig-hero-label">Ithaka &mdash; Infographic</div>
    <h1>{title}</h1>
  </div>
  {stats_html}
  {sections_html}
  {takeaway_html}
  <div class="ig-meta">{notebook_name} &middot; {date}</div>
</div>
</body>
</html>
"""


def _esc_bold(text: str) -> str:
    """Escape text for HTML, then re-render any surviving ``**bold**``
    markdown spans as <strong>.

    Escaping first (rather than converting markdown to HTML first) keeps the
    no-raw-interpolation property: the only HTML this ever emits is the
    literal <strong>/</strong> tags this function itself writes, never
    anything reconstructed from the input.
    """
    escaped = html.escape(text)
    return _BOLD_RE.sub(r'<strong>\1</strong>', escaped)


def _render_stats_html(stats: List[Tuple[str, str]]) -> str:
    if not stats:
        return ""
    cards = "".join(
        f'<div class="ig-stat-card">'
        f'<div class="ig-stat-value">{_esc_bold(number)}</div>'
        f'<div class="ig-stat-label">{_esc_bold(label)}</div>'
        f'</div>'
        for number, label in stats
    )
    return (
        '<div class="ig-eyebrow">Key numbers</div>'
        f'<div class="ig-stats-grid">{cards}</div>'
    )


def _render_card_html(heading: str, bullets: List[str], extra: List[str]) -> str:
    body = "".join(f'<p>{_esc_bold(p)}</p>' for p in extra)
    if bullets:
        body += "<ul>" + "".join(f"<li>{_esc_bold(b)}</li>" for b in bullets) + "</ul>"
    return f'<div class="ig-card"><h2>{_esc_bold(heading)}</h2>{body}</div>'


def _render_sections_html(
    sections: List[Tuple[str, List[str], List[str]]],
    leftover_bullets: List[str],
    leftover_paragraphs: List[str],
) -> str:
    cards = [_render_card_html(heading, bullets, extra) for heading, bullets, extra in sections]
    if leftover_bullets or leftover_paragraphs:
        cards.append(_render_card_html("Content", leftover_bullets, leftover_paragraphs))
    if not cards:
        return ""
    grid_class = "ig-sections-grid" + (" ig-single-card" if len(cards) == 1 else "")
    return f'<div class="{grid_class}">{"".join(cards)}</div>'


def generate_infographic(
    title: Optional[str],
    markdown: str,
    notebook_name: str,
    generated_at: datetime,
) -> str:
    """Render an infographic artifact's markdown as a self-contained HTML poster.

    `title` is used only as a fallback: a "# " heading in `markdown` (the
    generation prompt in src/notebook_artifacts.py asks for one) wins as the
    page title, matching the same title-precedence behavior
    generate_notebook_artifact_report has for the other text artifacts.

    Never raises on malformed/partial markdown: missing sections, missing
    stats, prose instead of bullets, or no structure at all all still
    produce a valid, non-empty page (see _parse_infographic_markdown).
    """
    parsed = _parse_infographic_markdown(markdown)
    effective_title = parsed["title"] or (title or "").strip() or "Infographic"

    stats_html = _render_stats_html(parsed["stats"])
    sections_html = _render_sections_html(
        parsed["sections"], parsed["leftover_bullets"], parsed["leftover_paragraphs"]
    )
    takeaway_html = (
        f'<div class="ig-takeaway">{_esc_bold(parsed["takeaway"])}</div>'
        if parsed["takeaway"] else ""
    )

    return _TEMPLATE.format(
        title=html.escape(effective_title),
        stats_html=stats_html,
        sections_html=sections_html,
        takeaway_html=takeaway_html,
        notebook_name=html.escape(notebook_name or ""),
        date=html.escape(generated_at.strftime("%B %d, %Y")),
    )


# ---------------------------------------------------------------------------
# Generation-time format validation
# ---------------------------------------------------------------------------

def validate_infographic_markdown(content: str) -> None:
    """Raise ValueError (Dutch, fed back to the model on retry) on a format miss.

    Registered in src/notebook_artifacts.py's _KIND_VALIDATORS so
    generate_artifact retries with the error fed back — same recovery shape
    as the slide-deck JSON validator. Checks the *outcome the renderer
    needs* (via _parse_infographic_markdown) rather than literal headings:
    anything accepted here renders as an actual poster, not as one big
    fallback "Content" card full of raw markdown — which is exactly what
    unvalidated free-prose model output used to produce.
    """
    parsed = _parse_infographic_markdown(content)
    problems: List[str] = []
    if not parsed["title"]:
        problems.append("geen '# '-titel gevonden")
    if not parsed["stats"]:
        problems.append("geen '## Key numbers'-sectie met bullets gevonden")
    elif all(not label for _num, label in parsed["stats"]):
        problems.append(
            "key-number-bullets missen de vorm '- **<getal>** — <label>' "
            "(vetgedrukt getal, gedachtestreepje, kort label)"
        )
    if not any(bullets for _heading, bullets, _extra in parsed["sections"]):
        problems.append("geen gewone '## <sectiekop>'-sectie met bullet-feiten gevonden")
    if any(line.lstrip().startswith("###") for line in (content or "").splitlines()):
        problems.append("'###'-koppen zijn niet toegestaan (alleen '# ' en '## ')")
    if problems:
        raise ValueError(
            "infographic-structuur klopt niet: "
            + "; ".join(problems)
            + ". Lever exact de gevraagde markdownstructuur."
        )
