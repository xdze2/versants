"""Delineate a real catchment polygon for any river from a DEM.

``bassin_versant_topographique`` only covers a fraction of named rivers and keys
its sub-basins to whole watercourses, so it cannot hand us a catchment for an
arbitrary pour point (see ``todo.md``). This module builds one from terrain
instead:

1. :func:`fetch_dem` downloads a Copernicus GLO-30 GeoTIFF for a bbox from the
   OpenTopography REST API and caches the tile on disk.
2. :func:`delineate` conditions the DEM (fill pits, fill depressions, resolve
   flats), routes D8 flow direction and accumulation, snaps the pour point onto
   the DEM channel network, walks the flow grid back from it with
   ``grid.catchment``, vectorises the result and measures its area in EPSG:2154.

Needs the ``dem`` extra installed (``pip install 'valleespyr[dem]'`` or
``uv sync --extra dem``) and a free ``OPENTOPOGRAPHY_API_KEY`` environment
variable, obtainable at https://portal.opentopography.org/.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

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
    ``OPENTOPOGRAPHY_API_KEY`` environment variable.
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

    api_key = os.environ.get("OPENTOPOGRAPHY_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENTOPOGRAPHY_API_KEY is not set. Get a free key at "
            "https://portal.opentopography.org/ and re-run:\n"
            "  OPENTOPOGRAPHY_API_KEY=xxxx uv run python scratchpad/dem_lutour.py"
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
        resp.raise_for_status()
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


def delineate(
    tif: Path,
    outlet_lon: float,
    outlet_lat: float,
    *,
    acc_channel_cells: int = ACC_CHANNEL_CELLS,
) -> tuple[BaseGeometry, dict]:
    """Delineate the catchment draining to ``(outlet_lon, outlet_lat)``.

    ``tif`` is a DEM GeoTIFF (as written by :func:`fetch_dem`). Returns
    ``(polygon, diagnostics)`` where ``polygon`` is a shapely geometry in
    EPSG:4326 and ``diagnostics`` is a ``dict`` with keys ``area_km2``,
    ``snap_moved_cells``, ``snap_xy``, ``depression_fill_frac`` and ``n_cells``.

    ``acc_channel_cells`` is the flow-accumulation threshold (in cells) above
    which a cell counts as a channel for the pour-point snap.
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

    # 3. snap the pour point ---------------------------------------------
    channel = acc > acc_channel_cells
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
    grid.clip_to(catch)
    mask = grid.view(catch, dtype=np.uint8)
    shapes = features.shapes(mask, mask=mask.astype(bool), transform=grid.affine)
    polys = [shape(geom) for geom, val in shapes if val == 1]
    if not polys:
        raise RuntimeError("[delineate] empty catchment - the snap landed off the network")
    poly = max(polys, key=lambda p: p.area)  # largest connected component
    poly = poly.simplify(grid.affine.a / 2)  # ~half a cell, cosmetic only

    # area via an equal-area reprojection of the mask, not degrees^2
    with rasterio.open(tif) as src:
        crs = src.crs
    gs = gpd.GeoSeries([poly], crs=crs).to_crs(2154)  # Lambert-93, metres
    area_km2 = float(gs.area.iloc[0] / 1e6)

    diag = {
        "area_km2": area_km2,
        "snap_moved_cells": moved_cells,
        "snap_xy": (x_snap, y_snap),
        "depression_fill_frac": frac,
        "n_cells": int(mask.sum()),
    }
    return gs.to_crs(4326).iloc[0], diag
