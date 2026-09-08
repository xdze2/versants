"""Streamlit explorer for BD TOPO topographic watersheds.

Run it with::

    streamlit run src/valleespyr/app.py
    # or, after `pip install -e '.[app]'`:
    valleespyr-app

It reads a local GeoParquet/GeoJSON dump (see ``valleespyr wfs dump``) and, if
none is found, offers to pull a bounding box live from the WFS. Everything else
— filtering, the map, the dissolve-by-watercourse view, downloads — runs on the
in-memory GeoDataFrame with no further network calls.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pydeck as pdk
import streamlit as st

from valleespyr.sources.wfs import GEOPLATEFORME_WFS, LAYER_BASSIN_VERSANT, WFSClient
from valleespyr.watershed import fetch_watersheds_bbox

# Default local dumps, most specific first.
DATA_CANDIDATES = [
    Path("data/raw/bassin_versant_topographique_pyrenees.parquet"),
    Path("data/raw/bassin_versant_topographique_fr.parquet"),
]
# Pyrénées chain, lon/lat — the default live-fetch extent.
PYRENEES_BBOX = (-2.0, 42.3, 3.2, 43.4)

# Attribute columns worth showing in the table (skip the all-null / internal ones).
TABLE_COLS = [
    "toponyme",
    "code_hydrographique",
    "code_bdcarthage",
    "libelle_du_bassin_hydrographique",
    "liens_vers_cours_d_eau_principal",
    "cleabs",
    "area_km2",
]


# --------------------------------------------------------------------------- data


@st.cache_data(show_spinner="Reading local dump…")
def load_local(path_str: str) -> gpd.GeoDataFrame:
    gdf = gpd.read_parquet(path_str) if path_str.endswith(".parquet") else gpd.read_file(path_str)
    return _prepare(gdf)


@st.cache_data(show_spinner="Fetching from WFS…")
def load_wfs(
    endpoint: str, layer: str, bbox: tuple[float, float, float, float]
) -> gpd.GeoDataFrame:
    fc = fetch_watersheds_bbox(
        WFSClient(endpoint=endpoint), bbox, layer=layer, srs_name="EPSG:4326"
    )
    gdf = gpd.GeoDataFrame.from_features(fc["features"], crs="EPSG:4326")
    return _prepare(gdf)


def _prepare(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Common post-load: WGS84, an area column, a stable index."""
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    gdf = gdf.to_crs("EPSG:4326")
    gdf["area_km2"] = gdf.to_crs("EPSG:2154").area / 1e6
    gdf = gdf.reset_index(drop=True)
    return gdf


WATERCOURSE_KEY = "liens_vers_cours_d_eau_principal"

# "Le Gave de Pau du confluent de X au confluent de Y" -> "Gave de Pau"
# Cut everything from the " du/de/des ... confluent/source ..." span-description clause,
# then drop a leading definite article so groups collapse ("Le Gave" / "La Gave" -> one).
_LEADING_ART = re.compile(r"^(?:l['’]|les |le |la )", re.IGNORECASE)
_FROM_CLAUSE = re.compile(
    r"\s+(?:de|du|des)\s+(?:sa source|son confluent|confluent)\b.*$", re.IGNORECASE
)


def river_name(toponyme: str) -> str:
    """Best-effort readable watercourse name from a sub-catchment toponyme."""
    if not toponyme:
        return "(sans nom)"
    body = _FROM_CLAUSE.sub("", toponyme.strip())
    if body == toponyme.strip():  # no span clause matched -> leave the name untouched
        return toponyme.strip()
    name = _LEADING_ART.sub("", body).strip(" .")
    return name[:1].upper() + name[1:] if name else toponyme


def dissolve_by_watercourse(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """One row per ``liens_vers_cours_d_eau_principal`` — the whole-watercourse catchment."""
    key = WATERCOURSE_KEY
    names = (
        gdf.assign(_name=gdf["toponyme"].map(river_name))
        .groupby(key)["_name"]
        .agg(lambda s: s.mode().iat[0] if not s.mode().empty else s.iat[0])
    )
    agg = gdf.dissolve(
        by=key,
        aggfunc={"code_hydrographique": "count", "area_km2": "sum"},
    ).rename(columns={"code_hydrographique": "n_subcatchments"})
    agg["watercourse"] = names
    return agg.reset_index()


# ---------------------------------------------------------------------------- map


def _fill_for(idx: int, selected: set[int]) -> list[int]:
    return [255, 140, 0, 160] if idx in selected else [70, 130, 180, 70]


def _zoom_for_bounds(minx: float, miny: float, maxx: float, maxy: float) -> float:
    """Rough web-mercator zoom that fits a lon/lat box (assumes a ~900px map)."""
    span = max(maxx - minx, (maxy - miny) * 1.6, 1e-4)
    import math

    return max(3.0, min(13.0, math.log2(360.0 / span) + 0.2))


def deck_for(
    gdf: gpd.GeoDataFrame,
    selected_ids: set[int],
    *,
    layer_id: str = "watersheds",
    label_col: str | None = None,
    fit_bounds: tuple[float, float, float, float] | None = None,
) -> pdk.Deck:
    if label_col is None:
        label_col = "watercourse" if "watercourse" in gdf.columns else (
            "toponyme" if "toponyme" in gdf.columns else WATERCOURSE_KEY
        )
    features = []
    for idx, row in gdf.iterrows():
        features.append(
            {
                "type": "Feature",
                "geometry": row.geometry.__geo_interface__,
                "properties": {
                    "pick_id": int(idx),
                    "label": str(row.get(label_col, "")),
                    "code_hydrographique": str(row.get("code_hydrographique", "")),
                    "area_km2": round(float(row.get("area_km2", 0.0)), 1),
                    "fill": _fill_for(idx, selected_ids),
                },
            }
        )
    layer = pdk.Layer(
        "GeoJsonLayer",
        {"type": "FeatureCollection", "features": features},
        id=layer_id,
        stroked=True,
        filled=True,
        get_fill_color="properties.fill",
        get_line_color=[40, 40, 40, 200],
        line_width_min_pixels=0.5,
        pickable=True,
        auto_highlight=True,
    )
    box = fit_bounds if fit_bounds is not None else tuple(gdf.total_bounds)
    minx, miny, maxx, maxy = box
    view = pdk.ViewState(
        longitude=(minx + maxx) / 2,
        latitude=(miny + maxy) / 2,
        zoom=_zoom_for_bounds(minx, miny, maxx, maxy),
    )
    return pdk.Deck(
        layers=[layer],
        initial_view_state=view,
        map_style=None,
        tooltip={"text": "{label}\n{code_hydrographique} — {area_km2} km²"},
    )


def _picked_ids(event, layer_id: str) -> set[int]:
    """Positional row indices of features clicked on the map for a given layer id.

    Prefers deck.gl's ``indices`` (already row positions in the layer data);
    falls back to the ``pick_id`` property embedded in each feature.
    """
    if not event or not getattr(event, "selection", None):
        return set()
    sel = event.selection
    idx = (sel.get("indices", {}) or {}).get(layer_id, [])
    if idx:
        return {int(i) for i in idx}
    out: set[int] = set()
    for o in (sel.get("objects", {}) or {}).get(layer_id, []):
        pid = (o.get("properties", {}) or o).get("pick_id")
        if pid is not None:
            out.add(int(pid))
    return out


# --------------------------------------------------------------------------- main


def main() -> None:
    st.set_page_config(page_title="valleespyr — watershed explorer", layout="wide")
    st.title("Pyrénées topographic watersheds — BD TOPO explorer")

    view_mode = st.sidebar.radio(
        "View",
        [
            "Drill-down map",
            "Upstream trace (streams)",
            "Sub-catchments",
            "Dissolved by watercourse",
        ],
        index=0,
    )
    st.sidebar.markdown("---")

    # The streams view uses the tronçon layer, not the watershed dump.
    if view_mode == "Upstream trace (streams)":
        _upstream_trace()
        return

    src = _sidebar_source()
    if src is None:
        st.stop()
    gdf = src

    if view_mode == "Drill-down map":
        _drilldown(gdf)
        return

    work = dissolve_by_watercourse(gdf) if view_mode.startswith("Dissolved") else gdf

    work = _sidebar_filters(work)

    left, right = st.columns([3, 2], gap="medium")

    with right:
        st.caption(f"{len(work)} feature(s)")
        show_cols = [c for c in TABLE_COLS if c in work.columns]
        if "n_subcatchments" in work.columns:
            show_cols = ["liens_vers_cours_d_eau_principal", "n_subcatchments", "area_km2"]
        table = work[show_cols].copy()
        if "area_km2" in table:
            table["area_km2"] = table["area_km2"].round(1)
        event = st.dataframe(
            table,
            width="stretch",
            height=520,
            on_select="rerun",
            selection_mode="multi-row",
        )
        selected_rows = set(event.selection.rows) if event and event.selection else set()
        selected_ids = {work.index[i] for i in selected_rows if i < len(work)}

    with left:
        if len(work):
            st.pydeck_chart(deck_for(work, selected_ids), width="stretch")
        else:
            st.info("No features match the current filters.")

    _downloads(work, selected_ids)


# ---------------------------------------------------------------------- drilldown


def _pad(box: tuple[float, float, float, float], frac: float = 0.12):
    minx, miny, maxx, maxy = box
    dx = (maxx - minx) * frac or 0.01
    dy = (maxy - miny) * frac or 0.01
    return (minx - dx, miny - dy, maxx + dx, maxy + dy)


def _drilldown(gdf: gpd.GeoDataFrame) -> None:
    """Two-level map: whole-watercourse catchments → click one → its sub-catchments."""
    ss = st.session_state
    ss.setdefault("dd_course", None)  # selected watercourse FK, or None at top level
    ss.setdefault("dd_sub", None)  # selected sub-catchment cleabs within a course

    courses = dissolve_by_watercourse(gdf)

    # ---- Level 1: a watercourse is selected -> show its sub-catchments ----
    if ss.dd_course is not None and ss.dd_course in set(courses[WATERCOURSE_KEY]):
        crow = courses.loc[courses[WATERCOURSE_KEY] == ss.dd_course].iloc[0]
        subs = gdf[gdf[WATERCOURSE_KEY] == ss.dd_course].reset_index(drop=True)

        top = st.container()
        with top:
            c1, c2 = st.columns([1, 5])
            if c1.button("← All watercourses"):
                ss.dd_course = ss.dd_sub = None
                st.rerun()
            c2.subheader(
                f"{crow['watercourse']} — {len(subs)} sub-catchments, "
                f"{crow['area_km2']:.0f} km²"
            )

        sel_pos = {
            i for i, cle in enumerate(subs["cleabs"]) if cle == ss.dd_sub
        }
        deck = deck_for(
            subs,
            sel_pos,
            layer_id="subcatchments",
            label_col="toponyme",
            fit_bounds=_pad(tuple(subs.total_bounds)),
        )
        event = st.pydeck_chart(
            deck, width="stretch", height=620, on_select="rerun", selection_mode="single-object"
        )
        picked = _picked_ids(event, "subcatchments")
        if picked:
            new = subs.iloc[min(picked)]["cleabs"]
            if new != ss.dd_sub:
                ss.dd_sub = new
                st.rerun()

        if ss.dd_sub is not None and ss.dd_sub in set(subs["cleabs"]):
            r = subs.loc[subs["cleabs"] == ss.dd_sub].iloc[0]
            st.markdown(f"**Selected sub-catchment:** {r['toponyme']}")
            st.dataframe(
                pd.DataFrame(r.drop(labels="geometry")).T,
                width="stretch",
                hide_index=True,
            )
        else:
            st.caption("Click a sub-catchment on the map to select it.")
        return

    # ---- Level 0: pick a watercourse ----
    st.subheader(f"{len(courses)} watercourse catchments — click one to drill in")
    order = courses.sort_values("area_km2", ascending=False).reset_index(drop=True)
    sel_pos = {
        i for i, k in enumerate(order[WATERCOURSE_KEY]) if k == ss.dd_course
    }
    deck = deck_for(
        order,
        sel_pos,
        layer_id="courses",
        label_col="watercourse",
        fit_bounds=_pad(tuple(order.total_bounds)),
    )
    event = st.pydeck_chart(
        deck, width="stretch", height=620, on_select="rerun", selection_mode="single-object"
    )
    picked = _picked_ids(event, "courses")
    if picked:
        ss.dd_course = order.iloc[min(picked)][WATERCOURSE_KEY]
        ss.dd_sub = None
        st.rerun()

    st.caption("Or pick from the list:")
    choice = st.selectbox(
        "Watercourse",
        options=list(order.index),
        format_func=lambda i: f"{order.at[i, 'watercourse']} ({order.at[i, 'area_km2']:.0f} km²)",
        index=None,
        label_visibility="collapsed",
    )
    if choice is not None:
        ss.dd_course = order.at[choice, WATERCOURSE_KEY]
        ss.dd_sub = None
        st.rerun()


# --------------------------------------------------------------- upstream trace

# Offline stream-network fixtures, most specific first.
TRONCON_CANDIDATES = [
    Path("data/raw/troncon_hydrographique_gavarnie_sample.geojson"),
    Path("data/raw/troncon_hydrographique_pyrenees.parquet"),
]
DEFAULT_POUR_POINT = "-0.0086, 42.7350"  # Gavarnie village on the Gave de Pau


@st.cache_data(show_spinner="Loading stream network…")
def load_troncons_cached(path_str: str) -> gpd.GeoDataFrame:
    from valleespyr.hydro.network import load_troncons

    return load_troncons(path_str)


@st.cache_data(show_spinner="Building network graph…")
def trace_cached(
    path_str: str, lon: float, lat: float, no_fictif: bool, min_order: int | None
) -> dict:
    """Snap + trace + tree, all keyed on the file so the graph is reused across points."""
    from valleespyr.hydro.network import build_graph
    from valleespyr.hydro.trace import (
        drop_fictif,
        snap_pour_point,
        to_tree,
        trace_upstream,
    )

    gdf = load_troncons_cached(path_str)
    snap = snap_pour_point(gdf, lon, lat)
    graph = build_graph(gdf)
    edge_ids, sub = trace_upstream(graph, snap["root_node"])
    tree = to_tree(sub, snap["root_node"], min_order=min_order)
    if no_fictif:
        drop_fictif(tree)
    # Geometry of the traced edges, as a GeoJSON FeatureCollection for the map.
    traced = gdf[gdf["cleabs"].isin(edge_ids)]
    if no_fictif and "fictif" in traced.columns:
        traced = traced[~traced["fictif"].astype(bool)]
    return {
        "snap": snap,
        "tree": tree,
        "n_edges": len(edge_ids),
        "traced_geojson": traced[["cleabs", "geometry"]].to_json(),
        "network_bounds": tuple(gdf.total_bounds),
        "traced_bounds": tuple(traced.total_bounds) if len(traced) else None,
    }


def _tree_lines(node: dict, prefix: str = "", is_last: bool = True, depth: int = 0):
    """Yield indented ``├──`` lines for the nested tree dict."""
    e = node.get("edge")
    if e is None:
        label = "● pour point"
    else:
        name = e.get("toponyme") or "(unnamed)"
        bits = []
        if e.get("order") is not None:
            bits.append(f"order {e['order']}")
        length = e.get("length_m", 0.0) + node.get("collapsed_length_m", 0.0)
        bits.append(f"{length / 1000:.1f} km")
        seg = 1 + node.get("collapsed_segments", 0)
        if seg > 1:
            bits.append(f"{seg} seg")
        if e.get("fictif"):
            bits.append("fictif")
        label = f"{name}  ({', '.join(bits)})"

    if depth == 0:
        yield label
    else:
        yield f"{prefix}{'└── ' if is_last else '├── '}{label}"

    kids = node["children"]
    for i, child in enumerate(kids):
        last = i == len(kids) - 1
        child_prefix = prefix + ("" if depth == 0 else ("    " if is_last else "│   "))
        yield from _tree_lines(child, child_prefix, last, depth + 1)


def _traced_deck(fc_json: str, bounds, pour_lon: float, pour_lat: float) -> pdk.Deck:
    import json as _json

    fc = _json.loads(fc_json)
    lines = pdk.Layer(
        "GeoJsonLayer",
        fc,
        get_line_color=[255, 90, 0, 220],
        line_width_min_pixels=1.8,
        pickable=False,
    )
    point = pdk.Layer(
        "ScatterplotLayer",
        [{"position": [pour_lon, pour_lat]}],
        get_position="position",
        get_fill_color=[20, 20, 20, 255],
        get_radius=40,
        radius_min_pixels=5,
    )
    minx, miny, maxx, maxy = bounds
    view = pdk.ViewState(
        longitude=(minx + maxx) / 2,
        latitude=(miny + maxy) / 2,
        zoom=_zoom_for_bounds(minx, miny, maxx, maxy),
    )
    return pdk.Deck(
        layers=[lines, point],
        initial_view_state=view,
        map_style=None,
        tooltip={"text": "{cleabs}"},
    )


def _upstream_trace() -> None:
    """Trace the BD TOPO stream network upstream of a pour point; tree + map."""
    st.sidebar.header("Stream network")
    found = next((p for p in TRONCON_CANDIDATES if p.exists()), None)
    default = str(found) if found else str(TRONCON_CANDIDATES[0])
    path_str = st.sidebar.text_input("Tronçon dump", value=default)
    if not Path(path_str).exists():
        st.warning(
            f"`{path_str}` not found. Dump the layer first, e.g.\n\n"
            "`valleespyr wfs dump --layer BDTOPO_V3:troncon_hydrographique "
            "--bbox -0.10,42.65,0.15,42.85 -o "
            "data/raw/troncon_hydrographique_gavarnie_sample.geojson`"
        )
        return

    point_s = st.sidebar.text_input("Pour point (lon, lat)", value=DEFAULT_POUR_POINT)
    no_fictif = st.sidebar.checkbox("Hide fictitious edges", value=False)
    min_order = st.sidebar.selectbox(
        "Min Strahler order", [None, 2, 3, 4, 5, 6], index=0
    )
    try:
        lon, lat = (float(x) for x in point_s.split(","))
    except ValueError:
        st.error("Pour point must be 'lon, lat' — two numbers.")
        return

    res = trace_cached(path_str, lon, lat, no_fictif, min_order)
    snap, tree = res["snap"], res["tree"]

    st.subheader(f"Upstream of {snap['toponyme'] or snap['cleabs']}")
    if res["n_edges"] == 0:
        st.info(
            "Nothing upstream of this point — it snapped to a headwater segment "
            "(or the pour point is outside the loaded network's bbox). "
            "Move it downstream onto a larger channel."
        )
        st.caption(
            f"snapped to `{snap['cleabs']}` — {snap['distance_m']:.0f} m from the point"
        )
        return
    c1, c2, c3 = st.columns(3)
    c1.metric("Edges traced", res["n_edges"])
    c2.metric("Segments shown", tree["upstream_segments"])
    c3.metric("Channel length", f"{tree['upstream_length_m'] / 1000:.1f} km")
    st.caption(
        f"snapped to `{snap['cleabs']}` — {snap['distance_m']:.0f} m from the point"
    )

    left, right = st.columns([3, 2], gap="medium")
    with left:
        bounds = res["traced_bounds"] or res["network_bounds"]
        st.pydeck_chart(
            _traced_deck(res["traced_geojson"], _pad(bounds), lon, lat),
            width="stretch",
            height=560,
        )
    with right:
        st.text("\n".join(_tree_lines(tree)))

    st.download_button(
        "Traced network (GeoJSON)",
        res["traced_geojson"],
        file_name="upstream_trace.geojson",
        mime="application/geo+json",
    )


def _sidebar_source() -> gpd.GeoDataFrame | None:
    st.sidebar.header("Data source")
    found = next((p for p in DATA_CANDIDATES if p.exists()), None)

    mode = st.sidebar.radio(
        "Load from",
        ["Local dump", "Live WFS (bbox)"],
        index=0 if found else 1,
    )

    if mode == "Local dump":
        default = str(found) if found else str(DATA_CANDIDATES[0])
        path_str = st.sidebar.text_input("Path", value=default)
        if not Path(path_str).exists():
            st.sidebar.error("File not found. Run `valleespyr wfs dump` or pick live WFS.")
            return None
        return load_local(path_str)

    endpoint = st.sidebar.text_input("WFS endpoint", value=GEOPLATEFORME_WFS)
    layer = st.sidebar.text_input("Layer", value=LAYER_BASSIN_VERSANT)
    c1, c2 = st.sidebar.columns(2)
    minx = c1.number_input("min lon", value=PYRENEES_BBOX[0], format="%.3f")
    miny = c2.number_input("min lat", value=PYRENEES_BBOX[1], format="%.3f")
    maxx = c1.number_input("max lon", value=PYRENEES_BBOX[2], format="%.3f")
    maxy = c2.number_input("max lat", value=PYRENEES_BBOX[3], format="%.3f")
    if not st.sidebar.button("Fetch", type="primary"):
        st.info("Set a bounding box and click **Fetch**.")
        return None
    return load_wfs(endpoint, layer, (minx, miny, maxx, maxy))


def _sidebar_filters(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    st.sidebar.header("Filters")
    out = gdf

    if "libelle_du_bassin_hydrographique" in out.columns:
        basins = sorted(out["libelle_du_bassin_hydrographique"].dropna().unique())
        picked = st.sidebar.multiselect("Basin district", basins, default=basins)
        out = out[out["libelle_du_bassin_hydrographique"].isin(picked)]

    if "toponyme" in out.columns:
        q = st.sidebar.text_input("Toponyme contains")
        if q:
            out = out[out["toponyme"].str.contains(q, case=False, na=False)]

    if "area_km2" in out.columns and len(out):
        lo, hi = float(out["area_km2"].min()), float(out["area_km2"].max())
        if hi > lo:
            rng = st.sidebar.slider("Area (km²)", lo, hi, (lo, hi))
            out = out[out["area_km2"].between(*rng)]

    return out.reset_index(drop=True)


def _downloads(gdf: gpd.GeoDataFrame, selected_ids: set[int]) -> None:
    st.sidebar.markdown("---")
    st.sidebar.header("Download")
    subset = gdf.loc[list(selected_ids)] if selected_ids else gdf
    label = f"{len(subset)} selected" if selected_ids else f"all {len(subset)}"

    gj = subset.to_json()
    st.sidebar.download_button(
        f"GeoJSON ({label})", gj, file_name="watersheds.geojson", mime="application/geo+json"
    )

    buf = io.BytesIO()
    subset.to_parquet(buf)
    st.sidebar.download_button(
        f"GeoParquet ({label})", buf.getvalue(), file_name="watersheds.parquet"
    )

    csv = pd.DataFrame(subset.drop(columns="geometry")).to_csv(index=False)
    st.sidebar.download_button(
        f"CSV attrs ({label})", csv, file_name="watersheds.csv", mime="text/csv"
    )


def _run_streamlit() -> None:
    """Entry point for the ``valleespyr-app`` script: re-exec under `streamlit run`."""
    import sys

    from streamlit.web import cli as stcli

    sys.argv = ["streamlit", "run", __file__, *sys.argv[1:]]
    sys.exit(stcli.main())


if __name__ == "__main__":
    main()
