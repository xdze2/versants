# TODO — path to a first clean version

Working list to close the gap between the current code and
[app_requirements.md](app_requirements.md) / [app_design.md](app_design.md).
"Clean" here means: no dead code, `docs/index.html` matches the documented
architecture (thin shell + external JSON, no inlined data), and the pipeline
is driven by config rather than hardcoded/ad-hoc flags. Not a feature
wishlist — scope is already fixed by the two spec docs.

## 1. Data-out-of-HTML (the historical-choice fix)

`render/catalog_html.py::render_catalog_html` currently does
`json.dumps(catalog)` straight into an inline `<script id="catalog-data">`
tag in the published page. This is the one concrete architecture mismatch
between the current code and app_design.md §5 ("Catalog build").

- [x] Split `render_catalog_html` into two steps: write `catalog_index.json`
      (the full tree/map payload) as its own file, and render `index.html`
      as a shell that `fetch()`s it on load instead of embedding it.
- [x] Update `catalog_map.js` / `catalog_graph.js` / `catalog_3d.js` (in
      `src/valleespyr/render/static/`) to read from the fetched payload
      instead of assuming a `#catalog-data` script tag is present.
      (Only `catalog_graph.js` read the tag directly; `catalog_map.js` /
      `catalog_3d.js` already received data as a function argument, so they
      needed no change.)
- [x] Update the Makefile's `site` target and `preview` target if the output
      filename/shape changes (currently `docs/index.html` is the single
      `$(PAGE)` target).
      (Added `PAGE_DATA := docs/catalog_index.json`; `clean` now removes it
      too, and `preview` prints a note about needing the server, not `file://`.)
- [x] Confirm `make preview`'s `python3 -m http.server` still serves the new
      JSON file correctly (it should — no code change needed, just a sanity
      check) and that a `file://` open of `index.html` is *not* relied upon
      anywhere (fetch of a local file needs a server, unlike the old inline
      version — worth a one-line README note if so).
      (Verified with a real `valleespyr catalog` build + `http.server` +
      headless Chrome screenshot — tree, map and click-to-select all work
      off the fetched JSON. README had no `file://` references to fix.)

## 2. Wire `config/study_area.yaml` into the pipeline

The config file exists (bbox, CRS, WFS source block, `roots` list) but
nothing reads it yet — `TRONCONS`/`BASSINS`/`ROOT` are still hardcoded
Makefile variables, and `--bbox` is a per-invocation CLI flag with no config
backing.

- [x] Add a small loader (`valleespyr/config.py`?) that parses
      `study_area.yaml` into the bbox/CRS/sources values already threaded
      through `dump.py`/`cli.py` as explicit params.
      (Added `valleespyr/config.py::load_study_area` /
      `load_study_area_if_present` — a `StudyArea` dataclass with
      `bbox_wgs84`/`bbox_str`/`crs`/`wfs_endpoint`/`troncon_layer`/
      `watershed_layer`/`roots`. Wired into `cli.py`: the top-level
      `--wfs-endpoint` and `wfs dump`'s `--bbox`/`--srs` now default from
      `config/study_area.yaml` when present in the cwd, still overridable by
      an explicit flag; `--cql` on `wfs dump` still works unbounded since the
      bbox default is only applied when neither `--bbox` nor `--cql` was
      passed. `dump.py`/`hydro`/`valley`/`catalog` commands are unchanged —
      they take a local `--from-file`, not the live WFS, so nothing to
      default there. Tests in `tests/test_config.py`.)
- [x] Resolve the `roots` list's `cours_d_eau_id: null` placeholders to real
      ids — needed before they're usable as CLI `catalog <root>` arguments.
      (Resolved via `valleespyr hydro rivers show <name> --from-file
      data/raw/troncon_hydrographique_pyrenees.parquet`: la Garonne →
      `COURDEAU0000002000894629` (53.9 km, unambiguous). l'Agout turned out
      to be a mistake in the original config — it's a Tarn tributary near
      Castres, outside the Pyrenees/Garonne study area entirely (confirmed
      absent from the local dump). Swapped for l'Adour, the other major
      Pyrenees-draining river, resolved the same way to
      `COURDEAU0000002000898951` (334.0 km, unambiguous vs. its "Bras de
      l'Adour" side channels).)
      Follow-up: narrowed `bbox_wgs84` from `[-1.5, 42.0, 3.5, 43.5]`
      (reached Toulouse) to `[-1.7, 42.0, 3.1, 43.15]` — a Pyrenees-only
      band capped on the north at Lourdes / Saint-Girons latitude, per
      explicit correction. La Garonne and l'Adour are scoped to their
      Pyrenean headwaters, not their lowland course; both resolved ids
      still fall inside the new bbox, no re-resolution needed.
- [x] Decide + implement how the Makefile picks up the config: read
      `ROOT`/bbox from `study_area.yaml` by default, keep `TRONCONS ?=` /
      `ROOT ?=` as override-friendly `make` variables layered on top (so
      `make ROOT=... site` still works for one-off testing).
      (`ROOT ?=` now shells out to `python3 -c "from valleespyr.config import
      load_study_area; print(load_study_area(...).roots[0].query)"` —
      resolves to la Garonne's id by default, still overridable with `make
      ROOT=<id> site`. Renamed the hardcoded `CATALOG` target from
      `rioumajou_catalog.json` to `catalog.json` since it's no longer
      Rioumajou-specific by default. `TRONCONS`/`BASSINS` still point at the
      small existing dump (`[-0.82, 42.58, 0.68, 43.57]`), narrower than the
      now-updated `study_area.yaml` bbox (`[-1.7, 42.0, 3.1, 43.15]`) —
      today's build is a 355-tributary partial catchment limited to what's
      in that dump, same limitation item 4 is meant to resolve by
      re-dumping at the full study-area bbox. Verified with
      `make site` + `http.server` + headless Chrome screenshot: tree and map
      render la Garonne's 98 local rivers correctly, 53.86 km / order 7 /
      2337.8 km² for the root segment.)
- [x] Once wired, either extend `roots` to support more than one build
      target cleanly (a `make site` per root, or a `--all-roots` mode) or
      explicitly defer multi-root builds and note it as a known limitation.
      (Went with all-roots-by-default, per explicit direction — comment out
      a root in `study_area.yaml` to skip it, rather than a separate CLI
      flag/mode. Added `RootRiver.slug` (`config.py`) to turn a name into a
      filesystem-safe stand-in, e.g. "l'Adour" -> "adour". `make site` now
      loops `study_area.yaml`'s `resolved_roots()`: the first root
      (la Garonne) builds to `docs/index.html` as the site's front page,
      every other root to its own `docs/<slug>/index.html` +
      `catalog_index.json`, with `data/processed/<slug>.json` keeping their
      intermediate catalogs from colliding. The loop is a shell `while read`
      over a `_build_root` sub-make target, not a Make pattern rule, to
      avoid fighting Make's static rule matching for a dynamically-shelled
      root list. `make ROOT=<id> site` still overrides to a single build at
      `docs/index.html`, bypassing the study-area list entirely (e.g. the
      old Rioumajou test build). `site` is now .PHONY — always rebuilds all
      roots on request rather than tracking per-root file timestamps, so
      `make site` no longer no-ops when nothing changed (a deliberate
      simplicity trade-off). `clean` now sweeps every `docs/**/index.html` +
      `catalog_index.json` plus all of `data/processed/*.json`. Verified
      both pages for real: la Garonne (99 local rivers) at `docs/index.html`
      unchanged from before, l'Adour (202 local rivers, 333.97 km, 1071.3
      km²) newly at `docs/l-adour/index.html` with its 3D terrain_url
      correctly relative (`../terrain`) — both screenshotted via headless
      Chrome + `http.server`. Added `tests/test_config.py::test_root_river_
      slug` (accented-name case included: "l'Échez" -> "l-echez").)

## 3. Finish the vanilla-JS shell to match the current UI spec

`docs/index.html` is the old prototype (predates the lane-based tree spec,
hover-preview, filter box in app_requirements.md §"User-facing features").
Rebuilding it is blocked on data-out-of-HTML (item 1) landing first, since
otherwise it's rework.

- [x] Tree selector: lanes (not indentation) — trunk lane runs unbroken,
      tributaries curve in at confluence, per app_requirements.md.
      (Already implemented pre-existing: `catalog_graph.js::drawGraph`.)
- [x] URL reflects selected river (linkable/bookmarkable, back/forward
      works).
      (Added to `catalog_graph.js`: `focusOn` now takes `{pushHash}` and
      pushes `#<river_id>` via `history.pushState` on every user-driven
      selection; a `popstate` listener restores the river named by the new
      hash — or the root when the hash is empty — via
      `focusOn(id, {pushHash: false})` so it doesn't re-push. Initial load
      reads `location.hash` to select that river (falling back to
      `meta.root_id`) and passes it through to `_catalogInitGeo` as a new
      `initialId` param, consumed by both `catalog_map.js` and
      `catalog_3d.js` so the map/3D column opens already framed on the
      linked river instead of re-focusing the root and immediately
      re-pushing a competing history entry. Verified end-to-end with a
      scripted Chrome DevTools Protocol session: click-selects push a hash,
      `history.back()`/`forward()` restore the right river and tree state,
      and reloading with `#<id>` in the URL opens directly on that river
      with correct breadcrumbs.)
- [x] Text filter to jump to a river by name.
      (Already implemented pre-existing: `catalog_graph.js::applyFilter`.)
- [x] Hover preview (tree row ↔ map) before clicking.
      (Added, as highlight-only per explicit direction — no camera move, no
      infobox change, no terrain fetch on hover. Tree row → map/3D: row
      `mouseover`/`mouseout` in `catalog_graph.js` call a new
      `window._catalogMapPreview(id|null)`, implemented in `catalog_map.js`
      as an amber polyline on a dedicated Leaflet `previewPane`, and in
      `catalog_3d.js` as an amber footprint restyle (`FOOTPRINT_PREVIEW`)
      alongside the existing selected/context colors. Map/3D → tree row:
      hovering a context polyline (now tracked per-id in `ctxLines`) or a
      3D footprint (new `pointermove`/`pointerleave` raycast in
      `catalog_3d.js`) calls a new `window._catalogRowPreview(id|null)` in
      `catalog_graph.js` that toggles a `.preview` CSS class on the matching
      row. New `.labels .row.preview` style in `catalog_html.py`. Verified
      via scripted CDP session: hovering a tree row triggers the map-side
      preview call with no exceptions, and driving `_catalogRowPreview`
      directly (standing in for the map/3D hover path) correctly adds/
      removes `.preview` on the right row — screenshotted.)
- [x] Map masking: when drainage area is known and in the "reads as one
      valley" range (~5–150 km²), mask outside the catchment boundary.
      (Already implemented pre-existing: `catalog_map.js::paintMask`, gated
      on `catalog.py`'s `_VALLEY_AREA_*` range.)
- [x] 3D view swap-in for valleys with baked `terrain.json`, lazy-loaded
      per-selection.
      (Confirmed still working after item 1's fetch-based data loading:
      rebuilt `docs/index.html` + `docs/l-adour/index.html` via `make site`
      and screenshotted both in headless Chrome — terrain fetch, footprint
      fallback for un-baked rivers, and orbit controls all work unchanged.)
- [ ] Svelte decision: **deferred** (see app_design.md "Open question") —
      revisit only once the above UI shape has stopped changing. Don't
      start a Svelte migration before this list's items are otherwise done.

## 3b. Bugs found in review, now fixed

- [x] `make preview`'s staleness check only watched `docs/index.html`, but
      `make site` (which it calls when stale) builds one page per root —
      `docs/index.html` plus `docs/<slug>/index.html` for every other root.
      A change touching only a non-front-page root (e.g. l'Adour) looked
      "up to date" forever and `make preview` silently kept serving a stale
      or missing secondary-root page.
      (Fixed: `PREVIEW_PAGES` now resolves every root's output path from
      `study_area.yaml` via the same `load_study_area(...).resolved_roots()`
      call `site` already uses, and both `preview`'s prerequisite and the
      rebuild rule (`$(PREVIEW_PAGES) &: $(PREVIEW_DEPS)`, a grouped-target
      rule — needs GNU Make ≥ 4.3) target the whole list instead of just
      `docs/index.html`. Verified: touching `config/study_area.yaml` after
      freshening only `docs/index.html` now correctly triggers a rebuild
      that previously would have been skipped.)
- [x] The 2D Leaflet map (`catalog_map.js`) wired hover-preview on every
      river polyline but never click-to-select, unlike the 3D view
      (`catalog_3d.js`), which already calls `focusOn(id)` on a footprint
      click — an inconsistency between the two map backends for the same
      interaction (`app_requirements.md`'s tree-first design intent means
      neither backend is *required* to support map clicks, but having only
      one of the two do so was an unintended asymmetry, not a choice).
      (Fixed: added `line.on('click', () => focusOn(id))` next to the
      existing hover handlers in `catalog_map.js`; updated the module
      docstring and the on-page hint text ("click a row or the map to
      select it") to match. Verified with a scripted Chrome session:
      selected a leaf river from the tree first (so the highlight pane
      only covers a sliver of the map, leaving other rivers' lines
      actually clickable), then clicked a distinct context river's line —
      confirmed the tree selection, URL hash, and infobox all updated to
      match, same as a tree-row click.)

## 3c. Per-valley hand-curated overrides (new, found while smoke-testing the pipeline)

Smoke-tested the pipeline end-to-end (`make site` + headless-Chrome
screenshots) before committing to the full-Pyrenees run — see 3b's bugs for
the same kind of check. Found the 3D view's auto-computed initial camera
shot can look wrong (e.g. Ruisseau de Lastie: clipped into the hillside):
`shotFor()` in `catalog_3d.js` derives camera height/backoff purely from the
valley's *horizontal* footprint size, with no reference to actual relief
(`z_min`/`z_max`), so a narrow/steep or wide/flat valley can get a
mismatched shot. No per-valley data file existed to hand-fix cases like this
without changing the general formula.

- [x] Added `config/valley_overrides.yaml` — hand-curated, per-river
      `cours_d_eau_id`-keyed data, loaded by
      `valleespyr.config.load_valley_overrides`/`_if_present`. Two fields
      for now: `blacklist: true` (drops the river, and everything upstream
      of it, from the tree — folded into the parent's `n_folded`, same
      mechanism as a DEM-undetermined leaf) and `camera` (spherical
      override for the 3D view's initial shot: `azimuth_deg`,
      `elevation_deg`, `distance_m`, `target_height_m`, all optional).
      Designed to grow: room for future curated fields per the file's
      header comment.
      (`build_catalog` gained an `overrides` param, wired into the existing
      `keep(cid)` child-filter closure for blacklist and into
      `_build_geo`'s per-river dict (`geo.rivers[id]["camera"]`) for camera.
      `catalog_3d.js`'s `shotFor()` checks `g.camera` first, converting the
      spherical params into the same `{pos, target}` shape the computed
      fallback produces, so the rest of the fly-to/orbit code is unchanged.
      CLI: new `catalog --overrides <path>` option, defaulting to
      `config/valley_overrides.yaml` when present (same pattern as
      `--wfs-endpoint`'s `study_area.yaml` default). Makefile: `_build_root`
      passes `--overrides` when `$(VALLEY_OVERRIDES)` exists (same
      `$(wildcard ...)`-gated pattern as `--bassins`/`--dem-catchments`),
      and it's in `PREVIEW_DEPS` so editing it triggers `make preview`'s
      rebuild. Added one real entry (Lastie's camera) and confirmed with a
      before/after headless-Chrome screenshot — the computed shot clipped
      into the hillside, the hand-tuned one shows the whole stream network
      from above. Tests: `tests/test_config.py` (parsing, empty file,
      missing file, the real project file loads) and
      `tests/test_catalog.py` (blacklist folds + drops from the tree
      without disturbing unrelated rivers, camera passes through to
      `geo.rivers` only for the overridden id). Full suite: 137 passed.
      Screenshots saved to `docs/images/test_screenshots/` for reference.)
- [ ] The underlying computed-default formula in `shotFor()` is still blind
      to relief — this override file is a hand-tunable escape hatch, not a
      fix to the general case. Worth revisiting once more baked valleys
      exist (item 4) and it's clear how common a bad default actually is:
      might be worth deriving `height`/`target.y` from the terrain
      payload's `z_min`/`z_max`/`z_exaggeration` instead of horizontal size
      alone, which would shrink how often a hand override is needed at all.

## 4. Full-Pyrenees / multi-root batch run

Currently only one root (`ROOT ?= COURDEAU...` for Neste de Rioumajou) has
ever been built end-to-end. Needed for a "first clean version" that
actually matches the stated scope (la Garonne down to headwaters):

- [ ] Run `wfs dump` for the full `study_area.yaml` bbox (~large — Garonne
      catchment + Pyrenees), replacing whatever partial/sample dumps exist
      in `data/raw/`.
- [ ] Run the catchment DEM-delineation batch (`valley catchments
      terrain-precompute` / the DEM precompute step) over the full river
      set — this is the slow, resumable, subprocess-isolated step per
      app_design.md's "Batch driver requirements". Expect some rivers to
      come back "undetermined" — that's expected behavior, not a bug.
- [ ] Run terrain precompute for every catchment in the "one valley" range.
- [ ] Rebuild `catalog_index.json` + `index.html` rooted at la Garonne
      (not Rioumajou) as the actual published page.
- [ ] Sanity-check load time / payload size at full scale (~600 rivers per
      the non-functional requirement) — this is the first real test of the
      "loads as one page, no per-click round trip" requirement at target
      scale.

## 5. Remaining dead-code / cleanup sweep

Most of this was done in the "Remove dropped-feature code, add study-area
config" commit. What's left to check once the above lands:

- [ ] `data/processed/rioumajou_catalog.json` — currently kept because it's
      the live output of `make site` with today's hardcoded `ROOT`. Once
      item 4 repoints the build at la Garonne, this file (and any
      Rioumajou-specific leftovers) becomes stale and should be deleted or
      regenerated under its new name.
- [ ] Re-run `uv run ruff check src/ tests/` after items 1–2 land and clear
      the pre-existing 28 lint errors (present before this cleanup too —
      not introduced by it, but worth fixing before calling this "clean").
- [ ] Grep for any remaining references to `bassin_versant`/dump filenames
      that assume the old sample/partial extent rather than the
      `study_area.yaml` bbox, once item 2 is wired up.
- [ ] Confirm `pyproject.toml` extras (`dem`, `render`, `dev`) are each
      still minimal and accurate now that `plate`/`viz`/`app` are gone —
      no leftover transitive deps only the removed code needed.

## Suggested order

1 (data-out-of-HTML) → 3 (finish shell against real data) can proceed in
parallel with 2 (config wiring), since they touch different code. 4 (full
Pyrenees run) depends on 2 being done (need the real bbox/roots) and
benefits from 1 being done first (so the full-scale payload is already in
the right shape rather than re-baked twice). 5 is cleanup that trails
1–4, not a blocker for them.
