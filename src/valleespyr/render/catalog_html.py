"""Render a valley catalog (the nested dict from :func:`valleespyr.catalog.build_catalog`)
as one self-contained static HTML file — a **git-graph of the river network**.

Each *river* (a branch) is one **lane**: a coloured vertical line in its own
column, tinted by Strahler order (dark & thick for the trunk, pale & thin for a
headwater). A tributary lane runs down from the row where it branches off its
parent, curving left into the parent lane at that row — a merge. The parent lane
runs straight through, never interrupted. There is **one row per river**, not per
reach: the confluence points BD TOPO splits a watercourse into are not drawn.
Row order is a depth-first walk from the root; lanes are recycled the moment a
tributary's whole sub-basin has been drawn, so the graph stays narrow.

The layout is computed here and emitted as a single inline ``<svg>`` next to a
column of labels (name + length / order / upstream count / Pfafstetter code).
Beyond ``--max-depth`` a branch collapses to one ``+N rivers`` leaf row. No
server, no CDN: inline CSS + a few lines of JS for a name filter.

``render_catalog_html(catalog, path)`` writes the file; ``catalog_to_html`` gives
the string.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# --- geometry -----------------------------------------------------------------
ROW_H = 24          # px per row
LANE_W = 15         # px per lane column
LANE_PAD = 10       # left padding before lane 0
DOT_R = 3.2         # merge/branch dot radius
LABEL_GAP = 14      # px between the lane area and the label column

# Strahler order -> (stroke width, colour). Clamped to the table ends.
_ORDER_STYLE = [
    (1.1, "#9db8d0"),  # 1 — pale headwater
    (1.4, "#7ba3c9"),  # 2
    (1.8, "#5b8bbf"),  # 3
    (2.3, "#3f74af"),  # 4
    (2.8, "#2f6096"),  # 5
    (3.4, "#244d7c"),  # 6
    (4.0, "#1c3e63"),  # 7+ — trunk
]


def _order_style(order: int | None) -> tuple[float, str]:
    i = 0 if not order else min(max(order, 1), len(_ORDER_STYLE)) - 1
    return _ORDER_STYLE[i]


# --- layout -----------------------------------------------------------------


@dataclass
class Row:
    """One river on one graph row."""

    node: dict[str, Any]
    lane: int
    parent_lane: int | None       # lane this river merges into (None for the root)
    depth: int
    is_collapsed_leaf: bool = False
    collapsed_count: int = 0


@dataclass
class Seg:
    """A vertical run of one lane, from row ``top`` to row ``bot`` inclusive,
    owned by the river whose head row is ``owner`` (for its style)."""

    lane: int
    top: int
    bot: int
    owner: int


class _Layout:
    """Depth-first row + lane assignment for one catalog tree.

    Row order: a river, then each of its tributaries' sub-trees (biggest first),
    then its mainline continuation — pre-order DFS, so a parent sits above every
    river in its catchment. The root's branch holds lane 0; a tributary takes
    the lowest lane not currently in use and releases it the moment its last
    descendant row is emitted. Each *branch* (a river + its mainline chain)
    records one :class:`Seg` for the vertical line it owns.
    """

    def __init__(self, root: dict[str, Any], *, max_depth: int | None):
        self.rows: list[Row] = []
        self.segs: list[Seg] = []
        self.max_depth = max_depth
        self._free: list[int] = []
        self._next_lane = 0
        self._walk(root, lane=self._alloc(), parent_lane=None, depth=0)
        self.max_lane = max((r.lane for r in self.rows), default=0)

    def _alloc(self) -> int:
        if self._free:
            lane = min(self._free)
            self._free.remove(lane)
            return lane
        lane = self._next_lane
        self._next_lane += 1
        return lane

    def _release(self, lane: int) -> None:
        self._free.append(lane)

    def _walk(
        self, node: dict[str, Any], *, lane: int, parent_lane: int | None, depth: int
    ) -> None:
        """Emit one branch (``node`` + its mainline chain) and recurse into every
        tributary. Records one vertical :class:`Seg` for the branch, spanning
        from its head row down to the LAST row of its whole sub-basin — so the
        lane's line runs unbroken past every tributary that hangs off it.

        Fold depth is the node's own ``depth`` (confluences from the root, set by
        :func:`valleespyr.catalog.build_catalog`); ``depth`` here is only the
        fallback when a node predates that field.
        """
        head_row = len(self.rows)
        cur: dict[str, Any] | None = node
        cur_parent_lane = parent_lane

        while cur is not None:
            cur_depth = cur.get("depth", depth)
            if self.max_depth is not None and cur_depth > self.max_depth:
                self.rows.append(
                    Row(cur, lane, cur_parent_lane, cur_depth,
                        is_collapsed_leaf=True, collapsed_count=_subtree_count(cur))
                )
                break

            self.rows.append(Row(cur, lane, cur_parent_lane, cur_depth))
            cur_parent_lane = None  # only the branch head merges into a parent

            for t in cur.get("tributaries") or []:
                tlane = self._alloc()
                self._walk(t, lane=tlane, parent_lane=lane, depth=cur_depth + 1)
            trunc = cur.get("tributaries_truncated") or 0
            if trunc:
                tlane = self._alloc()
                self.rows.append(
                    Row({"name": None, "id": f"+{trunc}"}, tlane, lane,
                        cur_depth + 1, is_collapsed_leaf=True, collapsed_count=trunc)
                )
                self.segs.append(Seg(tlane, len(self.rows) - 1, len(self.rows) - 1,
                                     len(self.rows) - 1))
                self._release(tlane)

            cur = cur.get("mainline")

        # the branch's line spans everything drawn since its head — its mainline
        # chain and every tributary sub-basin nested under it
        self.segs.append(Seg(lane, head_row, len(self.rows) - 1, head_row))
        self._release(lane)


def _subtree_count(node: dict[str, Any]) -> int:
    n = 1
    if node.get("mainline") is not None:
        n += _subtree_count(node["mainline"])
    for t in node.get("tributaries") or []:
        n += _subtree_count(t)
    n += node.get("tributaries_truncated") or 0
    return n


# --- SVG -------------------------------------------------------------------


def _lane_x(lane: int) -> float:
    return LANE_PAD + lane * LANE_W + LANE_W / 2


def _draw_svg(layout: _Layout) -> tuple[str, float]:
    """Return ``(svg_markup, lane_area_width_px)``.

    Drawn in three passes so lines sit under dots and the trunk (drawn last,
    lowest lane) sits on top of the pale headwater lines it crosses:
    vertical branch segments (:class:`Seg`), then merge curves, then dots.
    """
    n = len(layout.rows)
    lane_w = LANE_PAD + (layout.max_lane + 1) * LANE_W
    h = n * ROW_H

    def y(i: int) -> float:
        return i * ROW_H + ROW_H / 2

    # vertical segments, higher lane numbers first so the trunk overpaints them
    segs = sorted(layout.segs, key=lambda s: -s.lane)
    lines: list[str] = []
    for s in segs:
        r0 = layout.rows[s.owner]
        width, colour = _order_style(r0.node.get("strahler"))
        y1 = y(s.top)
        y2 = y(s.bot)
        lines.append(
            f'<line x1="{_lane_x(s.lane):.1f}" y1="{y1:.1f}" '
            f'x2="{_lane_x(s.lane):.1f}" y2="{y2:.1f}" '
            f'stroke="{colour}" stroke-width="{width:.1f}"/>'
        )

    curves: list[str] = []
    dots: list[str] = []
    for i, r in enumerate(layout.rows):
        width, colour = _order_style(r.node.get("strahler"))
        cx = _lane_x(r.lane)
        cy = y(i)

        if r.parent_lane is not None:
            px = _lane_x(r.parent_lane)
            # branch off the parent lane: start on it a full row up, S-curve
            # down-and-right into this river's dot
            ytop = cy - ROW_H
            cw = max(width, 1.6)
            curves.append(
                f'<path d="M{px:.1f},{ytop:.1f} '
                f'C{px:.1f},{cy - ROW_H * 0.15:.1f} '
                f'{cx:.1f},{cy - ROW_H * 0.55:.1f} '
                f'{cx:.1f},{cy:.1f}" '
                f'stroke="{colour}" stroke-width="{cw:.1f}"/>'
            )

        if r.is_collapsed_leaf:
            dots.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="2.3" fill="{_bg()}" '
                f'stroke="{colour}" stroke-width="1.5"/>'
            )
        else:
            dots.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{DOT_R:.1f}" '
                f'fill="{colour}"/>'
            )

    svg = (
        f'<svg class="graph" width="{lane_w}" height="{h}" '
        f'viewBox="0 0 {lane_w} {h}" aria-hidden="true">'
        f'<g stroke-linecap="round" fill="none">{"".join(lines)}{"".join(curves)}</g>'
        f'<g stroke-linecap="round">{"".join(dots)}</g>'
        "</svg>"
    )
    return svg, lane_w


def _bg() -> str:
    return "#fbfbfa"


# --- labels + document ----------------------------------------------------


def _fmt_facts(node: dict[str, Any]) -> str:
    bits = []
    if node.get("length_km") is not None:
        bits.append(f'<span class="k">len</span> {node["length_km"]:g} km')
    if node.get("strahler"):
        bits.append(f'<span class="k">ord</span> {node["strahler"]}')
    if node.get("n_upstream"):
        bits.append(f'<span class="k">up</span> {node["n_upstream"]}')
    if node.get("area_km2"):
        bits.append(f'<span class="k">area</span> {node["area_km2"]:g} km²')
    if node.get("pfafstetter"):
        bits.append(f'<span class="pfaf">{html.escape(node["pfafstetter"])}</span>')
    return " · ".join(bits)


def _also_html(node: dict[str, Any]) -> str:
    also = node.get("also_flows_into") or []
    if not also:
        return ""
    names = ", ".join(html.escape(a["name"] or a["id"]) for a in also)
    return f' <span class="also" title="bifurcation">⋔ also → {names}</span>'


def _labels_html(layout: _Layout) -> str:
    out = ['<ol class="labels">']
    for r in layout.rows:
        node = r.node
        if r.is_collapsed_leaf and not node.get("name") and str(node.get("id", "")).startswith("+"):
            out.append(
                f'<li class="row leaf" style="height:{ROW_H}px">'
                f'<span class="name unnamed">+{r.collapsed_count} rivers upstream</span>'
                "</li>"
            )
            continue
        name = node.get("name")
        if name:
            cls = "name"
            label = html.escape(name)
        else:
            cls = "name unnamed"
            label = html.escape(str(node.get("id", "?")))
        leaf = " leaf" if r.is_collapsed_leaf else ""
        extra = (
            f' <span class="more">+{r.collapsed_count}</span>'
            if r.is_collapsed_leaf
            else ""
        )
        src = " ▲" if _is_source(node) and not r.is_collapsed_leaf else ""
        out.append(
            f'<li class="row{leaf}" data-id="{html.escape(str(node.get("id", "")))}" '
            f'style="height:{ROW_H}px">'
            f'<span class="{cls}">{label}{src}</span>{extra}'
            f'<span class="facts">{_fmt_facts(node)}{_also_html(node)}</span>'
            "</li>"
        )
    out.append("</ol>")
    return "".join(out)


def _is_source(node: dict[str, Any]) -> bool:
    return bool(
        node.get("is_headwater")
        or (
            not (node.get("tributaries") or [])
            and not (node.get("tributaries_truncated") or 0)
            and node.get("mainline") is None
        )
    )


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
.controls {{ margin-top: 9px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
.controls input[type=search] {{
  padding: 5px 9px; border: 1px solid var(--line); border-radius: 6px;
  font: inherit; min-width: 220px; background: #fff;
}}
.legend {{ display: flex; gap: 10px; align-items: center; color: var(--faint); font-size: 11px; }}
.legend i {{ width: 20px; height: 0; border-top-style: solid; display: inline-block;
  vertical-align: middle; margin-right: 3px; }}

main {{ padding: 8px 22px 60px; }}
.graphwrap {{
  position: relative; display: grid;
  grid-template-columns: {lane_w}px 1fr; align-items: start;
}}
svg.graph {{ display: block; position: sticky; left: 0; }}
ol.labels {{ list-style: none; margin: 0; padding: 0; }}
.labels .row {{
  display: flex; align-items: center; gap: 10px;
  padding: 0 8px; white-space: nowrap;
}}
.labels .row:hover {{ background: #fff; box-shadow: inset 0 0 0 1px var(--line); }}
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

_JS = r"""
(function () {
  const q = document.getElementById('filter');
  const rows = Array.from(document.querySelectorAll('.labels .row'));
  q.addEventListener('input', () => {
    const t = q.value.trim().toLowerCase();
    rows.forEach(li => {
      const nameEl = li.querySelector('.name');
      const raw = nameEl ? nameEl.textContent : '';
      if (nameEl) nameEl.textContent = raw;  // clear old <mark>
      if (!t) { li.classList.remove('hidden'); return; }
      const i = raw.toLowerCase().indexOf(t);
      if (i < 0) { li.classList.add('hidden'); return; }
      li.classList.remove('hidden');
      if (nameEl) {
        const a = raw.slice(0, i), b = raw.slice(i, i + t.length), c = raw.slice(i + t.length);
        nameEl.innerHTML = a + '<mark>' + b + '</mark>' + c;
      }
    });
  });
})();
"""


def catalog_to_html(catalog: dict[str, Any], *, max_depth: int | None = None) -> str:
    """Return the self-contained git-graph HTML document for a catalog dict.

    The graph draws whatever is in the catalog tree; wherever the tree was
    already truncated (``tributaries_truncated``) a ``+N rivers`` leaf row
    stands in. ``max_depth`` optionally folds the graph further, to keep a very
    deep tree a sane height without rebuilding the JSON.
    """
    meta = catalog.get("meta", {})
    root = catalog["root"]
    title = meta.get("root_name") or meta.get("root_id") or "valley catalog"

    layout = _Layout(root, max_depth=max_depth)
    svg, lane_w = _draw_svg(layout)
    labels = _labels_html(layout)

    n_drawn = sum(1 for r in layout.rows if not r.is_collapsed_leaf)
    total = meta.get("n_nodes", n_drawn)
    depth_note = (
        f" · {n_drawn} of {total} rivers drawn (deeper branches folded to +N)"
        if n_drawn < total
        else f" · {n_drawn} rivers"
    )
    metaline = (
        f"catchment of <b>{html.escape(title)}</b> as a river git-graph — "
        f"each lane is one river, tinted by Strahler order; a lane curves into "
        f"its parent where the two meet{depth_note}"
    )

    legend = "".join(
        f'<span><i style="border-top-width:{w:.0f}px;border-top-color:{c}"></i>'
        f'ord {min(i + 1, 7)}{"+" if i == len(_ORDER_STYLE) - 1 else ""}</span>'
        for i, (w, c) in enumerate(_ORDER_STYLE)
    )

    css = _CSS_TMPL.format(lane_w=lane_w + LABEL_GAP)
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
    <span class="legend">{legend}</span>
  </div>
</header>
<main>
  <div class="graphwrap">{svg}{labels}</div>
</main>
<script id="catalog-data" type="application/json">{payload}</script>
<script>{_JS}</script>
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
