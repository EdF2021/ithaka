# src/notebook_slides.py
"""Slide-deck ("diapresentatie") artifact: JSON extraction/validation + the
standalone HTML slide viewer.

The generation prompt (src/notebook_artifacts.py, kind "slide_deck") asks
the model for exactly one ```json fence with {"title", "slides":[{"title",
"bullets", "notes"}]}. extract_slide_deck() is the strict counterpart used
as a post-generation validator (generate_artifact retries on ValueError,
mirroring the podcast script-format retry); generate_slide_deck() renders
the stored artifact markdown as a self-contained viewer page with
prev/next navigation, keyboard arrows and a speaker-notes toggle.
"""
import html
import json
import re
from datetime import datetime
from typing import Optional

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)

MAX_SLIDES = 20
MAX_BULLETS = 8


def extract_slide_deck(content: str) -> dict:
    """Parse and validate model output to {"title", "slides":[...]}.

    Raises ValueError (Dutch, fed back to the model on retry) when the JSON
    fence is missing, malformed, or the schema does not hold.
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
    title = data.get("title")
    slides = data.get("slides")
    if not isinstance(title, str) or not title.strip():
        raise ValueError('veld "title" ontbreekt of is leeg')
    if not isinstance(slides, list) or not slides:
        raise ValueError('veld "slides" ontbreekt of is leeg')
    if len(slides) > MAX_SLIDES:
        raise ValueError(f"te veel slides (maximaal {MAX_SLIDES})")
    cleaned = []
    for i, s in enumerate(slides, 1):
        if not isinstance(s, dict):
            raise ValueError(f"slide {i} is geen object")
        st = s.get("title")
        bullets = s.get("bullets", [])
        notes = s.get("notes", "")
        if not isinstance(st, str) or not st.strip():
            raise ValueError(f'slide {i}: veld "title" ontbreekt of is leeg')
        if not isinstance(bullets, list) or not all(isinstance(b, str) for b in bullets):
            raise ValueError(f'slide {i}: veld "bullets" moet een lijst strings zijn')
        if len(bullets) > MAX_BULLETS:
            raise ValueError(f"slide {i}: te veel bullets (maximaal {MAX_BULLETS})")
        if notes is None:
            notes = ""
        if not isinstance(notes, str):
            raise ValueError(f'slide {i}: veld "notes" moet een string zijn')
        cleaned.append({
            "title": st.strip(),
            "bullets": [b.strip() for b in bullets if b.strip()],
            "notes": notes.strip(),
        })
    return {"title": title.strip(), "slides": cleaned}


_TEMPLATE = """<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ --bg:#16181d; --fg:#e6e6e6; --panel:#1f2229; --border:#3a3f4b; --accent:#e06c75; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg); height:100vh; display:flex; flex-direction:column;
         font-family:'Fira Code','Cascadia Code',ui-monospace,monospace; }}
  header {{ padding:10px 18px; font-size:12px; opacity:.7; display:flex; justify-content:space-between; gap:12px; }}
  .sd-stage {{ flex:1; display:flex; align-items:center; justify-content:center; padding:0 18px 12px; min-height:0; }}
  .sd-slide {{ width:min(920px,100%); aspect-ratio:16/9; background:var(--panel); border:1px solid var(--border);
               border-radius:12px; padding:5% 6%; display:none; flex-direction:column; overflow:auto; }}
  .sd-slide.active {{ display:flex; }}
  .sd-slide h2 {{ margin:0 0 4%; font-size:clamp(18px,3.2vw,30px); border-bottom:2px solid var(--accent); padding-bottom:8px; }}
  .sd-slide li {{ font-size:clamp(13px,1.9vw,19px); margin-bottom:2.5%; }}
  .sd-notes {{ display:none; width:min(920px,100%); margin:0 auto 10px; padding:10px 14px; font-size:12px;
               border:1px dashed var(--border); border-radius:8px; opacity:.85; white-space:pre-wrap; }}
  body.show-notes .sd-notes.active {{ display:block; }}
  nav {{ display:flex; align-items:center; justify-content:center; gap:14px; padding:0 0 16px; }}
  nav button {{ background:var(--panel); color:var(--fg); border:1px solid var(--border); border-radius:8px;
                padding:7px 16px; font:inherit; font-size:13px; cursor:pointer; }}
  nav button:hover {{ border-color:var(--accent); }}
  .sd-counter {{ font-size:12px; opacity:.75; min-width:64px; text-align:center; }}
</style>
</head>
<body>
<header><span>{title} — {notebook_name}</span><span>{date}</span></header>
<div class="sd-stage">
{slides_html}
</div>
{notes_html}
<nav>
  <button type="button" id="sd-prev">&#8592; Vorige</button>
  <span class="sd-counter"><span id="sd-cur">1</span> / {count}</span>
  <button type="button" id="sd-next">Volgende &#8594;</button>
  <button type="button" id="sd-notes-toggle">Notities</button>
</nav>
<script>
  var idx = 0;
  var slides = document.querySelectorAll('.sd-slide');
  var notes = document.querySelectorAll('.sd-notes');
  function show(i) {{
    idx = Math.max(0, Math.min(slides.length - 1, i));
    slides.forEach(function (s, j) {{ s.classList.toggle('active', j === idx); }});
    notes.forEach(function (n, j) {{ n.classList.toggle('active', j === idx); }});
    document.getElementById('sd-cur').textContent = idx + 1;
  }}
  document.getElementById('sd-prev').addEventListener('click', function () {{ show(idx - 1); }});
  document.getElementById('sd-next').addEventListener('click', function () {{ show(idx + 1); }});
  document.getElementById('sd-notes-toggle').addEventListener('click', function () {{
    document.body.classList.toggle('show-notes');
  }});
  document.addEventListener('keydown', function (e) {{
    if (e.key === 'ArrowLeft') show(idx - 1);
    if (e.key === 'ArrowRight' || e.key === ' ') show(idx + 1);
  }});
  show(0);
</script>
</body>
</html>
"""


def generate_slide_deck(
    title: Optional[str],
    markdown: str,
    notebook_name: str,
    generated_at: datetime,
) -> str:
    """Render a slide_deck artifact's stored content as a viewer page.

    Lenient at view time (strictness lives in extract_slide_deck at
    generation time): unparseable stored content degrades to a one-slide
    page showing an explanatory message instead of raising.
    """
    try:
        deck = extract_slide_deck(markdown)
    except ValueError as e:
        deck = {
            "title": (title or "").strip() or "Diapresentatie",
            "slides": [{"title": "Kon de slides niet lezen",
                        "bullets": [f"Reden: {e}"], "notes": ""}],
        }
    effective_title = deck["title"] or (title or "").strip() or "Diapresentatie"

    slides_html = "".join(
        '<section class="sd-slide{act}"><h2>{t}</h2><ul>{lis}</ul></section>'.format(
            act=" active" if i == 0 else "",
            t=html.escape(s["title"]),
            lis="".join(f"<li>{html.escape(b)}</li>" for b in s["bullets"]),
        )
        for i, s in enumerate(deck["slides"])
    )
    notes_html = "".join(
        '<div class="sd-notes{act}">{n}</div>'.format(
            act=" active" if i == 0 else "",
            n=html.escape(s["notes"]) or "&mdash;",
        )
        for i, s in enumerate(deck["slides"])
    )
    return _TEMPLATE.format(
        title=html.escape(effective_title),
        notebook_name=html.escape(notebook_name or ""),
        date=html.escape(generated_at.strftime("%B %d, %Y")),
        count=len(deck["slides"]),
        slides_html=slides_html,
        notes_html=notes_html,
    )
