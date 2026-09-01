"""Mindmap artifact helpers: generation-time format validation + markmap viewer.

The mindmap generation prompt (src/notebook_artifacts.py) asks for exactly
one ```mermaid fence whose first line is "mindmap", a root((Topic)) node and
4-8 indented main branches. ``validate_mindmap_markdown`` below is
registered in src/notebook_artifacts.py's _KIND_VALIDATORS so
generate_artifact retries (with the error fed back) on such a format miss.

The viewer (``generate_mindmap_viewer``) renders the parsed tree using
**markmap** (https://markmap.js.org/) — an open-source library that
converts markdown headings into an interactive SVG mindmap with smooth
pan/zoom/collapse, matching the Google NotebookLM experience. The mermaid
mindmap content is converted to markdown headings (``#`` → root, ``##`` →
main branches, ``###`` → sub-branches) that markmap's autoloader renders
automatically. The page uses a light theme matching the other artifact
viewers.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime
from typing import Optional

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


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_mermaid_mindmap(content: str) -> Optional[dict]:
    """Parse the stored mermaid-mindmap markdown to a nested tree.

    Returns {"label", "children": [...], "caption"} (caption = the one-line
    plain-text sentence below the fence, if present), or None when there is
    no parseable ```mermaid mindmap fence — the viewer degrades on None.
    Hierarchy comes purely from indentation, mirroring what the generation
    prompt demands and what mermaid itself parses.
    """
    m = _MERMAID_FENCE_RE.search(content or "")
    if not m:
        return None
    lines = [l for l in m.group(1).splitlines() if l.strip()]
    if not lines or lines[0].strip() != "mindmap":
        return None
    root: Optional[dict] = None
    stack: list[tuple[int, dict]] = []
    for line in lines[1:]:
        indent = len(line) - len(line.lstrip())
        text = line.strip()
        rm = _ROOT_RE.match(line)
        if rm:
            text = rm.group(1).strip()
        node = {"label": text, "children": []}
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if not stack:
            if root is None:
                root = node
                stack.append((indent, node))
            continue
        stack[-1][1]["children"].append(node)
        stack.append((indent, node))
    if root is None:
        return None
    caption = ""
    for line in (content[m.end():] or "").splitlines():
        if line.strip():
            caption = line.strip()
            break
    root["caption"] = caption
    return root


# ---------------------------------------------------------------------------
# Mermaid → Markdown headings conversion (for markmap)
# ---------------------------------------------------------------------------

def _tree_to_markdown(tree: dict) -> str:
    """Convert the parsed mermaid-mindmap tree to markdown headings.

    Markmap uses ``#`` (h1) as the root, ``##`` (h2) for main branches,
    ``###`` for sub-branches, etc. The mermaid mindmap's indentation
    hierarchy maps directly to heading levels.
    """

    def _render(node: dict, level: int) -> list[str]:
        lines = [f"{'#' * level} {node['label']}"]
        for child in node.get("children", []):
            lines.extend(_render(child, level + 1))
        return lines

    return "\n".join(_render(tree, 1))


# ---------------------------------------------------------------------------
# Viewer rendering (markmap)
# ---------------------------------------------------------------------------

_TEMPLATE = """<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{
    --bg:#fbf9f4; --fg:#1a1817; --panel:#ffffff;
    --border:rgba(0,0,0,0.08); --accent:#b8543a;
    --font-display:'Charter','Iowan Old Style',Georgia,serif;
    --font-body:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin:0; background:var(--bg); color:var(--fg);
    font-family:var(--font-body); padding:24px; min-height:100vh;
  }}
  header {{ max-width:1100px; margin:0 auto 12px; text-align:center; }}
  h1 {{
    font-family:var(--font-display);
    font-size:clamp(1.3rem,3.5vw,1.8rem);
    margin:0 0 4px; font-weight:700; letter-spacing:-0.01em;
  }}
  .mm-meta {{ font-size:12px; color:#8a8580; }}
  .mm-controls {{
    max-width:1100px; margin:0 auto 14px;
    display:flex; gap:8px; justify-content:center;
  }}
  .mm-controls button {{
    background:var(--panel); color:var(--fg);
    border:1px solid var(--border); border-radius:8px;
    padding:6px 14px; font:inherit; font-size:12px; cursor:pointer;
  }}
  .mm-controls button:hover {{ border-color:var(--accent); color:var(--accent); }}
  .mm-canvas {{
    max-width:1100px; margin:0 auto;
    height:calc(100vh - 200px);
    min-height:400px;
    background:var(--panel);
    border:1px solid var(--border);
    border-radius:12px;
    overflow:hidden;
    position:relative;
  }}
  /* markmap fills the SVG inside .mm-canvas */
  .mm-canvas svg {{ width:100%; height:100%; }}
  .mm-canvas .markmap-node {{ cursor: pointer; }}
  .mm-click-hint {{
    max-width:1100px; margin:0 auto 10px; text-align:center;
    font-size:11px; color:#8a8580;
  }}
  .mm-caption {{
    max-width:1100px; margin:14px auto 0;
    text-align:center; font-size:13px; color:#5a5651;
    font-style:italic;
  }}
  .mm-empty {{
    max-width:1100px; margin:40px auto; color:#5a5651;
  }}
  .mm-empty pre {{
    background:var(--panel); border:1px solid var(--border);
    border-radius:8px; padding:14px; overflow-x:auto;
    font-size:12px; white-space:pre-wrap;
  }}
  footer {{
    max-width:1100px; margin:20px auto 0; font-size:11px;
    color:#8a8580; text-align:center;
  }}
  @media (max-width:480px) {{
    body {{ padding:12px; }}
    .mm-canvas {{ height:calc(100vh - 240px); }}
  }}
</style>
</head>
<body>
<header>
  <h1>{title}</h1>
  <div class="mm-meta">{notebook_name} &middot; Mindmap &middot; {date}</div>
</header>
{body_html}
<footer>Ithaka Notebooks</footer>
</body>
</html>
"""


def generate_mindmap_viewer(
    title: Optional[str],
    markdown: str,
    notebook_name: str,
    generated_at: datetime,
) -> str:
    """Render the stored mermaid-mindmap markdown as an interactive markmap.

    Uses the markmap-autoloader from cdn.jsdelivr.net to render markdown
    headings as an interactive SVG mindmap with smooth pan/zoom/collapse.
    The mermaid mindmap content is converted to markdown headings first.
    Content without a parseable mindmap degrades to a message plus the
    escaped raw text.
    """
    tree = parse_mermaid_mindmap(markdown)
    if tree is None:
        body_html = (
            '<div class="mm-empty"><p>Kon de mindmap niet lezen — de inhoud '
            "volgt niet het verwachte mermaid-formaat. Genereer het artifact "
            "opnieuw, of bekijk de ruwe inhoud hieronder.</p>"
            f"<pre>{html.escape((markdown or '').strip()[:4000])}</pre></div>"
        )
        page_title = (title or "").strip() or "Mindmap"
    else:
        controls = (
            '<div class="mm-controls">'
            '<button type="button" id="mm-fit">Passen</button>'
            '<button type="button" id="mm-expand-all">Alles uitklappen</button>'
            '<button type="button" id="mm-collapse-all">Alles inklappen</button>'
            "</div>"
            '<div class="mm-click-hint">Klik op een knoop om de AI-assistent te vragen over dat onderwerp</div>'
        )
        # Convert the mermaid tree to markdown headings for markmap.
        # html.escape the markdown so label-injected tags render as literal
        # text (markdown-it decodes the entities back to characters), then
        # JSON-embed it in the inline script — script content is raw text,
        # so entity-escaping alone can't be relied on there.
        md_content = _tree_to_markdown(tree)
        md_json = json.dumps(html.escape(md_content))

        canvas_html = (
            '<div class="mm-canvas">'
            '<svg id="mm-svg" class="markmap" style="width:100%;height:100%"></svg>'
            "</div>"
        )

        # Pinned self-contained bundles instead of markmap-autoloader: the
        # report-CSP keeps connect-src 'self', which blocks the autoloader's
        # runtime dependency fetches, so everything must arrive via script
        # tags (script-src allows cdn.jsdelivr.net).
        script_html = (
            '<script src="https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js" '
            'integrity="sha384-CjloA8y00+1SDAUkjs099PVfnY2KmDC2BZnws9kh8D/lX1s46w6EPhpXdqMfjK6i" '
            'crossorigin="anonymous"></script>'
            '<script src="https://cdn.jsdelivr.net/npm/markmap-lib@0.18.12/dist/browser/index.iife.min.js" '
            'integrity="sha384-mQgrLtILpAxOQmxspISBOEZByHJoRpKeG1+0/BEr0MO3hG1aBqcd4aJgrUoQGGE7" '
            'crossorigin="anonymous"></script>'
            '<script src="https://cdn.jsdelivr.net/npm/markmap-view@0.18.12/dist/browser/index.min.js" '
            'integrity="sha384-C8c2nsw+oZzYU5tGVHgXz8jVOoxdzionfzyQKFUQCqb/xLZgWZv2pnTamUfUiBSt" '
            'crossorigin="anonymous"></script>'
            "<script>"
            "window.addEventListener('load', function() {"
            f"  var mmMarkdown = {md_json};"
            "  var svg = document.getElementById('mm-svg');"
            "  if (!svg || !window.markmap || !window.markmap.Markmap) return;"
            "  var transformed = new window.markmap.Transformer().transform(mmMarkdown);"
            "  var mm = window.markmap.Markmap.create(svg, { autoFit: true }, transformed.root);"
            "  function setFoldAll(fold) {"
            "    var root = mm.state.data;"
            "    function walk(node) {"
            "      node.payload = Object.assign({}, node.payload, { fold: fold });"
            "      (node.children || []).forEach(walk);"
            "    }"
            "    (root.children || []).forEach(walk);"
            "    root.payload = Object.assign({}, root.payload, { fold: 0 });"
            "    mm.setData(root);"
            "    mm.fit();"
            "  }"
            "  var fitBtn = document.getElementById('mm-fit');"
            "  var expandBtn = document.getElementById('mm-expand-all');"
            "  var collapseBtn = document.getElementById('mm-collapse-all');"
            "  if (fitBtn) fitBtn.addEventListener('click', function() { mm.fit(); });"
            "  if (expandBtn) expandBtn.addEventListener('click', function() { setFoldAll(0); });"
            "  if (collapseBtn) collapseBtn.addEventListener('click', function() { setFoldAll(1); });"
            "  svg.addEventListener('click', function(e) {"
            "    if (!e.target || !e.target.closest) return;"
            "    if (e.target.closest('circle')) return;"
            "    var node = e.target.closest('g.markmap-node');"
            "    if (!node) return;"
            "    var textEl = node.querySelector('foreignObject div') || node.querySelector('text');"
            "    var label = textEl ? (textEl.textContent || '').trim() : '';"
            "    if (!label) return;"
            "    window.parent.postMessage({"
            "      type: 'nbws-mindmap-node-click',"
            "      label: label"
            "    }, '*');"
            "  });"
            "});"
            "</script>"
        )

        caption = (tree.get("caption") or "").strip()
        caption_html = f'<div class="mm-caption">{html.escape(caption)}</div>' if caption else ""
        body_html = controls + canvas_html + caption_html + script_html
        page_title = (title or "").strip() or tree["label"]
    return _TEMPLATE.format(
        title=html.escape(page_title),
        notebook_name=html.escape(notebook_name or ""),
        date=html.escape(generated_at.strftime("%B %d, %Y")),
        body_html=body_html,
    )