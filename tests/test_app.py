"""Smoke tests for the Streamlit river navigator.

Pure helpers run on a hand-built :class:`RiverNetwork`; the full-app test uses
Streamlit's AppTest and is skipped when the local tronçon dump isn't present.
"""

from __future__ import annotations

from pathlib import Path

import pytest

gpd = pytest.importorskip("geopandas")
pytest.importorskip("streamlit")
pytest.importorskip("pydeck")
pytest.importorskip("networkx")
from shapely.geometry import LineString  # noqa: E402

from valleespyr.app import (  # noqa: E402
    TRONCON_CANDIDATES,
    _catchment_area_feature,
    _catchment_tree_lines,
    _fc_bounds,
    river_deck,
    river_figure,
)
from valleespyr.hydro.network import build_graph  # noqa: E402
from valleespyr.hydro.rivers import build_river_network  # noqa: E402


def _network():
    """Trunk 'Main' <- tributary 'Trib' <- 'Creek'; see test_rivers for the shape."""
    rows = [
        ("MAIN_1", "N_s", "N_a", "CDE_MAIN", "Main", [(-0.05, 42.80), (-0.03, 42.78)]),
        ("MAIN_2", "N_a", "N_o", "CDE_MAIN", "Main", [(-0.03, 42.78), (0.02, 42.76)]),
        ("TRIB_1", "N_t", "N_a", "CDE_TRIB", "Trib", [(-0.02, 42.82), (-0.03, 42.78)]),
        ("CREEK_1", "N_c", "N_t", "CDE_CREEK", "Creek", [(-0.01, 42.85), (-0.02, 42.82)]),
    ]
    gdf = gpd.GeoDataFrame(
        {
            "cleabs": [r[0] for r in rows],
            "lien_vers_noeud_hydrographique_ini": [r[1] for r in rows],
            "lien_vers_noeud_hydrographique_fin": [r[2] for r in rows],
            "sens_de_l_ecoulement": ["Sens direct"] * len(rows),
            "numero_d_ordre": ["3", "3", "2", "1"],
            "cpx_toponyme_de_cours_d_eau": [r[4] for r in rows],
            "liens_vers_cours_d_eau": [r[3] for r in rows],
            "fictif": [False] * len(rows),
            "reseau_principal_coulant": [True] * len(rows),
            "nature": ["Ecoulement naturel"] * len(rows),
            "geometry": [LineString(r[5]) for r in rows],
        },
        crs="EPSG:4326",
    )
    gdf["length_m"] = gdf.geometry.to_crs("EPSG:2154").length
    return gdf


@pytest.fixture
def rn():
    return build_river_network(build_graph(_network()))


# --------------------------------------------------------------------- helpers


def test_fc_bounds_from_a_feature_collection():
    fc = {
        "type": "FeatureCollection",
        "features": [
            {"geometry": {"type": "LineString", "coordinates": [[0, 1], [2, 3]]}},
            {"geometry": {"type": "LineString", "coordinates": [[-1, -2], [0, 0]]}},
        ],
    }
    assert _fc_bounds(fc) == (-1, -2, 2, 3)
    assert _fc_bounds({"type": "FeatureCollection", "features": []}) is None


def test_river_deck_builds_and_colours_the_picked_tributary(rn):
    main = rn.by_name("Main", exact=True)[0]
    trib = rn.by_name("Trib", exact=True)[0]
    course = rn.river_path_geojson(main.id)
    catchment = rn.river_catchment_geojson(main.id)

    deck = river_deck(
        course,
        catchment,
        picked_river_id=trib.id,
        segment_to_river=rn.segment_to_river,
    )
    assert deck.to_json()

    cat_layer = next(layer for layer in deck.layers if layer.id == "catchment")
    feats = cat_layer.data["features"]
    # every catchment feature is tagged with the river it belongs to
    rids = {f["properties"]["river_id"] for f in feats}
    assert {"CDE_MAIN", "CDE_TRIB", "CDE_CREEK"} <= rids
    # the picked tributary's reaches use the highlight colour, others don't
    picked = [f for f in feats if f["properties"]["river_id"] == trib.id]
    other = [f for f in feats if f["properties"]["river_id"] != trib.id]
    assert picked and all(f["properties"]["color"][0] == 255 for f in picked)
    assert all(f["properties"]["color"] != picked[0]["properties"]["color"] for f in other)


def test_catchment_area_feature_none_without_a_polygon():
    assert _catchment_area_feature(None) is None
    from shapely.geometry import Polygon

    assert _catchment_area_feature(Polygon()) is None  # empty geom


def test_catchment_area_feature_wraps_a_polygon():
    from shapely.geometry import Polygon

    poly = Polygon([(0, 42), (0.1, 42), (0.1, 42.1), (0, 42.1)])
    feat = _catchment_area_feature(poly)
    assert feat["type"] == "Feature"
    assert feat["geometry"]["type"] == "Polygon"


def test_river_deck_draws_the_catchment_area_polygon_when_given_one():
    from shapely.geometry import Polygon

    poly = Polygon([(0, 42), (0.2, 42), (0.2, 42.2), (0, 42.2)])
    fc_lines = {
        "type": "FeatureCollection",
        "features": [
            {
                "properties": {"cleabs": f"C{i}", "toponyme": "x", "order": 2},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[i * 0.02, 42.0], [i * 0.02 + 0.01, 42.1]],
                },
            }
            for i in range(6)
        ],
    }
    bare = river_deck({"type": "FeatureCollection", "features": []}, fc_lines)
    assert "catchment_area" not in {layer.id for layer in bare.layers}  # no polygon -> no wash

    deck = river_deck({"type": "FeatureCollection", "features": []}, fc_lines, catchment_area=poly)
    assert "catchment_area" in {layer.id for layer in deck.layers}


def test_river_figure_draws_course_catchment_and_area(rn):
    plt = pytest.importorskip("matplotlib.pyplot")
    from shapely.geometry import Polygon

    main = rn.by_name("Main", exact=True)[0]
    poly = Polygon([(-0.06, 42.74), (0.03, 42.74), (0.03, 42.87), (-0.06, 42.87)])
    fig = river_figure(rn, main.id, catchment_area=poly, area_km2=42.0, n_sub_basins=3)
    assert fig is not None
    ax = fig.axes[0]
    # the polygon patch plus the catchment and course line collections
    assert ax.collections
    assert "Main" in ax.get_title() and "42" in ax.get_title()
    plt.close(fig)


def test_river_figure_without_a_catchment_polygon(rn):
    plt = pytest.importorskip("matplotlib.pyplot")

    main = rn.by_name("Main", exact=True)[0]
    fig = river_figure(rn, main.id)
    assert fig is not None
    # no sub-basin count -> no area in the title
    assert "km" not in fig.axes[0].get_title()
    plt.close(fig)


def test_river_figure_none_for_unknown_river(rn):
    pytest.importorskip("matplotlib.pyplot")
    assert river_figure(rn, "NOT_A_RIVER") is None


def test_catchment_tree_lines_nest_children_biggest_first(rn):
    main = rn.by_name("Main", exact=True)[0]
    lines = list(_catchment_tree_lines(rn, main.id))
    assert lines[0].startswith("Main")
    assert any("Trib" in line for line in lines)
    assert any("Creek" in line for line in lines)
    # Creek is nested under Trib -> more indented
    trib_line = next(line for line in lines if "Trib" in line)
    creek_line = next(line for line in lines if "Creek" in line)
    assert len(creek_line) - len(creek_line.lstrip(" │├└─")) > len(trib_line) - len(
        trib_line.lstrip(" │├└─")
    )


# ---------------------------------------------------------------- offline sample


@pytest.mark.skipif(
    not any(Path(p).exists() for p in TRONCON_CANDIDATES),
    reason="no local tronçon dump; run `valleespyr wfs dump`",
)
def test_app_runs_and_navigates_into_a_river():
    from streamlit.testing.v1 import AppTest

    import valleespyr.app as app_mod

    at = AppTest.from_file(app_mod.__file__, default_timeout=120).run()
    assert not at.exception
    assert at.title[0].value.startswith("Pyrénées")

    # Landing page: a table of root basins, no river selected yet.
    assert len(at.dataframe) >= 1

    # Drive the sidebar picker to a known river and check the header renders.
    at.session_state["river_id"] = _first_named_root(app_mod, at)
    at.run()
    assert not at.exception
    assert len(at.subheader) >= 1
    assert any(m.label == "Length" for m in at.metric)
    # the catchment tree text block is present
    assert any("km" in t.value for t in at.text)


def _first_named_root(app_mod, at) -> str:
    rn = app_mod.load_network(_dump_path())
    roots = [r for r in rn.roots() if r.name]
    roots.sort(key=lambda r: r.length_m, reverse=True)
    return roots[0].id


def _dump_path() -> str:
    return str(next(p for p in TRONCON_CANDIDATES if Path(p).exists()))
