# Method

How the river tree is derived, and how catchment (drainage basin) polygons
are computed. See `todo.md` / `next.md` for open issues and history.

## 1. River tree

**Data source:** IGN BD TOPO® v3, layer `BDTOPO_V3:troncon_hydrographique`,
served live via the Géoplateforme WFS (`sources/wfs.py`) or from a local
GeoParquet/GeoJSON dump (`hydro/network.py::load_troncons`). Each row is one
*tronçon*: a stream segment between two hydrographic nodes, with a
`sens_de_l_ecoulement` flow flag, a Strahler order, a `nature`/`fictif` flag,
and — when the segment belongs to a named watercourse — a stable
`liens_vers_cours_d_eau` id.

**Algorithm:**

1. `build_graph` (`hydro/network.py`) turns the tronçon table into a
   `networkx.DiGraph` where every edge points **downstream**. BD TOPO draws
   some segments upstream-first (`"Sens inverse"`); those are un-swapped by
   flipping the edge's two endpoint nodes (not its geometry). Segments flagged
   `"Double sens"` or `"Indéterminé"` (canals, tidal reaches) keep their drawn
   orientation and are marked `ambiguous=True` rather than guessed at.
   Parallel edges between the same node pair (braided channels) collapse to
   the longer one; self-loops are dropped.
2. `build_river_network` (`hydro/rivers.py`) rolls individual tronçons up
   into **rivers**: every edge with a `liens_vers_cours_d_eau` id is grouped
   by that id (first id when several are `/`-joined); an edge with no id (an
   *unnamed reach*) is merged into whichever named river it drains into. The
   result is a much smaller river-level `DiGraph` — one node per watercourse,
   one "flows into" edge to its parent — with each `River` carrying its
   segment set, total length, outlet node, Strahler order, and parent/child
   ids.
3. Everything downstream of this (a river's own path, its full upstream
   catchment as a set of rivers, tree navigation) is a walk over this river
   graph — see `RiverNetwork.upstream_rivers` / `downstream_path`.

**Parameters:** none tunable beyond the WFS bbox / local dump chosen as
input; the roll-up is driven entirely by BD TOPO's own `cours_d_eau` ids.

**Known difficulties:**

- **Axis-order quirk**: this WFS layer only returns features when queried
  with the URN CRS (`urn:ogc:def:crs:EPSG::4326`), and then answers in
  **lat, lon** order instead of lon, lat. `fetch_troncons` swaps the axes
  back on ingest; a local dump is assumed already lon, lat.
- **Inconsistent nulls**: text fields (names, ids) come back as float `NaN`,
  pandas `NA`, or the literal strings `"nan"`/`"None"` depending on the
  GDAL/pandas version — normalised to Python `None` in `_prepare`, since an
  un-normalised null breaks both name-matching and the river roll-up's
  "same id" grouping.
- **`river.outlet` can be mid-course**: the river-graph construction's cycle
  merge can leave a river's recorded outlet node short of its true mouth.
  `valley.py::outlet_point` re-walks the river's own edges downstream from
  `river.outlet` to find the actual terminal node, rather than trusting it
  directly.
- **Ambiguous flow direction**: `"Double sens"`/`"Indéterminé"` segments
  (canals, tidal stretches) have no reliable "downstream" — they're tagged
  `ambiguous=True` and left for the caller to filter or special-case, not
  silently guessed.

## 2. Catchment polygons

Two independent sources feed a catchment polygon, tried in this order:

### 2a. WFS `bassin_versant_topographique` (preferred when available)

**Data source:** BD TOPO v3, layer `BDTOPO_V3:bassin_versant_topographique`
(`watershed.py`, `sources/wfs.py`) — IGN's own pre-computed drainage-basin
polygons. Loaded as a local dump or fetched live; `catchment_polygon`
dissolves the polygons keyed to a given set of river ids into one shape.

**Difficulty:** this layer only covers a fraction of named rivers, and keys
each polygon to a whole watercourse rather than an arbitrary point on it — it
cannot hand back "the catchment above this exact outlet". That gap is what
the DEM path below exists to fill.

### 2b. DEM delineation (fills the gap)

**Data source:** Copernicus GLO-30 (~30 m posting) global DEM. Default
source is the public, unauthenticated AWS S3 bucket `copernicus-dem-30m`
(one 1°×1° COG per grid cell, no key, no rate limit); an OpenTopography REST
fallback exists for `demtype` values S3 doesn't carry, but needs a free API
key and caps at 50 downloads/24 h.

**Algorithm** (`hydro/dem.py`, orchestrated by `valley.py::delineate_river`):

1. **bbox**: `catchment_bbox` takes the bounds of the river's own catchment
   stream network (from the BD TOPO graph, §1) and pads every side by 15% of
   that side's span, plus an extra 10% on the two sides *away from* the
   outlet — the real drainage divide keeps widening upstream of the mapped
   streams.
2. **fetch**: the DEM tile(s) covering that bbox are downloaded and, when the
   bbox spans more than one 1°×1° cell, mosaicked with `rasterio.merge`.
3. **condition** (once per tile): pit-fill → depression-fill → resolve-flats,
   then D8 flow direction and flow accumulation over the whole tile. Filling
   more than 5% of the tile's cells is logged as a warning — a sign the fill
   may be swallowing a real glacial cirque/tarn rather than a DEM artifact.
4. **snap the pour point**: the river's outlet coordinate (from §1's
   `outlet_point`) rarely lands exactly on the DEM's own channel cells, so it
   is snapped onto the nearest cell where flow accumulation exceeds
   `ACC_CHANNEL_CELLS` (default 1000 cells, ≈0.9 km² of contributing area —
   a sane channel head for a Pyrenean torrent). A snap moving the point more
   than `SNAP_RADIUS_CELLS` (8 cells) logs a warning.
5. **delineate + vectorise**: `grid.catchment()` (pysheds) traces every cell
   draining to the snapped point; the resulting mask is vectorised, the
   largest connected component kept, lightly simplified (~half a cell), and
   its area measured in EPSG:2154 (Lambert-93, equal-area enough for this).
6. **sanity check**: `course_inside_frac` measures what fraction of the
   river's *own* mapped course (from BD TOPO) falls inside the delineated
   polygon. Below 90% is logged as a warning — the catchment likely locked
   onto the wrong drainage — but is **not** currently rejected automatically
   (see difficulties below).

**Batch driver** (`valleespyr valley catchments precompute`, `cli.py`):
walks a river's whole upstream tree (§1), skips anything already covered by
the WFS polygon or already cached, delineates the rest from DEM, and keeps a
result only if its *measured* area falls in `[5, 150] km²` — the range that
"reads as one valley" on the catalog map. Results are written to
`data/processed/catchments.json` after every kept river (not just at the
end), since a run over ~600 rivers is long enough to hit an unrecoverable
native crash that no Python `except` can catch.

**Known difficulties:**

- **Confluence pixel ambiguity** — the sharpest one in practice. When a
  tributary's true mouth sits only one or two DEM cells from a much larger
  trunk stream, the accumulation-threshold snap has no way to prefer the
  tributary's own (small) inflow cell over the neighbouring trunk cell — it
  just grabs whichever qualifying cell is nearest. Confirmed case: *Ruisseau
  d'Aube*, whose vector outlet sits one cell from the Neste du Louron's
  ~18,000-cell channel; the snap locked onto the trunk stream and
  `grid.catchment()` faithfully delineated *that* basin instead — a
  plausible-looking but wrong 12.7 km² polygon, with only 3% of Aube's own
  course inside it (correctly flagged by `course_inside_frac`, but not
  rejected — the batch's only hard gate is measured area, so a wrong-basin
  result can still pass through it and get cached).
- **Not actually recursive** — the batch driver iterates the upstream-river
  list flatly: each river gets its own from-scratch DEM condition + trace,
  with no reuse of a parent's flow grid and no check that a child's polygon
  nests inside its parent's. The `[5, 150] km²` area filter discards small
  headwater tributaries outright rather than merging them into anything, so
  "recursive" here is closer to "batched over the ancestor list" than a true
  tree composition.
- **Native crash on sparse tiles** — pysheds 0.5's `grid.catchment()` has
  been observed to corrupt memory (`double free or corruption`, an
  unrecoverable C-level abort, not a catchable Python exception) when a
  tile's channel mask has 0–1 cells above `ACC_CHANNEL_CELLS`. Guarded by a
  `MIN_CHANNEL_CELLS` (5) check that raises a clean `RuntimeError` before
  ever calling `grid.catchment()` on a tile that sparse — found by bisecting
  a reproducible segfault down to one specific river/tile.
- **DEM tile cache doesn't actually share tiles across rivers**, despite the
  intent described in `hydro/dem.py`'s docstring. `fetch_dem_s3` names its
  *cropped/mosaicked* output file by the exact padded bbox it was called
  with, and the precompute batch passes a fresh, river-specific bbox for
  every river — so in practice `data/raw/dem/` fills up with hundreds of
  near-duplicate, heavily overlapping crops (only the two raw per-cell S3
  tiles they're built from are actually shared). Only the raw elevation tile
  is cached at all; slope/hillshade/shadow are always recomputed on the fly
  at render time (`render/hillshade.py`, `render/plate.py`) and never
  written to disk.
- **No true ground truth to validate against** — `course_inside_frac`
  against the river's own BD TOPO course is the only automatic check;
  outside of visual spot-checks there's no independent basin-area source to
  confirm a DEM-delineated polygon is right even when it lands in the
  expected area range.
