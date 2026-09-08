"""Smoke tests for the Streamlit explorer.

The pure helpers run on a synthetic GeoDataFrame; the full-app test uses
Streamlit's AppTest and is skipped when the local dump isn't present.
"""

from __future__ import annotations

from pathlib import Path

import pytest

gpd = pytest.importorskip("geopandas")
pytest.importorskip("streamlit")
pytest.importorskip("pydeck")
from shapely.geometry import Polygon  # noqa: E402

from valleespyr.app import (  # noqa: E402
    DATA_CANDIDATES,
    deck_for,
    dissolve_by_watercourse,
    river_name,
)


def _synthetic() -> gpd.GeoDataFrame:
    rows = [
        ("A", "COURDEAU0001", "05B0001", Polygon([(0, 42), (0, 42.1), (0.1, 42.1), (0.1, 42)])),
        ("B", "COURDEAU0001", "05B0002", Polygon([(0.1, 42), (0.1, 42.1), (0.2, 42.1), (0.2, 42)])),
        ("C", "COURDEAU0002", "05B0003", Polygon([(1, 43), (1, 43.1), (1.1, 43.1), (1.1, 43)])),
    ]
    gdf = gpd.GeoDataFrame(
        {
            "toponyme": [r[0] for r in rows],
            "liens_vers_cours_d_eau_principal": [r[1] for r in rows],
            "code_hydrographique": [r[2] for r in rows],
            "area_km2": [10.0, 12.0, 5.0],
            "geometry": [r[3] for r in rows],
        },
        crs="EPSG:4326",
    )
    return gdf


def test_dissolve_by_watercourse_groups_and_counts():
    out = dissolve_by_watercourse(_synthetic())
    assert len(out) == 2
    row = out.set_index("liens_vers_cours_d_eau_principal").loc["COURDEAU0001"]
    assert row["n_subcatchments"] == 2
    assert row["area_km2"] == pytest.approx(22.0)


def test_dissolve_by_watercourse_adds_readable_name():
    gdf = _synthetic()
    gdf["toponyme"] = [
        "Le Gave de Pau du confluent de l'Ouzom au confluent du Béez",
        "La Gave de Pau de sa source au confluent du Pailla",
        "L'Ariège du confluent de X au confluent de Y",
    ]
    out = dissolve_by_watercourse(gdf).set_index("liens_vers_cours_d_eau_principal")
    assert out.loc["COURDEAU0001", "watercourse"] == "Gave de Pau"


@pytest.mark.parametrize(
    "toponyme, expected",
    [
        ("Le Gave de Pau du confluent de l'Ouzom au confluent du Béez", "Gave de Pau"),
        ("La Gave de Pau de sa source au confluent du Pailla", "Gave de Pau"),
        ("L'Ariège du confluent du Vicdessos au confluent du Lauze", "Ariège"),
        ("", "(sans nom)"),
    ],
)
def test_river_name(toponyme, expected):
    assert river_name(toponyme) == expected


def test_deck_for_builds_valid_deck_both_views():
    gdf = _synthetic()
    d1 = deck_for(gdf, {gdf.index[0]})
    assert d1.to_json()
    d2 = deck_for(dissolve_by_watercourse(gdf), set())  # no 'toponyme' -> label fallback
    assert d2.to_json()


def test_deck_for_selected_feature_is_highlighted():
    gdf = _synthetic()
    d = deck_for(gdf, {gdf.index[1]})
    feats = d.layers[0].data["features"]
    fills = [f["properties"]["fill"] for f in feats]
    assert fills[1][0] == 255 and fills[0][0] != 255  # only row 1 highlighted


@pytest.mark.skipif(
    not any(Path(p).exists() for p in DATA_CANDIDATES),
    reason="no local watershed dump; run `valleespyr wfs dump`",
)
def test_app_runs_without_exception():
    from streamlit.testing.v1 import AppTest

    import valleespyr.app as app_mod

    at = AppTest.from_file(app_mod.__file__, default_timeout=60).run()
    assert not at.exception
    assert at.title[0].value.startswith("Pyrénées")

    # Default view is the drill-down map (pydeck chart, no dataframe).
    assert len(at.dataframe) == 0

    view = next(r for r in at.radio if r.label == "View")
    view.set_value("Sub-catchments").run()
    assert not at.exception
    assert len(at.dataframe) == 1

    view = next(r for r in at.radio if r.label == "View")
    view.set_value("Dissolved by watercourse").run()
    assert not at.exception
    assert len(at.dataframe[0].value) < len(_synthetic()) + 10_000  # sane, dissolved is smaller


TRONCON_SAMPLE = Path("data/raw/troncon_hydrographique_gavarnie_sample.geojson")


@pytest.mark.skipif(
    not (any(Path(p).exists() for p in DATA_CANDIDATES) and TRONCON_SAMPLE.exists()),
    reason="need both the watershed dump and the gavarnie tronçon sample",
)
def test_upstream_trace_view_traces_from_the_default_pour_point():
    pytest.importorskip("networkx")
    from streamlit.testing.v1 import AppTest

    import valleespyr.app as app_mod

    at = AppTest.from_file(app_mod.__file__, default_timeout=120).run()
    view = next(r for r in at.radio if r.label == "View")
    view.set_value("Upstream trace (streams)").run()
    assert not at.exception

    assert any("Upstream of Gave de Pau" in s.value for s in at.subheader)
    edges = next(m for m in at.metric if m.label == "Edges traced")
    assert int(edges.value) > 200
    # the indented tree is rendered as a text block
    tree_text = next(t.value for t in at.text if "pour point" in t.value)
    assert "Gave de Pau" in tree_text
    assert "Gave de Héas" not in tree_text  # joins downstream, must not be upstream
