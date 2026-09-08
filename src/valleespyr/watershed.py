"""Fetch and select topographic watersheds (bassins versants) from a WFS source.

Two ways in:

* **live** — :func:`fetch_watersheds_bbox` / :func:`fetch_watershed_by_point` pull
  ``bassin_versant_topographique`` polygons straight off the WFS;
* **offline** — :func:`load_bassins` reads a local dump (``valleespyr wfs dump
  --layer BDTOPO_V3:bassin_versant_topographique``) and :func:`catchment_polygon`
  dissolves the sub-basins that belong to a set of ``cours_d_eau`` ids into one
  catchment-area polygon. That is the real drainage area — one BD Carthage
  sub-basin per watercourse reach — as opposed to a hull drawn around the stream
  lines, which is not a catchment at all.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .sources.wfs import LAYER_BASSIN_VERSANT, WFSClient

if TYPE_CHECKING:  # pragma: no cover - typing only
    import geopandas as gpd
    from shapely.geometry.base import BaseGeometry

# Each polygon links to exactly one ``cours_d_eau`` (its principal watercourse).
BASSIN_COURS_D_EAU = "liens_vers_cours_d_eau_principal"


def list_watershed_layers(client: WFSClient) -> list[Any]:
    """Feature types on the endpoint whose name/title mentions a watershed."""
    hits = client.list_feature_types(keyword="bassin")
    hits += [ft for ft in client.list_feature_types(keyword="versant") if ft not in hits]
    return hits


def describe_watershed_schema(
    client: WFSClient, layer: str = LAYER_BASSIN_VERSANT
) -> list[tuple[str, str]]:
    return client.describe_feature_type(layer)


def fetch_watersheds_bbox(
    client: WFSClient,
    bbox: tuple[float, float, float, float],
    *,
    layer: str = LAYER_BASSIN_VERSANT,
    bbox_crs: str = "EPSG:4326",
    srs_name: str = "EPSG:4326",
    max_features: int | None = None,
) -> dict[str, Any]:
    """Return a GeoJSON FeatureCollection of watersheds intersecting ``bbox``."""
    feats = list(
        client.iter_features(
            layer,
            bbox=bbox,
            bbox_crs=bbox_crs,
            srs_name=srs_name,
            max_features=max_features,
        )
    )
    return {"type": "FeatureCollection", "features": feats}


def fetch_watershed_by_point(
    client: WFSClient,
    lon: float,
    lat: float,
    *,
    layer: str = LAYER_BASSIN_VERSANT,
    srs_name: str = "EPSG:4326",
    geom_field: str = "geometrie",
) -> dict[str, Any]:
    """Return the watershed polygon(s) containing a point given as lon/lat (WGS84).

    The CQL point is always sent in EPSG:4326 authority axis order (lat lon),
    which is how the Géoplateforme interprets CQL geometry literals, regardless
    of the ``srs_name`` requested for the returned features.
    """
    cql = f"INTERSECTS({geom_field}, POINT({lat} {lon}))"
    return client.get_feature(layer, cql_filter=cql, srs_name=srs_name)


# ---------------------------------------------------------------- offline / dissolve


def load_bassins(path: str | Path) -> gpd.GeoDataFrame:
    """Read a local dump of the ``bassin_versant_topographique`` layer (lon, lat WGS84).

    Mirrors :func:`valleespyr.hydro.network.load_troncons`: accepts GeoParquet or
    any OGR vector format, forces WGS84, repairs invalid rings so a later
    ``unary_union`` can't blow up.
    """
    import geopandas as gpd
    from shapely.validation import make_valid

    path = Path(path)
    gdf = gpd.read_parquet(path) if path.suffix == ".parquet" else gpd.read_file(path)
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    gdf = gdf.to_crs("EPSG:4326")
    bad = ~gdf.geometry.is_valid
    if bad.any():
        gdf.loc[bad, "geometry"] = gdf.loc[bad, "geometry"].apply(make_valid)
    return gdf.reset_index(drop=True)


def catchment_polygon(
    bassins: gpd.GeoDataFrame,
    cours_d_eau_ids: Iterable[str],
    *,
    key: str = BASSIN_COURS_D_EAU,
) -> BaseGeometry | None:
    """Dissolve every sub-basin whose principal watercourse is in ``cours_d_eau_ids``.

    ``cours_d_eau_ids`` is a river plus everything upstream of it in the river
    graph (``{river.id} | {r.id for r in rn.upstream_rivers(river.id)}``); the
    ``bassin_versant_topographique`` polygons keyed to those ids already tile the
    drainage area, so their union *is* the catchment. Returns ``None`` when none
    of the ids has a polygon in this dump (e.g. a Spanish-side or headwater river
    the BD Carthage layer doesn't cover).

    **Scope.** A sub-basin is keyed to a *whole* ``cours_d_eau``, so this is the
    catchment of every watercourse in the set *over its full length* — including
    reaches downstream of whatever bbox the tronçon dump was clipped to. That is
    exactly right for a river whose outlet sits inside the dump (a root of the
    river graph). For a river the dump cuts off mid-course it will also pull in
    that river's downstream basin; dump a bbox that contains the outlet to avoid
    the over-reach.
    """
    from shapely.ops import unary_union

    wanted = {i for i in cours_d_eau_ids if i}
    if not wanted:
        return None
    hit = bassins[bassins[key].isin(wanted)]
    if hit.empty:
        return None
    merged = unary_union(hit.geometry.values)
    return merged if not merged.is_empty else None
