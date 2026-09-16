"""Walk the river graph into a browsable **valley catalog** — a JSON tree with a
*git-history* shape.

:mod:`valleespyr.hydro.rivers` gives us the topology: a contracted DAG where
``u -> v`` means "u flows into v", plus :meth:`RiverNetwork.children` /
:meth:`~RiverNetwork.upstream_rivers` / :meth:`~RiverNetwork.downstream_path`.
The natural mental model for it is a **git commit graph**, not a directory tree:

* a river   ↔ a commit (opaque stable id = its ``cours_d_eau``);
* its downstream river ↔ its parent commit;
* a confluence with 2+ inflows ↔ a merge commit;
* the trunk to the sea ↔ ``main``;
* :meth:`~RiverNetwork.downstream_path` ↔ ``git log --first-parent`` from a
  headwater back to ``main``.

So each node here splits its upstream neighbours into:

* **``mainline``** — the *one* child that continues this same valley system
  upstream (``HEAD~1`` on the same branch): a child sharing this river's
  ``cours_d_eau`` name if there is one, else the child with the largest
  sub-catchment. Walking ``mainline`` repeatedly follows the trunk to its
  source. ``None`` at a headwater.
* **``tributaries``** — every other child, each the tip of a branch that
  *merges* into this lineage at this confluence. Ordered by where it joins,
  mouth to source — the order you'd meet them walking upstream on the map.

A river with more than one downstream (a real bifurcation — anabranch, delta,
canal tap) is attached under its first downstream in topological order (``git``'s
first parent) and the alternates are recorded on the node as ``also_flows_into``.

Every node also carries the browse facts: length, Strahler order, valleys
upstream, a study-local Pfafstetter code, and — where a
``bassin_versant_topographique`` dump is passed as ``bassins`` — a drainage
``area_km2``.

With ``geo=True`` the catalog also gets a flat ``geo`` block: the catchment
bbox plus, per river id, its own tronçon centreline as one or more simplified
``[lon, lat]`` sub-lines and its outlet coordinate. That is what the HTML
renderer's Leaflet map draws — the whole catchment faint, the selected river
and its upstream network picked out on top. With ``bassins`` also given, a
river whose drainage area reads as "one valley" (roughly 5-150 km²) additionally
gets its own catchment boundary, so the map can mask everything outside it —
each valley shown as its own bounded world rather than a flat bird's-eye plane.
The WFS dump only covers a fraction of rivers; an optional ``dem_catchments``
(a precomputed batch of DEM-delineated polygons, see
``valleespyr valley catchments precompute``) fills the gap for a valley-sized
river the WFS misses, without ever overriding a WFS hit.

Pure graph + shapely. ``bassins`` is a :class:`geopandas.GeoDataFrame` (loaded
by :func:`valleespyr.watershed.load_bassins`); without it ``area_km2`` is
``None``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    import geopandas as gpd

    from valleespyr.hydro.rivers import River, RiverNetwork

logger = logging.getLogger(__name__)

# Pfafstetter codes the four largest tributaries of a stem (odd digits 1..9 from
# the mouth up) and the four interbasins between them (even digits). Deeper than
# this and the codes get unwieldy for a browser; the tree itself carries the
# rest of the structure.
_PFAF_MAX_DEPTH = 6


# --------------------------------------------------------------------------- public


# Map centrelines: each river's own tronçon line is simplified until it has at
# most this many vertices (Douglas-Peucker in degrees, ~1e-4° ≈ 10 m), then
# stored as rounded [lon, lat] pairs. Enough to read the valley's shape in a
# ~600px mini-map without bloating the JSON.
_GEO_MAX_VERTICES = 40
_GEO_DECIMALS = 5

# A river's own catchment boundary (for masking the map to "just this valley")
# is only worth shipping for a river that reads as one valley on a map: big
# enough that the shape means something, small enough that the mask isn't the
# whole page. Outside this area range the map falls back to the full-catchment
# context view with no mask.
_VALLEY_AREA_MIN_KM2 = 5.0
_VALLEY_AREA_MAX_KM2 = 150.0
_CATCHMENT_MAX_VERTICES = 60


def _never_gets_catchment(
    rn: RiverNetwork,
    river_id: str,
    *,
    bassins: gpd.GeoDataFrame | None,
    dem_catchments: dict[str, Any] | None,
) -> bool:
    """True if ``river_id`` is a leaf confirmed to never get its own catchment.

    A leaf (no rivers upstream of it) whose ``dem_catchments`` entry is
    ``"discarded"`` (measured, outside the "reads as one valley" area range)
    or ``"undetermined"`` (DEM pour-point search never found a confident
    channel — almost always a torrent too short/steep for COP30 to resolve)
    is noise in the browsable tree: a leaf row a viewer can click into but
    that shows no bounded valley, ever. Folded into its parent instead, same
    as an unnamed/nameless reach already folds in :mod:`valleespyr.hydro.rivers`.

    A river the batch never attempted (no entry at all — added to the network
    after the last precompute run, or out of its scope) is left alone: its
    fate is unknown, not confirmed absent. Same for anything with rivers
    upstream of it — folding those away would hide real valleys further up.
    """
    if dem_catchments is None or rn.upstream_rivers(river_id):
        return False
    if bassins is not None:
        area, _ = _area_for_river(rn, river_id, bassins)
        if area is not None:
            return False
    entry = dem_catchments.get(river_id)
    return entry is not None and entry.get("source") in ("discarded", "undetermined")


def build_catalog(
    rn: RiverNetwork,
    root_query: str,
    *,
    bassins: gpd.GeoDataFrame | None = None,
    max_depth: int | None = None,
    geo: bool = False,
    dem_catchments: dict[str, Any] | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the nested-dict catalog rooted at ``root_query``.

    ``root_query`` is a river name (case-insensitive substring) or a
    ``COURDEAU…`` id, resolved the same way the CLI does. ``max_depth`` limits
    how many **confluences** deep the tree is walked (``None`` = no limit); a
    branch cut short keeps ``mainline_truncated`` / ``tributaries_truncated``
    counts so the viewer can say "… 12 more".

    Each node carries a ``mainline`` child (the same-valley continuation
    upstream, or ``None`` at a headwater) and a ``tributaries`` list (branches
    that merge in at this confluence, biggest sub-catchment first). See the
    module docstring for the git analogy.

    With ``bassins`` given, each node whose catchment is covered by the
    ``bassin_versant_topographique`` dump gets ``area_km2`` and
    ``n_sub_basins``; otherwise those are ``None`` / ``0``.

    With ``geo=True`` a ``geo`` block is added: ``bbox`` (``[w, s, e, n]`` over
    the whole catchment) and ``rivers`` (``id -> {"line": [[[lon, lat], …], …],
    "outlet": [lon, lat]}``), each ``line`` the river's own tronçon centreline
    as one or more simplified sub-lines (split only across a lake / missing
    reach). The HTML renderer draws the whole catchment faint and picks the
    selected river plus its upstream network out on top.

    ``dem_catchments`` (optional) is the already-loaded ``"rivers"`` sub-dict of
    a ``valleespyr valley catchments precompute`` output — a DEM-derived
    fallback for a valley-sized river the ``bassins`` WFS dump doesn't cover.
    See :func:`_build_geo` for the precedence. ``build_catalog`` stays IO-free:
    the caller loads the JSON, same as it already loads ``bassins``.

    ``overrides`` (optional) is the already-loaded ``rivers`` mapping of a
    ``config/valley_overrides.yaml`` file (see
    :func:`valleespyr.config.load_valley_overrides`) — hand-curated,
    per-river ``cours_d_eau_id`` keyed data: ``blacklist: true`` drops that
    river (and everything upstream of it) from the tree entirely, folded into
    its parent's ``n_folded`` same as a DEM-undetermined leaf; ``camera``
    overrides the 3D view's auto-computed initial shot (see ``catalog_3d.js``
    ``shotFor``) for rivers where the computed one looks wrong.

    Returns a dict with ``root`` (the tree), ``meta`` (counts, the root id)
    and — with ``geo=True`` — ``geo``.
    """
    from valleespyr.valley import resolve_river

    root = resolve_river(rn, root_query)

    pfaf = _pfafstetter_codes(rn, root.id, max_depth=_PFAF_MAX_DEPTH)

    n_nodes = 0

    def visit(river: River, depth: int) -> dict[str, Any]:
        nonlocal n_nodes
        n_nodes += 1

        mainline_id, trib_ids = _partition_children(rn, river.id)

        def keep(cid: str) -> bool:
            if overrides is not None and overrides.get(cid, {}).get("blacklist"):
                return False
            return not _never_gets_catchment(
                rn, cid, bassins=bassins, dem_catchments=dem_catchments
            )

        n_folded = 0
        if mainline_id is not None and not keep(mainline_id):
            n_folded += 1
            mainline_id = None
        kept_tribs = []
        for t in trib_ids:
            if keep(t):
                kept_tribs.append(t)
            else:
                n_folded += 1
        trib_ids = kept_tribs

        node: dict[str, Any] = {
            "id": river.id,
            "name": river.name,
            "length_km": round(river.length_m / 1000, 2),
            "strahler": river.max_order,
            "n_upstream": len(rn.upstream_rivers(river.id)),
            "n_tributaries": len(trib_ids),
            "n_folded": n_folded,
            "depth": depth,
            "pfafstetter": pfaf.get(river.id),
            "is_headwater": mainline_id is None and not trib_ids,
            "also_flows_into": _also_flows_into(rn, river.id),
            "area_km2": None,
            "n_sub_basins": 0,
            "mainline": None,
            "tributaries": [],
        }

        if bassins is not None:
            area, n_sub = _area_for_river(rn, river.id, bassins)
            node["area_km2"] = area
            node["n_sub_basins"] = n_sub

        # A confluence (>=1 tributary) is one step of depth; following the
        # mainline through a river with no tributary is free (still the same
        # branch, just the next reach up).
        at_limit = max_depth is not None and depth >= max_depth and bool(trib_ids)
        if at_limit:
            if mainline_id is not None:
                node["mainline_truncated"] = True
            if trib_ids:
                node["tributaries_truncated"] = len(trib_ids)
            return node

        child_depth = depth + 1 if trib_ids else depth
        if mainline_id is not None:
            node["mainline"] = visit(rn.rivers[mainline_id], child_depth)
        node["tributaries"] = [
            visit(rn.rivers[t], child_depth) for t in trib_ids
        ]
        return node

    tree = visit(root, 0)
    out: dict[str, Any] = {
        "root": tree,
        "meta": {
            "root_id": root.id,
            "root_name": root.name,
            "n_nodes": n_nodes,
            "has_areas": bassins is not None,
            "has_geo": geo,
            "model": "git",
        },
    }
    if geo:
        out["geo"] = _build_geo(
            rn, tree, bassins=bassins, dem_catchments=dem_catchments, overrides=overrides
        )
    return out


def build_forest_catalog(
    rn: RiverNetwork,
    roots: list[tuple[str, str]],
    *,
    forest_name: str = "study area",
    bassins: gpd.GeoDataFrame | None = None,
    max_depth: int | None = None,
    geo: bool = False,
    dem_catchments: dict[str, Any] | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one catalog spanning every root in ``roots``, as a single tree.

    Garonne and Adour are not two unrelated catalogs that happen to share a
    build recipe — they are both top-level branches of one implicit root (the
    sea / the study area), same as any confluence splits into tributaries.
    This wraps each ``(name, root_query)`` root's own :func:`build_catalog`
    tree as a "tributary" of one synthetic node, so the existing browsable
    tree (breadcrumb, go-back link, tributary rows) switches between valleys
    for free — no separate picker UI, no per-root HTML/JSON duplication.

    The synthetic root has id ``"_forest"`` and carries no river facts of its
    own (no length/strahler/area — it is not a real river); its
    ``tributaries`` are the given roots' trees, ordered as given. ``geo``
    blocks (river ids are globally unique ``COURDEAU…`` ids, so no key
    collision) and ``meta.n_nodes`` are merged across all roots.

    Returns the same ``{"root", "meta", ...}`` shape as :func:`build_catalog`.
    """
    trees = []
    n_nodes = 0
    has_areas = False
    geo_rivers: dict[str, dict[str, Any]] = {}
    bbox: list[float] | None = None

    for name, root_query in roots:
        cat = build_catalog(
            rn,
            root_query,
            bassins=bassins,
            max_depth=max_depth,
            geo=geo,
            dem_catchments=dem_catchments,
            overrides=overrides,
        )
        trees.append(cat["root"])
        n_nodes += cat["meta"]["n_nodes"]
        has_areas = has_areas or cat["meta"]["has_areas"]
        if geo:
            g = cat["geo"]
            geo_rivers.update(g["rivers"])
            if g["bbox"] is not None:
                w, s, e, n = g["bbox"]
                bbox = [w, s, e, n] if bbox is None else [
                    min(bbox[0], w), min(bbox[1], s), max(bbox[2], e), max(bbox[3], n),
                ]

    forest_root: dict[str, Any] = {
        "id": "_forest",
        "name": forest_name,
        "length_km": None,
        "strahler": None,
        "n_upstream": n_nodes,
        "n_tributaries": len(trees),
        "n_folded": 0,
        "depth": 0,
        "pfafstetter": None,
        "is_headwater": False,
        "also_flows_into": [],
        "area_km2": None,
        "n_sub_basins": 0,
        "mainline": None,
        "tributaries": trees,
    }

    out: dict[str, Any] = {
        "root": forest_root,
        "meta": {
            "root_id": "_forest",
            "root_name": forest_name,
            "n_nodes": n_nodes + 1,
            "has_areas": has_areas,
            "has_geo": geo,
            "model": "git",
        },
    }
    if geo:
        out["geo"] = {"bbox": bbox, "rivers": geo_rivers}
    return out


# ------------------------------------------------------------------- map geometry


def _build_geo(
    rn: RiverNetwork,
    tree: dict[str, Any],
    *,
    bassins: gpd.GeoDataFrame | None = None,
    dem_catchments: dict[str, Any] | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``{"bbox": [w, s, e, n], "rivers": {id: {"line", "outlet", "catchment"}}}``.

    One entry per river node in ``tree``. ``line`` is the river's own tronçon
    centreline as a list of simplified sub-lines (``[[[lon, lat], …], …]``) —
    one per chain ``shapely.linemerge`` stitches from the river's tronçons; a
    braided reach or a node where three same-river reaches meet yields several,
    which drawn together still read as one river. ``outlet`` is the river's
    mouth coordinate. ``bbox`` spans every sub-line and every outlet.

    With ``bassins`` given, a river whose drainage area falls in the "reads as
    one valley" range (see ``_VALLEY_AREA_MIN_KM2`` / ``_MAX_KM2``) also gets
    ``catchment``: its own catchment boundary as one or more simplified
    ``[lon, lat]`` rings, for the HTML renderer to mask the map down to just
    that valley. Rivers outside the range get ``catchment: None`` — too big
    (the mask would cover the whole map) or too small (not worth a polygon).

    Resolution order per river: the WFS-derived polygon (:func:`_valley_catchment`)
    wins when present (authoritative, already simplified); when it is ``None``
    (no WFS coverage) and ``dem_catchments`` has an entry for this river id, that
    DEM-derived polygon fills the gap; otherwise ``catchment`` stays ``None``,
    same as today.
    """
    from valleespyr.valley import outlet_point

    rivers: dict[str, dict[str, Any]] = {}
    w = s = e = n = None

    def note(lon: float, lat: float) -> None:
        nonlocal w, s, e, n
        w = lon if w is None else min(w, lon)
        e = lon if e is None else max(e, lon)
        s = lat if s is None else min(s, lat)
        n = lat if n is None else max(n, lat)

    for node in _walk_tree(tree):
        rid = node["id"]
        if rid in rivers:
            continue
        parts = _river_lines(rn, rid)
        if not parts:
            continue
        try:
            olon, olat = outlet_point(rn, rid)
            outlet = [round(olon, _GEO_DECIMALS), round(olat, _GEO_DECIMALS)]
        except Exception:  # pragma: no cover - outlet geometry hiccup
            logger.debug("geo outlet failed for %s", rid, exc_info=True)
            outlet = parts[-1][-1]
        catchment = None
        area = node.get("area_km2")
        if bassins is not None and area is not None and (
            _VALLEY_AREA_MIN_KM2 <= area <= _VALLEY_AREA_MAX_KM2
        ):
            catchment = _valley_catchment(rn, rid, bassins)
        if catchment is None and dem_catchments is not None:
            entry = dem_catchments.get(rid)
            if entry is not None:
                catchment = entry.get("catchment")
        rivers[rid] = {"line": parts, "outlet": outlet, "catchment": catchment}
        if overrides is not None:
            camera = overrides.get(rid, {}).get("camera")
            if camera:
                rivers[rid]["camera"] = camera
        for part in parts:
            for lon, lat in part:
                note(lon, lat)
        note(*outlet)

    bbox = [w, s, e, n] if w is not None else None
    return {"bbox": bbox, "rivers": rivers}


def polygon_to_rings(
    poly, *, max_vertices: int = _CATCHMENT_MAX_VERTICES, decimals: int = _GEO_DECIMALS
) -> list[list[list[float]]] | None:
    """A shapely polygon's exterior ring(s) as simplified ``[lon, lat]`` rings.

    One ring per polygon part (usually one piece, but ``unary_union`` /
    DEM vectorisation can yield a ``MultiPolygon``). Each ring is
    Douglas-Peucker-simplified toward ``max_vertices``, the same recipe
    :func:`_river_lines` uses for centrelines. Shared by :func:`_valley_catchment`
    (WFS-derived polygons) and the ``valley catchments precompute`` CLI command
    (DEM-derived polygons), so both produce the same ``list[list[list[float]]]``
    ring shape for the catalog's ``geo.rivers[id].catchment``.
    """
    if poly is None:
        return None
    parts = poly.geoms if poly.geom_type == "MultiPolygon" else [poly]
    out: list[list[list[float]]] = []
    for part in parts:
        ring = part.exterior
        if ring is None or ring.is_empty:
            continue
        simple = ring
        tol = 1e-4  # ~10 m, same starting tolerance as _river_lines
        for _ in range(12):
            if len(simple.coords) <= max_vertices:
                break
            simple = ring.simplify(tol, preserve_topology=True)
            tol *= 1.8
            if simple.is_empty or len(simple.coords) < 4:
                simple = ring
                break
        out.append([[round(x, decimals), round(y, decimals)] for x, y in simple.coords])
    return out or None


def _valley_catchment(
    rn: RiverNetwork, river_id: str, bassins: gpd.GeoDataFrame
) -> list[list[list[float]]] | None:
    """A river's own catchment boundary in lon/lat, simplified for the map mask.

    See :func:`polygon_to_rings` for the simplification recipe.
    """
    from valleespyr.watershed import catchment_polygon

    river = rn.get(river_id)
    if river is None:
        return None
    ids = {river.id} | {r.id for r in rn.upstream_rivers(river.id)}
    poly = catchment_polygon(bassins, ids)
    return polygon_to_rings(poly)


def _walk_tree(node: dict[str, Any]):
    """Every river node in a git-model catalog tree (node, mainline, tributaries)."""
    yield node
    if node.get("mainline") is not None:
        yield from _walk_tree(node["mainline"])
    for t in node.get("tributaries") or []:
        yield from _walk_tree(t)


def _river_lines(rn: RiverNetwork, river_id: str) -> list[list[list[float]]]:
    """The river's own centreline as a list of simplified sub-lines (may be empty).

    ``shapely.linemerge`` stitches the river's tronçons into as few chains as it
    can — one where the reaches form a simple path, several where they braid or
    a node joins three reaches of the same river. Every chain is kept (they
    share endpoints, so drawn together they read as one river) and each is
    Douglas-Peucker-simplified toward ``_GEO_MAX_VERTICES``.
    """
    from shapely.geometry import LineString, MultiLineString, shape
    from shapely.ops import linemerge

    fc = rn.river_path_geojson(river_id)
    lines = [
        shape(f["geometry"])
        for f in fc.get("features", [])
        if f.get("geometry", {}).get("type") in ("LineString", "MultiLineString")
    ]
    if not lines:
        return []

    merged = linemerge(lines) if len(lines) > 1 else lines[0]
    if isinstance(merged, LineString):
        chains = [merged]
    elif isinstance(merged, MultiLineString):
        chains = [g for g in merged.geoms if not g.is_empty]
    else:  # pragma: no cover - defensive
        return []

    out: list[list[list[float]]] = []
    for line in chains:
        if len(line.coords) < 2:
            continue
        simple = line
        tol = 1e-4  # ~10 m
        for _ in range(12):
            if len(simple.coords) <= _GEO_MAX_VERTICES:
                break
            simple = line.simplify(tol, preserve_topology=False)
            tol *= 1.8
            if simple.is_empty or len(simple.coords) < 2:
                simple = line
                break
        out.append(
            [[round(x, _GEO_DECIMALS), round(y, _GEO_DECIMALS)] for x, y in simple.coords]
        )
    return out


# ---------------------------------------------------------------- git structure


def _primary_children(rn: RiverNetwork, river_id: str) -> list[River]:
    """Children for which ``river_id`` is the first downstream (git's first parent).

    A bifurcating river has several downstreams; it appears in the tree only
    under the lowest-id one, and its alternates surface via
    :func:`_also_flows_into` on the node.
    """
    out = []
    for c in rn.children(river_id):
        succs = sorted(rn.graph.successors(c.id))
        if succs and succs[0] == river_id:
            out.append(c)
    return out


def _join_order(rn: RiverNetwork, river_id: str, child_ids: set[str]) -> dict[str, int]:
    """Rank of each id in ``child_ids`` by where it joins ``river_id``, mouth first.

    Walks ``river_id``'s own tronçon chain upstream from its outlet, following
    only edges that resolve back to ``river_id``; each step, checks whether the
    node just left behind is the join point of one of ``child_ids`` (the
    tronçon-graph successor of that child's own outlet). The order those join
    points are crossed *is* the order you would meet the tributaries walking
    upstream on the map — rank 0 is the one nearest the mouth.

    Ids that never resolve to a join point on this walk (a stray topology edge
    case) are simply absent from the returned dict; the caller falls back to
    another sort key for those.
    """
    river = rn.get(river_id)
    if river is None or river.outlet is None or not child_ids:
        return {}
    troncons = rn._troncons
    segment_to_river = rn.segment_to_river

    def edge_river(u: str, v: str) -> str | None:
        data = troncons.get_edge_data(u, v) or {}
        cleabs = data.get("cleabs")
        return segment_to_river.get(cleabs) if cleabs is not None else None

    # join node (on river_id's own chain) -> child id, for every requested child
    join_node_of: dict[str, str] = {}
    for cid in child_ids:
        child = rn.get(cid)
        if child is None or child.outlet is None:
            continue
        for w in troncons.successors(child.outlet):
            if edge_river(child.outlet, w) == river_id:
                join_node_of[w] = cid
                break

    order: dict[str, int] = {}
    seen: set[str] = set()
    node = river.outlet
    while node is not None and node not in seen:
        seen.add(node)
        if node in join_node_of:
            order.setdefault(join_node_of[node], len(order))
        nxt = None
        for u in troncons.predecessors(node):
            if edge_river(u, node) == river_id:
                nxt = u
                break
        node = nxt
    return order


def _partition_children(
    rn: RiverNetwork, river_id: str
) -> tuple[str | None, list[str]]:
    """``(mainline_id | None, [tributary_id, …])`` for a river.

    **Mainline** = the child that continues the same valley upstream. Normally
    the child with the largest sub-catchment (ties: length, then id) — the
    dominant water path, git's ``main``. As a guard for BD TOPO's rare
    same-toponyme-different-``cours_d_eau`` split, a child that carries this
    river's exact toponyme wins even if another child drains more; the
    ``cours_d_eau`` roll-up means this almost never fires.

    **Tributaries** = every other primary child, ordered by where it joins the
    river — mouth to source, the order you would meet them walking upstream on
    the map — falling back to biggest sub-catchment first (then length, then
    id) for any child :func:`_join_order` couldn't place.
    """
    kids = _primary_children(rn, river_id)
    if not kids:
        return None, []

    river = rn.get(river_id)
    size = {c.id: len(rn.upstream_rivers(c.id)) for c in kids}

    def size_rank(c: River) -> tuple:
        return (-size[c.id], -c.length_m, c.name or c.id)

    mainline: River | None = None
    if river is not None and river.name:
        named_same = [c for c in kids if c.name == river.name]
        if named_same:
            mainline = min(named_same, key=size_rank)
    if mainline is None:
        mainline = min(kids, key=size_rank)

    trib_kids = [c for c in kids if c.id != mainline.id]
    join_order = _join_order(rn, river_id, {c.id for c in trib_kids})

    def trib_rank(c: River) -> tuple:
        # placed tributaries sort mouth-first by their join rank; anything the
        # walk couldn't place falls in after them, by the old size-based rank.
        return (0, join_order[c.id]) if c.id in join_order else (1, size_rank(c))

    tribs = sorted(trib_kids, key=trib_rank)
    return mainline.id, [c.id for c in tribs]


def _also_flows_into(rn: RiverNetwork, river_id: str) -> list[dict[str, str | None]]:
    """Downstream rivers of ``river_id`` other than its primary parent.

    Non-empty only for a genuine bifurcation. Each entry is ``{"id", "name"}``.
    """
    succs = sorted(rn.graph.successors(river_id))
    if len(succs) <= 1:
        return []
    out = []
    for sid in succs[1:]:
        r = rn.rivers.get(sid)
        out.append({"id": sid, "name": r.name if r else None})
    return out


# ------------------------------------------------------------------- pfafstetter


def _pfafstetter_codes(
    rn: RiverNetwork, root_id: str, *, max_depth: int
) -> dict[str, str]:
    """Assign a Pfafstetter-style code to every river in ``root_id``'s catchment.

    Pfafstetter on the tree: the **main stem** is the chain from ``root_id``
    upward that follows the largest-catchment child at every confluence. The
    four biggest tributaries joining that stem get odd digits ``3,5,7,9`` from
    the mouth upward; the stem reaches between and around them get even digits
    ``2,4,6,8`` and the headwater reach gets ``1``. Every coded tributary
    recurses with its digit appended, down to ``max_depth`` levels; below that
    (and for tributaries past the fourth, which are folded into the nearest even
    reach) the whole sub-catchment inherits one code.

    By construction every river ends up coded: it is on some stem, or it is a
    coded tributary, or it is upstream of one (recursion / inherit). ``root_id``
    is deliberately left out — the viewer shows it as the tree root.
    """
    codes: dict[str, str] = {}

    def subtree_size(rid: str) -> int:
        return len(rn.upstream_rivers(rid))

    def main_stem(start_id: str) -> list[str]:
        """Stem ids from ``start_id`` upward, largest-catchment child each step."""
        path = [start_id]
        cur = start_id
        seen = {start_id}
        while True:
            kids = [c for c in rn.children(cur) if c.id not in seen]
            if not kids:
                break
            nxt = max(kids, key=lambda r: (subtree_size(r.id), r.length_m, r.name or r.id))
            path.append(nxt.id)
            seen.add(nxt.id)
            cur = nxt.id
        return path

    def code_area(area_root: str, prefix: str, depth: int) -> None:
        stem = main_stem(area_root)
        stem_set = set(stem)

        # off-stem tributaries with their join index along the stem
        tribs: list[tuple[int, Any]] = []
        for i, sid in enumerate(stem):
            for c in rn.children(sid):
                if c.id not in stem_set:
                    tribs.append((i, c))

        # the four biggest get odd digits; order them mouth->source (join index asc)
        tribs.sort(key=lambda t: (-subtree_size(t[1].id), -t[1].length_m, t[1].name or t[1].id))
        top = sorted(tribs[:4], key=lambda t: t[0])
        rest = tribs[4:]

        odd = [3, 5, 7, 9]
        cut_at: dict[int, str] = {}  # stem index -> digit assigned to the trib joining there
        for k, (join_i, trib) in enumerate(top):
            digit = odd[k]
            tcode = f"{prefix}{digit}"
            codes[trib.id] = tcode
            cut_at[join_i] = digit
            if depth + 1 < max_depth:
                code_area(trib.id, tcode, depth + 1)
            else:
                _inherit(rn, trib.id, tcode, codes)

        # stem reaches: even digits between the coded joins, 1 at the head.
        # reach k covers stem nodes (cut_k, cut_{k+1}]; reach 0 is below the
        # first cut, the last reach is the headwater (digit 1).
        cuts = sorted(cut_at)
        even = [2, 4, 6, 8]
        bounds = [0, *[c + 1 for c in cuts], len(stem)]
        seen_b: set[int] = set()
        bounds = [b for b in bounds if not (b in seen_b or seen_b.add(b))]
        for seg_k in range(len(bounds) - 1):
            lo, hi = bounds[seg_k], bounds[seg_k + 1]
            is_head = seg_k == len(bounds) - 2
            digit = 1 if is_head else (even[seg_k] if seg_k < len(even) else 2)
            rcode = f"{prefix}{digit}"
            for sidx in range(lo, hi):
                sid = stem[sidx]
                if sid != area_root:
                    codes.setdefault(sid, rcode)
                # uncoded tributaries hanging off this reach inherit its code
                for c in rn.children(sid):
                    if c.id not in stem_set and c.id not in codes:
                        _inherit(rn, c.id, rcode, codes)

        # any straggler tributary past the fourth: fold into its join reach's code
        for join_i, trib in rest:
            if trib.id in codes:
                continue
            # find the reach code covering this join index
            rcode = codes.get(stem[join_i], f"{prefix}2")
            _inherit(rn, trib.id, rcode, codes)

    code_area(root_id, "", 0)
    codes.pop(root_id, None)
    return codes


def _inherit(rn: RiverNetwork, river_id: str, code: str, codes: dict[str, str]) -> None:
    """Give ``river_id`` and everything upstream of it ``code`` (unless already coded)."""
    if river_id not in codes:
        codes[river_id] = code
    for r in rn.upstream_rivers(river_id):
        codes.setdefault(r.id, code)


# -------------------------------------------------------------------- drainage area


def _area_for_river(
    rn: RiverNetwork,
    river_id: str,
    bassins: gpd.GeoDataFrame,
) -> tuple[float | None, int]:
    """(area_km2, n_sub_basins) for a river, or (None, 0) with no WFS coverage.

    Dissolves the ``bassin_versant_topographique`` sub-basins keyed to this
    river or any river upstream of it, and measures the union in an
    equal-area-ish metric CRS.
    """
    import geopandas as gpd

    from valleespyr.watershed import BASSIN_COURS_D_EAU, catchment_polygon

    river = rn.get(river_id)
    if river is None:
        return None, 0
    ids = {river.id} | {r.id for r in rn.upstream_rivers(river.id)}
    poly = catchment_polygon(bassins, ids)
    if poly is None:
        return None, 0

    gs = gpd.GeoSeries([poly], crs="EPSG:4326").to_crs("EPSG:2154")
    area_km2 = round(gs.iloc[0].area / 1e6, 1)
    n_sub = int(bassins[BASSIN_COURS_D_EAU].isin(ids).sum())
    return area_km2, n_sub


__all__ = ["build_catalog", "build_forest_catalog", "polygon_to_rings"]
