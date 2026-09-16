# valleespyr

**Catalogue des vallées des Pyrénées** — an atlas for exploring the Pyrenees
one valley at a time, built from open topographic and hydrographic data.

## Vision

The Pyrenees are not a flat map — they're a highly folded territory made of
hundreds of valleys, each a small world of its own. Two valleys that look
close as the crow flies can be a full day's drive apart by road, separated by
a ridge. There's no natural "next" or "nearby" between them — only the river
network that drains them, and the mountain passes that occasionally connect
them.

**valleespyr** treats *rivers*, not roads or grid tiles, as the map's spine.
Every named watercourse in the range is a node in one big tree, rooted at the
Garonne. Browsing the tree — trunk to tributary, tributary to headwater — is
the way to move through the mountains: pick a valley, see where it sits in
its river system, drop into a 3D view of its terrain, then climb back down
and branch off into the next one.

The result ships as one static, self-contained webpage: no backend, no
database, no login — open the page and start exploring.

## What it looks like

A two-level **drainage-tree selector**: the trunk river runs down the left as
a lane, tributaries branch off it (visually not unlike a commit graph), and
picking one re-centers the view one level down. Next to it, a map traces the
selected valley's course and catchment; picking a valley with a baked terrain
model swaps that map for an interactive 3D relief block — heightmap, draped
stream network, and a real IGN aerial/topo texture — built from open
elevation and map data.

![la Garonne — river drainage-tree selector](docs/images/garonne_catalog_html.png)

![Gave de Lutour — 3D terrain block, IGN basemap draped over a DEM heightmap](docs/images/lutour_3d_preview.png)

## How it's built, in one paragraph

IGN's BD TOPO stream-segment layer is rolled up into a graph of whole rivers
("flows into" edges), which gives the browsable tree and each valley's
catchment network for free. Where IGN's own watershed layer doesn't cover a
river, its drainage basin is delineated instead from a Copernicus GLO-30
elevation model. Everything — the tree, the maps, the 3D terrain blocks — is
precomputed offline into static JSON/HTML and published as a GitHub Pages
site with no server component.

See **[app_requirements.md](app_requirements.md)** for what the app does and
its UI, and **[app_design.md](app_design.md)** for the technical choices,
data sources, and algorithms behind it.

## Status

This repository is being rebuilt from scratch around the minimal scope
captured in those two documents, after an earlier, messier exploration phase
established what was worth keeping: the river-graph catalog, the DEM
catchment pipeline, and the 3D terrain view.
