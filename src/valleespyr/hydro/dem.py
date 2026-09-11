"""Delineate a real catchment polygon for any river from a DEM.

``bassin_versant_topographique`` only covers a fraction of named rivers and keys
its sub-basins to whole watercourses, so it cannot hand us a catchment for an
arbitrary pour point (see ``todo.md``). This module builds one from terrain
instead:

1. :func:`fetch_dem` downloads a Copernicus GLO-30 GeoTIFF for a bbox from the
   OpenTopography REST API and caches the tile on disk.
2. :func:`condition` conditions the DEM (fill pits, fill depressions, resolve
   flats) and routes D8 flow direction and accumulation for the whole tile —
   the expensive part, but it depends only on the tile, not on any one river.
3. :func:`trace` snaps one pour point onto the already-conditioned channel
   network, walks the flow grid back from it with ``grid.catchment``,
   vectorises the result and measures its area in EPSG:2154 — cheap, and
   specific to one outlet coordinate.
4. :func:`delineate` is the thin `condition` + `trace` combination for the
   common case of one river per tile.

The condition/trace split exists so that a future batch step could condition a
DEM tile once and :func:`trace` several rivers that share it, instead of
re-running the expensive conditioning per river — that clustering is not
implemented yet, but the split makes it possible without reshaping this module
again.

Needs the ``dem`` extra installed (``pip install 'valleespyr[dem]'`` or
``uv sync --extra dem``) and a free OpenTopography API key, obtainable at
https://portal.opentopography.org/ — set ``OPENTOPOGRAPHY_API_KEY``, or write
the key (nothing else) to a ``key.secret`` file at the repo root.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import geopandas as gpd
import numpy as np
import rasterio
import requests
from rasterio import features
from shapely.geometry import shape

if TYPE_CHECKING:  # pragma: no cover - typing only
    from shapely.geometry.base import BaseGeometry

# pysheds 0.5 (latest release) still calls np.in1d, removed in NumPy 2.0. It is
# an exact alias of np.isin for the 1-D flow-direction arrays it is used on.
# Restore it before importing pysheds anywhere below.
if not hasattr(np, "in1d"):
    np.in1d = np.isin  # type: ignore[attr-defined]

logger = logging.getLogger(__name__)

# Accumulation threshold (in cells) above which a cell counts as "channel".
# COP30 is ~30 m; ~1000 cells ~= 0.9 km2 of contributing area, a sane channel
# head for a Pyrenean torrent. The pour-point snap is the sensitive part, not
# this number - it just has to put *some* channel near the outlet.
ACC_CHANNEL_CELLS = 1000

# How far (in DEM cells) we allow the outlet to be snapped onto the channel
# before we warn that the DEM channel and the vector outlet disagree.
SNAP_RADIUS_CELLS = 8

# Minimum channel cells (``acc > acc_channel_cells``) a tile must have before
# trace() will call grid.catchment() at all. Below this, pysheds 0.5 has been
# observed to corrupt memory ("double free or corruption", not a catchable
# Python exception) rather than raise cleanly — see trace()'s guard. A working
# small-river tile easily clears this by two orders of magnitude (664 cells
# observed); the crashing cases had 0-1.
MIN_CHANNEL_CELLS = 5

# Repo-root fallback for the OpenTopography key when OPENTOPOGRAPHY_API_KEY
# isn't set in the environment — a single-line, gitignored (*.secret) file.
_KEY_FILE = Path(__file__).resolve().parents[3] / "key.secret"


def _opentopography_api_key() -> str | None:
    """The OpenTopography API key: ``OPENTOPOGRAPHY_API_KEY`` env var, else
    ``key.secret`` at the repo root, else ``None``."""
    key = os.environ.get("OPENTOPOGRAPHY_API_KEY")
    if key:
        return key
    try:
        key = _KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return key or None


def fetch_dem(
    bbox: tuple[float, float, float, float],
    dest: Path | None = None,
    *,
    demtype: str = "COP30",
) -> Path:
    """Download a global DEM GeoTIFF for ``bbox`` from OpenTopography.

    ``bbox`` is ``(west, south, east, north)`` in WGS84. Cached: if ``dest``
    exists and is non-empty it is returned untouched. When ``dest`` is ``None``
    the tile is written to
    ``data/raw/dem/<demtype>_<west>_<south>_<east>_<north>.tif`` (coordinates
    rounded to 4 decimals), creating the directory. Needs a free
    OpenTopography API key, from ``OPENTOPOGRAPHY_API_KEY`` or, failing that,
    a ``key.secret`` file at the repo root (see :func:`_opentopography_api_key`).
    """
    west, south, east, north = bbox
    if dest is None:
        name = (
            f"{demtype}_{round(west, 4)}_{round(south, 4)}_"
            f"{round(east, 4)}_{round(north, 4)}.tif"
        )
        dest = Path("data/raw/dem") / name
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists() and dest.stat().st_size > 0:
        logger.info("cached %s (%.1f MB)", dest, dest.stat().st_size / 1e6)
        return dest

    api_key = _opentopography_api_key()
    if not api_key:
        raise RuntimeError(
            "no OpenTopography API key found. Get a free key at "
            "https://portal.opentopography.org/ and either set "
            "OPENTOPOGRAPHY_API_KEY=xxxx or write it (nothing else) to "
            f"{_KEY_FILE}"
        )

    url = "https://portal.opentopography.org/API/globaldem"
    params = {
        "demtype": demtype,
        "west": west,
        "south": south,
        "east": east,
        "north": north,
        "outputFormat": "GTiff",
        "API_Key": api_key,
    }
    logger.info("fetching %s %s -> %s", demtype, bbox, dest)
    with requests.get(url, params=params, stream=True, timeout=120) as resp:
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            # stream=True means the body isn't read yet; grab it now, while the
            # connection is still open, and fold it into the exception message
            # (a caller catching this after fetch_dem has returned would only
            # see an already-closed response with an empty .text otherwise —
            # e.g. OpenTopography's "API maximum rate limit reached" comes
            # back as a 401 with the real reason only in this body).
            body = resp.text[:500]
            raise requests.exceptions.HTTPError(
                f"{exc} — response body: {body}", response=resp
            ) from exc
        ctype = resp.headers.get("Content-Type", "")
        if "tiff" not in ctype and "octet-stream" not in ctype:
            raise RuntimeError(f"[fetch] unexpected response ({ctype}): {resp.text[:500]}")
        tmp = dest.with_suffix(".tmp")
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                fh.write(chunk)
        tmp.replace(dest)
    logger.info("wrote %s (%.1f MB)", dest, dest.stat().st_size / 1e6)
    return dest


# --------------------------------------------------------- Copernicus DEM via S3
#
# The Copernicus GLO-30 DEM is also published as public, unauthenticated tiles
# on AWS S3 (arn:aws:s3:::copernicus-dem-30m; see
# https://registry.opendata.aws/copernicus-dem/) — one Cloud-Optimized GeoTIFF
# per 1x1 degree WGS84 grid cell, no API key and no rate limit, unlike
# fetch_dem()'s OpenTopography path (a free-tier account there caps at 50
# downloads/24h, which a per-river fetch burns through fast — see
# `valleespyr valley catchments precompute`). A whole Pyrenean valley system's
# bbox typically spans only 1-4 of these tiles, cached forever once fetched.

_S3_TILE_URL = (
    "https://copernicus-dem-30m.s3.amazonaws.com/"
    "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM/"
    "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM.tif"
)


def _s3_tile_grid(bbox: tuple[float, float, float, float]) -> list[tuple[int, int]]:
    """Every ``(lat, lon)`` 1x1 degree grid cell (SW corner, WGS84) overlapping ``bbox``.

    The Copernicus DEM S3 layout tiles the globe on integer-degree boundaries,
    each file named after its bottom-left corner — e.g. the cell
    ``[0, 42) x [42, 43)`` is ``lat=42, lon=0``. ``bbox`` is
    ``(west, south, east, north)``.
    """
    import math

    west, south, east, north = bbox
    lats = range(math.floor(south), math.ceil(north))
    lons = range(math.floor(west), math.ceil(east))
    return [(lat, lon) for lat in lats for lon in lons]


def _s3_tile_url(lat: int, lon: int) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return _S3_TILE_URL.format(ns=ns, lat=abs(lat), ew=ew, lon=abs(lon))


def _fetch_s3_tile(lat: int, lon: int, dem_dir: Path) -> Path:
    """Download one 1x1 degree Copernicus DEM tile from S3, cached by its cell."""
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    dest = dem_dir / f"COP30_S3_{ns}{abs(lat):02d}_{ew}{abs(lon):03d}.tif"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        logger.info("cached %s (%.1f MB)", dest, dest.stat().st_size / 1e6)
        return dest

    url = _s3_tile_url(lat, lon)
    logger.info("fetching S3 tile %s -> %s", url, dest)
    with requests.get(url, stream=True, timeout=120) as resp:
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            body = resp.text[:500]
            raise requests.exceptions.HTTPError(
                f"{exc} (tile {ns}{abs(lat):02d}/{ew}{abs(lon):03d} not published "
                f"in GLO-30's public S3 subset?) — response body: {body}",
                response=resp,
            ) from exc
        tmp = dest.with_suffix(".tmp")
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
        tmp.replace(dest)
    logger.info("wrote %s (%.1f MB)", dest, dest.stat().st_size / 1e6)
    return dest


def fetch_dem_s3(
    bbox: tuple[float, float, float, float],
    dest: Path | None = None,
    *,
    dem_dir: Path | None = None,
) -> Path:
    """Assemble a DEM GeoTIFF for ``bbox`` from public Copernicus S3 tiles.

    ``bbox`` is ``(west, south, east, north)`` in WGS84. Downloads (and caches
    under ``dem_dir``, default ``data/raw/dem``) every 1x1 degree S3 tile
    overlapping ``bbox``, and — when more than one is needed — mosaics them
    with :func:`rasterio.merge.merge` into a single GeoTIFF cropped to
    ``bbox``, cached at ``dest`` (default:
    ``data/raw/dem/COP30_S3_<west>_<south>_<east>_<north>.tif``, coordinates
    rounded to 4 decimals). No API key, no rate limit — unlike :func:`fetch_dem`.
    """
    from rasterio.merge import merge
    from rasterio.windows import from_bounds

    dem_dir = Path(dem_dir) if dem_dir else Path("data/raw/dem")
    if dest is None:
        west, south, east, north = bbox
        name = (
            f"COP30_S3_{round(west, 4)}_{round(south, 4)}_"
            f"{round(east, 4)}_{round(north, 4)}.tif"
        )
        dest = dem_dir / name
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists() and dest.stat().st_size > 0:
        logger.info("cached %s (%.1f MB)", dest, dest.stat().st_size / 1e6)
        return dest

    cells = _s3_tile_grid(bbox)
    if not cells:
        raise RuntimeError(f"[fetch_dem_s3] empty tile grid for bbox {bbox!r}")
    tiles = [_fetch_s3_tile(lat, lon, dem_dir) for lat, lon in cells]

    if len(tiles) == 1:
        src_path = tiles[0]
        with rasterio.open(src_path) as src:
            window = from_bounds(*bbox, transform=src.transform)
            data = src.read(1, window=window)
            transform = src.window_transform(window)
            profile = src.profile
    else:
        srcs = [rasterio.open(t) for t in tiles]
        try:
            mosaic, transform = merge(srcs, bounds=bbox)
        finally:
            for s in srcs:
                s.close()
        data = mosaic[0]
        profile = rasterio.open(tiles[0]).profile

    profile.update(height=data.shape[0], width=data.shape[1], transform=transform, count=1)
    tmp = dest.with_suffix(".tmp")
    with rasterio.open(tmp, "w", **profile) as out:
        out.write(data, 1)
    tmp.replace(dest)
    logger.info(
        "assembled %s from %d S3 tile(s) (%.1f MB)",
        dest,
        len(tiles),
        dest.stat().st_size / 1e6,
    )
    return dest


@dataclass
class ConditionedGrid:
    """A DEM tile after pit-fill / depression-fill / resolve-flats / flow routing.

    Everything a river's :func:`trace` needs, minus the pour point: the pysheds
    ``Grid`` (carries the affine transform), its D8 flow direction and
    accumulation rasters, the tile's CRS, and ``depression_fill_frac`` (the
    fraction of cells raised by depression filling — computed once here,
    surfaced again in :func:`trace`'s diagnostics since it describes the tile,
    not any one catchment).
    """

    grid: Any
    fdir: Any
    acc: Any
    crs: Any
    depression_fill_frac: float


def condition(tif: Path) -> ConditionedGrid:
    """Condition a DEM tile and route D8 flow — the per-tile, shared-across-rivers step.

    ``tif`` is a DEM GeoTIFF (as written by :func:`fetch_dem`). Fills pits,
    fills depressions, resolves flats, then computes flow direction and flow
    accumulation for the whole tile. Logs the tile shape, cell size, elevation
    range, and warns when depression filling alters more than 5% of the tile
    (a sign the fill may be swallowing a real glacial cirque/tarn).
    """
    from pysheds.grid import Grid

    grid = Grid.from_raster(str(tif))
    dem = grid.read_raster(str(tif))
    logger.info(
        "dem shape %s, cell ~%.0f m, z %.0f-%.0f m",
        dem.shape,
        grid.affine.a * 111320,
        float(np.nanmin(dem)),
        float(np.nanmax(dem)),
    )

    # 1. condition ------------------------------------------------------------
    pit_filled = grid.fill_pits(dem)
    flooded = grid.fill_depressions(pit_filled)
    inflated = grid.resolve_flats(flooded)

    altered = int(np.sum(np.asarray(flooded) - np.asarray(dem) > 0.5))
    frac = altered / dem.size
    logger.info(
        "conditioning raised %d cells >0.5 m by depression fill (%.2f%% of tile)",
        altered,
        frac * 100,
    )
    if frac > 0.05:
        logger.warning(
            ">5%% of tile altered by depression fill - check glacial cirques, "
            "the fill may be swallowing real tarns"
        )

    # 2. flow direction + accumulation -------------------------------------
    fdir = grid.flowdir(inflated)
    acc = grid.accumulation(fdir)
    logger.info("D8 flow routing; max accumulation %d cells", int(np.asarray(acc).max()))

    with rasterio.open(tif) as src:
        crs = src.crs

    return ConditionedGrid(
        grid=grid, fdir=fdir, acc=acc, crs=crs, depression_fill_frac=frac
    )


def trace(
    conditioned: ConditionedGrid,
    outlet_lon: float,
    outlet_lat: float,
    *,
    acc_channel_cells: int = ACC_CHANNEL_CELLS,
) -> tuple[BaseGeometry, dict]:
    """Delineate the catchment draining to ``(outlet_lon, outlet_lat)``.

    ``conditioned`` is the result of :func:`condition` on the DEM tile that
    covers this pour point — cheap to call again for another pour point on the
    same tile, since none of the conditioning is repeated. Returns
    ``(polygon, diagnostics)`` where ``polygon`` is a shapely geometry in
    EPSG:4326 and ``diagnostics`` is a ``dict`` with keys ``area_km2``,
    ``snap_moved_cells``, ``snap_xy``, ``depression_fill_frac`` and ``n_cells``.

    ``acc_channel_cells`` is the flow-accumulation threshold (in cells) above
    which a cell counts as a channel for the pour-point snap.
    """
    grid = conditioned.grid
    fdir = conditioned.fdir
    acc = conditioned.acc

    # 3. snap the pour point ---------------------------------------------
    channel = acc > acc_channel_cells
    n_channel = int(channel.sum())
    if n_channel < MIN_CHANNEL_CELLS:
        # A tile this sparse in channel cells (e.g. a bbox so small its own
        # padded extent barely accumulates any flow) is exactly the condition
        # under which pysheds 0.5's grid.catchment() has been observed to
        # corrupt memory ("double free or corruption") *after* successfully
        # returning a result — not a Python exception anything here could
        # catch, since it happens during the C-level array's teardown. A tile
        # with 664 channel cells works fine; one with 0-1 does not. Refuse
        # before calling catchment() rather than gambling on a segfault.
        raise RuntimeError(
            f"[trace] only {n_channel} channel cell(s) above "
            f"acc_channel_cells={acc_channel_cells} in this tile - too sparse "
            "to safely delineate (lower acc_channel_cells, widen the bbox, or "
            "accept no catchment for this river)"
        )
    x_snap, y_snap = grid.snap_to_mask(channel, (outlet_lon, outlet_lat))
    dcol = abs(x_snap - outlet_lon) / grid.affine.a
    drow = abs(y_snap - outlet_lat) / grid.affine.e
    moved_cells = float(np.hypot(dcol, drow))
    logger.info(
        "snapped outlet (%.5f, %.5f) -> (%.5f, %.5f), moved %.1f cells",
        outlet_lon,
        outlet_lat,
        x_snap,
        y_snap,
        moved_cells,
    )
    if moved_cells > SNAP_RADIUS_CELLS:
        logger.warning(
            "snapped >%d cells - the DEM channel and the vector outlet disagree "
            "badly; lower acc_channel_cells or check the outlet coord",
            SNAP_RADIUS_CELLS,
        )

    # 4. delineate + vectorise -----------------------------------------
    catch = grid.catchment(x=x_snap, y=y_snap, fdir=fdir, xytype="coordinate")
    # Crop the catchment mask to its own non-null bbox via a *local* trimmed
    # viewfinder, not `grid.clip_to(catch)` — that mutates `grid.viewfinder` in
    # place, which would corrupt a second `trace()` call sharing this same
    # `conditioned.grid` (see the module docstring on why sharing one
    # conditioned grid across rivers is the point of this split).
    from pysheds.view import View

    trimmed = View.trim_zeros(catch, pad=(0, 0, 0, 0))
    mask = grid.view(catch, target_view=trimmed.viewfinder, dtype=np.uint8)
    shapes = features.shapes(mask, mask=mask.astype(bool), transform=trimmed.viewfinder.affine)
    polys = [shape(geom) for geom, val in shapes if val == 1]
    if not polys:
        raise RuntimeError("[delineate] empty catchment - the snap landed off the network")
    poly = max(polys, key=lambda p: p.area)  # largest connected component
    poly = poly.simplify(trimmed.viewfinder.affine.a / 2)  # ~half a cell, cosmetic only

    # area via an equal-area reprojection of the mask, not degrees^2
    gs = gpd.GeoSeries([poly], crs=conditioned.crs).to_crs(2154)  # Lambert-93, metres
    area_km2 = float(gs.area.iloc[0] / 1e6)

    diag = {
        "area_km2": area_km2,
        "snap_moved_cells": moved_cells,
        "snap_xy": (x_snap, y_snap),
        "depression_fill_frac": conditioned.depression_fill_frac,
        "n_cells": int(mask.sum()),
    }
    return gs.to_crs(4326).iloc[0], diag


def delineate(
    tif: Path,
    outlet_lon: float,
    outlet_lat: float,
    *,
    acc_channel_cells: int = ACC_CHANNEL_CELLS,
) -> tuple[BaseGeometry, dict]:
    """Condition ``tif`` and trace the catchment for one pour point.

    Thin wrapper around :func:`condition` + :func:`trace` for the common case
    of delineating a single river per DEM tile. See :func:`trace` for the
    return shape; see the module docstring for why the two steps are split.
    """
    return trace(condition(tif), outlet_lon, outlet_lat, acc_channel_cells=acc_channel_cells)
