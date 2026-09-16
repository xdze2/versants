"""Batch-bake per-river 3D terrain for the catalog's 3D view.

The catalog's 2D map draws every river as a line, cheap enough to ship for
the whole tree in one JSON payload. Terrain is not: each river's heightmap +
draped streams + contours (:func:`~valleespyr.render.terrain_data.build_terrain_payload`)
comes from clipping a DEM tile, and even encoded compactly runs tens to
hundreds of KB per river. Baking that for a ~100-river catalog ahead of time,
as small per-river files loaded lazily by the page, is the only way the 3D
view stays responsive - see :mod:`valleespyr.render.catalog_html`.

:func:`precompute_terrain` walks every river that has a catchment polygon
(the same set :func:`valleespyr.catalog.build_catalog` would mask the 2D map
with) and, for each, re-delineates it from the DEM
(:func:`valleespyr.valley.delineate_river_search` - the pour-point search,
not the plain snap, since a straight :func:`~valleespyr.valley.delineate_river`
can lock onto a much bigger parent basin right at a confluence) and writes
``<out_dir>/<river_id>.json``. Safe to re-run: a river whose output file
already exists is skipped, so an interrupted batch resumes.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def precompute_terrain(
    rn,                              # valleespyr.hydro.rivers.RiverNetwork
    river_ids: list[str],
    out_dir: Path,
    *,
    dem_dir: Path | None = None,
    demtype: str = "COP30",
    dem_source: str = "s3",
    z_exaggeration: float = 1.0,
    pad_cells: int = 6,
    basemap: bool = False,
    basemap_tile_dir: Path | None = None,
) -> dict[str, Any]:
    """Bake terrain for each of ``river_ids`` into ``<out_dir>/<id>.json``.

    Returns a summary dict ``{"done": [...], "skipped": [...], "failed":
    {id: reason}}``. ``"skipped"`` covers both rivers whose output already
    exists (resume) and ones the pour-point search couldn't confidently
    delineate (recorded, not retried until ``out_dir`` is cleared).
    """
    from . import terrain_data
    from .. import valley as valley_mod

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    done: list[str] = []
    skipped: list[str] = []
    failed: dict[str, str] = {}

    for river_id in river_ids:
        out_path = out_dir / f"{river_id}.json"
        if out_path.exists():
            skipped.append(river_id)
            continue

        river = rn.rivers.get(river_id)
        if river is None:
            failed[river_id] = "unknown river id"
            continue

        try:
            result = valley_mod.delineate_river_search(
                rn, river_id, dem_dir=dem_dir, demtype=demtype, dem_source=dem_source,
            )
        except Exception as exc:  # noqa: BLE001 - one bad river shouldn't kill the batch
            logger.warning("[%s] delineation crashed: %s", river_id, exc)
            failed[river_id] = str(exc)
            continue

        if result is None:
            logger.info("[%s] undetermined pour point, skipping", river_id)
            skipped.append(river_id)
            continue

        poly, diag = result
        streams_fc = rn.river_catchment_geojson(river_id)
        payload = terrain_data.build_terrain_payload(
            poly, Path(diag["dem_tif"]), streams_fc,
            z_exaggeration=z_exaggeration, pad_cells=pad_cells,
            basemap=basemap, basemap_tile_dir=basemap_tile_dir,
        )
        payload["outlet_lonlat"] = list(diag["outlet_lonlat"])
        payload["title"] = river.name or river.id

        out_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        size_kb = out_path.stat().st_size / 1024
        logger.info("[%s] wrote %s (%.0f KB)", river_id, out_path.name, size_kb)
        done.append(river_id)

    return {"done": done, "skipped": skipped, "failed": failed}
