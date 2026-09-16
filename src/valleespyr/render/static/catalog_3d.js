// 3D map column: same window._catalogInitGeo/_catalogMapSelect/_catalogGeoSync
// contract as catalog_map.js (the Leaflet version), built on three.js instead.
// The selected river gets real DEM terrain (fetched lazily from
// __TERRAIN_URL__/<river_id>.json, baked by `valley catchments
// terrain-precompute`); every other river in view is a flat footprint at its
// true position/scale, traced from its own `catchment` polygon when it has
// one. Clicking a footprint or a tree row flies the camera to the new
// river's outlet, looking upstream into its bowl.
window._catalogInitGeo = function (data, root, meta, labelsEl, esc, focusOn) {
  (function initGeo() {
    const geo = data.geo;
    const wrap = document.getElementById('map3d');
    const cv = document.getElementById('cv3d');
    if (!geo || !geo.rivers || !geo.bbox || !wrap || !cv || !window.THREE) return;
    document.body.classList.add('has-geo');

    const TERRAIN_URL = __TERRAIN_URL__;  // relative to this HTML file
    const loadingEl = document.getElementById('loading3d');
    const cap = document.getElementById('infobox');

    // tree walk, same shape catalog_map.js uses: node[id] = catalog node,
    // up[id] = every river upstream of id (its own catchment's network)
    const node = {}, up = {};
    (function w(n) {
      node[n.id] = n;
      const kids = [];
      if (n.mainline) kids.push(n.mainline);
      for (const t of n.tributaries || []) kids.push(t);
      let acc = [];
      for (const k of kids) acc = acc.concat(w(k));
      up[n.id] = acc;
      return acc.concat([n.id]);
    })(root);

    // ---- world projection: lon/lat -> local metres around a shared origin -
    const [BW, BS, BE, BN] = geo.bbox;
    const ORIGIN_LON = (BW + BE) / 2, ORIGIN_LAT = (BS + BN) / 2;
    const M_PER_DEG_LON = 111320 * Math.cos(ORIGIN_LAT * Math.PI / 180);
    const M_PER_DEG_LAT = 110540;
    function toXZ(lon, lat) {
      return [(lon - ORIGIN_LON) * M_PER_DEG_LON, (ORIGIN_LAT - lat) * M_PER_DEG_LAT];
      // z grows southward the same way catalog_map's screen convention would
      // if it were 3D: north is -z, so "up" on screen (north) reads as away
      // from camera when we later look from the south.
    }
    const worldW = (BE - BW) * M_PER_DEG_LON;
    const worldH = (BN - BS) * M_PER_DEG_LAT;
    const WORLD_SCALE = Math.max(worldW, worldH);

    // ---- scene / renderer --------------------------------------------------
    const renderer = new THREE.WebGLRenderer({ canvas: cv, antialias: true });
    renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    const PAPER = 0xeae4d6;
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(PAPER);
    scene.fog = new THREE.Fog(PAPER, WORLD_SCALE * 1.6, WORLD_SCALE * 4.5);

    let camera;
    function sizeCamera() {
      const w = wrap.clientWidth || 1, h = wrap.clientHeight || 1;
      camera = new THREE.PerspectiveCamera(45, w / h, 1, 400000);
    }
    sizeCamera();

    scene.add(new THREE.HemisphereLight(0xffffff, 0xd8d0be, 1.15));
    const sun = new THREE.DirectionalLight(0xfff6e8, 0.85);
    sun.position.set(-worldW * 0.8, worldH * 1.6, worldH * 0.7);
    scene.add(sun);

    function resize() {
      const w = wrap.clientWidth || 1, h = wrap.clientHeight || 1;
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
    }
    new ResizeObserver(resize).observe(wrap);
    resize();

    // ---- flat footprints: one per river with a `catchment` ring -----------
    // Rivers without a catchment polygon (most of the catalog - see
    // catalog.py's area-range gate) get no footprint mesh, only their
    // centreline is available; they still work as text-only selections.
    const baseGroup = new THREE.Group();
    scene.add(baseGroup);
    const footprints = {};  // id -> THREE.Mesh (for raycasting + highlight)

    function ringsToShape(rings) {
      // rings: [[ [lon,lat], ... ], ...] - first is the outer ring, rest holes
      const shape = new THREE.Shape();
      rings[0].forEach(([lon, lat], i) => {
        const [x, z] = toXZ(lon, lat);
        if (i === 0) shape.moveTo(x, z); else shape.lineTo(x, z);
      });
      for (let i = 1; i < rings.length; i++) {
        const hole = new THREE.Path();
        rings[i].forEach(([lon, lat], j) => {
          const [x, z] = toXZ(lon, lat);
          if (j === 0) hole.moveTo(x, z); else hole.lineTo(x, z);
        });
        shape.holes.push(hole);
      }
      return shape;
    }

    // A terrain block's top surface is drawn roughly centred on y=0 (each
    // block's own elevation range, exaggeration included, spans anywhere
    // from ~1500 to ~2500 world units on the catchments seen so far - see
    // buildBlock's centreY). A flat footprint plane at a small fixed y (the
    // old value here was -30) sits mid-slope inside that range, reading as a
    // slab cutting through the terrain rather than ground far below it.
    // -1500 clears every block's floor with comfortable margin while staying
    // close enough that the eye still reads it as "this valley's base".
    const GROUND_Y = -1500;

    const FOOTPRINT_COLOR = 0xdcd5c0, FOOTPRINT_HI = 0xc9dec2;
    for (const id in geo.rivers) {
      const g = geo.rivers[id];
      if (!g.catchment || !g.catchment.length) continue;
      const shp = ringsToShape(g.catchment);
      const geom2d = new THREE.ShapeGeometry(shp);
      geom2d.rotateX(-Math.PI / 2);
      const mat = new THREE.MeshBasicMaterial({
        color: FOOTPRINT_COLOR, transparent: true, opacity: 0.4, side: THREE.DoubleSide,
      });
      const mesh = new THREE.Mesh(geom2d, mat);
      mesh.position.y = GROUND_Y;
      mesh.userData.riverId = id;
      baseGroup.add(mesh);
      footprints[id] = mesh;

      const edges = new THREE.EdgesGeometry(geom2d);
      const edgeLine = new THREE.LineSegments(edges,
        new THREE.LineBasicMaterial({ color: 0xb0a488, transparent: true, opacity: 0.5 }));
      edgeLine.position.y = GROUND_Y;
      baseGroup.add(edgeLine);
    }

    // context: every river's centreline, faint - same "whole network" read
    // the 2D map gives before/around the highlighted selection
    function toVec3s(subs) {
      const out = [];
      for (const sub of subs) {
        out.push(sub.map(([lon, lat]) => {
          const [x, z] = toXZ(lon, lat);
          return new THREE.Vector3(x, GROUND_Y + 5, z);
        }));
      }
      return out;
    }
    const lineMat = new THREE.LineBasicMaterial({ color: 0x8a94a0, transparent: true, opacity: 0.5 });
    for (const id in geo.rivers) {
      for (const pts of toVec3s(geo.rivers[id].line)) {
        if (pts.length < 2) continue;
        const g = new THREE.BufferGeometry().setFromPoints(pts);
        baseGroup.add(new THREE.Line(g, lineMat));
      }
    }

    // ---- terrain blocks (lazy-fetched, cached) -----------------------------
    const terrainCache = {};   // id -> payload | 'missing' | Promise
    const builtBlocks = {};    // id -> THREE.Group already added to scene

    function hypsometricRamp() {
      const stops = [
        [0.00, [0.86, 0.88, 0.79]], [0.25, [0.90, 0.90, 0.82]],
        [0.55, [0.93, 0.91, 0.84]], [0.82, [0.92, 0.90, 0.86]],
        [1.00, [0.95, 0.95, 0.94]],
      ];
      return (t) => {
        for (let i = 1; i < stops.length; i++) {
          if (t <= stops[i][0]) {
            const [a, ca] = stops[i - 1], [c, cc] = stops[i];
            const k = (t - a) / (c - a);
            return [0, 1, 2].map(j => ca[j] + (cc[j] - ca[j]) * k);
          }
        }
        return stops[stops.length - 1][1];
      };
    }
    const RAMP = hypsometricRamp();

    function fetchTerrain(id) {
      if (terrainCache[id]) return Promise.resolve(terrainCache[id]);
      if (!TERRAIN_URL) { terrainCache[id] = 'missing'; return Promise.resolve('missing'); }
      // no-store: these files get re-baked in place during development (a
      // precompute re-run overwrites <id>.json at the same URL) and a dev
      // static server sends no cache-control headers of its own, so the
      // browser's HTTP cache can otherwise serve a stale terrain payload
      // even after a hard reload.
      return fetch(TERRAIN_URL + '/' + encodeURIComponent(id) + '.json', { cache: 'no-store' })
        .then(r => (r.ok ? r.json() : 'missing'))
        .catch(() => 'missing')
        .then(v => { terrainCache[id] = v; return v; });
    }

    function buildBlock(id, payload, originXZ) {
      if (builtBlocks[id]) return builtBlocks[id];
      const meta2 = payload.meta;
      const COLS = meta2.cols, ROWS = meta2.rows;
      const b = meta2.bounds;
      const W = (b.east - b.west) * meta2.m_per_deg_lon;
      const H = (b.north - b.south) * meta2.m_per_deg_lat;
      const zSpanTile = meta2.z_max_tile - meta2.z_min_tile;
      const zEx = meta2.z_exaggeration;

      const img = new Image();
      const basemapImg = payload.basemap ? new Image() : null;
      const group = new THREE.Group();
      group.visible = false;
      scene.add(group);
      builtBlocks[id] = group;

      function onBothLoaded() {
        const cvs = document.createElement('canvas');
        cvs.width = COLS; cvs.height = ROWS;
        const ctx = cvs.getContext('2d', { willReadFrequently: true });
        ctx.drawImage(img, 0, 0);
        const data = ctx.getImageData(0, 0, COLS, ROWS).data;

        // the basemap raster is baked onto this exact same (COLS, ROWS) grid
        // (see terrain_data.fetch_basemap_rgb) - no separate UV mapping, just
        // read it back at the same resolution as the heightmap.
        let basemapData = null;
        if (basemapImg) {
          const bcvs = document.createElement('canvas');
          bcvs.width = COLS; bcvs.height = ROWS;
          const bctx = bcvs.getContext('2d', { willReadFrequently: true });
          bctx.drawImage(basemapImg, 0, 0, COLS, ROWS);
          basemapData = bctx.getImageData(0, 0, COLS, ROWS).data;
        }

        const inside = new Uint8Array(COLS * ROWS);
        const elevY = new Float32Array(COLS * ROWS);
        const normT = new Float32Array(COLS * ROWS);
        const shadeT = new Float32Array(COLS * ROWS);  // baked IGN-style relief shade
        let minY = Infinity, maxY = -Infinity;
        for (let i = 0; i < COLS * ROWS; i++) {
          const o = i * 4;
          const ins = data[o + 3] !== 0;
          inside[i] = ins ? 1 : 0;
          const q = data[o] + (data[o + 1] << 8);
          const t = q / 65535;
          normT[i] = t;
          shadeT[i] = data[o + 2] / 255;
          const y = t * zSpanTile * zEx;
          elevY[i] = y;
          if (ins) { if (y < minY) minY = y; if (y > maxY) maxY = y; }
        }
        const baseY = minY - zSpanTile * zEx * 0.18;
        const centreY = -(maxY + baseY) / 2;

        const sampleY = (u, v) => {
          const fc = Math.min(Math.max(u, 0), 1) * (COLS - 1);
          const fr = (1 - Math.min(Math.max(v, 0), 1)) * (ROWS - 1);
          const c0 = Math.floor(fc), r0 = Math.floor(fr);
          const c1 = Math.min(c0 + 1, COLS - 1), r1 = Math.min(r0 + 1, ROWS - 1);
          const tx = fc - c0, ty = fr - r0;
          const g = (r, c) => elevY[r * COLS + c];
          return (g(r0, c0) * (1 - tx) + g(r0, c1) * tx) * (1 - ty) +
                 (g(r1, c0) * (1 - tx) + g(r1, c1) * tx) * ty;
        };
        const insideUV = (u, v) => {
          const c = Math.round(Math.min(Math.max(u, 0), 1) * (COLS - 1));
          const r = Math.round((1 - Math.min(Math.max(v, 0), 1)) * (ROWS - 1));
          return inside[r * COLS + c] === 1;
        };
        const px = (c) => (c / (COLS - 1) - 0.5) * W;
        const pz = (r) => (0.5 - r / (ROWS - 1)) * H;
        const cellInside = (c, r) => {
          if (c < 0 || r < 0 || c >= COLS - 1 || r >= ROWS - 1) return false;
          const a = r * COLS + c;
          return inside[a] && inside[a + 1] && inside[a + COLS] && inside[a + COLS + 1];
        };

        const tPos = [], tCol = [], tIdx = [];
        const topIdx = new Int32Array(COLS * ROWS).fill(-1);
        function topVert(c, r) {
          const gi = r * COLS + c;
          if (topIdx[gi] !== -1) return topIdx[gi];
          const id2 = tPos.length / 3;
          tPos.push(px(c), elevY[gi], pz(r));
          const shade = 0.45 + 0.55 * shadeT[gi];
          let col;
          if (basemapData) {
            const bo = gi * 4;
            col = [basemapData[bo] / 255, basemapData[bo + 1] / 255, basemapData[bo + 2] / 255];
          } else {
            col = RAMP(normT[gi]);
          }
          tCol.push(col[0] * shade, col[1] * shade, col[2] * shade);
          topIdx[gi] = id2;
          return id2;
        }
        for (let r = 0; r < ROWS - 1; r++) {
          for (let c = 0; c < COLS - 1; c++) {
            if (!cellInside(c, r)) continue;
            const va = topVert(c, r), vb = topVert(c + 1, r);
            const vd = topVert(c, r + 1), ve = topVert(c + 1, r + 1);
            tIdx.push(va, vd, vb, vb, vd, ve);
          }
        }
        const topGeo = new THREE.BufferGeometry();
        topGeo.setAttribute('position', new THREE.Float32BufferAttribute(tPos, 3));
        topGeo.setAttribute('color', new THREE.Float32BufferAttribute(tCol, 3));
        topGeo.setIndex(tIdx);
        topGeo.computeVertexNormals();
        const topMesh = new THREE.Mesh(topGeo, new THREE.MeshStandardMaterial({
          vertexColors: true, roughness: 0.95, metalness: 0.0, side: THREE.DoubleSide,
        }));
        topMesh.position.y = centreY;
        group.add(topMesh);

        const wPos = [], wIdx = [];
        function sv(x, y, z) { const i2 = wPos.length / 3; wPos.push(x, y, z); return i2; }
        function panel(c1, r1, c2, r2) {
          const x1 = px(c1), z1 = pz(r1), y1 = elevY[r1 * COLS + c1];
          const x2 = px(c2), z2 = pz(r2), y2 = elevY[r2 * COLS + c2];
          const a = sv(x1, y1, z1), bb = sv(x2, y2, z2);
          const dd = sv(x1, baseY, z1), e = sv(x2, baseY, z2);
          wIdx.push(a, dd, bb, bb, dd, e);
        }
        for (let r = 0; r < ROWS - 1; r++) {
          for (let c = 0; c < COLS - 1; c++) {
            if (!cellInside(c, r)) continue;
            if (!cellInside(c, r - 1)) panel(c, r, c + 1, r);
            if (!cellInside(c, r + 1)) panel(c + 1, r + 1, c, r + 1);
            if (!cellInside(c - 1, r)) panel(c, r + 1, c, r);
            if (!cellInside(c + 1, r)) panel(c + 1, r, c + 1, r + 1);
          }
        }
        const b0 = wPos.length / 3;
        wPos.push(px(0), baseY, pz(0), px(COLS - 1), baseY, pz(0),
                  px(0), baseY, pz(ROWS - 1), px(COLS - 1), baseY, pz(ROWS - 1));
        wIdx.push(b0, b0 + 1, b0 + 2, b0 + 2, b0 + 1, b0 + 3);
        const wCol = new Float32Array((wPos.length / 3) * 3);
        const edgeTop = [0.80, 0.74, 0.62], edgeBot = [0.42, 0.37, 0.30];
        for (let v = 0; v < wPos.length / 3; v++) {
          const t = Math.max(0, Math.min(1, (wPos[v * 3 + 1] - baseY) / Math.max(1, maxY - baseY)));
          for (let j = 0; j < 3; j++) wCol[v * 3 + j] = edgeBot[j] + (edgeTop[j] - edgeBot[j]) * t;
        }
        const wallGeo = new THREE.BufferGeometry();
        wallGeo.setAttribute('position', new THREE.Float32BufferAttribute(wPos, 3));
        wallGeo.setAttribute('color', new THREE.BufferAttribute(wCol, 3));
        wallGeo.setIndex(wIdx);
        const wallMesh = new THREE.Mesh(wallGeo, new THREE.MeshBasicMaterial({
          vertexColors: true, side: THREE.DoubleSide,
        }));
        wallMesh.position.y = centreY;
        group.add(wallMesh);

        const lift = zSpanTile * zEx * 0.012;
        const streamMat = new THREE.MeshStandardMaterial({
          color: 0x5cc8ff, emissive: 0x14425f, roughness: 0.3, metalness: 0.15,
        });
        for (const line of payload.streams) {
          if (line.length < 2) continue;
          const pts = line.map(([u, v]) => new THREE.Vector3(
            (u - 0.5) * W, sampleY(u, v) + centreY + lift, (v - 0.5) * H));
          const curve = new THREE.CatmullRomCurve3(pts);
          const tube = new THREE.TubeGeometry(curve, Math.max(8, pts.length * 3), WORLD_SCALE * 0.002, 6, false);
          group.add(new THREE.Mesh(tube, streamMat));
        }

        addContours(group, payload.contours_index, centreY, W, H, COLS, ROWS,
                    sampleY, insideUV, zSpanTile, zEx, 0x704a24, 0.85, 0.0026);

        group.position.set(originXZ[0], 0, originXZ[1]);
        group.visible = true;
        group.userData.ready = true;
        group.userData.terrainCentreY = centreY;
      }

      // wait for the heightmap, and the basemap too when this payload has one,
      // before building - either image can finish loading first.
      let pending = basemapImg ? 2 : 1;
      function loaded() { pending -= 1; if (pending === 0) onBothLoaded(); }
      img.onload = loaded;
      img.src = payload.heightmap;
      if (basemapImg) { basemapImg.onload = loaded; basemapImg.src = payload.basemap; }
      return group;
    }

    function addContours(group, flat, yOffset, W, H, COLS, ROWS, sampleY, insideUV,
                          zSpanTile, zEx, colorHex, opacity, lift0) {
      const lift = zSpanTile * zEx * lift0;
      const verts = [];
      const maxRise = zSpanTile * zEx * 0.04;
      const cellW = Math.max(W / (COLS - 1), H / (ROWS - 1));
      const segMax2 = (cellW * 3) ** 2;
      let prev = null;
      for (let i = 0; i < flat.length; i += 2) {
        const u = flat[i], v = flat[i + 1];
        if (u === null || v === null || Number.isNaN(u) || !insideUV(u, v)) { prev = null; continue; }
        const p = [(u - 0.5) * W, sampleY(u, v) + yOffset + lift, (v - 0.5) * H];
        if (prev) {
          const d2 = (p[0]-prev[0])**2 + (p[1]-prev[1])**2 + (p[2]-prev[2])**2;
          if (Math.abs(p[1] - prev[1]) < maxRise && d2 < segMax2)
            verts.push(prev[0], prev[1], prev[2], p[0], p[1], p[2]);
        }
        prev = p;
      }
      const geo3 = new THREE.BufferGeometry();
      geo3.setAttribute('position', new THREE.Float32BufferAttribute(verts, 3));
      const mat = new THREE.LineBasicMaterial({ color: colorHex, transparent: true, opacity, depthWrite: false });
      group.add(new THREE.LineSegments(geo3, mat));
    }

    // ---- camera fly-to -------------------------------------------------
    let camPos = new THREE.Vector3(0, WORLD_SCALE, 0);
    let camTarget = new THREE.Vector3();
    let flightFrom = { pos: camPos.clone(), target: camTarget.clone() };
    let flightTo = { pos: camPos.clone(), target: camTarget.clone() };
    let flightT = 1, flightStart = 0;
    const FLIGHT_MS = 1300;
    let orbitAz = 0, orbitEl = 0, orbitDist = 1;

    function shotFor(id, sizeHint) {
      const g = geo.rivers[id];
      const [ex, ez] = toXZ(g.outlet[0], g.outlet[1]);
      let ox = ex, oz = ez;
      if (g.catchment && g.catchment.length) {
        let sx = 0, sz = 0, n = 0;
        for (const [lon, lat] of g.catchment[0]) {
          const [x, z] = toXZ(lon, lat); sx += x; sz += z; n++;
        }
        if (n) { ox = sx / n; oz = sz / n; }
      }
      const size = sizeHint || WORLD_SCALE * 0.12;
      const dx = ox - ex, dz = oz - ez;
      const len = Math.hypot(dx, dz) || 1;
      const dirX = dx / len, dirZ = dz / len;
      const backoff = size * 0.15, height = size * 0.28;
      const pos = new THREE.Vector3(ex - dirX * backoff, height, ez - dirZ * backoff);
      const target = new THREE.Vector3(ox, height * 0.12, oz);
      return { pos, target, originXZ: [ox, oz], outletXZ: [ex, ez] };
    }

    let currentId = null;
    function flyTo(shot) {
      flightFrom.pos.copy(camPos);
      flightFrom.target.copy(camTarget);
      flightTo.pos.copy(shot.pos);
      flightTo.target.copy(shot.target);
      flightT = 0;
      flightStart = performance.now();
      orbitAz = 0; orbitEl = 0; orbitDist = 1;
    }

    // ---- selection: fetch terrain (if any), fly, paint footprints ---------
    function paintMap(id) {
      const g = geo.rivers[id];
      if (!g) return;
      currentId = id;
      for (const fid in footprints) {
        footprints[fid].material.color.setHex(fid === id ? FOOTPRINT_HI : FOOTPRINT_COLOR);
        footprints[fid].material.opacity = fid === id ? 0.0 : 0.55;
      }
      for (const bid in builtBlocks) {
        if (bid !== id) builtBlocks[bid].visible = false;
      }

      const n = node[id] || {};
      loadingEl.hidden = !TERRAIN_URL;
      if (builtBlocks[id]) { builtBlocks[id].visible = true; loadingEl.hidden = true; }

      fetchTerrain(id).then((payload) => {
        if (currentId !== id) return;  // superseded by a later click
        if (payload === 'missing' || !payload) {
          loadingEl.hidden = true;
          const shot = shotFor(id);
          flyTo(shot);
        } else {
          const b = payload.meta.bounds;
          const size = Math.max(
            (b.east - b.west) * payload.meta.m_per_deg_lon,
            (b.north - b.south) * payload.meta.m_per_deg_lat,
          );
          const shot = shotFor(id, size);
          buildBlock(id, payload, shot.originXZ);
          loadingEl.hidden = true;
          flyTo(shot);
        }
        renderInfobox(id, payload);
      });
    }

    function renderInfobox(id, payload) {
      const n = node[id] || {};
      const nUp = (up[id] || []).length;
      const stats = [];
      if (n.length_km != null) stats.push(['length', (+n.length_km) + ' km']);
      if (n.strahler) stats.push(['order', n.strahler]);
      stats.push(['upstream', nUp ? nUp + ' rivers' : 'headwater']);
      if (n.area_km2) stats.push(['area', (+n.area_km2) + ' km²']);
      if (n.pfafstetter) stats.push(['pfafstetter', esc(n.pfafstetter)]);
      const hasTerrain = payload && payload !== 'missing';
      if (hasTerrain) stats.push(['relief', payload.meta.z_min.toFixed(0) + '–' + payload.meta.z_max.toFixed(0) + ' m']);
      const statsHtml = stats.map(([k, v]) =>
        '<span class="stat"><span class="v">' + v + '</span>' +
        '<span class="k">' + k + '</span></span>').join('');
      const note = hasTerrain ? '' : '<span class="hint" style="display:block;margin-top:6px;">no 3D terrain baked for this river yet — flat footprint only</span>';
      cap.innerHTML =
        '<div class="title"><b>' + esc(n.name || id) + '</b></div>' +
        '<div class="stats">' + statsHtml + '</div>' + note;
      syncSelectedRow();
    }

    let current = null;
    function syncSelectedRow() {
      if (current) current.classList.remove('selected');
      current = null;
      if (!currentId) return;
      const li = labelsEl.querySelector('.row[data-id="' + cssEsc(currentId) + '"]');
      if (li) { li.classList.add('selected'); current = li; }
    }
    function cssEsc(s) {
      return window.CSS && CSS.escape ? CSS.escape(s) : String(s).replace(/"/g, '\\"');
    }
    window._catalogGeoSync = syncSelectedRow;
    window._catalogMapSelect = paintMap;

    // ---- input: orbit + click-to-select on a footprint ---------------------
    let dragDist = 0;
    {
      let down = false, px0 = 0, py0 = 0;
      cv.addEventListener('pointerdown', e => { down = true; px0 = e.clientX; py0 = e.clientY; dragDist = 0; });
      addEventListener('pointerup', () => { down = false; });
      addEventListener('pointermove', e => {
        if (!down) return;
        const dx = e.clientX - px0, dy = e.clientY - py0;
        dragDist += Math.abs(dx) + Math.abs(dy);
        orbitAz -= dx * 0.006;
        orbitEl = Math.max(-0.6, Math.min(1.0, orbitEl - dy * 0.006));
        px0 = e.clientX; py0 = e.clientY;
      });
      cv.addEventListener('wheel', e => {
        e.preventDefault();
        orbitDist = Math.max(0.3, Math.min(3.5, orbitDist * (e.deltaY > 0 ? 1.08 : 0.92)));
      }, { passive: false });
    }
    const raycaster = new THREE.Raycaster();
    const mouse = new THREE.Vector2();
    cv.addEventListener('click', (e) => {
      if (dragDist > 6) return;
      const rect = cv.getBoundingClientRect();
      mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
      mouse.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(mouse, camera);
      const hits = raycaster.intersectObjects(Object.values(footprints));
      if (hits.length) {
        const id = hits[0].object.userData.riverId;
        if (id && id !== currentId) focusOn(id);
      }
    });

    function easeInOut(t) { return t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2; }

    const tip = document.createElement('div');
    tip.className = 'tip3d';
    tip.textContent = 'drag to orbit · scroll to zoom';
    wrap.appendChild(tip);

    // ---- north compass: a fixed screen-space gizmo. Rather than derive the
    // screen angle algebraically from the orbit azimuth (easy to get a sign
    // wrong), project world geographic north (-z, since toXZ maps increasing
    // latitude to decreasing z) through the camera's actual view+projection
    // each frame: the on-screen direction from camTarget to one step north
    // of it gives the needle heading directly, valid at any orbit/tilt.
    //
    // Declared (and updateCompass defined) before the render loop below,
    // which calls updateCompass() on its very first frame - a const inside a
    // later-declared block would still be in its temporal dead zone then.
    const compass = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    compass.setAttribute('class', 'compass3d');
    compass.setAttribute('viewBox', '0 0 48 48');
    compass.innerHTML =
      '<circle cx="24" cy="24" r="21" fill="rgba(255,255,255,.7)" stroke="#b0a488" stroke-width="1"/>' +
      '<g id="needle3d">' +
      '<path d="M24,6 L29,26 L24,22 L19,26 Z" fill="#a5682f"/>' +
      '<path d="M24,42 L29,26 L24,30 L19,26 Z" fill="#b0a488"/>' +
      '<text x="24" y="15" text-anchor="middle" font-size="8" font-weight="700" fill="#4a4234">N</text>' +
      '</g>';
    wrap.appendChild(compass);
    const needle = compass.querySelector('#needle3d');
    const _nA = new THREE.Vector3(), _nB = new THREE.Vector3();
    function updateCompass() {
      _nA.copy(camTarget);
      _nB.copy(camTarget).add(new THREE.Vector3(0, 0, -1));  // one step toward north
      _nA.project(camera);
      _nB.project(camera);
      const dx = _nB.x - _nA.x, dy = -(_nB.y - _nA.y);  // NDC y is flipped vs screen y
      if (Math.hypot(dx, dy) < 1e-6) return;  // looking straight down/up - keep last heading
      const deg = Math.atan2(dx, dy) * 180 / Math.PI;
      needle.setAttribute('transform', 'rotate(' + deg.toFixed(1) + ' 24 24)');
    }

    (function loop() {
      requestAnimationFrame(loop);
      const now = performance.now();
      if (flightT < 1) {
        flightT = Math.min(1, (now - flightStart) / FLIGHT_MS);
        const k = easeInOut(flightT);
        camPos.lerpVectors(flightFrom.pos, flightTo.pos, k);
        camTarget.lerpVectors(flightFrom.target, flightTo.target, k);
      }
      const offset = camPos.clone().sub(camTarget);
      const baseR = Math.hypot(offset.x, offset.z);
      const baseAz = Math.atan2(offset.z, offset.x);
      const r = baseR * orbitDist;
      const az = baseAz + orbitAz;
      const y = offset.y * orbitDist + baseR * Math.sin(orbitEl) * orbitDist;
      camera.position.set(camTarget.x + r * Math.cos(az), camTarget.y + y, camTarget.z + r * Math.sin(az));
      camera.lookAt(camTarget);
      renderer.render(scene, camera);
      updateCompass();
    })();

    if (geo.rivers[meta.root_id]) focusOn(meta.root_id);
  })();
};
