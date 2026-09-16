# valleespyr — build the static site published on GitHub Pages.
#
# The site is a single static page: one river catalog rendered as a
# collapsible git-graph + a map column (IGN/OSM basemap tiles fetched live
# for the 2D Leaflet fallback; a catalog built without --geo stays fully
# self-contained). Selecting a river with a baked terrain file (see `make
# terrain`) switches that map column to a three.js 3D scene with its own
# baked IGN basemap drape instead. GitHub Pages serves it from docs/ on the
# default branch.
#
#   make site      regenerate docs/index.html from the local tronçon dump
#   make terrain    bake per-river 3D terrain + IGN basemap into TERRAIN_DIR
#   make preview    serve docs/ at http://localhost:8000
#   make clean      remove the generated page
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

# Neste de Rioumajou — resolved to its stable cours_d_eau id so `make site`
# never trips over a name-ambiguity match. This is the only page the site
# publishes; there is no separate 2D-only build anymore.
ROOT ?= COURDEAU0000002000907013

PAGE      := docs/index.html
CATALOG   := data/processed/rioumajou_catalog.json
PAGE_DATA := docs/catalog_index.json

.PHONY: site terrain preview clean

site: $(PAGE)

# Regenerate whenever the source dump, the generator, or the renderer changes.
# DEM_CATCHMENTS and TERRAIN_DIR are intentionally not prerequisites: neither
# may exist yet (make site still works without them — see the --dem-catchments
# and --terrain-dir guards below) and both are produced by separate, slow
# batches (valley catchments precompute / terrain-precompute), not something
# a plain `make site` should trigger.
# `--html` also writes $(PAGE_DATA) alongside $(PAGE) (the JSON the shell
# fetches on load) — not listed as an explicit target since both come from
# the same `valleespyr catalog` invocation.
$(PAGE): $(TRONCONS) $(BASSINS) \
         src/valleespyr/catalog.py src/valleespyr/render/catalog_html.py \
         src/valleespyr/render/static/catalog_3d.js
	@test -f "$(TRONCONS)" || { \
	  echo "missing $(TRONCONS) — set TRONCONS=... (see Makefile header)"; exit 1; }
	mkdir -p docs data/processed
	uv run valleespyr catalog $(ROOT) \
	  --from-file "$(TRONCONS)" \
	  $(if $(wildcard $(BASSINS)),--bassins "$(BASSINS)",) \
	  $(if $(wildcard $(DEM_CATCHMENTS)),--dem-catchments "$(DEM_CATCHMENTS)",) \
	  $(if $(wildcard $(TERRAIN_DIR)),--terrain-dir "$(TERRAIN_DIR)",) \
	  --geo \
	  -o "$(CATALOG)" \
	  --html "$(PAGE)"
	@echo "built $(PAGE) and $(PAGE_DATA)"

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

preview: $(PAGE)
	@echo "serving docs/ at http://localhost:8000  (Ctrl-C to stop)"
	@echo "note: index.html fetches $(PAGE_DATA) — open it via this server, not file://"
	cd docs && python3 -m http.server 8000

clean:
	rm -f $(PAGE) $(PAGE_DATA) $(CATALOG)
