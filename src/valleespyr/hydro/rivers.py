"""Contract the tronçon graph into a **river graph**.

:func:`valleespyr.hydro.network.build_graph` gives a directed graph whose nodes
are hydrographic nodes and whose edges are individual stream segments
(*tronçons*), each pointing downstream. That is the right primitive for a precise
upstream trace, but it is too fine to *browse*: a single named river is hundreds
of edges.

This module rolls those segments up by watercourse. Each edge is assigned to a
**river** — keyed by ``liens_vers_cours_d_eau`` (BD TOPO's stable ``cours_d_eau``
id; the first id when several are ``/``-joined) when it has one, otherwise the
edge is an *unnamed reach* that gets merged downstream into the first named river
it reaches. The result is a small :class:`~networkx.DiGraph` of rivers with a
"flows into" edge between them, from which the browse-time facts fall out:

* a **list of rivers** (id, name, Strahler order, outlet node, total length);
* for each river, the **rivers in its catchment** (everything upstream in the
  river graph);
* its **parent** river (one step downstream) — ``None`` for a root;
* **root / leaf** flags (root: nothing downstream inside the loaded network;
  leaf: no upstream river);
* on demand, the **river path** and a coarse **catchment polygon** as GeoJSON
  (see :func:`river_path_geojson` / :func:`river_catchment_geojson`).

Everything here is pure graph work on an already-built
:class:`~networkx.DiGraph`; nothing hits the network.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    import networkx as nx


# Sentinel river id prefix for a maximal run of unnamed reaches that never
# reaches a named river before leaving the loaded network.
UNNAMED_PREFIX = "UNNAMED:"


@dataclass(eq=False)
class River:
    """One watercourse rolled up from its tronçons.

    ``id``          stable ``cours_d_eau`` id, or ``UNNAMED:<node>`` for an
                    orphan run of unnamed reaches.
    ``name``        display toponyme (most common one on the course), or ``None``.
    ``segments``    ``cleabs`` of every tronçon assigned to this river.
    ``nodes``       hydrographic nodes touched by those segments.
    ``outlet``      the most-downstream node of the river inside the network.
    ``source_nodes`` the river's headwater nodes (no upstream segment *of this
                    river*).
    ``length_m``    summed segment length.
    ``max_order``   max Strahler order seen on the river's segments (``None`` if
                    the source has none).
    ``parent_id``   id of the river this one flows into, or ``None`` (root).
    ``child_ids``   ids of rivers that flow directly into this one.
    """

    id: str
    name: str | None
    segments: set[str] = field(default_factory=set)
    nodes: set[str] = field(default_factory=set)
    outlet: str | None = None
    source_nodes: set[str] = field(default_factory=set)
    length_m: float = 0.0
    max_order: int | None = None
    parent_id: str | None = None
    child_ids: list[str] = field(default_factory=list)

    @property
    def is_root(self) -> bool:
        return self.parent_id is None

    @property
    def is_leaf(self) -> bool:
        return not self.child_ids

    @property
    def named(self) -> bool:
        """Backed by a BD TOPO ``cours_d_eau`` id (not an ``UNNAMED:`` orphan run).

        Note this is *identity*, not a toponyme: a ``cours_d_eau`` can be nameless
        (``self.name is None``) and still be ``named`` here. Filter on
        ``self.name`` when you want only rivers a person would recognise.
        """
        return not self.id.startswith(UNNAMED_PREFIX)


@dataclass
class RiverNetwork:
    """The whole set of rivers plus the river-level "flows into" graph."""

    rivers: dict[str, River]
    graph: nx.DiGraph  # node = river id, edge u->v = "u flows into v"
    #: segment cleabs -> river id (every non-ambiguous segment edge)
    segment_to_river: dict[str, str]
    #: the underlying downstream-pointing tronçon graph (kept for geometry work)
    _troncons: nx.DiGraph

    # ------------------------------------------------------------------ lookups

    def __iter__(self):
        return iter(self.rivers.values())

    def __len__(self) -> int:
        return len(self.rivers)

    def get(self, river_id: str) -> River | None:
        return self.rivers.get(river_id)

    def by_name(self, name: str, *, exact: bool = False) -> list[River]:
        """Rivers whose name matches ``name`` (case-insensitive substring by default)."""
        needle = name.casefold()
        out = []
        for r in self.rivers.values():
            if r.name is None:
                continue
            hay = r.name.casefold()
            if (hay == needle) if exact else (needle in hay):
                out.append(r)
        return out

    def roots(self) -> list[River]:
        return [r for r in self.rivers.values() if r.is_root]

    def leaves(self) -> list[River]:
        return [r for r in self.rivers.values() if r.is_leaf]

    # ------------------------------------------------------------- catchment nav

    def parent(self, river_id: str) -> River | None:
        r = self.rivers.get(river_id)
        if r is None or r.parent_id is None:
            return None
        return self.rivers.get(r.parent_id)

    def children(self, river_id: str) -> list[River]:
        r = self.rivers.get(river_id)
        if r is None:
            return []
        return [self.rivers[c] for c in r.child_ids if c in self.rivers]

    def upstream_rivers(self, river_id: str, *, named_only: bool = False) -> list[River]:
        """Every river in ``river_id``'s catchment (all rivers upstream of it).

        Includes tributaries of tributaries, recursively; excludes ``river_id``
        itself. Ordered by descending total length.
        """
        if river_id not in self.graph:
            return []
        import networkx as nx

        ids = nx.ancestors(self.graph, river_id)
        out = [self.rivers[i] for i in ids if i in self.rivers]
        if named_only:
            out = [r for r in out if r.name is not None]
        out.sort(key=lambda r: r.length_m, reverse=True)
        return out

    def downstream_path(self, river_id: str) -> list[River]:
        """``river_id`` then each river downstream of it, in order, to the root."""
        out: list[River] = []
        seen: set[str] = set()
        cur: str | None = river_id
        while cur is not None and cur not in seen and cur in self.rivers:
            seen.add(cur)
            r = self.rivers[cur]
            out.append(r)
            cur = r.parent_id
        return out

    # -------------------------------------------------------------- geo exports

    def river_path_geojson(self, river_id: str) -> dict[str, Any]:
        """GeoJSON ``FeatureCollection`` of the river's tronçon lines."""
        return _segments_geojson(
            self._troncons, self._segment_edges(river_id), river=self.rivers.get(river_id)
        )

    def catchment_segments(self, river_id: str) -> set[str]:
        """``cleabs`` of every tronçon in ``river_id``'s catchment.

        That is the river's own segments plus those of every river upstream of it
        in the river graph — **not** an upstream trace from the outlet node,
        which would also pull in the parent river's other branches past the
        confluence.
        """
        r = self.rivers.get(river_id)
        if r is None:
            return set()
        out = set(r.segments)
        for up in self.upstream_rivers(river_id):
            out |= up.segments
        return out

    def river_catchment_geojson(self, river_id: str) -> dict[str, Any]:
        """The catchment's *stream network* as a line ``FeatureCollection``.

        Not a filled polygon — dissolve it against DEM- or
        ``bassin_versant_topographique``-derived polygons for an actual area.
        """
        r = self.rivers.get(river_id)
        if r is None:
            return {"type": "FeatureCollection", "features": []}
        return _segments_geojson(self._troncons, self.catchment_segments(river_id), river=r)

    # ----------------------------------------------------------------- internals

    def _segment_edges(self, river_id: str) -> set[str]:
        r = self.rivers.get(river_id)
        return set(r.segments) if r else set()

    def summary(self) -> list[dict[str, Any]]:
        """One row per river, for a table / CLI listing (longest first)."""
        rows = []
        for r in sorted(self.rivers.values(), key=lambda x: x.length_m, reverse=True):
            rows.append(
                {
                    "id": r.id,
                    "name": r.name,
                    "named": r.named,
                    "length_km": round(r.length_m / 1000, 2),
                    "max_order": r.max_order,
                    "n_segments": len(r.segments),
                    "parent": r.parent_id,
                    "n_children": len(r.child_ids),
                    "is_root": r.is_root,
                    "is_leaf": r.is_leaf,
                }
            )
        return rows


# --------------------------------------------------------------------------- build


def _river_key(raw: Any) -> str | None:
    """First ``cours_d_eau`` id from ``liens_vers_cours_d_eau`` (``/``-joined), or None."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() in ("nan", "none"):
        return None
    return s.split("/", 1)[0].strip() or None


def build_river_network(
    troncons: nx.DiGraph,
    *,
    include_ambiguous: bool = False,
    river_key_attr: str = "liens_vers_cours_d_eau",
) -> RiverNetwork:
    """Roll a tronçon :class:`~networkx.DiGraph` up into a :class:`RiverNetwork`.

    ``troncons`` must be the downstream-pointing graph from
    :func:`valleespyr.hydro.network.build_graph`. Its edges are expected to carry
    ``cleabs``, ``length_m``, ``order``, ``toponyme``, ``fictif``, ``ambiguous``
    and, for river identity, ``river_key_attr`` (``liens_vers_cours_d_eau``).

    Segments flagged ``ambiguous`` (``Double sens`` / ``Indéterminé``) are left
    out of the river graph unless ``include_ambiguous`` is set.

    Assignment: an edge with a ``cours_d_eau`` id joins that river. An edge with
    no id is an *unnamed reach*; it is merged into the first named river found by
    walking downstream. A run of unnamed reaches that leaves the network without
    meeting a named river becomes its own ``UNNAMED:<outlet-node>`` river.
    """
    import networkx as nx

    # ---- 1. per-edge river key (or None) -------------------------------------
    # Iterate edges in a stable order so every downstream tie-break below is
    # reproducible regardless of hash seed.
    edges_sorted = sorted(troncons.edges(data=True), key=lambda e: (e[0], e[1]))
    edge_key: dict[tuple[str, str], str | None] = {}
    for u, v, data in edges_sorted:
        if data.get("ambiguous") and not include_ambiguous:
            continue
        edge_key[(u, v)] = _river_key(data.get(river_key_attr))

    # ---- 2. propagate a river id to unnamed reaches (walk downstream) -------
    # For an unnamed edge, follow the unique downstream edge chain until we hit
    # an edge that has a key; adopt it. If we fall off the network first, mint an
    # UNNAMED id from the last node reached.
    resolved: dict[tuple[str, str], str] = {}

    def resolve(edge: tuple[str, str]) -> str:
        if edge in resolved:
            return resolved[edge]
        chain: list[tuple[str, str]] = []
        cur: tuple[str, str] | None = edge
        guard: set[tuple[str, str]] = set()
        assigned: str | None = None
        while cur is not None and cur not in guard:
            guard.add(cur)
            key = edge_key.get(cur)
            if key is not None:
                assigned = key
                break
            if cur in resolved:  # someone downstream already resolved
                assigned = resolved[cur]
                break
            chain.append(cur)
            _, tail = cur
            # Follow the downstream edge. A node usually has one; where a braided
            # section gives several, prefer the one already carrying a river key,
            # then sort for a stable pick.
            outs = sorted(
                ((tail, w) for w in troncons.successors(tail) if (tail, w) in edge_key),
                key=lambda e: (edge_key.get(e) is None, e[1]),
            )
            cur = outs[0] if outs else None
        if assigned is None:
            # never reached a named river; key off the last node of the chain
            last_node = chain[-1][1] if chain else edge[1]
            assigned = f"{UNNAMED_PREFIX}{last_node}"
        for e in chain:
            resolved[e] = assigned
        resolved[edge] = assigned
        return assigned

    for edge in sorted(edge_key):
        key = edge_key[edge]
        resolved[edge] = key if key is not None else resolve(edge)

    # ---- 3. accumulate rivers --------------------------------------------------
    rivers: dict[str, River] = {}
    names: dict[str, Counter] = {}
    segment_to_river: dict[str, str] = {}

    for (u, v), rid in resolved.items():
        data = troncons.edges[u, v]
        r = rivers.get(rid)
        if r is None:
            r = rivers[rid] = River(id=rid, name=None)
            names[rid] = Counter()
        cid = data.get("cleabs")
        if cid is not None:
            r.segments.add(cid)
            segment_to_river[cid] = rid
        r.nodes.update((u, v))
        r.length_m += float(data.get("length_m") or 0.0)
        order = data.get("order")
        if isinstance(order, int):
            r.max_order = order if r.max_order is None else max(r.max_order, order)
        top = data.get("toponyme")
        if top:
            # A handful of tronçons carry two "/"-joined toponymes; count the
            # first (BD TOPO lists the primary name first).
            names[rid][str(top).split("/", 1)[0].strip()] += 1

    for rid, r in rivers.items():
        if names[rid]:
            r.name = names[rid].most_common(1)[0][0]

    # ---- 4. outflow nodes / sources per river ------------------------------
    # A river's *outflow* nodes are the ``fin`` of one of its segments that is
    # not the ``ini`` of another (``tails - heads``) — every point where water
    # leaves the river. Its *source* nodes are the mirror image.
    river_edges: dict[str, list[tuple[str, str]]] = {rid: [] for rid in rivers}
    for (u, v), rid in resolved.items():
        river_edges[rid].append((u, v))

    river_outflows: dict[str, list[str]] = {}
    for rid, r in rivers.items():
        heads = {u for u, _ in river_edges[rid]}
        tails = {v for _, v in river_edges[rid]}
        r.source_nodes = heads - tails
        outflows = sorted(tails - heads) or sorted(tails)
        river_outflows[rid] = outflows

    # ---- 5. river graph: an edge for every "river -> river below it" link --
    # Look at *all* of a river's outflow nodes (a braided or BD-TOPO-split river
    # can leave the network in more than one place); connect it to whatever
    # river each outflow drains into. A river with no such link is a root.
    rg = nx.DiGraph()
    rg.add_nodes_from(sorted(rivers))
    for rid in sorted(rivers):
        downs: set[str] = set()
        for node in river_outflows[rid]:
            for w in troncons.successors(node):
                d = resolved.get((node, w))
                if d is not None and d != rid:
                    downs.add(d)
        for d in sorted(downs):
            rg.add_edge(rid, d)

    # Pick a display outlet: the outflow node that actually reaches another
    # river (furthest downstream), else the first with no successors, else any.
    for rid, r in rivers.items():
        r.outlet = _pick_outlet(troncons, river_outflows[rid], rid, resolved)

    # BD TOPO sometimes over-splits one watercourse into several ``cours_d_eau``
    # records that, at their shared confluence, end up pointing at each other —
    # a cycle in the river graph. Merge each such cycle back into one river so
    # the graph is a DAG (catchment / downstream-path walks depend on it).
    _merge_cycles(rivers, rg, segment_to_river, names)

    # A ``cours_d_eau`` record with no toponyme (BD TOPO gave it a stable id but
    # nobody named it) is almost always a short headwater reach; left as its own
    # river it is pure noise in the browsable tree — a blank-named leaf at every
    # confluence. Fold each into the river directly downstream of it, same as an
    # unnamed *reach* (no id at all) already gets folded in step 2 above.
    _merge_nameless(rivers, rg, segment_to_river)

    # Collapsing a chain through a nameless river can turn an indirect path
    # between two named rivers into a direct edge, exposing a 2-river cycle
    # that the first _merge_cycles pass (run before any nameless node was
    # collapsed) couldn't see yet. Sweep again now the graph has settled.
    _merge_cycles(rivers, rg, segment_to_river, names)

    for rid in rivers:
        preds = list(rg.predecessors(rid))
        succs = sorted(rg.successors(rid))
        rivers[rid].child_ids = sorted(
            preds, key=lambda c: (-rivers[c].length_m, c)
        )
        rivers[rid].parent_id = succs[0] if succs else None

    return RiverNetwork(
        rivers=rivers,
        graph=rg,
        segment_to_river=segment_to_river,
        _troncons=troncons,
    )


def _merge_cycles(
    rivers: dict[str, River],
    rg: nx.DiGraph,
    segment_to_river: dict[str, str],
    names: dict[str, Counter],
) -> None:
    """Collapse every strongly-connected component of >1 river into its longest member."""
    import networkx as nx

    comps = sorted(
        (c for c in nx.strongly_connected_components(rg) if len(c) >= 2),
        key=lambda c: min(c),
    )
    for comp in comps:
        keep = max(sorted(comp), key=lambda i: rivers[i].length_m)
        kept = rivers[keep]
        for rid in sorted(comp):
            if rid == keep:
                continue
            other = rivers.pop(rid)
            kept.segments |= other.segments
            kept.nodes |= other.nodes
            kept.length_m += other.length_m
            if other.max_order is not None:
                kept.max_order = (
                    other.max_order
                    if kept.max_order is None
                    else max(kept.max_order, other.max_order)
                )
            names[keep].update(names.get(rid, {}))
            for cid in other.segments:
                segment_to_river[cid] = keep
            # rewire river-graph edges from/to the dropped node onto ``keep``
            for pred in list(rg.predecessors(rid)):
                if pred not in comp:
                    rg.add_edge(pred, keep)
            for succ in list(rg.successors(rid)):
                if succ not in comp:
                    rg.add_edge(keep, succ)
            rg.remove_node(rid)
        rg.remove_edges_from([(keep, keep)])
        if names[keep]:
            kept.name = names[keep].most_common(1)[0][0]
        # ``kept.outlet`` is left as-is: still a valid downstream node of the
        # merged river, and good enough for the catchment trace.


def _merge_nameless(
    rivers: dict[str, River],
    rg: nx.DiGraph,
    segment_to_river: dict[str, str],
) -> None:
    """Fold every nameless river into the river directly downstream of it.

    Walks each nameless river down its single river-graph successor (a DAG at
    this point — cycles are already merged) until it reaches a named river or a
    root, absorbing segments/length/order along the way; a chain of several
    nameless rivers in a row collapses in one pass. A nameless *root* (no
    downstream at all inside the loaded network) is left alone — nothing to
    merge it into.
    """
    import networkx as nx

    # process leaves-up so a river's own downstream target is already resolved
    for rid in list(nx.topological_sort(rg)):
        r = rivers.get(rid)
        if r is None or r.name is not None:
            continue
        succs = sorted(rg.successors(rid))
        if not succs:
            continue  # nameless root: nothing downstream to merge into
        target_id = succs[0]
        target = rivers[target_id]

        target.segments |= r.segments
        target.nodes |= r.nodes
        target.length_m += r.length_m
        if r.max_order is not None:
            target.max_order = (
                r.max_order if target.max_order is None else max(target.max_order, r.max_order)
            )
        for cid in r.segments:
            segment_to_river[cid] = target_id

        # rewire: everything that flowed into rid now flows into target
        for pred in list(rg.predecessors(rid)):
            if pred != target_id:
                rg.add_edge(pred, target_id)
        for succ in succs[1:]:
            rg.add_edge(target_id, succ)
        rg.remove_node(rid)
        del rivers[rid]


def _pick_outlet(
    troncons: nx.DiGraph,
    outflows: list[str],
    rid: str,
    resolved: dict[tuple[str, str], str],
) -> str | None:
    """The outflow node that best represents where the river ends.

    Prefers a node that drains into a *different* river; failing that, one with
    no downstream edge at all (the network boundary); failing that, the first.
    """
    if not outflows:
        return None
    for node in sorted(outflows):
        for w in sorted(troncons.successors(node)):
            d = resolved.get((node, w))
            if d is not None and d != rid:
                return node
    for node in sorted(outflows):
        if troncons.out_degree(node) == 0:
            return node
    return min(outflows)


# ------------------------------------------------------------------- geo helpers


def _segments_geojson(
    troncons: nx.DiGraph,
    edge_ids: set[str],
    *,
    river: River | None = None,
) -> dict[str, Any]:
    """Line ``FeatureCollection`` for a set of segment ``cleabs``.

    Reads geometries straight off the tronçon graph edges (they are shapely
    objects put there by :func:`build_graph`); skips any without geometry.
    """
    from shapely.geometry import mapping

    wanted = set(edge_ids)
    features = []
    for _u, _v, data in troncons.edges(data=True):
        cid = data.get("cleabs")
        if cid not in wanted:
            continue
        geom = data.get("geometry")
        if geom is None:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": mapping(geom),
                "properties": {
                    "cleabs": cid,
                    "toponyme": data.get("toponyme"),
                    "order": data.get("order"),
                    "fictif": bool(data.get("fictif")),
                    "length_m": round(float(data.get("length_m") or 0.0), 1),
                },
            }
        )
    fc: dict[str, Any] = {"type": "FeatureCollection", "features": features}
    if river is not None:
        fc["properties"] = {"river_id": river.id, "river_name": river.name}
    return fc
