"""Tests for the offline catchment-area helpers in :mod:`valleespyr.watershed`.

The unit tests build a tiny two-polygon ``bassin_versant_topographique`` frame so
the dissolve is obvious; one integration test dissolves the Gave de Pau
catchment from the saved Pyrénées sub-basin dump when it is present.
"""

from __future__ import annotations

from pathlib import Path

import pytest

gpd = pytest.importorskip("geopandas")
pytest.importorskip("shapely")

from shapely.geometry import Polygon  # noqa: E402

from valleespyr.watershed import (  # noqa: E402
    BASSIN_COURS_D_EAU,
    catchment_polygon,
    load_bassins,
)

BASSIN_SAMPLE = Path("data/raw/bassin_versant_topographique_pyrenees.parquet")
TRONCON_SAMPLE = Path("data/raw/troncon_hydrographique_gavarnie_sample.geojson")


def _bassins() -> gpd.GeoDataFrame:
    """Two abutting unit squares, one per ``cours_d_eau``."""
    return gpd.GeoDataFrame(
        {
            BASSIN_COURS_D_EAU: ["CDE_A", "CDE_B"],
            "toponyme": ["A", "B"],
            "geometry": [
                Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
                Polygon([(1, 0), (2, 0), (2, 1), (1, 1)]),
            ],
        },
        crs="EPSG:4326",
    )


def test_catchment_polygon_dissolves_the_matching_sub_basins():
    merged = catchment_polygon(_bassins(), {"CDE_A", "CDE_B"})
    assert merged is not None
    # the two unit squares merge into one 2x1 rectangle, no internal boundary
    assert merged.geom_type == "Polygon"
    assert merged.area == pytest.approx(2.0)
    assert merged.bounds == (0.0, 0.0, 2.0, 1.0)


def test_catchment_polygon_only_takes_requested_cours_d_eau():
    merged = catchment_polygon(_bassins(), {"CDE_A"})
    assert merged.area == pytest.approx(1.0)
    assert merged.bounds == (0.0, 0.0, 1.0, 1.0)


def test_catchment_polygon_none_when_nothing_matches():
    assert catchment_polygon(_bassins(), {"CDE_MISSING"}) is None
    assert catchment_polygon(_bassins(), set()) is None
    assert catchment_polygon(_bassins(), {None, ""}) is None  # falsy ids skipped


@pytest.mark.skipif(not BASSIN_SAMPLE.exists(), reason="bassin_versant sample not present")
def test_load_bassins_reads_the_dump_as_valid_wgs84_polygons():
    gdf = load_bassins(BASSIN_SAMPLE)
    assert len(gdf) > 100
    assert gdf.crs.to_epsg() == 4326
    assert gdf.geometry.is_valid.all()
    assert BASSIN_COURS_D_EAU in gdf.columns


@pytest.mark.skipif(
    not (BASSIN_SAMPLE.exists() and TRONCON_SAMPLE.exists()),
    reason="need both the tronçon and bassin_versant samples",
)
def test_gave_de_pau_catchment_dissolve_covers_the_upstream_network():
    from valleespyr.hydro.network import build_graph, load_troncons
    from valleespyr.hydro.rivers import build_river_network

    rn = build_river_network(build_graph(load_troncons(TRONCON_SAMPLE)))
    pau = rn.by_name("Gave de Pau", exact=True)[0]
    ids = {pau.id} | {r.id for r in rn.upstream_rivers(pau.id)}

    bassins = load_bassins(BASSIN_SAMPLE)
    poly = catchment_polygon(bassins, ids)
    assert poly is not None

    area_km2 = gpd.GeoSeries([poly], crs="EPSG:4326").to_crs("EPSG:2154").area.iloc[0] / 1e6
    # The Gavarnie sample cuts the Gave de Pau mid-course, so the dissolve is the
    # cours_d_eau's *full* BD Carthage basin (headwaters + downstream reaches),
    # a few thousand km² — not just the modelled mountain sub-network.
    assert 500 < area_km2 < 5000

    # every reach of the upstream stream network must fall inside that polygon
    from shapely.geometry import shape
    from shapely.ops import unary_union

    lines = unary_union(
        [shape(f["geometry"]) for f in rn.river_catchment_geojson(pau.id)["features"]]
    )
    assert lines.intersection(poly).length / lines.length > 0.95
