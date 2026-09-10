"""Pull the human layer of a valley — trails, roads, huts, summits, cols — from OpenStreetMap.

The DEM gives us terrain and :mod:`valleespyr.hydro` gives us the stream network,
but everything a walker reads off a topo — the GR10, a shepherd's cabane, the
name and altitude of the col at the head of the valley — lives in OSM. This
module fetches it for a bbox in one Overpass query and hands back plain
``GeoDataFrame``\\ s, one per drawn layer, all in EPSG:4326.

Only :func:`fetch_osm_features` is public. It caches the raw Overpass JSON under
``data/raw/osm/<bbox>.json`` (same pattern as
:func:`valleespyr.hydro.dem.fetch_dem`), so re-rendering a valley is offline and
Overpass is hit at most once per extent.

Cross-border by construction: Overpass does not stop at the FR/ES frontier, so a
catchment like the Neste de Rioumajou comes back with its Spanish head-of-valley
peaks and trails too — the gap the BD TOPO tronçon network has.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from pathlib import Path

import geopandas as gpd
from shapely.geometry import LineString, Point, Polygon

logger = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_UA = "valleespyr/0.1 (+https://github.com/xdze2/valleespyr)"

# One Overpass query for the whole human layer of a valley. ``{bbox}`` is filled
# with "south,west,north,east" (Overpass order). ``out tags center`` gives us a
# representative point for ways/relations without their full geometry, which is
# all the point layers need; the line/area layers re-request geometry below.
_QUERY = """
[out:json][timeout:{timeout}];
(
  way["highway"~"^(path|footway|track|bridleway|steps|cycleway)$"]({bbox});
  way["highway"~"^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|service|road)$"]({bbox});
  relation["route"="hiking"]({bbox});
  way["waterway"~"^(river|stream|canal)$"]({bbox});
  way["natural"="water"]({bbox});
  way["landuse"="reservoir"]({bbox});
  way["natural"="glacier"]({bbox});
  node["natural"="peak"]({bbox});
  node["natural"="saddle"]({bbox});
  node["mountain_pass"="yes"]({bbox});
  node["tourism"~"^(alpine_hut|wilderness_hut)$"]({bbox});
  node["amenity"="shelter"]({bbox});
  node["building"="cabin"]({bbox});
  node["place"~"^(city|town|village|hamlet|isolated_dwelling)$"]({bbox});
);
out tags geom;{maxsize}
"""

Bbox = tuple[float, float, float, float]  # (west, south, east, north), WGS84


# --------------------------------------------------------------------------- fetch


def _cache_path(bbox: Bbox, cache_dir: Path) -> Path:
    w, s, e, n = (round(c, 4) for c in bbox)
    return cache_dir / f"osm_{w}_{s}_{e}_{n}.json"


def _overpass(bbox: Bbox, *, timeout: int, retries: int) -> dict:
    """POST the query to Overpass, retrying on the transient 429 / 504 it throws under load."""
    w, s, e, n = bbox
    body = _QUERY.format(bbox=f"{s},{w},{n},{e}", timeout=timeout, maxsize="")
    data = urllib.parse.urlencode({"data": body}).encode()
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(OVERPASS_URL, data=data, headers={"User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout + 30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:  # 429 rate-limit, 504 gateway timeout
            last_exc = exc
            if exc.code not in (429, 504) or attempt == retries:
                raise
            wait = 5 * attempt
            logger.warning("Overpass %s, retry %d/%d in %ds", exc.code, attempt, retries, wait)
            time.sleep(wait)
        except urllib.error.URLError as exc:
            last_exc = exc
            if attempt == retries:
                raise
            time.sleep(5 * attempt)
    raise RuntimeError(f"Overpass failed after {retries} attempts: {last_exc}")


# ------------------------------------------------------------------------- parsing

# Which drawn layer an element belongs to, and how. Order matters only for the
# first match that wins in :func:`_classify`.
_PATH_HW = {"path", "footway", "track", "bridleway", "steps", "cycleway"}


def _classify(tags: dict) -> str | None:
    """Map an element's tags to one of our layer names, or ``None`` to drop it."""
    if tags.get("natural") == "peak":
        return "peaks"
    if tags.get("natural") == "saddle" or tags.get("mountain_pass") == "yes":
        return "cols"
    if tags.get("tourism") in ("alpine_hut", "wilderness_hut"):
        return "huts"
    if tags.get("amenity") == "shelter" or tags.get("building") == "cabin":
        return "huts"
    if tags.get("place") in ("city", "town", "village", "hamlet", "isolated_dwelling"):
        return "settlements"
    if tags.get("route") == "hiking":
        return "routes"
    if tags.get("waterway") in ("river", "stream", "canal"):
        return "water_lines"
    if tags.get("natural") == "water" or tags.get("landuse") == "reservoir":
        return "water_areas"
    if tags.get("natural") == "glacier":
        return "glaciers"
    hw = tags.get("highway")
    if hw in _PATH_HW:
        return "paths"
    if hw:
        return "roads"
    return None


def _geom_from_element(el: dict):
    """Shapely geometry for one Overpass element (``out geom`` / ``out center``)."""
    etype = el["type"]
    if etype == "node":
        return Point(el["lon"], el["lat"])
    if "geometry" in el:
        pts = [(p["lon"], p["lat"]) for p in el["geometry"]]
        if len(pts) < 2:
            return None
        if etype == "way" and pts[0] == pts[-1] and len(pts) >= 4:
            return Polygon(pts)
        return LineString(pts)
    if "center" in el:  # relations with 'out center' only
        return Point(el["center"]["lon"], el["center"]["lat"])
    return None


def _huts_role(tags: dict) -> str:
    """staffed refuge vs unstaffed cabane/shelter — drives the symbol."""
    if tags.get("tourism") == "alpine_hut":
        return "refuge"
    return "cabane"


def _peak_elev(tags: dict) -> float | None:
    raw = tags.get("ele")
    if raw is None:
        return None
    try:
        return float(str(raw).split()[0].replace(",", "."))
    except ValueError:
        return None


_LAYER_GEOM = {
    "peaks": "point",
    "cols": "point",
    "huts": "point",
    "settlements": "point",
    "routes": "line",
    "water_lines": "line",
    "paths": "line",
    "roads": "line",
    "water_areas": "area",
    "glaciers": "area",
}


def _to_frames(payload: dict) -> dict[str, gpd.GeoDataFrame]:
    """Split the Overpass element list into one ``GeoDataFrame`` per layer (EPSG:4326)."""
    buckets: dict[str, list[dict]] = {name: [] for name in _LAYER_GEOM}
    for el in payload.get("elements", []):
        tags = el.get("tags", {})
        layer = _classify(tags)
        if layer is None:
            continue
        geom = _geom_from_element(el)
        if geom is None or geom.is_empty:
            continue
        want = _LAYER_GEOM[layer]
        gt = geom.geom_type
        if want == "point" and gt != "Point":
            continue
        if want == "line" and gt not in ("LineString", "MultiLineString"):
            continue
        if want == "area" and gt not in ("Polygon", "MultiPolygon"):
            # a lake mapped as an unclosed way — skip rather than guess
            continue
        rec = {
            "osm_id": f"{el['type']}/{el['id']}",
            "name": tags.get("name"),
            "geometry": geom,
        }
        if layer == "peaks":
            rec["ele"] = _peak_elev(tags)
        elif layer == "cols":
            rec["ele"] = _peak_elev(tags)
        elif layer == "huts":
            rec["kind"] = _huts_role(tags)
        elif layer == "settlements":
            rec["place"] = tags.get("place")
        elif layer == "roads":
            rec["highway"] = tags.get("highway")
        elif layer == "paths":
            rec["sac_scale"] = tags.get("sac_scale")
        buckets[layer].append(rec)

    frames: dict[str, gpd.GeoDataFrame] = {}
    for name, recs in buckets.items():
        if recs:
            frames[name] = gpd.GeoDataFrame(recs, geometry="geometry", crs="EPSG:4326")
        else:
            frames[name] = gpd.GeoDataFrame(
                {"osm_id": [], "name": [], "geometry": []},
                geometry="geometry",
                crs="EPSG:4326",
            )
    return frames


# ---------------------------------------------------------------------------- api


def fetch_osm_features(
    bbox: Bbox,
    *,
    cache_dir: Path | None = None,
    timeout: int = 90,
    retries: int = 4,
    refresh: bool = False,
) -> dict[str, gpd.GeoDataFrame]:
    """Fetch the human layer of a valley from OSM as per-layer ``GeoDataFrame``\\ s.

    ``bbox`` is ``(west, south, east, north)`` in WGS84. Returns a dict keyed by
    layer name — ``peaks``, ``cols``, ``huts``, ``settlements``, ``routes``,
    ``water_lines``, ``water_areas``, ``paths``, ``roads``, ``glaciers`` — each an
    (possibly empty) ``GeoDataFrame`` in EPSG:4326. Attribute columns per layer:

    * ``peaks`` / ``cols`` — ``name``, ``ele`` (metres, may be ``NaN``)
    * ``huts`` — ``name``, ``kind`` (``"refuge"`` staffed vs ``"cabane"``)
    * ``settlements`` — ``name``, ``place``
    * ``roads`` — ``highway``; ``paths`` — ``sac_scale``

    The raw Overpass JSON is cached at ``<cache_dir>/osm_<w>_<s>_<e>_<n>.json``
    (``cache_dir`` defaults to ``data/raw/osm``); pass ``refresh=True`` to
    re-query even when the cache is warm.
    """
    cache_dir = cache_dir or Path("data/raw/osm")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = _cache_path(bbox, cache_dir)

    if cache.exists() and cache.stat().st_size > 0 and not refresh:
        logger.info("cached OSM %s (%.2f MB)", cache, cache.stat().st_size / 1e6)
        payload = json.loads(cache.read_text(encoding="utf-8"))
    else:
        logger.info("querying Overpass for %s", bbox)
        payload = _overpass(bbox, timeout=timeout, retries=retries)
        cache.write_text(json.dumps(payload), encoding="utf-8")
        logger.info(
            "wrote %s (%d elements, %.2f MB)",
            cache,
            len(payload.get("elements", [])),
            cache.stat().st_size / 1e6,
        )

    frames = _to_frames(payload)
    logger.info(
        "OSM layers: %s",
        ", ".join(f"{k}={len(v)}" for k, v in frames.items() if len(v)),
    )
    return frames


__all__ = ["fetch_osm_features"]
