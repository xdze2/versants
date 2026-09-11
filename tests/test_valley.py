"""Tests for :func:`valleespyr.valley.delineate_river`'s ``dem_source`` dispatch.

Only the dispatch logic is covered here — which ``hydro.dem`` fetch function
gets called for each ``dem_source`` value, and the error for an unknown one.
The actual DEM computation is covered by ``tests/test_dem.py``; the full
``valley catchments precompute`` CLI flow (including this same dispatch, one
level up) is covered by ``tests/test_cli.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

gpd = pytest.importorskip("geopandas")
pytest.importorskip("networkx")
pytest.importorskip("shapely")

from shapely.geometry import LineString  # noqa: E402

from valleespyr.hydro.network import build_graph  # noqa: E402
from valleespyr.hydro.rivers import build_river_network  # noqa: E402
from valleespyr.valley import delineate_river  # noqa: E402


@pytest.fixture
def simple_rn():
    rows = [
        ("MAIN_1", "N_s", "N_a", "2", "Main", "CDE_MAIN", [(-0.05, 42.80), (-0.03, 42.78)]),
        ("MAIN_2", "N_a", "N_o", "3", "Main", "CDE_MAIN", [(-0.03, 42.78), (0.02, 42.76)]),
    ]
    gdf = gpd.GeoDataFrame(
        {
            "cleabs": [r[0] for r in rows],
            "lien_vers_noeud_hydrographique_ini": [r[1] for r in rows],
            "lien_vers_noeud_hydrographique_fin": [r[2] for r in rows],
            "sens_de_l_ecoulement": ["Sens direct"] * len(rows),
            "numero_d_ordre": [r[3] for r in rows],
            "cpx_toponyme_de_cours_d_eau": [r[4] for r in rows],
            "liens_vers_cours_d_eau": [r[5] for r in rows],
            "fictif": [False] * len(rows),
            "reseau_principal_coulant": [True] * len(rows),
            "nature": ["Ecoulement naturel"] * len(rows),
            "geometry": [LineString(r[6]) for r in rows],
        },
        crs="EPSG:4326",
    )
    gdf["length_m"] = gdf.geometry.to_crs("EPSG:2154").length
    return build_river_network(build_graph(gdf))


def _stub_dem_module(monkeypatch, *, fetch_s3_calls, fetch_ot_calls):
    """Stub valleespyr.hydro.dem so delineate_river never touches a real DEM."""
    import valleespyr.hydro.dem as dem_mod

    def fake_fetch_dem_s3(bbox, dem_dir=None):
        fetch_s3_calls.append(bbox)
        return Path("/tmp/fake_s3.tif")

    def fake_fetch_dem(bbox, dest=None, demtype="COP30"):
        fetch_ot_calls.append(bbox)
        return Path("/tmp/fake_ot.tif")

    def fake_delineate(tif, lon, lat, acc_channel_cells=1000):
        from shapely.geometry import Polygon

        return Polygon([(-0.05, 42.75), (0.03, 42.75), (0.03, 42.83), (-0.05, 42.83)]), {
            "area_km2": 42.0,
            "snap_moved_cells": 0.1,
            "snap_xy": (-0.03, 42.78),
            "depression_fill_frac": 0.0,
            "n_cells": 100,
        }

    monkeypatch.setattr(dem_mod, "fetch_dem_s3", fake_fetch_dem_s3)
    monkeypatch.setattr(dem_mod, "fetch_dem", fake_fetch_dem)
    monkeypatch.setattr(dem_mod, "delineate", fake_delineate)


def test_delineate_river_defaults_to_s3(simple_rn, monkeypatch):
    s3_calls, ot_calls = [], []
    _stub_dem_module(monkeypatch, fetch_s3_calls=s3_calls, fetch_ot_calls=ot_calls)

    delineate_river(simple_rn, "CDE_MAIN")

    assert len(s3_calls) == 1
    assert ot_calls == []


def test_delineate_river_opentopography_source(simple_rn, monkeypatch):
    s3_calls, ot_calls = [], []
    _stub_dem_module(monkeypatch, fetch_s3_calls=s3_calls, fetch_ot_calls=ot_calls)

    delineate_river(simple_rn, "CDE_MAIN", dem_source="opentopography")

    assert s3_calls == []
    assert len(ot_calls) == 1


def test_delineate_river_rejects_unknown_dem_source(simple_rn, monkeypatch):
    s3_calls, ot_calls = [], []
    _stub_dem_module(monkeypatch, fetch_s3_calls=s3_calls, fetch_ot_calls=ot_calls)

    with pytest.raises(ValueError, match="unknown dem_source"):
        delineate_river(simple_rn, "CDE_MAIN", dem_source="bogus")
