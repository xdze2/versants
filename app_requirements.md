# App requirements

What valleespyr should do, and what the UI looks like. This is the product
spec — no implementation choices here (those are in
[app_design.md](app_design.md)).

## Scope

One thing, built well: a **browsable catalog of Pyrenean river valleys**,
published as a single static webpage.

In scope:

1. A **river catalog** — the full named-river network of the Pyrenees (or a
   study area within it), browsable as a tree rooted at a trunk river.
2. A **valley selector UI** — a git-graph-style two-level tree plus a map,
   for picking a valley and seeing where it sits in the river system.
3. A **3D terrain view** for a selected valley — heightmap, draped stream
   network, real basemap texture.
4. The **data pipeline** that produces the catalog and terrain data from
   public sources, as a documented, runnable CLI — not just the published
   site.

Out of scope (deliberately dropped from the earlier exploration):

- The 2D printable topo-plate export (contours + OSM human layer as an SVG
  "map plate"). Revisit only if the catalog + 3D view prove insufficient.
- The Streamlit interactive navigator. The static site is the product; a
  local dev server is a nice-to-have, not a deliverable.
- Anything not reachable from "Garonne down to a headwater" — no other river
  basins outside the Pyrenees.

## Core concept: the valley tree

The mental model is a **git commit graph**, not a directory tree or a flat
map:

| git                          | valley tree                                    |
|-------------------------------|------------------------------------------------|
| commit                        | a river                                         |
| parent commit                 | the river it flows into                         |
| merge commit                  | a confluence                                    |
| `main`                        | the trunk, e.g. la Garonne                      |
| `git log --first-parent`      | the trunk walked from a headwater to the root   |

Each river has exactly one **mainline** parent-direction neighbour upstream
(the dominant water path — the same valley, continuing) and zero or more
**tributaries** (each the tip of a side-branch joining at this confluence,
ordered biggest catchment first).

This matters because it is *the* way a user finds a valley: not by name
search on a flat list, not by clicking around a 2D map, but by walking the
tree — "I'm on the Garonne, what feeds it near Saint-Gaudens? the Neste —
what feeds the Neste? ...".

## User-facing features

### 1. Valley selector

- Two-level tree view, centered on a **currently selected river**:
  - one row above: the downstream river (a "go back up" link to its parent
    valley);
  - the selected river, highlighted;
  - its tributaries listed below, biggest first, each showing name and a
    couple of key facts (length, drainage area if known).
  - Rendered as a real git-graph: lanes, not indentation — a lane runs
    unbroken through the trunk and curves in at each confluence.
- Clicking a tributary re-centers the view one level down (that tributary
  becomes the new selection); clicking the "go back" link re-centers one
  level up.
- The page URL reflects the selected river (so a valley is linkable /
  shareable / bookmarkable, and back/forward navigation works).
- A text filter to jump straight to a river by name, for when a user already
  knows what they're looking for.
- Hovering a river (in the tree or on the map) previews it before clicking.

### 2. Map

- Shows the selected river's course and its upstream catchment network on a
  real basemap.
- When the selected valley's drainage area is known and "reads as one
  valley" (roughly 5–150 km²), the map masks out everything outside that
  valley's catchment boundary — reinforcing "this valley is its own bounded
  world", not a flat bird's-eye plane shared with its neighbours.

### 3. 3D terrain view

- For a valley with precomputed terrain data, the map is replaced (or
  supplemented) by an interactive 3D relief block:
  - a heightmap of the catchment, rendered as solid terrain (not just a
    draped surface — it should read as a piece of the mountain, like a
    physical relief model);
  - the traced stream network draped on top;
  - a real aerial/topographic basemap texture draped on the surface, not a
    flat color or synthetic hillshade;
  - orbit/pan/zoom controls; runs in-browser with no plugins.
- Not every river needs this — it's for valley-sized catchments a user would
  actually want to look at in 3D, not every 2 km headwater trickle.

### 4. Browsing facts

Per river, the UI should be able to show at minimum:
- name;
- length;
- Strahler stream order (a rough "how major is this river" signal);
- number of valleys (rivers) upstream of it;
- drainage area, when known (from official watershed data or delineated from
  terrain).

## Non-functional requirements

- **No backend.** The published artifact is static files (HTML/JS/CSS/JSON)
  servable from GitHub Pages or any static host. No database, no login, no
  server-side computation at view time.
- **Self-contained enough to actually load fast.** The full river tree for a
  reasonably sized study area (e.g. the whole Garonne catchment, ~600
  rivers) should load and render as one page without a per-click network
  round trip for the tree/map data. Per-valley 3D terrain can be lazy-loaded
  per selection (it's much heavier than the tree/map data).
- **Offline-first data pipeline.** Fetching from public data sources
  (IGN, Copernicus) is a separate, explicit, re-runnable step from building
  the site — a contributor should be able to rebuild the published page from
  a local data cache without re-fetching anything, and re-run only the
  affected step when a data source changes.
- **Resumable/idempotent batch steps.** Any pipeline step that processes many
  rivers (e.g. terrain baking) must be safe to interrupt and re-run without
  redoing already-finished work, and must not let one bad river's failure
  abort the whole batch.
- **Reproducible from scratch.** Given the documented data sources and the
  CLI, a new contributor should be able to regenerate the entire published
  site without any undocumented manual step.

## Explicitly deferred / not required for v1

- Search/filter by hiking-relevant attributes (has a lake, has a refuge, no
  road access, etc.) — a plausible future feature, not required now.
- Coverage beyond a single well-defined study area (e.g. all of the French
  Pyrenees, or cross-border into Spain).
- Any user accounts, saved state, or personalization.
- Mobile-specific UI (should not break on mobile, but is not designed
  mobile-first).
