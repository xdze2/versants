(function () {
  const G = __GEOM__;
  const ORDER_STYLE = __ORDER_STYLE__;
  const data = JSON.parse(document.getElementById('catalog-data').textContent);
  const root = data.root;
  const meta = data.meta || {};

  const graphSvg = document.getElementById('graph');
  const labelsEl = document.getElementById('labels');
  const metaExtra = document.getElementById('meta-extra');
  const slider = document.getElementById('order');
  const orderOut = document.getElementById('order-val');
  const filterEl = document.getElementById('filter');
  const focusBox = document.getElementById('focus');
  const crumbEl = document.getElementById('crumbs');

  // index every node by id, once
  const nodeById = {};
  (function idx(n) {
    nodeById[n.id] = n;
    if (n.mainline) idx(n.mainline);
    for (const t of n.tributaries || []) idx(t);
  })(root);

  // --- selection-relative folding state --------------------------------
  // When focus mode is on the graph folds around the selected river: the
  // spine from the root down to it always stays open; *inside* the selection's
  // own basin the order slider still applies, so focusing the trunk shows its
  // major tributaries (raise the slider to prune) while focusing a small
  // tributary shows all of it (nothing there is below threshold). Everything
  // outside the basin and off the spine folds to a +N leaf.
  let selectedId = null;
  let spine = new Set();       // ids on the root -> selection path (inclusive)
  let selBasin = new Set();    // every id inside the selection's catchment
  const spinePath = [];        // [rootNode, …, selNode] for the breadcrumb

  function recomputeSelection(id) {
    selectedId = id || null;
    spine = new Set();
    selBasin = new Set();
    spinePath.length = 0;
    if (!id || !nodeById[id]) return;
    (function find(n, trail) {
      const here = trail.concat(n);
      if (n.id === id) { here.forEach(x => spine.add(x.id)); spinePath.push(...here); return true; }
      for (const k of [n.mainline, ...(n.tributaries || [])].filter(Boolean))
        if (find(k, here)) return true;
      return false;
    })(root, []);
    (function sub(n) {
      selBasin.add(n.id);
      if (n.mainline) sub(n.mainline);
      for (const t of n.tributaries || []) sub(t);
    })(nodeById[id]);
  }

  // --- style helpers ------------------------------------------------------
  function orderStyle(order) {
    const i = !order ? 0 : Math.min(Math.max(order, 1), ORDER_STYLE.length) - 1;
    return ORDER_STYLE[i];
  }
  function laneX(lane) { return G.LANE_PAD + lane * G.LANE_W + G.LANE_W / 2; }
  function rowY(i) { return i * G.ROW_H + G.ROW_H / 2; }

  function subtreeCount(n) {
    let c = 1;
    if (n.mainline) c += subtreeCount(n.mainline);
    for (const t of n.tributaries || []) c += subtreeCount(t);
    c += n.tributaries_truncated || 0;
    return c;
  }
  function subtreeMaxOrder(n) {
    let m = n.strahler || 0;
    if (n.mainline) m = Math.max(m, subtreeMaxOrder(n.mainline));
    for (const t of n.tributaries || []) m = Math.max(m, subtreeMaxOrder(t));
    return m;
  }
  function isSource(n) {
    return !!(n.is_headwater ||
      (!(n.tributaries || []).length && !(n.tributaries_truncated || 0) && !n.mainline));
  }

  // --- per-river UI state (which sub-basins the user forced open/closed) --
  // key: river id -> true (forced open) / false (forced closed) / undefined
  const forced = new Map();

  // --- the layout walk --------------------------------------------------
  // Returns {rows, segs, maxLane}. A "row" is one visible river; "segs" are
  // vertical lane runs. minOrder gates a whole sub-basin: if its richest
  // river is below the threshold and the user has not forced it open, it
  // folds to a +N leaf on the row it joins.
  function layout(minOrder) {
    const rows = [], segs = [];
    const free = [];
    let nextLane = 0;
    const alloc = () => {
      if (free.length) { free.sort((a, b) => a - b); return free.shift(); }
      return nextLane++;
    };
    const release = (l) => free.push(l);

    const focus = focusMode && selectedId != null;

    function visibleTribs(node) {
      // visible tributaries (biggest first — input already sorted) plus the
      // count and the biggest hidden child, so a "+N" leaf can pivot into it
      const out = [];
      let folded = 0, biggestHidden = null;
      for (const t of node.tributaries || []) {
        const force = forced.get(t.id);
        let show;
        if (force === true) show = true;
        else if (force === false) show = false;
        else if (focus) {
          // spine is always open; inside the selection's basin the order
          // slider still prunes; everything else folds away
          show = spine.has(t.id) ||
            (selBasin.has(t.id) && subtreeMaxOrder(t) >= minOrder);
        } else {
          show = subtreeMaxOrder(t) >= minOrder;
        }
        if (show) out.push({ node: t });
        else {
          folded += subtreeCount(t);
          if (!biggestHidden) biggestHidden = t.id;  // list is sorted big->small
        }
      }
      folded += node.tributaries_truncated || 0;
      return { tribs: out, folded, biggestHidden };
    }

    function walk(node, lane, parentLane, depth) {
      const headRow = rows.length;
      let cur = node, curParentLane = parentLane;
      while (cur) {
        const curDepth = cur.depth != null ? cur.depth : depth;
        if (G.MAX_DEPTH != null && curDepth > G.MAX_DEPTH) {
          rows.push({ node: cur, lane, parentLane: curParentLane, depth: curDepth,
                      leaf: true, count: subtreeCount(cur), hasHidden: false });
          break;
        }
        const { tribs, folded, biggestHidden } = visibleTribs(cur);
        rows.push({ node: cur, lane, parentLane: curParentLane, depth: curDepth,
                    leaf: false, count: 0,
                    hasHidden: folded > 0, foldedCount: folded });
        curParentLane = null;  // only the branch head merges into a parent

        for (const item of tribs) {
          const tl = alloc();
          walk(item.node, tl, lane, curDepth + 1);
        }
        if (folded > 0) {
          const tl = alloc();
          rows.push({ node: { name: null, id: '+' + folded }, lane: tl,
                      parentLane: lane, depth: curDepth + 1, leaf: true,
                      count: folded, hasHidden: false,
                      foldInto: biggestHidden });
          segs.push({ lane: tl, top: rows.length - 1, bot: rows.length - 1,
                      owner: rows.length - 1 });
          release(tl);
        }
        cur = cur.mainline;
      }
      segs.push({ lane, top: headRow, bot: rows.length - 1, owner: headRow });
      release(lane);
    }

    walk(root, alloc(), null, 0);
    const maxLane = rows.reduce((m, r) => Math.max(m, r.lane), 0);
    return { rows, segs, maxLane };
  }

  // --- draw the graph SVG from a layout --------------------------------
  const FOLD_MARK = 14;  // px the "o-o-" collapsed marker overhangs its lane
  function drawGraph(lo) {
    const n = lo.rows.length;
    // widen the box if any collapsed leaf's marker would run past the last lane
    const hasFold = lo.rows.some(
      r => r.leaf && String(r.node.id || '').startsWith('+'));
    const laneW = G.LANE_PAD + (lo.maxLane + 1) * G.LANE_W +
      (hasFold ? FOLD_MARK : 0);
    const h = n * G.ROW_H;

    // vertical lane segments, higher lanes first so the trunk overpaints
    const segs = lo.segs.slice().sort((a, b) => b.lane - a.lane);
    let lines = '';
    for (const s of segs) {
      const [w, c] = orderStyle(lo.rows[s.owner].node.strahler);
      const x = laneX(s.lane);
      lines += '<line x1="' + x.toFixed(1) + '" y1="' + rowY(s.top).toFixed(1) +
               '" x2="' + x.toFixed(1) + '" y2="' + rowY(s.bot).toFixed(1) +
               '" stroke="' + c + '" stroke-width="' + w.toFixed(1) + '"/>';
    }

    let elbows = '', dots = '';
    lo.rows.forEach((r, i) => {
      const [w, c] = orderStyle(r.node.strahler);
      const cx = laneX(r.lane), cy = rowY(i);
      if (r.parentLane != null) {
        // a merge that comes OFF the parent lane just above this river's dot:
        // a single smooth cubic from a point one row up on the parent lane
        // across into the dot. No straight vertical stub running alongside the
        // parent (that read as a doubled line); the curve's control points sit
        // on the two lanes so it leaves vertical and arrives horizontal.
        const px = laneX(r.parentLane);
        const ew = Math.max(w, 1.4);
        const y0 = cy - G.ROW_H;              // start: a row up, on the parent
        elbows += '<path d="M' + px.toFixed(1) + ',' + y0.toFixed(1) +
                  ' C' + px.toFixed(1) + ',' + (y0 + G.ROW_H * 0.55).toFixed(1) +
                  ' ' + cx.toFixed(1) + ',' + (cy - G.ROW_H * 0.55).toFixed(1) +
                  ' ' + cx.toFixed(1) + ',' + cy.toFixed(1) + '" ' +
                  'stroke="' + c + '" stroke-width="' + ew.toFixed(1) + '"/>';
      }
      const collapsed = r.leaf && String(r.node.id || '').startsWith('+');
      if (collapsed) {
        // a "+N folded sub-basin" marker: two hollow dots joined by a stub,
        // trailing off toward the label — reads as a compressed "o-o-" chain
        const r1 = 2.6, gap = 6, r2 = 1.9, tail = 4;
        const x1 = cx, x2 = cx + gap;
        dots += '<circle cx="' + x1.toFixed(1) + '" cy="' + cy.toFixed(1) +
                '" r="' + r1 + '" fill="' + G.BG + '" stroke="' + c +
                '" stroke-width="1.4"/>';
        dots += '<line x1="' + (x1 + r1).toFixed(1) + '" y1="' + cy.toFixed(1) +
                '" x2="' + (x2 - r2).toFixed(1) + '" y2="' + cy.toFixed(1) +
                '" stroke="' + c + '" stroke-width="1.4"/>';
        dots += '<circle cx="' + x2.toFixed(1) + '" cy="' + cy.toFixed(1) +
                '" r="' + r2 + '" fill="' + G.BG + '" stroke="' + c +
                '" stroke-width="1.3"/>';
        dots += '<line x1="' + (x2 + r2).toFixed(1) + '" y1="' + cy.toFixed(1) +
                '" x2="' + (x2 + r2 + tail).toFixed(1) + '" y2="' + cy.toFixed(1) +
                '" stroke="' + c + '" stroke-width="1.3" stroke-dasharray="1.5 1.6"/>';
      } else if (r.leaf) {
        dots += '<circle cx="' + cx.toFixed(1) + '" cy="' + cy.toFixed(1) +
                '" r="2.3" fill="' + G.BG + '" stroke="' + c + '" stroke-width="1.5"/>';
      } else {
        dots += '<circle cx="' + cx.toFixed(1) + '" cy="' + cy.toFixed(1) +
                '" r="' + G.DOT_R.toFixed(1) + '" fill="' + c + '"/>';
      }
    });

    graphSvg.setAttribute('width', laneW);
    graphSvg.setAttribute('height', h);
    graphSvg.setAttribute('viewBox', '0 0 ' + laneW + ' ' + h);
    graphSvg.innerHTML =
      '<g stroke-linecap="round" fill="none">' + lines + elbows + '</g>' +
      '<g stroke-linecap="round">' + dots + '</g>';
    // keep the label column aligned with lane 0
    document.querySelector('.graphwrap').style.gridTemplateColumns =
      (laneW + G.LABEL_GAP) + 'px ' + G.REST_COLS;
  }

  // --- draw the label list from a layout ------------------------------
  function esc(s) {
    return String(s).replace(/[&<>"]/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }
  function alsoHtml(n) {
    const a = n.also_flows_into || [];
    if (!a.length) return '';
    const names = a.map(x => esc(x.name || x.id)).join(', ');
    return ' <span class="also" title="bifurcation">⋔ also → ' + names + '</span>';
  }

  function drawLabels(lo) {
    let out = '';
    for (const r of lo.rows) {
      const n = r.node;
      if (r.leaf && !n.name && String(n.id || '').startsWith('+')) {
        const into = r.foldInto ? ' data-fold-into="' + esc(r.foldInto) + '"' : '';
        const hint = (focusMode && r.foldInto) ? ' — click to open' : '';
        out += '<li class="row leaf"' + into + ' style="height:' + G.ROW_H + 'px">' +
               '<span class="caret" hidden></span>' +
               '<span class="name unnamed">+' + r.count + ' rivers' + hint + '</span></li>';
        continue;
      }
      const named = !!n.name;
      const cls = named ? 'name' : 'name unnamed';
      const label = esc(named ? n.name : (n.id || '?'));
      const leaf = r.leaf ? ' leaf' : '';
      const extra = r.leaf ? ' <span class="more">+' + r.count + '</span>' : '';
      const src = (isSource(n) && !r.leaf) ? ' ▲' : '';
      let caret = '<span class="caret" hidden></span>';
      if (!r.leaf && r.hasHidden) {
        caret = '<button class="caret" data-open="' +
                (forced.get(n.id) === true ? '1' : '0') + '" data-id="' +
                esc(n.id) + '" title="' + r.foldedCount +
                ' hidden upstream">▸</button>';
      } else if (!r.leaf && forced.get(n.id) === true) {
        // opened past the slider; offer to re-fold
        caret = '<button class="caret open" data-open="1" data-id="' + esc(n.id) +
                '" title="fold this sub-basin">▸</button>';
      }
      out += '<li class="row' + leaf + '" data-id="' + esc(n.id || '') +
             '" style="height:' + G.ROW_H + 'px">' + caret +
             '<span class="' + cls + '">' + label + src + '</span>' + extra +
             alsoHtml(n) + '</li>';
    }
    labelsEl.innerHTML = out;
    applyFilter();
  }

  // --- render = layout + both columns --------------------------------
  let focusMode = !!(focusBox && focusBox.checked);
  let curLayout = null;

  function drawCrumbs() {
    if (!crumbEl) return;
    if (!focusMode || !spinePath.length) { crumbEl.innerHTML = ''; return; }
    crumbEl.innerHTML = spinePath.map((n, i) => {
      const label = esc(n.name || n.id);
      const last = i === spinePath.length - 1;
      return last
        ? '<b>' + label + '</b>'
        : '<a href="#" data-id="' + esc(n.id) + '">' + label + '</a>';
    }).join('<span class="sep">›</span>');
  }

  function render() {
    const minOrder = +slider.value;
    orderOut.textContent = minOrder;
    curLayout = layout(minOrder);
    drawGraph(curLayout);
    drawLabels(curLayout);
    drawCrumbs();
    const drawn = curLayout.rows.filter(r => !r.leaf).length;
    const total = meta.n_nodes || drawn;
    let note = drawn < total
      ? ' · ' + drawn + ' of ' + total + ' rivers shown'
      : ' · ' + drawn + ' rivers';
    if (focusMode && selectedId != null) note += ' · folded to selection';
    metaExtra.textContent = note;
    if (window._catalogGeoSync) window._catalogGeoSync();
  }

  // a selection changed (from the map, a row, a +N leaf, or a crumb)
  function focusOn(id) {
    recomputeSelection(id);
    if (window._catalogMapSelect) window._catalogMapSelect(id);
    render();
  }
  window._catalogFocusOn = focusOn;

  slider.addEventListener('input', render);
  if (focusBox) focusBox.addEventListener('change', () => {
    focusMode = focusBox.checked;
    render();
  });
  if (crumbEl) crumbEl.addEventListener('click', ev => {
    const a = ev.target.closest('a[data-id]');
    if (!a) return;
    ev.preventDefault();
    focusOn(a.dataset.id);
  });
  labelsEl.addEventListener('click', ev => {
    const btn = ev.target.closest('.caret[data-id]');
    if (!btn) return;
    ev.stopPropagation();
    const id = btn.dataset.id;
    forced.set(id, btn.dataset.open === '1' ? false : true);
    render();
  });

  // --- name filter (hides rows; does not relayout the graph) ---------
  function applyFilter() {
    const t = (filterEl.value || '').trim().toLowerCase();
    for (const li of labelsEl.querySelectorAll('.row')) {
      const nameEl = li.querySelector('.name');
      const raw = nameEl ? nameEl.textContent : '';
      if (nameEl) nameEl.textContent = raw;
      if (!t) { li.classList.remove('hidden'); continue; }
      const i = raw.toLowerCase().indexOf(t);
      if (i < 0) { li.classList.add('hidden'); continue; }
      li.classList.remove('hidden');
      if (nameEl) {
        nameEl.innerHTML = esc(raw.slice(0, i)) + '<mark>' +
          esc(raw.slice(i, i + t.length)) + '</mark>' + esc(raw.slice(i + t.length));
      }
    }
  }
  filterEl.addEventListener('input', applyFilter);

  // rows drive the selection (focus refold + map). Works with or without geo.
  labelsEl.addEventListener('click', ev => {
    if (ev.target.closest('.caret')) return;
    const leaf = ev.target.closest('.row.leaf[data-fold-into]');
    if (leaf) { focusOn(leaf.dataset.foldInto); return; }
    const li = ev.target.closest('.row[data-id]');
    if (li && li.dataset.id && nodeById[li.dataset.id]) focusOn(li.dataset.id);
  });

  render();
  if (window._catalogInitGeo) window._catalogInitGeo(data, root, meta, labelsEl, esc, focusOn);
})();
