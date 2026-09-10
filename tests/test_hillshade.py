"""Analytic checks for the four separable relief fields."""

from __future__ import annotations

import numpy as np

from valleespyr.render.hillshade import (
    cast_shadows,
    compose_relief,
    core_shadow,
    hillshade,
    shaded_relief,
    slope,
    sun_vector,
)


def test_sun_vector_bearings():
    # from due north, 0 deg altitude -> +y, on the horizon
    v = sun_vector(0.0, 0.0)
    assert np.allclose(v, [0.0, 1.0, 0.0], atol=1e-9)
    # from due east -> +x
    v = sun_vector(90.0, 0.0)
    assert np.allclose(v, [1.0, 0.0, 0.0], atol=1e-9)
    # straight overhead -> +z
    v = sun_vector(123.0, 90.0)
    assert np.allclose(v, [0.0, 0.0, 1.0], atol=1e-9)


def test_flat_dem_no_shadows_uniform_hillshade():
    dem = np.zeros((40, 50))
    sun = sun_vector(135.0, 30.0)
    hs = hillshade(dem, dx=30.0, dy=30.0, sun=sun)
    # flat ground: normal is +z, so shade == sin(altitude)
    assert np.allclose(hs, np.sin(np.radians(30.0)), atol=1e-9)
    assert np.allclose(slope(dem, 30.0, 30.0), 0.0, atol=1e-12)  # flat -> slope 0
    assert not cast_shadows(dem, 30.0, 30.0, sun).any()
    assert not core_shadow(dem, 30.0, 30.0, sun).any()


def test_slope_is_directionless_and_zero_to_one():
    # a slope tilted the same amount up the east and up the north: slope must be
    # identical for both, since it ignores aspect.
    n = 30
    east = np.tile(np.arange(n) * 10.0, (n, 1))
    north = east.T
    se = slope(east, 20.0, 20.0)
    sn = slope(north, 20.0, 20.0)
    assert np.allclose(se[2:-2, 2:-2].mean(), sn[2:-2, 2:-2].mean(), rtol=1e-6)
    assert 0.0 <= np.nanmin(se) and np.nanmax(se) <= 1.0
    # a 10 m rise per 20 m cell -> arctan(0.5) / (pi/2)
    expect = np.arctan(0.5) / (np.pi / 2)
    assert np.isclose(se[n // 2, n // 2], expect, rtol=1e-6)


def test_vertical_step_shadow_length_matches_geometry():
    # a step running N-S: west half low, east half a plateau of height h.
    # sun from the east (azimuth 90) at altitude alpha throws a shadow of
    # length h / tan(alpha) westward onto the low ground.
    rows, cols = 20, 120
    cell = 10.0  # metres
    h = 200.0
    alpha = 25.0
    dem = np.zeros((rows, cols))
    edge = 60
    dem[:, edge:] = h

    sun = sun_vector(90.0, alpha)
    shadow = cast_shadows(dem, dx=cell, dy=cell, sun=sun)

    reach_cells = (h / np.tan(np.radians(alpha))) / cell
    lo = edge - int(reach_cells) + 1
    hi = edge - int(reach_cells) - 2

    row = rows // 2
    assert shadow[row, edge - 1]
    assert shadow[row, lo]
    if hi >= 0:
        assert not shadow[row, hi]
    assert not shadow[:, edge:].any()


def test_no_exaggeration_knob_on_the_shadow_march():
    import inspect

    sig = inspect.signature(cast_shadows)
    assert "z_exag" not in sig.parameters
    assert "shade_gain" not in sig.parameters
    # shaded_relief no longer composites — no zenith_weight there either
    assert "zenith_weight" not in inspect.signature(shaded_relief).parameters
    assert "zenith_weight" not in inspect.signature(hillshade).parameters

    rows, cols = 12, 260
    cell = 5.0
    h = 300.0
    alpha = 40.0
    edge = 200
    dem = np.zeros((rows, cols))
    dem[:, edge:] = h
    shadow = cast_shadows(dem, cell, cell, sun_vector(90.0, alpha))
    reach = int(round((h / np.tan(np.radians(alpha))) / cell))
    assert 0 < edge - reach - 5
    row = rows // 2
    assert shadow[row, edge - reach + 1]
    assert shadow[row, edge - 1]
    assert not shadow[row, edge - reach - 3]


def test_core_vs_cast_are_disjoint_and_named_right():
    rows, cols = 30, 121
    crest = 60
    x = np.abs(np.arange(cols) - crest)
    dem = np.tile((crest - x) * 6.0, (rows, 1))

    sun = sun_vector(90.0, 22.0)  # low, from the east
    core = core_shadow(dem, 12.0, 12.0, sun)
    _hs, _sl, core_f, cast_f = shaded_relief(
        dem, dx=12.0, dy=12.0, sun_azimuth=90.0, sun_altitude=22.0
    )
    core_split = core_f > 0.5
    cast_split = cast_f > 0.5

    row = rows // 2
    assert core[row, crest - 20]
    assert core_split[row, crest - 20]
    assert not core[row, crest + 20]
    assert not (core_split & cast_split).any()


def test_cast_shadow_is_beyond_the_terminator():
    rows, cols = 20, 160
    dem = np.zeros((rows, cols))
    dem[:, 90:96] = 250.0

    _hs, _sl, core_f, cast_f = shaded_relief(
        dem, dx=10.0, dy=10.0, sun_azimuth=90.0, sun_altitude=20.0
    )
    row = rows // 2
    assert cast_f[row, 88] > 0.5
    assert core_f[row, 88] < 0.5
    reach = int(round((250.0 / np.tan(np.radians(20.0))) / 10.0))
    assert cast_f[row, 90 - reach - 3] < 0.5


def test_shade_gain_steepens_shading_only():
    rows, cols = 20, 20
    dem = np.tile(np.arange(cols) * 4.0, (rows, 1))
    sun = sun_vector(90.0, 40.0)  # from the east -> east-up slope faces away

    faithful = hillshade(dem, 15.0, 15.0, sun, shade_gain=1.0)
    steep = hillshade(dem, 15.0, 15.0, sun, shade_gain=2.0)
    assert steep.mean() < faithful.mean()
    # slope field also steepens
    assert slope(dem, 15.0, 15.0, shade_gain=2.0).mean() > slope(
        dem, 15.0, 15.0, shade_gain=1.0
    ).mean()
    # geometry unchanged
    assert not cast_shadows(dem, 15.0, 15.0, sun).any()


def test_compose_relief_two_lights():
    # a steep uniform slope dropping to the east; sun raking from the west so
    # the east-facing slope is the anti-sun side and the hillshade there ~0.
    rows, cols = 24, 24
    dem = np.tile((cols - np.arange(cols)) * 30.0, (rows, 1))
    hs, sl, _core, _cast = shaded_relief(
        dem, dx=20.0, dy=20.0, sun_azimuth=270.0, sun_altitude=22.0
    )
    interior = (slice(2, -2), slice(2, -2))

    single = compose_relief(hs, sl, zenith_weight=0.0)
    two = compose_relief(hs, sl, zenith_weight=0.5)

    # w=0 is the hillshade untouched
    assert np.array_equal(single, hs)
    # the raking-only shade is near black here; the zenithal blend lifts it well
    # off the floor (steep, so 1-slope is modest) without whiting it out
    assert np.nanmean(single[interior]) < 0.15
    assert np.nanmean(two[interior]) > np.nanmean(single[interior]) + 0.1
    assert np.nanmean(two[interior]) < 0.9

    # flat ground: slope 0, hillshade = sin(alt); blend is the plain average
    flat = np.zeros((10, 10))
    hf, slf, _c, _k = shaded_relief(
        flat, dx=20.0, dy=20.0, sun_azimuth=270.0, sun_altitude=22.0
    )
    b = compose_relief(hf, slf, zenith_weight=0.5)
    assert np.allclose(b, 0.5 * np.sin(np.radians(22.0)) + 0.5 * 1.0, atol=1e-9)


def test_compose_relief_does_not_touch_the_shadow_masks():
    rows, cols = 20, 40
    dem = np.tile((cols - np.arange(cols)) * 20.0, (rows, 1))
    hs, sl, core_f, cast_f = shaded_relief(
        dem, dx=15.0, dy=15.0, sun_azimuth=270.0, sun_altitude=20.0
    )
    a = compose_relief(hs, sl, zenith_weight=0.0)
    b = compose_relief(hs, sl, zenith_weight=0.6)
    assert not np.allclose(a, b)          # the wash changed
    # core/cast are inputs to compose, untouched by it
    hs2, sl2, core2, cast2 = shaded_relief(
        dem, dx=15.0, dy=15.0, sun_azimuth=270.0, sun_altitude=20.0
    )
    assert np.array_equal(np.isfinite(core_f) & (core_f > 0.5),
                          np.isfinite(core2) & (core2 > 0.5))
    assert np.array_equal(np.isfinite(cast_f) & (cast_f > 0.5),
                          np.isfinite(cast2) & (cast2 > 0.5))


def test_shaded_relief_returns_four_fields_nan_outside():
    dem = np.full((20, 20), 100.0)
    dem[:5, :] = np.nan
    dem[10:, 10:] = 400.0
    out = shaded_relief(dem, dx=30.0, dy=30.0, sun_azimuth=110.0, sun_altitude=25.0)
    assert len(out) == 4
    hs, sl, core_f, cast_f = out
    for layer in out:
        assert layer.shape == dem.shape
        assert np.isnan(layer[:5, :]).all()
    assert np.isfinite(hs[5:, :]).all()
    assert np.isfinite(sl[5:, :]).all()
    for layer in out:
        assert np.nanmin(layer) >= 0.0 and np.nanmax(layer) <= 1.0


def test_nan_outside_is_preserved():
    dem = np.full((20, 20), 100.0)
    dem[:5, :] = np.nan
    sun = sun_vector(135.0, 40.0)
    hs = hillshade(dem, 30.0, 30.0, sun)
    assert np.isnan(hs[:5, :]).all()
    assert np.isfinite(hs[5:, :]).all()
    assert np.isnan(slope(dem, 30.0, 30.0)[:5, :]).all()
    assert not cast_shadows(dem, 30.0, 30.0, sun)[:5, :].any()
    assert not core_shadow(dem, 30.0, 30.0, sun)[:5, :].any()
