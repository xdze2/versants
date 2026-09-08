"""Streamlit navigator for the Pyrénées river graph.

Run it with::

    streamlit run src/valleespyr/app.py   # or: valleespyr-app

Loads a local ``troncon_hydrographique`` dump (``valleespyr wfs dump``), rolls the
segments up into whole rivers and lets you walk the "flows into" graph: pick a
river to map its course and catchment, click through its tributaries, export
either as GeoJSON. A ``bassin_versant_topographique`` dump alongside adds the
real drainage-area polygon.

Everything after the initial load is graph work on the in-memory
``RiverNetwork`` — no further network calls. The graph is cached per dump file.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd
import pydeck as pdk
import streamlit as st
from pydeck.data_utils import compute_view

from valleespyr.hydro.rivers import RiverNetwork, build_river_network

# Offline stream-network dumps, most specific first.
TRONCON_CANDIDATES = [
    Path("data/raw/troncon_hydrographique_pyrenees.parquet"),
    Path("data/raw/troncon_hydrographique_gavarnie_sample.geojson"),
]
# Topographic sub-basin dumps for the real catchment-area polygon, optional.
BASSIN_CANDIDATES = [
    Path("data/raw/bassin_versant_topographique_pyrenees.parquet"),
    Path("data/raw/bassin_versant_topographique_fr.parquet"),
]
# Pyrénées chain, lon/lat — shown in the "dump it yourself" hint.
PYRENEES_BBOX = (-2.0, 42.3, 3.2, 43.4)

COURSE_COLOR = [255, 90, 0, 235]  # the selected river itself
TRIB_COLOR = [70, 130, 180, 220]  # everything else in its catchment
PICKED_TRIB_COLOR = [255, 190, 60, 240]  # a tributary hovered/selected in the list
AREA_FILL = [90, 150, 200, 55]  # translucent catchment-area wash (real sub-basin union)
AREA_LINE = [60, 110, 160, 190]  # its outline


# --------------------------------------------------------------------------- data

# Part of the cache key (a plain name, not `_version` — Streamlit excludes
# leading-underscore args from hashing). Bump it when the roll-up / catchment
# logic changes so a running server drops its cached RiverNetwork.
_GRAPH_CACHE_VERSION = 2


@st.cache_resource(show_spinner="Building river graph…")
def load_network(path_str: str, version: int = _GRAPH_CACHE_VERSION) -> RiverNetwork:
    """Load a tronçon dump and roll it up into a :class:`RiverNetwork` (cached per file)."""
    from valleespyr.hydro.network import build_graph, load_troncons

    gdf = load_troncons(path_str)
    return build_river_network(build_graph(gdf))


@st.cache_data(show_spinner=False)
def summary_frame(path_str: str, version: int = _GRAPH_CACHE_VERSION) -> pd.DataFrame:
    """One row per river for the picker / table (longest first)."""
    rn = load_network(path_str)
    return pd.DataFrame(rn.summary())


@st.cache_resource(show_spinner="Loading sub-basins…")
def load_bassins_cached(path_str: str):
    """Local ``bassin_versant_topographique`` dump for the catchment-area polygon."""
    from valleespyr.watershed import load_bassins

    return load_bassins(path_str)


@st.cache_data(show_spinner=False)
def _catchment_area(rn_key: str, river_id: str, bassins_path: str | None):
    """(polygon, area_km2, n_sub_basins) for a river's catchment, or (None, 0, 0).

    Dissolves the ``bassin_versant_topographique`` sub-basins whose principal
    watercourse is this river or any river upstream of it. ``rn_key`` is the
    tronçon dump path — it keys the cache to the same graph the rest of the app
    is showing.
    """
    if not bassins_path:
        return None, 0.0, 0
    import geopandas as gpd

    from valleespyr.watershed import BASSIN_COURS_D_EAU, catchment_polygon

    rn = load_network(rn_key)
    river = rn.get(river_id)
    if river is None:
        return None, 0.0, 0
    ids = {river.id} | {r.id for r in rn.upstream_rivers(river.id)}
    bassins = load_bassins_cached(bassins_path)
    poly = catchment_polygon(bassins, ids)
    if poly is None:
        return None, 0.0, 0
    n = int(bassins[BASSIN_COURS_D_EAU].isin(ids).sum())
    area_km2 = gpd.GeoSeries([poly], crs="EPSG:4326").to_crs("EPSG:2154").area.iloc[0] / 1e6
    return poly, area_km2, n


# ---------------------------------------------------------------------------- map


def _fc_bounds(fc: dict) -> tuple[float, float, float, float] | None:
    xs: list[float] = []
    ys: list[float] = []

    def walk(coords):
        if not coords:
            return
        if isinstance(coords[0], (int, float)):
            xs.append(coords[0])
            ys.append(coords[1])
        else:
            for c in coords:
                walk(c)

    for feat in fc["features"]:
        walk(feat["geometry"].get("coordinates"))
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _catchment_area_feature(polygon) -> dict | None:
    """Wrap a dissolved catchment polygon (shapely) as a GeoJSON ``Feature``.

    ``None`` when there is no polygon: no sub-basin dump loaded, or none of the
    catchment's watercourses appears in it.
    """
    if polygon is None or polygon.is_empty:
        return None
    from shapely.geometry import mapping

    return {"type": "Feature", "geometry": mapping(polygon), "properties": {}}


def river_deck(
    course_fc: dict,
    catchment_fc: dict,
    *,
    picked_river_id: str | None = None,
    segment_to_river: dict[str, str] | None = None,
    catchment_area=None,
) -> pdk.Deck:
    """Catchment area wash + stream network (pickable) under the selected course.

    Layers, bottom to top: the real catchment-area polygon (dissolved
    ``bassin_versant_topographique`` sub-basins, passed in as ``catchment_area``
    — a shapely geometry or ``None``), the catchment stream lines (width scaled
    by Strahler order), then the selected river's own course. Each catchment
    feature carries ``river_id`` (looked up from its ``cleabs``) so a click can
    resolve to a river.
    """
    s2r = segment_to_river or {}
    layers: list[pdk.Layer] = []

    area = _catchment_area_feature(catchment_area)
    if area is not None:
        layers.append(
            pdk.Layer(
                "GeoJsonLayer",
                {"type": "FeatureCollection", "features": [area]},
                id="catchment_area",
                stroked=True,
                filled=True,
                get_fill_color=AREA_FILL,
                get_line_color=AREA_LINE,
                line_width_min_pixels=1.5,
                pickable=False,
            )
        )

    cat_feats = []
    for feat in catchment_fc["features"]:
        cid = feat["properties"].get("cleabs")
        rid = s2r.get(cid)
        order = feat["properties"].get("order") or 1
        color = PICKED_TRIB_COLOR if (rid and rid == picked_river_id) else TRIB_COLOR
        cat_feats.append(
            {
                "type": "Feature",
                "geometry": feat["geometry"],
                "properties": {
                    "cleabs": cid,
                    "river_id": rid or "",
                    "toponyme": feat["properties"].get("toponyme") or "",
                    "order": order,
                    "color": color,
                    # thicker trunks, thin headwaters
                    "width": 1.0 + 0.9 * float(order),
                },
            }
        )
    layers.append(
        pdk.Layer(
            "GeoJsonLayer",
            {"type": "FeatureCollection", "features": cat_feats},
            id="catchment",
            stroked=True,
            filled=False,
            get_line_color="properties.color",
            get_line_width="properties.width",
            line_width_units="pixels",
            line_width_min_pixels=1.2,
            pickable=True,
            auto_highlight=True,
        )
    )
    layers.append(
        pdk.Layer(
            "GeoJsonLayer",
            course_fc,
            id="course",
            stroked=True,
            filled=False,
            get_line_color=COURSE_COLOR,
            line_width_min_pixels=3.5,
            pickable=False,
        )
    )

    # Frame to the widest thing on the map: the area polygon if we have one
    # (it covers the interfluves the stream lines don't), else the lines.
    box = None
    if area is not None:
        box = _fc_bounds({"type": "FeatureCollection", "features": [area]})
    minx, miny, maxx, maxy = (
        box or _fc_bounds(catchment_fc) or _fc_bounds(course_fc) or PYRENEES_BBOX
    )
    view = compute_view([[minx, miny], [maxx, maxy], [minx, maxy], [maxx, miny]])
    return pdk.Deck(
        layers=layers,
        initial_view_state=view,
        # An explicit CARTO style (no token needed). Do NOT pass ``map_style=None``
        # here: with no base map the deck.gl view never reaches "ready" and
        # Streamlit renders it at its default world view (lat 0/lon 0/zoom 1),
        # ignoring ``initial_view_state`` — which is why every basin showed up
        # tiny and off near the Mediterranean.
        map_style="light",
        map_provider="carto",
        tooltip={"text": "{toponyme}\n{cleabs}"},
    )


def river_figure(
    rn: RiverNetwork,
    river_id: str,
    *,
    catchment_area=None,
    area_km2: float = 0.0,
    n_sub_basins: int = 0,
):
    """Static matplotlib map of a river: catchment wash, tributaries, own course.

    The print/export counterpart to :func:`river_deck` — no base map, no picking,
    equal-aspect lon/lat axes. Returns a matplotlib ``Figure``, or ``None`` if the
    river has no geometry.
    """
    import geopandas as gpd
    import matplotlib.pyplot as plt

    river = rn.get(river_id)
    if river is None:
        return None
    course_fc = rn.river_path_geojson(river_id)
    catchment_fc = rn.river_catchment_geojson(river_id)
    if not course_fc["features"] and not catchment_fc["features"]:
        return None

    fig, ax = plt.subplots(figsize=(7, 8))
    if catchment_area is not None and not catchment_area.is_empty:
        gpd.GeoSeries([catchment_area], crs="EPSG:4326").plot(
            ax=ax, color="lightsteelblue", edgecolor="steelblue", linewidth=0.8, zorder=1
        )
    if catchment_fc["features"]:
        gpd.GeoDataFrame.from_features(catchment_fc["features"], crs="EPSG:4326").plot(
            ax=ax, color="tab:blue", linewidth=0.7, zorder=2
        )
    if course_fc["features"]:
        gpd.GeoDataFrame.from_features(course_fc["features"], crs="EPSG:4326").plot(
            ax=ax, color="orangered", linewidth=2.5, zorder=3
        )

    title = river.name or river.id
    if n_sub_basins:
        title += f" — {area_km2:,.0f} km², {n_sub_basins} sub-basin"
        title += "" if n_sub_basins == 1 else "s"
    ax.set_title(title)
    # lon/lat degrees are not equal lengths; ~1/cos(lat) keeps the shape honest.
    ax.set_aspect(1 / math.cos(math.radians(ax.get_ylim()[0] or 42.8)))
    fig.tight_layout()
    return fig


def _figure_png(fig) -> bytes:
    """Render a matplotlib figure to PNG bytes for the download button."""
    import io

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    return buf.getvalue()


def _picked_river(event, s2r: dict[str, str]) -> str | None:
    """River id for a feature clicked on the catchment layer, or None."""
    if not event or not getattr(event, "selection", None):
        return None
    objs = (event.selection.get("objects", {}) or {}).get("catchment", [])
    for o in objs:
        props = o.get("properties", o)
        rid = props.get("river_id")
        if rid:
            return rid
        cid = props.get("cleabs")
        if cid and cid in s2r:
            return s2r[cid]
    return None


# ---------------------------------------------------------------------- tree text


def _catchment_tree_lines(rn: RiverNetwork, river_id: str, max_depth: int = 4):
    """Indented ``├──`` lines of the river's tributary tree, biggest child first."""
    root = rn.get(river_id)
    if root is None:
        return

    def walk(rid: str, prefix: str, is_last: bool, depth: int):
        r = rn.get(rid)
        if r is None:
            return
        name = r.name or r.id
        head = f"{name}  ({r.length_m / 1000:.1f} km"
        if r.max_order is not None:
            head += f", ord {r.max_order}"
        head += ")"
        if depth == 0:
            yield head
        else:
            yield f"{prefix}{'└── ' if is_last else '├── '}{head}"
        kids = rn.children(rid)
        if depth >= max_depth:
            if kids:
                child_prefix = prefix + ("    " if is_last else "│   ")
                total = len(rn.upstream_rivers(rid))
                yield f"{child_prefix}… {len(kids)} tribut/ {total} rivers upstream"
            return
        for i, c in enumerate(kids):
            last = i == len(kids) - 1
            child_prefix = prefix + ("" if depth == 0 else ("    " if is_last else "│   "))
            yield from walk(c.id, child_prefix, last, depth + 1)

    yield from walk(river_id, "", True, 0)


# --------------------------------------------------------------------------- main


def main() -> None:
    st.set_page_config(page_title="valleespyr — river navigator", layout="wide")
    st.title("Pyrénées rivers — catchment navigator")

    path_str = _sidebar_source()
    if path_str is None:
        st.stop()

    rn = load_network(path_str)
    summ = summary_frame(path_str)

    st.sidebar.markdown("---")
    st.sidebar.caption(
        f"{len(rn)} rivers · {sum(1 for r in rn if r.name)} named · "
        f"{len(rn.roots())} roots · {len(rn.leaves())} leaves"
    )

    ss = st.session_state
    ss.setdefault("river_id", None)
    ss.setdefault("picked_trib", None)

    _sidebar_picker(rn, summ)
    bassins_path = _sidebar_bassins()

    if ss.river_id is None or ss.river_id not in rn.rivers:
        _landing(rn, summ)
        return

    _river_view(rn, path_str, bassins_path)


def _landing(rn: RiverNetwork, summ: pd.DataFrame) -> None:
    st.info("Pick a river from the sidebar, or a root basin below, to start navigating.")
    roots = summ[summ["is_root"]].sort_values("length_km", ascending=False)
    st.subheader(f"{len(roots)} root basins (nothing downstream in this dump)")
    show = roots[["name", "id", "length_km", "max_order", "n_children"]].head(40)
    event = st.dataframe(
        show,
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
    )
    if event and event.selection and event.selection.rows:
        st.session_state.river_id = show.iloc[event.selection.rows[0]]["id"]
        st.session_state.picked_trib = None
        st.rerun()


def _river_view(rn: RiverNetwork, path_str: str, bassins_path: str | None = None) -> None:
    ss = st.session_state
    river = rn.get(ss.river_id)

    # ---- breadcrumb: outlet ... -> this river -------------------------------
    chain = list(reversed(rn.downstream_path(river.id)))  # root ... -> river
    crumbs = st.columns(len(chain) + 1)
    for col, r in zip(crumbs, chain, strict=False):
        label = (r.name or f"…{r.id[-6:]}") + (" ▸" if r.id != river.id else "")
        if col.button(label, key=f"crumb_{r.id}", disabled=(r.id == river.id)):
            ss.river_id = r.id
            ss.picked_trib = None
            st.rerun()
    if crumbs[len(chain)].button("✕ clear", key="crumb_clear"):
        ss.river_id = ss.picked_trib = None
        st.rerun()

    # ---- header facts -----------------------------------------------------------
    parent = rn.parent(river.id)
    state = "root" if river.is_root else "leaf" if river.is_leaf else "interior"
    st.subheader(f"{river.name or '(unnamed watercourse)'}")
    c = st.columns(5)
    c[0].metric("Length", f"{river.length_m / 1000:.1f} km")
    c[1].metric("Strahler order", river.max_order or "—")
    c[2].metric("Direct tributaries", len(river.child_ids))
    c[3].metric("Rivers in catchment", len(rn.upstream_rivers(river.id)))
    c[4].metric("State", state)
    if parent is not None:
        if st.button(f"↓ downstream into **{parent.name or parent.id}**"):
            ss.river_id = parent.id
            ss.picked_trib = None
            st.rerun()
    else:
        st.caption("Outlet leaves the loaded network — dump a wider bbox to go further downstream.")

    course_fc = rn.river_path_geojson(river.id)
    catchment_fc = rn.river_catchment_geojson(river.id)
    area_poly, area_km2, area_covered = _catchment_area(path_str, river.id, bassins_path)

    if area_poly is not None:
        note = (
            ""
            if river.is_root
            else " — includes this watercourse's reaches downstream of the loaded network"
        )
        basins = f"{area_covered} sub-basin" + ("" if area_covered == 1 else "s")
        st.caption(
            f"Catchment area ≈ **{area_km2:,.0f} km²** "
            f"({basins}, BD Carthage topographic watersheds){note}."
        )
    elif bassins_path:
        st.caption(
            "No topographic sub-basin covers this watercourse in the loaded "
            "`bassin_versant_topographique` dump — showing the stream network only."
        )

    show_panel = st.sidebar.toggle(
        "Show info panel", value=True, key="show_panel", help="Collapse it for a full-width map."
    )
    static_map = st.sidebar.toggle(
        "Static map",
        value=False,
        key="static_map",
        help="Plain matplotlib figure — no base map or picking, but it prints and exports cleanly.",
    )

    left, right = st.columns([3, 2], gap="medium") if show_panel else (st.container(), None)
    map_height = 620 if show_panel else 800

    with left:
        if static_map:
            fig = river_figure(
                rn,
                river.id,
                catchment_area=area_poly,
                area_km2=area_km2,
                n_sub_basins=area_covered,
            )
            if fig is None:
                st.info("No geometry to draw for this river.")
            else:
                st.pyplot(fig, width="stretch")
                st.download_button(
                    "Download PNG",
                    _figure_png(fig),
                    file_name=f"{(river.name or river.id).replace(' ', '_')}.png",
                    mime="image/png",
                )
            st.caption(
                f"Orange = {river.name or 'this river'}'s course · blue = its catchment "
                "stream network · pale blue = drainage area. Switch off *Static map* "
                "in the sidebar to pick tributaries on the interactive map."
            )
        else:
            deck = river_deck(
                course_fc,
                catchment_fc,
                picked_river_id=ss.picked_trib,
                segment_to_river=rn.segment_to_river,
                catchment_area=area_poly,
            )
            event = st.pydeck_chart(
                deck,
                width="stretch",
                height=map_height,
                on_select="rerun",
                selection_mode="single-object",
                # Re-key per river so the widget remounts and actually applies the
                # new initial_view_state — pydeck-in-Streamlit keeps the old camera
                # across plain reruns, which left every basin framed at Europe zoom.
                key=f"deck_{river.id}",
            )
            hit = _picked_river(event, rn.segment_to_river)
            if hit and hit != ss.picked_trib:
                ss.picked_trib = hit
                st.rerun()
            wash = " · pale-blue wash = drainage area" if area_poly is not None else ""
            st.caption(
                f"Orange = {river.name or 'this river'}'s course · blue = its catchment "
                f"stream network{wash} · click any blue reach to select that tributary."
            )

    if right is not None:
        with right:
            _info_panel(rn, river)

    _downloads(river, course_fc, catchment_fc)


def _info_panel(rn: RiverNetwork, river) -> None:
    """The right-hand column: tributary table + catchment tree text."""
    ss = st.session_state
    tribs = rn.children(river.id)
    st.markdown(f"**Tributaries ({len(tribs)})** — biggest first")
    if tribs:
        trib_df = pd.DataFrame(
            {
                "name": [t.name or t.id for t in tribs],
                "km": [round(t.length_m / 1000, 1) for t in tribs],
                "ord": [t.max_order for t in tribs],
                "upstream": [len(rn.upstream_rivers(t.id)) for t in tribs],
                "id": [t.id for t in tribs],
            }
        )
        ev = st.dataframe(
            trib_df.drop(columns="id"),
            width="stretch",
            height=280,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key="trib_table",
        )
        rows = ev.selection.rows if ev and ev.selection else []
        if rows:
            ss.picked_trib = trib_df.iloc[rows[0]]["id"]
        if ss.picked_trib and ss.picked_trib in rn.rivers:
            pt = rn.get(ss.picked_trib)
            b1, b2 = st.columns(2)
            if b1.button(f"▸ enter {pt.name or pt.id}", type="primary"):
                ss.river_id = pt.id
                ss.picked_trib = None
                st.rerun()
            n_up = len(rn.upstream_rivers(pt.id))
            b2.caption(f"{pt.length_m / 1000:.1f} km · {n_up} upstream")
    else:
        st.caption("Leaf river — no tributaries.")

    st.markdown("**Catchment tree**")
    st.text("\n".join(_catchment_tree_lines(rn, river.id)))


# --------------------------------------------------------------------------- sidebar


def _sidebar_source() -> str | None:
    st.sidebar.header("Stream-network dump")
    found = next((p for p in TRONCON_CANDIDATES if p.exists()), None)
    default = str(found) if found else str(TRONCON_CANDIDATES[0])
    path_str = st.sidebar.text_input("tronçon GeoJSON / GeoParquet", value=default)
    if not Path(path_str).exists():
        bbox = ",".join(str(x) for x in PYRENEES_BBOX)
        st.warning(
            f"`{path_str}` not found. Dump the layer first, e.g.\n\n"
            "```\nvalleespyr wfs dump --layer BDTOPO_V3:troncon_hydrographique \\\n"
            f"    --bbox {bbox} -o data/raw/troncon_hydrographique_pyrenees.parquet\n```\n"
            "or the smaller Gavarnie sample bbox `-0.10,42.65,0.15,42.85`."
        )
        return None
    return path_str


def _sidebar_bassins() -> str | None:
    """Path to a local ``bassin_versant_topographique`` dump, or ``None``.

    Optional: without it the map shows the stream network only. With it, the real
    catchment-area polygon (dissolved sub-basins) is shaded under each river.
    """
    st.sidebar.markdown("---")
    st.sidebar.header("Catchment-area polygon")
    found = next((p for p in BASSIN_CANDIDATES if p.exists()), None)
    default = str(found) if found else ""
    path_str = st.sidebar.text_input(
        "bassin_versant_topographique dump (optional)",
        value=default,
        help="Dump it with:  valleespyr wfs dump --layer "
        "BDTOPO_V3:bassin_versant_topographique --bbox <pyrenees> -o "
        "data/raw/bassin_versant_topographique_pyrenees.parquet",
    )
    path_str = path_str.strip()
    if not path_str:
        return None
    if not Path(path_str).exists():
        st.sidebar.warning(f"`{path_str}` not found — showing stream lines only.")
        return None
    return path_str


def _sidebar_picker(rn: RiverNetwork, summ: pd.DataFrame) -> None:
    st.sidebar.header("Go to river")
    named = summ[summ["name"].notna()].copy()
    only_roots = st.sidebar.checkbox("Roots only", value=False)
    if only_roots:
        named = named[named["is_root"]]
    q = st.sidebar.text_input("name contains")
    if q:
        named = named[named["name"].str.contains(q, case=False, na=False)]
    named = named.sort_values("length_km", ascending=False)

    options = named["id"].tolist()
    labels = {r.id: f"{r['name']}  ({r['length_km']:.0f} km)" for _, r in named.iterrows()}
    choice = st.sidebar.selectbox(
        f"{len(options)} match",
        options=options,
        format_func=lambda i: labels.get(i, i),
        index=None,
        placeholder="select…",
    )
    if choice is not None and choice != st.session_state.get("river_id"):
        st.session_state.river_id = choice
        st.session_state.picked_trib = None
        st.rerun()


def _downloads(river, course_fc: dict, catchment_fc: dict) -> None:
    st.sidebar.markdown("---")
    st.sidebar.header("Download")
    stem = (river.name or river.id).replace(" ", "_").replace("'", "")
    st.sidebar.download_button(
        f"Course — {len(course_fc['features'])} lines",
        json.dumps(course_fc),
        file_name=f"{stem}_course.geojson",
        mime="application/geo+json",
    )
    st.sidebar.download_button(
        f"Catchment — {len(catchment_fc['features'])} lines",
        json.dumps(catchment_fc),
        file_name=f"{stem}_catchment.geojson",
        mime="application/geo+json",
    )


def _run_streamlit() -> None:
    """Entry point for the ``valleespyr-app`` script: re-exec under `streamlit run`."""
    import sys

    from streamlit.web import cli as stcli

    sys.argv = ["streamlit", "run", __file__, *sys.argv[1:]]
    sys.exit(stcli.main())


if __name__ == "__main__":
    main()
