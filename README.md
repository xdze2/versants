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
```

## Layout

```
config/            per-valley config (bbox, pour point, CRS, source URLs)
src/valleespyr/
  cli.py           click CLI  (group: `wfs`)
  watershed.py     fetch / select topographic watersheds
  sources/wfs.py   minimal OGC WFS 2.0 client
data/raw|processed getignored working data
tests/             offline unit tests
```

## First valley

`config/gavarnie.yaml` — Gave de Pau headwaters / Cirque de Gavarnie
(Hautes-Pyrénées). Compact, dramatic relief, entirely on the French side.
