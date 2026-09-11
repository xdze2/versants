"""Tests for rolling the tronçon graph up into a river graph.

A hand-built network with three named watercourses and an unnamed reach makes the
expected roll-up obvious; a couple of checks run against the saved Gavarnie
tronçon sample when it is present.
"""

from __future__ import annotations

from pathlib import Path

import pytest

gpd = pytest.importorskip("geopandas")
pytest.importorskip("networkx")
pytest.importorskip("shapely")

from shapely.geometry import LineString  # noqa: E402

from valleespyr.hydro.network import build_graph  # noqa: E402
from valleespyr.hydro.rivers import (  # noqa: E402
    UNNAMED_PREFIX,
    build_river_network,
)

SAMPLE = Path("data/raw/troncon_hydrographique_gavarnie_sample.geojson")


# --------------------------------------------------------------------------- fixture


def _network() -> gpd.GeoDataFrame:
    """A trunk 'Main' fed by tributary 'Trib' and an unnamed reach.

        MAIN_1  MAIN_2      (cours_d_eau CDE_MAIN, "Main")
       s ---> a ---> b ---> o
                     ^
               TRIB_1|      (cours_d_eau CDE_TRIB, "Trib")
                     t
               UNN_1 |      (no cours_d_eau id -> unnamed reach, drains into Trib)
                     u

    Expected roll-up: two named rivers (Main, Trib); the unnamed reach merges
    downstream into Trib, so it does *not* become its own river. Trib flows into
    Main. Main is the root and not a leaf; Trib is a leaf.
    """
    rows = [
        # cleabs, ini, fin, cde, toponyme, geometry
        ("MAIN_1", "N_s", "N_a", "CDE_MAIN", "Main", [(-0.05, 42.80), (-0.03, 42.78)]),
        ("MAIN_2a", "N_a", "N_b", "CDE_MAIN", "Main", [(-0.03, 42.78), (-0.01, 42.77)]),
        ("MAIN_2b", "N_b", "N_o", "CDE_MAIN", "Main", [(-0.01, 42.77), (0.02, 42.76)]),
        ("TRIB_1", "N_t", "N_b", "CDE_TRIB", "Trib", [(-0.02, 42.82), (-0.01, 42.77)]),
        ("UNN_1", "N_u", "N_t", None, None, [(-0.03, 42.85), (-0.02, 42.82)]),
    ]
    gdf = gpd.GeoDataFrame(
        {
            "cleabs": [r[0] for r in rows],
            "lien_vers_noeud_hydrographique_ini": [r[1] for r in rows],
            "lien_vers_noeud_hydrographique_fin": [r[2] for r in rows],
            "sens_de_l_ecoulement": ["Sens direct"] * len(rows),
            "numero_d_ordre": ["2", "2", "3", "1", "1"],
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


# ---------------------------------------------------------------------- roll-up


def test_segments_group_into_named_rivers(rn):
    names = {r.name for r in rn if r.name}
    assert names == {"Main", "Trib"}
    main = rn.by_name("Main", exact=True)[0]
    assert main.id == "CDE_MAIN"
    assert main.segments == {"MAIN_1", "MAIN_2a", "MAIN_2b"}


def test_unnamed_reach_merges_downstream_into_its_named_river(rn):
    # UNN_1 has no cours_d_eau id; it drains into Trib and should be folded in,
    # not left as its own UNNAMED river.
    assert not any(r.id.startswith(UNNAMED_PREFIX) for r in rn)
    trib = rn.by_name("Trib", exact=True)[0]
    assert "UNN_1" in trib.segments
    assert rn.segment_to_river["UNN_1"] == trib.id


def test_river_graph_connects_trib_into_main(rn):
    trib = rn.by_name("Trib", exact=True)[0]
    main = rn.by_name("Main", exact=True)[0]
    assert trib.parent_id == main.id
    assert trib.id in main.child_ids
    assert [c.name for c in rn.children(main.id)] == ["Trib"]


def test_root_and_leaf_flags(rn):
    main = rn.by_name("Main", exact=True)[0]
    trib = rn.by_name("Trib", exact=True)[0]
    assert main.is_root and not main.is_leaf
    assert trib.is_leaf and not trib.is_root
    assert rn.roots() == [main]
    assert set(rn.leaves()) == {trib}


def test_outlet_and_source_nodes(rn):
    main = rn.by_name("Main", exact=True)[0]
    trib = rn.by_name("Trib", exact=True)[0]
    assert main.outlet == "N_o"
    assert main.source_nodes == {"N_s"}
    # Trib's outlet is the confluence with Main; its source is the unnamed reach's head
    assert trib.outlet == "N_b"
    assert trib.source_nodes == {"N_u"}


def test_upstream_rivers_and_downstream_path(rn):
    main = rn.by_name("Main", exact=True)[0]
    trib = rn.by_name("Trib", exact=True)[0]
    assert [r.name for r in rn.upstream_rivers(main.id, named_only=True)] == ["Trib"]
    assert rn.upstream_rivers(trib.id) == []
    assert [r.name for r in rn.downstream_path(trib.id)] == ["Trib", "Main"]
    assert [r.name for r in rn.downstream_path(main.id)] == ["Main"]


def test_max_order_taken_from_segments(rn):
    main = rn.by_name("Main", exact=True)[0]
    assert main.max_order == 3  # MAIN_2b is order 3


# -------------------------------------------------------------------- geo export


def test_river_path_geojson_has_only_that_rivers_lines(rn):
    main = rn.by_name("Main", exact=True)[0]
    fc = rn.river_path_geojson(main.id)
    assert fc["type"] == "FeatureCollection"
    ids = {f["properties"]["cleabs"] for f in fc["features"]}
    assert ids == {"MAIN_1", "MAIN_2a", "MAIN_2b"}
    assert fc["properties"]["river_name"] == "Main"


def test_river_catchment_geojson_includes_upstream_tribs(rn):
    main = rn.by_name("Main", exact=True)[0]
    fc = rn.river_catchment_geojson(main.id)
    ids = {f["properties"]["cleabs"] for f in fc["features"]}
    assert ids == {"MAIN_1", "MAIN_2a", "MAIN_2b", "TRIB_1", "UNN_1"}


def test_catchment_of_a_tributary_is_only_itself_and_its_upstream(rn):
    """A tributary's catchment must NOT include the trunk it flows into.

    Regression: it used to trace upstream from the river's outlet node, which is
    the confluence with the trunk, so it wrongly pulled in the trunk's headwaters.
    """
    trib = rn.by_name("Trib", exact=True)[0]
    ids = rn.catchment_segments(trib.id)
    assert ids == {"TRIB_1", "UNN_1"}  # Trib + its merged unnamed reach, nothing of Main
    assert rn.upstream_rivers(trib.id) == []  # a leaf: catchment == own segments
    assert ids == set(trib.segments)


def test_summary_rows_sorted_longest_first(rn):
    rows = rn.summary()
    lengths = [r["length_km"] for r in rows]
    assert lengths == sorted(lengths, reverse=True)
    assert rows[0]["name"] in {"Main", "Trib"}  # the two named rivers, longest first


# ------------------------------------------------------------ nameless cours_d_eau


def test_nameless_river_merges_into_its_named_downstream_river():
    """A cours_d_eau WITH a stable id but no toponyme is pure tree-graph noise;
    it should fold into the named river directly downstream of it, same as an
    unnamed reach (no id at all) already folds into its named river.

        MAIN_1                      (cours_d_eau CDE_MAIN, "Main")
       s ---> o
              ^
        TRIB_1|                     (cours_d_eau CDE_TRIB, "Trib")
              t
              ^
       NONAME_1|                    (cours_d_eau CDE_NONAME, no toponyme)
               n
    """
    rows = [
        ("MAIN_1", "N_s", "N_o", "CDE_MAIN", "Main", [(-0.05, 42.80), (0.0, 42.78)]),
        ("TRIB_1", "N_t", "N_s", "CDE_TRIB", "Trib", [(-0.06, 42.83), (-0.05, 42.80)]),
        ("NONAME_1", "N_n", "N_t", "CDE_NONAME", None, [(-0.07, 42.86), (-0.06, 42.83)]),
    ]
    gdf = gpd.GeoDataFrame(
        {
            "cleabs": [r[0] for r in rows],
            "lien_vers_noeud_hydrographique_ini": [r[1] for r in rows],
            "lien_vers_noeud_hydrographique_fin": [r[2] for r in rows],
            "sens_de_l_ecoulement": ["Sens direct"] * len(rows),
            "numero_d_ordre": ["2", "1", "1"],
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
    # a string-dtype column can't hold a real None (pandas coerces it to "nan");
    # real data goes through network.py::_prepare to fix this up before it
    # reaches build_graph, so redo that one normalisation here.
    col = gdf["cpx_toponyme_de_cours_d_eau"].astype(object)
    gdf["cpx_toponyme_de_cours_d_eau"] = col.where(col.notna(), None)

    rn = build_river_network(build_graph(gdf))

    assert {r.id for r in rn} == {"CDE_MAIN", "CDE_TRIB"}
    trib = rn.get("CDE_TRIB")
    assert "NONAME_1" in trib.segments
    assert rn.segment_to_river["NONAME_1"] == "CDE_TRIB"
    assert trib.parent_id == "CDE_MAIN"


# ------------------------------------------------------------------ offline sample


@pytest.mark.skipif(not SAMPLE.exists(), reason="gavarnie tronçon sample not present")
def test_gavarnie_sample_rolls_up_into_a_sane_river_graph():
    from valleespyr.hydro.network import load_troncons

    rn = build_river_network(build_graph(load_troncons(SAMPLE)))

    assert len(rn) > 100
    assert sum(1 for r in rn if r.name) > 50
    assert rn.graph.number_of_edges() > 100

    pau = rn.by_name("Gave de Pau", exact=True)
    assert len(pau) == 1
    pau = pau[0]
    assert pau.is_root  # nothing downstream inside this bbox
    assert pau.max_order and pau.max_order >= 6

    catchment = {r.name for r in rn.upstream_rivers(pau.id, named_only=True)}
    assert {"Gave de Héas", "Gave d'Estaubé", "Gave d'Ossoue"} <= catchment

    # la Neste is in the bbox but drains elsewhere -> its own root, NOT in Pau's catchment
    neste = rn.by_name("la Neste", exact=True)[0]
    assert neste.is_root
    assert "la Neste" not in catchment

    # Héas sits between its tributaries and the Gave de Pau
    heas = rn.by_name("Gave de Héas", exact=True)[0]
    assert not heas.is_root and not heas.is_leaf
    assert [r.name for r in rn.downstream_path(heas.id)] == ["Gave de Héas", "Gave de Pau"]
