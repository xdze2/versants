# TODO — DEM catchment delineation (Copernicus GLO-30 + pysheds demo)

Goal: compute a **real catchment polygon for any river**, from a DEM, because
`bassin_versant_topographique` cannot give us one.

Next session: a single self-contained demo script that delineates the **Gave de
Lutour** catchment from a GLO-30 tile and compares it against the known 39 km²
BD TOPO polygon. Validate on one valley before building any pipeline.

## DONE — the Lutour demo works (2026-09-10)

`scratchpad/dem_lutour.py` runs end to end on a real COP30 tile
(`data/raw/cop30_lutour.tif`, 1.1 MB, 576×432 @ ~31 m). Needs a free
`OPENTOPOGRAPHY_API_KEY`; note this in the README when the code is promoted.

| check | result |
|---|---|
| area (DEM) vs reference | **39.49 km² vs 39.34 km² → +0.4%** |
| IoU vs reference | **0.96** |
| pour-point snap | 0.3 cells — DEM channel and BD TOPO outlet agree |
| depression fill | 0.61% of tile (well under the 5% cirque warning) |
| Lutour tronçons inside | 45/45 (the last is a `fictif` connector *on* the divide) |

The DEM boundary **follows ridgelines** where the 2012 BDCarto reference cuts
across them — see `scratchpad/lutour_catchment_dem.png`. Method proven; the
`snap` step was a non-event here because the outlet coord came straight from
tronçon geometry, as planned.

Gotchas found:

- **pysheds 0.5 (latest) is broken on NumPy 2.x** — it calls `np.in1d`, removed
  in NumPy 2.0. The script shims `np.in1d = np.isin` before importing pysheds
  (exact alias for 1-D arrays). Carry this into `hydro/dem.py`. Pinning
  `numpy<2` instead would drag numba/scipy/rasterio back and fight the rest of
  the project.
- `Grid.from_raster` warns "No `nodata` value detected. Defaulting to 0." — COP30
  from OpenTopography has no nodata tag. Harmless here (min elevation 870 m), but
  set nodata explicitly when a tile might contain real 0 m cells (coast).
- pysheds API vs the todo sketch: it's `grid.fill_pits` → `fill_depressions` →
  `resolve_flats` (three steps, not two), and `snap_to_mask(mask, (x, y))`
  returns `(x, y)`, not indices.

### Next

1. Run the *same* script on **Neste de Rioumajou** — the border-straddling case
   the whole exercise is for. COP30 covers Spain; the outlet coord comes from the
   tronçon dump the same way. No reference polygon exists, so the check is
   "plausible area + contains its own streams".
2. Then promote to `src/valleespyr/hydro/dem.py` (see "After the demo works").

## Why (measured, not assumed)

`bassin_versant_topographique` is the only BD TOPO source of drainage area — the
tronçon layer is `LineString` only, `cours_d_eau` is `MultiLineString`, and
`surface_hydrographique` is open water (lakes/reservoirs, median 0.001 km²), not
catchments. That single area source fails us in three ways:

- **Coverage.** Only **86 of 2755 named rivers** in the Pyrénées dump get a
  polygon. `Neste de Rioumajou` (23.4 km, ord 5) and `Neste de la Géla` (21 km)
  get none, while the neighbouring `Gave de Lutour` / `Gave d'Ossoue` do — the
  gaps are not systematic, which is worse than uniform coarseness for a UI.
- **Model.** Sub-basins are keyed to a *whole* `cours_d_eau` via
  `liens_vers_cours_d_eau_principal`, one polygon per *reach* of a trunk river.
  Tributary basins are absorbed into the trunk's reaches — literally marked
  `(incluse)` in the toponyme, e.g. *"La Neste du confluent de la Neste de
  Rioumajou (incluse) au confluent du Lavedan"*. Rioumajou's basin exists in the
  data, just not as its own record.
- **Resolution.** The Lutour polygon is 173 vertices over a 29 km perimeter —
  **median vertex spacing 165 m**, `precision_planimetrique` 20 m, digitised from
  BDCarto, last modified 2012. Draped on a 1 m RGE ALTI surface it will visibly
  cut across terrain instead of following ridgelines. Not good enough for 3D.

Checked for a finer source, found none: `BDCARTO_V5:bassin_versant_topographique`
returns the same 9 polygons over the Rioumajou bbox, `MTE_MASSE-EAU:_surface_
bassins-versants` returns 0 there, and Sandre's authoritative TOPAGE
(`sa:BassinVersantTopographique_FXX`) returns the same 9 with the same
`(incluse)` toponymes. No `topage` layer exists on the Géoplateforme.

## Why GLO-30 first (not RGE ALTI)

Prototype on **Copernicus GLO-30** (30 m, global, OpenTopography API):

- small enough to iterate on — a valley tile is MB, not GB;
- **covers Spain**, so border-straddling catchments work. RGE ALTI stops at the
  frontier, the same limitation that already truncates our tronçon network
  (54 `code_du_pays = ES` segments in the gavarnie sample, unconnected);
- 30 m is plenty to validate that the *pipeline* is correct.

Move to RGE ALTI 1 m / 5 m only once the method is proven, and only per-valley.

## The demo (next session)

One script, `scratchpad/dem_lutour.py` (promote to `hydro/dem.py` once it works).

**Target: Gave de Lutour.** Chosen because it has a known reference polygon
(39 km², 1 sub-basin) to check against, and it is a compact upper valley.
Course bounds `-0.1090, 42.7742 → -0.0847, 42.8728`; fetch a padded tile, say
`-0.16, 42.74 → -0.04, 42.90`.

### Steps

1. **Fetch the DEM.** OpenTopography REST API, `demtype=COP30`, the bbox above,
   GeoTIFF out. Needs a free API key (`OPENTOPOGRAPHY_API_KEY` env var) — note it
   in the README when this lands. Cache the tile under `data/raw/`.

2. **Condition the DEM.** `fill_depressions` → `resolve_flats`. Pits are cells
   lower than every neighbour (noise, bridges, or real karst/tarns); water entering
   one never leaves and flow routing stalls. **Watch this step in glacial cirques**
   — some Pyrenean depressions are real, and filling them is technically wrong but
   usually necessary. Compare filled vs raw to see how much was altered.

3. **Flow direction + accumulation.** D8 (steepest of 8 neighbours). Its known
   weakness is quantising flow to 8 angles, which zig-zags on smooth slopes;
   D-infinity is the fallback if the delineation looks bad. Accumulation = how many
   cells drain through each cell, so high values trace the channel network.

4. **Snap the pour point — the step that actually decides success.** The outlet
   comes from BD TOPO vector coords, the flow grid comes from the DEM; they are
   different datasets and will not agree exactly. Off by two cells onto a valley
   wall and you delineate a 0.2 km² hillside instead of a 39 km² valley, silently
   and plausibly. Use `grid.snap_to_mask(acc > threshold, (lon, lat))`. We have an
   advantage here: BD TOPO tronçon geometry says where the stream really is, so
   snap toward it rather than guessing.

5. **Delineate + vectorise.** `grid.catchment(...)` walks the flow grid backwards
   from the outlet; every cell draining to it is in the catchment. Then
   `rasterio.features.shapes` → polygon → simplify a little → WGS84.

### Sketch

```python
grid = Grid.from_raster(tif)
dem  = grid.read_raster(tif)
filled   = grid.fill_depressions(dem)
inflated = grid.resolve_flats(filled)
fdir = grid.flowdir(inflated)
acc  = grid.accumulation(fdir)
x, y = grid.snap_to_mask(acc > 1000, (outlet_lon, outlet_lat))
catch = grid.catchment(x=x, y=y, fdir=fdir, xytype='coordinate')
```

### Validation — the whole point of picking Lutour

| check | expectation |
|---|---|
| area vs BD TOPO reference | **39 km²**, ±15% is fine at 30 m |
| shape | follows ridgelines; IoU vs reference > ~0.8 |
| contains its own streams | every Lutour tronçon inside the polygon |
| pour-point sanity | moving the outlet ±100 m must not change area 10× |

Then run the *same* code on **Neste de Rioumajou** (no reference exists) and check
the result is plausible — that is the case the whole exercise is for.

### Deps

`pysheds` (NumPy-based, pip-installable, lightest) + the existing `dem` extra
(`rasterio`, `rioxarray`, `xarray`). Note: the `dem` extra is declared in
`pyproject.toml` but **not currently installed** — `uv sync --extra dem` first.
Alternatives if pysheds disappoints: `richdem` (faster on big grids),
`WhiteboxTools` (most complete, ships a binary).

## After the demo works

- Promote to `src/valleespyr/hydro/dem.py`: `fetch_dem(bbox)`,
  `delineate(dem, lon, lat)` → shapely polygon, tile caching.
- Wire into the app: when a river has no `bassin_versant_topographique` polygon,
  offer "compute catchment from DEM". Keep the BD TOPO polygon where it exists —
  it stays useful as a **validation reference**, not a competitor.
- Real area/elevation stats per catchment (hypsometry, min/max/mean elevation) —
  this is what the 3D maps actually want.
- Drape the traced stream network on the DEM (`Merge traced network → single
  MultiLineString`, carried over from the earlier todo).

## Still open (carried over, unrelated to DEM)

- **`--wfs-endpoint` does not exist.** The README says the endpoint is
  configurable; it is not — there is no such option on `valleespyr wfs` or its
  subcommands. Had to `curl` Sandre directly to check TOPAGE. Add the flag or fix
  the README.
- **Catchment display bug, unresolved.** The area polygon rendered far too large
  and convex in the browser for some rivers. Not reproducible from current code —
  every river checked frames correctly and all real polygons are strongly
  non-convex (area/hull 0.32–0.41). Suspected stale `@st.cache_resource` in a
  long-running server, never confirmed. The new static matplotlib map bypasses
  deck.gl entirely, so it is now a useful discriminator: if a river looks right
  there and wrong on the interactive map, the fault is in the deck viewport.
- **28 root rivers still over-reach.** A sub-basin is keyed to a whole
  watercourse, so a river whose outlet leaves the dump gets a polygon covering its
  full length. Inherent to any finite bbox. Consider a visible warning on
  `river.is_root` rather than the current trailing caption clause.
- `sens_de_l_ecoulement` inversion is coded but still untested on real data — the
  whole Pyrénées dump may be `"Sens direct"`. Find a bbox with `"Sens inverse"`.
- A multi-outflow river can have >1 parent; `parent_id` / `downstream_path` follow
  the first sorted one. Revisit if it matters.
- Short same-name `cours_d_eau` stubs (e.g. a 0.3 km second "Gave d'Ossoue")
  list as tributaries of the main course. Merge, or accept as a data quirk.
- `RiverNetwork` persistence: 7.4 s to build the 6652-river graph from the 62k
  Pyrénées dump on every cold start. Pickle/parquet the rolled-up graph.
- Click-on-map pour point in Streamlit (currently a text field).
- Strahler recomputation from the graph (don't trust `numero_d_ordre`).

## Current state

- **Data** (gitignored):
  - `data/raw/troncon_hydrographique_pyrenees.parquet` — **62111 features**,
    bbox `-0.80,42.60,0.65,43.55`. 6652 rivers, 2755 named, 1639 roots.
    Graph builds in 7.4 s.
  - `data/raw/bassin_versant_topographique_pyrenees.parquet` — 532 polygons,
    covers the tronçon bbox comfortably (`-1.857,42.333 → 3.357,43.639`).
  - `data/raw/troncon_hydrographique_gavarnie_sample.geojson` — 3097 edges.
    **Keep as the offline test fixture** (fast).
- **Code**: `hydro/network.py` (tronçon → DiGraph), `hydro/trace.py` (pour-point
  upstream trace), `hydro/rivers.py` (roll up to `RiverNetwork`), `watershed.py`
  (`catchment_polygon` dissolve), `app.py` (Streamlit navigator, pydeck +
  static matplotlib map), `dump.py`, `sources/wfs.py`. 50 tests pass.
- The wider re-dump fixed the false-root problem: **la Neste** went 30.2 km /
  root → **158.2 km flowing into la Garonne**, and its polygon over-reach dropped
  from 0.34° to 0.009°.

## Reference: BD TOPO quirks worth not rediscovering

- `troncon_hydrographique` wants `BBOX` in `urn:ogc:def:crs:EPSG::4326`
  (lat,lon). Plain `EPSG:4326` returns 0 features. The watershed layer accepts
  plain `EPSG:4326` — do not assume uniformity across layers.
- Segments connect by shared node id: B follows A when `B.ini == A.fin`. Node ids
  say how segments *touch*, not which way water flows — that is
  `sens_de_l_ecoulement` (`Sens inverse` ⇒ swap the nodes before adding the edge).
- A tronçon is **not** one reach between confluences: the layer also splits on
  attribute changes, so segments are short (median 210 m, up to 84 per river).
  Confluences are always splits, though.
- `fictif` marks virtual connectors drawn through lakes/braids (~11% of the
  gavarnie sample) to keep the network connected.
- BD TOPO over-splits some watercourses into `cours_d_eau` records that point at
  each other at the confluence → `_merge_cycles` contracts each SCC>1 so the
  river graph stays a DAG.
