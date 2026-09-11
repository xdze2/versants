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

# Everything below runs in the browser. It owns the layout: one depth-first
# walk assigns rows + lanes for the *currently visible* tree, then paints the
# SVG (elbow merges) and the <li> list from that single pass, so the two
# columns can never desync as the tree collapses.
_JS = r"""
(function () {
  const G = __GEOM__;
  const ORDER_STYLE = __ORDER_STYLE__;
  const data = JSON.parse(document.getElementById('catalog-data').textContent);
  const root = data.root;
  const meta = data.meta || {};

  const graphSvg = document.getElementById('graph');
  const labelsEl = document.getElementById('labels');
  const metaExtra = document.getElementById('meta-extra');
  const slider = document.getElementById('order');
  const orderOut = document.getElementById('order-val');
  const filterEl = document.getElementById('filter');
  const focusBox = document.getElementById('focus');
  const crumbEl = document.getElementById('crumbs');

  // index every node by id, once
  const nodeById = {};
  (function idx(n) {
    nodeById[n.id] = n;
    if (n.mainline) idx(n.mainline);
    for (const t of n.tributaries || []) idx(t);
  })(root);

  // --- selection-relative folding state --------------------------------
  // When focus mode is on the graph folds around the selected river: the
  // spine from the root down to it always stays open; *inside* the selection's
  // own basin the order slider still applies, so focusing the trunk shows its
  // major tributaries (raise the slider to prune) while focusing a small
  // tributary shows all of it (nothing there is below threshold). Everything
  // outside the basin and off the spine folds to a +N leaf.
  let selectedId = null;
  let spine = new Set();       // ids on the root -> selection path (inclusive)
  let selBasin = new Set();    // every id inside the selection's catchment
  const spinePath = [];        // [rootNode, …, selNode] for the breadcrumb

  function recomputeSelection(id) {
    selectedId = id || null;
    spine = new Set();
    selBasin = new Set();
    spinePath.length = 0;
    if (!id || !nodeById[id]) return;
    (function find(n, trail) {
      const here = trail.concat(n);
      if (n.id === id) { here.forEach(x => spine.add(x.id)); spinePath.push(...here); return true; }
      for (const k of [n.mainline, ...(n.tributaries || [])].filter(Boolean))
        if (find(k, here)) return true;
      return false;
    })(root, []);
    (function sub(n) {
      selBasin.add(n.id);
      if (n.mainline) sub(n.mainline);
      for (const t of n.tributaries || []) sub(t);
    })(nodeById[id]);
  }

  // --- style helpers ------------------------------------------------------
  function orderStyle(order) {
    const i = !order ? 0 : Math.min(Math.max(order, 1), ORDER_STYLE.length) - 1;
    return ORDER_STYLE[i];
  }
  function laneX(lane) { return G.LANE_PAD + lane * G.LANE_W + G.LANE_W / 2; }
  function rowY(i) { return i * G.ROW_H + G.ROW_H / 2; }

  function subtreeCount(n) {
    let c = 1;
    if (n.mainline) c += subtreeCount(n.mainline);
    for (const t of n.tributaries || []) c += subtreeCount(t);
    c += n.tributaries_truncated || 0;
    return c;
  }
  function subtreeMaxOrder(n) {
    let m = n.strahler || 0;
    if (n.mainline) m = Math.max(m, subtreeMaxOrder(n.mainline));
    for (const t of n.tributaries || []) m = Math.max(m, subtreeMaxOrder(t));
    return m;
  }
  function isSource(n) {
    return !!(n.is_headwater ||
      (!(n.tributaries || []).length && !(n.tributaries_truncated || 0) && !n.mainline));
  }

  // --- per-river UI state (which sub-basins the user forced open/closed) --
  // key: river id -> true (forced open) / false (forced closed) / undefined
  const forced = new Map();

  // --- the layout walk --------------------------------------------------
  // Returns {rows, segs, maxLane}. A "row" is one visible river; "segs" are
  // vertical lane runs. minOrder gates a whole sub-basin: if its richest
  // river is below the threshold and the user has not forced it open, it
  // folds to a +N leaf on the row it joins.
  function layout(minOrder) {
    const rows = [], segs = [];
    const free = [];
    let nextLane = 0;
    const alloc = () => {
      if (free.length) { free.sort((a, b) => a - b); return free.shift(); }
      return nextLane++;
    };
    const release = (l) => free.push(l);

    const focus = focusMode && selectedId != null;

    function visibleTribs(node) {
      // visible tributaries (biggest first — input already sorted) plus the
      // count and the biggest hidden child, so a "+N" leaf can pivot into it
      const out = [];
      let folded = 0, biggestHidden = null;
      for (const t of node.tributaries || []) {
        const force = forced.get(t.id);
        let show;
        if (force === true) show = true;
        else if (force === false) show = false;
        else if (focus) {
          // spine is always open; inside the selection's basin the order
          // slider still prunes; everything else folds away
          show = spine.has(t.id) ||
            (selBasin.has(t.id) && subtreeMaxOrder(t) >= minOrder);
        } else {
          show = subtreeMaxOrder(t) >= minOrder;
        }
        if (show) out.push({ node: t });
        else {
          folded += subtreeCount(t);
          if (!biggestHidden) biggestHidden = t.id;  // list is sorted big->small
        }
      }
      folded += node.tributaries_truncated || 0;
      return { tribs: out, folded, biggestHidden };
    }

    function walk(node, lane, parentLane, depth) {
      const headRow = rows.length;
      let cur = node, curParentLane = parentLane;
      while (cur) {
        const curDepth = cur.depth != null ? cur.depth : depth;
        if (G.MAX_DEPTH != null && curDepth > G.MAX_DEPTH) {
          rows.push({ node: cur, lane, parentLane: curParentLane, depth: curDepth,
                      leaf: true, count: subtreeCount(cur), hasHidden: false });
          break;
        }
        const { tribs, folded, biggestHidden } = visibleTribs(cur);
        rows.push({ node: cur, lane, parentLane: curParentLane, depth: curDepth,
                    leaf: false, count: 0,
                    hasHidden: folded > 0, foldedCount: folded });
        curParentLane = null;  // only the branch head merges into a parent

        for (const item of tribs) {
          const tl = alloc();
          walk(item.node, tl, lane, curDepth + 1);
        }
        if (folded > 0) {
          const tl = alloc();
          rows.push({ node: { name: null, id: '+' + folded }, lane: tl,
                      parentLane: lane, depth: curDepth + 1, leaf: true,
                      count: folded, hasHidden: false,
                      foldInto: biggestHidden });
          segs.push({ lane: tl, top: rows.length - 1, bot: rows.length - 1,
                      owner: rows.length - 1 });
          release(tl);
        }
        cur = cur.mainline;
      }
      segs.push({ lane, top: headRow, bot: rows.length - 1, owner: headRow });
      release(lane);
    }

    walk(root, alloc(), null, 0);
    const maxLane = rows.reduce((m, r) => Math.max(m, r.lane), 0);
    return { rows, segs, maxLane };
  }

  // --- draw the graph SVG from a layout --------------------------------
  const FOLD_MARK = 14;  // px the "o-o-" collapsed marker overhangs its lane
  function drawGraph(lo) {
    const n = lo.rows.length;
    // widen the box if any collapsed leaf's marker would run past the last lane
    const hasFold = lo.rows.some(
      r => r.leaf && String(r.node.id || '').startsWith('+'));
    const laneW = G.LANE_PAD + (lo.maxLane + 1) * G.LANE_W +
      (hasFold ? FOLD_MARK : 0);
    const h = n * G.ROW_H;

    // vertical lane segments, higher lanes first so the trunk overpaints
    const segs = lo.segs.slice().sort((a, b) => b.lane - a.lane);
    let lines = '';
    for (const s of segs) {
      const [w, c] = orderStyle(lo.rows[s.owner].node.strahler);
      const x = laneX(s.lane);
      lines += '<line x1="' + x.toFixed(1) + '" y1="' + rowY(s.top).toFixed(1) +
               '" x2="' + x.toFixed(1) + '" y2="' + rowY(s.bot).toFixed(1) +
               '" stroke="' + c + '" stroke-width="' + w.toFixed(1) + '"/>';
    }

    let elbows = '', dots = '';
    lo.rows.forEach((r, i) => {
      const [w, c] = orderStyle(r.node.strahler);
      const cx = laneX(r.lane), cy = rowY(i);
      if (r.parentLane != null) {
        // a merge that comes OFF the parent lane just above this river's dot:
        // a single smooth cubic from a point one row up on the parent lane
        // across into the dot. No straight vertical stub running alongside the
        // parent (that read as a doubled line); the curve's control points sit
        // on the two lanes so it leaves vertical and arrives horizontal.
        const px = laneX(r.parentLane);
        const ew = Math.max(w, 1.4);
        const y0 = cy - G.ROW_H;              // start: a row up, on the parent
        elbows += '<path d="M' + px.toFixed(1) + ',' + y0.toFixed(1) +
                  ' C' + px.toFixed(1) + ',' + (y0 + G.ROW_H * 0.55).toFixed(1) +
                  ' ' + cx.toFixed(1) + ',' + (cy - G.ROW_H * 0.55).toFixed(1) +
                  ' ' + cx.toFixed(1) + ',' + cy.toFixed(1) + '" ' +
                  'stroke="' + c + '" stroke-width="' + ew.toFixed(1) + '"/>';
      }
      const collapsed = r.leaf && String(r.node.id || '').startsWith('+');
      if (collapsed) {
        // a "+N folded sub-basin" marker: two hollow dots joined by a stub,
        // trailing off toward the label — reads as a compressed "o-o-" chain
        const r1 = 2.6, gap = 6, r2 = 1.9, tail = 4;
        const x1 = cx, x2 = cx + gap;
        dots += '<circle cx="' + x1.toFixed(1) + '" cy="' + cy.toFixed(1) +
                '" r="' + r1 + '" fill="' + G.BG + '" stroke="' + c +
                '" stroke-width="1.4"/>';
        dots += '<line x1="' + (x1 + r1).toFixed(1) + '" y1="' + cy.toFixed(1) +
                '" x2="' + (x2 - r2).toFixed(1) + '" y2="' + cy.toFixed(1) +
                '" stroke="' + c + '" stroke-width="1.4"/>';
        dots += '<circle cx="' + x2.toFixed(1) + '" cy="' + cy.toFixed(1) +
                '" r="' + r2 + '" fill="' + G.BG + '" stroke="' + c +
                '" stroke-width="1.3"/>';
        dots += '<line x1="' + (x2 + r2).toFixed(1) + '" y1="' + cy.toFixed(1) +
                '" x2="' + (x2 + r2 + tail).toFixed(1) + '" y2="' + cy.toFixed(1) +
                '" stroke="' + c + '" stroke-width="1.3" stroke-dasharray="1.5 1.6"/>';
      } else if (r.leaf) {
        dots += '<circle cx="' + cx.toFixed(1) + '" cy="' + cy.toFixed(1) +
                '" r="2.3" fill="' + G.BG + '" stroke="' + c + '" stroke-width="1.5"/>';
      } else {
        dots += '<circle cx="' + cx.toFixed(1) + '" cy="' + cy.toFixed(1) +
                '" r="' + G.DOT_R.toFixed(1) + '" fill="' + c + '"/>';
      }
    });

    graphSvg.setAttribute('width', laneW);
    graphSvg.setAttribute('height', h);
    graphSvg.setAttribute('viewBox', '0 0 ' + laneW + ' ' + h);
    graphSvg.innerHTML =
      '<g stroke-linecap="round" fill="none">' + lines + elbows + '</g>' +
      '<g stroke-linecap="round">' + dots + '</g>';
    // keep the label column aligned with lane 0
    document.querySelector('.graphwrap').style.gridTemplateColumns =
      (laneW + G.LABEL_GAP) + 'px ' + G.REST_COLS;
  }

  // --- draw the label list from a layout ------------------------------
  function esc(s) {
    return String(s).replace(/[&<>"]/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }
  function alsoHtml(n) {
    const a = n.also_flows_into || [];
    if (!a.length) return '';
    const names = a.map(x => esc(x.name || x.id)).join(', ');
    return ' <span class="also" title="bifurcation">⋔ also → ' + names + '</span>';
  }

  function drawLabels(lo) {
    let out = '';
    for (const r of lo.rows) {
      const n = r.node;
      if (r.leaf && !n.name && String(n.id || '').startsWith('+')) {
        const into = r.foldInto ? ' data-fold-into="' + esc(r.foldInto) + '"' : '';
        const hint = (focusMode && r.foldInto) ? ' — click to open' : '';
        out += '<li class="row leaf"' + into + ' style="height:' + G.ROW_H + 'px">' +
               '<span class="caret" hidden></span>' +
               '<span class="name unnamed">+' + r.count + ' rivers' + hint + '</span></li>';
        continue;
      }
      const named = !!n.name;
      const cls = named ? 'name' : 'name unnamed';
      const label = esc(named ? n.name : (n.id || '?'));
      const leaf = r.leaf ? ' leaf' : '';
      const extra = r.leaf ? ' <span class="more">+' + r.count + '</span>' : '';
      const src = (isSource(n) && !r.leaf) ? ' ▲' : '';
      let caret = '<span class="caret" hidden></span>';
      if (!r.leaf && r.hasHidden) {
        caret = '<button class="caret" data-open="' +
                (forced.get(n.id) === true ? '1' : '0') + '" data-id="' +
                esc(n.id) + '" title="' + r.foldedCount +
                ' hidden upstream">▸</button>';
      } else if (!r.leaf && forced.get(n.id) === true) {
        // opened past the slider; offer to re-fold
        caret = '<button class="caret open" data-open="1" data-id="' + esc(n.id) +
                '" title="fold this sub-basin">▸</button>';
      }
      out += '<li class="row' + leaf + '" data-id="' + esc(n.id || '') +
             '" style="height:' + G.ROW_H + 'px">' + caret +
             '<span class="' + cls + '">' + label + src + '</span>' + extra +
             alsoHtml(n) + '</li>';
    }
    labelsEl.innerHTML = out;
    applyFilter();
  }

  // --- render = layout + both columns --------------------------------
  let focusMode = !!(focusBox && focusBox.checked);
  let curLayout = null;

  function drawCrumbs() {
    if (!crumbEl) return;
    if (!focusMode || !spinePath.length) { crumbEl.innerHTML = ''; return; }
    crumbEl.innerHTML = spinePath.map((n, i) => {
      const label = esc(n.name || n.id);
      const last = i === spinePath.length - 1;
      return last
        ? '<b>' + label + '</b>'
        : '<a href="#" data-id="' + esc(n.id) + '">' + label + '</a>';
    }).join('<span class="sep">›</span>');
  }

  function render() {
    const minOrder = +slider.value;
    orderOut.textContent = minOrder;
    curLayout = layout(minOrder);
    drawGraph(curLayout);
    drawLabels(curLayout);
    drawCrumbs();
    const drawn = curLayout.rows.filter(r => !r.leaf).length;
    const total = meta.n_nodes || drawn;
    let note = drawn < total
      ? ' · ' + drawn + ' of ' + total + ' rivers shown'
      : ' · ' + drawn + ' rivers';
    if (focusMode && selectedId != null) note += ' · folded to selection';
    metaExtra.textContent = note;
    if (window._catalogGeoSync) window._catalogGeoSync();
  }

  // a selection changed (from the map, a row, a +N leaf, or a crumb)
  function focusOn(id) {
    recomputeSelection(id);
    if (window._catalogMapSelect) window._catalogMapSelect(id);
    render();
  }
  window._catalogFocusOn = focusOn;

  slider.addEventListener('input', render);
  if (focusBox) focusBox.addEventListener('change', () => {
    focusMode = focusBox.checked;
    render();
  });
  if (crumbEl) crumbEl.addEventListener('click', ev => {
    const a = ev.target.closest('a[data-id]');
    if (!a) return;
    ev.preventDefault();
    focusOn(a.dataset.id);
  });
  labelsEl.addEventListener('click', ev => {
    const btn = ev.target.closest('.caret[data-id]');
    if (!btn) return;
    ev.stopPropagation();
    const id = btn.dataset.id;
    forced.set(id, btn.dataset.open === '1' ? false : true);
    render();
  });

  // --- name filter (hides rows; does not relayout the graph) ---------
  function applyFilter() {
    const t = (filterEl.value || '').trim().toLowerCase();
    for (const li of labelsEl.querySelectorAll('.row')) {
      const nameEl = li.querySelector('.name');
      const raw = nameEl ? nameEl.textContent : '';
      if (nameEl) nameEl.textContent = raw;
      if (!t) { li.classList.remove('hidden'); continue; }
      const i = raw.toLowerCase().indexOf(t);
      if (i < 0) { li.classList.add('hidden'); continue; }
      li.classList.remove('hidden');
      if (nameEl) {
        nameEl.innerHTML = esc(raw.slice(0, i)) + '<mark>' +
          esc(raw.slice(i, i + t.length)) + '</mark>' + esc(raw.slice(i + t.length));
      }
    }
  }
  filterEl.addEventListener('input', applyFilter);

  // rows drive the selection (focus refold + map). Works with or without geo.
  labelsEl.addEventListener('click', ev => {
    if (ev.target.closest('.caret')) return;
    const leaf = ev.target.closest('.row.leaf[data-fold-into]');
    if (leaf) { focusOn(leaf.dataset.foldInto); return; }
    const li = ev.target.closest('.row[data-id]');
    if (li && li.dataset.id && nodeById[li.dataset.id]) focusOn(li.dataset.id);
  });

  render();
  if (window._catalogInitGeo) window._catalogInitGeo(data, root, meta, labelsEl, esc, focusOn);
})();
"""

# Only inlined when the catalog carries a ``geo`` block — keeps a plain
# catalog page (no ``geo=True``) fully offline, with zero references to any
# external host anywhere in its source. This is the one part of the page that
# needs network access: it draws a real Leaflet map, with IGN/OSM tile
# basemaps for topography, roads and (via labels on the IGN layer) summits,
# and the river network on top as styled vector layers.
_JS_GEO = r"""
window._catalogInitGeo = function (data, root, meta, labelsEl, esc, focusOn) {
  (function initGeo() {
    const geo = data.geo;
    const mapEl = document.getElementById('map');
    if (!geo || !geo.rivers || !geo.bbox || !mapEl || !window.L) return;
    document.body.classList.add('has-geo');

    // line weight scales with Strahler order, same idea as the git-graph
    // lanes but a wider spread so a trunk reads clearly against its
    // headwaters. `boost` fattens the picked-out selection over the faint ctx.
    function mapWidth(order, boost) {
      const o = Math.min(Math.max(order || 1, 1), 7);
      const w = (1.1 + (o - 1) * 0.7) * (boost || 1);
      return boost && boost > 1 ? Math.max(w, 2.2) : w;
    }

    const node = {}, up = {};
    (function w(n) {
      node[n.id] = n;
      const kids = [];
      if (n.mainline) kids.push(n.mainline);
      for (const t of n.tributaries || []) kids.push(t);
      let acc = [];
      for (const k of kids) acc = acc.concat(w(k));
      up[n.id] = acc;
      return acc.concat([n.id]);
    })(root);

    const [BW, BS, BE, BN] = geo.bbox;
    const bounds = L.latLngBounds([BS, BW], [BN, BE]);

    const map = L.map(mapEl, {
      zoomControl: true, attributionControl: true, minZoom: 6, maxZoom: 17,
    });
    map.fitBounds(bounds, { padding: [14, 14] });

    // --- basemaps: IGN topo (key-free Plan IGN) and OSM, radio-selected ---
    const ignPlan = L.tileLayer(
      'https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0' +
      '&LAYER=GEOGRAPHICALGRIDSYSTEMS.PLANIGNV2&STYLE=normal&FORMAT=image/png' +
      '&TILEMATRIXSET=PM&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}',
      { maxNativeZoom: 16, maxZoom: 17, attribution: 'Plan IGN — IGN/Geoportail' });
    const osm = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxNativeZoom: 19, maxZoom: 19, attribution: '&copy; OpenStreetMap contributors',
    });
    // key-free hillshade ("estompage"), its own tile matrix set (PM_0_15).
    const hillshade = L.tileLayer(
      'https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0' +
      '&LAYER=ELEVATION.ELEVATIONGRIDCOVERAGE.SHADOW&STYLE=estompage_grayscale' +
      '&FORMAT=image/png&TILEMATRIXSET=PM_0_15&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}',
      { maxNativeZoom: 15, maxZoom: 17, opacity: 0.45, attribution: 'Estompage — IGN/Geoportail' });

    ignPlan.addTo(map);

    // river network panes, drawn above the basemap
    const ctxPane = L.featureGroup().addTo(map);
    const hiPane = L.featureGroup().addTo(map);

    function toLatLngs(subs) {
      return subs.map(sub => sub.map(([lon, lat]) => [lat, lon]));
    }

    for (const id in geo.rivers) {
      L.polyline(toLatLngs(geo.rivers[id].line), {
        color: '#7c8894', weight: mapWidth((node[id] || {}).strahler),
        opacity: 0.55, lineCap: 'round', lineJoin: 'round',
      }).addTo(ctxPane);
    }

    const cap = document.getElementById('infobox');
    let current = null, currentId = null;
    function cssEsc(s) {
      return window.CSS && CSS.escape ? CSS.escape(s) : String(s).replace(/"/g, '\\"');
    }
    function boundsOf(ids) {
      const pts = [];
      for (const id of ids) {
        const g = geo.rivers[id];
        if (!g) continue;
        for (const sub of g.line) for (const [lon, lat] of sub) pts.push([lat, lon]);
      }
      return pts.length ? L.latLngBounds(pts) : null;
    }

    // paint the map for a selection; the graph refold is driven separately
    // by the outer focusOn(), which calls this.
    function paintMap(id) {
      const g = geo.rivers[id];
      if (!g) return;
      currentId = id;
      hiPane.clearLayers();
      for (const uid of up[id] || []) {
        const ug = geo.rivers[uid];
        if (!ug) continue;
        L.polyline(toLatLngs(ug.line), {
          color: '#5b6b7a', weight: mapWidth((node[uid] || {}).strahler, 1.25),
          opacity: 0.85, lineCap: 'round', lineJoin: 'round',
        }).addTo(hiPane);
      }
      L.polyline(toLatLngs(g.line), {
        color: '#2f6f4f', weight: mapWidth((node[id] || {}).strahler, 1.7),
        opacity: 1, lineCap: 'round', lineJoin: 'round',
      }).addTo(hiPane);
      L.circleMarker([g.outlet[1], g.outlet[0]], {
        radius: 4.5, color: '#fff', weight: 1, fillColor: '#2f6f4f', fillOpacity: 1,
      }).addTo(hiPane);

      const box = boundsOf([id].concat(up[id] || []));
      if (box) map.flyToBounds(box, { padding: [34, 34], duration: 0.4, maxZoom: 15 });

      const n = node[id] || {};
      const nUp = (up[id] || []).length;
      const stats = [];
      if (n.length_km != null) stats.push(['length', (+n.length_km) + ' km']);
      if (n.strahler) stats.push(['order', n.strahler]);
      stats.push(['upstream', nUp ? nUp + ' rivers' : 'headwater']);
      if (n.area_km2) stats.push(['area', (+n.area_km2) + ' km²']);
      if (n.pfafstetter) stats.push(['pfafstetter', esc(n.pfafstetter)]);
      const statsHtml = stats.map(([k, v]) =>
        '<span class="stat"><span class="v">' + v + '</span>' +
        '<span class="k">' + k + '</span></span>').join('');
      const a = n.also_flows_into || [];
      const alsoHtml = a.length
        ? '<span class="also" title="bifurcation">⋔ also flows into ' +
          a.map(x => esc(x.name || x.id)).join(', ') + '</span>'
        : '';
      cap.innerHTML =
        '<div class="title"><b>' + esc(n.name || id) +
        '</b><a href="#" id="mapreset">⤢ whole catchment</a></div>' +
        '<div class="stats">' + statsHtml + '</div>' + alsoHtml;
      document.getElementById('mapreset').addEventListener('click', ev => {
        ev.preventDefault(); map.flyToBounds(bounds, { padding: [14, 14], duration: 0.4 });
      });
      syncSelectedRow();
    }
    function syncSelectedRow() {
      if (current) current.classList.remove('selected');
      current = null;
      if (!currentId) return;
      const li = labelsEl.querySelector('.row[data-id="' + cssEsc(currentId) + '"]');
      if (li) { li.classList.add('selected'); current = li; }
    }
    window._catalogGeoSync = syncSelectedRow;
    window._catalogMapSelect = paintMap;

    // --- layer switcher UI: basemap radios + hillshade toggle -----------
    const baseRadios = document.querySelectorAll('input[name="maplayer"]');
    baseRadios.forEach(r => r.addEventListener('change', () => {
      map.removeLayer(ignPlan); map.removeLayer(osm);
      (r.value === 'osm' ? osm : ignPlan).addTo(map);
      ctxPane.bringToFront(); hiPane.bringToFront();
    }));
    const shadeBox = document.getElementById('maphillshade');
    if (shadeBox) shadeBox.addEventListener('change', () => {
      if (shadeBox.checked) { hillshade.addTo(map); ctxPane.bringToFront(); hiPane.bringToFront(); }
      else map.removeLayer(hillshade);
    });

    if (geo.rivers[meta.root_id]) focusOn(meta.root_id);
  })();
};
"""


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
