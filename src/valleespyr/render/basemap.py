"""Fetch and bake an IGN basemap texture for a catchment's terrain block.

The catalog's 2D map (:mod:`valleespyr.render.static.catalog_map`) already
draws IGN's key-free Plan IGN as a live Leaflet WMTS layer - fine there since
a browser only ever fetches the handful of tiles the current view needs. The
3D terrain block has no such thing: it wants one texture image draped over
its own heightmap grid, and doing that live (per pageview, in the browser)
would mean implementing the whole WMTS-tile-to-mesh-UV pipeline in
JavaScript. Baking it once at precompute time - the same offline-first shape
as :mod:`valleespyr.render.terrain_data` - is simpler and reuses this
project's existing rasterio/requests conventions
(:func:`valleespyr.hydro.dem.fetch_dem_s3`'s per-cell tile cache is the model
followed here).

:func:`fetch_basemap_rgb` is the entry point: given the same ``(rows, cols,
bounds)`` grid :func:`~valleespyr.render.terrain_data.clip_dem` produces, it
fetches the IGN WMTS tiles covering that bbox (cached individually under
``tile_dir``, shared across nearby catchments the way DEM tiles are),
mosaics them in Web Mercator, and warps the result onto the exact same grid
the heightmap uses - so the two line up pixel-for-pixel with no separate UV
math on the client.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

import numpy as np
import requests

logger = logging.getLogger(__name__)

# Key-free Plan IGN WMTS, standard Web Mercator (EPSG:3857) tile grid - same
# layer/style the 2D Leaflet map uses (see catalog_map.js).
_WMTS_URL = (
    "https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0"
    "&LAYER=GEOGRAPHICALGRIDSYSTEMS.PLANIGNV2&STYLE=normal&FORMAT=image/png"
    "&TILEMATRIXSET=PM&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}"
)
_TILE_PX = 256
_MAX_TILES = 64  # refuse a bbox/zoom combination that would fetch more than this


def _lonlat_to_tile(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    """Fractional (x, y) tile coordinates for a lon/lat at ``zoom`` (standard XYZ)."""
    n = 2 ** zoom
    x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def _pick_zoom(bbox: tuple[float, float, float, float], target_px: int) -> int:
    """Highest zoom whose tile grid covering ``bbox`` needs <= ``_MAX_TILES`` tiles."""
    west, south, east, north = bbox
    for zoom in range(16, 4, -1):
        x0, y0 = _lonlat_to_tile(west, north, zoom)
        x1, y1 = _lonlat_to_tile(east, south, zoom)
        cols = math.floor(x1) - math.floor(x0) + 1
        rows = math.floor(y1) - math.floor(y0) + 1
        if cols * rows <= _MAX_TILES:
            # good enough once tiles comfortably exceed the requested pixel size
            if cols * _TILE_PX >= target_px or zoom <= 6:
                return zoom
    return 6


def _fetch_tile(zoom: int, x: int, y: int, tile_dir: Path) -> Path:
    dest = tile_dir / f"ign_plan_{zoom}_{x}_{y}.png"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = _WMTS_URL.format(z=zoom, x=x, y=y)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    return dest


def fetch_basemap_rgb(
    bounds: dict,          # {"west", "south", "east", "north"} - same shape as terrain_data's meta["bounds"]
    rows: int,
    cols: int,
    *,
    tile_dir: Path | None = None,
) -> np.ndarray | None:
    """IGN Plan basemap resampled onto a ``(rows, cols, 3)`` uint8 grid matching ``bounds``.

    The grid is oriented the same way the heightmap is: row 0 = north, column
    0 = west - so a client can sample it with the same ``(u, v)`` it already
    uses for elevation, no separate transform needed. Returns ``None`` (with
    a warning logged) if the WMTS fetch fails or the bbox would need an
    unreasonable number of tiles - the caller should fall back to no basemap
    rather than fail the whole precompute run over one river.
    """
    from PIL import Image
    from rasterio.transform import from_bounds as transform_from_bounds
    from rasterio.warp import Resampling, reproject

    tile_dir = Path(tile_dir) if tile_dir else Path("data/raw/basemap_tiles")
    west, south, east, north = bounds["west"], bounds["south"], bounds["east"], bounds["north"]

    try:
        zoom = _pick_zoom((west, south, east, north), max(rows, cols))
        x0f, y0f = _lonlat_to_tile(west, north, zoom)
        x1f, y1f = _lonlat_to_tile(east, south, zoom)
        xs = range(math.floor(x0f), math.floor(x1f) + 1)
        ys = range(math.floor(y0f), math.floor(y1f) + 1)
        if len(xs) * len(ys) > _MAX_TILES:
            logger.warning("[basemap] bbox needs too many tiles at zoom %s, skipping", zoom)
            return None

        mosaic = Image.new("RGB", (len(xs) * _TILE_PX, len(ys) * _TILE_PX))
        for j, y in enumerate(ys):
            for i, x in enumerate(xs):
                path = _fetch_tile(zoom, x, y, tile_dir)
                tile_img = Image.open(path).convert("RGB")
                mosaic.paste(tile_img, (i * _TILE_PX, j * _TILE_PX))

        # Web Mercator bounds of the fetched tile block (not `bounds` itself -
        # the tile grid always over-covers it a little)
        n = 2 ** zoom
        merc_west = xs.start / n * 2 * math.pi - math.pi
        merc_east = (xs.stop) / n * 2 * math.pi - math.pi
        def _merc_y(tile_y: float) -> float:
            return math.pi - tile_y / n * 2 * math.pi
        merc_north = _merc_y(ys.start)
        merc_south = _merc_y(ys.stop)
        R = 6378137.0  # Web Mercator sphere radius
        src_transform = transform_from_bounds(
            merc_west * R, merc_south * R, merc_east * R, merc_north * R,
            mosaic.width, mosaic.height,
        )

        mosaic_arr = np.array(mosaic)  # (H, W, 3)
        dst = np.zeros((rows, cols, 3), dtype=np.uint8)
        dst_transform = transform_from_bounds(west, south, east, north, cols, rows)
        for band in range(3):
            reproject(
                source=mosaic_arr[..., band],
                destination=dst[..., band],
                src_transform=src_transform, src_crs="EPSG:3857",
                dst_transform=dst_transform, dst_crs="EPSG:4326",
                resampling=Resampling.bilinear,
            )
        return dst
    except Exception as exc:  # noqa: BLE001 - a missing basemap shouldn't fail the batch
        logger.warning("[basemap] fetch/warp failed: %s", exc)
        return None
