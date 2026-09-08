"""Load ``BDTOPO_V3:troncon_hydrographique`` edges and turn them into a directed graph.

A *tronçon* is one stream segment between two hydrographic nodes. BD TOPO gives
each one a from-node (``lien_vers_noeud_hydrographique_ini``) and a to-node
(``..._fin``) plus a flow-direction flag (``sens_de_l_ecoulement``) that says how
the drawn geometry relates to the real flow. We normalise every edge to point
**downstream** (`from -> to` follows the water) and hang the useful attributes off
it, so an upstream trace is just a walk against the arrows.

WFS quirk (see ``todo.md``): this layer wants the URN CRS
``urn:ogc:def:crs:EPSG::4326`` for ``BBOX``/``SRSNAME`` and then returns
coordinates in **lat, lon** order. :func:`fetch_troncons` swaps them back;
:func:`load_troncons` assumes a local file is already lon, lat (as the saved
gavarnie sample is).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..sources.wfs import WFSClient

if TYPE_CHECKING:  # pragma: no cover - typing only
    import geopandas as gpd
    import networkx as nx

# Stream-network layer on the Géoplateforme.
LAYER_TRONCON = "BDTOPO_V3:troncon_hydrographique"

# This layer needs the authority-axis (lat, lon) URN CRS or it returns 0 features.
TRONCON_BBOX_CRS = "urn:ogc:def:crs:EPSG::4326"
TRONCON_SRS = "urn:ogc:def:crs:EPSG::4326"

# Working CRS for lengths / snapping (Lambert-93, metres).
CRS_METRIC = "EPSG:2154"

# BD TOPO topology fields we carry onto graph edges.
FROM_NODE = "lien_vers_noeud_hydrographique_ini"
TO_NODE = "lien_vers_noeud_hydrographique_fin"
FLOW_DIR = "sens_de_l_ecoulement"
ORDER = "numero_d_ordre"
TOPONYME = "cpx_toponyme_de_cours_d_eau"
NATURE = "nature"
FICTIF = "fictif"
MAIN_NET = "reseau_principal_coulant"
# Stable ``cours_d_eau`` id (may be several ``/``-joined); used to roll segments
# up into rivers (see ``hydro/rivers.py``).
COURS_D_EAU = "liens_vers_cours_d_eau"

# ``sens_de_l_ecoulement`` values -> what to do with the drawn geometry.
FLOW_DIRECT = "Sens direct"  # geometry already points downstream
FLOW_INVERSE = "Sens inverse"  # geometry points upstream -> swap the nodes
FLOW_BOTH = "Double sens"  # tidal / canal both ways
FLOW_UNKNOWN = "Indéterminé"


# --------------------------------------------------------------------------- load


def load_troncons(path: str | Path) -> gpd.GeoDataFrame:
    """Read a local GeoJSON/GeoParquet dump of the tronçon layer.

    Assumes lon, lat geometry (the saved ``troncon_hydrographique_*`` samples are).
    Drops any Z coordinate and reprojects to a metric CRS so ``length_m`` and
    point snapping work.
    """
    import geopandas as gpd

    path = Path(path)
    gdf = gpd.read_parquet(path) if path.suffix == ".parquet" else gpd.read_file(path)
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    return _prepare(gdf)


def fetch_troncons(
    client: WFSClient,
    bbox: tuple[float, float, float, float],
    *,
    layer: str = LAYER_TRONCON,
    max_features: int | None = None,
) -> gpd.GeoDataFrame:
    """Fetch tronçons intersecting ``bbox`` (lon, lat) live from the WFS.

    Handles this layer's lat/lon axis quirk: the request uses the URN CRS and the
    response comes back lat, lon, which we flip to lon, lat on ingest.
    """
    import geopandas as gpd
    from shapely.ops import transform

    minx, miny, maxx, maxy = bbox
    feats = list(
        client.iter_features(
            layer,
            bbox=(miny, minx, maxy, maxx),  # URN CRS wants lat, lon
            bbox_crs=TRONCON_BBOX_CRS,
            srs_name=TRONCON_SRS,
            max_features=max_features,
        )
    )
    gdf = gpd.GeoDataFrame.from_features(feats, crs="EPSG:4326")
    # Response coords are lat, lon -> swap to lon, lat.
    gdf["geometry"] = gdf.geometry.apply(
        lambda g: transform(lambda x, y, z=None: (y, x), g)
    )
    return _prepare(gdf)


def _prepare(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Common post-load: WGS84 canonical, drop Z, add ``length_m``, stable index."""
    from shapely.ops import transform

    gdf = gdf.to_crs("EPSG:4326")
    has_z = gdf.geometry.has_z
    if has_z.any():
        gdf.loc[has_z, "geometry"] = gdf.loc[has_z, "geometry"].apply(
            lambda g: transform(lambda x, y, z=None: (x, y), g)
        )
    # Nulls in text columns come back inconsistently across GDAL/pandas versions:
    # float NaN, pandas NA, or the literal string "nan"/"None". Normalise to None
    # so name comparisons (chain collapse) and JSON output behave.
    for c in gdf.columns:
        if c == "geometry":
            continue
        s = gdf[c]
        if s.dtype == object or str(s.dtype) in ("string", "str"):
            gdf[c] = s.astype(object).where(
                s.notna() & ~s.isin(["nan", "None", "NaN", ""]), None
            )
    gdf["length_m"] = gdf.geometry.to_crs(CRS_METRIC).length
    return gdf.reset_index(drop=True)


# -------------------------------------------------------------------------- graph


def build_graph(gdf: gpd.GeoDataFrame) -> nx.DiGraph:
    """Directed graph of the stream network, every edge pointing **downstream**.

    Nodes are hydrographic-node ids (strings). Each edge ``(from, to)`` carries:
    ``cleabs``, ``order`` (Strahler, int or ``None``), ``toponyme``, ``nature``,
    ``fictif`` (bool), ``main_network`` (bool), ``length_m`` (float), ``geometry``,
    and ``flow`` (the raw ``sens_de_l_ecoulement``). Edges flagged ``Double sens``
    or ``Indéterminé`` keep their drawn orientation and are marked
    ``ambiguous=True`` so callers can drop or special-case them.

    Parallel edges between the same node pair (braided channels) are collapsed to
    the longest one; a self-loop (``from == to``) is skipped.
    """
    import networkx as nx

    g = nx.DiGraph()
    for row in gdf.itertuples(index=False):
        d = row._asdict()
        u = _clean(d.get(FROM_NODE))
        v = _clean(d.get(TO_NODE))
        if not u or not v or u == v:
            continue

        flow = d.get(FLOW_DIR)
        ambiguous = flow in (FLOW_BOTH, FLOW_UNKNOWN)
        if flow == FLOW_INVERSE:
            u, v = v, u

        attrs = {
            "cleabs": d.get("cleabs"),
            "order": _as_int(d.get(ORDER)),
            "toponyme": d.get(TOPONYME),
            "nature": d.get(NATURE),
            "fictif": bool(d.get(FICTIF)),
            "main_network": bool(d.get(MAIN_NET)) if d.get(MAIN_NET) is not None else None,
            COURS_D_EAU: d.get(COURS_D_EAU),
            "length_m": float(d.get("length_m") or 0.0),
            "flow": flow,
            "ambiguous": ambiguous,
            "geometry": d.get("geometry"),
        }
        if g.has_edge(u, v):
            if attrs["length_m"] <= g[u][v].get("length_m", 0.0):
                continue  # keep the longer parallel edge
        g.add_edge(u, v, **attrs)
    return g


def _clean(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
