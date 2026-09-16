window._catalogInitGeo = function (data, root, meta, labelsEl, esc, focusOn, initialId) {
  (function initGeo() {
    const geo = data.geo;
    const mapEl = document.getElementById('map');
    if (!geo || !geo.rivers || !geo.bbox || !mapEl || !window.maplibregl) return;
    document.body.classList.add('has-geo');

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
    const bounds = [[BW, BS], [BE, BN]];  // MapLibre: [[west,south],[east,north]]

    // --- basemaps: a custom outdoor style (vector, needs MapLibre — raster
    // export of a custom MapTiler style is paid-tier only) with the
    // previous IGN/OSM raster choices kept as alternates. MapTiler needs a
    // free, origin-restricted API key (see catalog_html.py's
    // _maptiler_api_key/__MAPTILER_KEY__); options that need it are omitted
    // from the page entirely when no key is configured.
    const MAPTILER_KEY = __MAPTILER_KEY__;
    const CUSTOM_STYLE_ID = __MAPTILER_STYLE_ID__;

    const IGN_PLAN_SOURCE = {
      type: 'raster', tileSize: 256, maxzoom: 16,
      tiles: ['https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0' +
        '&LAYER=GEOGRAPHICALGRIDSYSTEMS.PLANIGNV2&STYLE=normal&FORMAT=image/png' +
        '&TILEMATRIXSET=PM&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}'],
      attribution: 'Plan IGN — IGN/Geoportail',
    };
    const IGN_STYLE = {
      version: 8, sources: { ignPlan: IGN_PLAN_SOURCE },
      layers: [{ id: 'ignPlan', type: 'raster', source: 'ignPlan' }],
    };
    const OSM_STYLE = MAPTILER_KEY ? {
      version: 8,
      sources: { osm: {
        type: 'raster', tileSize: 256, maxzoom: 20,
        tiles: [`https://api.maptiler.com/maps/streets-v2/{z}/{x}/{y}.png?key=${MAPTILER_KEY}`],
        attribution: '&copy; OpenStreetMap contributors &copy; <a href="https://www.maptiler.com/copyright/">MapTiler</a>',
      } },
      layers: [{ id: 'osm', type: 'raster', source: 'osm' }],
    } : null;
    // Custom MapTiler Cloud style (vector) — the actual target look; only
    // offered when both a key and a style id are configured.
    const CUSTOM_STYLE_URL = (MAPTILER_KEY && CUSTOM_STYLE_ID)
      ? `https://api.maptiler.com/maps/${CUSTOM_STYLE_ID}/style.json?key=${MAPTILER_KEY}`
      : null;

    const BASEMAPS = {
      ign: IGN_STYLE,
      osm: OSM_STYLE,
      custom: CUSTOM_STYLE_URL,
    };
    const initialBase = CUSTOM_STYLE_URL ? 'custom' : 'ign';

    const map = new maplibregl.Map({
      container: mapEl,
      style: BASEMAPS[initialBase],
      bounds, fitBoundsOptions: { padding: 14 },
      minZoom: 6, maxZoom: 17,
      attributionControl: { compact: true },
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-left');

    // key-free hillshade ("estompage"), its own tile matrix set (PM_0_15) —
    // added/removed as an overlay on top of whichever base style is active.
    const HILLSHADE_SOURCE_ID = 'hillshade-src';
    const HILLSHADE_LAYER_ID = 'hillshade-layer';
    function addHillshade() {
      if (map.getSource(HILLSHADE_SOURCE_ID)) return;
      map.addSource(HILLSHADE_SOURCE_ID, {
        type: 'raster', tileSize: 256, maxzoom: 15,
        tiles: ['https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0' +
          '&LAYER=ELEVATION.ELEVATIONGRIDCOVERAGE.SHADOW&STYLE=estompage_grayscale' +
          '&FORMAT=image/png&TILEMATRIXSET=PM_0_15&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}'],
        attribution: 'Estompage — IGN/Geoportail',
      });
      map.addLayer({ id: HILLSHADE_LAYER_ID, type: 'raster', source: HILLSHADE_SOURCE_ID, paint: { 'raster-opacity': 0.45 } });
    }
    function removeHillshade() {
      if (map.getLayer(HILLSHADE_LAYER_ID)) map.removeLayer(HILLSHADE_LAYER_ID);
      if (map.getSource(HILLSHADE_SOURCE_ID)) map.removeSource(HILLSHADE_SOURCE_ID);
    }

    function toLngLats(subs) {
      return subs.map(sub => sub.map(([lon, lat]) => [lon, lat]));
    }

    // --- river geometry as one GeoJSON source; per-river state (context /
    // upstream / selected / preview) drives paint via feature-state, since
    // MapLibre styles many features from one source rather than one object
    // per line the way Leaflet did.
    const riverIds = Object.keys(geo.rivers);
    const riverFeatures = riverIds.map(id => ({
      type: 'Feature',
      id,
      properties: { id, strahler: (node[id] || {}).strahler || 1 },
      geometry: { type: 'MultiLineString', coordinates: toLngLats(geo.rivers[id].line) },
    }));
    const riversGeoJSON = { type: 'FeatureCollection', features: riverFeatures };

    const RIVERS_SOURCE_ID = 'rivers-src';
    const RIVERS_LINE_ID = 'rivers-line';
    const RIVERS_HIT_ID = 'rivers-hit';  // wide invisible line, easier hover/click target
    const OUTLET_SOURCE_ID = 'outlet-src';
    const OUTLET_LAYER_ID = 'outlet-layer';
    const MASK_SOURCE_ID = 'mask-src';
    const MASK_LAYER_ID = 'mask-layer';

    const CTX_COLOR = '#7c8894', UP_COLOR = '#5b6b7a', SEL_COLOR = '#2f6f4f', PREVIEW_COLOR = '#a5682f';

    function addRiverLayers() {
      if (!map.getSource(RIVERS_SOURCE_ID)) {
        map.addSource(RIVERS_SOURCE_ID, { type: 'geojson', data: riversGeoJSON, promoteId: 'id' });
      }
      if (!map.getLayer(RIVERS_LINE_ID)) {
        map.addLayer({
          id: RIVERS_LINE_ID, type: 'line', source: RIVERS_SOURCE_ID,
          layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: {
            'line-color': [
              'case',
              ['boolean', ['feature-state', 'selected'], false], SEL_COLOR,
              ['boolean', ['feature-state', 'preview'], false], PREVIEW_COLOR,
              ['boolean', ['feature-state', 'upstream'], false], UP_COLOR,
              CTX_COLOR,
            ],
            'line-opacity': [
              'case',
              ['boolean', ['feature-state', 'selected'], false], 1,
              ['boolean', ['feature-state', 'preview'], false], 0.95,
              ['boolean', ['feature-state', 'upstream'], false], 0.85,
              0.55,
            ],
            'line-width': [
              'case',
              ['boolean', ['feature-state', 'selected'], false], ['*', mapWidthExpr(), 1.7],
              ['boolean', ['feature-state', 'preview'], false], ['*', mapWidthExpr(), 1.5],
              ['boolean', ['feature-state', 'upstream'], false], ['*', mapWidthExpr(), 1.25],
              mapWidthExpr(),
            ],
          },
        });
      }
      if (!map.getLayer(RIVERS_HIT_ID)) {
        map.addLayer({
          id: RIVERS_HIT_ID, type: 'line', source: RIVERS_SOURCE_ID,
          layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: { 'line-color': '#000', 'line-opacity': 0, 'line-width': 14 },
        });
      }
    }
    // Line weight scales with Strahler order, same idea as the git-graph
    // lanes but a wider spread so a trunk reads clearly against its
    // headwaters — as a MapLibre expression over the 'strahler' property
    // (rather than a plain JS function) so it can live inside a
    // data-driven 'line-width' paint spec.
    function mapWidthExpr() {
      return [
        'let', 'o', ['min', ['max', ['coalesce', ['get', 'strahler'], 1], 1], 7],
        ['+', 1.1, ['*', ['-', ['var', 'o'], 1], 0.7]],
      ];
    }
    // boost>1's Math.max(w, 2.2) floor doesn't translate cleanly into the
    // multiplicative expression above; applied as a floor on selected/
    // preview/upstream widths after the fact via a wrapping 'max'.
    function applyWidthFloor() {
      const base = mapWidthExpr();
      map.setPaintProperty(RIVERS_LINE_ID, 'line-width', [
        'case',
        ['boolean', ['feature-state', 'selected'], false], ['max', ['*', base, 1.7], 2.2],
        ['boolean', ['feature-state', 'preview'], false], ['max', ['*', base, 1.5], 2.2],
        ['boolean', ['feature-state', 'upstream'], false], ['max', ['*', base, 1.25], 2.2],
        base,
      ]);
    }

    function addOutletLayer() {
      if (!map.getSource(OUTLET_SOURCE_ID)) {
        map.addSource(OUTLET_SOURCE_ID, { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
      }
      if (!map.getLayer(OUTLET_LAYER_ID)) {
        map.addLayer({
          id: OUTLET_LAYER_ID, type: 'circle', source: OUTLET_SOURCE_ID,
          paint: {
            'circle-radius': 4.5, 'circle-color': SEL_COLOR,
            'circle-stroke-color': '#fff', 'circle-stroke-width': 1,
          },
        });
      }
    }

    // valley mask: world rectangle with the catchment ring(s) as holes
    // (even-odd fill), sitting between the basemap and the river lines.
    const WORLD_RING = [[-180, -89], [180, -89], [180, 89], [-180, 89], [-180, -89]];
    function addMaskLayer() {
      if (!map.getSource(MASK_SOURCE_ID)) {
        map.addSource(MASK_SOURCE_ID, { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
      }
      if (!map.getLayer(MASK_LAYER_ID)) {
        map.addLayer({
          id: MASK_LAYER_ID, type: 'fill', source: MASK_SOURCE_ID,
          paint: { 'fill-color': '#ffffff', 'fill-opacity': 0.75 },
        }, RIVERS_LINE_ID);  // insert below river lines, above the basemap
        map.addLayer({
          id: MASK_LAYER_ID + '-outline', type: 'line', source: MASK_SOURCE_ID,
          paint: { 'line-color': '#2f6f4f', 'line-width': 1.5, 'line-opacity': 0.6 },
        }, RIVERS_LINE_ID);
      }
    }

    function setMask(id) {
      const catchment = (geo.rivers[id] || {}).catchment;
      const src = map.getSource(MASK_SOURCE_ID);
      if (!src) return;
      if (!catchment || !catchment.length) {
        src.setData({ type: 'FeatureCollection', features: [] });
        return;
      }
      const holes = catchment.map(ring => ring.map(([lon, lat]) => [lon, lat]));
      src.setData({
        type: 'FeatureCollection',
        features: [{
          type: 'Feature', properties: {},
          geometry: { type: 'Polygon', coordinates: [WORLD_RING, ...holes] },
        }],
      });
    }

    function addAllLayers() {
      addRiverLayers();
      applyWidthFloor();
      addOutletLayer();
      addMaskLayer();
    }

    // river layers/sources live on the vector-tile style object and are
    // wiped on every style swap (basemap radio, or loading the custom
    // style initially) — re-add them on every 'styledata' event (MapLibre's
    // 'style.load' only ever fires once, for the very first style, despite
    // the name suggesting otherwise — a subsequent setStyle() call fires
    // 'styledata' instead, and isStyleLoaded() is unreliable at that point,
    // so this doesn't gate on it). 'styledata' can fire more than once for
    // the same swap; every add*Layer() here is itself idempotent (guarded
    // on getSource()/getLayer()), so re-running this a few extra times is
    // harmless, and simpler than trying to de-duplicate the event.
    function onStyleReady() {
      addAllLayers();
      if (currentId) paintMap(currentId, { flyTo: false });
      if (shadeBox && shadeBox.checked) addHillshade();
    }
    map.on('styledata', onStyleReady);

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
        for (const sub of g.line) for (const [lon, lat] of sub) pts.push([lon, lat]);
      }
      if (!pts.length) return null;
      let w = pts[0][0], s = pts[0][1], e = pts[0][0], n = pts[0][1];
      for (const [lon, lat] of pts) {
        w = Math.min(w, lon); e = Math.max(e, lon);
        s = Math.min(s, lat); n = Math.max(n, lat);
      }
      return [[w, s], [e, n]];
    }

    let statefulIds = [];  // ids currently carrying a non-default feature-state, to clear cheaply
    function clearRiverState() {
      for (const id of statefulIds) {
        map.setFeatureState({ source: RIVERS_SOURCE_ID, id }, { selected: false, upstream: false });
      }
      statefulIds = [];
    }

    // paint the map for a selection; the graph refold is driven separately
    // by the outer focusOn(), which calls this.
    function paintMap(id, opts) {
      const g = geo.rivers[id];
      if (!g || !map.getSource(RIVERS_SOURCE_ID)) return;
      currentId = id;
      clearRiverState();
      setMask(id);
      for (const uid of up[id] || []) {
        if (!geo.rivers[uid]) continue;
        map.setFeatureState({ source: RIVERS_SOURCE_ID, id: uid }, { upstream: true });
        statefulIds.push(uid);
      }
      map.setFeatureState({ source: RIVERS_SOURCE_ID, id }, { selected: true });
      statefulIds.push(id);

      const outletSrc = map.getSource(OUTLET_SOURCE_ID);
      if (outletSrc) {
        outletSrc.setData({
          type: 'FeatureCollection',
          features: [{ type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates: [g.outlet[0], g.outlet[1]] } }],
        });
      }

      if (!opts || opts.flyTo !== false) {
        const box = boundsOf([id].concat(up[id] || []));
        if (box) map.fitBounds(box, { padding: 34, duration: 400, maxZoom: 15 });
      }

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
        setMask(null);
        map.fitBounds(bounds, { padding: 14, duration: 400 });
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
      if (previewId && previewId !== currentId) {
        map.setFeatureState({ source: RIVERS_SOURCE_ID, id: previewId }, { preview: false });
      }
      previewId = id;
      if (!id || id === currentId || !map.getSource(RIVERS_SOURCE_ID)) return;
      if (!geo.rivers[id]) return;
      map.setFeatureState({ source: RIVERS_SOURCE_ID, id }, { preview: true });
    }
    window._catalogMapPreview = paintPreview;

    // --- map -> tree hover/click, via the wide invisible hit layer -------
    let hoveredId = null;
    map.on('mousemove', RIVERS_HIT_ID, e => {
      if (!e.features || !e.features.length) return;
      const id = e.features[0].properties.id;
      if (id === hoveredId) return;
      hoveredId = id;
      mapEl.style.cursor = 'pointer';
      if (window._catalogRowPreview) window._catalogRowPreview(id);
    });
    map.on('mouseleave', RIVERS_HIT_ID, () => {
      hoveredId = null;
      mapEl.style.cursor = '';
      if (window._catalogRowPreview) window._catalogRowPreview(null);
    });
    map.on('click', RIVERS_HIT_ID, e => {
      if (!e.features || !e.features.length) return;
      focusOn(e.features[0].properties.id);
    });

    // --- layer switcher UI: basemap radios + hillshade toggle -----------
    const baseRadios = document.querySelectorAll('input[name="maplayer"]');
    baseRadios.forEach(r => r.addEventListener('change', () => {
      const style = BASEMAPS[r.value];
      if (!style) return;
      map.setStyle(style);  // river/mask/outlet layers re-added on 'style.load'
    }));
    const shadeBox = document.getElementById('maphillshade');
    if (shadeBox) shadeBox.addEventListener('change', () => {
      if (shadeBox.checked) addHillshade(); else removeHillshade();
    });

    map.on('load', () => {
      const startId = (initialId && geo.rivers[initialId]) ? initialId : meta.root_id;
      if (geo.rivers[startId]) focusOn(startId, { pushHash: false });
    });
  })();
};
