"""Export a catchment's DEM to a plain array + JSON that Blender can read.

``render_valley.py`` runs inside Blender's own Python — it has ``bpy`` and a
bundled ``numpy``, but **no** rasterio / geopandas / shapely. So the geo work
happens here, in the project env, and the result is dropped into
``data/valleys/<slug>/derived/`` as two dependency-free files:

* ``terrain.npy`` — a ``float32`` array, shape ``(rows, cols)``, elevation in
  metres, ``NaN`` for cells outside the drainage divide. Row 0 is the north
  edge (rasterio raster order).
* ``terrain.json`` — everything needed to place that grid in real-world metres:
  geographic bounds, cell size, metres-per-degree at this latitude, the clipped
  and full-tile z-ranges, and the valid-cell count.

This mirrors ``diorama._clip_dem`` (same masking, same "keep the largest
4-connected blob" cleanup) but writes files instead of an HTML data URI.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import rasterio
from rasterio.mask import mask as rio_mask

from .diorama import _keep_largest_component

logger = logging.getLogger(__name__)


def export_terrain(
    catchment_polygon,
    dem_tif: Path,
    out_dir: Path,
    *,
    pad_cells: int = 6,
) -> tuple[Path, Path]:
    """Clip ``dem_tif`` to ``catchment_polygon`` and write ``terrain.{npy,json}``.

    ``catchment_polygon`` is a shapely geometry in the DEM's CRS (WGS84 for the
    OpenTopography tiles). ``pad_cells`` grows the mask outward so the diorama
    block keeps a thin apron of terrain around the divide rather than a knife
    edge — keep it in sync with the diorama's own ``pad_cells``.

    Returns ``(npy_path, json_path)``.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    with rasterio.open(dem_tif) as src:
        buffered = catchment_polygon.buffer(pad_cells * src.res[0])
        arr, transform = rio_mask(src, [buffered], crop=True, filled=True, nodata=np.nan)
        band = arr[0].astype("float64")
        full = src.read(1).astype("float64")
        crs = src.crs

    rows, cols = band.shape
    west = transform.c
    north = transform.f
    dx = transform.a
    dy = -transform.e  # positive
    east = west + cols * dx
    south = north - rows * dy

    band = _keep_largest_component(band)
    valid = np.isfinite(band)
    if not valid.any():
        raise RuntimeError("clipped DEM has no valid cells — check the catchment polygon")

    meta = {
        "rows": rows,
        "cols": cols,
        "crs": str(crs),
        "bounds": {"west": west, "south": south, "east": east, "north": north},
        "cell_deg": dx,
        "m_per_deg_lon": 111320.0 * float(np.cos(np.radians((north + south) / 2))),
        "m_per_deg_lat": 110540.0,
        "z_min": float(np.nanmin(band)),
        "z_max": float(np.nanmax(band)),
        "z_min_tile": float(full.min()),
        "z_max_tile": float(full.max()),
        "n_valid": int(valid.sum()),
    }

    npy_path = out_dir / "terrain.npy"
    json_path = out_dir / "terrain.json"
    np.save(npy_path, band.astype("float32"))
    json_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    logger.info(
        "wrote %s (%d x %d, %d valid, z %.0f-%.0f m) and %s",
        npy_path,
        rows,
        cols,
        meta["n_valid"],
        meta["z_min"],
        meta["z_max"],
        json_path,
    )
    return npy_path, json_path


def export_streams(streams_fc: dict, meta: dict, out_dir: Path) -> Path:
    """Write the catchment stream network as normalised-grid polylines.

    ``streams.json`` is ``{"lines": [[[u, v], ...], ...]}`` with ``u`` west→east
    and ``v`` south→north, both in ``[0, 1]`` over ``meta["bounds"]`` — the same
    space ``render_valley.py`` drapes onto the mesh. Densified to ~1 grid cell so
    long segments still follow the surface. ``meta`` is the dict from
    :func:`export_terrain`.
    """
    from shapely.geometry import shape

    b = meta["bounds"]
    du = b["east"] - b["west"]
    dv = b["north"] - b["south"]
    step = meta["cell_deg"]

    lines: list[list[list[float]]] = []
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
                seg = float(np.hypot(lon1 - lon0, lat1 - lat0))
                n = max(1, int(seg / step))
                for k in range(n):
                    f = k / n
                    dense.append((lon0 + f * (lon1 - lon0), lat0 + f * (lat1 - lat0)))
            dense.append(raw[-1])

            pts = []
            for lon, lat in dense:
                u = (lon - b["west"]) / du
                v = (lat - b["south"]) / dv
                if -0.05 <= u <= 1.05 and -0.05 <= v <= 1.05:
                    pts.append([round(u, 5), round(v, 5)])
            if len(pts) >= 2:
                lines.append(pts)

    out = out_dir / "streams.json"
    out.write_text(json.dumps({"lines": lines}), encoding="utf-8")
    logger.info("wrote %s (%d polylines)", out, len(lines))
    return out


__all__ = ["export_terrain", "export_streams"]
