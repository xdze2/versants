"""Trace the stream network upstream from a pour point and shape it as a tree.

Given the downstream-pointing :class:`~networkx.DiGraph` from
:func:`valleespyr.hydro.network.build_graph`:

1. :func:`snap_pour_point` finds the edge nearest a lon/lat point and returns its
   upstream node as the trace root.
2. :func:`trace_upstream` walks the graph against the arrows from that root,
   collecting every edge that drains to it (a cycle guard handles braided
   sections).
3. :func:`to_tree` turns the traced sub-network into a nested ``dict`` for a
   text or map tree view, with unbranched chains optionally collapsed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    import geopandas as gpd
    import networkx as nx

from .network import CRS_METRIC

# --------------------------------------------------------------------------- snap


def snap_pour_point(
    gdf: gpd.GeoDataFrame,
    lon: float,
    lat: float,
    *,
    from_node_col: str = "lien_vers_noeud_hydrographique_ini",
    to_node_col: str = "lien_vers_noeud_hydrographique_fin",
    flow_col: str = "sens_de_l_ecoulement",
) -> dict[str, Any]:
    """Snap a lon/lat point to the nearest tronçon.

    Returns ``{"cleabs", "root_node", "distance_m", "toponyme"}`` where
    ``root_node`` is the **upstream** node of the matched edge (its ``ini``,
    swapped for ``Sens inverse``) — the node to trace upstream from.
    """
    import geopandas as gpd_
    from shapely.geometry import Point

    metric = gdf.geometry.to_crs(CRS_METRIC)
    pt = gpd_.GeoSeries([Point(lon, lat)], crs="EPSG:4326").to_crs(CRS_METRIC).iloc[0]
    dists = metric.distance(pt)
    i = dists.idxmin()
    row = gdf.loc[i]

    ini = str(row[from_node_col]).strip()
    fin = str(row[to_node_col]).strip()
    root = fin if row.get(flow_col) == "Sens inverse" else ini
    return {
        "cleabs": row.get("cleabs"),
        "root_node": root,
        "distance_m": float(dists.loc[i]),
        "toponyme": row.get("cpx_toponyme_de_cours_d_eau"),
    }


# -------------------------------------------------------------------------- trace


def trace_upstream(
    graph: nx.DiGraph, root_node: str, *, include_ambiguous: bool = False
) -> tuple[set[str], nx.DiGraph]:
    """Everything that drains to ``root_node``.

    Walks predecessors (upstream) breadth-first from ``root_node``. Returns
    ``(edge_cleabs, subgraph)`` — the set of ``cleabs`` of every upstream edge and
    the induced sub-:class:`~networkx.DiGraph` (a view; copy it if you mutate).
    ``Double sens`` / ``Indéterminé`` edges are skipped unless
    ``include_ambiguous`` is set. A visited-node set makes braided loops safe.
    """
    if root_node not in graph:
        return set(), graph.subgraph([]).copy()

    visited: set[str] = {root_node}
    stack = [root_node]
    edge_ids: set[str] = set()
    while stack:
        node = stack.pop()
        for pred in graph.predecessors(node):
            data = graph.edges[pred, node]
            if data.get("ambiguous") and not include_ambiguous:
                continue
            cid = data.get("cleabs")
            if cid is not None:
                edge_ids.add(cid)
            if pred not in visited:
                visited.add(pred)
                stack.append(pred)

    return edge_ids, graph.subgraph(visited)


# --------------------------------------------------------------------------- tree


def to_tree(
    subgraph: nx.DiGraph,
    root_node: str,
    *,
    collapse_chains: bool = True,
    min_order: int | None = None,
) -> dict[str, Any]:
    """Nest the traced sub-network into ``{node, edge, children, ...}`` from the root up.

    Each node dict has: ``node`` (id), ``edge`` (the downstream edge's attrs, or
    ``None`` at the root), ``upstream_length_m`` (this node + everything above it),
    ``upstream_segments`` (edge count above, inclusive), and ``children`` (ordered
    by descending ``upstream_length_m``).

    ``collapse_chains`` folds a run of single-child nodes on the same watercourse
    into the branch node, adding ``collapsed_segments`` / ``collapsed_length_m``.
    ``min_order`` prunes branches whose max Strahler order is below the threshold.
    """
    empty = {
        "node": root_node,
        "edge": None,
        "children": [],
        "upstream_segments": 0,
        "upstream_length_m": 0.0,
    }
    if root_node not in subgraph:
        return empty

    seen: set[str] = set()

    def build(node: str, in_edge: dict[str, Any] | None) -> dict[str, Any] | None:
        if node in seen:  # braided safety
            return None
        seen.add(node)

        kids: list[dict[str, Any]] = []
        for pred in subgraph.predecessors(node):
            e = subgraph.edges[pred, node]
            child = build(pred, e)
            if child is not None:
                kids.append(child)

        if min_order is not None and in_edge is not None:
            branch_max = _max_order(in_edge, kids)
            if branch_max is not None and branch_max < min_order:
                return None

        kids.sort(key=lambda c: c["upstream_length_m"], reverse=True)
        seg_len = float(in_edge["length_m"]) if in_edge else 0.0
        node_dict = {
            "node": node,
            "edge": _edge_summary(in_edge) if in_edge else None,
            "children": kids,
            "upstream_segments": (1 if in_edge else 0) + sum(c["upstream_segments"] for c in kids),
            "upstream_length_m": seg_len + sum(c["upstream_length_m"] for c in kids),
        }
        if collapse_chains:
            _collapse(node_dict)
        return node_dict

    root = build(root_node, None)
    return root if root is not None else empty


def _edge_summary(e: dict[str, Any]) -> dict[str, Any]:
    return {
        "cleabs": e.get("cleabs"),
        "toponyme": e.get("toponyme"),
        "order": e.get("order"),
        "nature": e.get("nature"),
        "fictif": e.get("fictif"),
        "length_m": float(e.get("length_m") or 0.0),
    }


def _max_order(edge: dict[str, Any], kids: list[dict[str, Any]]) -> int | None:
    vals = [edge.get("order")]
    for k in kids:
        e = k.get("edge") or {}
        vals.append(e.get("order"))
        vals.append(k.get("_branch_max_order"))
    nums = [v for v in vals if isinstance(v, int)]
    return max(nums) if nums else None


def drop_fictif(node: dict[str, Any]) -> None:
    """Splice fictitious edges out of a :func:`to_tree` dict, in place.

    A fictif edge (lake / void connector) is removed as a tree level: its children
    are promoted onto its parent and re-sorted by upstream length. The node's
    running totals are left as traced — the connectors are still counted, they
    just get no line in the rendered tree.
    """
    promoted: list[dict[str, Any]] = []
    for child in node["children"]:
        drop_fictif(child)
        if (child.get("edge") or {}).get("fictif"):
            promoted.extend(child["children"])
        else:
            promoted.append(child)
    promoted.sort(key=lambda c: c["upstream_length_m"], reverse=True)
    node["children"] = promoted


def _collapse(node_dict: dict[str, Any]) -> None:
    """Fold a unary chain on the same watercourse into ``node_dict`` in place."""
    collapsed = 0
    collapsed_len = 0.0
    while len(node_dict["children"]) == 1:
        (child,) = node_dict["children"]
        top = (node_dict["edge"] or {}).get("toponyme")
        cur = (child["edge"] or {}).get("toponyme")
        if child["edge"] is None or top != cur:
            break
        collapsed += 1
        collapsed_len += float(child["edge"]["length_m"] or 0.0)
        node_dict["children"] = child["children"]
        node_dict.setdefault("_chain_tip", child["node"])
        node_dict["_chain_tip"] = child["node"]
    if collapsed:
        node_dict["collapsed_segments"] = collapsed
        node_dict["collapsed_length_m"] = collapsed_len
