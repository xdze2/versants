"""Analytic checks for the numpy hillshade + core/cast-shadow split."""

from __future__ import annotations

import numpy as np

from valleespyr.render.hillshade import (
    cast_shadows,
    core_shadow,
    hillshade,
    shaded_relief,
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
    assert not cast_shadows(dem, 30.0, 30.0, sun).any()
    assert not core_shadow(dem, 30.0, 30.0, sun).any()


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

    # expected westward reach in cells, from the foot of the wall
    reach_cells = (h / np.tan(np.radians(alpha))) / cell
    lo = edge - int(reach_cells) + 1   # firmly inside the shadow
    hi = edge - int(reach_cells) - 2   # firmly in sunlight (may be < 0)

    row = rows // 2
    assert shadow[row, edge - 1]           # right at the wall foot: shadowed
    assert shadow[row, lo]                 # within the predicted reach
    if hi >= 0:
        assert not shadow[row, hi]         # beyond it: lit
    # the plateau top faces the sun and is never self-shadowed here
    assert not shadow[:, edge:].any()


def test_no_vertical_exaggeration_knob_on_the_shadow_march():
    # cast_shadows takes only geometry + sun; there is no z_exag / gain to slip
    # a fictional sun angle in through. The shadow of a wall must be exactly
    # h / tan(alpha) long, full stop.
    import inspect

    sig = inspect.signature(cast_shadows)
    assert "z_exag" not in sig.parameters
    assert "shade_gain" not in sig.parameters

    rows, cols = 12, 260
    cell = 5.0
    h = 300.0
    alpha = 40.0
    edge = 200
    dem = np.zeros((rows, cols))
    dem[:, edge:] = h  # high ground on the east; eastern sun -> shadow goes west
    shadow = cast_shadows(dem, cell, cell, sun_vector(90.0, alpha))
    reach = int(round((h / np.tan(np.radians(alpha))) / cell))
    assert 0 < edge - reach - 5  # shadow tip fits on the low ground
    row = rows // 2
    assert shadow[row, edge - reach + 1]   # inside the shadow
    assert shadow[row, edge - 1]           # at the wall foot
    assert not shadow[row, edge - reach - 3]  # past the tip: lit


def test_core_vs_cast_are_disjoint_and_named_right():
    # a single ridge: two facing ramps meeting at a crest running N-S.
    rows, cols = 30, 121
    crest = 60
    x = np.abs(np.arange(cols) - crest)
    dem = np.tile((crest - x) * 6.0, (rows, 1))  # apex ~360 m at the crest

    sun = sun_vector(90.0, 22.0)  # low, from the east
    core = core_shadow(dem, 12.0, 12.0, sun)
    _hs, core_f, cast_f = shaded_relief(
        dem, dx=12.0, dy=12.0, sun_azimuth=90.0, sun_altitude=22.0
    )
    core_split = core_f > 0.5
    cast_split = cast_f > 0.5

    # the west ramp faces away from an eastern sun -> core shadow there
    row = rows // 2
    assert core[row, crest - 20]
    assert core_split[row, crest - 20]
    # the east ramp faces the sun -> not core-shadowed
    assert not core[row, crest + 20]
    # shaded_relief's cast layer never overlaps its core layer
    assert not (core_split & cast_split).any()


def test_cast_shadow_is_beyond_the_terminator():
    # a tall thin tower on flat ground: it self-shadows its own west face (core)
    # AND throws a cast shadow onto the flat ground to its west (sun from east).
    rows, cols = 20, 160
    dem = np.zeros((rows, cols))
    dem[:, 90:96] = 250.0  # a 6-cell-wide block

    _hs, core_f, cast_f = shaded_relief(
        dem, dx=10.0, dy=10.0, sun_azimuth=90.0, sun_altitude=20.0
    )
    row = rows // 2
    # flat ground just west of the block: sun-facing (flat), but occluded ->
    # this is a *cast* shadow, not core
    assert cast_f[row, 88] > 0.5
    assert core_f[row, 88] < 0.5
    # far west, past the shadow tip: lit
    reach = int(round((250.0 / np.tan(np.radians(20.0))) / 10.0))
    assert cast_f[row, 90 - reach - 3] < 0.5


def test_shade_gain_steepens_shading_only():
    # a gentle uniform slope. Raising shade_gain darkens the anti-sun face more,
    # but must not change the (here empty) cast-shadow mask.
    rows, cols = 20, 20
    dem = np.tile(np.arange(cols) * 4.0, (rows, 1))  # slopes up to the east
    sun = sun_vector(90.0, 40.0)  # from the east -> east-up slope faces away

    faithful = hillshade(dem, 15.0, 15.0, sun, shade_gain=1.0)
    steep = hillshade(dem, 15.0, 15.0, sun, shade_gain=2.0)
    assert steep.mean() < faithful.mean()          # darker overall
    # geometry unchanged: no cast shadow on a single planar slope either way
    assert not cast_shadows(dem, 15.0, 15.0, sun).any()


def test_shaded_relief_returns_three_layers_nan_outside():
    dem = np.full((20, 20), 100.0)
    dem[:5, :] = np.nan
    dem[10:, 10:] = 400.0
    out = shaded_relief(dem, dx=30.0, dy=30.0, sun_azimuth=110.0, sun_altitude=25.0)
    assert len(out) == 3
    hs, core_f, cast_f = out
    for layer in out:
        assert layer.shape == dem.shape
        assert np.isnan(layer[:5, :]).all()        # NaN preserved outside the DEM
    assert np.isfinite(hs[5:, :]).all()
    # masks are 0..1 where finite
    assert np.nanmin(core_f) >= 0.0 and np.nanmax(core_f) <= 1.0
    assert np.nanmin(cast_f) >= 0.0 and np.nanmax(cast_f) <= 1.0


def test_nan_outside_is_preserved():
    dem = np.full((20, 20), 100.0)
    dem[:5, :] = np.nan
    sun = sun_vector(135.0, 40.0)
    hs = hillshade(dem, 30.0, 30.0, sun)
    assert np.isnan(hs[:5, :]).all()
    assert np.isfinite(hs[5:, :]).all()
    assert not cast_shadows(dem, 30.0, 30.0, sun)[:5, :].any()
    assert not core_shadow(dem, 30.0, 30.0, sun)[:5, :].any()
