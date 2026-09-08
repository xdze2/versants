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


def dissolve_by_watercourse(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """One row per ``liens_vers_cours_d_eau_principal`` — the whole-watercourse catchment."""
    key = "liens_vers_cours_d_eau_principal"
    agg = gdf.dissolve(
        by=key,
        aggfunc={"code_hydrographique": "count", "area_km2": "sum"},
    ).rename(columns={"code_hydrographique": "n_subcatchments"})
    return agg.reset_index()


# ---------------------------------------------------------------------------- map


def _fill_for(idx: int, selected: set[int]) -> list[int]:
    return [255, 140, 0, 160] if idx in selected else [70, 130, 180, 70]


def deck_for(gdf: gpd.GeoDataFrame, selected_ids: set[int]) -> pdk.Deck:
    label_col = (
        "liens_vers_cours_d_eau_principal"
        if "toponyme" not in gdf.columns
        else "toponyme"
    )
    features = []
    for idx, row in gdf.iterrows():
        features.append(
            {
                "type": "Feature",
                "geometry": row.geometry.__geo_interface__,
                "properties": {
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
        stroked=True,
        filled=True,
        get_fill_color="properties.fill",
        get_line_color=[40, 40, 40, 200],
        line_width_min_pixels=0.5,
        pickable=True,
        auto_highlight=True,
    )
    minx, miny, maxx, maxy = gdf.total_bounds
    view = pdk.ViewState(
        longitude=(minx + maxx) / 2,
        latitude=(miny + maxy) / 2,
        zoom=7.5,
    )
    return pdk.Deck(
        layers=[layer],
        initial_view_state=view,
        map_style=None,
        tooltip={"text": "{label}\n{code_hydrographique} — {area_km2} km²"},
    )


# --------------------------------------------------------------------------- main


def main() -> None:
    st.set_page_config(page_title="valleespyr — watershed explorer", layout="wide")
    st.title("Pyrénées topographic watersheds — BD TOPO explorer")

    src = _sidebar_source()
    if src is None:
        st.stop()
    gdf = src

    st.sidebar.markdown("---")
    view_mode = st.sidebar.radio(
        "View", ["Sub-catchments", "Dissolved by watercourse"], index=0
    )
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
