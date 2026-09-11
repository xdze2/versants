"""Tests for the valley catalog: the nested-dict tree and its static HTML.

Two hand-built networks make the expected shape obvious:

* ``_simple_network`` — a trunk with one real tributary and a merged unnamed
  reach (same fixture idea as ``test_rivers``);
* ``_branchy_network`` — a main stem with four tributaries of decreasing
  catchment plus a bifurcation, to exercise ordering, Pfafstetter digits and
  ``also_flows_into``.

The Gavarnie tronçon sample drives one end-to-end check when it is present.
No DEM / bassin dump is used, so every ``icon`` is ``None`` here; the icon path
is smoke-tested separately against ``_points_to_svg_path`` directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

gpd = pytest.importorskip("geopandas")
pytest.importorskip("networkx")
pytest.importorskip("shapely")

from shapely.geometry import LineString  # noqa: E402

from valleespyr.catalog import _points_to_svg_path, build_catalog  # noqa: E402
from valleespyr.hydro.network import build_graph  # noqa: E402
from valleespyr.hydro.rivers import build_river_network  # noqa: E402
from valleespyr.render.catalog_html import catalog_to_html  # noqa: E402

SAMPLE = Path("data/raw/troncon_hydrographique_gavarnie_sample.geojson")


# --------------------------------------------------------------------------- fixtures


def _gdf(rows: list[tuple]) -> gpd.GeoDataFrame:
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
    return gdf


def _simple_network() -> gpd.GeoDataFrame:
    """Main <- Trib (with a merged unnamed reach). Same as test_rivers._network."""
    rows = [
        ("MAIN_1", "N_s", "N_a", "2", "Main", "CDE_MAIN", [(-0.05, 42.80), (-0.03, 42.78)]),
        ("MAIN_2a", "N_a", "N_b", "2", "Main", "CDE_MAIN", [(-0.03, 42.78), (-0.01, 42.77)]),
        ("MAIN_2b", "N_b", "N_o", "3", "Main", "CDE_MAIN", [(-0.01, 42.77), (0.02, 42.76)]),
        ("TRIB_1", "N_t", "N_b", "1", "Trib", "CDE_TRIB", [(-0.02, 42.82), (-0.01, 42.77)]),
        ("UNN_1", "N_u", "N_t", "1", None, None, [(-0.03, 42.85), (-0.02, 42.82)]),
    ]
    return _gdf(rows)


def _branchy_network() -> gpd.GeoDataFrame:
    """A four-node main stem S1->S2->S3->S4->OUT with tributaries of shrinking size.

        TribA  (2 seg, has its own upstream TribAup) --> N2
        TribB  (1 seg)                                --> N3
        TribC  (1 seg) --> N4, and also splits to a second stem (bifurcation)
        TribD  (1 seg) --> N4   (5th-ranked: folds into an even reach)

    A short second stem ``Stem2`` gives TribC's bifurcating branch a *named*
    river to drain into, so the river graph gets two out-edges from ``CDE_C``.
    """
    rows = [
        # main stem
        ("S1", "N1", "N2", "4", "Stem", "CDE_STEM", [(0.00, 42.90), (0.00, 42.88)]),
        ("S2", "N2", "N3", "4", "Stem", "CDE_STEM", [(0.00, 42.88), (0.00, 42.86)]),
        ("S3", "N3", "N4", "4", "Stem", "CDE_STEM", [(0.00, 42.86), (0.00, 42.84)]),
        ("S4", "N4", "N_OUT", "4", "Stem", "CDE_STEM", [(0.00, 42.84), (0.00, 42.82)]),
        # a small independent second stem, its own root
        ("S2A", "M1", "M_OUT", "2", "Stem2", "CDE_STEM2", [(0.03, 42.86), (0.03, 42.83)]),
        # tributary A: two segments, joins at N2 (largest sub-catchment)
        ("TA1", "NA0", "NA1", "2", "TribA", "CDE_A", [(-0.06, 42.92), (-0.03, 42.90)]),
        ("TA2", "NA1", "N2", "2", "TribA", "CDE_A", [(-0.03, 42.90), (0.00, 42.88)]),
        ("TA_up", "NA_s", "NA0", "1", "TribAup", "CDE_AUP", [(-0.08, 42.94), (-0.06, 42.92)]),
        # tributary B: one segment, joins at N3
        ("TB1", "NB0", "N3", "1", "TribB", "CDE_B", [(-0.05, 42.87), (0.00, 42.86)]),
        # tributary C: one segment to N4, and a bifurcating branch into Stem2
        ("TC1", "NC0", "N4", "1", "TribC", "CDE_C", [(-0.04, 42.85), (0.00, 42.84)]),
        ("TC_bif", "NC0", "M1", "1", "TribC", "CDE_C", [(-0.04, 42.85), (0.03, 42.86)]),
        # tributary D: one segment to N4 (5th-ranked trib overall on the stem)
        ("TD1", "ND0", "N4", "1", "TribD", "CDE_D", [(0.04, 42.85), (0.00, 42.84)]),
    ]
    return _gdf(rows)


@pytest.fixture
def simple_rn():
    return build_river_network(build_graph(_simple_network()))


@pytest.fixture
def branchy_rn():
    return build_river_network(build_graph(_branchy_network()))


def walk(node):
    """Every node in a git-model catalog tree: the node, its mainline, its tribs."""
    yield node
    if node.get("mainline") is not None:
        yield from walk(node["mainline"])
    for t in node.get("tributaries") or []:
        yield from walk(t)


def spine(head):
    """The head node then each ``mainline`` in turn (one branch, mouth -> source)."""
    out = [head]
    cur = head.get("mainline")
    while cur is not None:
        out.append(cur)
        cur = cur.get("mainline")
    return out


# ------------------------------------------------------------------- tree shape


def test_root_resolves_by_name_and_id(simple_rn):
    by_name = build_catalog(simple_rn, "Main")
    by_id = build_catalog(simple_rn, "CDE_MAIN")
    assert by_name["root"]["id"] == by_id["root"]["id"] == "CDE_MAIN"
    assert by_name["meta"]["n_nodes"] == 2


def test_bad_root_raises(simple_rn):
    with pytest.raises(ValueError, match="no river matching"):
        build_catalog(simple_rn, "Nonexistent")


def test_node_carries_browse_facts(simple_rn):
    cat = build_catalog(simple_rn, "Main")
    root = cat["root"]
    assert root["name"] == "Main"
    assert root["strahler"] == 3
    assert root["n_upstream"] == 1
    assert root["depth"] == 0
    assert root["pfafstetter"] is None  # the root is never coded
    # Main has a single upstream child (Trib) -> the valley continues along it as
    # the mainline; no fork here, so no tributaries.
    assert root["n_tributaries"] == 0
    assert root["tributaries"] == []
    cont = root["mainline"]
    assert cont["name"] == "Trib"
    assert cont["depth"] == 0  # a free mainline hop, not a confluence
    assert cont["is_headwater"] is True
    assert cont["pfafstetter"]  # every descendant gets a code


def test_meta_declares_git_model(simple_rn):
    assert build_catalog(simple_rn, "Main")["meta"]["model"] == "git"


def test_no_bassins_means_no_icons(simple_rn):
    cat = build_catalog(simple_rn, "Main")
    assert cat["meta"]["has_icons"] is False
    assert cat["meta"]["n_icons"] == 0
    assert all(n["icon"] is None for n in walk(cat["root"]))


# ------------------------------------------------------- mainline vs tributaries


def test_mainline_is_the_largest_subcatchment_child(branchy_rn):
    cat = build_catalog(branchy_rn, "CDE_STEM")
    root = cat["root"]
    # Stem has no same-name child post-roll-up, so the mainline is the biggest:
    # TribA (it carries its own upstream river TribAup).
    assert root["mainline"] is not None
    assert root["mainline"]["name"] == "TribA"
    # the rest are tributaries, biggest-catchment first
    trib_names = [t["name"] for t in root["tributaries"]]
    assert "TribA" not in trib_names
    assert set(trib_names) >= {"TribB", "TribC", "TribD"}


def test_same_name_reaches_are_one_node_so_mainline_is_by_catchment():
    """The roll-up merges every same-``cours_d_eau`` reach into one river, so a
    river never has a same-name *child* — the mainline is always the
    largest-sub-catchment child. (The same-name preference in the code is a
    harmless fallback that this data model can't actually exercise.)"""
    rows = [
        ("R0", "N0", "N_OUT", "3", "Riu", "CDE_RIU", [(0.0, 42.9), (0.0, 42.88)]),
        ("R1", "N1", "N0", "2", "Riu", "CDE_RIU", [(0.0, 42.92), (0.0, 42.9)]),
        ("B1", "NB1", "NB2", "2", "Big", "CDE_BIG", [(-0.05, 42.95), (-0.03, 42.92)]),
        ("B2", "NB2", "N0", "2", "Big", "CDE_BIG", [(-0.03, 42.92), (0.0, 42.9)]),
        ("S1", "NS1", "N0", "1", "Small", "CDE_SML", [(0.05, 42.95), (0.0, 42.9)]),
    ]
    rn = build_river_network(build_graph(_gdf(rows)))
    cat = build_catalog(rn, "CDE_RIU")
    # CDE_RIU's own R1 reach is folded in; its graph children are Big and Small.
    assert cat["root"]["mainline"]["name"] == "Big"       # bigger catchment
    assert [t["name"] for t in cat["root"]["tributaries"]] == ["Small"]


def test_spine_walks_mouth_to_source(branchy_rn):
    cat = build_catalog(branchy_rn, "CDE_STEM")
    ids = [n["id"] for n in spine(cat["root"])]
    # root, then its mainline chain: CDE_STEM -> CDE_A -> CDE_AUP
    assert ids[0] == "CDE_STEM"
    assert "CDE_A" in ids and "CDE_AUP" in ids
    assert ids.index("CDE_A") < ids.index("CDE_AUP")
    assert spine(cat["root"])[-1]["is_headwater"] is True


def test_every_descendant_is_coded(branchy_rn):
    cat = build_catalog(branchy_rn, "CDE_STEM")
    nodes = list(walk(cat["root"]))
    assert cat["root"]["pfafstetter"] is None
    assert all(n["pfafstetter"] for n in nodes if n is not cat["root"])


# --------------------------------------------------------------- DAG / bifurcation


def _find(node, name):
    for n in walk(node):
        if n["name"] == name:
            return n
    raise AssertionError(f"{name!r} not in tree")


def test_bifurcation_recorded_as_also_flows_into(branchy_rn):
    cat = build_catalog(branchy_rn, "CDE_STEM")
    tribc = _find(cat["root"], "TribC")
    # TribC drains to the stem outlet and into Stem2 -> one alternate
    assert tribc["also_flows_into"], "expected the bifurcation to be flagged"
    assert all("id" in a for a in tribc["also_flows_into"])


def test_bifurcating_river_appears_once_in_the_tree(branchy_rn):
    cat = build_catalog(branchy_rn, "CDE_STEM")
    ids = [n["id"] for n in walk(cat["root"])]
    assert ids.count("CDE_C") == 1


# --------------------------------------------------------------------- max_depth


def test_max_depth_counts_confluences_not_free_mainline_hops(branchy_rn):
    # depth 0 = the root confluence. Its tributaries are depth 1. A mainline hop
    # through a river with no tributary (TribA -> TribAup) does NOT add depth, so
    # at max_depth=1 the whole first-confluence spine is still expanded.
    cat = build_catalog(branchy_rn, "CDE_STEM", max_depth=1)
    root = cat["root"]
    assert root["depth"] == 0
    triba = root["mainline"]
    assert triba["name"] == "TribA" and triba["depth"] == 1
    assert triba["mainline"]["name"] == "TribAup"  # free hop, still shown
    assert triba["mainline"]["depth"] == 1


def test_max_depth_zero_truncates_root_tributaries(branchy_rn):
    cat = build_catalog(branchy_rn, "CDE_STEM", max_depth=0)
    root = cat["root"]
    assert root["depth"] == 0
    assert root["mainline"] is None
    assert root["tributaries"] == []
    assert root.get("mainline_truncated") is True
    assert root.get("tributaries_truncated") == root["n_tributaries"]


# --------------------------------------------------------------------- svg path


def test_points_to_svg_path_fits_viewbox():
    square = [(0.0, 0.0), (100.0, 0.0), (100.0, 40.0), (0.0, 40.0), (0.0, 0.0)]
    d = _points_to_svg_path(square)
    assert d.startswith("M") and d.endswith("Z")
    nums = [
        float(tok)
        for tok in d.replace("M", " ").replace("L", " ").replace("Z", " ").split()
    ]
    xs, ys = nums[0::2], nums[1::2]
    assert min(xs) >= 0 and max(xs) <= 100
    assert min(ys) >= 0 and max(ys) <= 100
    # wider-than-tall input -> fills the x extent, centred in y
    assert max(xs) - min(xs) > max(ys) - min(ys)


def test_points_to_svg_path_orients_outlet_down():
    # a tall diamond centred at origin (metres, y up); outlet due south
    diamond = [(0.0, 10.0), (5.0, 0.0), (0.0, -10.0), (-5.0, 0.0), (0.0, 10.0)]
    d = _points_to_svg_path(diamond, outlet_xy=(0.0, -100.0))
    pts = [
        tuple(map(float, pair.split()))
        for pair in d.replace("M", "").replace("Z", "").split("L")
    ]
    lowest = max(pts, key=lambda p: p[1])  # largest y = bottom in SVG
    cx = sum(x for x, _ in pts) / len(pts)
    # the vertex nearest the outlet ends up at the bottom, roughly centred in x
    assert abs(lowest[0] - cx) < 15


# ------------------------------------------------------------------------- html


def test_catalog_to_html_is_self_contained(branchy_rn):
    cat = build_catalog(branchy_rn, "CDE_STEM")
    doc = catalog_to_html(cat)
    assert doc.lstrip().startswith("<!doctype html>")
    assert "http://" not in doc.replace('lang="en"', "")  # no external refs
    assert "https://" not in doc
    assert "<script" in doc and "</script>" in doc
    assert "TribA" in doc
    # the data payload is embedded for a downstream reader
    assert '<script id="catalog-data"' in doc


def test_catalog_to_html_is_a_collapsible_git_graph(branchy_rn):
    cat = build_catalog(branchy_rn, "CDE_STEM")
    doc = catalog_to_html(cat)
    # the graph is laid out in the browser: an empty <svg> + <ol> the script
    # fills, the catalog JSON, and the client renderer that walks the tree
    assert '<svg id="graph" class="graph"' in doc
    assert '<ol id="labels" class="labels">' in doc
    assert '<script id="catalog-data"' in doc
    assert "function layout(" in doc and "function drawGraph(" in doc
    # the order slider that folds low-order headwaters
    assert 'id="order" type="range"' in doc
    # every river's name is reachable from the embedded payload
    for node in walk(cat["root"]):
        if node.get("name"):
            assert node["name"] in doc


def test_catalog_to_html_passes_max_depth_to_the_client(branchy_rn):
    cat = build_catalog(branchy_rn, "CDE_STEM")
    assert '"MAX_DEPTH": null' in catalog_to_html(cat)
    assert '"MAX_DEPTH": 0' in catalog_to_html(cat, max_depth=0)


def test_catalog_to_html_has_selection_focus_controls(branchy_rn):
    cat = build_catalog(branchy_rn, "CDE_STEM")
    doc = catalog_to_html(cat)
    # the "fold around selection" toggle + a breadcrumb strip
    assert 'id="focus" type="checkbox"' in doc
    assert 'id="crumbs"' in doc
    # the client recomputes a root->selection spine and folds tributaries to it
    assert "function recomputeSelection(" in doc
    assert "spine.has(" in doc and "selBasin.has(" in doc


def test_catalog_to_html_escapes_names(simple_rn):
    cat = build_catalog(simple_rn, "Main")
    cat["meta"]["root_name"] = "A & B <em>x</em>"
    cat["root"]["name"] = "Tricky <script>x</script>"
    doc = catalog_to_html(cat)
    # the title/header run through html.escape
    assert "A &amp; B &lt;em&gt;x&lt;/em&gt;" in doc
    # a river name only reaches the page via the embedded JSON payload, whose
    # every "<" is neutralised so it cannot open a tag inside the <script>
    assert "<script>x</script>" not in doc
    assert "</script>x" not in doc
    assert "Tricky \\u003cscript>x\\u003c/script>" in doc


# ---------------------------------------------------------------------- geo block


def test_no_geo_by_default(branchy_rn):
    cat = build_catalog(branchy_rn, "CDE_STEM")
    assert "geo" not in cat
    assert cat["meta"]["has_geo"] is False


def test_geo_block_shape(branchy_rn):
    cat = build_catalog(branchy_rn, "CDE_STEM", geo=True)
    assert cat["meta"]["has_geo"] is True
    geo = cat["geo"]

    w, s, e, n = geo["bbox"]
    assert w < e and s < n

    tree_ids = {node["id"] for node in walk(cat["root"])}
    assert set(geo["rivers"]) <= tree_ids
    assert cat["root"]["id"] in geo["rivers"]

    for g in geo["rivers"].values():
        subs = g["line"]  # list of sub-lines, one per connected piece
        assert isinstance(subs, list) and subs
        for sub in subs:
            assert len(sub) >= 2
            assert all(len(pt) == 2 for pt in sub)
            assert all(w <= x <= e and s <= y <= n for x, y in sub)
        assert len(g["outlet"]) == 2


def test_geo_lines_are_simplified(branchy_rn):
    geo = build_catalog(branchy_rn, "CDE_STEM", geo=True)["geo"]
    from valleespyr.catalog import _GEO_MAX_VERTICES

    for g in geo["rivers"].values():
        assert all(len(sub) <= _GEO_MAX_VERTICES for sub in g["line"])


def test_geo_line_pieces_share_endpoints(branchy_rn):
    """When a river comes back as several sub-lines they still join up — every
    sub-line after the first starts where some other sub-line ends."""
    geo = build_catalog(branchy_rn, "CDE_STEM", geo=True)["geo"]
    for g in geo["rivers"].values():
        subs = g["line"]
        if len(subs) < 2:
            continue
        ends = {tuple(sub[0]) for sub in subs} | {tuple(sub[-1]) for sub in subs}
        for sub in subs:
            assert tuple(sub[0]) in ends and tuple(sub[-1]) in ends


def _valley_sized_bassins() -> gpd.GeoDataFrame:
    """One sub-basin per ``cours_d_eau`` in ``branchy_rn``, sized in real km².

    Squares in degrees near this fixture's own coordinates (lon ~0, lat
    ~42.8-42.9), scaled so the reprojected area lands where intended:
    ``CDE_A``/``CDE_AUP`` together read as one ~50 km² valley (inside the
    5-150 km² range a catchment mask is worth drawing for); ``CDE_STEM`` on
    its own is a tiny sliver (too small); the whole tree's total (via
    upstream_rivers from the root) comfortably exceeds 150 km² (too big).
    """
    from shapely.geometry import Polygon

    def square(cx: float, cy: float, side_deg: float) -> Polygon:
        h = side_deg / 2
        return Polygon(
            [(cx - h, cy - h), (cx + h, cy - h), (cx + h, cy + h), (cx - h, cy + h)]
        )

    # ~0.0744 deg side ~= 50 km^2 near lat 42.85 (see watershed tests for the
    # same idea at unit scale); split across A + AUP so their union lands there.
    rows = [
        ("CDE_A", square(-0.045, 42.91, 0.06)),
        ("CDE_AUP", square(-0.07, 42.93, 0.05)),
        ("CDE_B", square(-0.03, 42.865, 0.02)),  # tiny on its own
        ("CDE_C", square(-0.02, 42.845, 0.02)),
        ("CDE_D", square(0.02, 42.845, 0.02)),
        ("CDE_STEM2", square(0.03, 42.845, 0.02)),
        # CDE_STEM itself: a big square so the whole-tree union is way over
        # the valley range (this is the root; too big for a mask).
        ("CDE_STEM", square(0.0, 42.7, 1.5)),
    ]
    return gpd.GeoDataFrame(
        {
            "liens_vers_cours_d_eau_principal": [r[0] for r in rows],
            "geometry": [r[1] for r in rows],
        },
        crs="EPSG:4326",
    )


def test_valley_sized_river_gets_a_catchment_mask_polygon(branchy_rn):
    cat = build_catalog(
        branchy_rn, "CDE_STEM", bassins=_valley_sized_bassins(), geo=True
    )
    geo = cat["geo"]

    # find CDE_A's node to confirm it landed in the valley-size range
    a_node = next(n for n in walk(cat["root"]) if n["id"] == "CDE_A")
    assert a_node["area_km2"] is not None
    from valleespyr.catalog import _VALLEY_AREA_MAX_KM2, _VALLEY_AREA_MIN_KM2

    assert _VALLEY_AREA_MIN_KM2 <= a_node["area_km2"] <= _VALLEY_AREA_MAX_KM2

    catchment = geo["rivers"]["CDE_A"]["catchment"]
    assert catchment, "expected a catchment ring for a valley-sized river"
    for ring in catchment:
        assert len(ring) >= 4
        assert all(len(pt) == 2 for pt in ring)


def test_too_big_or_uncovered_rivers_have_no_catchment_mask(branchy_rn):
    cat = build_catalog(
        branchy_rn, "CDE_STEM", bassins=_valley_sized_bassins(), geo=True
    )
    geo = cat["geo"]
    # the root's own catchment is the whole tree's union -- way over the range
    root_id = cat["root"]["id"]
    assert geo["rivers"][root_id]["catchment"] is None


def test_no_bassins_means_no_catchment_mask(branchy_rn):
    geo = build_catalog(branchy_rn, "CDE_STEM", geo=True)["geo"]
    assert all(g["catchment"] is None for g in geo["rivers"].values())


def test_geo_html_gets_a_map(branchy_rn):
    cat = build_catalog(branchy_rn, "CDE_STEM", geo=True)
    doc = catalog_to_html(cat)
    assert 'id="map"' in doc and 'class="mapcol"' in doc
    assert "has-geo" in doc  # JS opts the body into click-to-map
    # the map draws real IGN/OSM basemap tiles, so (unlike the rest of the
    # page) this is the one part that needs network access — Leaflet plus
    # the tile layers are pulled from public hosts
    assert "leaflet" in doc.lower()
    assert "data.geopf.fr" in doc
    # no map column without a geo block, and a plain catalog stays offline
    plain_cat = build_catalog(branchy_rn, "CDE_STEM")
    plain = catalog_to_html(plain_cat)
    assert 'id="map"' not in plain
    assert "https://" not in plain


def test_map_line_width_scales_with_strahler_order(branchy_rn):
    """The mini-map ships a mapWidth(order) helper and feeds each drawn line its
    river's Strahler order, so a trunk renders heavier than a headwater."""
    doc = catalog_to_html(build_catalog(branchy_rn, "CDE_STEM", geo=True))
    assert "function mapWidth(order" in doc
    # context, upstream and selected lines all get a per-order stroke-width
    assert "mapWidth((node[id] || {}).strahler)" in doc
    assert "mapWidth((node[uid] || {}).strahler, 1.25)" in doc
    assert "mapWidth((node[id] || {}).strahler, 1.7)" in doc


def test_map_ships_the_valley_mask_function(branchy_rn):
    """The client ships paintMask(id): draws a world-covering polygon with the
    river's own catchment ring cut out as a hole (even-odd fill), so a
    valley-sized selection dims everything outside it."""
    doc = catalog_to_html(build_catalog(branchy_rn, "CDE_STEM", geo=True))
    assert "function paintMask(id)" in doc
    assert "fillRule: 'evenodd'" in doc
    assert "paintMask(id);" in doc  # called from paintMap on every selection


# ------------------------------------------------------------------ offline sample


@pytest.mark.skipif(not SAMPLE.exists(), reason="gavarnie tronçon sample not present")
def test_gavarnie_catalog_end_to_end():
    from valleespyr.hydro.network import load_troncons

    rn = build_river_network(build_graph(load_troncons(SAMPLE)))
    cat = build_catalog(rn, "Gave de Pau", max_depth=8)

    root = cat["root"]
    assert root["name"] == "Gave de Pau"
    assert cat["meta"]["n_nodes"] > 100
    assert cat["meta"]["model"] == "git"

    # the root's tributaries are ordered biggest-catchment first
    ups = [t["n_upstream"] for t in root["tributaries"]]
    assert ups == sorted(ups, reverse=True)

    # walking the mainline from the root stays on the trunk down to a headwater
    trunk = spine(root)
    assert len(trunk) >= 2
    assert trunk[-1]["is_headwater"] is True

    by_name = {n["name"]: n for n in walk(root) if n["name"]}
    assert "Gave de Héas" in by_name
    assert by_name["Gave de Héas"]["pfafstetter"]

    doc = catalog_to_html(cat)
    assert "Gave de Héas" in doc
