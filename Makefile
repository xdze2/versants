# valleespyr — build the static site published on GitHub Pages.
#
# The site is one page per root river in config/study_area.yaml — each a
# collapsible git-graph + a map column (IGN/OSM basemap tiles fetched live
# for the 2D Leaflet fallback; a catalog built without --geo stays fully
# self-contained). Selecting a river with a baked terrain file (see `make
# terrain`) switches that map column to a three.js 3D scene with its own
# baked IGN basemap drape instead. GitHub Pages serves it from docs/ on the
# default branch.
#
#   make site      regenerate docs/index.html + docs/<root>/index.html per
#                   root in study_area.yaml, from the local tronçon dump
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

CATALOG_DIR := data/processed

.PHONY: site terrain preview clean

# All resolved roots in study_area.yaml by default, one page each — comment
# out a root in that file's `roots:` list to skip it. The first root
# (currently la Garonne) is the site's front page (docs/index.html); every
# other root gets its own docs/<slug>/index.html (slug from RootRiver.slug,
# e.g. l'Adour -> docs/adour/). Override with `make ROOT=<id-or-name> site`
# to build just one root to docs/index.html instead (e.g. the old Rioumajou
# test build: `make ROOT=COURDEAU0000002000907013 site`).
# `site` is .PHONY (declared above) so it always rebuilds every root on
# request rather than tracking per-root file timestamps — simpler than a
# pattern rule over a dynamically-shelled root list, at the cost of no
# incremental skip when nothing changed.
site:
	@test -f "$(TRONCONS)" || { \
	  echo "missing $(TRONCONS) — set TRONCONS=... (see Makefile header)"; exit 1; }
	@echo "== make site: TRONCONS=$(TRONCONS)"
	mkdir -p docs $(CATALOG_DIR)
ifdef ROOT
	@echo "== building single root $(ROOT) -> docs/"
	$(MAKE) --no-print-directory _build_root SLUG=catalog QUERY="$(ROOT)" DIR=docs
else
	@uv run python3 -c \
	  "from valleespyr.config import load_study_area; \
	  [print(r.slug, r.query) for r in load_study_area('$(STUDY_AREA)').resolved_roots()]" \
	> /tmp/valleespyr_roots.$$$$; \
	n=$$(wc -l < /tmp/valleespyr_roots.$$$$); \
	echo "== $$n root(s) from $(STUDY_AREA)"; \
	i=0; first=1; \
	while read -r slug query; do \
	  i=$$((i + 1)); \
	  if [ "$$first" = 1 ]; then dir=docs; first=0; else dir="docs/$$slug"; fi; \
	  echo "== [$$i/$$n] building $$slug ($$query) -> $$dir/"; \
	  $(MAKE) --no-print-directory _build_root SLUG="$$slug" QUERY="$$query" DIR="$$dir" \
	    || { rm -f /tmp/valleespyr_roots.$$$$; exit 1; }; \
	done < /tmp/valleespyr_roots.$$$$; \
	rm -f /tmp/valleespyr_roots.$$$$; \
	echo "== make site: done ($$n root(s))"
endif

# One catalog build: $(DIR) (default docs) gets index.html + catalog_index.json,
# named after $(SLUG) in $(CATALOG_DIR) (data/processed/<slug>.json) so
# multiple roots' catalog JSON don't collide.
DIR ?= docs
.PHONY: _build_root
_build_root:
	mkdir -p "$(DIR)"
	uv run valleespyr catalog "$(QUERY)" \
	  --from-file "$(TRONCONS)" \
	  $(if $(wildcard $(BASSINS)),--bassins "$(BASSINS)",) \
	  $(if $(wildcard $(DEM_CATCHMENTS)),--dem-catchments "$(DEM_CATCHMENTS)",) \
	  $(if $(wildcard $(TERRAIN_DIR)),--terrain-dir "$(TERRAIN_DIR)",) \
	  --geo \
	  -o "$(CATALOG_DIR)/$(SLUG).json" \
	  --html "$(DIR)/index.html"
	@echo "built $(DIR)/index.html and $(DIR)/catalog_index.json"

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

# Skips the rebuild when every root's docs/**/index.html already looks up to
# date — `site` itself stays .PHONY (always rebuilds on direct request), but
# re-running `make preview` repeatedly (the common case: tweak, look, tweak,
# look) shouldn't re-walk the whole tronçon network each time. "Up to date"
# means newer than the raw data, the study-area config, and the renderer's
# own source (Python + the static JS/CSS it inlines) — anything narrower
# risks a silently stale preview after an unrelated code change. Checked
# against *every* resolved root's index.html (docs/index.html plus
# docs/<slug>/index.html for each other root), not just the front page — a
# root's page rebuilds every time `site` runs, but was previously never
# rechecked here, so a change touching only a non-front-page root looked
# up to date forever.
PREVIEW_DEPS := $(TRONCONS) $(STUDY_AREA) \
  $(wildcard src/valleespyr/render/*.py src/valleespyr/render/static/*.js \
             src/valleespyr/*.py src/valleespyr/hydro/*.py)
PREVIEW_PAGES := $(shell uv run python3 -c \
	  "from valleespyr.config import load_study_area; \
	  roots = load_study_area('$(STUDY_AREA)').resolved_roots(); \
	  print(' '.join(['docs/index.html'] + ['docs/' + r.slug + '/index.html' for r in roots[1:]]))" \
	  2>/dev/null || echo docs/index.html)
preview: $(PREVIEW_PAGES)
	@echo "== serving docs/ at http://localhost:8000  (Ctrl-C to stop)"
	@echo "note: each page fetches its own catalog_index.json — open via this server, not file://"
	cd docs && python3 -m http.server 8000

$(PREVIEW_PAGES) &: $(PREVIEW_DEPS)
	$(MAKE) --no-print-directory site

clean:
	find docs \( -name index.html -o -name catalog_index.json \) -delete
	rm -f $(CATALOG_DIR)/*.json
