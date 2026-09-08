# TODO — next session: `troncon_hydrographique` + upstream tree

Goal: from a pour point, trace the stream network **upstream** and present it as a
**tree view** (text/indented first, map later). This is the building block for a
real "valley catchment", which the coarse `bassin_versant_topographique` layer
can't give us.

## Context recap (state at end of last session)

- Repo has: `sources/wfs.py` (WFS 2.0 client), `watershed.py`, `dump.py`
  (`valleespyr wfs dump`), `app.py` (Streamlit explorer). 13 tests pass.
- Local data (gitignored, present on this machine):
  - `data/raw/bassin_versant_topographique_fr.parquet` — 6633 feats, all FR
  - `data/raw/bassin_versant_topographique_pyrenees.parquet` — 532 feats
  - `data/raw/troncon_hydrographique_gavarnie_sample.geojson` — 3097 edges,
    bbox `42.68,-0.10 .. 42.85,0.20` (Gave de Pau headwaters, incl. gaves
    d'Ossoue / Estaubé / Héas / Aspé). **Use this as the offline fixture.**

## Layer: `BDTOPO_V3:troncon_hydrographique`

WFS quirk: this layer's `BBOX` wants CRS `urn:ogc:def:crs:EPSG::4326` (lat,lon
order). Plain `EPSG:4326` returned 0 features. `SRSNAME=urn:ogc:def:crs:EPSG::4326`
also returns coords in lat,lon — reproject/swap on ingest.
(The watershed layer accepted plain `EPSG:4326` fine — don't assume uniformity.)

### Topology fields (verified against real data)

| field | meaning | observed |
|---|---|---|
| `cleabs` | stable edge id | `TRON_EAU0000002222751383` |
| `lien_vers_noeud_hydrographique_ini` | from-node | `NOEUDHYD…` |
| `lien_vers_noeud_hydrographique_fin` | to-node | `NOEUDHYD…` |
| `sens_de_l_ecoulement` | flow dir vs geometry | all `"Sens direct"` in sample; also `"Sens inverse"`, `"Double sens"`, `"Indéterminé"` exist — handle them |
| `numero_d_ordre` | Strahler order | `"1".."7"`, sometimes `None` |
| `reseau_principal_coulant` | on main flowing network | bool |
| `fictif` | connector w/ no real channel (through lakes etc.) | ~11% true in sample |
| `nature` | `Ecoulement naturel`, `Canal`, `Conduit forcé`, `Retenue`, `Lac`, … |
| `liens_vers_cours_d_eau` | FK → `cours_d_eau` | `COURDEAU…` |
| `cpx_toponyme_de_cours_d_eau` | river name (denormalized) | `"Gave de Pau"`; `None` for ~60% (small tribs) |
| `geometrie` | `gml:CurvePropertyType` (LineString) | |

### Graph model

- Each tronçon = directed edge `ini -> fin` **after** applying `sens_de_l_ecoulement`
  (`Sens inverse` ⇒ swap; `Double sens`/`Indéterminé` ⇒ flag, maybe treat as
  undirected or drop).
- **Upstream trace** from a node N: BFS/DFS collecting every edge whose `fin` is
  already in the visited-node set, starting `visited = {N}`. Upstream of a point on
  a tree network is itself a tree (each node has exactly one downstream edge on the
  main network; braided `type_de_bras` sections can violate this — dedupe by node).
- Root selection: snap pour point to nearest edge geometry, split conceptually,
  take that edge's `ini` node as the trace root (or `fin` if you want to include
  the edge the point sits on).

## Tasks

1. **`sources/wfs.py`**: nothing needed structurally, but add a note/const that
   some layers need the URN CRS for BBOX. Consider a `bbox_crs` default per call
   site rather than global.

2. **`hydro/network.py`** (new module):
   - `fetch_troncons(client, bbox, ...)` → GeoDataFrame (reproject lat,lon→lon,lat,
     then to EPSG:2154 for length/snap).
   - `build_graph(gdf)` → directed graph. Use `networkx.DiGraph` (add `networkx`
     to core deps) OR a plain `dict[node] -> list[edge]` if we want zero deps.
     Recommend networkx: we'll want `dfs_tree`, `descendants`, subgraph, etc.
   - Node attrs: none from WFS (nodes are just ids); optionally fetch
     `noeud_hydrographique` layer for node geometry/type if needed for display.
   - Edge attrs: `cleabs`, `order`, `toponyme`, `nature`, `fictif`, `length_m`,
     `reseau_principal_coulant`.

3. **`hydro/trace.py`**:
   - `snap_pour_point(gdf, lon, lat)` → (edge cleabs, root node, distance).
   - `trace_upstream(graph, root_node)` → set of edge cleabs + induced subgraph.
   - `to_tree(subgraph, root_node)` → nested dict:
     `{node, edge_cleabs, toponyme, order, length_m, children: [...]}`.
     Order children by descending upstream length or Strahler order.

4. **Tree view — CLI first**:
   - `valleespyr hydro tree --point LON,LAT [--bbox ...] [--from-file f.geojson]`
   - Pretty indented print (rich.tree if we add `rich`, else manual `├──`).
   - Collapse long unbranched chains: "Gave de Pau (order 4, 8.2 km, 12 segments)".
   - `--max-depth`, `--min-order`, `--json` (emit the nested dict).
   - Summary line: total upstream length, #segments, Strahler order at root,
     drainage density if we also have area.

5. **Tree view — Streamlit** (extend `app.py` or new `app_network.py`):
   - Click/enter a pour point → show `st.dataframe` hierarchy or an indented
     `st.expander` tree; highlight traced edges on the pydeck map.
   - Toggle: exclude `fictif`, min Strahler order, main network only.

6. **Validation**:
   - Compare traced-upstream extent vs the dissolved `bassin_versant_topographique`
     polygon for the same watercourse — should roughly nest.
   - Known case: pour point at Gavarnie village `(-0.0086, 42.7350)` → upstream
     should include gaves d'Ossoue, d'Aspé, de Pau source; NOT Gave de Héas
     (that joins downstream). Sanity-check against the map.

7. **Tests** (offline, use the saved gavarnie sample or a tiny hand-built graph):
   - `sens_de_l_ecoulement` inversion handling.
   - upstream trace on a Y-shaped toy network (2 tribs + trunk).
   - `to_tree` nesting + child ordering.
   - snap picks the nearest edge.
   - cycle guard (braided channels) doesn't infinite-loop.

## Open questions / decisions for next session

- **networkx dependency?** (recommended) vs hand-rolled adjacency.
- **rich for the tree print?** (nice) vs plain ASCII.
- Handle `Double sens` / `Indéterminé` how — drop, or undirected fallback?
- Do we need the `noeud_hydrographique` layer at all, or are node ids enough?
- Where does the pour point come from — CLI arg, config yaml
  (`config/gavarnie.yaml` already has `pour_point_wgs84`), or click-on-map?
- Scope of the bbox fetch: auto-expand until the trace stops hitting the bbox
  edge? (start simple: fixed generous bbox, warn if trace touches boundary.)

## Nice-to-have later

- Merge traced network → single MultiLineString for draping on the DEM.
- Derive a pour-point watershed from the DEM (`pysheds`/WhiteboxTools) and compare
  to both the BD TOPO polygon and the traced network extent.
- Strahler recomputation from the graph (don't trust `numero_d_ordre` blindly).
