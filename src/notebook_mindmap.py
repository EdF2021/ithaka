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

import html
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
    # (indent, node) stack; children attach to the deepest shallower entry.
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
            # A second top-level node outside the root is prompt-invalid;
            # skip it rather than corrupting the tree.
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
# Viewer rendering
# ---------------------------------------------------------------------------

# Dark, self-contained, zero external resources — same constraint and token
# set as the flashcards viewer (src/notebook_flashcards.py).
_TEMPLATE = """<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ --bg:#16181d; --fg:#e6e6e6; --panel:#1f2229; --border:#3a3f4b; --accent:#e06c75; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg);
         font-family:'Fira Code','Cascadia Code',ui-monospace,monospace; padding:24px; }}
  header {{ max-width:1100px; margin:0 auto 16px; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  .mm-meta {{ font-size:12px; opacity:.7; }}
  .mm-controls {{ max-width:1100px; margin:0 auto 18px; display:flex; gap:10px; }}
  .mm-controls button {{ background:var(--panel); color:var(--fg); border:1px solid var(--border);
                         border-radius:8px; padding:6px 12px; font:inherit; font-size:12px; cursor:pointer; }}
  .mm-controls button:hover {{ border-color:var(--accent); }}
  .mm-tree {{ max-width:1100px; margin:0 auto; }}
  .mm-tree ul {{ list-style:none; margin:0; padding-left:26px; border-left:1px solid var(--border); }}
  .mm-tree > ul {{ border-left:none; padding-left:0; }}
  .mm-tree li {{ margin:6px 0; position:relative; }}
  .mm-node {{ background:var(--panel); color:var(--fg); border:1px solid var(--border);
              border-radius:8px; padding:7px 12px; font:inherit; font-size:13px;
              cursor:pointer; text-align:left; }}
  .mm-node:hover {{ border-color:var(--accent); }}
  .mm-node.mm-root {{ border-color:var(--accent); font-weight:600; font-size:15px; }}
  .mm-node .mm-caret {{ display:inline-block; width:1em; opacity:.7; }}
  .mm-leaf {{ cursor:default; opacity:.9; }}
  li.mm-collapsed > ul {{ display:none; }}
  li.mm-collapsed > .mm-node .mm-caret {{ transform:rotate(-90deg); }}
  .mm-caption {{ max-width:1100px; margin:18px auto 0; font-size:12px; opacity:.7; }}
  .mm-empty {{ max-width:1100px; margin:40px auto; opacity:.75; }}
  .mm-empty pre {{ background:var(--panel); border:1px solid var(--border); border-radius:8px;
                   padding:14px; overflow-x:auto; font-size:12px; white-space:pre-wrap; }}
  footer {{ max-width:1100px; margin:28px auto 0; font-size:11px; opacity:.55; }}
  @media (max-width:480px) {{ body {{ padding:12px; }} .mm-tree ul {{ padding-left:14px; }} }}
</style>
</head>
<body>
<header>
  <h1>{title}</h1>
  <div class="mm-meta">{notebook_name} &middot; Mindmap &middot; {date}</div>
</header>
{body_html}
<footer>Ithaka Notebooks</footer>
<script>
  document.querySelectorAll('.mm-node[data-toggle]').forEach(function (btn) {{
    btn.addEventListener('click', function () {{
      btn.parentElement.classList.toggle('mm-collapsed');
    }});
  }});
  var expand = document.getElementById('mm-expand');
  var collapse = document.getElementById('mm-collapse');
  if (expand) expand.addEventListener('click', function () {{
    document.querySelectorAll('li.mm-collapsed').forEach(function (li) {{ li.classList.remove('mm-collapsed'); }});
  }});
  if (collapse) collapse.addEventListener('click', function () {{
    document.querySelectorAll('.mm-node[data-toggle]').forEach(function (btn) {{
      if (!btn.classList.contains('mm-root')) btn.parentElement.classList.add('mm-collapsed');
    }});
  }});
</script>
</body>
</html>
"""


def _render_node(node: dict, is_root: bool = False) -> str:
    label = html.escape(node.get("label") or "")
    children = node.get("children") or []
    cls = "mm-node mm-root" if is_root else "mm-node"
    if children:
        btn = (f'<button type="button" class="{cls}" data-toggle="1">'
               f'<span class="mm-caret">&#9662;</span>{label}</button>')
        kids = "".join(f"<li>{_render_node(c)}</li>" for c in children)
        return f'{btn}<ul class="mm-children">{kids}</ul>'
    return f'<span class="{cls} mm-leaf">{label}</span>'


def generate_mindmap_viewer(
    title: Optional[str],
    markdown: str,
    notebook_name: str,
    generated_at: datetime,
) -> str:
    """Render the stored mermaid-mindmap markdown as an interactive viewer.

    Self-contained page (no external resources) with click-to-collapse
    branches and expand/collapse-all controls. Content without a parseable
    mindmap degrades to a message plus the escaped raw text — same posture
    as the slide-deck viewer's "Kon de slides niet lezen".
    """
    tree = parse_mermaid_mindmap(markdown)
    if tree is None:
        body_html = (
            '<div class="mm-empty"><p>Kon de mindmap niet lezen — de inhoud '
            "volgt niet het verwachte mermaid-formaat. Genereer het artifact "
            "opnieuw, of bekijk de ruwe inhoud hieronder.</p>"
            f"<pre>{html.escape((markdown or '').strip()[:4000])}</pre></div>"
        )
        caption_html = ""
        page_title = (title or "").strip() or "Mindmap"
    else:
        body_html = (
            '<div class="mm-controls">'
            '<button type="button" id="mm-expand">Alles uitklappen</button>'
            '<button type="button" id="mm-collapse">Alles inklappen</button>'
            "</div>"
            f'<div class="mm-tree"><ul><li>{_render_node(tree, is_root=True)}</li></ul></div>'
        )
        caption = (tree.get("caption") or "").strip()
        caption_html = f'<div class="mm-caption">{html.escape(caption)}</div>' if caption else ""
        body_html += caption_html
        page_title = (title or "").strip() or tree["label"]
    return _TEMPLATE.format(
        title=html.escape(page_title),
        notebook_name=html.escape(notebook_name or ""),
        date=html.escape(generated_at.strftime("%B %d, %Y")),
        body_html=body_html,
    )
