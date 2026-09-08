"""Bulk-download a whole WFS layer selection to a local file.

BD TOPO's watershed layer is small (~6.6k features for all of metropolitan
France), so paging the WFS is a perfectly good bulk source — no per-department
7z archives needed. Output is GeoJSON (no extra deps) or GeoParquet (needs
geopandas + pyarrow, from the ``[dem]``/geopandas extra).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from .sources.wfs import WFSClient

ProgressFn = Callable[[int, int | None], None]


def _iter_with_progress(
    it: Iterator[dict[str, Any]],
    total: int | None,
    progress: ProgressFn | None,
) -> Iterator[dict[str, Any]]:
    n = 0
    for feat in it:
        n += 1
        if progress is not None and (n % 500 == 0 or n == total):
            progress(n, total)
        yield feat
    if progress is not None and (total is None or n != total):
        progress(n, total)


def dump_layer(
    client: WFSClient,
    layer: str,
    out_path: str | Path,
    *,
    bbox: tuple[float, float, float, float] | None = None,
    bbox_crs: str = "EPSG:4326",
    cql_filter: str | None = None,
    srs_name: str = "EPSG:4326",
    page_size: int = 1000,
    max_features: int | None = None,
    progress: ProgressFn | None = None,
) -> int:
    """Page through ``layer`` and write every matching feature to ``out_path``.

    Format is chosen from the suffix: ``.parquet``/``.gpq`` -> GeoParquet,
    ``.gpkg`` -> GeoPackage, anything else -> GeoJSON. Returns the feature count.
    """
    out_path = Path(out_path)
    suffix = out_path.suffix.lower()

    try:
        total: int | None = client.count_features(
            layer, bbox=bbox, bbox_crs=bbox_crs, cql_filter=cql_filter
        )
    except Exception:  # noqa: BLE001 - count is best-effort, don't fail the dump
        total = None

    feats_iter = client.iter_features(
        layer,
        page_size=page_size,
        max_features=max_features,
        bbox=bbox,
        bbox_crs=bbox_crs,
        cql_filter=cql_filter,
        srs_name=srs_name,
    )
    feats_iter = _iter_with_progress(feats_iter, max_features or total, progress)

    if suffix in {".parquet", ".gpq", ".gpkg"}:
        return _write_geo(feats_iter, out_path, suffix, srs_name)
    return _write_geojson(feats_iter, out_path)


def _write_geojson(feats: Iterator[dict[str, Any]], out_path: Path) -> int:
    """Stream features into a GeoJSON FeatureCollection without holding all in memory."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_path.open("w", encoding="utf-8") as fh:
        fh.write('{"type":"FeatureCollection","features":[\n')
        first = True
        for feat in feats:
            fh.write("" if first else ",\n")
            json.dump(feat, fh, ensure_ascii=False)
            first = False
            n += 1
        fh.write("\n]}\n")
    return n


def _write_geo(
    feats: Iterator[dict[str, Any]], out_path: Path, suffix: str, srs_name: str
) -> int:
    try:
        import geopandas as gpd
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            f"Writing {suffix} needs geopandas: pip install 'valleespyr[dem]' "
            "(or: pip install geopandas pyarrow)"
        ) from exc

    features = list(feats)
    if not features:
        raise RuntimeError("no features matched — nothing written")

    gdf = gpd.GeoDataFrame.from_features(features, crs=srs_name)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if suffix == ".gpkg":
        gdf.to_file(out_path, driver="GPKG")
    else:
        gdf.to_parquet(out_path)
    return len(gdf)
