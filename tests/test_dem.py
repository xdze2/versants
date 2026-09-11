"""Tests for :mod:`valleespyr.hydro.dem`: the condition/trace split and the
OpenTopography API key lookup.

``condition()`` (pit-fill/depression-fill/resolve-flats/flowdir/accumulation)
and ``trace()`` (snap/catchment/vectorise) used to be fused into one
``delineate()``. This module checks that the split still produces the same
result shape, and that ``condition()`` can be reused across two ``trace()``
calls against the same tile.

The DEM-computation tests need the ``dem`` extra (pysheds, rasterio); skipped
otherwise. The heavier checks additionally need the small cached Lutour DEM
tile saved at ``data/raw/cop30_lutour.tif`` (no network access — this is a
local fixture, not a live OpenTopography fetch) and are skipped when it is
absent. The API key lookup tests need neither.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from valleespyr.hydro import dem as dem_key_module  # noqa: E402 - key lookup only

pytest.importorskip("pysheds")
pytest.importorskip("rasterio")

import rasterio  # noqa: E402

from valleespyr.hydro import dem  # noqa: E402

LUTOUR_TIF = Path("data/raw/cop30_lutour.tif")
# Gave de Lutour outlet (see scratchpad/dem_lutour.py); BD TOPO reference
# sub-basin area is 39.34 km2.
OUTLET_LON, OUTLET_LAT = -0.10879174, 42.87283362


# --------------------------------------------------- sparse-channel tile guard


def test_trace_refuses_a_tile_with_too_few_channel_cells():
    """A tile whose padded bbox is so small that almost no cell accumulates
    enough flow to count as a channel (0-1 cells observed in practice, for a
    handful of tiny Pyrenean rivers) must be refused *before*
    grid.catchment() is called — that call has been observed to corrupt
    memory ("double free or corruption", not a catchable Python exception)
    rather than raise cleanly on inputs this degenerate. A real repro needs
    the exact tile/outlet that triggers pysheds' corruption (not reproduced
    here — a segfault can't be asserted on safely in a test process anyway);
    this test instead locks in the guard that prevents ever reaching that call."""
    import numpy as np

    from valleespyr.hydro.dem import MIN_CHANNEL_CELLS, ConditionedGrid, trace

    acc = np.zeros((10, 10))
    acc[5, 5] = 1  # exactly one cell above threshold - below MIN_CHANNEL_CELLS
    assert 1 < MIN_CHANNEL_CELLS, "test assumes the default guard exceeds 1"

    conditioned = ConditionedGrid(
        grid=object(),  # never touched: the guard raises before grid.catchment()
        fdir=object(),
        acc=acc,
        crs="EPSG:4326",
        depression_fill_frac=0.0,
    )

    with pytest.raises(RuntimeError, match="too sparse"):
        trace(conditioned, 0.0, 0.0, acc_channel_cells=0)


def test_trace_proceeds_past_the_guard_with_enough_channel_cells(monkeypatch):
    """Sanity check the guard's threshold direction: enough channel cells
    must NOT raise the sparse-tile RuntimeError (whatever happens next, e.g.
    a real snap_to_mask call on this fake grid, is irrelevant here)."""
    import numpy as np

    from valleespyr.hydro.dem import MIN_CHANNEL_CELLS, ConditionedGrid, trace

    acc = np.zeros((10, 10))
    acc[:, :] = MIN_CHANNEL_CELLS + 5  # comfortably every cell is "channel"

    class _StubGrid:
        def snap_to_mask(self, *a, **kw):
            raise LookupError("reached past the sparse-tile guard, as expected")

    conditioned = ConditionedGrid(
        grid=_StubGrid(), fdir=object(), acc=acc, crs="EPSG:4326", depression_fill_frac=0.0
    )

    with pytest.raises(LookupError, match="reached past the sparse-tile guard"):
        trace(conditioned, 0.0, 0.0, acc_channel_cells=0)


# ------------------------------------------------------------ API key lookup


def test_api_key_prefers_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENTOPOGRAPHY_API_KEY", "from-env")
    monkeypatch.setattr(dem_key_module, "_KEY_FILE", tmp_path / "key.secret")
    (tmp_path / "key.secret").write_text("from-file", encoding="utf-8")
    assert dem_key_module._opentopography_api_key() == "from-env"


def test_api_key_falls_back_to_key_secret_file(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENTOPOGRAPHY_API_KEY", raising=False)
    key_file = tmp_path / "key.secret"
    key_file.write_text("  from-file-with-whitespace  \n", encoding="utf-8")
    monkeypatch.setattr(dem_key_module, "_KEY_FILE", key_file)
    assert dem_key_module._opentopography_api_key() == "from-file-with-whitespace"


def test_api_key_none_when_neither_is_set(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENTOPOGRAPHY_API_KEY", raising=False)
    monkeypatch.setattr(dem_key_module, "_KEY_FILE", tmp_path / "missing.secret")
    assert dem_key_module._opentopography_api_key() is None


# ---------------------------------------------------- fetch_dem error handling


class _FakeStreamedErrorResponse:
    """A minimal stand-in for a ``requests.Response`` from a ``stream=True``
    GET that comes back with an error status: mirrors the real behaviour where
    the body is only readable while the connection (here: this fake) is open,
    which is exactly why ``fetch_dem`` must read it before letting the
    ``with`` block's ``__exit__`` close things out from under a caller."""

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self._body = body
        self._closed = False

    @property
    def text(self) -> str:
        if self._closed:
            return ""  # the real bug this class exists to reproduce
        return self._body

    def raise_for_status(self):
        if 400 <= self.status_code:
            import requests

            raise requests.exceptions.HTTPError(
                f"{self.status_code} Client Error", response=self
            )

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._closed = True


def test_s3_tile_grid_single_cell():
    from valleespyr.hydro.dem import _s3_tile_grid

    assert _s3_tile_grid((0.30, 42.75, 0.35, 42.78)) == [(42, 0)]


def test_s3_tile_grid_spans_a_degree_boundary():
    from valleespyr.hydro.dem import _s3_tile_grid

    cells = _s3_tile_grid((0.30, 42.95, 0.35, 43.05))
    assert set(cells) == {(42, 0), (43, 0)}


def test_s3_tile_url_handles_negative_lat_lon():
    from valleespyr.hydro.dem import _s3_tile_url

    assert _s3_tile_url(42, 0) == (
        "https://copernicus-dem-30m.s3.amazonaws.com/"
        "Copernicus_DSM_COG_10_N42_00_E000_00_DEM/"
        "Copernicus_DSM_COG_10_N42_00_E000_00_DEM.tif"
    )
    assert _s3_tile_url(-1, -2) == (
        "https://copernicus-dem-30m.s3.amazonaws.com/"
        "Copernicus_DSM_COG_10_S01_00_W002_00_DEM/"
        "Copernicus_DSM_COG_10_S01_00_W002_00_DEM.tif"
    )


def _write_fake_tile(path: Path, *, west: float, south: float, size: int = 40) -> None:
    """A tiny 1x1 degree fake DEM tile — plausible elevations, real georeferencing."""
    import numpy as np
    from rasterio.transform import from_origin

    data = (np.random.default_rng(0).random((size, size)) * 1000 + 500).astype("float32")
    transform = from_origin(west, south + 1, 1.0 / size, 1.0 / size)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=size,
        width=size,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
    ) as dst:
        dst.write(data, 1)


def test_fetch_dem_s3_caches_and_mosaics_across_a_tile_boundary(monkeypatch, tmp_path):
    """A bbox spanning two S3 grid cells must mosaic both into one cropped
    GeoTIFF, and a repeat call for the same bbox must be served from cache
    without any further download."""
    from valleespyr.hydro import dem as dem_mod

    fetch_calls: list[tuple[int, int]] = []

    def fake_fetch_s3_tile(lat, lon, dem_dir):
        fetch_calls.append((lat, lon))
        path = dem_dir / f"COP30_S3_N{lat:02d}_E{lon:03d}.tif"
        if not path.exists():
            _write_fake_tile(path, west=float(lon), south=float(lat))
        return path

    monkeypatch.setattr(dem_mod, "_fetch_s3_tile", fake_fetch_s3_tile)

    bbox = (0.3, 42.9, 0.4, 43.1)  # spans lat cells 42 and 43
    tif = dem_mod.fetch_dem_s3(bbox, dem_dir=tmp_path)

    assert tif.exists()
    assert sorted(fetch_calls) == [(42, 0), (43, 0)]
    with rasterio.open(tif) as src:
        assert src.crs.to_string() == "EPSG:4326"
        b = src.bounds
        assert b.left == pytest.approx(bbox[0], abs=0.02)
        assert b.right == pytest.approx(bbox[2], abs=0.02)

    # second call: same dest already cached -> no new tile fetches
    fetch_calls.clear()
    tif2 = dem_mod.fetch_dem_s3(bbox, dem_dir=tmp_path)
    assert tif2 == tif
    assert fetch_calls == []


def test_fetch_dem_folds_error_body_into_the_exception_message(monkeypatch, tmp_path):
    """OpenTopography's daily-quota rejection is a 401 whose real reason lives
    only in the (streamed) body — once fetch_dem's own ``with`` block has
    exited, a caller's ``exc.response.text`` reads back empty, so the body
    must survive some other way: folded into the exception's message."""
    import requests

    from valleespyr.hydro import dem as dem_mod

    fake_resp = _FakeStreamedErrorResponse(
        401, "<error>Error: API maximum rate limit reached. (50 API calls/24hrs)</error>"
    )
    monkeypatch.setattr(dem_mod, "_opentopography_api_key", lambda: "fake-key")
    monkeypatch.setattr(requests, "get", lambda *a, **kw: fake_resp)

    with pytest.raises(requests.exceptions.HTTPError) as excinfo:
        dem_mod.fetch_dem((0.0, 42.0, 0.1, 42.1), tmp_path / "never_written.tif")

    assert "rate limit" in str(excinfo.value).lower()
    # and confirm the bug this guards against: the closed fake's .text is
    # indeed empty by now, so the message is the only place the body survives
    assert fake_resp.text == ""


def test_delineate_matches_condition_then_trace():
    """delineate() must be exactly the condition()+trace() composition."""
    if not LUTOUR_TIF.exists():
        pytest.skip("data/raw/cop30_lutour.tif not present")

    poly_a, diag_a = dem.delineate(LUTOUR_TIF, OUTLET_LON, OUTLET_LAT)

    conditioned = dem.condition(LUTOUR_TIF)
    poly_b, diag_b = dem.trace(conditioned, OUTLET_LON, OUTLET_LAT)

    assert poly_a.equals(poly_b)
    assert diag_a == diag_b


def test_delineate_return_shape_and_plausible_area():
    if not LUTOUR_TIF.exists():
        pytest.skip("data/raw/cop30_lutour.tif not present")

    poly, diag = dem.delineate(LUTOUR_TIF, OUTLET_LON, OUTLET_LAT)

    assert poly.geom_type in ("Polygon", "MultiPolygon")
    for key in (
        "area_km2",
        "snap_moved_cells",
        "snap_xy",
        "depression_fill_frac",
        "n_cells",
    ):
        assert key in diag

    # within +-15% of the BD TOPO reference (39.34 km2), same tolerance the
    # scratchpad validation script uses at COP30's ~30 m resolution
    assert 33.0 < diag["area_km2"] < 46.0


def test_condition_result_is_reusable_across_two_traces():
    """The whole point of the split: condition() once, trace() more than once."""
    if not LUTOUR_TIF.exists():
        pytest.skip("data/raw/cop30_lutour.tif not present")

    conditioned = dem.condition(LUTOUR_TIF)
    assert isinstance(conditioned, dem.ConditionedGrid)
    assert 0.0 <= conditioned.depression_fill_frac <= 1.0

    poly_1, diag_1 = dem.trace(conditioned, OUTLET_LON, OUTLET_LAT)
    # tracing again from the very same outlet against the same conditioned
    # grid must be deterministic and not mutate shared state
    poly_2, diag_2 = dem.trace(conditioned, OUTLET_LON, OUTLET_LAT)

    assert poly_1.equals(poly_2)
    assert diag_1["area_km2"] == pytest.approx(diag_2["area_km2"])
    # depression_fill_frac travelled from condition() into both traces' diag
    assert diag_1["depression_fill_frac"] == conditioned.depression_fill_frac
    assert diag_2["depression_fill_frac"] == conditioned.depression_fill_frac
