# valleespyr — build the static site published on GitHub Pages.
#
# The site is one page: a collapsible git-graph + a map column (IGN/OSM
# basemap tiles fetched live for the 2D Leaflet fallback; a catalog built
# without --geo stays fully self-contained). Every root river in
# config/study_area.yaml is a top-level branch of one synthetic root node —
# Garonne and Adour aren't separate sites, they're siblings in the same tree,
# same as any confluence splits into tributaries — so switching valleys is
# just the existing "go back" / tributary-row navigation, not a picker.
# Selecting a river with a baked terrain file (see `make terrain`) switches
# that map column to a three.js 3D scene with its own baked IGN basemap drape
# instead. GitHub Pages serves it from docs/ on the default branch.
#
#   make site      regenerate docs/index.html from the local tronçon dump,
#                   one tree spanning every root in study_area.yaml
#   make terrain    bake per-river 3D terrain + IGN basemap into TERRAIN_DIR
#   make preview    serve docs/ at http://localhost:8000
#   make clean      remove the generated pages
#
# `make site` needs the raw BD TOPO tronçon dump locally (it is NOT in git —
# ~29 MB). Point TRONCONS at it, or drop it at the default path below. Get one
# with:  valleespyr hydro fetch --bbox "<minx,miny,maxx,maxy>" -o <file>
# (or the project's existing data-prep step).

TRONCONS ?= data/raw/troncon_hydrographique_pyrenees.parquet
BASSINS  ?= data/raw/bassin_versant_topographique_pyrenees.parquet
DEM_CATCHMENTS ?= data/processed/catchments.json
# Must live under docs/: GitHub Pages (and `make preview`'s http.server) only
# serves that directory, so a terrain dir outside it 404s in the browser even
# though the file exists on disk.
TERRAIN_DIR ?= docs/terrain

STUDY_AREA ?= config/study_area.yaml
VALLEY_OVERRIDES ?= config/valley_overrides.yaml

CATALOG_DIR := data/processed

.PHONY: site terrain preview clean

# `site` is .PHONY so it always rebuilds on direct request rather than
# tracking the output's timestamp — simpler than a pattern rule, at the cost
# of no incremental skip when nothing changed (see PREVIEW_DEPS below for
# that skip, used by `make preview` instead).
# Override with `make ROOT=<id-or-name> site` to build just one river's own
# catalog to docs/index.html instead of the whole study area (e.g. the old
# Rioumajou test build: `make ROOT=COURDEAU0000002000907013 site`).
site:
	@test -f "$(TRONCONS)" || { \
	  echo "missing $(TRONCONS) — set TRONCONS=... (see Makefile header)"; exit 1; }
	@echo "== make site: TRONCONS=$(TRONCONS)"
	mkdir -p docs $(CATALOG_DIR)
ifdef ROOT
	@echo "== building single root $(ROOT) -> docs/"
	uv run valleespyr catalog "$(ROOT)" \
	  --from-file "$(TRONCONS)" \
	  $(if $(wildcard $(BASSINS)),--bassins "$(BASSINS)",) \
	  $(if $(wildcard $(DEM_CATCHMENTS)),--dem-catchments "$(DEM_CATCHMENTS)",) \
	  $(if $(wildcard $(TERRAIN_DIR)),--terrain-dir "$(TERRAIN_DIR)",) \
	  $(if $(wildcard $(VALLEY_OVERRIDES)),--overrides "$(VALLEY_OVERRIDES)",) \
	  --geo \
	  -o "$(CATALOG_DIR)/catalog.json" \
	  --html "docs/index.html"
else
	uv run valleespyr catalog-forest \
	  --study-area "$(STUDY_AREA)" \
	  --from-file "$(TRONCONS)" \
	  $(if $(wildcard $(BASSINS)),--bassins "$(BASSINS)",) \
	  $(if $(wildcard $(DEM_CATCHMENTS)),--dem-catchments "$(DEM_CATCHMENTS)",) \
	  $(if $(wildcard $(TERRAIN_DIR)),--terrain-dir "$(TERRAIN_DIR)",) \
	  $(if $(wildcard $(VALLEY_OVERRIDES)),--overrides "$(VALLEY_OVERRIDES)",) \
	  --geo \
	  -o "$(CATALOG_DIR)/catalog.json" \
	  --html "docs/index.html"
endif
	@echo "== make site: done"

# Bakes one <river_id>.json (heightmap + streams + contours + IGN basemap)
# per DEM-sourced river in DEM_CATCHMENTS. Slow and network-heavy (WMTS
# tile fetches for --basemap); safe to re-run — rivers already baked are
# skipped, so an interrupted run resumes.
terrain: $(DEM_CATCHMENTS)
	uv run valleespyr valley catchments terrain-precompute "$(DEM_CATCHMENTS)" \
	  --from-file "$(TRONCONS)" \
	  --basemap \
	  -o "$(TERRAIN_DIR)"
	@echo "baked $(TERRAIN_DIR)/"

# Skips the rebuild when docs/index.html already looks up to date — `site`
# itself stays .PHONY (always rebuilds on direct request), but re-running
# `make preview` repeatedly (the common case: tweak, look, tweak, look)
# shouldn't re-walk the whole tronçon network each time. "Up to date" means
# newer than the raw data, the study-area config, and the renderer's own
# source (Python + the static JS/CSS it inlines) — anything narrower risks a
# silently stale preview after an unrelated code change.
PREVIEW_DEPS := $(TRONCONS) $(STUDY_AREA) $(wildcard $(VALLEY_OVERRIDES)) \
  $(wildcard src/valleespyr/render/*.py src/valleespyr/render/static/*.js \
             src/valleespyr/*.py src/valleespyr/hydro/*.py)
preview: docs/index.html
	@echo "== serving docs/ at http://localhost:8000  (Ctrl-C to stop)"
	@echo "note: the page fetches its own catalog_index.json — open via this server, not file://"
	cd docs && python3 -m http.server 8000

docs/index.html: $(PREVIEW_DEPS)
	$(MAKE) --no-print-directory site

clean:
	rm -f docs/index.html docs/catalog_index.json
	rm -f $(CATALOG_DIR)/*.json
