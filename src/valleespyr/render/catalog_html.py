"""Render a valley catalog (the nested dict from :func:`valleespyr.catalog.build_catalog`)
as one self-contained static HTML file — a **collapsible git-graph of the river
network**.

Each *river* (a branch) is one **lane**: a coloured vertical line in its own
column, tinted by Strahler order (dark & thick for the trunk, pale & thin for a
headwater). A tributary lane runs down from the row where it branches off its
parent and turns left into the parent lane with a right-angle **elbow** — a
merge. The parent lane runs straight through, never interrupted. There is **one
row per river**, not per reach.

Unlike a frozen ``<svg>``, the graph here is **laid out in the browser**: the
page ships the catalog JSON and a small script that walks the tree, assigns
lanes, and draws the SVG + label list together. Because both come from the same
walk they never desync, so the graph can *collapse*:

* an **order slider** hides every river below a chosen Strahler order — a whole
  Garonne opens legible, with low-order headwaters folded into a ``+N`` count on
  the row they join;
* any river with hidden children shows a ``▸`` caret; clicking it splices that
  sub-basin back in (or folds it away) and the graph re-renders.

The server still renders the *initial* state into the document (so it is a
valid, readable git-graph with no JS), then the script re-renders on every
slider move or caret click. Beyond ``--max-depth`` a branch is folded to a
``+N rivers`` leaf as before.

When the catalog carries a ``geo`` block (``valleespyr catalog --geo``) a third
column holds a sticky Leaflet map: an IGN topo or OpenStreetMap basemap (with
an optional IGN relief-shading overlay), both fetched live from public tile
servers — this is the one part of the page that needs network access.
Clicking a row draws just that river and its upstream network on top as
styled polylines, fit to frame. Each line's stroke width scales with the
river's Strahler order (same read as the git-graph lanes), with the selected
river and its network drawn heavier than the faint catchment context.

``render_catalog_html(catalog, path)`` writes the file; ``catalog_to_html`` gives
the string.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

# --- geometry (shared with the client via _GEOM) ---------------------------
ROW_H = 24          # px per row
LANE_W = 15         # px per lane column
LANE_PAD = 10       # left padding before lane 0
DOT_R = 3.2         # merge/branch dot radius
LABEL_GAP = 14      # px between the lane area and the label column
ELBOW_R = 5.0       # px corner radius on a merge elbow
# Mini-map (only with a geo block).
MAP_MIN = 340       # px — floor for the map column on narrow windows
MAP_MAX_VW = 75     # % of window width — ceiling for the map column
MAP_H = 560         # px — fixed height of the Leaflet map viewport
LEAFLET_VERSION = "1.9.4"

# Strahler order -> (stroke width, colour). Index 0 == order 1; clamped to ends.
_ORDER_STYLE = [
    (1.1, "#9db8d0"),  # 1 — pale headwater
    (1.4, "#7ba3c9"),  # 2
    (1.8, "#5b8bbf"),  # 3
    (2.3, "#3f74af"),  # 4
    (2.8, "#2f6096"),  # 5
    (3.4, "#244d7c"),  # 6
    (4.0, "#1c3e63"),  # 7+ — trunk
]
_BG = "#fbfbfa"


def _max_order(root: dict[str, Any]) -> int:
    m = 0
    stack = [root]
    while stack:
        n = stack.pop()
        m = max(m, n.get("strahler") or 0)
        if n.get("mainline"):
            stack.append(n["mainline"])
        stack.extend(n.get("tributaries") or [])
    return m


# --- document -------------------------------------------------------------

_CSS_TMPL = """
:root {{
  --ink: #1b1b1b; --dim: #6b6b6b; --faint: #9a9a9a;
  --line: #e6e6e6; --bg: #fbfbfa; --accent: #2f6f4f;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; background: var(--bg); color: var(--ink);
  font: 13px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}}
header {{
  padding: 18px 22px 12px; border-bottom: 1px solid var(--line);
  position: sticky; top: 0; background: var(--bg); z-index: 3;
}}
h1 {{ margin: 0 0 2px; font-size: 17px; font-weight: 600; }}
.meta {{ color: var(--dim); font-size: 12px; }}
.controls {{ margin-top: 9px; display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }}
.controls input[type=search] {{
  padding: 5px 9px; border: 1px solid var(--line); border-radius: 6px;
  font: inherit; min-width: 200px; background: #fff;
}}
.orderctl {{ display: flex; gap: 7px; align-items: center; color: var(--dim); font-size: 12px; }}
.orderctl input[type=range] {{ width: 120px; }}
.orderctl input[type=range]:disabled {{ opacity: .4; }}
.orderctl b {{ color: var(--ink); font-variant-numeric: tabular-nums; }}
.orderctl .n {{ color: var(--faint); }}
.focusctl {{ display: flex; gap: 5px; align-items: center; color: var(--dim);
  font-size: 12px; cursor: pointer; }}
.legend {{ display: flex; gap: 10px; align-items: center; color: var(--faint); font-size: 11px; }}
.legend i {{ width: 20px; height: 0; border-top-style: solid; display: inline-block;
  vertical-align: middle; margin-right: 3px; }}
.crumbs {{ margin-top: 8px; font-size: 12px; color: var(--faint); min-height: 18px; }}
.crumbs:empty {{ display: none; }}
.crumbs a {{ color: var(--dim); text-decoration: none; }}
.crumbs a:hover {{ color: var(--accent); text-decoration: underline; }}
.crumbs b {{ color: var(--ink); font-weight: 600; }}
.crumbs .sep {{ margin: 0 5px; color: var(--line); }}

main {{ padding: 8px 22px 60px; }}
.graphwrap {{
  position: relative; display: grid;
  grid-template-columns: {grid_cols}; align-items: start;
}}
svg.graph {{ display: block; position: sticky; left: 0; }}
ol.labels {{ list-style: none; margin: 0; padding: 0; }}
.labels .row {{
  display: flex; align-items: center; gap: 8px;
  padding: 0 8px; white-space: nowrap; cursor: default;
}}
.labels .row:hover {{ background: #fff; box-shadow: inset 0 0 0 1px var(--line); }}
.has-geo .labels .row {{ cursor: pointer; }}
.labels .row.selected {{ background: #eef5f0; }}
.labels .row.selected .name {{ color: var(--accent); }}
.caret {{
  flex: none; width: 15px; height: 15px; margin-left: -3px; border: 0;
  padding: 0; background: none; cursor: pointer; color: var(--faint);
  font-size: 10px; line-height: 15px; text-align: center;
}}
.caret:hover {{ color: var(--ink); }}
.caret[hidden] {{ display: inline-block; visibility: hidden; }}
.caret.open {{ transform: rotate(90deg); }}

.mapcol {{ position: sticky; top: {map_top}px; align-self: start; }}
.mapcard {{
  border: 1px solid var(--line); border-radius: 8px; background: #fff;
  overflow: hidden; width: 100%;
}}
#map {{ display: block; width: 100%; height: {map_h}px; background: #dde3e7; }}
#map .leaflet-container {{ font: inherit; background: #dde3e7; }}
.maplayers {{
  display: flex; gap: 12px; align-items: center; flex-wrap: wrap;
  padding: 7px 10px; border-bottom: 1px solid var(--line);
  font-size: 11.5px; color: var(--dim); background: #fff;
}}
.maplayers label {{ display: flex; gap: 5px; align-items: center; cursor: pointer; }}
.maplayers .sep {{ width: 1px; height: 13px; background: var(--line); }}
.infobox {{
  padding: 10px 12px; border-top: 1px solid var(--line); font-size: 12px;
  color: var(--dim); line-height: 1.5; min-height: 34px;
}}
.infobox .hint {{ color: var(--faint); }}
.infobox .title {{
  display: flex; align-items: baseline; gap: 8px; margin-bottom: 6px;
}}
.infobox .title b {{ color: var(--ink); font-size: 14px; font-weight: 600; }}
.infobox .title a {{ color: var(--accent); text-decoration: none; font-size: 11.5px;
  white-space: nowrap; margin-left: auto; }}
.infobox .title a:hover {{ text-decoration: underline; }}
.infobox .stats {{
  display: grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr));
  gap: 6px 14px;
}}
.infobox .stat {{ display: flex; flex-direction: column; }}
.infobox .stat .v {{ color: var(--ink); font-variant-numeric: tabular-nums;
  font-size: 13px; }}
.infobox .stat .k {{ color: var(--faint); font-size: 10.5px; text-transform: uppercase;
  letter-spacing: .04em; }}
.infobox .also {{ display: block; margin-top: 6px; }}
.name {{ font-weight: 600; }}
.name.unnamed {{ color: var(--faint); font-weight: 400;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; }}
.row.leaf .name {{ font-weight: 400; color: var(--faint); font-style: italic; }}
.row.leaf[data-fold-into] {{ cursor: pointer; }}
.row.leaf[data-fold-into]:hover .name {{ color: var(--accent); }}
.more {{ color: var(--faint); font-size: 11px; }}
.pfaf {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 10.5px; color: var(--accent); }}
.also {{ color: #a5682f; font-size: 11px; }}
.row.hidden {{ display: none; }}
mark {{ background: #ffe9a8; color: inherit; }}
"""

# Everything in these two files runs in the browser. catalog_graph.js owns
# the layout: one depth-first walk assigns rows + lanes for the *currently
# visible* tree, then paints the SVG (elbow merges) and the <li> list from
# that single pass, so the two columns can never desync as the tree
# collapses. catalog_map.js (only inlined when the catalog carries a ``geo``
# block — keeps a plain catalog page fully offline, with zero references to
# any external host anywhere in its source) draws the Leaflet map: IGN/OSM
# tile basemaps and the river network as styled vector layers. Both are kept
# as real .js files (not Python string literals) for editor/lint support;
# ``__GEOM__``/``__ORDER_STYLE__`` in catalog_graph.js are substituted with
# JSON at render time, same as an inline template would.
_STATIC_DIR = Path(__file__).parent / "static"
_JS = (_STATIC_DIR / "catalog_graph.js").read_text(encoding="utf-8")
_JS_GEO = (_STATIC_DIR / "catalog_map.js").read_text(encoding="utf-8")


def catalog_to_html(catalog: dict[str, Any], *, max_depth: int | None = None) -> str:
    """Return the self-contained collapsible git-graph HTML document.

    The document ships the catalog JSON plus a script that lays the graph out
    in the browser and re-renders it on every order-slider move or caret
    click. ``max_depth`` folds the tree beyond N confluences from the root,
    exactly as before, and is passed through to the client.
    """
    meta = catalog.get("meta", {})
    root = catalog["root"]
    title = meta.get("root_name") or meta.get("root_id") or "valley catalog"

    has_geo = bool(catalog.get("geo") and catalog["geo"].get("rivers"))
    max_ord = max(_max_order(root), 1)
    n_nodes = meta.get("n_nodes") or 1
    # open the slider at an order that keeps the first paint legible: a small
    # basin shows whole, a large one starts well pruned (the graph lands
    # focused on the root, so the slider is the only thing bounding it).
    if n_nodes <= 120:
        start_order = 1
    elif n_nodes <= 400:
        start_order = min(3, max_ord)
    else:
        start_order = min(max_ord - 1, max(4, max_ord - 3))

    if has_geo:
        map_w_css = f"clamp({MAP_MIN}px, 60vw, {MAP_MAX_VW}vw)"
        rest_cols = f"minmax(160px, 1fr) {map_w_css}"
        map_col = (
            f'<div class="mapcol"><div class="mapcard">'
            f'<div class="maplayers">'
            f'<label><input type="radio" name="maplayer" value="ign" checked> IGN topo</label>'
            f'<label><input type="radio" name="maplayer" value="osm"> OpenStreetMap</label>'
            f'<span class="sep"></span>'
            f'<label><input type="checkbox" id="maphillshade"> relief shading</label>'
            f"</div>"
            f'<div id="map" aria-label="selected river network"></div>'
            f'<div id="infobox" class="infobox"><span class="hint">click a river…</span></div>'
            f"</div></div>"
        )
    else:
        rest_cols = "1fr"
        map_col = ""

    # a placeholder lane width; the script recomputes it on first render
    grid_cols = f"{LANE_PAD + LANE_W + LABEL_GAP}px {rest_cols}"

    metaline = (
        f"catchment of <b>{html.escape(title)}</b> as a collapsible river "
        f"git-graph — each lane is one river, tinted by Strahler order; a lane "
        f"turns into its parent where the two meet"
        f'<span id="meta-extra"></span>'
        + (
            " · click a row to select it — the graph folds around it"
            if has_geo
            else " · click a row to fold the graph around it"
        )
    )

    legend = "".join(
        f'<span><i style="border-top-width:{w:.0f}px;border-top-color:{c}"></i>'
        f'ord {min(i + 1, 7)}{"+" if i == len(_ORDER_STYLE) - 1 else ""}</span>'
        for i, (w, c) in enumerate(_ORDER_STYLE)
    )

    css = _CSS_TMPL.format(
        grid_cols=grid_cols,
        map_top=128,
        map_h=MAP_H,
    )

    geom = {
        "ROW_H": ROW_H, "LANE_W": LANE_W, "LANE_PAD": LANE_PAD, "DOT_R": DOT_R,
        "LABEL_GAP": LABEL_GAP, "ELBOW_R": ELBOW_R, "BG": _BG,
        "MAX_DEPTH": max_depth,
        "REST_COLS": rest_cols,
    }
    js = (
        _JS.replace("__GEOM__", json.dumps(geom))
        .replace("__ORDER_STYLE__", json.dumps(_ORDER_STYLE))
    )
    # _JS_GEO must run first: it defines window._catalogInitGeo, which the
    # base script's IIFE calls (synchronously, at its own end) once it exists.
    if has_geo:
        js = _JS_GEO + js

    payload = json.dumps(catalog, ensure_ascii=False).replace("<", "\\u003c")

    leaflet_head = (
        f'<link rel="stylesheet" '
        f'href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/{LEAFLET_VERSION}/leaflet.min.css">'
        f'<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/{LEAFLET_VERSION}/leaflet.js">'
        f"</script>"
        if has_geo
        else ""
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} — river git-graph</title>
<style>{css}</style>
{leaflet_head}
</head>
<body>
<header>
  <h1>{html.escape(title)}</h1>
  <div class="meta">{metaline}</div>
  <div class="controls">
    <input id="filter" type="search" placeholder="filter by name…" autocomplete="off">
    <label class="orderctl">min order <input id="order" type="range" min="1"
      max="{max_ord}" value="{start_order}" step="1"><b id="order-val">{start_order}</b>
      <span class="n">/ {max_ord}</span></label>
    <label class="focusctl"><input id="focus" type="checkbox" checked> fold around
      selection</label>
    <span class="legend">{legend}</span>
  </div>
  <div id="crumbs" class="crumbs"></div>
</header>
<main>
  <div class="graphwrap">
    <svg id="graph" class="graph" width="1" height="1" viewBox="0 0 1 1" aria-hidden="true"></svg>
    <ol id="labels" class="labels"></ol>
    {map_col}
  </div>
</main>
<script id="catalog-data" type="application/json">{payload}</script>
<script>{js}</script>
</body>
</html>
"""


def render_catalog_html(
    catalog: dict[str, Any], path: str | Path, *, max_depth: int | None = None
) -> Path:
    """Write the catalog as a self-contained git-graph HTML file; return the path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(catalog_to_html(catalog, max_depth=max_depth), encoding="utf-8")
    return path


__all__ = ["catalog_to_html", "render_catalog_html"]
