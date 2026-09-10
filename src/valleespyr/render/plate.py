"""Render a valley catchment as a minimal black-and-white topo plate (SVG + PNG).

The climbing-guidebook look: a clipped contour map of one river's DEM catchment
with the human layer — trails, the GR10, roads, refuges and cabanes, named
summits and cols — drawn over it in line work only. No hillshade, no colour, no
labels fighting the terrain; the contours carry the relief and the type carries
the names.

Two entry points, split so the same prepared geometry can later feed an
isometric 3-D draw:

* :func:`prepare_layers` — reproject every input to one metric CRS, clip it to
  the catchment, and pull contour polylines off the DEM. Returns a
  :class:`PlateLayers` bundle of projected GeoDataFrames + contour arrays.
* :func:`draw_plate` — take a :class:`PlateLayers` and paint it with matplotlib,
  writing an ``.svg`` and a sibling ``.png``.

:func:`build_plate` runs both.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.mask import mask as rio_mask
from shapely.geometry import LineString
from shapely.geometry.base import BaseGeometry

logger = logging.getLogger(__name__)

CONTOUR_MINOR_M = 20      # intermediate contour spacing
CONTOUR_INDEX_M = 100     # every Nth line is a heavier index contour

# ------------------------------------------------------------------ prepared bundle


@dataclass
class PlateLayers:
    """Everything :func:`draw_plate` needs, projected to ``crs`` and clipped.

    Vector layers are ``GeoDataFrame``\\ s (possibly empty). ``contours_minor`` /
    ``contours_index`` are lists of ``(N, 2)`` float arrays — polylines in
    projected coordinates, already clipped to the catchment.
    """

    crs: str
    catchment: gpd.GeoSeries          # single-row, the clip boundary
    contours_minor: list[np.ndarray]
    contours_index: list[np.ndarray]
    # optional sun-lit relief underlay: four separable [0,1] fields on one grid
    # in ``crs`` metres, ready for ``ax.imshow`` at ``relief_extent``. ``None``
    # unless ``hillshade=`` was passed. See :mod:`valleespyr.render.hillshade`:
    #   relief       — directional Lambert hillshade (aspect × sun altitude)
    #   slope        — steepness, direction-free (for a zenithal 2nd light)
    #   core_shadow  — n·L ≤ 0, faces turned from the sun (the hillshade already
    #                  blacks this out; kept for a deliberate ink or debugging)
    #   cast_shadow  — projected shadow alone (blocked by higher ground upwind,
    #                  core removed) — the overlay carrying new information
    relief: np.ndarray | None
    slope: np.ndarray | None
    core_shadow: np.ndarray | None
    cast_shadow: np.ndarray | None
    relief_extent: tuple[float, float, float, float] | None
    water_lines: gpd.GeoDataFrame
    water_areas: gpd.GeoDataFrame
    glaciers: gpd.GeoDataFrame
    streams: gpd.GeoDataFrame         # BD TOPO catchment network (from hydro)
    paths: gpd.GeoDataFrame
    routes: gpd.GeoDataFrame
    roads: gpd.GeoDataFrame
    peaks: gpd.GeoDataFrame
    cols: gpd.GeoDataFrame
    huts: gpd.GeoDataFrame
    settlements: gpd.GeoDataFrame
    z_min: float
    z_max: float
    title: str


# ----------------------------------------------------------------------- contours


def _contour_polylines(
    band: np.ndarray, transform, levels: np.ndarray
) -> dict[float, list[np.ndarray]]:
    """Contour ``band`` at ``levels``; return world-coordinate polylines per level.

    Uses matplotlib's contour engine (already a dependency) on pixel-centre
    coordinates, then maps each vertex through the raster ``transform``. NaN
    cells (outside the catchment) break the lines on their own.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows, cols = band.shape
    xs = np.arange(cols)
    ys = np.arange(rows)
    fig = plt.figure()
    try:
        cs = plt.contour(xs, ys, band, levels=levels)
    finally:
        plt.close(fig)

    out: dict[float, list[np.ndarray]] = {}
    a, b, c, d, e, f = (
        transform.a,
        transform.b,
        transform.c,
        transform.d,
        transform.e,
        transform.f,
    )
    for lvl, segs in zip(cs.levels, cs.allsegs, strict=False):
        polys: list[np.ndarray] = []
        for seg in segs:
            if len(seg) < 2:
                continue
            px = seg[:, 0]
            py = seg[:, 1]
            # affine: world = (a*col + b*row + c, d*col + e*row + f), pixel centres
            wx = a * px + b * py + c
            wy = d * px + e * py + f
            polys.append(np.column_stack([wx, wy]))
        if polys:
            out[float(lvl)] = polys
    return out


# ---------------------------------------------------------------------- hillshade


def _shaded_relief_layer(
    band: np.ndarray,
    transform,
    dem_crs,
    crs: str,
    catchment_polygon: BaseGeometry,
    sun: tuple[float, float],
    shade_gain: float,
) -> tuple[
    np.ndarray, np.ndarray, np.ndarray, np.ndarray,
    tuple[float, float, float, float],
]:
    """The four relief fields for the clipped DEM, warped to ``crs``.

    Returns ``(hillshade, slope, core_shadow, cast_shadow, extent)`` on one grid,
    each ``[0, 1]`` with NaN outside the divide, plus the ``imshow`` extent in
    ``crs``. Nothing is combined — the renderer composes them. The shadow passes
    use the true DEM and true sun altitude; ``shade_gain`` steepens the
    hillshade and slope fields only (cartographic, 1.0 = faithful).

    Cell size in metres comes from the pixel size and the metres-per-degree at
    this latitude when the DEM is geographic. Arrays are masked to
    ``catchment_polygon`` (the ``rio_mask`` clip keeps a pad apron and the
    fictif outlet adds a stray tongue) and then warped into ``crs`` so they
    register pixel-exact with the line work — a lat/lon bbox is not a rectangle
    in Lambert-93.
    """
    from rasterio.features import geometry_mask
    from rasterio.warp import Resampling, calculate_default_transform, reproject

    from .hillshade import shaded_relief

    rows, cols = band.shape
    west, north = transform.c, transform.f
    px, py = transform.a, -transform.e

    if getattr(dem_crs, "is_geographic", False):
        lat_mid = north - rows * py / 2
        dx = px * 111_320.0 * float(np.cos(np.radians(lat_mid)))
        dy = py * 110_540.0
    else:  # already metric
        dx, dy = px, py

    hs, sl, core, cast = shaded_relief(
        band, dx=dx, dy=dy,
        sun_azimuth=sun[0], sun_altitude=sun[1],
        shade_gain=shade_gain, soft_px=1.0,
    )

    outside = geometry_mask(
        [catchment_polygon], out_shape=band.shape, transform=transform, invert=False
    )
    hs = np.where(outside, np.nan, hs)
    sl = np.where(outside, np.nan, sl)
    core = np.where(outside, np.nan, core)
    cast = np.where(outside, np.nan, cast)

    dst_transform, dw, dh = calculate_default_transform(
        dem_crs, crs, cols, rows, west, north - rows * py, west + cols * px, north
    )

    def _warp(src: np.ndarray) -> np.ndarray:
        out = np.full((dh, dw), np.nan, dtype="float64")
        reproject(
            source=src, destination=out,
            src_transform=transform, src_crs=dem_crs,
            dst_transform=dst_transform, dst_crs=crs,
            src_nodata=np.nan, dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )
        return out

    x0 = dst_transform.c
    y1 = dst_transform.f
    x1 = x0 + dw * dst_transform.a
    y0 = y1 + dh * dst_transform.e
    return _warp(hs), _warp(sl), _warp(core), _warp(cast), (x0, x1, y0, y1)


# ------------------------------------------------------------------------- prepare


def _clip(gdf: gpd.GeoDataFrame, boundary: BaseGeometry, crs: str) -> gpd.GeoDataFrame:
    """Reproject ``gdf`` to ``crs`` and keep only what intersects ``boundary``."""
    if gdf is None or len(gdf) == 0:
        return gpd.GeoDataFrame(
            {"geometry": []}, geometry="geometry", crs=crs
        )
    g = gdf.to_crs(crs)
    hit = g[g.intersects(boundary)].copy()
    if len(hit) == 0:
        return hit
    hit["geometry"] = hit.geometry.intersection(boundary)
    hit = hit[~hit.geometry.is_empty]
    return hit


def prepare_layers(
    catchment_polygon: BaseGeometry,
    dem_tif: Path,
    streams_fc: dict,
    osm: dict[str, gpd.GeoDataFrame],
    *,
    title: str,
    crs: str = "EPSG:2154",
    hillshade: tuple[float, float] | None = None,
    shade_gain: float = 1.0,
) -> PlateLayers:
    """Project, clip and contour everything for one valley plate.

    ``catchment_polygon`` is the DEM-delineated basin in EPSG:4326 (from
    :func:`valleespyr.valley.delineate_river`); ``dem_tif`` the COP30 tile it was
    cut from; ``streams_fc`` the catchment stream network as GeoJSON
    (:meth:`~valleespyr.hydro.rivers.RiverNetwork.river_catchment_geojson`);
    ``osm`` the dict from :func:`valleespyr.sources.osm.fetch_osm_features`.

    Everything is reprojected to ``crs`` (Lambert-93 by default — right for the
    French Pyrénées; pass a UTM CRS for a wider basin) and clipped to the
    catchment outline.

    Pass ``hillshade=(sun_azimuth_deg, sun_altitude_deg)`` to also compute the
    four relief fields (hillshade, slope, core shadow, cast shadow — all numpy,
    no Blender); :func:`draw_plate` composes them per style. The shadow passes
    use the true DEM and true sun altitude; ``shade_gain`` steepens the
    hillshade and slope fields only, a cartographic slope exaggeration for
    legibility (1.0 = physically faithful).
    """
    catch_ll = gpd.GeoSeries([catchment_polygon], crs="EPSG:4326")
    catch = catch_ll.to_crs(crs)
    boundary = catch.iloc[0]

    # --- DEM: clip to the catchment, contour it -------------------------------
    with rasterio.open(dem_tif) as src:
        arr, transform = rio_mask(
            src, [catchment_polygon], crop=True, filled=True, nodata=np.nan
        )
        band = arr[0].astype("float64")
        dem_crs = src.crs

    finite = np.isfinite(band)
    if not finite.any():
        raise RuntimeError("DEM clip is empty — catchment polygon and tile do not overlap")
    z_min = float(np.nanmin(band))
    z_max = float(np.nanmax(band))

    lo = np.floor(z_min / CONTOUR_MINOR_M) * CONTOUR_MINOR_M + CONTOUR_MINOR_M
    levels = np.arange(lo, z_max, CONTOUR_MINOR_M)
    by_level = _contour_polylines(band, transform, levels)

    minor: list[np.ndarray] = []
    index: list[np.ndarray] = []
    # contours come out in the DEM's own CRS (EPSG:4326 here); reproject the
    # vertices in bulk to the working CRS.
    tf = _transformer(dem_crs, crs)
    for lvl, polys in by_level.items():
        bucket = index if round(lvl) % CONTOUR_INDEX_M == 0 else minor
        for poly in polys:
            x, y = tf(poly[:, 0], poly[:, 1])
            bucket.append(np.column_stack([x, y]))

    # --- optional sun-lit relief underlay (four separable fields) -------
    relief: np.ndarray | None = None
    relief_slope: np.ndarray | None = None
    core_shadow: np.ndarray | None = None
    cast_shadow: np.ndarray | None = None
    relief_extent: tuple[float, float, float, float] | None = None
    if hillshade is not None:
        (relief, relief_slope, core_shadow, cast_shadow,
         relief_extent) = _shaded_relief_layer(
            band, transform, dem_crs, crs, catchment_polygon,
            hillshade, shade_gain,
        )

    # --- BD TOPO catchment streams (GeoJSON -> GDF) -------------------------
    streams = gpd.GeoDataFrame.from_features(
        streams_fc.get("features", []), crs="EPSG:4326"
    )
    if len(streams) == 0:
        streams = gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs="EPSG:4326")
    streams = _clip(streams, boundary, crs)

    def L(name: str) -> gpd.GeoDataFrame:
        return _clip(osm.get(name), boundary, crs)

    layers = PlateLayers(
        crs=crs,
        catchment=catch,
        contours_minor=minor,
        contours_index=index,
        relief=relief,
        slope=relief_slope,
        core_shadow=core_shadow,
        cast_shadow=cast_shadow,
        relief_extent=relief_extent,
        water_lines=L("water_lines"),
        water_areas=L("water_areas"),
        glaciers=L("glaciers"),
        streams=streams,
        paths=L("paths"),
        routes=L("routes"),
        roads=L("roads"),
        peaks=L("peaks"),
        cols=L("cols"),
        huts=L("huts"),
        settlements=L("settlements"),
        z_min=z_min,
        z_max=z_max,
        title=title,
    )
    logger.info(
        "[plate] contours %d minor / %d index; streams %d; "
        "paths %d, routes %d, roads %d; peaks %d, cols %d, huts %d, settlements %d",
        len(minor), len(index), len(streams),
        len(layers.paths), len(layers.routes), len(layers.roads),
        len(layers.peaks), len(layers.cols), len(layers.huts), len(layers.settlements),
    )
    return layers


def _transformer(src_crs, dst_crs):
    """A vectorised ``(xs, ys) -> (xs, ys)`` coordinate transform."""
    from pyproj import Transformer

    t = Transformer.from_crs(src_crs, dst_crs, always_xy=True)
    return lambda xs, ys: t.transform(xs, ys)


# ---------------------------------------------------------------------------- draw

# black on warm white, one ink. Weights in points.
INK = "#1a1a1a"
PAPER = "#faf8f2"
_STYLE = {
    "catchment": dict(color=INK, lw=1.4),
    "contour_minor": dict(color="#8a8a8a", lw=0.35),
    "contour_index": dict(color="#4d4d4d", lw=0.7),
    "water_fill": dict(facecolor="#c9d3d6", edgecolor=INK, lw=0.5),
    "glacier_fill": dict(facecolor="#e4e9ec", edgecolor="#7f8a90", lw=0.4),
    "water_line": dict(color="#3f5560", lw=0.7),
    "stream": dict(color="#5a7079", lw=0.4),
    "road": dict(color=INK, lw=1.1),
    "road_minor": dict(color=INK, lw=0.7),
    "route_case": dict(color=INK, lw=2.2, alpha=0.25),
    "path": dict(color=INK, lw=0.8, dashes=(2.2, 1.8)),
}

_MINOR_ROADS = {"service", "residential", "living_street", "track", "road", "unclassified"}


def _draw_lines(ax, gdf: gpd.GeoDataFrame, **kw) -> None:
    if gdf is None or len(gdf) == 0:
        return
    for geom in gdf.geometry:
        for part in _line_parts(geom):
            x, y = part.xy
            ax.plot(x, y, solid_capstyle="round", **kw)


def _line_parts(geom: BaseGeometry):
    gt = geom.geom_type
    if gt == "LineString":
        return [geom]
    if gt == "MultiLineString":
        return list(geom.geoms)
    if gt in ("GeometryCollection", "MultiPolygon", "Polygon"):
        out = []
        geoms = [geom] if gt == "Polygon" else getattr(geom, "geoms", [])
        for g in geoms:
            if g.geom_type == "Polygon":
                out.append(LineString(g.exterior.coords))
        return out
    return []


def _draw_polys(ax, gdf: gpd.GeoDataFrame, **kw) -> None:
    if gdf is None or len(gdf) == 0:
        return
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path as MplPath

    for geom in gdf.geometry:
        polys = [geom] if geom.geom_type == "Polygon" else list(
            getattr(geom, "geoms", [])
        )
        for poly in polys:
            if poly.geom_type != "Polygon":
                continue
            verts = list(poly.exterior.coords)
            codes = [MplPath.MOVETO] + [MplPath.LINETO] * (len(verts) - 1)
            for ring in poly.interiors:
                rv = list(ring.coords)
                verts += rv
                codes += [MplPath.MOVETO] + [MplPath.LINETO] * (len(rv) - 1)
            ax.add_patch(PathPatch(MplPath(verts, codes), **kw))


def _label_boxes_overlap(a, b, pad=0.0) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (ax1 + pad < bx0 or bx1 + pad < ax0 or ay1 + pad < by0 or by1 + pad < ay0)


def _place_labels(ax, items: list[tuple[float, float, str, dict]], span: float) -> None:
    """Greedy de-collision: ``items`` already sorted by priority; drop overlaps.

    Each item is ``(x, y, text, kw)``. Label box size is estimated from the text
    length and a fraction of the map span — good enough for a demo plate.
    """
    placed: list[tuple[float, float, float, float]] = []
    ch = span * 0.011  # rough character cell
    for x, y, text, kw in items:
        longest = max((len(line) for line in str(text).splitlines()), default=1)
        w = longest * ch * 0.62
        h = ch * 1.3
        dx = span * 0.008
        box = (x + dx, y - h / 2, x + dx + w, y + h / 2)
        if any(_label_boxes_overlap(box, p, pad=span * 0.002) for p in placed):
            continue
        placed.append(box)
        ax.annotate(text, (x + dx, y), **kw)


def _draw_relief(
    ax,
    layers: PlateLayers,
    *,
    hillshade_strength: float = 0.28,
    zenith_weight: float = 0.0,
    core_strength: float = 0.0,
    cast_strength: float = 0.62,
) -> None:
    """Compose the four relief fields into washes under the line work.

    All four arrays share one grid + extent; each knob picks how much of that
    field reaches the paper:

    * **hillshade wash** — the raking Lambert light, optionally blended with a
      zenithal second light (``zenith_weight``, ``1 - slope``: bright flat, dark
      steep, IGN "deux soleils") so lee slopes stay modelled instead of going
      black. Laid down at ``hillshade_strength`` alpha in a warm neutral.
    * **core-shadow ink** (``core_strength``) — the ``n·L ≤ 0`` faces. Off by
      default: the hillshade wash already darkens them. Turn it on to mark the
      sun/shade line as its own edge.
    * **cast-shadow ink** (``cast_strength``) — the projected shadow alone
      (sun-facing ground blocked upwind, core removed), a cool slate on top. A
      crisp bounded shape, so it stays legible against the isolines.
    """
    import numpy as _np
    from matplotlib.colors import to_rgb

    from .hillshade import compose_relief

    x0, x1, y0, y1 = layers.relief_extent
    common = dict(extent=(x0, x1, y0, y1), origin="upper",
                  interpolation="bilinear", clip_on=True)

    def _wash(mask: _np.ndarray, hex_color: str, strength: float, zorder: float):
        m = _np.where(_np.isfinite(mask), _np.clip(mask, 0.0, 1.0), 0.0)
        rgba = _np.zeros((*m.shape, 4), dtype=float)
        rgba[..., :3] = to_rgb(hex_color)
        rgba[..., 3] = m * strength
        ax.imshow(rgba, zorder=zorder, **common)

    if layers.relief is not None:
        shade = compose_relief(
            layers.relief, layers.slope, zenith_weight=zenith_weight
        )
        # darkness = 1 - brightness; warm neutral ink
        _wash(1.0 - shade, "#6b6152", hillshade_strength, 0.4)

    if layers.core_shadow is not None and core_strength > 0:
        _wash(layers.core_shadow, "#4d4a44", core_strength, 0.45)

    if layers.cast_shadow is not None and cast_strength > 0:
        _wash(layers.cast_shadow, "#39414d", cast_strength, 0.5)


def draw_plate(
    layers: PlateLayers,
    out_svg: Path,
    *,
    subtitle: str = "",
    write_png: bool = True,
    dpi: int = 200,
    zenith_weight: float = 0.0,
    core_strength: float = 0.0,
    cast_strength: float = 0.62,
) -> list[Path]:
    """Paint a :class:`PlateLayers` bundle to ``out_svg`` (+ a sibling ``.png``).

    Returns the paths written. ``subtitle`` goes in the title block under the
    valley name (e.g. an area / elevation-range string). The relief knobs
    (``zenith_weight``, ``core_strength``, ``cast_strength``) are passed straight
    to :func:`_draw_relief`; they do nothing unless ``layers`` carries a relief.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    minx, miny, maxx, maxy = layers.catchment.total_bounds
    span = max(maxx - minx, maxy - miny)
    pad = span * 0.06

    fig, ax = plt.subplots(figsize=(10, 10))
    fig.patch.set_facecolor(PAPER)
    ax.set_facecolor(PAPER)
    ax.set_xlim(minx - pad, maxx + pad)
    ax.set_ylim(miny - pad, maxy + pad)
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    for s in ax.spines.values():
        s.set_visible(False)

    # --- sun-lit relief underlay (optional, below everything) -------------
    if layers.relief is not None and layers.relief_extent is not None:
        _draw_relief(
            ax, layers, zenith_weight=zenith_weight,
            core_strength=core_strength, cast_strength=cast_strength,
        )

    # --- fills (bottom) ----------------------------------------------------
    _draw_polys(ax, layers.glaciers, **_STYLE["glacier_fill"], zorder=1)
    _draw_polys(ax, layers.water_areas, **_STYLE["water_fill"], zorder=4)

    # --- contours --------------------------------------------------------
    for poly in layers.contours_minor:
        ax.plot(poly[:, 0], poly[:, 1], **_STYLE["contour_minor"], zorder=2)
    for poly in layers.contours_index:
        ax.plot(poly[:, 0], poly[:, 1], **_STYLE["contour_index"], zorder=3)

    # --- water lines + BD TOPO streams ----------------------------------
    _draw_lines(ax, layers.streams, **_STYLE["stream"], zorder=4)
    _draw_lines(ax, layers.water_lines, **_STYLE["water_line"], zorder=5)

    # --- catchment outline --------------------------------------------
    for geom in layers.catchment.geometry:
        for part in _line_parts(geom):
            x, y = part.xy
            ax.plot(x, y, **_STYLE["catchment"], zorder=6)

    # --- roads, hiking routes (cased), paths --------------------------
    if len(layers.roads):
        minor_mask = layers.roads.get("highway", "").isin(_MINOR_ROADS)
        _draw_lines(ax, layers.roads[~minor_mask], **_STYLE["road"], zorder=7)
        _draw_lines(ax, layers.roads[minor_mask], **_STYLE["road_minor"], zorder=7)
    _draw_lines(ax, layers.routes, **_STYLE["route_case"], zorder=7.5)
    _draw_lines(ax, layers.paths, **_STYLE["path"], zorder=8)

    # --- point symbols + labels -------------------------------------
    label_items: list[tuple[float, float, str, dict]] = []
    _draw_peaks(ax, layers.peaks, span, label_items)
    _draw_cols(ax, layers.cols, span, label_items)
    _draw_huts(ax, layers.huts, span, label_items)
    _draw_settlements(ax, layers.settlements, span, label_items)
    _place_labels(ax, label_items, span)

    # --- frame, scale bar, north arrow, title block ------------------
    _decorate(ax, fig, layers, span, subtitle)

    fig.subplots_adjust(left=0.03, right=0.97, top=0.97, bottom=0.03)
    out_svg = Path(out_svg)
    written = [out_svg]
    fig.savefig(out_svg, facecolor=PAPER)
    if write_png:
        png = out_svg.with_suffix(".png")
        fig.savefig(png, facecolor=PAPER, dpi=dpi)
        written.append(png)
    plt.close(fig)
    logger.info("[plate] wrote %s", ", ".join(str(p) for p in written))
    return written


# ------------------------------------------------------------------ point symbols


def _xy(gdf: gpd.GeoDataFrame):
    return gdf.geometry.x.to_numpy(), gdf.geometry.y.to_numpy()


def _draw_peaks(ax, gdf, span, labels) -> None:
    if len(gdf) == 0:
        return
    r = span * 0.006
    named = gdf[gdf["name"].notna()] if "name" in gdf else gdf.iloc[0:0]
    # label highest first so the greedy de-collision keeps the big summits
    order = (
        named.sort_values("ele", ascending=False)
        if "ele" in named and named["ele"].notna().any()
        else named
    )
    for _, row in gdf.iterrows():
        x, y = row.geometry.x, row.geometry.y
        ax.plot(
            [x - r, x + r, x, x - r], [y - r, y - r, y + r, y - r],
            color=INK, lw=0.9, solid_capstyle="round", zorder=9,
        )
    for _, row in order.iterrows():
        x, y = row.geometry.x, row.geometry.y
        ele = row.get("ele")
        txt = row["name"] + (f"\n{int(ele)} m" if ele == ele and ele else "")
        labels.append((x, y, txt, dict(
            fontsize=7, color=INK, va="center", ha="left", zorder=10,
            linespacing=0.95,
        )))


def _draw_cols(ax, gdf, span, labels) -> None:
    if len(gdf) == 0:
        return
    r = span * 0.006
    for _, row in gdf.iterrows():
        x, y = row.geometry.x, row.geometry.y
        # a small bowtie ")(" — two arcs facing away
        t = np.linspace(-0.9, 0.9, 12)
        ax.plot(x - r + 0.5 * r * (1 - np.cos(t)), y + r * np.sin(t),
                color=INK, lw=0.8, zorder=9)
        ax.plot(x + r - 0.5 * r * (1 - np.cos(t)), y + r * np.sin(t),
                color=INK, lw=0.8, zorder=9)
    named = gdf[gdf["name"].notna()] if "name" in gdf else gdf.iloc[0:0]
    for _, row in named.iterrows():
        x, y = row.geometry.x, row.geometry.y
        ele = row.get("ele")
        txt = row["name"] + (f"\n{int(ele)} m" if ele == ele and ele else "")
        labels.append((x, y, txt, dict(
            fontsize=6.5, color=INK, va="center", ha="left", style="italic",
            zorder=10, linespacing=0.95,
        )))


def _draw_huts(ax, gdf, span, labels) -> None:
    if len(gdf) == 0:
        return
    r = span * 0.006
    for _, row in gdf.iterrows():
        x, y = row.geometry.x, row.geometry.y
        sq_x = [x - r, x + r, x + r, x - r, x - r]
        sq_y = [y - r, y - r, y + r, y + r, y - r]
        filled = row.get("kind") == "refuge"
        if filled:
            ax.fill(sq_x, sq_y, color=INK, zorder=9)
        else:
            ax.plot(sq_x, sq_y, color=INK, lw=0.9, zorder=9)
        name = row.get("name")
        if isinstance(name, str) and name:
            labels.append((x, y, name, dict(
                fontsize=7, color=INK, va="center", ha="left",
                fontweight="bold" if filled else "normal", zorder=10,
            )))


def _draw_settlements(ax, gdf, span, labels) -> None:
    if len(gdf) == 0:
        return
    r = span * 0.004
    for _, row in gdf.iterrows():
        x, y = row.geometry.x, row.geometry.y
        circ = plt_circle(x, y, r)
        ax.plot(circ[0], circ[1], color=INK, lw=0.9, zorder=9)
        ax.fill(circ[0], circ[1], color=PAPER, zorder=8.5)
        name = row.get("name")
        if isinstance(name, str) and name:
            labels.append((x, y, name.upper(), dict(
                fontsize=7.5, color=INK, va="center", ha="left",
                fontweight="bold", zorder=10,
            )))


def plt_circle(cx, cy, r, n=24):
    a = np.linspace(0, 2 * np.pi, n)
    return cx + r * np.cos(a), cy + r * np.sin(a)


# ---------------------------------------------------------------------- decoration


def _decorate(ax, fig, layers: PlateLayers, span: float, subtitle: str) -> None:
    minx, miny, maxx, maxy = layers.catchment.total_bounds

    # frame just inside the axes
    fr = span * 0.03
    ax.plot(
        [minx - fr, maxx + fr, maxx + fr, minx - fr, minx - fr],
        [miny - fr, miny - fr, maxy + fr, maxy + fr, miny - fr],
        color=INK, lw=1.0, zorder=20, clip_on=False,
    )

    # scale bar: a "nice" round length ~1/4 of the span
    raw = span * 0.25
    nice = _nice_round(raw)
    x0 = minx
    y0 = miny - span * 0.008
    ax.plot([x0, x0 + nice], [y0, y0], color=INK, lw=2.4, zorder=21,
            solid_capstyle="butt", clip_on=False)
    ax.plot([x0, x0], [y0 - span * 0.006, y0 + span * 0.006], color=INK, lw=1.0,
            zorder=21, clip_on=False)
    ax.plot([x0 + nice, x0 + nice], [y0 - span * 0.006, y0 + span * 0.006],
            color=INK, lw=1.0, zorder=21, clip_on=False)
    ax.annotate(
        f"{nice / 1000:g} km" if nice >= 1000 else f"{nice:g} m",
        (x0 + nice / 2, y0 + span * 0.012), ha="center", va="bottom",
        fontsize=7.5, color=INK, zorder=21,
    )

    # north arrow, top-right
    nx = maxx + fr - span * 0.03
    ny = maxy - span * 0.02
    ax.annotate(
        "", (nx, ny), (nx, ny - span * 0.07),
        arrowprops=dict(arrowstyle="-|>", color=INK, lw=1.4), zorder=21,
        clip_on=False,
    )
    ax.annotate("N", (nx, ny + span * 0.006), ha="center", va="bottom",
                fontsize=9, fontweight="bold", color=INK, zorder=21, clip_on=False)

    # title block, top-left
    ax.annotate(
        layers.title, (minx - fr, maxy + fr + span * 0.01),
        ha="left", va="bottom", fontsize=15, fontweight="bold", color=INK,
        zorder=21, clip_on=False,
    )
    sub = subtitle or (
        f"{layers.z_min:.0f}–{layers.z_max:.0f} m  ·  contours {CONTOUR_MINOR_M} m "
        f"(index {CONTOUR_INDEX_M} m)  ·  COP30  ·  OSM"
    )
    ax.annotate(
        sub, (minx - fr, maxy + fr - span * 0.004),
        ha="left", va="top", fontsize=7.5, color="#555", zorder=21, clip_on=False,
    )


def _nice_round(x: float) -> float:
    """Round ``x`` down to 1/2/5 × 10ⁿ."""
    if x <= 0:
        return 1.0
    mag = 10 ** np.floor(np.log10(x))
    for m in (5, 2, 1):
        if x >= m * mag:
            return m * mag
    return mag


# ---------------------------------------------------------------------------- top


def build_plate(
    catchment_polygon: BaseGeometry,
    dem_tif: Path,
    streams_fc: dict,
    osm: dict[str, gpd.GeoDataFrame],
    out_svg: Path,
    *,
    title: str,
    subtitle: str = "",
    crs: str = "EPSG:2154",
    write_png: bool = True,
    hillshade: tuple[float, float] | None = None,
    shade_gain: float = 1.0,
    zenith_weight: float = 0.0,
    core_strength: float = 0.0,
    cast_strength: float = 0.62,
) -> list[Path]:
    """Prepare + draw a valley topo plate in one call. Returns the paths written.

    ``hillshade=(sun_azimuth, sun_altitude)`` computes the four relief fields;
    ``shade_gain`` steepens the hillshade/slope fields only (1.0 = faithful).
    The compositing knobs — ``zenith_weight`` (IGN "deux soleils" blend),
    ``core_strength`` (ink the terminator), ``cast_strength`` (projected-shadow
    overlay) — choose how those fields reach the paper.
    """
    layers = prepare_layers(
        catchment_polygon, dem_tif, streams_fc, osm, title=title, crs=crs,
        hillshade=hillshade, shade_gain=shade_gain,
    )
    return draw_plate(
        layers, out_svg, subtitle=subtitle, write_png=write_png,
        zenith_weight=zenith_weight, core_strength=core_strength,
        cast_strength=cast_strength,
    )


__all__ = ["PlateLayers", "prepare_layers", "draw_plate", "build_plate"]
