# src/notebook_flashcards.py
"""Flip-card renderer for the notebook "flashcards" artifact.

Same shape as src/notebook_infographic.py: the artifact's markdown (one
"### front" heading per card, answer paragraphs below it — see the
generation prompt in src/notebook_artifacts.py) is parsed leniently and
rendered as a self-contained HTML page with clickable flip cards. A
separate compact template instead of the shared editorial report because
flashcards are an interaction (click to reveal), not a long-form read.

Never raises on malformed/partial markdown: no cards, prose-only input or
a missing title all still produce a valid, non-empty page.
"""
import html
import re
from datetime import datetime
from typing import Optional

_H1_RE = re.compile(r"^#\s+(.+?)\s*$")
_CARD_RE = re.compile(r"^###\s+(.+?)\s*$")


def _parse_flashcards_markdown(markdown: str) -> dict:
    """Parse to {"title": str, "cards": [{"front", "back"}]}.

    Lenient: preamble prose is ignored, a card heading without any body is
    dropped (nothing to reveal), the back keeps blank-line paragraph breaks.
    """
    title = ""
    cards: list[dict] = []
    current_front: Optional[str] = None
    current_back: list[str] = []

    def _flush():
        nonlocal current_front, current_back
        if current_front is not None:
            back = "\n".join(current_back).strip()
            if back:
                cards.append({"front": current_front, "back": back})
        current_front, current_back = None, []

    for line in (markdown or "").splitlines():
        m = _CARD_RE.match(line)
        if m:
            _flush()
            current_front = m.group(1)
            continue
        m = _H1_RE.match(line)
        if m and not title and current_front is None:
            title = m.group(1)
            continue
        if current_front is not None:
            current_back.append(line)
    _flush()
    return {"title": title, "cards": cards}


def _render_back(back: str) -> str:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", back) if p.strip()]
    return "".join(f"<p>{html.escape(p)}</p>" for p in paragraphs)


# Dark, self-contained, zero external resources (matches the infographic
# template's constraint: the page must render offline and leak nothing).
_TEMPLATE = """<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{
    --bg:#fbf9f4; --fg:#1a1817; --panel:#ffffff; --border:rgba(0,0,0,0.08);
    --accent:#b8543a; --accent-light:#d97a5e; --accent-bg:rgba(184,84,58,0.06);
    --font-display:'Charter','Iowan Old Style',Georgia,serif;
    --font-body:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg);
         font-family:var(--font-body); padding:24px; }}
  header {{ max-width:960px; margin:0 auto 20px; text-align:center; }}
  .fc-eyebrow {{ text-transform:uppercase; letter-spacing:0.22em; font-size:0.68rem;
                 font-weight:600; color:var(--accent); margin-bottom:0.6rem; }}
  h1 {{ font-family:var(--font-display); font-size:clamp(1.3rem,3.5vw,1.8rem);
       margin:0 0 4px; font-weight:700; letter-spacing:-0.01em; }}
  .fc-meta {{ font-size:12px; color:#8a8580; }}
  .fc-grid {{ max-width:960px; margin:0 auto; display:grid;
              grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:14px; }}
  .fc-card {{ position:relative; min-height:150px; cursor:pointer; perspective:900px; }}
  .fc-inner {{ position:absolute; inset:0; transition:transform .45s; transform-style:preserve-3d; }}
  .fc-card.flipped .fc-inner {{ transform:rotateY(180deg); }}
  .fc-face {{ position:absolute; inset:0; display:flex; flex-direction:column; justify-content:center;
              padding:16px; border:1px solid var(--border); border-radius:10px;
              background:var(--panel); backface-visibility:hidden; overflow-y:auto;
              box-shadow:0 1px 3px rgba(0,0,0,0.05); }}
  .fc-front {{ font-weight:600; }}
  .fc-front::before {{ content:""; position:absolute; top:0; left:0; right:0; height:3px;
                       background:var(--accent); border-radius:10px 10px 0 0; }}
  .fc-front::after {{ content:""; position:absolute; left:16px; right:16px; bottom:10px;
                      border-bottom:2px solid var(--accent); opacity:.4; }}
  .fc-back {{ transform:rotateY(180deg); font-size:13px; }}
  .fc-back::before {{ content:""; position:absolute; top:0; left:0; right:0; height:3px;
                      background:var(--accent-light); border-radius:10px 10px 0 0; }}
  .fc-back p {{ margin:0 0 8px; }}
  .fc-hint {{ max-width:960px; margin:0 auto 14px; font-size:12px;
              color:#5a5651; text-align:center; }}
  .fc-empty {{ max-width:960px; margin:40px auto; color:#5a5651; }}
  footer {{ max-width:960px; margin:28px auto 0; font-size:11px;
             color:#8a8580; text-align:center;
             border-top:1px solid var(--border); padding-top:1rem; }}
  @media (max-width:480px) {{ body {{ padding:12px; }} }}
</style>
</head>
<body>
<header>
  <div class="fc-eyebrow">Ithaka &mdash; Flashcards</div>
  <h1>{title}</h1>
  <div class="fc-meta">{notebook_name} &middot; {count_label} &middot; {date}</div>
</header>
{hint_html}
{cards_html}
<footer>Ithaka Notebooks</footer>
<script>
  document.querySelectorAll('.fc-card').forEach(function (card) {{
    card.addEventListener('click', function () {{ card.classList.toggle('flipped'); }});
  }});
</script>
</body>
</html>
"""


def generate_flashcards(
    title: Optional[str],
    markdown: str,
    notebook_name: str,
    generated_at: datetime,
) -> str:
    """Render a flashcards artifact's markdown as a self-contained HTML page.

    `title` is only a fallback: a "# " heading in `markdown` wins as the
    page title, matching generate_infographic's title precedence.
    """
    parsed = _parse_flashcards_markdown(markdown)
    effective_title = parsed["title"] or (title or "").strip() or "Flashcards"

    if parsed["cards"]:
        cards_html = '<div class="fc-grid">' + "".join(
            '<div class="fc-card" role="button" tabindex="0">'
            '<div class="fc-inner">'
            f'<div class="fc-face fc-front">{html.escape(c["front"])}</div>'
            f'<div class="fc-face fc-back">{_render_back(c["back"])}</div>'
            "</div></div>"
            for c in parsed["cards"]
        ) + "</div>"
        hint_html = '<div class="fc-hint">Klik op een kaart om het antwoord te zien.</div>'
    else:
        cards_html = '<div class="fc-empty">Geen kaarten gevonden in dit artifact.</div>'
        hint_html = ""

    count = len(parsed["cards"])
    count_label = f"{count} kaarten" if count != 1 else "1 kaart"
    return _TEMPLATE.format(
        title=html.escape(effective_title),
        notebook_name=html.escape(notebook_name or ""),
        count_label=html.escape(count_label),
        date=html.escape(generated_at.strftime("%B %d, %Y")),
        hint_html=hint_html,
        cards_html=cards_html,
    )


# ---------------------------------------------------------------------------
# Generation-time format validation
# ---------------------------------------------------------------------------

_H2_HEADING_RE = re.compile(r"^##\s+\S")
_MIN_CARDS = 3


def validate_flashcards_markdown(markdown: str) -> None:
    """Raise ValueError (Dutch, fed back to the model on retry) on a format miss.

    Registered in src/notebook_artifacts.py's _KIND_VALIDATORS. A 2026-08-22
    production artifact showed a model answering with "## " chapters and a
    single "### " heading — the deck then rendered as one lonely card. The
    prompt asks for 10-15 cards; the floor here is deliberately lower
    (_MIN_CARDS) so thin sources still pass, while chapter-prose does not.
    """
    problems: list[str] = []
    parsed = _parse_flashcards_markdown(markdown)
    headings = sum(1 for line in (markdown or "").splitlines() if _CARD_RE.match(line))
    if headings < _MIN_CARDS:
        problems.append(
            f"slechts {headings} '### '-kaartkoppen gevonden; maak er 10 tot 15, "
            "elke kaart als '### <voorzijde>' met de achterzijde als alinea's eronder"
        )
    elif len(parsed["cards"]) < _MIN_CARDS:
        problems.append(
            "kaartkoppen zonder achterzijde: zet onder elke '### <voorzijde>' "
            "één of twee alinea's met het antwoord"
        )
    if any(_H2_HEADING_RE.match(line) for line in (markdown or "").splitlines()):
        problems.append(
            "'## '-koppen zijn niet toegestaan; gebruik uitsluitend '# ' voor de "
            "titel en '### ' per kaart"
        )
    if problems:
        raise ValueError(
            "flashcards-structuur klopt niet: " + "; ".join(problems)
            + ". Lever exact de gevraagde markdownstructuur."
        )
