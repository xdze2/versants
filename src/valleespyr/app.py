"""Streamlit navigator for the Pyrénées **river graph**.

Run it with::

    streamlit run src/valleespyr/app.py
    # or, after `pip install -e '.[app]'`:
    valleespyr-app

It loads a local ``troncon_hydrographique`` dump (see ``valleespyr wfs dump``),
rolls the segments up into whole rivers (:func:`valleespyr.hydro.rivers.build_river_network`)
and lets you walk the "flows into" graph:

* pick a river — its **own course** and its whole **catchment** stream network
  are drawn on the map, its facts (Strahler order, length, root/leaf state) up
  top;
* its **tributaries** are listed biggest-first — click one to drop into that
  sub-valley, and a breadcrumb walks you back down to the outlet;
* export the current river's course or catchment as GeoJSON.

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

from valleespyr.hydro.rivers import RiverNetwork, build_river_network

# Offline stream-network dumps, most specific first.
TRONCON_CANDIDATES = [
    Path("data/raw/troncon_hydrographique_pyrenees.parquet"),
    Path("data/raw/troncon_hydrographique_gavarnie_sample.geojson"),
]
# Pyrénées chain, lon/lat — shown in the "dump it yourself" hint.
PYRENEES_BBOX = (-2.0, 42.3, 3.2, 43.4)

COURSE_COLOR = [255, 90, 0, 235]  # the selected river itself
TRIB_COLOR = [70, 130, 180, 150]  # everything else in its catchment
PICKED_TRIB_COLOR = [255, 190, 60, 230]  # a tributary hovered/selected in the list


# --------------------------------------------------------------------------- data


@st.cache_resource(show_spinner="Building river graph…")
def load_network(path_str: str) -> RiverNetwork:
    """Load a tronçon dump and roll it up into a :class:`RiverNetwork` (cached per file)."""
    from valleespyr.hydro.network import build_graph, load_troncons

    gdf = load_troncons(path_str)
    return build_river_network(build_graph(gdf))


@st.cache_data(show_spinner=False)
def summary_frame(path_str: str) -> pd.DataFrame:
    """One row per river for the picker / table (longest first)."""
    rn = load_network(path_str)
    return pd.DataFrame(rn.summary())


# ---------------------------------------------------------------------------- map


def _zoom_for_bounds(minx: float, miny: float, maxx: float, maxy: float) -> float:
    """Rough web-mercator zoom that fits a lon/lat box (assumes a ~900px map)."""
    span = max(maxx - minx, (maxy - miny) * 1.6, 1e-4)
    return max(3.0, min(13.5, math.log2(360.0 / span) + 0.2))


def _pad(box: tuple[float, float, float, float], frac: float = 0.12):
    minx, miny, maxx, maxy = box
    dx = (maxx - minx) * frac or 0.01
    dy = (maxy - miny) * frac or 0.01
    return (minx - dx, miny - dy, maxx + dx, maxy + dy)


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


def river_deck(
    course_fc: dict,
    catchment_fc: dict,
    *,
    picked_river_id: str | None = None,
    segment_to_river: dict[str, str] | None = None,
) -> pdk.Deck:
    """Two line layers: the catchment network (pickable) under the selected course.

    Each catchment feature carries ``river_id`` (looked up from its ``cleabs``)
    so a click can resolve to a river to drill into.
    """
    s2r = segment_to_river or {}
    cat_feats = []
    for feat in catchment_fc["features"]:
        cid = feat["properties"].get("cleabs")
        rid = s2r.get(cid)
        color = PICKED_TRIB_COLOR if (rid and rid == picked_river_id) else TRIB_COLOR
        cat_feats.append(
            {
                "type": "Feature",
                "geometry": feat["geometry"],
                "properties": {
                    "cleabs": cid,
                    "river_id": rid or "",
                    "toponyme": feat["properties"].get("toponyme") or "",
                    "order": feat["properties"].get("order"),
                    "color": color,
                },
            }
        )
    catchment_layer = pdk.Layer(
        "GeoJsonLayer",
        {"type": "FeatureCollection", "features": cat_feats},
        id="catchment",
        stroked=True,
        filled=False,
        get_line_color="properties.color",
        line_width_min_pixels=1.0,
        pickable=True,
        auto_highlight=True,
    )
    course_layer = pdk.Layer(
        "GeoJsonLayer",
        course_fc,
        id="course",
        stroked=True,
        filled=False,
        get_line_color=COURSE_COLOR,
        line_width_min_pixels=3.0,
        pickable=False,
    )
    box = _fc_bounds(catchment_fc) or _fc_bounds(course_fc) or PYRENEES_BBOX
    minx, miny, maxx, maxy = _pad(box)
    view = pdk.ViewState(
        longitude=(minx + maxx) / 2,
        latitude=(miny + maxy) / 2,
        zoom=_zoom_for_bounds(minx, miny, maxx, maxy),
    )
    return pdk.Deck(
        layers=[catchment_layer, course_layer],
        initial_view_state=view,
        map_style=None,
        tooltip={"text": "{toponyme}\n{cleabs}"},
    )


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

    if ss.river_id is None or ss.river_id not in rn.rivers:
        _landing(rn, summ)
        return

    _river_view(rn, path_str)


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


def _river_view(rn: RiverNetwork, path_str: str) -> None:
    ss = st.session_state
    river = rn.get(ss.river_id)

    # ---- breadcrumb: outlet ... -> this river -------------------------------
    chain = list(reversed(rn.downstream_path(river.id)))  # root ... -> river
    crumbs = st.columns(len(chain) + 1)
    for col, r in zip(crumbs, chain, strict=False):
        label = (r.name or r.id[:12]) + (" ▸" if r.id != river.id else "")
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

    left, right = st.columns([3, 2], gap="medium")

    with left:
        deck = river_deck(
            course_fc,
            catchment_fc,
            picked_river_id=ss.picked_trib,
            segment_to_river=rn.segment_to_river,
        )
        event = st.pydeck_chart(
            deck,
            width="stretch",
            height=620,
            on_select="rerun",
            selection_mode="single-object",
        )
        hit = _picked_river(event, rn.segment_to_river)
        if hit and hit != ss.picked_trib:
            ss.picked_trib = hit
            st.rerun()
        st.caption(
            f"Orange = {river.name or 'this river'}'s course · blue = its catchment network · "
            "click any blue reach to select that tributary."
        )

    with right:
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

    _downloads(river, course_fc, catchment_fc)


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
    labels = {
        r.id: f"{r['name']}  ({r['length_km']:.0f} km)" for _, r in named.iterrows()
    }
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
