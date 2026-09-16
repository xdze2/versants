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

- [ ] Add a small loader (`valleespyr/config.py`?) that parses
      `study_area.yaml` into the bbox/CRS/sources values already threaded
      through `dump.py`/`cli.py` as explicit params.
- [ ] Resolve the `roots` list's `cours_d_eau_id: null` placeholders (la
      Garonne, l'Agout) to real ids — needed before they're usable as CLI
      `catalog <root>` arguments.
- [ ] Decide + implement how the Makefile picks up the config: read
      `ROOT`/bbox from `study_area.yaml` by default, keep `TRONCONS ?=` /
      `ROOT ?=` as override-friendly `make` variables layered on top (so
      `make ROOT=... site` still works for one-off testing).
- [ ] Once wired, either extend `roots` to support more than one build
      target cleanly (a `make site` per root, or a `--all-roots` mode) or
      explicitly defer multi-root builds and note it as a known limitation.

## 3. Finish the vanilla-JS shell to match the current UI spec

`docs/index.html` is the old prototype (predates the lane-based tree spec,
hover-preview, filter box in app_requirements.md §"User-facing features").
Rebuilding it is blocked on data-out-of-HTML (item 1) landing first, since
otherwise it's rework.

- [ ] Tree selector: lanes (not indentation) — trunk lane runs unbroken,
      tributaries curve in at confluence, per app_requirements.md.
- [ ] URL reflects selected river (linkable/bookmarkable, back/forward
      works).
- [ ] Text filter to jump to a river by name.
- [ ] Hover preview (tree row ↔ map) before clicking.
- [ ] Map masking: when drainage area is known and in the "reads as one
      valley" range (~5–150 km²), mask outside the catchment boundary.
- [ ] 3D view swap-in for valleys with baked `terrain.json`, lazy-loaded
      per-selection (already largely working per README screenshots —
      confirm it still works once data loading changes under item 1).
- [ ] Svelte decision: **deferred** (see app_design.md "Open question") —
      revisit only once the above UI shape has stopped changing. Don't
      start a Svelte migration before this list's items are otherwise done.

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
