"""Bridge a river in the graph to a real catchment polygon and its DEM tile.

:mod:`valleespyr.hydro.rivers` gives us a *river* — a run of tronçons rolled up
by watercourse, with an outlet node and a fan of upstream stream lines — but no
drainage area: ``bassin_versant_topographique`` only covers a fraction of named
rivers (see ``todo.md``). :mod:`valleespyr.hydro.dem` can delineate a catchment
from terrain, but it speaks in bboxes and pour-point coordinates, not river ids.

This module is the glue. Given a :class:`~valleespyr.hydro.rivers.RiverNetwork`
and a river id (or a name), it:

* :func:`resolve_river` — turn a ``COURDEAU…`` id or a name substring into one
  :class:`~valleespyr.hydro.rivers.River` (the disambiguation the CLI already
  does inline, factored out so both callers agree);
* :func:`outlet_point` — derive the river's real mouth coordinate (lon, lat)
  from tronçon geometry, the way ``scratchpad/dem_lutour.py`` hand-typed it;
* :func:`catchment_bbox` — a padded WGS84 tile that comfortably contains the
  basin, grown extra on the upstream sides where the basin keeps widening past
  the mapped streams;
* :func:`delineate_river` — run the whole chain (fetch tile → snap outlet →
  delineate) and hand back the polygon plus a diagnostics dict that also says
  how much of the river's own course landed inside it;
* :func:`delineate_river_search` — the same, but for the confluence cases
  where a single outlet snap locks onto a neighbouring parent river's basin
  instead of the tributary's own: tries several :func:`upstream_offset_point`
  / accumulation-threshold combinations against one shared conditioned grid
  and keeps whichever best matches the river's own mapped course, or reports
  "undetermined" rather than caching a wrong polygon.

Only the ``valley render`` CLI path calls :func:`delineate_river` /
:func:`delineate_river_search`, and that path already requires the ``dem`` /
``render`` extras; :func:`resolve_river`, :func:`outlet_point`,
:func:`upstream_offset_point` and :func:`catchment_bbox` are pure graph +
shapely work. The heavy :mod:`valleespyr.hydro.dem` import (rasterio,
geopandas) is therefore done **inside** the delineation functions, so
importing this module stays cheap for callers that only need the graph
helpers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from shapely.geometry import LineString, MultiLineString, shape

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from shapely.geometry.base import BaseGeometry

    from valleespyr.hydro.rivers import River, RiverNetwork

logger = logging.getLogger(__name__)

# ``sens_de_l_ecoulement`` values whose drawn geometry points *upstream* — for
# these the downstream end of the line is ``coords[0]``, not ``coords[-1]``.
# ``build_graph`` swaps the *nodes* for "Sens inverse" but leaves the geometry
# untouched, so we undo that here when reading the mouth coordinate.
_FLOW_INVERSE = "Sens inverse"


# --------------------------------------------------------------------------- resolve


def resolve_river(rn: RiverNetwork, query: str) -> River:
    """Resolve ``query`` to a single :class:`~valleespyr.hydro.rivers.River`.

    ``query`` is either a stable ``COURDEAU…`` id (matched exactly first) or a
    case-insensitive name substring. Raises :class:`ValueError` when nothing
    matches, or when a name matches several rivers (the message lists them,
    longest first, so the caller can pass an id instead).

    This mirrors the disambiguation in ``cli.py``'s ``rivers show`` so the CLI
    can call it instead of repeating the logic.
    """
    river = rn.get(query)
    if river is not None:
        return river

    matches = rn.by_name(query)
    if not matches:
        raise ValueError(f"no river matching {query!r}")
    if len(matches) > 1:
        listing = "\n".join(
            f"  {m.length_m / 1000:6.1f} km  {m.name}  [{m.id}]"
            for m in sorted(matches, key=lambda r: r.length_m, reverse=True)
        )
        raise ValueError(
            f"{len(matches)} rivers match {query!r}; be more specific or pass the id:\n"
            f"{listing}"
        )
    return matches[0]


# ---------------------------------------------------------------------- outlet point


def _downstream_end(geom: BaseGeometry, flow: str | None) -> tuple[float, float]:
    """The (lon, lat) at the downstream end of one tronçon geometry.

    ``geom`` is a shapely ``LineString`` or ``MultiLineString`` as put on the
    graph edge by ``build_graph`` — drawn in flow order unless ``flow`` is
    ``"Sens inverse"``, in which case it is drawn upstream-first.
    """
    if isinstance(geom, MultiLineString):
        parts = list(geom.geoms)
        line = parts[0] if flow == _FLOW_INVERSE else parts[-1]
    elif isinstance(geom, LineString):
        line = geom
    else:  # pragma: no cover - defensive; edges only ever carry (Multi)LineString
        raise TypeError(f"unexpected outlet geometry {geom.geom_type!r}")
    coord = line.coords[0] if flow == _FLOW_INVERSE else line.coords[-1]
    return (float(coord[0]), float(coord[1]))


def outlet_point(rn: RiverNetwork, river_id: str) -> tuple[float, float]:
    """(lon, lat) of the river's outlet — the downstream tip of its main stem.

    We walk the river's own tronçon edges downstream (following only edges that
    belong to *this* river) from ``river.outlet``, because ``river.outlet`` can
    be left mid-course by the river-graph cycle merge in ``build_river_network``.
    The terminal node of that walk is the true mouth; we return the downstream
    coordinate of the last edge, honouring its ``sens_de_l_ecoulement`` and
    picking the right sub-line of a ``MultiLineString``.
    """
    river = rn.get(river_id)
    if river is None:
        raise ValueError(f"unknown river id {river_id!r}")

    troncons = rn._troncons
    segs = river.segments
    # This river's edges, and its outflow nodes (a segment tail that is no
    # segment's head) as the fallback set of candidate mouths. Read from the
    # precomputed per-river index rather than scanning every tronçon edge —
    # this runs once per river in a catalog build, so a full-graph scan here
    # made that O(rivers * total_edges).
    river_edges = rn._edges_by_river.get(river_id, [])
    if not river_edges:
        raise RuntimeError(f"river {river_id!r} has no tronçon geometry")
    heads = {u for u, _v, _d in river_edges}
    tails = {v for _u, v, _d in river_edges}
    outflows = tails - heads

    # Start at river.outlet if it is a node of this river, else at an outflow.
    node = river.outlet
    river_nodes = heads | tails
    if node not in river_nodes:
        node = min(outflows) if outflows else min(river_nodes)

    def _next_edge(n: str):
        """The downstream edge of this river leaving ``n`` (longest, then id)."""
        cands = [
            (u, v, d)
            for u, v, d in troncons.out_edges(n, data=True)
            if d.get("cleabs") in segs
        ]
        if not cands:
            return None
        return max(cands, key=lambda e: (e[2].get("length_m") or 0.0, e[2].get("cleabs") or ""))

    last: tuple[str, str, dict] | None = None
    seen: set[str] = set()
    while node is not None and node not in seen:
        seen.add(node)
        edge = _next_edge(node)
        if edge is None:
            break
        last = edge
        node = edge[1]

    if last is None:
        # river.outlet is already the mouth: take the segment ending there.
        ending = [e for e in river_edges if e[1] == node]
        if not ending:
            ending = [e for e in river_edges if e[1] in outflows] or river_edges
        last = max(
            ending,
            key=lambda e: (e[2].get("length_m") or 0.0, e[2].get("cleabs") or ""),
        )

    _u, _v, data = last
    geom = data.get("geometry")
    if geom is None:
        raise RuntimeError(f"outlet tronçon of {river_id!r} has no geometry")
    return _downstream_end(geom, data.get("flow"))


# --------------------------------------------------------------- upstream offset point


def _ordered_coords(data: dict) -> list[tuple[float, float]]:
    """A tronçon edge's own coords, ordered downstream (last point == its ``v`` node)."""
    geom = data.get("geometry")
    line = geom if geom.geom_type == "LineString" else list(geom.geoms)[-1]
    coords = [(float(x), float(y)) for x, y in line.coords]
    return list(reversed(coords)) if data.get("flow") == _FLOW_INVERSE else coords


def upstream_offset_point(
    rn: RiverNetwork, river_id: str, offset_m: float
) -> tuple[float, float] | None:
    """A point ``offset_m`` upstream of the outlet, walked along the river's own edges.

    Starting from the outflow node used by :func:`outlet_point`, walks the
    river's tronçon graph upstream (the longest in-edge at each node, same tie
    break as :func:`outlet_point`'s downstream walk), accumulating edge length
    until ``offset_m`` is reached, then interpolates the exact point on that
    edge. Returns ``None`` if the river's own network is shorter than
    ``offset_m`` (the walk runs out of upstream edges first).

    This exists because a river's outlet coordinate sits *at* its confluence
    with its parent — exactly where a pour-point snap is most likely to lock
    onto the parent's much larger channel instead (see
    :func:`delineate_river_search`). Offsetting upstream along the river's own
    mapped course, rather than snapping straight onto the DEM at the mouth,
    gives the snap a point that is actually inside this river's own catchment.
    """
    river = rn.get(river_id)
    if river is None:
        raise ValueError(f"unknown river id {river_id!r}")

    troncons = rn._troncons
    segs = river.segments
    river_edges = rn._edges_by_river.get(river_id, [])
    if not river_edges:
        raise RuntimeError(f"river {river_id!r} has no tronçon geometry")
    heads = {u for u, _v, _d in river_edges}
    tails = {v for _u, v, _d in river_edges}
    outflows = tails - heads
    node = river.outlet if river.outlet in (heads | tails) else (min(outflows) if outflows else min(heads | tails))

    def _prev_edge(n: str):
        cands = [(u, v, d) for u, v, d in troncons.in_edges(n, data=True) if d.get("cleabs") in segs]
        if not cands:
            return None
        return max(cands, key=lambda e: (e[2].get("length_m") or 0.0, e[2].get("cleabs") or ""))

    if offset_m <= 0:
        return outlet_point(rn, river_id)

    accumulated = 0.0
    seen: set[str] = set()
    while node not in seen:
        seen.add(node)
        edge = _prev_edge(node)
        if edge is None:
            return None
        u, _v, data = edge
        seg_len_m = data.get("length_m") or 0.0
        coords = _ordered_coords(data)  # coords[-1] is downstream (this edge's v)
        if accumulated + seg_len_m >= offset_m:
            need = offset_m - accumulated
            cum = 0.0
            for i in range(len(coords) - 1, 0, -1):
                p1, p2 = coords[i], coords[i - 1]
                seg_m = LineString([p1, p2]).length * 111_320
                if cum + seg_m >= need:
                    frac = (need - cum) / seg_m if seg_m > 0 else 0.0
                    return (p1[0] + (p2[0] - p1[0]) * frac, p1[1] + (p2[1] - p1[1]) * frac)
                cum += seg_m
            return coords[0]
        accumulated += seg_len_m
        node = u
    return None


# ---------------------------------------------------------------------- catchment bbox


def _fc_bounds(fc: dict) -> tuple[float, float, float, float] | None:
    """(west, south, east, north) over every feature of a GeoJSON FeatureCollection."""
    feats = fc.get("features") or []
    if not feats:
        return None
    west = south = east = north = None
    for feat in feats:
        geom = feat.get("geometry")
        if geom is None:
            continue
        minx, miny, maxx, maxy = shape(geom).bounds
        if west is None:
            west, south, east, north = minx, miny, maxx, maxy
        else:
            west, south = min(west, minx), min(south, miny)
            east, north = max(east, maxx), max(north, maxy)
    if west is None:
        return None
    return (west, south, east, north)


def catchment_bbox(
    rn: RiverNetwork,
    river_id: str,
    *,
    pad_frac: float = 0.15,
    extra_upstream_frac: float = 0.10,
) -> tuple[float, float, float, float]:
    """A padded WGS84 tile ``(west, south, east, north)`` for the DEM fetch.

    Starts from the bounds of ``rn.river_catchment_geojson(river_id)`` — the
    catchment's stream network, which already fans across the basin — and pads
    every side by ``pad_frac`` of that side's span. The real drainage divide
    keeps widening upstream of the mapped streams, so the two sides *away from
    the outlet* get an additional ``extra_upstream_frac``. Falls back to the
    river's own path bounds if the catchment FeatureCollection is empty.
    """
    bounds = _fc_bounds(rn.river_catchment_geojson(river_id))
    if bounds is None:
        bounds = _fc_bounds(rn.river_path_geojson(river_id))
    if bounds is None:
        raise RuntimeError(f"river {river_id!r} has no geometry to build a bbox from")
    west, south, east, north = bounds
    dx = east - west or 1e-4
    dy = north - south or 1e-4

    olon, olat = outlet_point(rn, river_id)
    cx, cy = (west + east) / 2, (south + north) / 2

    # Base pad on every side; extra pad only on the side opposite the outlet.
    west -= dx * pad_frac + (dx * extra_upstream_frac if olon >= cx else 0.0)
    east += dx * pad_frac + (dx * extra_upstream_frac if olon < cx else 0.0)
    south -= dy * pad_frac + (dy * extra_upstream_frac if olat >= cy else 0.0)
    north += dy * pad_frac + (dy * extra_upstream_frac if olat < cy else 0.0)
    return (west, south, east, north)


# ------------------------------------------------------------------- delineate river


def _course_inside_frac(fc: dict, poly: BaseGeometry) -> float:
    """Fraction of a line FeatureCollection's total length that lies inside ``poly``."""
    total = 0.0
    inside = 0.0
    for feat in fc.get("features") or []:
        geom = feat.get("geometry")
        if geom is None:
            continue
        line = shape(geom)
        length = line.length
        if length <= 0:
            continue
        total += length
        try:
            inside += line.intersection(poly).length
        except Exception:  # pragma: no cover - topology hiccup, treat as outside
            logger.debug("intersection failed for a course feature", exc_info=True)
    return inside / total if total > 0 else 0.0


def delineate_river(
    rn: RiverNetwork,
    river_id: str,
    *,
    dem_dir: Path | None = None,
    demtype: str = "COP30",
    acc_channel_cells: int = 1000,
    dem_source: str = "s3",
) -> tuple[BaseGeometry, dict]:
    """Delineate ``river_id``'s catchment from a DEM and sanity-check it.

    Fetches a DEM tile for :func:`catchment_bbox`, derives the pour point with
    :func:`outlet_point`, and runs :func:`valleespyr.hydro.dem.delineate`.

    ``dem_source`` picks how the tile is obtained:

    * ``"s3"`` (default) — :func:`~valleespyr.hydro.dem.fetch_dem_s3`: public,
      unauthenticated Copernicus GLO-30 tiles from AWS S3, cached per 1x1
      degree grid cell (shared across every river whose bbox falls in the same
      cell — most Pyrenean valleys need only one or two). No API key, no rate
      limit.
    * ``"opentopography"`` — :func:`~valleespyr.hydro.dem.fetch_dem`: the
      OpenTopography REST API, needs an API key and is capped at 50
      downloads/24h on the free tier (see ``valleespyr valley catchments
      precompute``'s docstring). Kept for ``demtype`` values S3 doesn't carry.

    When ``dem_dir`` is given, tiles are cached under it (per-cell for
    ``"s3"``, per-river for ``"opentopography"``); otherwise each fetch
    function picks its own default location under ``data/raw/dem``.

    Returns ``(polygon, diag)`` — ``diag`` is the dict from
    :func:`~valleespyr.hydro.dem.delineate` with four keys added:

    * ``outlet_lonlat`` — the ``(lon, lat)`` pour point used;
    * ``dem_bbox`` — the ``(west, south, east, north)`` tile fetched;
    * ``dem_tif`` — the tile path, as a ``str``;
    * ``course_inside_frac`` — fraction of the river's *own* course length
      (``rn.river_path_geojson``) that falls inside the polygon.

    A low ``course_inside_frac`` is **not** an error — the caller judges it —
    but it is ``logger.warning``-ed when below 0.9.
    """
    from valleespyr.hydro import dem as _dem

    bbox = catchment_bbox(rn, river_id)
    olon, olat = outlet_point(rn, river_id)

    if dem_source == "s3":
        tif = _dem.fetch_dem_s3(bbox, dem_dir=dem_dir)
    elif dem_source == "opentopography":
        dest: Path | None = None
        if dem_dir is not None:
            dem_dir.mkdir(parents=True, exist_ok=True)
            dest = dem_dir / f"{demtype}_{river_id}.tif"
        tif = _dem.fetch_dem(bbox, dest, demtype=demtype)
    else:
        raise ValueError(f"unknown dem_source {dem_source!r} (expected 's3' or 'opentopography')")

    poly, diag = _dem.delineate(
        tif, olon, olat, acc_channel_cells=acc_channel_cells
    )

    course_fc = rn.river_path_geojson(river_id)
    frac = _course_inside_frac(course_fc, poly)
    if frac < 0.9:
        logger.warning(
            "only %.0f%% of %s's own course lies inside the delineated catchment "
            "- check the outlet coord / acc_channel_cells / DEM tile extent",
            frac * 100,
            river_id,
        )

    diag["course_inside_frac"] = frac
    diag["outlet_lonlat"] = (olon, olat)
    diag["dem_bbox"] = bbox
    diag["dem_tif"] = str(tif)
    return poly, diag


# ------------------------------------------------------------- search delineation

# Upstream offsets (metres) tried when the outlet snap lands on a confluence
# ambiguity. 0 is always tried first (most rivers need no offset at all); the
# rest climb past a plausible confluence-braid zone — confirmed on Ruisseau
# d'Aube/Neste du Louron, whose two channels are indistinguishable in COP30
# for ~1 km above their confluence (see METHOD.md).
_SEARCH_OFFSETS_M: tuple[float, ...] = (0, 100, 200, 300, 500, 700, 900, 1100)

# Accumulation thresholds tried at each offset. A wrong (too-high) threshold
# for a small tributary either finds no channel near the offset point or grabs
# an unrelated small rivulet, not the tributary's own integrated channel — see
# METHOD.md. Trying a couple of thresholds per offset costs nothing extra
# (same conditioned grid), and removes the need to hand-tune this per river.
_SEARCH_ACC_THRESHOLDS: tuple[int, ...] = (200, 1000)

# Minimum course_inside_frac (see _course_inside_frac) for a candidate to be
# accepted at all. Below this the delineation is judged wrong, not just noisy.
_SEARCH_MIN_INSIDE_FRAC = 0.9


def delineate_river_search(
    rn: RiverNetwork,
    river_id: str,
    *,
    dem_dir: Path | None = None,
    demtype: str = "COP30",
    dem_source: str = "s3",
    offsets_m: tuple[float, ...] = _SEARCH_OFFSETS_M,
    acc_thresholds: tuple[int, ...] = _SEARCH_ACC_THRESHOLDS,
    min_inside_frac: float = _SEARCH_MIN_INSIDE_FRAC,
) -> tuple[BaseGeometry, dict] | None:
    """Delineate ``river_id`` by searching pour points, not trusting one snap.

    :func:`delineate_river` snaps the vector outlet straight onto the DEM
    channel. That works for most rivers, but fails hard right at a confluence
    with a much bigger parent: the snap has no way to prefer the tributary's
    own (small) inflow cell over the neighbouring trunk cell, and can lock
    onto the *parent's* basin instead — a plausible-looking, wrong polygon
    (confirmed case: Ruisseau d'Aube locking onto la Neste du Louron, see
    METHOD.md). A low ``course_inside_frac`` flags this after the fact but
    :func:`delineate_river` still returns the wrong polygon.

    This function turns that check into a search instead of a warning: it
    tries :func:`upstream_offset_point` at each of ``offsets_m`` (0 first —
    most rivers need no offset), each against ``acc_thresholds``, traces a
    catchment for every combination against **one shared conditioned DEM
    grid** (:func:`~valleespyr.hydro.dem.condition` is only run once), and
    keeps the highest-``course_inside_frac`` candidate — but only if that
    fraction clears ``min_inside_frac``. Returns ``None`` (rather than a
    guess) when no candidate clears the bar; the caller should treat that as
    "undetermined", not silently accept a low-confidence polygon.

    Returns ``(polygon, diag)`` on success, with the same keys as
    :func:`delineate_river`'s ``diag`` plus:

    * ``offset_m`` — the upstream offset (metres) of the winning candidate;
    * ``acc_channel_cells`` — the accumulation threshold of the winning candidate;
    * ``n_candidates`` — how many (offset, threshold) combinations were tried.
    """
    from valleespyr.hydro import dem as _dem

    bbox = catchment_bbox(rn, river_id)

    if dem_source == "s3":
        tif = _dem.fetch_dem_s3(bbox, dem_dir=dem_dir)
    elif dem_source == "opentopography":
        dest: Path | None = None
        if dem_dir is not None:
            dem_dir.mkdir(parents=True, exist_ok=True)
            dest = dem_dir / f"{demtype}_{river_id}.tif"
        tif = _dem.fetch_dem(bbox, dest, demtype=demtype)
    else:
        raise ValueError(f"unknown dem_source {dem_source!r} (expected 's3' or 'opentopography')")

    conditioned = _dem.condition(tif)
    course_fc = rn.river_path_geojson(river_id)

    best: tuple[BaseGeometry, dict, float, float, int] | None = None  # poly, diag, frac, offset, acc_th
    n_tried = 0
    for offset_m in offsets_m:
        point = upstream_offset_point(rn, river_id, offset_m)
        if point is None:
            continue
        for acc_th in acc_thresholds:
            n_tried += 1
            try:
                poly, diag = _dem.trace(conditioned, point[0], point[1], acc_channel_cells=acc_th)
            except RuntimeError:
                continue
            frac = _course_inside_frac(course_fc, poly)
            if best is None or frac > best[2]:
                best = (poly, diag, frac, offset_m, acc_th)
            if frac >= 0.99:  # near-perfect match, no point searching further
                break
        if best is not None and best[2] >= 0.99:
            break

    if best is None or best[2] < min_inside_frac:
        logger.warning(
            "%s: no pour-point candidate reached course_inside_frac >= %.2f "
            "over %d tried (best %.2f) - treating as undetermined",
            river_id,
            min_inside_frac,
            n_tried,
            0.0 if best is None else best[2],
        )
        return None

    poly, diag, frac, offset_m, acc_th = best
    diag["course_inside_frac"] = frac
    diag["outlet_lonlat"] = upstream_offset_point(rn, river_id, offset_m)
    diag["dem_bbox"] = bbox
    diag["dem_tif"] = str(tif)
    diag["offset_m"] = offset_m
    diag["acc_channel_cells"] = acc_th
    diag["n_candidates"] = n_tried
    return poly, diag


__all__ = [
    "resolve_river",
    "outlet_point",
    "upstream_offset_point",
    "catchment_bbox",
    "delineate_river",
    "delineate_river_search",
]
