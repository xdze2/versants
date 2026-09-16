"""Render a valley catalog (the nested dict from :func:`valleespyr.catalog.build_catalog`)
as one self-contained static HTML file — a **two-level local river selector**.

The page shows only what is near the current selection: the downstream river
as a "go back" link, up to one sibling river on each side (with a ``…``
marker when more are hidden), the selected river itself, and its direct
sub-rivers (``mainline`` continuation + ``tributaries``) each tagged with
their own tributary count. Clicking any row re-centers the view on it; a
breadcrumb above the list shows the full downstream trail back to the root.

When the catalog carries a ``geo`` block (``valleespyr catalog --geo``) a
second column holds a sticky Leaflet map: an IGN topo or OpenStreetMap
basemap (with an optional IGN relief-shading overlay), both fetched live from
public tile servers — this is the one part of the page that needs network
access. Clicking a row (or a river's line on the map itself) draws just that
river and its upstream network on top as styled polylines, fit to frame. Each
line's stroke width scales with the river's Strahler order, with the selected
river and its network drawn heavier than the faint catchment context.

``render_catalog_html(catalog, path)`` writes the file; ``catalog_to_html`` gives
the string.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

# Mini-map (only with a geo block).
MAP_MIN = 340       # px — floor for the map column on narrow windows
MAP_H = 560         # px — fixed height of the Leaflet map viewport
LEAFLET_VERSION = "1.9.4"

# --- git-graph lane geometry (shared with the client via _GEOM) ------------
# Two fixed lanes always: 0 is the trunk (back link / selected / mainline
# child), 1 is where every sibling or tributary draws its own short "o--"
# branch stub off the trunk at its own row. A river with 50 tributaries
# costs the same width as one with two — nothing scales with fan-out.
ROW_H = 26          # px per row — matches .labels .row's own height
LANE_W = 15         # px per lane column
LANE_PAD = 10       # left padding before lane 0
DOT_R = 3.2         # merge/branch dot radius
LABEL_GAP = 10      # px between the lane area and the label column
N_LANES = 2

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
  padding: 10px 22px; border-bottom: 1px solid var(--line);
  position: sticky; top: 0; background: var(--bg); z-index: 3;
  display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap;
}}
.header-text {{
  flex: 1 1 auto; min-width: 200px;
  display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap;
}}
h1 {{ margin: 0; font-size: 17px; font-weight: 600; }}
h1::after {{ content: "·"; margin-left: 8px; color: var(--line); font-weight: 400; }}
.meta {{ color: var(--dim); font-size: 12px; }}
.crumbs {{ font-size: 12px; color: var(--faint); flex: 0 0 100%; }}
.crumbs:empty {{ display: none; flex-basis: 0; }}
.crumbs a {{ color: var(--dim); text-decoration: none; }}
.crumbs a:hover {{ color: var(--accent); text-decoration: underline; }}
.crumbs b {{ color: var(--ink); font-weight: 600; }}
.crumbs .sep {{ margin: 0 5px; color: var(--faint); }}

main {{ padding: 8px 22px 60px; }}
.graphwrap {{
  position: relative; display: flex; align-items: flex-start; gap: 6px; flex-wrap: wrap;
}}
.graphwrap > .mapcol {{ flex: 1 1 {map_min}px; min-width: {map_min}px; }}
.treecol {{
  display: grid; grid-template-columns: {lane_w}px 1fr; align-items: start;
  width: 280px; flex: 0 0 auto;
}}
.treewrap {{ overflow-x: auto; overflow-y: hidden; }}
svg.graph {{ display: block; }}
ol.labels {{ list-style: none; margin: 0; padding: 0; min-width: 0; }}
.labels .row {{
  display: flex; align-items: center; gap: 8px; height: {row_h}px;
  padding: 0 10px; white-space: nowrap; cursor: pointer;
  border-radius: 6px; overflow: hidden; text-overflow: ellipsis;
}}
.labels .row .name {{ overflow: hidden; text-overflow: ellipsis; }}
.labels .row.back .hint, .labels .row .more {{ flex: 0 0 auto; }}
.labels .row:hover {{ background: #fff; box-shadow: inset 0 0 0 1px var(--line); }}
.labels .row.selected-river {{ cursor: default; }}
.labels .row.selected-river .name {{ color: var(--accent); font-weight: 700; }}
.labels .row.preview {{ background: #fdf3e3; box-shadow: inset 0 0 0 1px #e8c99a; }}
.labels .row.back {{ color: var(--dim); }}
.labels .row.back .arrow {{ color: var(--faint); }}
.labels .row.back .hint {{ color: var(--faint); font-size: 11px; margin-left: 2px; }}
.labels .row.sibling {{ color: var(--dim); }}
.labels .row.more-sibs {{ color: var(--faint); font-size: 11.5px; }}
.labels .row.more-sibs .name {{ font-weight: 400; }}

.mapcol {{ position: sticky; top: {map_top}px; align-self: start; }}
.mapcard {{
  position: relative; border: 1px solid var(--line); border-radius: 8px;
  background: #fff; overflow: hidden; width: 100%;
}}
#map {{ display: block; width: 100%; height: {map_h}px; background: #dde3e7; }}
#map .leaflet-container {{ font: inherit; background: #dde3e7; }}
#map3d {{
  position: relative; width: 100%; height: {map_h}px; background: #eae4d6;
  overflow: hidden;
}}
#cv3d {{ display: block; width: 100%; height: 100%; }}
#loading3d {{
  position: absolute; inset: 0; display: flex; align-items: center;
  justify-content: center; background: #eae4d6; color: #8a8068; font-size: 12.5px;
}}
#loading3d[hidden] {{ display: none; }}
#map3d .tip3d {{
  position: absolute; right: 10px; bottom: 8px; font-size: 10.5px; color: #8a8068;
  opacity: .75; pointer-events: none;
}}
#map3d .compass3d {{
  position: absolute; left: 10px; top: 10px; width: 44px; height: 44px;
  pointer-events: none;
}}
.maplayers {{
  display: flex; gap: 12px; align-items: center; flex-wrap: wrap;
  padding: 7px 10px; border-bottom: 1px solid var(--line);
  font-size: 11.5px; color: var(--dim); background: #fff;
}}
.maplayers label {{ display: flex; gap: 5px; align-items: center; cursor: pointer; }}
.maplayers .sep {{ width: 1px; height: 13px; background: var(--line); }}
.infobox {{
  position: absolute; right: 10px; bottom: 10px; z-index: 2;
  max-width: min(78%, 320px);
  padding: 9px 11px; border-radius: 8px; font-size: 12px;
  color: var(--dim); line-height: 1.5;
  background: rgba(255, 255, 255, 0.88); backdrop-filter: blur(3px);
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.15), 0 0 0 1px rgba(0, 0, 0, 0.06);
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
  display: grid; grid-template-columns: repeat(auto-fill, minmax(88px, 1fr));
  gap: 4px 12px;
}}
.infobox .stat {{ display: flex; flex-direction: column; }}
.infobox .stat .v {{ color: var(--ink); font-variant-numeric: tabular-nums;
  font-size: 13px; }}
.infobox .stat .k {{ color: var(--faint); font-size: 10.5px; text-transform: uppercase;
  letter-spacing: .04em; }}
.infobox .also {{ display: block; margin-top: 6px; }}
.name {{ font-weight: 600; }}
.more {{ color: var(--faint); font-size: 11px; }}
.also {{ color: #a5682f; font-size: 11px; }}
"""

# Everything in these two files runs in the browser. catalog_graph.js owns
# the two-level local view: given the selected river it renders the go-back
# link to its parent, its immediate siblings, itself, and its direct
# sub-rivers, re-rendering on every click. catalog_map.js (only inlined when
# the catalog carries a ``geo`` block — keeps a plain catalog page fully
# offline, with zero references to any external host anywhere in its source)
# draws the Leaflet map: IGN/OSM tile basemaps and the river network as
# styled vector layers. Both are kept as real .js files (not Python string
# literals) for editor/lint support.
_STATIC_DIR = Path(__file__).parent / "static"
_JS = (_STATIC_DIR / "catalog_graph.js").read_text(encoding="utf-8")
_JS_GEO = (_STATIC_DIR / "catalog_map.js").read_text(encoding="utf-8")
_JS_GEO_3D = (_STATIC_DIR / "catalog_3d.js").read_text(encoding="utf-8")


def catalog_to_html(
    catalog: dict[str, Any],
    *,
    max_depth: int | None = None,
    terrain_url: str | None = None,
    data_url: str = "catalog_index.json",
) -> str:
    """Return the two-level river selector HTML document (a thin shell).

    The document ships a script that ``fetch()``es the catalog JSON from
    ``data_url`` (relative to the HTML file) and renders the local
    neighbourhood of the selected river (downstream link, siblings, itself,
    its sub-rivers), re-rendering on every click. It does not embed the
    catalog data itself — see :func:`render_catalog_html`, which writes the
    JSON file alongside the HTML. ``max_depth`` is accepted for API
    compatibility but no longer changes the initial render — the view only
    ever shows two levels regardless of tree depth.

    ``terrain_url`` (a directory path or URL, relative to the HTML file — see
    ``valleespyr catalog --terrain-dir``) switches the map column from the
    Leaflet 2D map to a three.js 3D scene: the selected river's real DEM
    terrain (:mod:`valleespyr.render.terrain_precompute`'s output,
    ``<terrain_url>/<river_id>.json``) fetched lazily on click, with every
    other river in view as a flat footprint at its true position. A river
    with no terrain file there falls back to a flat highlighted footprint.
    """
    meta = catalog.get("meta", {})
    title = meta.get("root_name") or meta.get("root_id") or "valley catalog"

    has_geo = bool(catalog.get("geo") and catalog["geo"].get("rivers"))
    use_3d = has_geo and terrain_url is not None

    if has_geo and not use_3d:
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
    elif use_3d:
        map_col = (
            f'<div class="mapcol"><div class="mapcard">'
            f'<div id="map3d" aria-label="selected river in 3D">'
            f'<canvas id="cv3d"></canvas>'
            f'<div id="loading3d">loading terrain…</div>'
            f"</div>"
            f'<div id="infobox" class="infobox"><span class="hint">click a river…</span></div>'
            f"</div></div>"
        )
    else:
        map_col = ""

    metaline = "Explore the Pyrenees valley by valley"

    lane_w = LANE_PAD + N_LANES * LANE_W

    css = _CSS_TMPL.format(
        map_top=88,
        map_h=MAP_H,
        map_min=MAP_MIN,
        lane_w=lane_w,
        row_h=ROW_H,
    )

    geom = {
        "ROW_H": ROW_H, "LANE_W": LANE_W, "LANE_PAD": LANE_PAD,
        "DOT_R": DOT_R, "LABEL_GAP": LABEL_GAP, "BG": _BG,
    }
    js = (
        _JS.replace("__GEOM__", json.dumps(geom))
        .replace("__ORDER_STYLE__", json.dumps(_ORDER_STYLE))
        .replace("__DATA_URL__", json.dumps(data_url))
    )
    # The geo script must run first: it defines window._catalogInitGeo, which
    # the base script's IIFE calls (synchronously, at its own end) once it exists.
    if use_3d:
        js = _JS_GEO_3D.replace("__TERRAIN_URL__", json.dumps(terrain_url)) + js
    elif has_geo:
        js = _JS_GEO + js

    if use_3d:
        leaflet_head = (
            '<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/0.159.0/three.min.js">'
            "</script>"
        )
    elif has_geo:
        leaflet_head = (
            f'<link rel="stylesheet" '
            f'href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/{LEAFLET_VERSION}/leaflet.min.css">'
            f'<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/{LEAFLET_VERSION}/leaflet.js">'
            f"</script>"
        )
    else:
        leaflet_head = ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Versant — {html.escape(title)}</title>
<style>{css}</style>
{leaflet_head}
</head>
<body>
<header>
  <div class="header-text">
    <h1>Versant</h1>
    <div class="meta">{metaline}</div>
  </div>
  <div id="crumbs" class="crumbs"></div>
</header>
<main>
  <div class="graphwrap">
    <div id="treecol" class="treecol">
      <div class="treewrap"><svg id="graph" class="graph" width="1" height="1" viewBox="0 0 1 1" aria-hidden="true"></svg></div>
      <ol id="labels" class="labels"></ol>
    </div>
    {map_col}
  </div>
</main>
<script>{js}</script>
</body>
</html>
"""


def render_catalog_html(
    catalog: dict[str, Any],
    path: str | Path,
    *,
    max_depth: int | None = None,
    terrain_url: str | None = None,
    data_filename: str = "catalog_index.json",
) -> Path:
    """Write the catalog as a thin HTML shell plus its own JSON data file.

    Writes ``<path's directory>/<data_filename>`` (the full catalog tree/map
    payload, as plain JSON — inspectable/diffable on its own) and ``path``
    itself (markup/CSS/JS only, no baked-in data), which ``fetch()``es that
    JSON file on load. Returns ``path``.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    (path.parent / data_filename).write_text(
        json.dumps(catalog, ensure_ascii=False), encoding="utf-8"
    )
    path.write_text(
        catalog_to_html(
            catalog,
            max_depth=max_depth,
            terrain_url=terrain_url,
            data_url=data_filename,
        ),
        encoding="utf-8",
    )
    return path


__all__ = ["catalog_to_html", "render_catalog_html"]
