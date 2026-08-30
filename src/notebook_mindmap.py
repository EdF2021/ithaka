"""Mindmap artifact helpers: generation-time format validation + interactive
radial SVG viewer.

The mindmap generation prompt (src/notebook_artifacts.py) asks for exactly
one ```mermaid fence whose first line is "mindmap", a root((Topic)) node and
4-8 indented main branches. ``validate_mindmap_markdown`` below is
registered in src/notebook_artifacts.py's _KIND_VALIDATORS so
generate_artifact retries (with the error fed back) on such a format miss.

The viewer (``generate_mindmap_viewer``) renders the parsed tree as an
interactive **radial** SVG mindmap: the root sits at center, main branches
radiate outward as colored curves, sub-branches fan out further. Clicking
a branch toggles its children. Pan and zoom are supported via mouse drag
and wheel. The page uses a light theme matching the other artifact viewers.
"""

from __future__ import annotations

import html
import json
import math
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
# Radial SVG viewer rendering
# ---------------------------------------------------------------------------

_BRANCH_COLORS = [
    "#b8543a", "#2a8a8c", "#7a4cb8", "#3d8a3d",
    "#b88a2e", "#c0456e", "#4560b8", "#8a6d3b",
]


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _lighten(hex_color: str, factor: float = 0.35) -> str:
    r, g, b = _hex_to_rgb(hex_color)
    r = int(r + (255 - r) * factor)
    g = int(g + (255 - g) * factor)
    b = int(b + (255 - b) * factor)
    return f"#{r:02x}{g:02x}{b:02x}"


def _subtree_sizes(layout: list[dict]) -> list[int]:
    """Compute the size of each node's subtree (including itself).

    Layout is in pre-order, so a node's subtree is the contiguous run of
    deeper nodes immediately following it.
    """
    sizes = [1] * len(layout)
    for i in range(len(layout) - 1, -1, -1):
        j = i + 1
        while j < len(layout) and layout[j]["depth"] > layout[i]["depth"]:
            if layout[j]["depth"] == layout[i]["depth"] + 1:
                sizes[i] += sizes[j]
                j += sizes[j]
            else:
                j += 1
    return sizes


def _build_layout(node: dict, depth: int, angle_start: float, angle_end: float,
                  color: str, layout: list[dict]) -> None:
    """Recursively assign (depth, angle, color) to each node, plus a unique id.

    The root (depth 0) sits at center. Each main branch (depth 1) gets an
    equal angular slice; sub-branches divide their parent's slice.
    child_ids are filled in a second pass by _assign_child_ids.
    """
    nid = f"n{len(layout)}"
    mid_angle = (angle_start + angle_end) / 2
    entry = {
        "id": nid,
        "label": node["label"],
        "depth": depth,
        "angle": mid_angle,
        "color": color,
        "collapsed": False,
    }
    layout.append(entry)

    children = node.get("children", [])
    if not children:
        return

    n = len(children)
    span = angle_end - angle_start
    if depth > 0:
        margin = span * 0.08
        child_span = (span - 2 * margin) / n
        cs_start = angle_start + margin
    else:
        child_span = span / n
        cs_start = angle_start

    for i, child in enumerate(children):
        cs = cs_start + i * child_span
        ce = cs + child_span
        if depth == 0:
            child_color = _BRANCH_COLORS[i % len(_BRANCH_COLORS)]
        else:
            child_color = _lighten(color, 0.2)
        _build_layout(child, depth + 1, cs, ce, child_color, layout)


def _assign_child_ids(layout: list[dict]) -> None:
    """Second pass: fill child_ids for each entry based on tree traversal order."""
    sizes = _subtree_sizes(layout)
    for i, entry in enumerate(layout):
        child_ids = []
        j = i + 1
        while j < len(layout) and layout[j]["depth"] > entry["depth"]:
            if layout[j]["depth"] == entry["depth"] + 1:
                child_ids.append(layout[j]["id"])
                j += sizes[j]
            else:
                j += 1
        entry["child_ids"] = child_ids


def _polar_to_cartesian(cx: float, cy: float, r: float, angle_deg: float) -> tuple[float, float]:
    a = math.radians(angle_deg - 90)
    return cx + r * math.cos(a), cy + r * math.sin(a)


def _compute_radius(depth: int, max_depth: int, svg_size: int) -> float:
    center = svg_size / 2
    if depth == 0:
        return 0
    if max_depth <= 1:
        return center * 0.55
    return center * 0.22 + (center * 0.68) * (depth / max_depth)


def _truncate_label(label: str, max_len: int = 18) -> str:
    if len(label) <= max_len:
        return label
    return label[:max_len - 1] + "\u2026"


def _render_radial_svg(tree: dict, svg_size: int = 800) -> str:
    """Build the radial mindmap as an SVG string with interactive JS."""
    layout: list[dict] = []
    _build_layout(tree, 0, 0, 360, _BRANCH_COLORS[0], layout)
    _assign_child_ids(layout)

    max_depth = max(e["depth"] for e in layout) if layout else 1
    center = svg_size / 2

    for entry in layout:
        r = _compute_radius(entry["depth"], max_depth, svg_size)
        entry["x"], entry["y"] = _polar_to_cartesian(center, center, r, entry["angle"])

    def _font_size(depth: int) -> int:
        if depth == 0:
            return 16
        if depth == 1:
            return 13
        return 11

    def _node_radius(depth: int) -> int:
        if depth == 0:
            return 42
        if depth == 1:
            return 28
        return 22

    edges_svg: list[str] = []
    nodes_svg: list[str] = []

    # Build a lookup: id -> layout index
    id_to_idx = {e["id"]: i for i, e in enumerate(layout)}

    # Draw edges
    for entry in layout:
        if entry["depth"] == 0:
            continue
        parent = None
        idx = id_to_idx[entry["id"]]
        for j in range(idx - 1, -1, -1):
            if layout[j]["depth"] == entry["depth"] - 1:
                parent = layout[j]
                break
        if parent is None:
            continue
        edge_color = entry["color"]
        sw = max(2, 4 - entry["depth"])
        path = (
            f"M {parent['x']:.1f} {parent['y']:.1f} "
            f"C {(parent['x']+entry['x'])/2:.1f} {parent['y']:.1f}, "
            f"{(parent['x']+entry['x'])/2:.1f} {entry['y']:.1f}, "
            f"{entry['x']:.1f} {entry['y']:.1f}"
        )
        edges_svg.append(
            f'<path class="mm-edge" d="{path}" stroke="{edge_color}" '
            f'stroke-width="{sw}" fill="none" opacity="0.55" '
            f'data-from="{parent["id"]}" data-to="{entry["id"]}"/>'
        )

    # Draw nodes
    for entry in layout:
        x, y = entry["x"], entry["y"]
        nr = _node_radius(entry["depth"])
        fs = _font_size(entry["depth"])
        label = html.escape(_truncate_label(entry["label"]))
        color = entry["color"]
        has_children = bool(entry.get("child_ids"))
        fill = color if entry["depth"] <= 1 else _lighten(color, 0.55)
        text_color = "#fff" if entry["depth"] <= 1 else "#1a1817"
        cursor = "pointer" if has_children else "default"

        nodes_svg.append(
            f'<g class="mm-node" data-id="{entry["id"]}" style="cursor:{cursor}">'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{nr}" fill="{fill}" '
            f'stroke="{color}" stroke-width="2" opacity="0.95"/>'
            f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="middle" dominant-baseline="central" '
            f'font-size="{fs}" fill="{text_color}" font-family="system-ui,sans-serif" '
            f'font-weight="{600 if entry["depth"] <= 1 else 400}">{label}</text>'
            f'</g>'
        )

    all_edges = "\n".join(edges_svg)
    all_nodes = "\n".join(nodes_svg)

    js_layout = json.dumps([
        {"id": e["id"], "child_ids": e.get("child_ids", []), "collapsed": False}
        for e in layout
    ])

    return f'''<div id="mm-canvas" class="mm-canvas">
<svg id="mm-svg" viewBox="0 0 {svg_size} {svg_size}" preserveAspectRatio="xMidYMid meet" style="width:100%;height:100%;cursor:grab">
  <rect width="{svg_size}" height="{svg_size}" fill="transparent" id="mm-bg"/>
  <g id="mm-viewport">
    <g id="mm-edges">{all_edges}</g>
    <g id="mm-nodes">{all_nodes}</g>
  </g>
</svg>
</div>
<script>
(function() {{
  var layout = {js_layout};
  var nodeMap = {{}};
  layout.forEach(function(n) {{ nodeMap[n.id] = n; }});

  document.querySelectorAll('.mm-node').forEach(function(g) {{
    g.addEventListener('click', function(e) {{
      e.stopPropagation();
      var id = g.dataset.id;
      var node = nodeMap[id];
      if (!node || !node.child_ids.length) return;
      node.collapsed = !node.collapsed;
      updateVisibility();
    }});
  }});

  function updateVisibility() {{
    function hideDescendants(id, hide) {{
      var n = nodeMap[id];
      if (!n) return;
      n.child_ids.forEach(function(cid) {{
        var el = document.querySelector('.mm-node[data-id="' + cid + '"]');
        var edge = document.querySelector('.mm-edge[data-to="' + cid + '"]');
        if (el) {{ el.style.opacity = hide ? '0' : '1'; el.style.pointerEvents = hide ? 'none' : ''; }}
        if (edge) edge.style.opacity = hide ? '0' : '0.55';
        hideDescendants(cid, hide);
      }});
    }}
    layout.forEach(function(n) {{
      if (n.child_ids.length) hideDescendants(n.id, n.collapsed);
    }});
  }}

  var svg = document.getElementById('mm-svg');
  var viewport = document.getElementById('mm-viewport');
  var isPanning = false, startX = 0, startY = 0;
  var vb = {{ x: 0, y: 0, w: {svg_size}, h: {svg_size} }};

  svg.addEventListener('mousedown', function(e) {{
    if (e.target.closest('.mm-node')) return;
    isPanning = true; startX = e.clientX; startY = e.clientY;
    svg.style.cursor = 'grabbing';
  }});
  window.addEventListener('mousemove', function(e) {{
    if (!isPanning) return;
    var dx = (e.clientX - startX) * vb.w / svg.clientWidth;
    var dy = (e.clientY - startY) * vb.h / svg.clientHeight;
    vb.x -= dx; vb.y -= dy;
    startX = e.clientX; startY = e.clientY;
    applyVb();
  }});
  window.addEventListener('mouseup', function() {{
    isPanning = false; svg.style.cursor = 'grab';
  }});

  svg.addEventListener('wheel', function(e) {{
    e.preventDefault();
    var delta = e.deltaY > 0 ? 1.12 : 0.89;
    var newW = vb.w * delta, newH = vb.h * delta;
    var rect = svg.getBoundingClientRect();
    var mx = (e.clientX - rect.left) / rect.width;
    var my = (e.clientY - rect.top) / rect.height;
    vb.x += (vb.w - newW) * mx;
    vb.y += (vb.h - newH) * my;
    vb.w = newW; vb.h = newH;
    applyVb();
  }}, {{ passive: false }});

  function applyVb() {{
    var s = vb.w / {svg_size};
    viewport.setAttribute('transform', 'translate(' + vb.x + ' ' + vb.y + ') scale(' + s + ')');
  }}

  document.getElementById('mm-expand')?.addEventListener('click', function() {{
    layout.forEach(function(n) {{ n.collapsed = false; }});
    updateVisibility();
  }});
  document.getElementById('mm-collapse')?.addEventListener('click', function() {{
    layout.forEach(function(n) {{
      if (n.child_ids.length && n.id !== 'n0') n.collapsed = true;
    }});
    updateVisibility();
  }});
}})();
</script>'''


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
  body {{ margin:0; background:var(--bg); color:var(--fg);
         font-family:var(--font-body); padding:24px; min-height:100vh; }}
  header {{ max-width:1100px; margin:0 auto 12px; text-align:center; }}
  h1 {{ font-family:var(--font-display); font-size:clamp(1.3rem,3.5vw,1.8rem);
       margin:0 0 4px; font-weight:700; letter-spacing:-0.01em; }}
  .mm-meta {{ font-size:12px; color:#8a8580; }}
  .mm-controls {{ max-width:1100px; margin:0 auto 14px;
                  display:flex; gap:8px; justify-content:center; }}
  .mm-controls button {{ background:var(--panel); color:var(--fg);
                          border:1px solid var(--border); border-radius:8px;
                          padding:6px 14px; font:inherit; font-size:12px; cursor:pointer; }}
  .mm-controls button:hover {{ border-color:var(--accent); color:var(--accent); }}
  .mm-canvas {{ max-width:1100px; margin:0 auto;
                height:calc(100vh - 180px); min-height:400px;
                background:var(--panel); border:1px solid var(--border);
                border-radius:12px; overflow:hidden; position:relative; }}
  .mm-edge {{ transition: opacity 0.25s ease; }}
  .mm-node {{ transition: opacity 0.25s ease; }}
  .mm-node:hover circle {{ stroke-width:3; }}
  .mm-caption {{ max-width:1100px; margin:14px auto 0; text-align:center;
                  font-size:13px; color:#5a5651; font-style:italic; }}
  .mm-empty {{ max-width:1100px; margin:40px auto; color:#5a5651; }}
  .mm-empty pre {{ background:var(--panel); border:1px solid var(--border);
                    border-radius:8px; padding:14px; overflow-x:auto;
                    font-size:12px; white-space:pre-wrap; }}
  footer {{ max-width:1100px; margin:20px auto 0; font-size:11px;
             color:#8a8580; text-align:center; }}
  @media (max-width:480px) {{ body {{ padding:12px; }}
    .mm-canvas {{ height:calc(100vh - 220px); }} }}
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
    """Render the stored mermaid-mindmap markdown as an interactive radial viewer.

    Self-contained page (no external resources) with click-to-collapse
    branches, expand/collapse-all controls, and pan/zoom. Content without a
    parseable mindmap degrades to a message plus the escaped raw text.
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
            '<button type="button" id="mm-expand">Alles uitklappen</button>'
            '<button type="button" id="mm-collapse">Alles inklappen</button>'
            "</div>"
        )
        svg_html = _render_radial_svg(tree)
        caption = (tree.get("caption") or "").strip()
        caption_html = f'<div class="mm-caption">{html.escape(caption)}</div>' if caption else ""
        body_html = controls + svg_html + caption_html
        page_title = (title or "").strip() or tree["label"]
    return _TEMPLATE.format(
        title=html.escape(page_title),
        notebook_name=html.escape(notebook_name or ""),
        date=html.escape(generated_at.strftime("%B %d, %Y")),
        body_html=body_html,
    )