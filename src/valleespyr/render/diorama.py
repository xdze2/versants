"""Build a self-contained isometric 3D diorama of a river's catchment.

Given a DEM-delineated catchment polygon, the Copernicus GLO-30 (COP30) tile
covering it and the catchment's stream network as GeoJSON, :func:`build_diorama`
bakes them into a single ``.html`` file:

* the elevation grid, clipped to the catchment outline, as a 16-bit heightmap
  PNG embedded as a data URI;
* the stream network, with a height sampled from the DEM at every vertex, as an
  array of 3D polylines;
* a three.js scene (orthographic camera at the true isometric angle,
  hypsometric tint + hillshade, configurable vertical exaggeration) that renders
  it all client-side. No server, no build step - open the file in a browser.

three.js loads from cdnjs at view time; everything else is baked in.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .terrain_data import build_terrain_payload

logger = logging.getLogger(__name__)

HTML = """<title>{title} 3D</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ margin: 0; background: #eae4d6; color: #5a5040;
         font: 13px/1.5 system-ui, sans-serif; overflow: hidden; }}
  #cv {{ display: block; width: 100vw; height: 100vh; }}
  #hud {{ position: fixed; left: 14px; top: 12px; pointer-events: none;
         text-shadow: 0 1px 2px rgba(255,255,255,.6); }}
  #hud h1 {{ margin: 0 0 2px; font-size: 15px; font-weight: 600; color: #4a4234; }}
  #hud p {{ margin: 0; opacity: .7; }}
  #tip {{ position: fixed; right: 14px; bottom: 12px; opacity: .5;
         text-align: right; }}
</style>
<canvas id="cv"></canvas>
<div id="hud">
  <h1>{title} &mdash; {subtitle}</h1>
  <p id="stats"></p>
</div>
<div id="tip">drag to orbit &middot; scroll to zoom &middot;
  {exag}&times; vertical &middot; contours 20 m</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/0.159.0/three.min.js"></script>
<script>
const META = {meta};
const STREAMS = {streams};
const CONTOURS_MINOR = {contours_minor};
const CONTOURS_INDEX = {contours_index};
const HEIGHTMAP = "{heightmap}";

// cdnjs stopped shipping OrbitControls as a standalone classic script, so the
// orbit/zoom below is a small hand-rolled version - enough for a demo.

const cv = document.getElementById('cv');
const renderer = new THREE.WebGLRenderer({{ canvas: cv, antialias: true }});
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));

const PAPER = 0xeae4d6;                 // IGN-ish cream ground
const scene = new THREE.Scene();
scene.background = new THREE.Color(PAPER);

// ---- world dimensions (metres, centred on origin) --------------------------
const b = META.bounds;
const W = (b.east - b.west) * META.m_per_deg_lon;
const H = (b.north - b.south) * META.m_per_deg_lat;
const zSpanTile = (META.z_max_tile - META.z_min_tile);
const SCALE = Math.max(W, H);

// faint depth haze toward the paper colour; range must span the camera
// distance or it swallows the whole scene
scene.fog = new THREE.Fog(PAPER, SCALE * 2.6, SCALE * 6.0);

let camera;
// half-height of the ortho frustum in world units; ~0.42 of the long side
// frames the block with a small margin
const VIEW_HALF = SCALE * 0.42;
{{
  const aspect = innerWidth / innerHeight;
  camera = new THREE.OrthographicCamera(
    -VIEW_HALF * aspect, VIEW_HALF * aspect, VIEW_HALF, -VIEW_HALF, 0.1, 400000);
}}

// ---- lights: flat and bright, like a printed sheet ----------------------
scene.add(new THREE.HemisphereLight(0xffffff, 0xd8d0be, 1.15));
const sun = new THREE.DirectionalLight(0xfff6e8, 0.85);
sun.position.set(-W * 0.8, H * 1.6, H * 0.7);  // gentle NW relief only
scene.add(sun);

// ---- terrain block --------------------------------------------------------
// Built as a solid diorama: only in-catchment grid cells get a top surface;
// the catchment rim is walled straight down to a flat base a little below the
// lowest cell. All one indexed BufferGeometry with vertex colours.
const COLS = META.cols, ROWS = META.rows;
const zEx = META.z_exaggeration;
let terrainCentreY = 0;
let sampleSurfaceY = null;  // (u, v) -> world Y on the terrain top, set by loader
let insideUV = null;        // (u, v) -> bool, is this point in the catchment

const loader = new THREE.TextureLoader();
loader.load(HEIGHTMAP, (tex) => {{
  const cvs = document.createElement('canvas');
  cvs.width = COLS; cvs.height = ROWS;
  const ctx = cvs.getContext('2d', {{ willReadFrequently: true }});
  ctx.drawImage(tex.image, 0, 0);
  const data = ctx.getImageData(0, 0, COLS, ROWS).data;

  const inside = new Uint8Array(COLS * ROWS);
  const elevY = new Float32Array(COLS * ROWS);   // exaggerated height, world
  const normT = new Float32Array(COLS * ROWS);   // 0..1 for the colour ramp
  const shadeT = new Float32Array(COLS * ROWS);  // 0..1 baked IGN-style relief shade
  let minY = Infinity, maxY = -Infinity;
  for (let i = 0; i < COLS * ROWS; i++) {{
    const o = i * 4;
    const ins = data[o + 3] !== 0;
    inside[i] = ins ? 1 : 0;
    const q = data[o] + (data[o + 1] << 8);
    const t = q / 65535;
    normT[i] = t;
    shadeT[i] = data[o + 2] / 255;
    const y = t * zSpanTile * zEx;
    elevY[i] = y;
    if (ins) {{ if (y < minY) minY = y; if (y > maxY) maxY = y; }}
  }}
  const baseY = minY - zSpanTile * zEx * 0.18;   // block floor
  terrainCentreY = -(maxY + baseY) / 2;

  // bilinear surface-height sampler in normalised grid space (u east, v north)
  sampleSurfaceY = (u, v) => {{
    const fc = Math.min(Math.max(u, 0), 1) * (COLS - 1);
    const fr = (1 - Math.min(Math.max(v, 0), 1)) * (ROWS - 1);
    const c0 = Math.floor(fc), r0 = Math.floor(fr);
    const c1 = Math.min(c0 + 1, COLS - 1), r1 = Math.min(r0 + 1, ROWS - 1);
    const tx = fc - c0, ty = fr - r0;
    const g = (r, c) => elevY[r * COLS + c];
    return (g(r0, c0) * (1 - tx) + g(r0, c1) * tx) * (1 - ty) +
           (g(r1, c0) * (1 - tx) + g(r1, c1) * tx) * ty;
  }};

  // nearest-cell mask test - used to break contour lines at the catchment rim
  // so they don't drop a vertical "icicle" down the skirt
  insideUV = (u, v) => {{
    const c = Math.round(Math.min(Math.max(u, 0), 1) * (COLS - 1));
    const r = Math.round((1 - Math.min(Math.max(v, 0), 1)) * (ROWS - 1));
    return inside[r * COLS + c] === 1;
  }};

  // world x/z for a grid node (col, row); row 0 = north (+z)
  const px = (c) => (c / (COLS - 1) - 0.5) * W;
  const pz = (r) => (0.5 - r / (ROWS - 1)) * H;

  const cellInside = (c, r) => {{
    if (c < 0 || r < 0 || c >= COLS - 1 || r >= ROWS - 1) return false;
    const a = r * COLS + c;
    return inside[a] && inside[a + 1] && inside[a + COLS] && inside[a + COLS + 1];
  }};

  // --- top surface: smooth-shaded, hypsometric vertex colours -------------
  const tPos = [], tCol = [], tIdx = [];
  const topIdx = new Int32Array(COLS * ROWS).fill(-1);
  const ramp = hypsometricRamp();
  function topVert(c, r) {{
    const gi = r * COLS + c;
    if (topIdx[gi] !== -1) return topIdx[gi];
    const id = tPos.length / 3;
    tPos.push(px(c), elevY[gi], pz(r));
    const col = ramp(normT[gi]);
    // blend the baked shade with a flat 1.0 so shadow-side slopes darken
    // without ever going fully black - the ramp already carries the
    // hypsometric colour, this only modulates its brightness
    const shade = 0.45 + 0.55 * shadeT[gi];
    tCol.push(col[0] * shade, col[1] * shade, col[2] * shade);
    topIdx[gi] = id;
    return id;
  }}
  for (let r = 0; r < ROWS - 1; r++) {{
    for (let c = 0; c < COLS - 1; c++) {{
      if (!cellInside(c, r)) continue;
      const va = topVert(c, r), vb = topVert(c + 1, r);
      const vd = topVert(c, r + 1), ve = topVert(c + 1, r + 1);
      tIdx.push(va, vd, vb,  vb, vd, ve); // CCW seen from above
    }}
  }}
  const topGeo = new THREE.BufferGeometry();
  topGeo.setAttribute('position', new THREE.Float32BufferAttribute(tPos, 3));
  topGeo.setAttribute('color', new THREE.Float32BufferAttribute(tCol, 3));
  topGeo.setIndex(tIdx);
  topGeo.computeVertexNormals();
  const topMesh = new THREE.Mesh(topGeo, new THREE.MeshStandardMaterial({{
    vertexColors: true, roughness: 0.95, metalness: 0.0, side: THREE.DoubleSide,
  }}));
  topMesh.position.y = terrainCentreY;
  scene.add(topMesh);

  // --- rim skirt + base: one clean vertical curtain from the true rim height
  //     down to the block floor. Sharing the exact top-surface rim vertices
  //     means no slivers between wall panels. Uniform dark rock colour. -----
  const wPos = [], wIdx = [];
  function skirtVert(x, y, z) {{ const id = wPos.length / 3; wPos.push(x, y, z); return id; }}
  function panel(c1, r1, c2, r2) {{
    // top edge follows the surface exactly (same elevY as the top mesh)
    const x1 = px(c1), z1 = pz(r1), y1 = elevY[r1 * COLS + c1];
    const x2 = px(c2), z2 = pz(r2), y2 = elevY[r2 * COLS + c2];
    const a = skirtVert(x1, y1, z1), b = skirtVert(x2, y2, z2);
    const d = skirtVert(x1, baseY, z1), e = skirtVert(x2, baseY, z2);
    wIdx.push(a, d, b, b, d, e);
  }}
  for (let r = 0; r < ROWS - 1; r++) {{
    for (let c = 0; c < COLS - 1; c++) {{
      if (!cellInside(c, r)) continue;
      if (!cellInside(c, r - 1)) panel(c, r, c + 1, r);
      if (!cellInside(c, r + 1)) panel(c + 1, r + 1, c, r + 1);
      if (!cellInside(c - 1, r)) panel(c, r + 1, c, r);
      if (!cellInside(c + 1, r)) panel(c + 1, r, c + 1, r + 1);
    }}
  }}
  const b0 = wPos.length / 3;
  wPos.push(px(0), baseY, pz(0), px(COLS - 1), baseY, pz(0),
            px(0), baseY, pz(ROWS - 1), px(COLS - 1), baseY, pz(ROWS - 1));
  wIdx.push(b0, b0 + 1, b0 + 2, b0 + 2, b0 + 1, b0 + 3);

  // Unlit: the cut face reads as a warm paper edge, darkening toward the base
  // like the deckle of a stacked map. A vertical gradient via vertex colour;
  // no lighting, so the per-panel normal flips can't stripe it.
  const wCol = new Float32Array((wPos.length / 3) * 3);
  const edgeTop = [0.80, 0.74, 0.62], edgeBot = [0.42, 0.37, 0.30];
  for (let v = 0; v < wPos.length / 3; v++) {{
    const t = Math.max(0, Math.min(1, (wPos[v * 3 + 1] - baseY) / Math.max(1, maxY - baseY)));
    for (let j = 0; j < 3; j++)
      wCol[v * 3 + j] = edgeBot[j] + (edgeTop[j] - edgeBot[j]) * t;
  }}
  const wallGeo = new THREE.BufferGeometry();
  wallGeo.setAttribute('position', new THREE.Float32BufferAttribute(wPos, 3));
  wallGeo.setAttribute('color', new THREE.BufferAttribute(wCol, 3));
  wallGeo.setIndex(wIdx);
  const wallMesh = new THREE.Mesh(wallGeo, new THREE.MeshBasicMaterial({{
    vertexColors: true, side: THREE.DoubleSide,
  }}));
  wallMesh.position.y = terrainCentreY;
  scene.add(wallMesh);

  addContours(CONTOURS_MINOR, terrainCentreY, 0xa67846, 0.33, 0.0016);
  addContours(CONTOURS_INDEX, terrainCentreY, 0x704a24, 0.85, 0.0026);
  addStreams(terrainCentreY);
  document.getElementById('stats').textContent =
    `${{META.n_valid.toLocaleString()}} cells  |  ` +
    `${{META.z_min.toFixed(0)}}-${{META.z_max.toFixed(0)}} m  |  ~30 m COP30  |  ` +
    `${{META.z_exaggeration}}x vertical  |  contours 20 m`;
}});

// ---- contour lines ------------------------------------------------------
// The flat [u,v,u,v,...,NaN,NaN,...] payload becomes one LineSegments buffer:
// each polyline expands to its consecutive point pairs, draped on the surface.
function addContours(flat, yOffset, colorHex, opacity, lift0) {{
  const lift = zSpanTile * zEx * lift0;
  const verts = [];
  const maxRise = zSpanTile * zEx * 0.04;   // reject wild vertical jumps (icicles)
  const cellW = Math.max(W / (COLS - 1), H / (ROWS - 1));
  const segMax2 = (cellW * 3) ** 2;         // a real contour step is <~1 cell
  let prev = null;
  for (let i = 0; i < flat.length; i += 2) {{
    const u = flat[i], v = flat[i + 1];
    if (u === null || v === null || Number.isNaN(u) || !insideUV(u, v)) {{
      prev = null; continue;  // gap marker, or a point off the catchment
    }}
    const p = [ (u - 0.5) * W,
                sampleSurfaceY(u, v) + yOffset + lift,
                (v - 0.5) * H ];
    if (prev) {{
      const d2 = (p[0]-prev[0])**2 + (p[1]-prev[1])**2 + (p[2]-prev[2])**2;
      if (Math.abs(p[1] - prev[1]) < maxRise && d2 < segMax2)
        verts.push(prev[0], prev[1], prev[2], p[0], p[1], p[2]);
    }}
    prev = p;
  }}
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.Float32BufferAttribute(verts, 3));
  const mat = new THREE.LineBasicMaterial({{
    color: colorHex, transparent: true, opacity, depthWrite: false,
  }});
  // vertices already carry yOffset (via sampleSurfaceY + yOffset), mesh at 0
  scene.add(new THREE.LineSegments(geo, mat));
}}

// ---- stream polylines -----------------------------------------------------
// Heights come from the same surface sampler the terrain uses, so the tubes
// ride on the mesh rather than a separate DEM read.
function addStreams(yOffset) {{
  const lift = zSpanTile * zEx * 0.012;   // float just clear of the surface
  const mat = new THREE.MeshStandardMaterial({{
    color: 0x5cc8ff, emissive: 0x14425f, roughness: 0.3, metalness: 0.15,
  }});
  const g = new THREE.Group();
  for (const line of STREAMS) {{
    if (line.length < 2) continue;
    const pts = line.map(([u, v]) => new THREE.Vector3(
      (u - 0.5) * W,
      sampleSurfaceY(u, v) + yOffset + lift,
      (v - 0.5) * H
    ));
    const curve = new THREE.CatmullRomCurve3(pts);
    const tube = new THREE.TubeGeometry(
      curve, Math.max(8, pts.length * 3), SCALE * 0.0035, 6, false);
    g.add(new THREE.Mesh(tube, mat));
  }}
  scene.add(g);
}}

// ---- pale IGN-style ramp: cream everywhere, a wash of green low down and a
//      hint of grey rock up high. The contour lines carry the relief. -------
function hypsometricRamp() {{
  const stops = [
    [0.00, [0.86, 0.88, 0.79]],  // valley: faint green-grey
    [0.25, [0.90, 0.90, 0.82]],
    [0.55, [0.93, 0.91, 0.84]],  // mid slope: near paper
    [0.82, [0.92, 0.90, 0.86]],  // upper: barely cooler
    [1.00, [0.95, 0.95, 0.94]],  // summits: pale grey-white
  ];
  return (t) => {{
    for (let i = 1; i < stops.length; i++) {{
      if (t <= stops[i][0]) {{
        const [a, ca] = stops[i-1], [c, cc] = stops[i];
        const k = (t - a) / (c - a);
        return [0,1,2].map(j => ca[j] + (cc[j] - ca[j]) * k);
      }}
    }}
    return stops[stops.length-1][1];
  }};
}}

// ---- controls: hand-rolled orbit + wheel-zoom on the ortho camera --------
let az = Math.PI / 4;                    // azimuth
let el = Math.atan(1 / Math.SQRT2);     // elevation ~ 35.264 deg (true iso)
const ORBIT_R = SCALE * 3.0;
function applyCam() {{
  camera.position.set(
    ORBIT_R * Math.cos(el) * Math.cos(az),
    ORBIT_R * Math.sin(el),
    ORBIT_R * Math.cos(el) * Math.sin(az)
  );
  camera.up.set(0, 1, 0);
  camera.lookAt(0, 0, 0);
}}
applyCam();
{{
  let down = false, px = 0, py = 0;
  cv.addEventListener('pointerdown', e => {{ down = true; px = e.clientX; py = e.clientY; }});
  addEventListener('pointerup', () => {{ down = false; }});
  addEventListener('pointermove', e => {{
    if (!down) return;
    az -= (e.clientX - px) * 0.006;
    el = Math.max(0.12, Math.min(1.5, el + (e.clientY - py) * 0.006));
    px = e.clientX; py = e.clientY; applyCam();
  }});
  cv.addEventListener('wheel', e => {{
    e.preventDefault();
    camera.zoom = Math.max(0.35, Math.min(7, camera.zoom * (e.deltaY > 0 ? 0.9 : 1.11)));
    camera.updateProjectionMatrix();
  }}, {{ passive: false }});
}}

// ---- resize + loop -------------------------------------------------------
function resize() {{
  renderer.setSize(innerWidth, innerHeight);
  const aspect = innerWidth / innerHeight;
  camera.left = -VIEW_HALF * aspect; camera.right = VIEW_HALF * aspect;
  camera.top = VIEW_HALF; camera.bottom = -VIEW_HALF;
  camera.updateProjectionMatrix();
}}
addEventListener('resize', resize);
resize();

(function loop() {{
  requestAnimationFrame(loop);
  renderer.render(scene, camera);
}})();
</script>
"""


def build_diorama(
    catchment_polygon,      # shapely (Multi)Polygon, EPSG:4326 - the DEM-delineated catchment
    dem_tif: Path,          # the COP30 GeoTIFF covering it
    streams_fc: dict,       # GeoJSON FeatureCollection of the catchment stream network (WGS84)
    out_html: Path,
    *,
    title: str,             # e.g. "Gave de Lutour"
    subtitle: str = "catchment from COP30",
    z_exaggeration: float = 1.0,
    pad_cells: int = 6,
) -> Path:
    """Bake a catchment into a self-contained isometric 3D diorama HTML file.

    ``catchment_polygon`` is the DEM-delineated basin outline (shapely geometry,
    EPSG:4326); ``dem_tif`` the COP30 GeoTIFF covering it; ``streams_fc`` a
    GeoJSON ``FeatureCollection`` of the catchment's stream lines in WGS84 (e.g.
    :meth:`valleespyr.hydro.rivers.RiverNetwork.river_catchment_geojson`).

    The DEM is clipped to the outline (with a ``pad_cells``-wide apron), encoded
    as a 16-bit heightmap PNG, and dropped into a three.js scene alongside the
    draped stream tubes and 20 m contour lines. ``z_exaggeration`` scales the
    vertical (1.0 = true scale). Writes ``out_html`` and returns its path.
    """
    payload = build_terrain_payload(
        catchment_polygon, dem_tif, streams_fc,
        z_exaggeration=z_exaggeration, pad_cells=pad_cells,
    )
    meta = payload["meta"]

    html = HTML.format(
        meta=json.dumps(meta),
        streams=json.dumps(payload["streams"]),
        contours_minor=json.dumps(payload["contours_minor"]),
        contours_index=json.dumps(payload["contours_index"]),
        heightmap=payload["heightmap"],
        exag=meta["z_exaggeration"],
        title=title,
        subtitle=subtitle,
    )
    out_html = Path(out_html)
    out_html.write_text(html, encoding="utf-8")
    logger.info("[out] %s (%.2f MB)", out_html, len(html) / 1e6)
    return out_html
