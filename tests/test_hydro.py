"""Tests for the stream-network graph + upstream trace.

Most run on a tiny hand-built Y network so the expected topology is obvious;
a few use the saved Gavarnie tronçon sample when it is present.
"""

from __future__ import annotations

from pathlib import Path

import pytest

gpd = pytest.importorskip("geopandas")
pytest.importorskip("networkx")
pytest.importorskip("shapely")

from shapely.geometry import LineString  # noqa: E402

from valleespyr.hydro.network import build_graph  # noqa: E402
from valleespyr.hydro.trace import (  # noqa: E402
    drop_fictif,
    snap_pour_point,
    to_tree,
    trace_upstream,
)

SAMPLE = Path("data/raw/troncon_hydrographique_gavarnie_sample.geojson")


# --------------------------------------------------------------------------- fixtures


def _y_network() -> gpd.GeoDataFrame:
    """Two headwater tribs (A, B) joining at node J, then a trunk J->O (the outlet).

        A\
          J --- O
        B/

    Placed in the Pyrénées (near Gavarnie) so the EPSG:2154 metric CRS behaves.
    Edge B is drawn downstream-to-upstream and tagged ``Sens inverse`` — the graph
    must still put water flowing B_src -> J. Trib A is the longer of the two.
    """
    rows = [
        # cleabs, ini, fin, sens, order, toponyme, geometry (lon, lat)
        ("EDGE_A", "N_A", "N_J", "Sens direct", "1", "Trib A",
         LineString([(-0.05, 42.78), (0.00, 42.75)])),
        ("EDGE_B", "N_J", "N_B", "Sens inverse", "1", "Trib B",
         LineString([(0.00, 42.75), (0.02, 42.73)])),  # drawn J->B, real B->J
        ("EDGE_TRUNK", "N_J", "N_O", "Sens direct", "2", "Trunk",
         LineString([(0.00, 42.75), (0.05, 42.75)])),
    ]
    gdf = gpd.GeoDataFrame(
        {
            "cleabs": [r[0] for r in rows],
            "lien_vers_noeud_hydrographique_ini": [r[1] for r in rows],
            "lien_vers_noeud_hydrographique_fin": [r[2] for r in rows],
            "sens_de_l_ecoulement": [r[3] for r in rows],
            "numero_d_ordre": [r[4] for r in rows],
            "cpx_toponyme_de_cours_d_eau": [r[5] for r in rows],
            "fictif": [False, False, False],
            "reseau_principal_coulant": [True, True, True],
            "nature": ["Ecoulement naturel"] * 3,
            "geometry": [r[6] for r in rows],
        },
        crs="EPSG:4326",
    )
    gdf["length_m"] = gdf.geometry.to_crs("EPSG:2154").length
    return gdf


# --------------------------------------------------------------------------- graph


def test_build_graph_orients_edges_downstream():
    g = build_graph(_y_network())
    # every edge points the way the water goes
    assert g.has_edge("N_A", "N_J")
    assert g.has_edge("N_B", "N_J")  # Sens inverse -> nodes swapped
    assert not g.has_edge("N_J", "N_B")
    assert g.has_edge("N_J", "N_O")


def test_build_graph_skips_self_loops_and_keeps_longest_parallel():
    gdf = _y_network()
    gdf = gpd.GeoDataFrame(
        {
            "cleabs": ["SELF", "PAR_SHORT", "PAR_LONG"],
            "lien_vers_noeud_hydrographique_ini": ["N_X", "N_P", "N_P"],
            "lien_vers_noeud_hydrographique_fin": ["N_X", "N_Q", "N_Q"],
            "sens_de_l_ecoulement": ["Sens direct"] * 3,
            "numero_d_ordre": ["1", "1", "1"],
            "cpx_toponyme_de_cours_d_eau": [None, "Braid", "Braid"],
            "fictif": [False, False, False],
            "reseau_principal_coulant": [True, True, True],
            "nature": ["Ecoulement naturel"] * 3,
            "geometry": [
                LineString([(0, 0), (0, 0.001)]),
                LineString([(0, 0), (0.1, 0)]),
                LineString([(0, 0), (0.3, 0)]),
            ],
        },
        crs="EPSG:4326",
    )
    gdf["length_m"] = gdf.geometry.to_crs("EPSG:2154").length
    g = build_graph(gdf)
    assert not g.has_edge("N_X", "N_X")
    assert g.number_of_edges() == 1
    assert g["N_P"]["N_Q"]["cleabs"] == "PAR_LONG"


def test_build_graph_flags_ambiguous_flow():
    gdf = _y_network()
    gdf.loc[gdf["cleabs"] == "EDGE_TRUNK", "sens_de_l_ecoulement"] = "Double sens"
    g = build_graph(gdf)
    assert g["N_J"]["N_O"]["ambiguous"] is True
    assert g["N_A"]["N_J"]["ambiguous"] is False


# --------------------------------------------------------------------------- trace


def test_trace_upstream_from_outlet_collects_whole_network():
    g = build_graph(_y_network())
    ids, sub = trace_upstream(g, "N_O")
    assert ids == {"EDGE_A", "EDGE_B", "EDGE_TRUNK"}
    assert set(sub.nodes) == {"N_A", "N_B", "N_J", "N_O"}


def test_trace_upstream_from_junction_excludes_trunk():
    g = build_graph(_y_network())
    ids, _ = trace_upstream(g, "N_J")
    assert ids == {"EDGE_A", "EDGE_B"}  # trunk is downstream of J


def test_trace_upstream_unknown_node_is_empty():
    g = build_graph(_y_network())
    ids, sub = trace_upstream(g, "NOPE")
    assert ids == set()
    assert sub.number_of_nodes() == 0


def test_trace_upstream_survives_a_cycle():
    """A braided section can make a 2-cycle; the visited set must stop the walk."""
    import networkx as nx

    g = nx.DiGraph()
    g.add_edge("a", "b", cleabs="AB", length_m=1.0, fictif=False, ambiguous=False,
               toponyme="x", order=1)
    g.add_edge("b", "a", cleabs="BA", length_m=1.0, fictif=False, ambiguous=False,
               toponyme="x", order=1)
    g.add_edge("b", "out", cleabs="BO", length_m=1.0, fictif=False, ambiguous=False,
               toponyme="x", order=1)
    ids, sub = trace_upstream(g, "out")
    assert ids == {"AB", "BA", "BO"}
    assert set(sub.nodes) == {"a", "b", "out"}


# ---------------------------------------------------------------------------- tree


def test_to_tree_nests_from_root_and_orders_children_by_length():
    g = build_graph(_y_network())
    _, sub = trace_upstream(g, "N_O")
    tree = to_tree(sub, "N_O", collapse_chains=False)

    assert tree["edge"] is None  # root is the pour point
    (trunk,) = tree["children"]
    assert trunk["edge"]["toponyme"] == "Trunk"
    names = [c["edge"]["toponyme"] for c in trunk["children"]]
    assert set(names) == {"Trib A", "Trib B"}
    # A is the longer trib in the fixture -> listed first
    assert names[0] == "Trib A"
    assert trunk["upstream_segments"] == 3


def test_to_tree_min_order_prunes_small_tributaries():
    g = build_graph(_y_network())
    _, sub = trace_upstream(g, "N_O")
    tree = to_tree(sub, "N_O", collapse_chains=False, min_order=2)
    (trunk,) = tree["children"]
    assert trunk["children"] == []  # order-1 tribs dropped


def test_drop_fictif_promotes_children_over_connector_edges():
    import networkx as nx

    g = nx.DiGraph()
    # root <- FIC (fictif connector) <- REAL (real channel)
    g.add_edge("m", "root", cleabs="FIC", length_m=50.0, fictif=True, ambiguous=False,
               toponyme="Lake outlet", order=3)
    g.add_edge("s", "m", cleabs="REAL", length_m=800.0, fictif=False, ambiguous=False,
               toponyme="Real River", order=3)
    tree = to_tree(g, "root", collapse_chains=False)
    assert tree["children"][0]["edge"]["cleabs"] == "FIC"  # present before

    drop_fictif(tree)
    kids = tree["children"]
    assert [k["edge"]["cleabs"] for k in kids] == ["REAL"]  # connector spliced out
    # totals untouched: the fictif segment is still counted
    assert tree["upstream_segments"] == 2


def test_to_tree_collapses_unbranched_same_river_chain():
    import networkx as nx

    g = nx.DiGraph()
    # root <- s1 <- s2 <- s3, all "River R", then s3 branches
    for u, v, cid in [("s1", "root", "E1"), ("s2", "s1", "E2"), ("s3", "s2", "E3")]:
        g.add_edge(u, v, cleabs=cid, length_m=1000.0, fictif=False, ambiguous=False,
                   toponyme="River R", order=3)
    g.add_edge("t1", "s3", cleabs="T1", length_m=500.0, fictif=False, ambiguous=False,
               toponyme="River R", order=3)
    g.add_edge("t2", "s3", cleabs="T2", length_m=400.0, fictif=False, ambiguous=False,
               toponyme="Side", order=1)

    tree = to_tree(g, "root", collapse_chains=True)
    (river,) = tree["children"]
    assert river["edge"]["toponyme"] == "River R"
    # E1..E3 fold into one node; it now has the branch's two children
    assert river["collapsed_segments"] >= 1
    assert {c["edge"]["toponyme"] for c in river["children"]} == {"River R", "Side"}


# --------------------------------------------------------------------------- snap


def test_snap_pour_point_picks_nearest_edge_and_returns_upstream_node():
    gdf = _y_network()
    # a point sitting right on the trunk, closer to it than to any trib
    hit = snap_pour_point(gdf, 0.025, 42.75)
    assert hit["cleabs"] == "EDGE_TRUNK"
    assert hit["root_node"] == "N_J"  # ini of the trunk == upstream node
    assert hit["distance_m"] < 50.0


def test_snap_pour_point_respects_sens_inverse_for_root_node():
    gdf = _y_network()
    # near trib B, whose geometry is drawn reversed (Sens inverse)
    hit = snap_pour_point(gdf, 0.018, 42.732)
    assert hit["cleabs"] == "EDGE_B"
    # ini/fin as stored are N_J/N_B; Sens inverse -> upstream root is 'fin' == N_B
    assert hit["root_node"] == "N_B"


# ------------------------------------------------------------------- offline sample


@pytest.mark.skipif(not SAMPLE.exists(), reason="gavarnie tronçon sample not present")
def test_gavarnie_sample_traces_a_plausible_headwater_network():
    from valleespyr.hydro.network import load_troncons

    gdf = load_troncons(SAMPLE)
    assert len(gdf) > 1000
    assert (gdf["length_m"] > 0).all()

    snap = snap_pour_point(gdf, -0.0086, 42.7350)  # Gavarnie village on the Gave de Pau
    assert snap["toponyme"] == "Gave de Pau"
    assert snap["distance_m"] < 500

    g = build_graph(gdf)
    ids, sub = trace_upstream(g, snap["root_node"])
    assert len(ids) > 200  # a big upstream network
    tree = to_tree(sub, snap["root_node"])
    assert tree["upstream_length_m"] > 50_000  # >50 km of channel upstream

    # the Gave de Héas joins the Gave de Pau *downstream* of Gavarnie village,
    # so it must NOT appear in the upstream trace
    names = set()
    stack = [tree]
    while stack:
        n = stack.pop()
        if n["edge"]:
            names.add(n["edge"]["toponyme"])
        stack.extend(n["children"])
    assert "Gave de Héas" not in names
