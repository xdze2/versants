"""Sun-lit relief for a heightmap — four independent scalar fields. Pure numpy.

Blender's ray tracer is overkill for draping one directional light over a DEM:
there is no reflection, no refraction, no indirect bounce to solve. What a relief
map is really made of is a handful of *separable* quantities, each answering a
different question about a pixel:

* **hillshade** — ``max(0, n · L)``. Depends on the surface normal **and** the
  sun direction: how directly does this face catch the raking light? Carries
  aspect × sun altitude. This is the soft modelling.
* **slope** — steepness alone, ``arctan(‖∇z‖) / (π/2)`` in ``[0, 1]``. Depends
  on the normal only, **no direction**: 0 on the flats, 1 at the vertical. The
  ingredient for a zenithal "second sun" or a plain slope tint.
* **core shadow** (self-shadow / terminator) — ``n · L ≤ 0``. A face turned away
  from the sun. Local, aspect-only, no ray march. On a shaded map this is
  exactly where the hillshade has already gone to black.
* **cast shadow** (projected / drop shadow) — a face that *does* point toward the
  sun but is blocked by higher ground upwind. Non-local: a 1-D horizon march per
  pixel along the sun's bearing. The part that actually needs a raytrace, and
  the part that carries new information on a plate — "low sun, hidden behind that
  headwall", not just "this slope faces away".

:func:`shaded_relief` returns all four as separate arrays. Combining them is a
rendering choice, not a compute-time one: :func:`compose_relief` does the IGN
"deux soleils" blend (one raking light + one zenithal) from ``hillshade`` and
``slope``; a caller is free to weight them differently, tint by ``slope``, or ink
``core_shadow`` in its own colour.

Geometry note: the ray march uses the **true** DEM and the **true** sun altitude.
There is no vertical exaggeration here — a stretched Z against a fixed sun angle
is the shadow pattern of a sun that does not exist. Slope *shading* may be
punched up for legibility (``shade_gain``, a cartographic convention, off by
default), but that stylistic gain never touches the shadow passes.

Lighting note: on a north-up *plate* the light is placed at the top of the sheet
(NW ~315°), which is not where the sun is — but the eye assumes light from above
and inverts relief lit from below, so the map convention wins over realism. A
rotatable *3-D* view has no such constraint; there a physically-placed sun is
fine.

Everything is ``numpy`` only (no rasterio / bpy); callers pass the DEM as a plain
array plus its cell size in metres.

Conventions:

* the DEM is ``(rows, cols)``, **row 0 = north**, column 0 = west;
* sun azimuth is a compass bearing in degrees — 0 = from the north, 90 = from
  the east, measured clockwise seen from above;
* sun altitude is degrees above the horizon.
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "sun_vector",
    "hillshade",
    "slope",
    "core_shadow",
    "cast_shadows",
    "shaded_relief",
    "compose_relief",
]


def sun_vector(azimuth_deg: float, altitude_deg: float) -> np.ndarray:
    """Unit vector pointing *toward* the sun, in a Z-up world.

    ``x`` runs west→east (column direction), ``y`` runs south→north (opposite
    the DEM's row direction), ``z`` is up. Azimuth is a compass bearing.
    """
    az = np.radians(azimuth_deg)
    alt = np.radians(altitude_deg)
    ca = np.cos(alt)
    return np.array([np.sin(az) * ca, np.cos(az) * ca, np.sin(alt)], dtype=float)


def hillshade(
    dem: np.ndarray,
    dx: float,
    dy: float,
    sun: np.ndarray,
    *,
    shade_gain: float = 1.0,
) -> np.ndarray:
    """Directional Lambert shading of ``dem``: ``max(0, n · L)`` in ``[0, 1]``.

    ``dx`` / ``dy`` are the cell size in metres (east and north). ``shade_gain``
    multiplies the horizontal gradient before the normal is formed — a purely
    cartographic slope exaggeration for legibility (Swiss-style maps often shade
    as if slopes were ~1.3–1.5× steeper). It is **not** a vertical exaggeration
    of the terrain and has no bearing on the shadow passes; leave it at 1.0 for
    a physically faithful shade.

    This is the raking light *only* — no zenithal component. For IGN's "deux
    soleils" estompage, blend this with :func:`slope` via :func:`compose_relief`.

    Returns a ``[0, 1]`` float array, NaN where ``dem`` is NaN. The surface
    normal is ``(-dz/dx, -dz/dy, 1)`` normalised; ``dz/dy`` uses the north-up
    sign (row 0 is north, so ``d/drow`` is ``-d/dnorth``).
    """
    z = dem.astype("float64")
    # np.gradient over a NaN-padded array spreads NaN one cell into the interior;
    # fill holes with a local mean first so the shading stays defined to the rim.
    filled = _fill_nan(z)
    return _hillshade_prefilled(filled, dx, dy, sun, np.isfinite(dem), shade_gain)


def _hillshade_prefilled(
    filled: np.ndarray,
    dx: float,
    dy: float,
    sun: np.ndarray,
    valid: np.ndarray,
    shade_gain: float = 1.0,
) -> np.ndarray:
    """``hillshade`` core, given the already-NaN-filled DEM."""
    nx, ny, nz = _surface_normal(filled, dx, dy, shade_gain)
    ndl = np.clip(nx * sun[0] + ny * sun[1] + nz * sun[2], 0.0, 1.0)
    return np.where(valid, ndl, np.nan)


def slope(
    dem: np.ndarray,
    dx: float,
    dy: float,
    *,
    shade_gain: float = 1.0,
) -> np.ndarray:
    """Steepness of ``dem`` as ``arctan(‖∇z‖) / (π/2)`` in ``[0, 1]``.

    Direction-free: 0 on level ground, → 1 approaching vertical. This is the
    ingredient for a zenithal light (``1 - slope`` = a straight-down "sun":
    bright flat, dark steep, aspect ignored) or a plain slope tint. ``shade_gain``
    steepens it the same way it steepens the hillshade. NaN where ``dem`` is NaN.
    """
    filled = _fill_nan(dem.astype("float64"))
    return _slope_prefilled(filled, dx, dy, np.isfinite(dem), shade_gain)


def _slope_prefilled(
    filled: np.ndarray,
    dx: float,
    dy: float,
    valid: np.ndarray,
    shade_gain: float = 1.0,
) -> np.ndarray:
    gr, gc = np.gradient(filled)
    grad = np.hypot(shade_gain * gc / dx, shade_gain * gr / dy)
    s = np.arctan(grad) / (np.pi / 2.0)
    return np.where(valid, np.clip(s, 0.0, 1.0), np.nan)


def _surface_normal(
    filled: np.ndarray, dx: float, dy: float, shade_gain: float = 1.0
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Unit surface normal components ``(nx, ny, nz)`` for a NaN-free grid.

    ``nz > 0`` everywhere. ``shade_gain`` steepens the apparent slope (shading
    only). Used by the hillshade and the core-shadow test.
    """
    gr, gc = np.gradient(filled)
    dz_dx = shade_gain * gc / dx          # east
    dz_dy = shade_gain * -gr / dy         # north (row increases southward)
    nx, ny, nz = -dz_dx, -dz_dy, np.ones_like(filled)
    norm = np.sqrt(nx * nx + ny * ny + nz * nz)
    return nx / norm, ny / norm, nz / norm


def core_shadow(
    dem: np.ndarray,
    dx: float,
    dy: float,
    sun: np.ndarray,
) -> np.ndarray:
    """Boolean mask: ``True`` where the surface faces away from the sun (``n·L ≤ 0``).

    The self-shadow / terminator — a local test on the surface normal, no ray
    march. On a shaded map this is exactly the region the hillshade has already
    driven to black, so it usually needs no separate ink; it is returned so a
    caller can subtract it from the cast-shadow mask or ink it deliberately.
    """
    filled = _fill_nan(dem.astype("float64"))
    nx, ny, nz = _surface_normal(filled, dx, dy)
    ndl = nx * sun[0] + ny * sun[1] + nz * sun[2]
    return (ndl <= 0.0) & np.isfinite(dem)


def cast_shadows(
    dem: np.ndarray,
    dx: float,
    dy: float,
    sun: np.ndarray,
    *,
    max_steps: int | None = None,
) -> np.ndarray:
    """Boolean mask: ``True`` where a pixel is hidden from the sun by higher ground.

    For every pixel, walk from it toward the sun in ~1-cell steps. At step ``k``
    the straight sun ray sits at height ``dem + k * step_len * tan(altitude)``;
    if the terrain sampled there is above the ray, the pixel is occluded. The
    march stops once the ray clears the DEM's own maximum (nothing can occlude
    it any more) or the sample leaves the grid.

    Uses the true DEM and the true sun altitude — no vertical exaggeration. The
    mask includes lee slopes that are also core-shadowed; subtract
    :func:`core_shadow` to get the *projected* shadow alone.

    All pixels march together, so the cost is ``O(max_steps * rows * cols)`` —
    for a few-hundred-square grid that is a handful of milliseconds. Off-grid
    (NaN) pixels come back ``False``.
    """
    z = dem.astype("float64")
    return _cast_shadows_prefilled(z, _fill_nan(z), dx, dy, sun,
                                   np.isfinite(dem), max_steps)


def _cast_shadows_prefilled(
    z: np.ndarray,
    filled: np.ndarray,
    dx: float,
    dy: float,
    sun: np.ndarray,
    valid: np.ndarray,
    max_steps: int | None,
) -> np.ndarray:
    """``cast_shadows`` core, given the already-NaN-filled DEM and a valid mask.

    Only the pixels still in play march each step: they are carried as flat
    index arrays and pruned as soon as a pixel is shadowed or its ray climbs
    clear of the terrain maximum. That turns the inner loop from "resample the
    whole grid ~700 times" into a shrinking 1-D gather.
    """
    rows, cols = z.shape
    zmax = float(np.nanmax(filled))

    hx, hy = sun[0], sun[1]
    hmag = float(np.hypot(hx, hy))
    if hmag < 1e-9:  # sun straight overhead: nothing is shadowed
        return np.zeros_like(z, dtype=bool)
    # cells to advance per step: normalise to the larger axis so the longest
    # stride is ~1 cell. Row moves north = -y.
    step_c = hx / hmag
    step_r = -(hy / hmag)
    scale = 1.0 / max(abs(step_c), abs(step_r))
    step_c *= scale
    step_r *= scale
    step_len_m = float(np.hypot(step_c * dx, step_r * dy))
    dz_per_step = step_len_m * np.tan(np.radians(_altitude_deg(sun)))

    if max_steps is None:
        max_steps = int(np.hypot(rows, cols)) + 2

    shadow_flat = np.zeros(rows * cols, dtype=bool)
    idx = np.flatnonzero(valid)                 # flat indices still marching
    rr = (idx // cols).astype("float64")
    cc = (idx % cols).astype("float64")
    ray_h = filled.ravel()[idx].copy()

    for _ in range(max_steps):
        if idx.size == 0:
            break
        cc += step_c
        rr += step_r
        ray_h += dz_per_step
        keep = (cc >= 0) & (cc <= cols - 1) & (rr >= 0) & (rr <= rows - 1) & (
            ray_h < zmax
        )
        if not keep.all():
            idx, rr, cc, ray_h = idx[keep], rr[keep], cc[keep], ray_h[keep]
            if idx.size == 0:
                break
        terr = _bilinear_flat(filled, rr, cc)
        hit = terr > ray_h
        if hit.any():
            shadow_flat[idx[hit]] = True
            live = ~hit
            idx, rr, cc, ray_h = idx[live], rr[live], cc[live], ray_h[live]

    return shadow_flat.reshape(rows, cols) & valid


def shaded_relief(
    dem: np.ndarray,
    *,
    dx: float,
    dy: float,
    sun_azimuth: float,
    sun_altitude: float,
    shade_gain: float = 1.0,
    soft_px: float = 0.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """The four separable relief fields, on the DEM grid, NaN outside the DEM.

    Returns ``(hillshade, slope, core_shadow, cast_shadow)``:

    * ``hillshade`` — ``[0, 1]`` directional Lambert (aspect × sun altitude);
    * ``slope`` — ``[0, 1]`` steepness, direction-free (``arctan(‖∇z‖)``);
    * ``core_shadow`` — float ``[0, 1]`` (after any ``soft_px`` blur), 1 where
      the surface faces away from the sun (``n·L ≤ 0``);
    * ``cast_shadow`` — float ``[0, 1]``, 1 where a *sun-facing* pixel is blocked
      by higher ground upwind. Core-shadowed pixels are removed, so this is the
      projected shadow alone.

    Nothing is combined here. ``shade_gain`` steepens the ``hillshade`` and
    ``slope`` fields only (cartographic; 1.0 = faithful) and never the shadow
    geometry. Use :func:`compose_relief` for the IGN "deux soleils" blend.
    """
    sun = sun_vector(sun_azimuth, sun_altitude)
    z = dem.astype("float64")
    valid = np.isfinite(dem)
    filled = _fill_nan(z)                 # one fill shared by every pass

    hs = _hillshade_prefilled(filled, dx, dy, sun, valid, shade_gain)
    sl = _slope_prefilled(filled, dx, dy, valid, shade_gain)

    nx, ny, nz = _surface_normal(filled, dx, dy)
    ndl = nx * sun[0] + ny * sun[1] + nz * sun[2]
    core = (ndl <= 0.0) & valid

    occ = _cast_shadows_prefilled(z, filled, dx, dy, sun, valid, None)
    cast = occ & ~core                    # projected shadow, minus the terminator

    core_f = core.astype("float64")
    cast_f = cast.astype("float64")
    if soft_px > 0:
        core_f = _box_blur(core_f, soft_px)
        cast_f = _box_blur(cast_f, soft_px)

    core_f = np.where(valid, core_f, np.nan)
    cast_f = np.where(valid, cast_f, np.nan)
    return hs, sl, core_f, cast_f


def compose_relief(
    hillshade_arr: np.ndarray,
    slope_arr: np.ndarray,
    *,
    zenith_weight: float = 0.0,
) -> np.ndarray:
    """Blend a raking hillshade with a zenithal light — IGN's "deux soleils".

    ``(1 - w) * hillshade + w * (1 - slope)``. The zenithal term ``1 - slope``
    is a straight-down light: bright on the flats, dark on the steeps, aspect
    ignored. Mixing it in keeps slopes facing away from the raking light from
    collapsing to solid black. ``w = 0`` returns the hillshade untouched;
    ``w ≈ 0.5`` is the usual estompage. NaN is preserved.
    """
    w = float(np.clip(zenith_weight, 0.0, 1.0))
    if w == 0.0:
        return hillshade_arr
    out = (1.0 - w) * hillshade_arr + w * (1.0 - slope_arr)
    return np.clip(out, 0.0, 1.0)


# --------------------------------------------------------------------- helpers


def _altitude_deg(sun: np.ndarray) -> float:
    return float(np.degrees(np.arcsin(np.clip(sun[2], -1.0, 1.0))))


def _bilinear(arr: np.ndarray, rr: np.ndarray, cc: np.ndarray) -> np.ndarray:
    """Sample ``arr`` (2-D, no NaN) at float row/col coords, clamped to edges."""
    return _bilinear_flat(arr, np.asarray(rr, dtype="float64"),
                          np.asarray(cc, dtype="float64"))


def _bilinear_flat(arr: np.ndarray, rr: np.ndarray, cc: np.ndarray) -> np.ndarray:
    """Bilinear sample of ``arr`` at matching-shape float ``rr`` / ``cc``.

    Works for any shape (the shadow march passes 1-D index arrays); indices are
    clamped to the array edges. One flat gather instead of 2-D fancy indexing.
    """
    rows, cols = arr.shape
    flat = arr.ravel()
    r0 = np.clip(np.floor(rr), 0, rows - 1).astype(np.intp)
    c0 = np.clip(np.floor(cc), 0, cols - 1).astype(np.intp)
    r1 = np.minimum(r0 + 1, rows - 1)
    c1 = np.minimum(c0 + 1, cols - 1)
    fr = np.clip(rr - r0, 0.0, 1.0)
    fc = np.clip(cc - c0, 0.0, 1.0)
    v00 = flat[r0 * cols + c0]
    v01 = flat[r0 * cols + c1]
    v10 = flat[r1 * cols + c0]
    v11 = flat[r1 * cols + c1]
    top = v00 * (1 - fc) + v01 * fc
    bot = v10 * (1 - fc) + v11 * fc
    return top * (1 - fr) + bot * fr


def _fill_nan(z: np.ndarray) -> np.ndarray:
    """Replace NaN with an iteratively grown neighbour mean; keeps gradients sane.

    Good enough for shading a thin apron around the divide — a few dilation
    passes, then anything still unset drops to the global min.
    """
    out = z.copy()
    nan = ~np.isfinite(out)
    if not nan.any():
        return out
    for _ in range(8):
        if not nan.any():
            break
        acc = np.zeros_like(out)
        cnt = np.zeros_like(out)
        for sr, sc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            shifted = np.roll(out, (sr, sc), axis=(0, 1))
            good = np.roll(~nan, (sr, sc), axis=(0, 1))
            acc += np.where(good, shifted, 0.0)
            cnt += good
        fillable = nan & (cnt > 0)
        out[fillable] = acc[fillable] / cnt[fillable]
        nan = ~np.isfinite(out)
    out[~np.isfinite(out)] = np.nanmin(z)
    return out


def _box_blur(a: np.ndarray, radius_px: float) -> np.ndarray:
    """Separable box blur via a running sum; ``radius_px`` rounded up to pixels.

    O(rows*cols) with no per-line Python loop: a padded cumulative sum gives the
    windowed mean along each axis in two slices.
    """
    r = int(np.ceil(radius_px))
    if r < 1:
        return a
    w = 2 * r + 1
    out = a.astype("float64")
    for axis in (0, 1):
        pad = [(0, 0), (0, 0)]
        pad[axis] = (r + 1, r)
        cs = np.cumsum(np.pad(out, pad, mode="edge"), axis=axis)
        lo = np.take(cs, np.arange(0, out.shape[axis]), axis=axis)
        hi = np.take(cs, np.arange(w, w + out.shape[axis]), axis=axis)
        out = (hi - lo) / w
    return out
