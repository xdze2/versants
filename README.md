# valleespyr

3D visualization of mountain valleys in the Pyrénées (France), built from open
topographic and hydrographic data.

## Status

Early scaffold. First deliverable: a CLI to **explore BD TOPAGE / BD TOPO
topographic watersheds (`bassin versant topographique`) via the IGN Géoplateforme
WFS**.

## Data sources

| Layer | Source | Access |
|---|---|---|
| Topographic watersheds | BD TOPO v3 `bassin_versant_topographique` (same concept as BD TOPAGE) | IGN Géoplateforme WFS 2.0 — `https://data.geopf.fr/wfs/ows` |
| Full BD TOPAGE hydrography | IGN / OFB / Sandre | Sandre WFS (also OGC-compliant) |
| Elevation (planned) | IGN RGE ALTI 1 m / 5 m | Géoplateforme WCS / department tiles |
| Elevation, prototype (planned) | Copernicus GLO-30 | OpenTopography REST API |

The WFS endpoint and layer name are configurable (`--wfs-endpoint`, `--layer`) so
the same tooling points at Sandre or a different dataset.

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # add ",dem,viz" once those stages land
```

## Usage

```bash
# What watershed-related layers does the endpoint advertise?
valleespyr wfs layers --keyword bassin

# Attribute schema of the watershed layer
valleespyr wfs schema

# How many watersheds intersect a bounding box (lon/lat)? — no download
valleespyr wfs count --bbox -0.10,42.65,0.15,42.85

# Fetch watersheds in a bbox as GeoJSON, reprojected to Lambert-93
valleespyr wfs watersheds --bbox -0.10,42.65,0.15,42.85 --srs EPSG:2154 -o data/raw/gavarnie_ws.geojson

# Fetch the watershed containing a pour point
valleespyr wfs watersheds --point -0.0086,42.7350 -o data/raw/gavarnie_pourpoint_ws.geojson

# Bulk-download a whole layer (paged). Output format from the suffix:
#   .geojson              -> streamed GeoJSON, no extra deps
#   .parquet / .gpkg      -> GeoParquet / GeoPackage (needs geopandas + pyarrow)
valleespyr wfs dump -o data/raw/bassin_versant_topographique_fr.parquet          # all of France (~6.6k feats, ~1 min, 99 MB)
valleespyr wfs dump --bbox -2.0,42.3,3.2,43.4 -o data/raw/bv_pyrenees.parquet     # Pyrénées only (~530 feats, 2 MB)
```

### Explore it in the browser

```bash
pip install -e ".[app]"
streamlit run src/valleespyr/app.py      # or: valleespyr-app
```

The Streamlit explorer reads the local dump (falls back to a live WFS bbox
fetch if none is found) and lets you:

- pan/zoom a pydeck map of the watershed polygons
- filter by basin district, toponyme substring, and area
- switch between **sub-catchments** and **dissolved by watercourse**
  (`liens_vers_cours_d_eau_principal` — one polygon per whole named river)
- select rows in the table to highlight them on the map
- download the current selection as GeoJSON / GeoParquet / CSV

### Why WFS and not the bulk file store

IGN's *Service Téléchargement* (`data.geopf.fr/telechargement`) ships BD TOPO as
per-département `.7z` archives of **every** theme (~1–2 GB each), indexed through a
paginated Atom feed that is awkward to script (and rate-limits). The watershed
layer alone is ~6,600 features for all of metropolitan France, so paging the WFS
is the simpler bulk source — no archive, no unwanted themes. Use the file store
later if you need the fine stream-topology layers (`troncon_hydrographique`,
`cours_d_eau`).

## Layout

```
config/            per-valley config (bbox, pour point, CRS, source URLs)
src/valleespyr/
  cli.py           click CLI  (group: `wfs`)
  watershed.py     fetch / select topographic watersheds
  dump.py          bulk-download a whole layer (paged) to GeoJSON/GeoParquet/GPKG
  app.py           Streamlit explorer (map + table + filters + downloads)
  sources/wfs.py   minimal OGC WFS 2.0 client
data/raw|processed getignored working data
tests/             offline unit tests
```

## First valley

`config/gavarnie.yaml` — Gave de Pau headwaters / Cirque de Gavarnie
(Hautes-Pyrénées). Compact, dramatic relief, entirely on the French side.
