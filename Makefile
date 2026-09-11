# valleespyr — build the static site published on GitHub Pages.
#
# The site is a single self-contained page: the Garonne river catalog rendered
# as a collapsible git-graph + catchment mini-map (no server, no external
# assets). GitHub Pages serves it from docs/ on the default branch.
#
#   make site      regenerate docs/index.html from the local tronçon dump
#   make preview    serve docs/ at http://localhost:8000
#   make clean      remove the generated page
#
# `make site` needs the raw BD TOPO tronçon dump locally (it is NOT in git —
# ~29 MB). Point TRONCONS at it, or drop it at the default path below. Get one
# with:  valleespyr hydro fetch --bbox "<minx,miny,maxx,maxy>" -o <file>
# (or the project's existing data-prep step).

TRONCONS ?= data/raw/troncon_hydrographique_pyrenees.parquet
BASSINS  ?= data/raw/bassin_versant_topographique_pyrenees.parquet

# la Garonne — resolved to its stable cours_d_eau id so `make site` never trips
# over the "2 rivers match 'la Garonne'" ambiguity.
ROOT ?= COURDEAU0000002000894629

PAGE     := docs/index.html
CATALOG  := data/processed/garonne_catalog.json

.PHONY: site preview clean

site: $(PAGE)

# Regenerate whenever the source dump, the generator, or the renderer changes.
$(PAGE): $(TRONCONS) $(BASSINS) \
         src/valleespyr/catalog.py src/valleespyr/render/catalog_html.py
	@test -f "$(TRONCONS)" || { \
	  echo "missing $(TRONCONS) — set TRONCONS=... (see Makefile header)"; exit 1; }
	mkdir -p docs data/processed
	uv run valleespyr catalog $(ROOT) \
	  --from-file "$(TRONCONS)" \
	  $(if $(wildcard $(BASSINS)),--bassins "$(BASSINS)",) \
	  --geo \
	  -o "$(CATALOG)" \
	  --html "$(PAGE)"
	@echo "built $(PAGE)"

preview: $(PAGE)
	@echo "serving docs/ at http://localhost:8000  (Ctrl-C to stop)"
	cd docs && python3 -m http.server 8000

clean:
	rm -f $(PAGE) $(CATALOG)
