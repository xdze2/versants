"""Clip a river catchment's DEM and stream network into a compact terrain payload.

Shared by :mod:`valleespyr.render.diorama` (one standalone 3D HTML file) and
:mod:`valleespyr.render.terrain_precompute` (a batch of small per-river JSON
files for the catalog's 3D view): both need the same heightmap PNG + draped
stream polylines + contour polylines, just packaged differently at the end.
"""

from __future__ import annotations

import base64
import io
import logging
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from PIL import Image
from rasterio.mask import mask as rio_mask
from shapely.geometry import shape

logger = logging.getLogger(__name__)

CONTOUR_INTERVAL_M = 20     # minor contour spacing
CONTOUR_INDEX_M = 100       # every Nth line drawn as a heavier index contour


def _keep_largest_component(band: np.ndarray) -> np.ndarray:
    """NaN out every valid cell not 4-connected to the largest valid blob."""
    valid = np.isfinite(band)
    labels = np.zeros(valid.shape, dtype=np.int32)
    cur = 0
    stack: list[tuple[int, int]] = []
    for r in range(valid.shape[0]):
        for c in range(valid.shape[1]):
            if not valid[r, c] or labels[r, c]:
                continue
            cur += 1
            labels[r, c] = cur
            stack.append((r, c))
            while stack:
                y, x = stack.pop()
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = y + dy, x + dx
                    if (0 <= ny < valid.shape[0] and 0 <= nx < valid.shape[1]
                            and valid[ny, nx] and not labels[ny, nx]):
                        labels[ny, nx] = cur
                        stack.append((ny, nx))
    if cur <= 1:
        return band
    counts = np.bincount(labels.ravel())
    counts[0] = 0
    keep = counts.argmax()
    out = band.copy()
    out[labels != keep] = np.nan
    return out


def clip_dem(
    catchment_polygon,
    dem_tif: Path,
    pad_cells: int,
    z_exaggeration: float,
) -> tuple[np.ndarray, dict]:
    """Return (elevation array with NaN outside the catchment, meta dict)."""
    geom = catchment_polygon

    with rasterio.open(dem_tif) as src:
        # buffer the polygon by a few cells so the mask keeps an apron
        buffered = geom.buffer(pad_cells * src.res[0])
        arr, transform = rio_mask(src, [buffered], crop=True, filled=True,
                                  nodata=np.nan)
        band = arr[0].astype("float64")
        # full (unclipped) tile height range, for a stable colour ramp
        full = src.read(1).astype("float64")

    rows, cols = band.shape
    west = transform.c
    north = transform.f
    dx = transform.a
    dy = -transform.e  # positive
    east = west + cols * dx
    south = north - rows * dy

    # drop cells that are not 4-connected to the main body: the raster mask has
    # a few diagonal one-cell bridges (the fictif outlet connector, stray pixels)
    # that otherwise extrude into spikes.
    band = _keep_largest_component(band)

    valid = np.isfinite(band)
    meta = {
        "rows": rows,
        "cols": cols,
        "bounds": {"west": west, "south": south, "east": east, "north": north},
        "cell_deg": dx,
        # metres per degree at this latitude, for x/y world scaling
        "m_per_deg_lon": 111320.0 * np.cos(np.radians((north + south) / 2)),
        "m_per_deg_lat": 110540.0,
        "z_min": float(np.nanmin(band)),
        "z_max": float(np.nanmax(band)),
        "z_min_tile": float(full.min()),
        "z_max_tile": float(full.max()),
        "n_valid": int(valid.sum()),
        "z_exaggeration": z_exaggeration,
    }
    return band, meta


# IGN's "deux soleils" estompage preset (see valleespyr.cli's `plate --style
# ign`) - a raking NW sun blended with a zenithal fill so slopes facing away
# from the sun don't collapse to solid black. Kept as the fixed default here
# (unlike `plate`, the 3D view has no per-call --sun-azimuth flag) so every
# baked terrain file shades consistently.
RELIEF_SUN_AZIMUTH_DEG = 315.0
RELIEF_SUN_ALTITUDE_DEG = 45.0
RELIEF_ZENITH_WEIGHT = 0.55


def _relief_shade(band: np.ndarray, meta: dict) -> np.ndarray:
    """``[0, 1]`` IGN-style shade for ``band``, NaN outside the catchment."""
    from .hillshade import compose_relief, shaded_relief

    dx = meta["cell_deg"] * meta["m_per_deg_lon"]
    dy = meta["cell_deg"] * meta["m_per_deg_lat"]
    hs, sl, _core, _cast = shaded_relief(
        band, dx=dx, dy=dy,
        sun_azimuth=RELIEF_SUN_AZIMUTH_DEG, sun_altitude=RELIEF_SUN_ALTITUDE_DEG,
    )
    return compose_relief(hs, sl, zenith_weight=RELIEF_ZENITH_WEIGHT)


def heightmap_png(band: np.ndarray, meta: dict) -> str:
    """Greyscale+shade PNG as a data URI.

    RG channels carry a 16-bit height: elevation scaled over the *tile*
    z-range (stable ramp), low byte in R, high byte in G, so the browser can
    recover ~1700 m / 65535 ~= 3 cm resolution from an 8-bit canvas read. B
    carries an IGN-style hillshade+slope estompage (see :func:`_relief_shade`),
    baked once here instead of relit client-side, so the terrain reads with
    real relief instead of flat hypsometric tint. Alpha 0 marks cells outside
    the catchment.
    """
    z0, z1 = meta["z_min_tile"], meta["z_max_tile"]
    norm = np.clip((band - z0) / (z1 - z0), 0, 1)
    valid = np.isfinite(band)
    q = np.where(valid, np.round(norm * 65535), 0).astype(np.uint16)

    shade = _relief_shade(band, meta)
    shade_q = np.where(valid, np.round(np.nan_to_num(shade, nan=1.0) * 255), 0).astype(np.uint8)

    rgba = np.zeros((*band.shape, 4), dtype=np.uint8)
    rgba[..., 0] = q & 0xFF          # low byte
    rgba[..., 1] = (q >> 8) & 0xFF   # high byte
    rgba[..., 2] = shade_q           # relief shade
    rgba[..., 3] = np.where(valid, 255, 0)

    img = Image.fromarray(rgba, mode="RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def streams_polylines(meta: dict, streams_fc: dict) -> list[list[list[float]]]:
    """Stream lines as polylines of [u, v], u/v normalised to the clipped grid.

    Consumes the features of ``streams_fc`` (a GeoJSON ``FeatureCollection`` of
    the catchment stream network in WGS84); each feature geometry is a
    LineString or MultiLineString.

    Height is sampled on the client from the same heightmap the surface uses, so
    the tubes sit exactly on the terrain instead of a slightly different DEM read.
    Vertices are densified to <=~1 grid cell spacing so a long segment still
    follows the surface between its original nodes.
    """
    b = meta["bounds"]
    du = b["east"] - b["west"]
    dv = b["north"] - b["south"]
    step_deg = meta["cell_deg"]  # ~1 grid cell

    out: list[list[list[float]]] = []
    for feat in streams_fc.get("features", []):
        geom = feat.get("geometry")
        if geom is None:
            continue
        g = shape(geom)
        parts = g.geoms if g.geom_type == "MultiLineString" else [g]
        for part in parts:
            raw = [(c[0], c[1]) for c in part.coords]
            dense: list[tuple[float, float]] = []
            for (lon0, lat0), (lon1, lat1) in zip(raw, raw[1:], strict=False):
                seg = np.hypot(lon1 - lon0, lat1 - lat0)
                n = max(1, int(seg / step_deg))
                for k in range(n):
                    f = k / n
                    dense.append((lon0 + f * (lon1 - lon0),
                                  lat0 + f * (lat1 - lat0)))
            dense.append(raw[-1])

            pts = []
            for lon, lat in dense:
                u = (lon - b["west"]) / du
                v = (lat - b["south"]) / dv
                if -0.05 <= u <= 1.05 and -0.05 <= v <= 1.05:
                    pts.append([round(u, 5), round(v, 5)])
            if len(pts) >= 2:
                out.append(pts)
    return out


def contour_polylines(band: np.ndarray, meta: dict):
    """20 m contour polylines over the clipped DEM, in normalised grid space.

    Returns (minor, index) where each is a list of [[u, v], ...] polylines.
    ``u`` runs west->east in [0,1], ``v`` south->north in [0,1] - the same
    space the streams use, so the client drapes them with one sampler.
    NaN cells (outside the catchment) are left out: matplotlib breaks the
    lines there on its own.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows, cols = band.shape
    z0 = np.floor(meta["z_min"] / CONTOUR_INTERVAL_M) * CONTOUR_INTERVAL_M
    z1 = meta["z_max"]
    levels = np.arange(z0 + CONTOUR_INTERVAL_M, z1, CONTOUR_INTERVAL_M)

    # contour on pixel-centre coordinates, then normalise. row 0 is north, so
    # v = 1 - row/(rows-1).
    xs = np.arange(cols)
    ys = np.arange(rows)
    fig = plt.figure()
    cs = plt.contour(xs, ys, band, levels=levels)
    plt.close(fig)

    minor: list[list[list[float]]] = []
    index: list[list[list[float]]] = []
    for lvl, segs in zip(cs.levels, cs.allsegs, strict=False):
        is_index = round(lvl) % CONTOUR_INDEX_M == 0
        bucket = index if is_index else minor
        for seg in segs:
            if len(seg) < 2:
                continue
            poly = [[round(x / (cols - 1), 5), round(1 - y / (rows - 1), 5)]
                    for x, y in seg]
            bucket.append(poly)
    return minor, index


def flatten_polylines(polylines: list[list[list[float]]]) -> list[float]:
    """[[ [u,v], ... ], ...] -> flat [u,v, u,v, ..., NaN,NaN, ...] with NaN gaps.

    One flat array + NaN break markers keeps the payload small and lets the
    client build a single LineSegments buffer.
    """
    out: list[float] = []
    for poly in polylines:
        for u, v in poly:
            out.append(u)
            out.append(v)
        out.append(None)  # JSON null -> NaN on the client, a line break
        out.append(None)
    return out


def _basemap_jpeg(bounds: dict, rows: int, cols: int, tile_dir: Path | None) -> str | None:
    """IGN Plan basemap for ``bounds``, as a JPEG data URI, or ``None`` if unavailable."""
    import io

    from PIL import Image

    from .basemap import fetch_basemap_rgb

    rgb = fetch_basemap_rgb(bounds, rows, cols, tile_dir=tile_dir)
    if rgb is None:
        return None
    buf = io.BytesIO()
    Image.fromarray(rgb, mode="RGB").save(buf, format="JPEG", quality=82)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def build_terrain_payload(
    catchment_polygon,
    dem_tif: Path,
    streams_fc: dict,
    *,
    z_exaggeration: float = 1.0,
    pad_cells: int = 6,
    basemap: bool = False,
    basemap_tile_dir: Path | None = None,
) -> dict[str, Any]:
    """Clip, encode and return one river's terrain as a JSON-able dict.

    ``{"meta", "heightmap", "streams", "contours_minor", "contours_index"}`` -
    everything a client needs to build the same solid terrain block
    :func:`valleespyr.render.diorama.build_diorama` bakes into a standalone
    HTML, minus the page chrome. Used by both that function and
    :mod:`valleespyr.render.terrain_precompute` so the two stay in sync.

    With ``basemap=True`` an ``"basemap"`` key is added: the IGN Plan raster
    (:func:`valleespyr.render.basemap.fetch_basemap_rgb`) resampled onto the
    same grid the heightmap uses, as a JPEG data URI - or omitted if the WMTS
    fetch fails (network hiccup, no coverage), so a caller should treat it as
    optional. Needs network access at bake time; the DEM/streams/contours
    stay fully offline either way.
    """
    band, meta = clip_dem(catchment_polygon, dem_tif, pad_cells, z_exaggeration)
    heightmap = heightmap_png(band, meta)
    streams = streams_polylines(meta, streams_fc)
    minor, index = contour_polylines(band, meta)
    logger.info(
        "[dem] clipped %sx%s, %s valid cells, z %.0f-%.0f m",
        meta["rows"], meta["cols"], meta["n_valid"], meta["z_min"], meta["z_max"],
    )
    logger.info("[streams] %s polylines", len(streams))
    logger.info(
        "[contours] %s m: %s minor + %s index polylines",
        CONTOUR_INTERVAL_M, len(minor), len(index),
    )
    payload = {
        "meta": meta,
        "heightmap": heightmap,
        "streams": streams,
        "contours_minor": flatten_polylines(minor),
        "contours_index": flatten_polylines(index),
    }
    if basemap:
        basemap_uri = _basemap_jpeg(meta["bounds"], meta["rows"], meta["cols"], basemap_tile_dir)
        if basemap_uri:
            payload["basemap"] = basemap_uri
            logger.info("[basemap] baked (%.0f KB)", len(basemap_uri) / 1024)
        else:
            logger.info("[basemap] unavailable, terrain falls back to hypsometric tint")
    return payload
