"""Tests for :mod:`valleespyr.valley`'s DEM-delineation glue.

``delineate_river``'s ``dem_source`` dispatch is covered here — which
``hydro.dem`` fetch function gets called for each ``dem_source`` value, and
the error for an unknown one — plus :func:`~valleespyr.valley.upstream_offset_point`
and the candidate-search / accept-or-reject logic of
:func:`~valleespyr.valley.delineate_river_search`. The actual DEM computation
is covered by ``tests/test_dem.py``; the full ``valley catchments precompute``
CLI flow (including this same dispatch, one level up) is covered by
``tests/test_cli.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

gpd = pytest.importorskip("geopandas")
pytest.importorskip("networkx")
pytest.importorskip("shapely")

from shapely.geometry import LineString, Polygon  # noqa: E402

from valleespyr.hydro.network import build_graph  # noqa: E402
from valleespyr.hydro.rivers import build_river_network  # noqa: E402
from valleespyr.valley import (  # noqa: E402
    delineate_river,
    delineate_river_search,
    upstream_offset_point,
)


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


# --------------------------------------------------------- upstream_offset_point


@pytest.fixture
def trib_rn():
    """Main <- Trib, each a single ~2.2km tronçon, joining at N_a."""
    rows = [
        ("MAIN_1", "N_a", "N_o", "2", "Main", "CDE_MAIN", [(-0.03, 42.78), (0.02, 42.76)]),
        ("TRIB_1", "N_t", "N_a", "1", "Trib", "CDE_TRIB", [(-0.02, 42.82), (-0.03, 42.78)]),
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


def test_upstream_offset_point_zero_is_the_outlet(trib_rn):
    from valleespyr.valley import outlet_point

    assert upstream_offset_point(trib_rn, "CDE_TRIB", 0) == outlet_point(trib_rn, "CDE_TRIB")


def test_upstream_offset_point_moves_toward_the_source(trib_rn):
    from shapely.geometry import Point

    outlet = Point(*upstream_offset_point(trib_rn, "CDE_TRIB", 0))
    source = Point(-0.02, 42.82)  # TRIB_1's upstream end

    near = Point(*upstream_offset_point(trib_rn, "CDE_TRIB", 200))
    far = Point(*upstream_offset_point(trib_rn, "CDE_TRIB", 1000))

    # further offsets land further from the outlet, closer to the source
    assert outlet.distance(near) < outlet.distance(far)
    assert far.distance(source) < near.distance(source)


def test_upstream_offset_point_none_past_the_rivers_own_length(trib_rn):
    # TRIB_1 is ~2.2 km long: an offset well beyond that has nowhere to land.
    assert upstream_offset_point(trib_rn, "CDE_TRIB", 50_000) is None


# ------------------------------------------------------------- delineate_river_search


def _stub_dem_search(monkeypatch, river_id, *, frac_by_offset: dict[float, float], rn, fetch_calls=None):
    """Stub hydro.dem.condition/trace so delineate_river_search never touches a real DEM.

    ``frac_by_offset`` maps an offset_m -> the course_inside_frac the fake
    trace() should produce for a pour point at that offset. Candidate points
    are matched back to an offset by exact (lon, lat), computed once via the
    real :func:`upstream_offset_point` so this doesn't need to duplicate the
    walk logic.
    """
    import valleespyr.hydro.dem as dem_mod
    import valleespyr.valley as valley_mod

    poly = Polygon([(-0.05, 42.75), (0.03, 42.75), (0.03, 42.83), (-0.05, 42.83)])
    point_to_frac = {
        upstream_offset_point(rn, river_id, offset_m): frac for offset_m, frac in frac_by_offset.items()
    }

    def fake_fetch_dem_s3(bbox, dem_dir=None):
        if fetch_calls is not None:
            fetch_calls.append(bbox)
        return Path("/tmp/fake_s3.tif")

    def fake_condition(tif):
        return object()  # opaque; only ever passed back into fake_trace

    def fake_trace(conditioned, lon, lat, *, acc_channel_cells=1000):
        return poly, {
            "area_km2": 42.0,
            "snap_moved_cells": 0.1,
            "snap_xy": (lon, lat),
            "depression_fill_frac": 0.0,
            "n_cells": 100,
            "_point": (lon, lat),  # smuggled through diag for fake_course_inside_frac below
        }

    monkeypatch.setattr(dem_mod, "fetch_dem_s3", fake_fetch_dem_s3)
    monkeypatch.setattr(dem_mod, "condition", fake_condition)
    monkeypatch.setattr(dem_mod, "trace", fake_trace)

    # _course_inside_frac only sees (fc, poly), not which candidate produced
    # poly - since every candidate here shares the same canned poly, smuggle
    # the point through a module-level slot fake_trace just set instead.
    last_point: list[tuple[float, float]] = []
    real_trace = fake_trace

    def fake_trace_recording(conditioned, lon, lat, *, acc_channel_cells=1000):
        last_point[:] = [(lon, lat)]
        return real_trace(conditioned, lon, lat, acc_channel_cells=acc_channel_cells)

    monkeypatch.setattr(dem_mod, "trace", fake_trace_recording)

    def fake_course_inside_frac(fc, poly_):
        return point_to_frac[last_point[0]]

    monkeypatch.setattr(valley_mod, "_course_inside_frac", fake_course_inside_frac)


def test_delineate_river_search_prefers_offset_0_when_it_scores_well(trib_rn, monkeypatch):
    _stub_dem_search(monkeypatch, "CDE_TRIB", rn=trib_rn, frac_by_offset={0: 0.99, 100: 0.5, 200: 0.4, 300: 0.3})

    result = delineate_river_search(trib_rn, "CDE_TRIB")

    assert result is not None
    _poly, diag = result
    assert diag["offset_m"] == 0
    assert diag["n_candidates"] == 1  # early exit: offset 0 already >= 0.99


def test_delineate_river_search_finds_a_better_offset(trib_rn, monkeypatch):
    # Mirrors the confirmed Ruisseau d'Aube case: offset 0 locks onto the
    # parent's basin (low frac), a later offset clears the confluence.
    _stub_dem_search(
        monkeypatch,
        "CDE_TRIB",
        rn=trib_rn,
        frac_by_offset={0: 0.03, 100: 0.02, 200: 0.91, 300: 0.96, 500: 0.94},
    )

    result = delineate_river_search(trib_rn, "CDE_TRIB", offsets_m=(0, 100, 200, 300, 500))

    assert result is not None
    _poly, diag = result
    assert diag["offset_m"] == 300  # highest-scoring candidate, not the first one tried
    assert diag["course_inside_frac"] == pytest.approx(0.96)


def test_delineate_river_search_returns_none_when_nothing_clears_the_bar(trib_rn, monkeypatch):
    _stub_dem_search(
        monkeypatch,
        "CDE_TRIB",
        rn=trib_rn,
        frac_by_offset=dict.fromkeys([0, 100, 200, 300, 500, 700, 900, 1100], 0.2),
    )

    assert delineate_river_search(trib_rn, "CDE_TRIB") is None
