"""Render one valley's catchment as a sun-lit isometric diorama — headless Blender.

Runs inside Blender's bundled Python (``bpy`` + ``numpy``, no geo libs). Feed it
a valley directory prepared by ``valleespyr.render.terrain_export``::

    blender --background --python scripts/render_valley.py -- data/valleys/rioumajou --view SE01

It reads

* ``<dir>/valley.toml``            — the render recipe (see schema below)
* ``<dir>/derived/terrain.npy``    — float32 (rows, cols), metres, NaN off-basin
* ``<dir>/derived/terrain.json``   — bounds / cell size / z-range
* ``<dir>/derived/streams.json``   — optional; normalised-grid polylines

and writes ``<dir>/<slug>_<view>_3d.png`` plus a sibling ``.blend`` you can open
and tweak by hand (pass --no-save-blend to skip the .blend).

------------------------------------------------------------------- valley.toml
    [meta]
    slug = "rioumajou"

    [terrain]
    exaggeration = 1.8          # vertical stretch; 1.0 = true scale
    base_thickness_frac = 0.15  # solid plinth depth, as a frac of the z-span

    [view.SE01]
    camera_azimuth   = 135.0    # deg, 0 = looking from due north, clockwise
    camera_elevation = 32.0     # deg above the horizon
    ortho_margin     = 1.06     # ortho frustum = basin bbox * this
    sun_azimuth      = 115.0    # deg, compass bearing the sun sits at
    sun_altitude     = 30.0     # deg above the horizon
    sun_hardness     = 0.5      # sun angular diameter, deg (0.53 = real sun; ↑ = softer)
    samples          = 256      # Cycles samples
    resolution       = [6000, 4500]
    background       = "#efe9dc"   # world colour; "transparent" for alpha
    show_streams     = true
------------------------------------------------------------------------------
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import bpy  # type: ignore
import numpy as np

try:  # py3.11+ in modern Blender; fall back for older bundles
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


# --------------------------------------------------------------------------- args


def _parse_args() -> tuple[Path, str, bool]:
    argv = sys.argv
    args = argv[argv.index("--") + 1 :] if "--" in argv else []
    if not args:
        raise SystemExit("usage: ... -- <valley_dir> [--view NAME] [--no-save-blend]")
    valley_dir = Path(args[0]).resolve()
    view = "default"
    save_blend = True  # a .blend next to the PNG by default; --no-save-blend skips it
    i = 1
    while i < len(args):
        if args[i] == "--view":
            view = args[i + 1]
            i += 2
        elif args[i] == "--no-save-blend":
            save_blend = False
            i += 1
        elif args[i] == "--save-blend":  # accepted for symmetry; already the default
            save_blend = True
            i += 1
        else:
            raise SystemExit(f"unknown arg {args[i]!r}")
    return valley_dir, view, save_blend


# ----------------------------------------------------------------------- config


def _load_config(valley_dir: Path, view: str) -> tuple[dict, dict, str]:
    cfg = tomllib.loads((valley_dir / "valley.toml").read_text("utf-8"))
    slug = cfg.get("meta", {}).get("slug") or valley_dir.name
    terrain = cfg.get("terrain", {})
    views = cfg.get("view", {})
    if view not in views:
        raise SystemExit(
            f"view {view!r} not in valley.toml (have: {', '.join(views) or 'none'})"
        )
    return terrain, views[view], slug


def _hex_rgb(s: str) -> tuple[float, float, float]:
    s = s.lstrip("#")
    r, g, b = (int(s[i : i + 2], 16) / 255 for i in (0, 2, 4))
    # sRGB -> linear, so the world colour looks right in Cycles
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return (lin(r), lin(g), lin(b))


# ------------------------------------------------------------------------- mesh


def _build_terrain_mesh(valley_dir: Path, exaggeration: float, base_frac: float):
    """A solid diorama block: draped top surface over in-basin cells, walls to a base.

    Returns ``(obj, meta, world_dims)`` where ``world_dims`` is
    ``(width_m, height_m, z_span_m)`` and the object is centred on the origin
    with its base plinth at ``z = 0``.
    """
    band = np.load(valley_dir / "derived" / "terrain.npy")
    meta = json.loads((valley_dir / "derived" / "terrain.json").read_text("utf-8"))
    rows, cols = band.shape
    b = meta["bounds"]

    W = (b["east"] - b["west"]) * meta["m_per_deg_lon"]
    H = (b["north"] - b["south"]) * meta["m_per_deg_lat"]
    z_span = max(meta["z_max"] - meta["z_min"], 1.0)

    inside = np.isfinite(band)
    z = np.where(inside, band, meta["z_min"]).astype(np.float64)
    zc = (z - meta["z_min"]) * exaggeration  # metres above the plinth top
    base_y = -base_frac * z_span * exaggeration  # plinth floor, below zc=0

    # world x/z for grid node (col, row); row 0 = north.
    def px(c: np.ndarray | float):
        return (c / (cols - 1) - 0.5) * W

    def pz(r: np.ndarray | float):
        return (0.5 - r / (rows - 1)) * H

    verts: list[tuple[float, float, float]] = []
    faces: list[tuple[int, ...]] = []

    # -- top surface: one vertex per in-basin grid node, quads where all 4 corners are in
    top_idx = np.full((rows, cols), -1, dtype=np.int64)

    def add_top(r: int, c: int) -> int:
        if top_idx[r, c] != -1:
            return int(top_idx[r, c])
        vid = len(verts)
        verts.append((px(c), zc[r, c], pz(r)))  # note: Blender Z-up handled at export
        top_idx[r, c] = vid
        return vid

    def cell_in(c: int, r: int) -> bool:
        if c < 0 or r < 0 or c >= cols - 1 or r >= rows - 1:
            return False
        return bool(
            inside[r, c] and inside[r, c + 1] and inside[r + 1, c] and inside[r + 1, c + 1]
        )

    for r in range(rows - 1):
        for c in range(cols - 1):
            if not cell_in(c, r):
                continue
            a = add_top(r, c)
            bb = add_top(r, c + 1)
            d = add_top(r + 1, c)
            e = add_top(r + 1, c + 1)
            faces.append((a, d, e, bb))  # quad, CCW from above

    # -- walls: a vertical curtain wherever an in-basin cell meets an out cell
    def wall(c1: int, r1: int, c2: int, r2: int) -> None:
        x1, z1, y1 = px(c1), zc[r1, c1], pz(r1)
        x2, z2, y2 = px(c2), zc[r2, c2], pz(r2)
        t1 = len(verts)
        verts.append((x1, z1, y1))
        t2 = len(verts)
        verts.append((x2, z2, y2))
        b1 = len(verts)
        verts.append((x1, base_y, y1))
        b2 = len(verts)
        verts.append((x2, base_y, y2))
        faces.append((t1, b1, b2, t2))

    for r in range(rows - 1):
        for c in range(cols - 1):
            if not cell_in(c, r):
                continue
            if not cell_in(c, r - 1):
                wall(c, r, c + 1, r)
            if not cell_in(c, r + 1):
                wall(c + 1, r + 1, c, r + 1)
            if not cell_in(c - 1, r):
                wall(c, r + 1, c, r)
            if not cell_in(c + 1, r):
                wall(c + 1, r, c + 1, r + 1)

    # -- base quad
    b0 = len(verts)
    verts.extend(
        [
            (px(0), base_y, pz(0)),
            (px(cols - 1), base_y, pz(0)),
            (px(cols - 1), base_y, pz(rows - 1)),
            (px(0), base_y, pz(rows - 1)),
        ]
    )
    faces.append((b0, b0 + 1, b0 + 2, b0 + 3))

    # y-up (our build) -> z-up (Blender): (x, y, z_our) becomes (x, z_our, y)
    bl_verts = [(x, y, z) for (x, z, y) in verts]

    mesh = bpy.data.meshes.new("terrain")
    mesh.from_pydata(bl_verts, [], faces)
    mesh.validate()
    mesh.update()
    obj = bpy.data.objects.new("terrain", mesh)
    bpy.context.collection.objects.link(obj)

    # centre on origin, plinth base at z = 0
    obj.location = (0.0, 0.0, -base_y)

    mod = obj.modifiers.new("smooth", "SMOOTH")
    mod.iterations = 1
    mod.factor = 0.3
    for poly in mesh.polygons:
        poly.use_smooth = True

    z_span_world = float(zc[inside].max() - base_y)
    return obj, meta, (W, H, z_span_world)


# --------------------------------------------------------------------- material


def _paper_material(name: str, rgb=(0.82, 0.80, 0.73)):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (*rgb, 1.0)
    bsdf.inputs["Roughness"].default_value = 0.95
    if "Specular" in bsdf.inputs:
        bsdf.inputs["Specular"].default_value = 0.15
    elif "Specular IOR Level" in bsdf.inputs:
        bsdf.inputs["Specular IOR Level"].default_value = 0.2
    return mat


# ----------------------------------------------------------------------- scene


def _clear_scene() -> None:
    for coll in (bpy.data.objects, bpy.data.meshes, bpy.data.materials,
                 bpy.data.lights, bpy.data.cameras, bpy.data.curves):
        for item in list(coll):
            coll.remove(item, do_unlink=True)


def _link(obj) -> None:
    """Link ``obj`` into the scene's master collection (works in --background)."""
    bpy.context.scene.collection.objects.link(obj)


def _dir_from_az_alt(azimuth_deg: float, altitude_deg: float) -> np.ndarray:
    """Unit vector pointing *toward* a compass azimuth / altitude.

    Azimuth is a bearing: 0 = +Y (our "north"), 90 = +X (east), measured
    clockwise seen from above. Altitude is degrees above the horizon.
    World is Z-up.
    """
    az = math.radians(azimuth_deg)
    alt = math.radians(altitude_deg)
    return np.array(
        [math.sin(az) * math.cos(alt), math.cos(az) * math.cos(alt), math.sin(alt)],
        dtype=float,
    )


def _look_along(dir_vec):
    """Euler that orients an object's -Z axis along ``dir_vec`` (world), +Y up hint."""
    import mathutils  # type: ignore

    seq = dir_vec.tolist() if hasattr(dir_vec, "tolist") else list(dir_vec)
    return mathutils.Vector(seq).normalized().to_track_quat("-Z", "Y").to_euler()


def _add_sun(azimuth_deg: float, altitude_deg: float, hardness_deg: float):
    light = bpy.data.lights.new("sun", "SUN")
    light.energy = 4.0
    light.angle = math.radians(max(hardness_deg, 0.02))
    obj = bpy.data.objects.new("sun", light)
    _link(obj)
    # The lamp shines along its -Z. It sits toward (az, alt) and shines back at
    # the scene, so -Z must point *away* from the sun position: along -dir.
    sun_dir = _dir_from_az_alt(azimuth_deg, altitude_deg)
    obj.rotation_euler = _look_along(-sun_dir)
    return obj


def _add_camera(terrain_obj, world_dims, azimuth_deg, elevation_deg, margin):
    """Orthographic camera at (azimuth, elevation), framed on the terrain block.

    Distance and ``ortho_scale`` are derived from the block's *projected* extent
    seen from this angle, so any valley shape and any view direction lands
    centred and filling the frame to ``margin``.
    """
    import mathutils  # type: ignore

    W, H, Z = world_dims
    diag = math.sqrt(W * W + H * H + Z * Z)
    view_dir = _dir_from_az_alt(azimuth_deg, elevation_deg)  # points at the viewer

    # centre of the mesh in world space
    verts = terrain_obj.data.vertices
    co = np.array([terrain_obj.matrix_world @ v.co for v in verts])
    centre = co.mean(axis=0)

    cam_data = bpy.data.cameras.new("cam")
    cam_data.type = "ORTHO"
    cam = bpy.data.objects.new("cam", cam_data)
    _link(cam)
    bpy.context.scene.camera = cam

    dist = diag * 1.5
    cam.location = (centre + view_dir * dist).tolist()
    cam.rotation_euler = _look_along(mathutils.Vector((-view_dir).tolist()))
    bpy.context.view_layer.update()

    # project every vertex into camera space; size the frustum to the spread
    m_inv = cam.matrix_world.inverted()
    cam_co = np.array([(m_inv @ mathutils.Vector(p.tolist()))[:] for p in co])
    half_w = np.abs(cam_co[:, 0]).max()
    half_h = np.abs(cam_co[:, 1]).max()
    cam_data.ortho_scale = 2.0 * max(half_w, half_h) * margin
    depth = -cam_co[:, 2]  # +ve in front
    cam_data.clip_start = max(depth.min() - diag * 0.1, 0.01)
    cam_data.clip_end = depth.max() + diag * 0.1
    return cam


def _setup_denoising(scene, samples: int) -> None:
    """Enable an available Cycles denoiser; else bump samples and render raw.

    Blender takes the ``denoiser`` enum assignment without complaint even when
    the build ships no denoiser — it only fails at render time (``Build without
    OpenImageDenoiser``). So probe the enum's actual items and fall back.
    """
    cy = scene.cycles
    try:
        items = cy.bl_rna.properties["denoiser"].enum_items.keys()
    except Exception:
        items = []
    for name in ("OPENIMAGEDENOISE", "OPTIX"):
        if name in items:
            try:
                cy.denoiser = name
                cy.use_denoising = True
                print(f"[render] denoiser: {name}")
                return
            except (TypeError, RuntimeError):
                pass
    cy.use_denoising = False
    cy.samples = max(samples, 512)
    print(f"[render] no denoiser in this build — raw render at {cy.samples} spp")


def _add_streams(valley_dir: Path, meta: dict, world_dims, exaggeration: float):
    path = valley_dir / "derived" / "streams.json"
    if not path.exists():
        return
    lines = json.loads(path.read_text("utf-8")).get("lines", [])
    if not lines:
        return
    W, H, _ = world_dims
    band = np.load(valley_dir / "derived" / "terrain.npy")
    rows, cols = band.shape
    z_min = meta["z_min"]
    lift = (meta["z_max"] - z_min) * exaggeration * 0.008

    def sample_z(u: float, v: float) -> float:
        fc = min(max(u, 0.0), 1.0) * (cols - 1)
        fr = (1.0 - min(max(v, 0.0), 1.0)) * (rows - 1)
        c0, r0 = int(fc), int(fr)
        c1, r1 = min(c0 + 1, cols - 1), min(r0 + 1, rows - 1)
        tx, ty = fc - c0, fr - r0
        vals = band[[r0, r0, r1, r1], [c0, c1, c0, c1]]
        vals = np.where(np.isfinite(vals), vals, z_min)
        top = vals[0] * (1 - tx) + vals[1] * tx
        bot = vals[2] * (1 - tx) + vals[3] * tx
        return float((top * (1 - ty) + bot * ty - z_min) * exaggeration + lift)

    curve = bpy.data.curves.new("streams", "CURVE")
    curve.dimensions = "3D"
    curve.bevel_depth = max(W, H) * 0.0016
    curve.bevel_resolution = 2
    for poly in lines:
        spline = curve.splines.new("POLY")
        spline.points.add(len(poly) - 1)
        for i, (u, v) in enumerate(poly):
            spline.points[i].co = ((u - 0.5) * W, (v - 0.5) * H, sample_z(u, v), 1.0)
    obj = bpy.data.objects.new("streams", curve)
    bpy.context.collection.objects.link(obj)
    mat = bpy.data.materials.new("water")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (0.20, 0.52, 0.72, 1.0)
    bsdf.inputs["Roughness"].default_value = 0.25
    curve.materials.append(mat)


# ------------------------------------------------------------------------ render


def main() -> None:
    valley_dir, view, save_blend = _parse_args()
    terrain_cfg, v, slug = _load_config(valley_dir, view)

    exaggeration = float(terrain_cfg.get("exaggeration", 1.0))
    base_frac = float(terrain_cfg.get("base_thickness_frac", 0.15))

    _clear_scene()
    obj, meta, world_dims = _build_terrain_mesh(valley_dir, exaggeration, base_frac)
    obj.data.materials.append(_paper_material("terrain_paper"))

    _add_sun(
        float(v.get("sun_azimuth", 135.0)),
        float(v.get("sun_altitude", 30.0)),
        float(v.get("sun_hardness", 0.53)),
    )
    _add_camera(
        obj,
        world_dims,
        float(v.get("camera_azimuth", 135.0)),
        float(v.get("camera_elevation", 32.0)),
        float(v.get("ortho_margin", 1.06)),
    )
    if v.get("show_streams", True):
        _add_streams(valley_dir, meta, world_dims, exaggeration)

    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    try:
        scene.cycles.device = "GPU"
    except Exception:
        pass
    scene.cycles.samples = int(v.get("samples", 256))
    _setup_denoising(scene, int(v.get("samples", 256)))

    res = v.get("resolution", [4000, 3000])
    scene.render.resolution_x, scene.render.resolution_y = int(res[0]), int(res[1])
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_depth = "16"

    bg = v.get("background", "#efe9dc")
    world = bpy.data.worlds.new("world")
    world.use_nodes = True
    scene.world = world
    bg_node = world.node_tree.nodes["Background"]
    if bg == "transparent":
        scene.render.film_transparent = True
        bg_node.inputs["Strength"].default_value = 0.6
        bg_node.inputs["Color"].default_value = (0.9, 0.9, 0.9, 1.0)
    else:
        bg_node.inputs["Color"].default_value = (*_hex_rgb(bg), 1.0)
        bg_node.inputs["Strength"].default_value = 0.55

    out_png = valley_dir / f"{slug}_{view}_3d.png"
    scene.render.filepath = str(out_png)
    print(f"[render] {slug} / {view} -> {out_png}  ({res[0]}x{res[1]}, {scene.cycles.samples} spp)")
    bpy.ops.render.render(write_still=True)
    print(f"[render] wrote {out_png}")

    if save_blend:
        blend = valley_dir / f"{slug}_{view}_3d.blend"
        bpy.ops.wm.save_as_mainfile(filepath=str(blend))
        print(f"[render] wrote {blend}")


if __name__ == "__main__":
    main()
