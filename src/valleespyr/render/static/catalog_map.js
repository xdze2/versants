window._catalogInitGeo = function (data, root, meta, labelsEl, esc, focusOn, initialId) {
  (function initGeo() {
    const geo = data.geo;
    const mapEl = document.getElementById('map');
    if (!geo || !geo.rivers || !geo.bbox || !mapEl || !window.L) return;
    document.body.classList.add('has-geo');

    // line weight scales with Strahler order, same idea as the git-graph
    // lanes but a wider spread so a trunk reads clearly against its
    // headwaters. `boost` fattens the picked-out selection over the faint ctx.
    function mapWidth(order, boost) {
      const o = Math.min(Math.max(order || 1, 1), 7);
      const w = (1.1 + (o - 1) * 0.7) * (boost || 1);
      return boost && boost > 1 ? Math.max(w, 2.2) : w;
    }

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

    const [BW, BS, BE, BN] = geo.bbox;
    const bounds = L.latLngBounds([BS, BW], [BN, BE]);

    const map = L.map(mapEl, {
      zoomControl: true, attributionControl: true, minZoom: 6, maxZoom: 17,
    });
    map.fitBounds(bounds, { padding: [14, 14] });

    // --- basemaps: IGN topo (key-free Plan IGN) and OSM, radio-selected ---
    const ignPlan = L.tileLayer(
      'https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0' +
      '&LAYER=GEOGRAPHICALGRIDSYSTEMS.PLANIGNV2&STYLE=normal&FORMAT=image/png' +
      '&TILEMATRIXSET=PM&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}',
      { maxNativeZoom: 16, maxZoom: 17, attribution: 'Plan IGN — IGN/Geoportail' });
    const osm = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxNativeZoom: 19, maxZoom: 19, attribution: '&copy; OpenStreetMap contributors',
    });
    // key-free hillshade ("estompage"), its own tile matrix set (PM_0_15).
    const hillshade = L.tileLayer(
      'https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0' +
      '&LAYER=ELEVATION.ELEVATIONGRIDCOVERAGE.SHADOW&STYLE=estompage_grayscale' +
      '&FORMAT=image/png&TILEMATRIXSET=PM_0_15&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}',
      { maxNativeZoom: 15, maxZoom: 17, opacity: 0.45, attribution: 'Estompage — IGN/Geoportail' });

    ignPlan.addTo(map);

    // river network panes, drawn above the basemap; the valley mask sits
    // between the basemap and the river lines, so a masked selection still
    // shows its highlighted network crisply on top of the dimmed backdrop.
    const maskPane = map.createPane('valleyMask');
    maskPane.style.zIndex = 350;  // above tiles (200), below overlayPane (400)
    let maskLayer = null;
    const ctxPane = L.featureGroup().addTo(map);
    const hiPane = L.featureGroup().addTo(map);
    const previewPane = L.featureGroup().addTo(map);

    function toLatLngs(subs) {
      return subs.map(sub => sub.map(([lon, lat]) => [lat, lon]));
    }

    const ctxLines = {};  // id -> Leaflet polyline, for hover restyle + hover-out map->tree
    for (const id in geo.rivers) {
      const line = L.polyline(toLatLngs(geo.rivers[id].line), {
        color: '#7c8894', weight: mapWidth((node[id] || {}).strahler),
        opacity: 0.55, lineCap: 'round', lineJoin: 'round',
      }).addTo(ctxPane);
      ctxLines[id] = line;
      line.on('mouseover', () => { if (window._catalogRowPreview) window._catalogRowPreview(id); });
      line.on('mouseout', () => { if (window._catalogRowPreview) window._catalogRowPreview(null); });
      line.on('click', () => focusOn(id));
    }

    const cap = document.getElementById('infobox');
    let current = null, currentId = null;
    function cssEsc(s) {
      return window.CSS && CSS.escape ? CSS.escape(s) : String(s).replace(/"/g, '\\"');
    }
    function boundsOf(ids) {
      const pts = [];
      for (const id of ids) {
        const g = geo.rivers[id];
        if (!g) continue;
        for (const sub of g.line) for (const [lon, lat] of sub) pts.push([lat, lon]);
      }
      return pts.length ? L.latLngBounds(pts) : null;
    }

    // A river whose own catchment reads as "one valley" (see _VALLEY_AREA_*
    // in catalog.py) ships a `catchment` ring: draw everything outside it
    // dimmed, so that valley reads as its own bounded world rather than a
    // flat, undifferentiated stretch of the bird's-eye basemap. A river
    // outside that size range has no ring — falls back to the plain
    // full-catchment-context view, no mask.
    const WORLD_RING = [[-89, -180], [-89, 180], [89, 180], [89, -180]];
    function paintMask(id) {
      if (maskLayer) { map.removeLayer(maskLayer); maskLayer = null; }
      const catchment = (geo.rivers[id] || {}).catchment;
      if (!catchment || !catchment.length) return;
      const holes = catchment.map(ring => ring.map(([lon, lat]) => [lat, lon]));
      maskLayer = L.polygon([WORLD_RING, ...holes], {
        pane: 'valleyMask', stroke: true, color: '#2f6f4f', weight: 1.5,
        opacity: 0.6, fill: true, fillColor: '#ffffff', fillOpacity: 0.75,
        fillRule: 'evenodd', interactive: false,
      }).addTo(map);
    }

    // paint the map for a selection; the graph refold is driven separately
    // by the outer focusOn(), which calls this.
    function paintMap(id) {
      const g = geo.rivers[id];
      if (!g) return;
      currentId = id;
      hiPane.clearLayers();
      paintMask(id);
      for (const uid of up[id] || []) {
        const ug = geo.rivers[uid];
        if (!ug) continue;
        L.polyline(toLatLngs(ug.line), {
          color: '#5b6b7a', weight: mapWidth((node[uid] || {}).strahler, 1.25),
          opacity: 0.85, lineCap: 'round', lineJoin: 'round',
        }).addTo(hiPane);
      }
      L.polyline(toLatLngs(g.line), {
        color: '#2f6f4f', weight: mapWidth((node[id] || {}).strahler, 1.7),
        opacity: 1, lineCap: 'round', lineJoin: 'round',
      }).addTo(hiPane);
      L.circleMarker([g.outlet[1], g.outlet[0]], {
        radius: 4.5, color: '#fff', weight: 1, fillColor: '#2f6f4f', fillOpacity: 1,
      }).addTo(hiPane);

      const box = boundsOf([id].concat(up[id] || []));
      if (box) map.flyToBounds(box, { padding: [34, 34], duration: 0.4, maxZoom: 15 });

      const n = node[id] || {};
      const nUp = (up[id] || []).length;
      const stats = [];
      if (n.length_km != null) stats.push(['length', (+n.length_km) + ' km']);
      if (n.strahler) stats.push(['order', n.strahler]);
      stats.push(['upstream', nUp ? nUp + ' rivers' : 'headwater']);
      if (n.area_km2) stats.push(['area', (+n.area_km2) + ' km²']);
      if (n.pfafstetter) stats.push(['pfafstetter', esc(n.pfafstetter)]);
      const statsHtml = stats.map(([k, v]) =>
        '<span class="stat"><span class="v">' + v + '</span>' +
        '<span class="k">' + k + '</span></span>').join('');
      const a = n.also_flows_into || [];
      const alsoHtml = a.length
        ? '<span class="also" title="bifurcation">⋔ also flows into ' +
          a.map(x => esc(x.name || x.id)).join(', ') + '</span>'
        : '';
      cap.innerHTML =
        '<div class="title"><b>' + esc(n.name || id) +
        '</b><a href="#" id="mapreset">⤢ whole catchment</a></div>' +
        '<div class="stats">' + statsHtml + '</div>' + alsoHtml;
      document.getElementById('mapreset').addEventListener('click', ev => {
        ev.preventDefault();
        if (maskLayer) { map.removeLayer(maskLayer); maskLayer = null; }
        map.flyToBounds(bounds, { padding: [14, 14], duration: 0.4 });
      });
      syncSelectedRow();
    }
    function syncSelectedRow() {
      if (current) current.classList.remove('selected');
      current = null;
      if (!currentId) return;
      const li = labelsEl.querySelector('.row[data-id="' + cssEsc(currentId) + '"]');
      if (li) { li.classList.add('selected'); current = li; }
    }
    window._catalogGeoSync = syncSelectedRow;
    window._catalogMapSelect = paintMap;

    // --- hover preview: highlight a river's line, no camera/selection change
    let previewId = null;
    function paintPreview(id) {
      if (previewId === id) return;
      previewId = id;
      previewPane.clearLayers();
      if (!id || id === currentId) return;
      const g = geo.rivers[id];
      if (!g) return;
      L.polyline(toLatLngs(g.line), {
        color: '#a5682f', weight: mapWidth((node[id] || {}).strahler, 1.5),
        opacity: 0.95, lineCap: 'round', lineJoin: 'round', interactive: false,
      }).addTo(previewPane);
    }
    window._catalogMapPreview = paintPreview;

    // --- layer switcher UI: basemap radios + hillshade toggle -----------
    const baseRadios = document.querySelectorAll('input[name="maplayer"]');
    baseRadios.forEach(r => r.addEventListener('change', () => {
      map.removeLayer(ignPlan); map.removeLayer(osm);
      (r.value === 'osm' ? osm : ignPlan).addTo(map);
      ctxPane.bringToFront(); hiPane.bringToFront();
    }));
    const shadeBox = document.getElementById('maphillshade');
    if (shadeBox) shadeBox.addEventListener('change', () => {
      if (shadeBox.checked) { hillshade.addTo(map); ctxPane.bringToFront(); hiPane.bringToFront(); }
      else map.removeLayer(hillshade);
    });

    const startId = (initialId && geo.rivers[initialId]) ? initialId : meta.root_id;
    if (geo.rivers[startId]) focusOn(startId, { pushHash: false });
  })();
};
