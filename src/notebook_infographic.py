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

v2 (2026-09-03): new artifacts store the JSON model from
`extract_infographic`; `generate_infographic` dispatches on
`is_infographic_v2`. The markdown path below stays for previously
generated artifacts.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime
from typing import List, Optional, Tuple

from src.notebook_slides import _JSON_FENCE_RE

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

/* No dark palette: notebook viewers render always-light, matching the
   mindmap/flashcards/slides templates. */

body {{
  font-family: var(--font-body);
  background: var(--bg);
  color: var(--text);
  line-height: 1.5;
  font-size: 15px;
  -webkit-font-smoothing: antialiased;
}}

.ig-wrap {{ max-width: 1180px; margin: 0 auto; padding: 2.25rem 1.75rem 2.5rem; }}

.ig-hero {{ text-align: center; padding: 0.25rem 0 1.9rem; }}
.ig-hero-label {{
  text-transform: uppercase; letter-spacing: 0.28em; font-size: 0.66rem;
  font-weight: 600; color: var(--accent); margin-bottom: 0.8rem;
}}
.ig-hero h1 {{
  font-family: var(--font-display);
  font-size: clamp(1.6rem, 3.4vw, 2.4rem);
  font-weight: 700; line-height: 1.12; color: var(--text);
  letter-spacing: -0.02em;
  max-width: 60ch; margin: 0 auto;
}}

/* ── Poster grid: sections left/right, hero + bars center ─────────── */
.ig-grid {{
  display: grid;
  grid-template-columns: 1fr 1.2fr 1fr;
  gap: 1.5rem 2.25rem;
  align-items: start;
}}
.ig-grid.ig-single {{ grid-template-columns: 1fr; max-width: 640px; margin: 0 auto; }}
.ig-col-left, .ig-col-right {{ display: flex; flex-direction: column; gap: 1.9rem; }}
.ig-center {{ display: flex; flex-direction: column; gap: 1.4rem; align-items: center; }}

.ig-panel {{ text-align: center; }}
.ig-icon {{
  width: 58px; height: 58px; border-radius: 50%;
  display: inline-flex; align-items: center; justify-content: center;
  background: var(--pc-tint); color: var(--pc);
  margin-bottom: 0.65rem;
}}
.ig-icon svg {{ width: 28px; height: 28px; }}
.ig-panel h2 {{
  font-size: 0.86rem; font-weight: 700; letter-spacing: 0.08em;
  text-transform: uppercase; color: var(--text); margin-bottom: 0.45rem;
}}
.ig-panel ul {{ list-style: none; margin: 0; padding: 0; text-align: left; }}
.ig-panel li {{
  margin-bottom: 0.4rem; color: var(--text-dim); font-size: 0.86rem;
  padding-left: 0.9rem; position: relative;
}}
.ig-panel li::before {{
  content: ""; position: absolute; left: 0; top: 0.52em;
  width: 0.4rem; height: 2px; background: var(--pc);
}}
.ig-panel p {{ color: var(--text-dim); font-size: 0.86rem; margin-bottom: 0.45rem; text-align: left; }}
.ig-panel strong {{ color: var(--text); }}

.ig-hero-art {{ width: min(100%, 300px); margin: 0.25rem auto 0; }}
.ig-hero-art svg {{ width: 100%; height: auto; display: block; }}

.ig-takeaway {{
  padding: 0.9rem 1.3rem;
  border-left: 3px solid var(--gold);
  background: var(--gold-bg);
  border-radius: 0 var(--radius) var(--radius) 0;
  font-family: var(--font-display);
  font-style: italic;
  font-size: 0.98rem;
  text-align: center;
  color: var(--text);
}}

/* ── Capacity bars (Key numbers) ──────────────────────────────────── */
.ig-bars {{ width: 100%; }}
.ig-eyebrow {{
  text-transform: uppercase; letter-spacing: 0.18em; font-size: 0.7rem;
  font-weight: 700; color: var(--text); margin: 0 0 0.8rem;
  text-align: center;
}}
.ig-bar-row {{
  display: grid; grid-template-columns: minmax(72px, auto) 1fr;
  gap: 0.3rem 0.8rem; align-items: center; margin-bottom: 0.7rem;
}}
.ig-bar-label {{
  font-size: 0.7rem; font-weight: 700; letter-spacing: 0.05em;
  text-transform: uppercase; color: var(--text); text-align: right;
  line-height: 1.25;
}}
.ig-bar-track {{
  height: 18px; border-radius: 9px;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  overflow: hidden; position: relative;
}}
.ig-bar-fill {{ height: 100%; border-radius: 9px; }}
.ig-bar-value {{
  grid-column: 2; font-size: 0.76rem; color: var(--text-dim);
  padding-left: 0.2rem;
}}

.ig-meta {{
  text-align: center; font-size: 0.75rem; color: var(--text-muted);
  margin-top: 2.25rem; padding-top: 1rem; border-top: 1px solid var(--border);
}}

@media (max-width: 900px) {{
  .ig-grid {{ grid-template-columns: 1fr; }}
  .ig-center {{ order: -1; }}
  .ig-hero-art {{ width: min(60%, 240px); }}
}}

@media (max-width: 480px) {{
  .ig-bar-row {{ grid-template-columns: 1fr; gap: 0.25rem; }}
  .ig-bar-label {{ text-align: left; }}
  .ig-bar-value {{ grid-column: 1; }}
}}

@media print {{
  /* Plain white on paper: flatten the tinted page background so
     ink-friendly output stays crisp. */
  body {{ background: #fff !important; color: #1a1817 !important; }}
  .ig-panel h2, .ig-panel li, .ig-panel p {{ color: #1a1817 !important; }}
  .ig-hero-label {{ color: #b8543a !important; }}
  .ig-takeaway {{
    background: rgba(201,149,46,0.09) !important;
    border-left-color: #c9952e !important; color: #1a1817 !important;
  }}
  .ig-meta {{ color: #8a8580 !important; }}
  .ig-bar-track {{ border-color: rgba(0,0,0,0.2) !important; }}
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
  {grid_html}
  <div class="ig-meta">{notebook_name} &middot; {date}</div>
</div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Poster building blocks: palette, icons, hero art
# ---------------------------------------------------------------------------

# One (stroke, tint) pair per panel, cycled in order — mirrors the varied
# pastel icon clusters of the NotebookLM-style reference poster.
_PALETTE = [
    ("#2a9d8f", "rgba(42,157,143,0.13)"),   # teal
    ("#4a7fb5", "rgba(74,127,181,0.13)"),   # blue
    ("#e76f51", "rgba(231,111,81,0.13)"),   # orange
    ("#7d6bb0", "rgba(125,107,176,0.13)"),  # purple
    ("#b8543a", "rgba(184,84,58,0.12)"),    # accent red
    ("#c9952e", "rgba(201,149,46,0.13)"),   # gold
]

_ICON_ATTRS = (
    'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"'
)

# Keyword → icon body (24x24 stroke icons). Keys are matched against the
# lowercased section heading, Dutch and English variants together.
_ICONS = {
    "sources": '<path d="M4 16a4 4 0 0 1 .8-7.9A5.5 5.5 0 0 1 15.5 6a4.5 4.5 0 0 1 4.3 6.9"/><path d="M12 12v8"/><path d="m8.5 15.5 3.5-3.5 3.5 3.5"/>',
    "audio": '<path d="M4 13a8 8 0 0 1 16 0"/><rect x="3" y="13" width="4" height="6" rx="1.5"/><rect x="17" y="13" width="4" height="6" rx="1.5"/>',
    "video": '<rect x="3" y="5" width="18" height="14" rx="3"/><path d="m10 9 5 3-5 3z"/>',
    "chat": '<path d="M4 5h12a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H9l-4 3v-3H4a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2z" transform="translate(1.5 0)"/><path d="M8.5 9h7"/><path d="M8.5 12h4.5"/>',
    "graph": '<circle cx="12" cy="12" r="2.2"/><circle cx="5" cy="6" r="1.8"/><circle cx="19" cy="6" r="1.8"/><circle cx="5" cy="18" r="1.8"/><circle cx="19" cy="18" r="1.8"/><path d="M10.4 10.6 6.4 7.3M13.6 10.6l4-3.3M10.4 13.4l-4 3.3M13.6 13.4l4 3.3"/>',
    "bars": '<path d="M4 20V10"/><path d="M10 20V4"/><path d="M16 20v-8"/><path d="M22 20H2"/>',
    "target": '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4.5"/><circle cx="12" cy="12" r="1.2" fill="currentColor"/>',
    "warning": '<path d="M12 4 2.5 20h19z"/><path d="M12 10v4.5"/><path d="M12 17.4v.1"/>',
    "gear": '<path d="M4 6h8a3.5 3.5 0 0 1 0 7H9a3.5 3.5 0 0 0 0 7h11"/><circle cx="4" cy="6" r="1.6"/><circle cx="20" cy="20" r="1.6"/>',
    "people": '<circle cx="9" cy="8.5" r="3"/><path d="M3.5 19a5.5 5.5 0 0 1 11 0"/><circle cx="17" cy="9.5" r="2.4"/><path d="M15.5 14.2a4.6 4.6 0 0 1 5.5 4.3"/>',
    "doc": '<path d="M6 2.5h8l4 4V21a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V3.5a1 1 0 0 1 1-1z"/><path d="M14 2.5v4h4"/><path d="M8.5 12h7M8.5 15.5h7M8.5 8.5H11"/>',
    "search": '<circle cx="10.5" cy="10.5" r="6"/><path d="m15 15 5.5 5.5"/>',
    "spark": '<path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"/><circle cx="12" cy="12" r="2.6"/>',
}

_ICON_KEYWORDS = [
    ("sources", ("bron", "source", "input", "upload", "materiaal")),
    ("audio", ("audio", "podcast", "geluid", "spraak")),
    ("video", ("video", "film")),
    ("chat", ("chat", "vraag", "vragen", "gesprek", "antwoord", "q&a", "faq")),
    ("graph", ("mindmap", "concept", "relatie", "structuur", "verband", "netwerk")),
    ("bars", ("cijfer", "number", "getal", "data", "stat", "meting", "kern")),
    # people before target: "doelgroep" must match here, not on "doel".
    ("people", ("student", "gebruiker", "mens", "team", "doelgroep", "begeleid", "docent", "stakeholder", "rol")),
    ("target", ("doel", "goal", "missie", "visie", "ambitie", "resultaat")),
    ("warning", ("risico", "risk", "uitdaging", "knelpunt", "pijnpunt", "probleem", "aandachtspunt")),
    ("gear", ("stap", "proces", "werkwijze", "aanpak", "fase", "workflow", "implementatie", "planning")),
    ("doc", ("document", "rapport", "artifact", "output", "deliverable", "verslag")),
    ("search", ("zoek", "onderzoek", "analyse", "research", "verkenning")),
]


def _pick_icon(heading: str) -> str:
    """Return the inline-SVG body for a section heading via keyword match."""
    low = (heading or "").lower()
    for name, keywords in _ICON_KEYWORDS:
        if any(k in low for k in keywords):
            return _ICONS[name]
    return _ICONS["spark"]


# Central hero: an abstract knowledge-hub network in the panel palette —
# the poster's focal point, standing in for the reference's illustration.
_HERO_ART = (
    '<svg viewBox="0 0 300 260" ' + _ICON_ATTRS.replace('stroke-width="1.8"', 'stroke-width="2"') + '>'
    '<defs><radialGradient id="ig-hub" cx="0.5" cy="0.45" r="0.65">'
    '<stop offset="0" stop-color="#7d6bb0" stop-opacity="0.35"/>'
    '<stop offset="1" stop-color="#4a7fb5" stop-opacity="0.06"/></radialGradient>'
    '<linearGradient id="ig-core" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0" stop-color="#4a7fb5"/><stop offset="1" stop-color="#7d6bb0"/></linearGradient></defs>'
    '<circle cx="150" cy="128" r="112" fill="url(#ig-hub)" stroke="none"/>'
    '<g stroke="#a9a29a" stroke-width="1.6">'
    '<path d="M150 128 74 62"/><path d="M150 128 226 62"/><path d="M150 128 44 140"/>'
    '<path d="M150 128 256 140"/><path d="M150 128 92 208"/><path d="M150 128 208 208"/>'
    '</g>'
    '<circle cx="74" cy="62" r="13" fill="#2a9d8f" stroke="none"/>'
    '<circle cx="226" cy="62" r="13" fill="#e76f51" stroke="none"/>'
    '<circle cx="44" cy="140" r="11" fill="#c9952e" stroke="none"/>'
    '<circle cx="256" cy="140" r="11" fill="#b8543a" stroke="none"/>'
    '<circle cx="92" cy="208" r="12" fill="#4a7fb5" stroke="none"/>'
    '<circle cx="208" cy="208" r="12" fill="#7d6bb0" stroke="none"/>'
    '<circle cx="150" cy="128" r="34" fill="url(#ig-core)" stroke="none"/>'
    '<g stroke="#ffffff" stroke-width="2">'
    '<path d="M141 137l9-9 6 6 8-12"/>'
    '<circle cx="141" cy="137" r="2.4" fill="#ffffff" stroke="none"/>'
    '<circle cx="150" cy="128" r="2.4" fill="#ffffff" stroke="none"/>'
    '<circle cx="156" cy="134" r="2.4" fill="#ffffff" stroke="none"/>'
    '<circle cx="164" cy="122" r="2.4" fill="#ffffff" stroke="none"/>'
    '</g>'
    '</svg>'
)


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


_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def _render_stats_html(stats: List[Tuple[str, str]]) -> str:
    """Render the key numbers as horizontal capacity bars.

    Widths are RANK-based, not linearly value-based: stat magnitudes in one
    poster routinely span unrelated units ("3 varianten" next to "85%" next
    to "1000 studenten"), so a linear scale flattens everything but the
    peak. Ranking keeps the visual ordering honest (bigger number = longer
    bar) while every bar stays readable — the same stylized staggering the
    reference poster uses. Stats without a parseable number get full width.
    """
    if not stats:
        return ""
    values: List[Optional[float]] = []
    for number, _label in stats:
        m = _NUMBER_RE.search(number.replace(",", "."))
        values.append(abs(float(m.group(0))) if m else None)
    ordered = sorted({v for v in values if v is not None})
    span = max(len(ordered) - 1, 1)

    rows = []
    for i, (number, label) in enumerate(stats):
        color, _tint = _PALETTE[i % len(_PALETTE)]
        next_color, _ = _PALETTE[(i + 1) % len(_PALETTE)]
        v = values[i]
        width = 100.0 if v is None else 34.0 + 66.0 * (ordered.index(v) / span)
        rows.append(
            '<div class="ig-bar-row">'
            f'<div class="ig-bar-label">{_esc_bold(label) or "&nbsp;"}</div>'
            '<div class="ig-bar-track">'
            f'<div class="ig-bar-fill" style="width:{width:.0f}%;'
            f'background:linear-gradient(90deg,{color},{next_color})"></div>'
            "</div>"
            f'<div class="ig-bar-value">{_esc_bold(number)}</div>'
            "</div>"
        )
    return (
        '<div class="ig-bars"><div class="ig-eyebrow">Key numbers</div>'
        + "".join(rows)
        + "</div>"
    )


def _render_panel_html(heading: str, bullets: List[str], extra: List[str], index: int) -> str:
    color, tint = _PALETTE[index % len(_PALETTE)]
    icon = _pick_icon(heading)
    body = "".join(f"<p>{_esc_bold(p)}</p>" for p in extra)
    if bullets:
        body += "<ul>" + "".join(f"<li>{_esc_bold(b)}</li>" for b in bullets) + "</ul>"
    return (
        f'<div class="ig-panel" style="--pc:{color};--pc-tint:{tint}">'
        f'<div class="ig-icon"><svg {_ICON_ATTRS}>{icon}</svg></div>'
        f"<h2>{_esc_bold(heading)}</h2>{body}</div>"
    )


def _render_grid_html(parsed: dict) -> str:
    """Compose the three-zone poster grid from the parsed markdown."""
    sections = list(parsed["sections"])
    if parsed["leftover_bullets"] or parsed["leftover_paragraphs"]:
        sections.append(("Content", parsed["leftover_bullets"], parsed["leftover_paragraphs"]))

    panels = [
        _render_panel_html(heading, bullets, extra, i)
        for i, (heading, bullets, extra) in enumerate(sections)
    ]
    stats_html = _render_stats_html(parsed["stats"])
    takeaway_html = (
        f'<div class="ig-takeaway">{_esc_bold(parsed["takeaway"])}</div>'
        if parsed["takeaway"] else ""
    )

    # Degenerate input (one fallback panel, nothing else): a single centered
    # column reads better than an empty poster skeleton.
    if len(panels) <= 1 and not stats_html and not takeaway_html:
        inner = "".join(panels)
        return f'<div class="ig-grid ig-single">{inner}</div>'

    left = "".join(panels[0::2])
    right = "".join(panels[1::2])
    center = (
        f'<div class="ig-hero-art">{_HERO_ART}</div>{takeaway_html}{stats_html}'
    )
    return (
        '<div class="ig-grid">'
        f'<div class="ig-col-left">{left}</div>'
        f'<div class="ig-center">{center}</div>'
        f'<div class="ig-col-right">{right}</div>'
        "</div>"
    )


def generate_infographic(
    title: Optional[str],
    markdown: str,
    notebook_name: str,
    generated_at: datetime,
    *,
    illustrations_url_base: str = "/api/notebook-illustration/",
    poll_url: Optional[str] = None,
) -> str:
    """Render a stored infographic artifact as a self-contained HTML page.

    v2 content (JSON, see is_infographic_v2) goes to render_infographic_v2;
    `poll_url` (set by the route while an illustration job runs) makes the
    page poll for illustrations. Legacy markdown keeps the poster renderer
    below. Corrupt v2 JSON degrades to the legacy fallback card with a
    Dutch notice instead of raising.
    """
    if is_infographic_v2(markdown):
        try:
            data = extract_infographic(markdown)
        except ValueError as exc:
            fallback_md = (
                f"# {(title or 'Infographic').strip()}\n\n"
                f"Deze inhoud kon niet als infographic worden gerenderd ({exc}).\n"
            )
            return _render_legacy(title, fallback_md, notebook_name, generated_at)
        return render_infographic_v2(
            data, notebook_name, generated_at,
            illustrations_url_base=illustrations_url_base, poll_url=poll_url,
        )
    return _render_legacy(title, markdown, notebook_name, generated_at)


def _render_legacy(title, markdown, notebook_name, generated_at) -> str:
    """Render legacy infographic markdown as a self-contained HTML poster.

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

    return _TEMPLATE.format(
        title=html.escape(effective_title),
        grid_html=_render_grid_html(parsed),
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


# ---------------------------------------------------------------------------
# v2: JSON content model (spec 2026-09-03 Deel A)
# ---------------------------------------------------------------------------

MAX_ILLUSTRATIONS = 5
BLOCK_TYPES = frozenset({"column", "steps", "icon_card", "hero", "comparison", "key_numbers"})
COLUMN_CHILD_TYPES = frozenset({"steps", "icon_card", "key_numbers"})
# Whole-word, case-insensitive: an illustration prompt asking for text in
# the image defeats the whole point of v2 (text stays in HTML).
TEXT_IN_IMAGE_WORDS = ("text", "label", "caption", "words", "letters")
_TEXT_IN_IMAGE_RE = re.compile(r"\b(" + "|".join(TEXT_IN_IMAGE_WORDS) + r")\b", re.IGNORECASE)
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
MIN_BLOCKS, MAX_BLOCKS = 5, 8
MAX_COLUMNS = 2


def is_infographic_v2(content: Optional[str]) -> bool:
    """True when stored content is the v2 JSON model (bare object or ```json fence).

    The fence may be preceded by prose (the model sometimes prefaces the
    fenced block with a sentence), so this searches for the fence anywhere
    in the content rather than anchoring at the start of the string — same
    approach as extract_infographic's own _JSON_FENCE_RE.search.
    """
    s = (content or "").strip()
    if s.startswith("{"):
        return True
    m = _JSON_FENCE_RE.search(content or "")
    return bool(m and m.group(1).strip().startswith("{"))


def _req_str(obj: dict, key: str, where: str, *, max_len: int, required: bool = True) -> Optional[str]:
    val = obj.get(key)
    if val is None or (isinstance(val, str) and not val.strip()):
        if required:
            raise ValueError(f'{where}: veld "{key}" ontbreekt of is leeg')
        return None
    if not isinstance(val, str):
        raise ValueError(f'{where}: veld "{key}" moet een string zijn')
    val = val.strip()
    if len(val) > max_len:
        raise ValueError(f'{where}: veld "{key}" is te lang (maximaal {max_len} tekens)')
    return val


def _validate_block(raw: object, where: str, *, nested: bool, seen_ids: set) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{where} is geen object")
    block_id = _req_str(raw, "id", where, max_len=40)
    if not _SLUG_RE.match(block_id):
        raise ValueError(f'{where}: veld "id" moet een slug zijn (a-z, 0-9, - of _)')
    if block_id in seen_ids:
        raise ValueError(f'{where}: id "{block_id}" is niet uniek')
    seen_ids.add(block_id)
    btype = _req_str(raw, "type", where, max_len=20)
    if btype not in BLOCK_TYPES:
        raise ValueError(f'{where}: onbekend type "{btype}"')
    if nested and btype not in COLUMN_CHILD_TYPES:
        raise ValueError(f'{where}: type "{btype}" mag niet binnen een column staan')
    heading = _req_str(raw, "heading", where, max_len=60)
    icon = raw.get("icon")
    icon = icon if isinstance(icon, str) and icon in _ICONS else None
    prompt = _req_str(raw, "illustration_prompt", where, max_len=200, required=False)
    if prompt and _TEXT_IN_IMAGE_RE.search(prompt):
        raise ValueError(
            f'{where}: illustration_prompt mag geen tekst in beeld vragen '
            f'(geen {", ".join(TEXT_IN_IMAGE_WORDS)})'
        )
    block = {"id": block_id, "type": btype, "heading": heading, "icon": icon,
             "illustration_prompt": prompt}

    if btype == "column":
        block["subheading"] = _req_str(raw, "subheading", where, max_len=120)
        children = raw.get("children")
        if not isinstance(children, list) or not 2 <= len(children) <= 3:
            raise ValueError(f'{where}: veld "children" moet 2 tot 3 sub-blokken bevatten')
        block["children"] = [
            _validate_block(c, f"{where} > child {i}", nested=True, seen_ids=seen_ids)
            for i, c in enumerate(children, 1)
        ]
    elif btype == "steps":
        items = raw.get("items")
        if not isinstance(items, list) or not 2 <= len(items) <= 5:
            raise ValueError(f"{where}: steps heeft 2 tot 5 items nodig")
        block["items"] = [
            {"label": _req_str(it, "label", f"{where} stap {i}", max_len=60),
             "text": _req_str(it, "text", f"{where} stap {i}", max_len=120)}
            if isinstance(it, dict) else _raise(f"{where} stap {i} is geen object")
            for i, it in enumerate(items, 1)
        ]
    elif btype == "icon_card":
        block["text"] = _req_str(raw, "text", where, max_len=200)
    elif btype == "hero":
        block["text"] = _req_str(raw, "text", where, max_len=240)
    elif btype == "comparison":
        rows = raw.get("rows")
        if not isinstance(rows, list) or not 2 <= len(rows) <= 4:
            raise ValueError(f"{where}: comparison heeft 2 tot 4 rows nodig")
        cleaned_rows = []
        for i, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                raise ValueError(f"{where} row {i} is geen object")
            ratio = row.get("ratio")
            if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or not 0 <= ratio <= 1:
                raise ValueError(f"{where} row {i}: ratio moet een getal tussen 0 en 1 zijn")
            cleaned_rows.append({
                "label": _req_str(row, "label", f"{where} row {i}", max_len=60),
                "value": _req_str(row, "value", f"{where} row {i}", max_len=80),
                "ratio": float(ratio),
            })
        block["rows"] = cleaned_rows
    elif btype == "key_numbers":
        items = raw.get("items")
        if not isinstance(items, list) or not 3 <= len(items) <= 5:
            raise ValueError(f"{where}: key_numbers heeft 3 tot 5 items nodig")
        cleaned_items = []
        for i, it in enumerate(items, 1):
            if not isinstance(it, dict):
                raise ValueError(f"{where} item {i} is geen object")
            label = _req_str(it, "label", f"{where} item {i}", max_len=80)
            if len(label.split()) > 8:
                raise ValueError(f"{where} item {i}: label maximaal 8 woorden")
            cleaned_items.append({"number": _req_str(it, "number", f"{where} item {i}", max_len=40),
                                  "label": label})
        block["items"] = cleaned_items
    return block


def _raise(msg: str):
    raise ValueError(msg)


def extract_infographic(content: str) -> dict:
    """Parse + validate v2 JSON. Raises ValueError (Dutch) on any schema miss.

    Registered as the `infographic` validator in src/notebook_artifacts.py,
    so the message is fed back to the model on retry — same contract as
    extract_slide_deck. Returns a cleaned dict (stripped strings, unknown
    icons dropped) with an `illustrations` map (block_id -> filename) that
    the illustration job fills in later (src/notebook_illustrations.py).
    """
    m = _JSON_FENCE_RE.search(content or "")
    raw = (m.group(1) if m else (content or "")).strip()
    if not raw:
        raise ValueError("geen JSON gevonden in het antwoord")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"ongeldige JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("JSON is geen object")

    title = _req_str(data, "title", "infographic", max_len=80)
    subtitle = _req_str(data, "subtitle", "infographic", max_len=120, required=False)
    takeaway = _req_str(data, "takeaway", "infographic", max_len=240)
    blocks = data.get("blocks")
    if not isinstance(blocks, list) or not MIN_BLOCKS <= len(blocks) <= MAX_BLOCKS:
        raise ValueError(f'veld "blocks" moet {MIN_BLOCKS} tot {MAX_BLOCKS} blokken bevatten')
    seen: set = set()
    cleaned = [_validate_block(b, f"blok {i}", nested=False, seen_ids=seen)
               for i, b in enumerate(blocks, 1)]
    heroes = sum(1 for b in cleaned if b["type"] == "hero")
    if heroes != 1:
        raise ValueError(f"precies één blok van type hero verwacht (gevonden: {heroes})")
    columns = sum(1 for b in cleaned if b["type"] == "column")
    if columns < 1 or columns > MAX_COLUMNS:
        raise ValueError(f"1 tot {MAX_COLUMNS} blokken van type column verwacht (gevonden: {columns})")

    illustrations = data.get("illustrations")
    if not isinstance(illustrations, dict):
        illustrations = {}
    illustrations = {k: v for k, v in illustrations.items()
                     if isinstance(k, str) and isinstance(v, str)}
    return {"title": title, "subtitle": subtitle, "takeaway": takeaway,
            "blocks": cleaned, "illustrations": illustrations}


def iter_blocks(data: dict) -> List[dict]:
    """All blocks in document order; a column is followed by its children."""
    out: List[dict] = []
    for b in data.get("blocks", []):
        out.append(b)
        out.extend(b.get("children", []))
    return out


# ---------------------------------------------------------------------------
# v2 rendering (spec Deel C)
# ---------------------------------------------------------------------------

_TEMPLATE_V2 = """\
<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
:root {{
  --font-display: 'Charter', 'Iowan Old Style', Georgia, serif;
  --font-body: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --bg: #fbf9f4; --bg-surface: #ffffff; --border: rgba(0,0,0,0.08);
  --text: #1a1817; --text-dim: #5a5651; --text-muted: #8a8580;
  --accent: #b8543a; --gold: #c9952e; --gold-bg: rgba(201,149,46,0.09);
  --radius: 12px; --shadow-sm: 0 1px 3px rgba(0,0,0,0.05);
}}
/* Always-light, like every notebook viewer (#98). */
body {{ font-family: var(--font-body); background: var(--bg); color: var(--text); line-height: 1.5; font-size: 15px; -webkit-font-smoothing: antialiased; }}
.ig2-wrap {{ max-width: 1280px; margin: 0 auto; padding: 2rem 1.5rem 2.5rem; }}
.ig2-head {{ text-align: center; padding: 0 0 1.6rem; }}
.ig2-head-label {{ text-transform: uppercase; letter-spacing: 0.28em; font-size: 0.66rem; font-weight: 600; color: var(--accent); margin-bottom: 0.7rem; }}
.ig2-head h1 {{ font-family: var(--font-display); font-size: clamp(1.6rem, 3.2vw, 2.4rem); font-weight: 700; line-height: 1.12; letter-spacing: -0.02em; max-width: 60ch; margin: 0 auto; }}
.ig2-head p {{ color: var(--text-dim); margin-top: 0.5rem; font-size: 0.98rem; }}

/* Desktop: [column] [hero + rest] [column]. Wrappers are flex columns so
   `order` (document index) keeps document order inside each wrapper. */
.ig2-grid {{ display: grid; grid-template-columns: 1fr 1.25fr 1fr; gap: 1.5rem; align-items: start; }}
.ig2-grid.ig2-grid--one {{ grid-template-columns: 1fr 1.25fr; }}
.ig2-col, .ig2-center {{ display: flex; flex-direction: column; gap: 1.25rem; min-width: 0; }}
.ig2-block {{ order: var(--o, 0); min-width: 0; background: var(--bg-surface); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow-sm); padding: 1.1rem 1.15rem 1.2rem; }}
.ig2-block h2 {{ font-size: 0.86rem; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; margin-bottom: 0.45rem; }}
.ig2-block p {{ color: var(--text-dim); font-size: 0.88rem; }}
.ig2-art {{ display: flex; justify-content: center; margin-bottom: 0.75rem; }}
.ig2-icon {{ width: 58px; height: 58px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; background: var(--pc-tint); color: var(--pc); }}
.ig2-icon svg {{ width: 28px; height: 28px; }}
.ig2-img {{ width: 100%; max-width: 320px; height: auto; border-radius: 10px; display: block; opacity: 1; transition: opacity 0.4s ease; }}
.ig2-img--fade {{ opacity: 0; }}

.ig2-column {{ background: transparent; border: 0; box-shadow: none; padding: 0; }}
.ig2-column-head {{ padding: 0 0.25rem 0.75rem; text-align: center; }}
.ig2-column-head h2 {{ color: var(--pc); }}
.ig2-column-head p {{ font-size: 0.82rem; }}
.ig2-column-body {{ display: flex; flex-direction: column; gap: 1rem; }}

.ig2-steps {{ list-style: none; counter-reset: none; position: relative; margin-top: 0.3rem; }}
.ig2-steps li {{ display: grid; grid-template-columns: 28px 1fr; gap: 0.6rem; position: relative; padding-bottom: 0.9rem; }}
.ig2-steps li::before {{ content: ""; position: absolute; left: 13px; top: 28px; bottom: 0; width: 2px; background: var(--pc-tint); }}
.ig2-steps li:last-child::before {{ display: none; }}
.ig2-steps li:last-child {{ padding-bottom: 0; }}
.ig2-step-n {{ width: 28px; height: 28px; border-radius: 50%; background: var(--pc); color: #fff; font-size: 0.78rem; font-weight: 700; display: inline-flex; align-items: center; justify-content: center; }}
.ig2-steps strong {{ display: block; font-size: 0.86rem; }}

.ig2-hero {{ text-align: center; padding: 1.4rem 1.4rem 1.5rem; }}
.ig2-hero .ig2-img {{ max-width: 100%; }}
.ig2-hero .ig2-icon {{ width: 96px; height: 96px; }}
.ig2-hero .ig2-icon svg {{ width: 44px; height: 44px; }}
.ig2-hero h2 {{ font-family: var(--font-display); font-size: 1.3rem; text-transform: none; letter-spacing: -0.01em; }}
.ig2-hero p {{ font-size: 0.98rem; color: var(--text); }}

.ig2-cmp-row {{ display: grid; grid-template-columns: minmax(64px, auto) 1fr auto; gap: 0.5rem 0.8rem; align-items: center; margin-bottom: 0.6rem; }}
.ig2-cmp-label {{ font-size: 0.7rem; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; text-align: right; }}
.ig2-cmp-track {{ height: 16px; border-radius: 8px; background: var(--bg); border: 1px solid var(--border); overflow: hidden; }}
.ig2-cmp-fill {{ height: 100%; border-radius: 8px; background: var(--pc); }}
.ig2-cmp-value {{ font-size: 0.78rem; color: var(--text-dim); white-space: nowrap; }}

.ig2-stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(96px, 1fr)); gap: 0.6rem; margin-top: 0.4rem; }}
.ig2-stat {{ text-align: center; padding: 0.6rem 0.4rem; border-radius: 10px; background: var(--pc-tint); }}
.ig2-stat-n {{ font-family: var(--font-display); font-size: 1.35rem; font-weight: 700; color: var(--pc); line-height: 1.1; }}
.ig2-stat-l {{ font-size: 0.72rem; color: var(--text-dim); margin-top: 0.2rem; }}

.ig-takeaway {{ margin-top: 1.6rem; padding: 0.9rem 1.3rem; border-left: 3px solid var(--gold); background: var(--gold-bg); border-radius: 0 var(--radius) var(--radius) 0; font-family: var(--font-display); font-style: italic; font-size: 0.98rem; text-align: center; }}
.ig2-meta {{ text-align: center; font-size: 0.75rem; color: var(--text-muted); margin-top: 2rem; padding-top: 1rem; border-top: 1px solid var(--border); }}

/* Mobile: one column in document order. Wrappers dissolve (display:
   contents) so `order` sorts every block globally. */
@media (max-width: 959px) {{
  .ig2-grid, .ig2-grid.ig2-grid--one {{ display: flex; flex-direction: column; gap: 1rem; align-items: stretch; }}
  .ig2-col, .ig2-center {{ display: contents; }}
  .ig2-wrap {{ padding: 1.25rem 0.9rem 2rem; }}
  .ig2-cmp-row {{ grid-template-columns: 1fr auto; }}
  .ig2-cmp-label {{ grid-column: 1 / -1; text-align: left; }}
}}
@media print {{ body {{ background: #fff !important; }} .ig2-block {{ box-shadow: none; }} }}
</style>
</head>
<body>
<div class="ig2-wrap"{pending_attrs}>
  <div class="ig2-head">
    <div class="ig2-head-label">Ithaka &mdash; Infographic</div>
    <h1>{title}</h1>{subtitle_html}
  </div>
  {grid_html}
  <div class="ig-takeaway">{takeaway}</div>
  <div class="ig2-meta">{notebook_name} &middot; {date}</div>
</div>
{script}
</body>
</html>
"""

# Not run through str.format — braces are literal. Polls the status endpoint
# every 3 s (max 120 s) and swaps each icon for its <img> with a short fade.
_VIEWER_SCRIPT = """<script>
(function () {
  var root = document.querySelector('.ig2-wrap[data-illustrations="pending"]');
  if (!root) return;
  var url = root.getAttribute('data-poll-url');
  if (!url) return;
  var INTERVAL = 3000, MAX_MS = 120000, started = Date.now();
  function stop() { root.removeAttribute('data-illustrations'); }
  function apply(map) {
    Object.keys(map || {}).forEach(function (id) {
      var slot = root.querySelector('.ig2-art[data-block-id="' + CSS.escape(id) + '"]');
      if (!slot || slot.getAttribute('data-done') === '1') return;
      slot.setAttribute('data-done', '1');
      var img = new Image();
      img.className = 'ig2-img ig2-img--fade';
      img.alt = '';
      img.onload = function () {
        slot.innerHTML = '';
        slot.appendChild(img);
        requestAnimationFrame(function () { img.classList.remove('ig2-img--fade'); });
      };
      img.src = map[id];
    });
  }
  function tick() {
    fetch(url, { credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) { throw new Error('http ' + r.status); } return r.json(); })
      .then(function (d) {
        if (d && d.illustrations) apply(d.illustrations);
        if (!d || d.status !== 'running' || Date.now() - started > MAX_MS) { stop(); return; }
        setTimeout(tick, INTERVAL);
      })
      .catch(function () {
        if (Date.now() - started > MAX_MS) { stop(); return; }
        setTimeout(tick, INTERVAL);
      });
  }
  setTimeout(tick, INTERVAL);
})();
</script>"""


def _icon_body(block: dict) -> str:
    key = block.get("icon")
    return _ICONS[key] if key in _ICONS else _pick_icon(block.get("heading", ""))


def _art_html(block: dict, illustrations: dict, url_base: str) -> str:
    """Illustration slot: <img> when the job delivered one, else icon circle."""
    filename = illustrations.get(block["id"])
    if filename:
        src = html.escape(url_base + filename, quote=True)
        inner = f'<img class="ig2-img" src="{src}" loading="lazy" alt="">'
    else:
        inner = f'<span class="ig2-icon"><svg {_ICON_ATTRS}>{_icon_body(block)}</svg></span>'
    return f'<div class="ig2-art" data-block-id="{html.escape(block["id"], quote=True)}">{inner}</div>'


def _block_body_html(block: dict) -> str:
    t = block["type"]
    if t == "steps":
        items = "".join(
            f'<li><span class="ig2-step-n">{i}</span><div><strong>{html.escape(it["label"])}</strong>'
            f'<p>{html.escape(it["text"])}</p></div></li>'
            for i, it in enumerate(block["items"], 1)
        )
        return f'<ol class="ig2-steps">{items}</ol>'
    if t in ("icon_card", "hero"):
        return f"<p>{html.escape(block['text'])}</p>"
    if t == "comparison":
        return "".join(
            '<div class="ig2-cmp-row">'
            f'<div class="ig2-cmp-label">{html.escape(r["label"])}</div>'
            f'<div class="ig2-cmp-track"><div class="ig2-cmp-fill" style="width:{round(r["ratio"] * 100)}%"></div></div>'
            f'<div class="ig2-cmp-value">{html.escape(r["value"])}</div>'
            "</div>"
            for r in block["rows"]
        )
    if t == "key_numbers":
        return '<div class="ig2-stats">' + "".join(
            f'<div class="ig2-stat"><div class="ig2-stat-n">{html.escape(it["number"])}</div>'
            f'<div class="ig2-stat-l">{html.escape(it["label"])}</div></div>'
            for it in block["items"]
        ) + "</div>"
    return ""


def _block_html(block: dict, order: int, palette_index: int, illustrations: dict, url_base: str) -> str:
    color, tint = _PALETTE[palette_index % len(_PALETTE)]
    style = f"--o:{order};--pc:{color};--pc-tint:{tint}"
    t = block["type"]
    if t == "column":
        children = "".join(
            _block_html(c, order, palette_index + 1 + i, illustrations, url_base)
            for i, c in enumerate(block["children"])
        )
        return (
            f'<section class="ig2-block ig2-column" style="{style}">'
            f'<div class="ig2-column-head">{_art_html(block, illustrations, url_base)}'
            f'<h2>{html.escape(block["heading"])}</h2><p>{html.escape(block["subheading"])}</p></div>'
            f'<div class="ig2-column-body">{children}</div></section>'
        )
    cls = {"hero": "ig2-hero", "icon_card": "ig2-card", "steps": "ig2-card ig2-card--steps",
           "comparison": "ig2-card ig2-card--cmp", "key_numbers": "ig2-card ig2-card--stats"}[t]
    return (
        f'<section class="ig2-block {cls}" style="{style}">'
        f'{_art_html(block, illustrations, url_base)}<h2>{html.escape(block["heading"])}</h2>'
        f'{_block_body_html(block)}</section>'
    )


def render_infographic_v2(data: dict, notebook_name: str, generated_at: datetime, *,
                          illustrations_url_base: str, poll_url: Optional[str]) -> str:
    """Render a validated v2 dict (from extract_infographic) as the poster page."""
    illustrations = data.get("illustrations") or {}
    blocks = data["blocks"]
    columns = [b for b in blocks if b["type"] == "column"]
    hero = next(b for b in blocks if b["type"] == "hero")
    rest = [b for b in blocks if b["type"] not in ("column", "hero")]

    order = {b["id"]: i for i, b in enumerate(blocks)}
    palette = {b["id"]: i * 3 for i, b in enumerate(blocks)}

    def _render(b):
        return _block_html(b, order[b["id"]], palette[b["id"]], illustrations, illustrations_url_base)

    left = _render(columns[0])
    right = _render(columns[1]) if len(columns) > 1 else ""
    center = _render(hero) + "".join(_render(b) for b in rest)
    grid_cls = "ig2-grid" if right else "ig2-grid ig2-grid--one"
    grid_html = (
        f'<div class="{grid_cls}"><div class="ig2-col">{left}</div>'
        f'<div class="ig2-center">{center}</div>'
        + (f'<div class="ig2-col">{right}</div>' if right else "")
        + "</div>"
    )

    pending_attrs = ""
    script = ""
    if poll_url:
        pending_attrs = f' data-illustrations="pending" data-poll-url="{html.escape(poll_url, quote=True)}"'
        script = _VIEWER_SCRIPT
    subtitle_html = f"<p>{html.escape(data['subtitle'])}</p>" if data.get("subtitle") else ""
    return _TEMPLATE_V2.format(
        title=html.escape(data["title"]),
        subtitle_html=subtitle_html,
        grid_html=grid_html,
        takeaway=html.escape(data["takeaway"]),
        notebook_name=html.escape(notebook_name or ""),
        date=html.escape(generated_at.strftime("%d-%m-%Y")),
        pending_attrs=pending_attrs,
        script=script,
    )
