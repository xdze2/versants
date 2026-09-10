"""Delineate the Gave de Lutour catchment from a Copernicus GLO-30 DEM.

Self-contained demo (see ``todo.md``). One valley, one known reference:

* **Target** – Gave de Lutour, upper Cauterets valley. Outlet at the confluence
  into the Gave de Pau, ``-0.108792, 42.872834`` (~1030 m). Source near Lac de
  Lutour, ~2580 m.
* **Reference** – ``bassin_versant_topographique`` sub-basin keyed to
  ``cours_d_eau`` id ``COURDEAU0000002000907698``: **39.34 km²**, 173 vertices,
  digitised from BD Carto (last modified 2012). Good enough to check an area and
  a rough shape against, not good enough to drape on 1 m terrain.

Pipeline: fetch COP30 tile -> condition (fill pits, resolve flats) -> D8 flow
direction + accumulation -> snap the BD TOPO outlet onto the DEM channel ->
``grid.catchment`` -> vectorise -> compare to the reference.

Run::

    OPENTOPOGRAPHY_API_KEY=xxxx uv run python scratchpad/dem_lutour.py

Promote ``fetch_dem`` / ``delineate`` to ``src/valleespyr/hydro/dem.py`` once the
numbers below check out.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
import requests
from rasterio import features
from shapely.geometry import shape

# pysheds 0.5 (latest release) still calls np.in1d, removed in NumPy 2.0. It is
# an exact alias of np.isin for the 1-D flow-direction arrays it is used on.
# Restore it before importing pysheds anywhere below.
if not hasattr(np, "in1d"):
    np.in1d = np.isin  # type: ignore[attr-defined]

# --- constants ---------------------------------------------------------------

# Lutour outlet, from BD TOPO troncon geometry (downstream end of the last
# segment of cours_d_eau COURDEAU0000002000907698, "sens direct").
OUTLET_LON, OUTLET_LAT = -0.10879174, 42.87283362

# The reference sub-basin.
REF_RIVER_ID = "COURDEAU0000002000907698"
REF_AREA_KM2 = 39.34

# Padded tile around the course (course bounds -0.109,42.779 -> -0.088,42.873).
# South/west padding is generous: the outlet is at the *north* edge of the
# course and the catchment fans out south and west toward the cirque.
TILE_BBOX = (-0.16, 42.74, -0.04, 42.90)  # west, south, east, north (WGS84)

RAW_DIR = Path("data/raw")
DEM_TIF = RAW_DIR / "cop30_lutour.tif"
REF_PARQUET = RAW_DIR / "bassin_versant_topographique_pyrenees.parquet"
OUT_DIR = Path("scratchpad")

# Accumulation threshold (in cells) above which a cell counts as "channel".
# COP30 is ~30 m; ~1000 cells ~= 0.9 km2 of contributing area, a sane channel
# head for a Pyrenean torrent. The pour-point snap is the sensitive part, not
# this number - it just has to put *some* channel near the outlet.
ACC_CHANNEL_CELLS = 1000

# How far (in DEM cells) we allow the outlet to be snapped onto the channel.
SNAP_RADIUS_CELLS = 8


# --- 1. fetch --------------------------------------------------------------


def fetch_dem(bbox: tuple[float, float, float, float], dest: Path) -> Path:
    """Download a Copernicus GLO-30 GeoTIFF for ``bbox`` from OpenTopography.

    Cached: if ``dest`` exists we keep it. Needs ``OPENTOPOGRAPHY_API_KEY``.
    """
    if dest.exists() and dest.stat().st_size > 0:
        print(f"[fetch] cached {dest} ({dest.stat().st_size / 1e6:.1f} MB)")
        return dest

    api_key = os.environ.get("OPENTOPOGRAPHY_API_KEY")
    if not api_key:
        sys.exit(
            "OPENTOPOGRAPHY_API_KEY is not set. Get a free key at "
            "https://portal.opentopography.org/ and re-run:\n"
            "  OPENTOPOGRAPHY_API_KEY=xxxx uv run python scratchpad/dem_lutour.py"
        )

    west, south, east, north = bbox
    url = "https://portal.opentopography.org/API/globaldem"
    params = {
        "demtype": "COP30",
        "west": west,
        "south": south,
        "east": east,
        "north": north,
        "outputFormat": "GTiff",
        "API_Key": api_key,
    }
    print(f"[fetch] COP30 {bbox} -> {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, params=params, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "")
        if "tiff" not in ctype and "octet-stream" not in ctype:
            sys.exit(f"[fetch] unexpected response ({ctype}): {resp.text[:500]}")
        tmp = dest.with_suffix(".tmp")
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                fh.write(chunk)
        tmp.replace(dest)
    print(f"[fetch] wrote {dest} ({dest.stat().st_size / 1e6:.1f} MB)")
    return dest


# --- 2-5. delineate --------------------------------------------------------


def delineate(tif: Path, outlet_lon: float, outlet_lat: float):
    """Return (catchment_polygon_wgs84, diagnostics dict).

    Polygon is a shapely geometry in EPSG:4326. Diagnostics carry the numbers
    the validation table wants.
    """
    from pysheds.grid import Grid

    grid = Grid.from_raster(str(tif))
    dem = grid.read_raster(str(tif))
    print(f"[dem] shape {dem.shape}, cell ~{grid.affine.a * 111320:.0f} m, "
          f"z {float(np.nanmin(dem)):.0f}-{float(np.nanmax(dem)):.0f} m")

    # 2. condition --------------------------------------------------------
    pit_filled = grid.fill_pits(dem)
    flooded = grid.fill_depressions(pit_filled)
    inflated = grid.resolve_flats(flooded)

    altered = int(np.sum(np.asarray(flooded) - np.asarray(dem) > 0.5))
    frac = altered / dem.size
    print(f"[condition] {altered} cells raised >0.5 m by depression fill "
          f"({frac:.2%} of tile)")
    if frac > 0.05:
        print("[condition] WARNING: >5% of tile altered - check glacial cirques, "
              "the fill may be swallowing real tarns")

    # 3. flow direction + accumulation ---------------------------------
    fdir = grid.flowdir(inflated)
    acc = grid.accumulation(fdir)
    print(f"[flow] D8; max accumulation {int(np.asarray(acc).max())} cells")

    # 4. snap the pour point -------------------------------------------
    channel = acc > ACC_CHANNEL_CELLS
    x_snap, y_snap = grid.snap_to_mask(channel, (outlet_lon, outlet_lat))
    # distance moved, in cells, as a sanity gate
    dcol = abs(x_snap - outlet_lon) / grid.affine.a
    drow = abs(y_snap - outlet_lat) / grid.affine.e
    moved_cells = float(np.hypot(dcol, drow))
    print(f"[snap] outlet ({outlet_lon:.5f}, {outlet_lat:.5f}) -> "
          f"({x_snap:.5f}, {y_snap:.5f}), moved {moved_cells:.1f} cells")
    if moved_cells > SNAP_RADIUS_CELLS:
        print(f"[snap] WARNING: snapped >{SNAP_RADIUS_CELLS} cells - the DEM "
              "channel and the BD TOPO outlet disagree badly; lower "
              "ACC_CHANNEL_CELLS or check the outlet coord")

    # 5. delineate + vectorise ---------------------------------------
    catch = grid.catchment(x=x_snap, y=y_snap, fdir=fdir, xytype="coordinate")
    grid.clip_to(catch)
    mask = grid.view(catch, dtype=np.uint8)
    shapes = features.shapes(mask, mask=mask.astype(bool), transform=grid.affine)
    polys = [shape(geom) for geom, val in shapes if val == 1]
    if not polys:
        sys.exit("[delineate] empty catchment - the snap landed off the network")
    poly = max(polys, key=lambda p: p.area)  # largest connected component
    poly = poly.simplify(grid.affine.a / 2)  # ~half a cell, cosmetic only

    # area via an equal-area reprojection of the mask, not degrees^2
    with rasterio.open(tif) as src:
        crs = src.crs
    gs = gpd.GeoSeries([poly], crs=crs).to_crs(2154)  # Lambert-93, metres
    area_km2 = float(gs.area.iloc[0] / 1e6)

    diag = {
        "area_km2": area_km2,
        "snap_moved_cells": moved_cells,
        "snap_xy": (x_snap, y_snap),
        "depression_fill_frac": frac,
        "n_cells": int(mask.sum()),
    }
    return gs.to_crs(4326).iloc[0], diag


# --- validation ----------------------------------------------------------


def load_reference() -> gpd.GeoSeries:
    bv = gpd.read_parquet(REF_PARQUET)
    m = bv["liens_vers_cours_d_eau_principal"].astype(str).str.contains(REF_RIVER_ID)
    ref = bv[m]
    if ref.empty:
        sys.exit(f"[ref] no bassin_versant polygon for {REF_RIVER_ID}")
    return gpd.GeoSeries([ref.geometry.union_all()], crs=bv.crs)


def load_lutour_streams() -> gpd.GeoDataFrame:
    tr = gpd.read_parquet(RAW_DIR / "troncon_hydrographique_pyrenees.parquet")
    m = tr["liens_vers_cours_d_eau"].astype(str).str.contains(REF_RIVER_ID)
    return tr[m]


def validate(poly_wgs84, diag: dict) -> None:
    ref = load_reference()
    ref_m = ref.to_crs(2154)
    got_m = gpd.GeoSeries([poly_wgs84], crs=4326).to_crs(2154)

    ref_area = float(ref_m.area.iloc[0] / 1e6)
    got_area = diag["area_km2"]
    inter = got_m.iloc[0].intersection(ref_m.iloc[0]).area
    union = got_m.iloc[0].union(ref_m.iloc[0]).area
    iou = inter / union if union else 0.0

    streams = load_lutour_streams().to_crs(4326)
    # The last segment is a `fictif` connector drawn *into* the Gave de Pau
    # confluence; it necessarily crosses the divide because the outlet is on the
    # divide. Judge it by "mostly inside" (>=90%), the real reaches by >98%.
    outlet_pt = shape({"type": "Point", "coordinates": [OUTLET_LON, OUTLET_LAT]})

    def _covered(row) -> bool:
        g = row.geometry
        if g.within(poly_wgs84):
            return True
        frac = g.intersection(poly_wgs84).length / g.length
        touches_outlet = g.distance(outlet_pt) < 1e-6
        return frac > (0.90 if (row.get("fictif") and touches_outlet) else 0.98)

    inside = streams.apply(_covered, axis=1)
    n_in, n_tot = int(inside.sum()), len(streams)

    print("\n=== validation ===")
    print(f"{'area (DEM)':<28} {got_area:6.2f} km2")
    print(f"{'area (BD TOPO reference)':<28} {ref_area:6.2f} km2  "
          f"(target ~{REF_AREA_KM2})")
    dev = (got_area - ref_area) / ref_area
    print(f"{'deviation':<28} {dev:+6.1%}   "
          f"{'PASS' if abs(dev) <= 0.15 else 'FAIL'} (+-15% at 30 m)")
    print(f"{'IoU vs reference':<28} {iou:6.2f}   "
          f"{'PASS' if iou > 0.8 else 'FAIL'} (>0.8)")
    print(f"{'Lutour troncons inside':<28} {n_in}/{n_tot}   "
          f"{'PASS' if n_in == n_tot else 'FAIL'}")
    print(f"{'pour point moved':<28} {diag['snap_moved_cells']:6.1f} cells")
    print(f"{'depression fill':<28} {diag['depression_fill_frac']:6.2%} of tile")


def save_outputs(poly_wgs84, diag: dict) -> None:
    OUT_DIR.mkdir(exist_ok=True)
    gdf = gpd.GeoDataFrame(
        {"source": ["dem_cop30"], "area_km2": [diag["area_km2"]]},
        geometry=[poly_wgs84],
        crs=4326,
    )
    gj = OUT_DIR / "lutour_catchment_dem.geojson"
    gdf.to_file(gj, driver="GeoJSON")
    print(f"\n[out] {gj}")

    try:
        _plot(poly_wgs84)
    except Exception as exc:  # pragma: no cover - plotting is optional
        print(f"[out] plot skipped: {exc}")


def _plot(poly_wgs84) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ref = load_reference().to_crs(4326)
    streams = load_lutour_streams().to_crs(4326)

    fig, ax = plt.subplots(figsize=(7, 8))
    with rasterio.open(DEM_TIF) as src:
        band = src.read(1).astype(float)
        band[band == src.nodata] = np.nan
        extent = (src.bounds.left, src.bounds.right, src.bounds.bottom, src.bounds.top)
    ax.imshow(band, extent=extent, cmap="terrain", origin="upper", alpha=0.8)
    gpd.GeoSeries([poly_wgs84], crs=4326).boundary.plot(
        ax=ax, color="red", linewidth=2, label="DEM catchment"
    )
    ref.boundary.plot(ax=ax, color="black", linewidth=1.5, linestyle="--",
                      label="BD TOPO reference")
    streams.plot(ax=ax, color="blue", linewidth=0.8)
    ax.plot(OUTLET_LON, OUTLET_LAT, "k*", markersize=14)
    ax.set_title("Gave de Lutour catchment — COP30 vs BD TOPO")
    ax.legend(loc="lower right")
    ax.set_xlim(TILE_BBOX[0], TILE_BBOX[2])
    ax.set_ylim(TILE_BBOX[1], TILE_BBOX[3])
    out = OUT_DIR / "lutour_catchment_dem.png"
    fig.savefig(out, dpi=130, bbox_inches="tight")
    print(f"[out] {out}")


def main() -> None:
    tif = fetch_dem(TILE_BBOX, DEM_TIF)
    poly, diag = delineate(tif, OUTLET_LON, OUTLET_LAT)
    validate(poly, diag)
    save_outputs(poly, diag)


if __name__ == "__main__":
    main()
