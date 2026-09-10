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
column holds a sticky mini-map: clicking a row draws just that river and its
upstream network there, lon/lat projected in the browser, fit to frame.

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
MAP_MAX_VW = 50     # % of window width — ceiling for the map column
MAP_VB_W = 720      # SVG viewBox width  (internal units)
MAP_VB_H = 760      # SVG viewBox height (internal units)

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
.orderctl b {{ color: var(--ink); font-variant-numeric: tabular-nums; }}
.orderctl .n {{ color: var(--faint); }}
.legend {{ display: flex; gap: 10px; align-items: center; color: var(--faint); font-size: 11px; }}
.legend i {{ width: 20px; height: 0; border-top-style: solid; display: inline-block;
  vertical-align: middle; margin-right: 3px; }}

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
#map {{ display: block; width: 100%; height: auto;
  aspect-ratio: {map_ar}; background: #fbfcfd; }}
#map path, #map circle {{ vector-effect: non-scaling-stroke; }}
#map .ctx {{ fill: none; stroke: #d3dae1; stroke-width: 1;
  stroke-linecap: round; stroke-linejoin: round; }}
#map .up {{ fill: none; stroke: #94a3b1; stroke-width: 1.3;
  stroke-linecap: round; stroke-linejoin: round; }}
#map .sel {{ fill: none; stroke: var(--accent); stroke-linecap: round;
  stroke-linejoin: round; stroke-width: 2.8; }}
#map .outlet {{ fill: var(--accent); stroke: #fff; stroke-width: 1; }}
.mapcap {{
  padding: 8px 10px; border-top: 1px solid var(--line); font-size: 11.5px;
  color: var(--dim); line-height: 1.5; min-height: 34px;
}}
.mapcap b {{ color: var(--ink); }}
.mapcap a {{ color: var(--accent); text-decoration: none; white-space: nowrap; }}
.mapcap a:hover {{ text-decoration: underline; }}
.mapcap .hint {{ color: var(--faint); }}
.name {{ font-weight: 600; }}
.name.unnamed {{ color: var(--faint); font-weight: 400;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; }}
.row.leaf .name {{ font-weight: 400; color: var(--faint); font-style: italic; }}
.more {{ color: var(--faint); font-size: 11px; }}
.facts {{ color: var(--dim); font-size: 11.5px; overflow: hidden; text-overflow: ellipsis; }}
.facts .k {{ color: var(--faint); }}
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

    function visibleTribs(node) {
      // [{node} | {fold: n, count}], biggest first (input already sorted)
      const out = [];
      let folded = 0;
      for (const t of node.tributaries || []) {
        const force = forced.get(t.id);
        const show = force === true ||
          (force !== false && subtreeMaxOrder(t) >= minOrder);
        if (show) out.push({ node: t });
        else folded += subtreeCount(t);
      }
      folded += node.tributaries_truncated || 0;
      return { tribs: out, folded };
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
        const { tribs, folded } = visibleTribs(cur);
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
                      count: folded, hasHidden: false });
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
  function drawGraph(lo) {
    const n = lo.rows.length;
    const laneW = G.LANE_PAD + (lo.maxLane + 1) * G.LANE_W;
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
      if (r.leaf) {
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
  function facts(n) {
    const b = [];
    if (n.length_km != null) b.push('<span class="k">len</span> ' + (+n.length_km) + ' km');
    if (n.strahler) b.push('<span class="k">ord</span> ' + n.strahler);
    if (n.n_upstream) b.push('<span class="k">up</span> ' + n.n_upstream);
    if (n.area_km2) b.push('<span class="k">area</span> ' + (+n.area_km2) + ' km²');
    if (n.pfafstetter) b.push('<span class="pfaf">' + esc(n.pfafstetter) + '</span>');
    return b.join(' · ');
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
        out += '<li class="row leaf" style="height:' + G.ROW_H + 'px">' +
               '<span class="caret" hidden></span>' +
               '<span class="name unnamed">+' + r.count + ' rivers upstream</span></li>';
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
             '<span class="facts">' + facts(n) + alsoHtml(n) + '</span></li>';
    }
    labelsEl.innerHTML = out;
    applyFilter();
  }

  // --- render = layout + both columns --------------------------------
  let curLayout = null;
  function render() {
    const minOrder = +slider.value;
    orderOut.textContent = minOrder;
    curLayout = layout(minOrder);
    drawGraph(curLayout);
    drawLabels(curLayout);
    const drawn = curLayout.rows.filter(r => !r.leaf).length;
    const total = meta.n_nodes || drawn;
    metaExtra.textContent = drawn < total
      ? ' · ' + drawn + ' of ' + total + ' rivers shown'
      : ' · ' + drawn + ' rivers';
    if (window._catalogGeoSync) window._catalogGeoSync();
  }

  slider.addEventListener('input', render);
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

  // --- catchment mini-map (only when the catalog carries a geo block) ---
  (function initGeo() {
    const geo = data.geo;
    const svg = document.getElementById('map');
    if (!geo || !geo.rivers || !geo.bbox || !svg) return;
    document.body.classList.add('has-geo');

    const W = svg.viewBox.baseVal.width, H = svg.viewBox.baseVal.height, PAD = 14;
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
    const bdx = (BE - BW) || 1e-6, bdy = (BN - BS) || 1e-6;
    const K = Math.min((W - 2 * PAD) / bdx, (H - 2 * PAD) / bdy);
    const OX = (W - K * bdx) / 2, OY = (H - K * bdy) / 2;
    const px = lon => OX + (lon - BW) * K;
    const py = lat => OY + (BN - lat) * K;

    function pathD(subs) {
      let d = '';
      for (const sub of subs)
        d += sub.map((p, i) => (i ? 'L' : 'M') + px(p[0]).toFixed(1) + ' ' +
          py(p[1]).toFixed(1)).join('');
      return d;
    }
    function boxOf(ids) {
      let w = Infinity, s = Infinity, e = -Infinity, nn = -Infinity;
      for (const id of ids) {
        const g = geo.rivers[id];
        if (!g) continue;
        for (const sub of g.line) for (const [lon, lat] of sub) {
          if (lon < w) w = lon; if (lon > e) e = lon;
          if (lat < s) s = lat; if (lat > nn) nn = lat;
        }
      }
      return isFinite(w) ? [w, s, e, nn] : null;
    }

    let ctx = '';
    for (const id in geo.rivers)
      ctx += '<path class="ctx" d="' + pathD(geo.rivers[id].line) + '"/>';
    svg.innerHTML = '<g class="ctxg">' + ctx + '</g><g class="hi"></g>';
    const hi = svg.querySelector('.hi');

    let anim = null;
    function setVB(v) { svg.setAttribute('viewBox', v.map(n => n.toFixed(1)).join(' ')); }
    function easeVB(to) {
      const from = [svg.viewBox.baseVal.x, svg.viewBox.baseVal.y,
                    svg.viewBox.baseVal.width, svg.viewBox.baseVal.height];
      if (anim) cancelAnimationFrame(anim);
      if (!window.requestAnimationFrame) { setVB(to); return; }
      const t0 = performance.now(), dur = 260;
      setVB(to);
      (function step(now) {
        let u = Math.min(1, (now - t0) / dur);
        u = u < .5 ? 2 * u * u : 1 - Math.pow(-2 * u + 2, 2) / 2;
        setVB(from.map((f, i) => f + (to[i] - f) * u));
        if (u < 1) anim = requestAnimationFrame(step); else setVB(to);
      })(t0);
    }
    const MIN_SPAN = Math.min(W, H) * 0.42;
    function frameToPx(box, mf) {
      let cx = (px(box[0]) + px(box[2])) / 2, cy = (py(box[1]) + py(box[3])) / 2;
      let w = Math.abs(px(box[2]) - px(box[0])), h = Math.abs(py(box[1]) - py(box[3]));
      w += 2 * Math.max(w, h) * mf; h += 2 * Math.max(w, h) * mf;
      w = Math.max(w, MIN_SPAN); h = Math.max(h, MIN_SPAN);
      const ar = W / H;
      if (w / h < ar) w = h * ar; else h = w / ar;
      let x0 = cx - w / 2, y0 = cy - h / 2;
      if (w >= W) { x0 = 0; w = W; } else x0 = Math.max(0, Math.min(x0, W - w));
      if (h >= H) { y0 = 0; h = H; } else y0 = Math.max(0, Math.min(y0, H - h));
      return [x0, y0, w, h];
    }

    const cap = document.getElementById('mapcap');
    let current = null, currentId = null;
    function cssEsc(s) {
      return window.CSS && CSS.escape ? CSS.escape(s) : String(s).replace(/"/g, '\\"');
    }

    function select(id) {
      const g = geo.rivers[id];
      if (!g) return;
      currentId = id;
      let parts = '';
      for (const uid of up[id] || []) {
        const ug = geo.rivers[uid];
        if (ug) parts += '<path class="up" d="' + pathD(ug.line) + '"/>';
      }
      parts += '<path class="sel" d="' + pathD(g.line) + '"/>';
      const ox = px(g.outlet[0]), oy = py(g.outlet[1]);
      parts += '<circle class="outlet" cx="' + ox.toFixed(1) + '" cy="' +
               oy.toFixed(1) + '" r="3.2"/>';
      hi.innerHTML = parts;
      const box = boxOf([id].concat(up[id] || []));
      if (box) easeVB(frameToPx(box, 0.18));
      const n = node[id] || {};
      const bits = [];
      if (n.length_km != null) bits.push(n.length_km + ' km');
      if (n.area_km2) bits.push(n.area_km2 + ' km²');
      const nUp = (up[id] || []).length;
      bits.push(nUp ? nUp + ' rivers upstream' : 'headwater');
      cap.innerHTML = '<b>' + esc(n.name || id) + '</b> · ' + bits.join(' · ') +
        ' · <a href="#" id="mapreset">⤢ whole catchment</a>';
      document.getElementById('mapreset').addEventListener('click', ev => {
        ev.preventDefault(); easeVB([0, 0, W, H]);
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

    labelsEl.addEventListener('click', ev => {
      const li = ev.target.closest('.row[data-id]');
      if (li && !ev.target.closest('.caret') && geo.rivers[li.dataset.id])
        select(li.dataset.id);
    });

    if (geo.rivers[meta.root_id]) select(meta.root_id);
  })();

  render();
})();
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
    # open at an order that keeps the initial graph legible for a big basin
    start_order = 1 if max_ord <= 4 else (2 if max_ord <= 6 else 3)

    if has_geo:
        map_w_css = f"clamp({MAP_MIN}px, {MAP_MAX_VW}vw, 50vw)"
        rest_cols = f"minmax(220px, 1fr) {map_w_css}"
        map_col = (
            f'<div class="mapcol"><div class="mapcard">'
            f'<svg id="map" viewBox="0 0 {MAP_VB_W} {MAP_VB_H}" '
            f'preserveAspectRatio="xMidYMid meet" aria-label="selected river network"></svg>'
            f'<div id="mapcap" class="mapcap"><span class="hint">click a river…</span></div>'
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
        + (" · click a row to map its network" if has_geo else "")
    )

    legend = "".join(
        f'<span><i style="border-top-width:{w:.0f}px;border-top-color:{c}"></i>'
        f'ord {min(i + 1, 7)}{"+" if i == len(_ORDER_STYLE) - 1 else ""}</span>'
        for i, (w, c) in enumerate(_ORDER_STYLE)
    )

    css = _CSS_TMPL.format(
        grid_cols=grid_cols,
        map_ar=f"{MAP_VB_W} / {MAP_VB_H}",
        map_top=104,
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

    payload = json.dumps(catalog, ensure_ascii=False).replace("<", "\\u003c")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} — river git-graph</title>
<style>{css}</style>
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
    <span class="legend">{legend}</span>
  </div>
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
