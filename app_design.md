# App design

Technical choices for rebuilding valleespyr to the scope in
[app_requirements.md](app_requirements.md): stack, data sources, algorithms,
and the pipeline that turns raw data into the published static site.

## Architecture overview

```
                     OFFLINE (Python CLI, run by a contributor / CI)
┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│ IGN BD TOPO  │──▶│ river graph  │──▶│ valley tree  │──▶│ catalog.json │
│ (WFS/dump)   │   │ (networkx)   │   │ (JSON, git-  │   │ + index.html │
└──────────────┘   └──────────────┘   │  graph shape)│   └──────────────┘
                                       └──────────────┘          │
┌──────────────┐   ┌──────────────┐   ┌──────────────┐          │
│ Copernicus   │──▶│ catchment    │──▶│ per-river    │──────────┤
│ GLO-30 DEM   │   │ delineation  │   │ terrain.json │          │
│ (S3, no key) │   │ (pysheds)    │   │ (heightmap + │          │
└──────────────┘   └──────────────┘   │  streams +   │          │
                                       │  IGN texture)│          ▼
┌──────────────┐                      └──────────────┘   ┌──────────────┐
│ IGN WMTS     │──────────────────────────────────────────▶│ docs/  │
│ basemap tiles│              (baked once per valley)     │ (static site,│
└──────────────┘                                          │  GitHub Pages)│
                                                           └──────────────┘
                                                                   │
                                                          ONLINE (browser)
                                                          no server, no DB
```

Everything above the dotted line into `docs/` runs offline, ahead of time.
The browser only ever reads static JSON/HTML/JS and draws.

## Stack

| Concern                     | Choice                                   | Why |
|------------------------------|-------------------------------------------|-----|
| Data pipeline / CLI          | Python, `click`                          | Good geo-data ecosystem (geopandas, rasterio, pysheds, shapely, networkx). |
| River topology               | `networkx.DiGraph`                       | Graph is the natural structure; upstream/downstream walks are graph traversals. |
| Vector geo data               | `geopandas` / `shapely`                  | Reading WFS/GeoParquet, dissolving polygons, geometry ops. |
| Raster / DEM                 | `rasterio` + `pysheds`                   | Standard hydrology raster stack; pysheds gives D8 flow routing + watershed delineation. |
| Published output             | Static HTML + vanilla JS, no framework   | No backend requirement; a build step, not a running app — a JS framework buys nothing here. |
| 2D map                       | Leaflet + IGN/OSM tile layers            | Lightweight, no API key needed for the basemap tiles used. |
| 3D terrain view               | three.js (from CDN at view time)         | Only mainstream option for in-browser WebGL with no plugins. |
| Site hosting                  | GitHub Pages, serving `docs/`            | Free, static, matches "no backend" requirement. |

No JS build step (bundler/transpiler) — the published page is authored
JS/CSS directly, so "view source" on the output is the actual code.

## Data sources

| Source | What we take from it | Access |
|---|---|---|
| IGN BD TOPO® v3, `troncon_hydrographique` | Stream segments: geometry, flow direction, Strahler order, `cours_d_eau` id, nature/fictif flags | Géoplateforme WFS 2.0, or a bulk local dump (GeoParquet) for repeated runs |
| IGN BD TOPO® v3, `bassin_versant_topographique` | Pre-computed drainage-basin polygons, keyed to a watercourse | Same WFS, bulk dump |
| Copernicus GLO-30 DEM | ~30 m global elevation, for catchment delineation and 3D terrain | Public unauthenticated AWS S3 bucket `copernicus-dem-30m` (1°×1° COG tiles, plain HTTPS, no key, no rate limit) |
| IGN Plan IGN (WMTS) | Basemap texture, for the 2D Leaflet map (live tiles) and baked into the 3D terrain block (fetched once, offline) | Key-free WMTS endpoint, Web Mercator tile grid |

A local bulk dump (GeoParquet) is the standard input for pipeline runs, not
live WFS paging — faster, reproducible, and avoids re-fetching identical data
on every rebuild. Live WFS access remains available for exploring a new area
or refreshing the dump itself.

## Algorithm: from stream segments to a valley tree

### 1. Segment graph → directed, downstream-pointing

Build a `networkx.DiGraph` from `troncon_hydrographique`, one edge per
segment, oriented downstream:

- A segment flagged "Sens inverse" is un-swapped by flipping its two
  endpoint nodes (not its geometry).
- A segment flagged "Double sens" or "Indéterminé" (canals, tidal reaches)
  has no reliable downstream direction — keep its drawn orientation but mark
  it `ambiguous=True`; never guess.
- Parallel edges between the same node pair (braided channels) collapse to
  the longer one; self-loops are dropped.

### 2. Segment graph → river graph

Roll individual segments up into **rivers**, one node per named watercourse:

- Group every edge carrying a `cours_d_eau` id by that id (first id when a
  segment lists several, `/`-joined).
- An edge with **no** id (an unnamed reach) merges into whichever named river
  it drains into.
- A `cours_d_eau` that has an id but **no name** — a headwater BD TOPO
  tracked but nobody named — is folded into the named river directly
  downstream of it. Left standalone these are pure noise in a browsable
  tree (this step alone can cut the node count nearly in half on a real
  catchment).
- Collapsing the above can turn an indirect path between two named rivers
  into a direct edge, creating a cycle — detect and merge these too.

The result: a much smaller `DiGraph`, one node per river, one "flows into"
edge to its parent, each node carrying its segment set, total length, outlet
node, and Strahler order. This is the graph every later step (a river's own
path, its upstream catchment, tree navigation) walks.

**Known data quirks to handle:**
- This WFS layer only returns features when queried with the URN CRS form
  and then answers in **lat, lon** order — swap axes back on ingest from the
  live API (a local dump is assumed already lon, lat).
- Null text fields arrive inconsistently as float `NaN`, pandas `NA`, or the
  literal strings `"nan"`/`"None"` — normalize all to Python `None` before
  any name-matching or id-grouping, or the river roll-up silently
  mis-groups.
- A river's recorded outlet node can be short of its true mouth after the
  cycle-merge step above; re-walk the river's own edges downstream from the
  recorded outlet to find the actual terminal node before using it as a
  DEM pour point.

### 3. River graph → git-graph-shaped valley tree

Pick a root river (e.g. la Garonne). For each river, split its upstream
neighbours (children in the flow-into graph) into:

- **mainline**: the one child continuing the same valley upstream — the
  child sharing this river's own name if one does, else the child with the
  largest sub-catchment. `None` at a headwater.
- **tributaries**: every other child, ordered by where it joins (mouth to
  source, the order met walking upstream) — each the tip of a side branch.

A river with more than one downstream (a genuine bifurcation — anabranch,
delta, canal tap) is attached under its first downstream in topological
order; the alternates are recorded as `also_flows_into` rather than
duplicating the node.

Each node also carries: length, Strahler order, count of valleys upstream,
a **study-local Pfafstetter-style code** (a path from the chosen root over
the *loaded* network — sorts and supports "is upstream of" *within one
catalog build*, not comparable across a different root or a published
Pfafstetter dataset), and drainage `area_km2` where a watershed polygon
covers it.

## Algorithm: catchment delineation

Two sources feed a catchment polygon, tried in this order — WFS first
because it's authoritative where it exists, DEM only fills the gap:

### A. WFS `bassin_versant_topographique` (preferred)

Dissolve the pre-computed polygons keyed to a river's segment set. Covers
only a fraction of named rivers, and only for a whole watercourse (not an
arbitrary point on it) — the DEM path below exists entirely to cover what
this can't.

### B. DEM delineation (fills the gap)

Given a river with no WFS polygon:

1. **bbox**: bounds of the river's own upstream stream network, padded 15%
   per side plus an extra 10% on the sides away from the outlet (the real
   drainage divide keeps widening upstream of mapped streams).
2. **fetch**: download the covering Copernicus GLO-30 tile(s); mosaic when
   the bbox spans more than one 1°×1° cell.
3. **condition** (once per tile, expensive): pit-fill → depression-fill →
   resolve-flats, then D8 flow direction + flow accumulation over the whole
   tile. Log a warning if more than 5% of cells get filled — may be
   swallowing a real glacial cirque rather than a DEM artifact.
4. **snap the pour point**: the river's outlet rarely lands exactly on the
   DEM's own channel cells — snap to the nearest cell where flow
   accumulation exceeds a channel threshold (default ≈0.9 km² contributing
   area, a sane channel head for a Pyrenean torrent). Warn if the snap moves
   the point more than a few cells.
5. **delineate + vectorize**: trace every cell draining to the snapped
   point (pysheds `grid.catchment()`), vectorize the mask, keep the largest
   connected component, simplify lightly, measure area in an equal-area CRS
   (Lambert-93 is equal-area enough here).
6. **sanity check**: `course_inside_frac` — the fraction of the river's own
   mapped course that falls inside the delineated polygon. Low values catch
   a wrong-basin delineation before it's trusted.

**Confluence pixel ambiguity, and why one snap isn't enough.** When a
tributary's true mouth sits only one or two DEM cells from a much larger
trunk stream, a single-threshold snap has no way to prefer the tributary's
own small inflow cell over the neighbouring trunk cell — the accumulation
raster genuinely can't resolve the two as separate flow paths for a stretch
near the confluence at 30 m posting. The fix that actually works: don't
trust one snap. Walk back along the river's own segment chain by a handful
of offsets (0, 100, …, ~1 km — try the unmodified pour point first, since
most rivers need no offset), try each against a couple of channel
thresholds, all against one shared conditioned grid (condition once per
river, not once per candidate). Score every candidate by
`course_inside_frac`; keep the best if it clears ~0.9; report the river
**undetermined** — no polygon written — if nothing does, rather than caching
a wrong basin. This is a search over plausible pour points, not a smarter
single heuristic — the ambiguity lives in the terrain data itself.

**Batch driver requirements:**
- Keep a result only if the *measured* area falls in a "reads as one valley"
  range (roughly 5–150 km²) — this range is also what the map-masking and
  3D-view eligibility (in the app) key off.
- Write output after every kept river, not just at the end — a run over
  hundreds of rivers is long enough to hit an unrecoverable native crash.
- Run each river's delineation in its own subprocess. `pysheds`'
  `grid.catchment()` has been observed to corrupt memory (an unrecoverable
  C-level abort, not a catchable Python exception) on a tile whose channel
  mask is nearly empty — guard with a minimum-channel-cells check that
  raises a clean error first, but don't rely on the guard alone: isolate
  each river's native calls in a subprocess so a crash only kills that one
  worker.
- One river's failure (bad geometry, a degenerate bbox, an unresolved
  confluence) must not abort the batch — log and skip, keep going.

## Algorithm / pipeline: 3D terrain view

Per valley eligible for a 3D view (has a catchment polygon that reads as
"one valley"):

1. Re-run DEM delineation (search variant, not the plain snap — see above)
   to get the catchment polygon and conditioned grid already in hand.
2. Clip the DEM to the catchment polygon (plus a small padding apron of
   cells so the block doesn't end on a knife edge), keep only the largest
   connected component, and export:
   - `terrain.npy` / equivalent — a float array of elevation, `NaN` outside
     the divide, plus a small JSON of geographic bounds, cell size, and
     z-range;
   - `streams.json` — the traced stream network as polylines, densified and
     normalized into the same `[0,1]×[0,1]` grid space as the heightmap, so
     the client drapes them with no separate UV math.
3. Bake a basemap texture: fetch the IGN WMTS tiles covering the same bbox,
   mosaic in Web Mercator, and warp onto the *exact same grid* the heightmap
   uses — texture and heightmap line up pixel-for-pixel with no client-side
   georeferencing. Baking this offline (rather than live WMTS-to-mesh-UV in
   the browser) is a deliberate simplification: the alternative is
   reimplementing tile mosaicking and warping in JavaScript for a one-shot
   payload the client would otherwise fetch every view.
4. Write one self-contained JSON per river; the page loads it lazily only
   when that river is selected (never inline everything in the main
   catalog payload — this data is orders of magnitude larger per-river than
   the tree/map data).

Client side: three.js builds a solid extruded terrain mesh from the
heightmap (reads as a physical relief block, not a floating sheet), drapes
the stream polylines and the baked basemap texture on top, and provides
orbit/pan/zoom.

## Build pipeline (offline)

Stages, each independently re-runnable and cacheable:

1. **Ingest**: bulk-dump `troncon_hydrographique` and
   `bassin_versant_topographique` for the study area to local GeoParquet.
   (Manual/occasional — data doesn't change often.)
2. **River graph**: build the segment graph → river graph (§ above) from the
   local dump. Cheap, deterministic, safe to always rebuild.
3. **Catchment precompute** (batch, slow): for every river in the tree
   without WFS coverage, DEM-delineate a catchment polygon; cache results
   keyed by river id, including "undetermined" outcomes so they aren't
   endlessly retried. Resumable; a crash loses at most the river in flight.
4. **Terrain precompute** (batch, slow, network-heavy): for every river with
   a catchment polygon in the "one valley" range, bake `terrain.json`
   (heightmap + streams + basemap texture). Resumable — a river whose output
   file already exists is skipped.
5. **Catalog build**: walk the river graph from the chosen root into the
   git-graph-shaped JSON tree, folding in catchment polygons (for map
   masking) and the set of rivers with baked terrain (so the UI knows which
   selections can offer a 3D view). Render to one self-contained
   `index.html` plus the per-river `terrain.json` files it lazy-loads.
6. **Publish**: commit the generated `docs/` (or the relevant subset — raw
   dumps and intermediate caches stay out of git) and let GitHub Pages serve
   it from the default branch.

A single entry point (e.g. a `Makefile` or equivalent task runner) should
chain 2–6 given the stage-1 dump already present locally, so "rebuild the
site" is one command once the raw data is on disk.

## Deliberately out of scope for this design

- No incremental/live re-computation — the site is a build artifact,
  rebuilt from scratch (or from cached intermediate stages) on demand, never
  computed per-request.
- No attempt to make the study-local Pfafstetter code comparable across
  catalog builds with a different root — it's an internal sort/ordering aid,
  not a published identifier scheme.
- No cross-tile-sharing of DEM conditioning across different rivers in the
  batch precompute — each river conditions its own (possibly overlapping)
  tile. Worth revisiting only if the batch runtime becomes a real problem at
  full-Pyrenees scale.
