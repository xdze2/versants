"""Fetch and select topographic watersheds (bassins versants) from a WFS source."""

from __future__ import annotations

from typing import Any

from .sources.wfs import LAYER_BASSIN_VERSANT, WFSClient


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
