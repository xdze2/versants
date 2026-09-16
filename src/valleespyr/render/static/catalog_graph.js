(function () {
  const G = __GEOM__;
  const ORDER_STYLE = __ORDER_STYLE__;
  const data = JSON.parse(document.getElementById('catalog-data').textContent);
  const root = data.root;
  const meta = data.meta || {};

  const graphSvg = document.getElementById('graph');
  const labelsEl = document.getElementById('labels');
  const metaExtra = document.getElementById('meta-extra');
  const filterEl = document.getElementById('filter');
  const crumbEl = document.getElementById('crumbs');

  // index every node by id, and record each node's parent + ordered
  // siblings (parent's [mainline, ...tributaries] list), once.
  const nodeById = {};
  const parentOf = {};
  const siblingsOf = {};  // id -> ordered list of sibling ids (incl. self)
  (function idx(n, parent) {
    nodeById[n.id] = n;
    if (parent) parentOf[n.id] = parent.id;
    const kids = [n.mainline, ...(n.tributaries || [])].filter(Boolean);
    const kidIds = kids.map(k => k.id);
    for (const k of kids) siblingsOf[k.id] = kidIds;
    for (const k of kids) idx(k, n);
  })(root, null);

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
  function label(n) {
    return n.name ? esc(n.name) : esc(n.id || '?');
  }
  function orderStyle(order) {
    const i = !order ? 0 : Math.min(Math.max(order, 1), ORDER_STYLE.length) - 1;
    return ORDER_STYLE[i];
  }

  // --- selection & breadcrumb -------------------------------------------
  let selectedId = null;
  const spinePath = [];  // [rootNode, …, selNode] for the breadcrumb

  function recomputeSpine(id) {
    spinePath.length = 0;
    let n = nodeById[id];
    const trail = [];
    while (n) { trail.push(n); n = parentOf[n.id] ? nodeById[parentOf[n.id]] : null; }
    trail.reverse();
    spinePath.push(...trail);
  }

  function drawCrumbs() {
    if (!crumbEl) return;
    if (!spinePath.length) { crumbEl.innerHTML = ''; return; }
    crumbEl.innerHTML = spinePath.map((n, i) => {
      const last = i === spinePath.length - 1;
      return last ? '<b>' + label(n) + '</b>'
                   : '<a href="#" data-id="' + esc(n.id) + '">' + label(n) + '</a>';
    }).join('<span class="sep">›</span>');
  }

  // --- the local view: one row per line, in draw order --------------------
  // Every row is either on the TRUNK (lane 0 — the go-back parent, the
  // selected river, its mainline child: one continuous vertical line down
  // the rows that carry `trunk: true`) or a BRANCH (lane 1 — a sibling or a
  // tributary, drawn as a short "o--" stub off the trunk at its own row,
  // regardless of how many other branches there are). A "+N more" row is
  // just another branch row, not a special case.
  function buildRows() {
    const sel = nodeById[selectedId] || root;
    selectedId = sel.id;
    recomputeSpine(sel.id);

    const parentId = parentOf[sel.id];
    const parent = parentId ? nodeById[parentId] : null;
    const sibs = siblingsOf[sel.id] || [sel.id];
    const idx = sibs.indexOf(sel.id);

    const rows = [];  // {kind, node, trunk, selected, moreCount}

    if (parent) rows.push({ kind: 'back', node: parent, trunk: true });

    // The "+N more" row jumps straight to the *farthest* hidden sibling in
    // that direction — one click surfaces a fresh neighbourhood instead of
    // stepping one row at a time through a long list.
    const hiddenBefore = idx;  // siblings before `prev` that aren't shown
    if (idx > 1) rows.push({ kind: 'more', node: nodeById[sibs[0]], trunk: false, moreCount: hiddenBefore - 1 });
    if (idx > 0) rows.push({ kind: 'sibling', node: nodeById[sibs[idx - 1]], trunk: false });

    rows.push({ kind: 'selected', node: sel, trunk: true, selected: true });

    const kids = [sel.mainline, ...(sel.tributaries || [])].filter(Boolean);
    kids.forEach((k, i) => {
      const isMainline = i === 0 && k === sel.mainline;
      rows.push({ kind: 'sub', node: k, trunk: isMainline });
    });

    const hiddenAfter = idx >= 0 ? sibs.length - 1 - idx : 0;
    if (idx >= 0 && idx < sibs.length - 1) rows.push({ kind: 'sibling', node: nodeById[sibs[idx + 1]], trunk: false });
    if (idx >= 0 && hiddenAfter > 1) {
      rows.push({ kind: 'more', node: nodeById[sibs[sibs.length - 1]], trunk: false, moreCount: hiddenAfter - 1 });
    }

    return rows;
  }

  function rowHtml(r) {
    if (r.kind === 'more') {
      const id = r.node ? esc(r.node.id) : '';
      const target = r.node ? esc(label(r.node)) : '';
      return '<li class="row more-sibs" data-id="' + id + '" title="jump to ' + target + '">' +
             '<span class="name">+' + r.moreCount + ' more sibling' + (r.moreCount === 1 ? '' : 's') +
             ' — click to jump to ' + target + '</span></li>';
    }
    if (r.kind === 'back') {
      return '<li class="row back" data-id="' + esc(r.node.id) + '">' +
             '<span class="arrow">^</span> <span class="name">' + label(r.node) +
             '</span> <span class="hint">downstream — go back</span></li>';
    }
    const cls = r.kind === 'selected' ? ' selected-river'
              : r.kind === 'sibling' ? ' sibling'
              : r.trunk ? ' sub' : ' sub branch';
    const n = r.node;
    const nTrib = r.kind === 'sub' ? (n.n_tributaries || 0) : 0;
    const extra = nTrib ? ' <span class="more">+' + nTrib + '</span>' : '';
    return '<li class="row' + cls + '" data-id="' + esc(n.id) + '">' +
           '<span class="name">' + label(n) + '</span>' + extra + alsoHtml(n) + '</li>';
  }

  // --- draw the graph SVG --------------------------------------------------
  // lane 0 = trunk, lane 1 = branch stub. Width never depends on how many
  // branch rows there are — N tributaries cost one extra column, not N.
  function laneX(lane) { return G.LANE_PAD + lane * G.LANE_W + G.LANE_W / 2; }
  function rowY(i) { return i * G.ROW_H + G.ROW_H / 2; }
  const TRUNK_LANE = 0, BRANCH_LANE = 1;

  function drawGraph(rows) {
    const w = G.LANE_PAD + (BRANCH_LANE + 1) * G.LANE_W;
    const h = rows.length * G.ROW_H;

    let trunkTop = null, trunkBot = null;
    rows.forEach((r, i) => { if (r.trunk) { if (trunkTop == null) trunkTop = i; trunkBot = i; } });

    let lines = '', dots = '';
    if (trunkTop != null) {
      const x = laneX(TRUNK_LANE);
      lines += '<line x1="' + x.toFixed(1) + '" y1="' + rowY(trunkTop).toFixed(1) +
               '" x2="' + x.toFixed(1) + '" y2="' + rowY(trunkBot).toFixed(1) +
               '" stroke="' + orderStyle(0)[1] + '" stroke-width="2.2"/>';
    }

    rows.forEach((r, i) => {
      const cy = rowY(i);
      if (r.trunk) {
        const [, c] = orderStyle(r.node && r.node.strahler);
        const cx = laneX(TRUNK_LANE);
        if (r.selected) {
          dots += '<circle cx="' + cx.toFixed(1) + '" cy="' + cy.toFixed(1) +
                  '" r="' + G.DOT_R.toFixed(1) + '" fill="' + c + '"/>';
        } else {
          dots += '<circle cx="' + cx.toFixed(1) + '" cy="' + cy.toFixed(1) +
                  '" r="2.3" fill="' + G.BG + '" stroke="' + c + '" stroke-width="1.5"/>';
        }
        return;
      }
      // every branch row — including "+N more" — is a stub off the trunk,
      // always connected at this row's y, whether or not the trunk line
      // itself reaches this far (a leading/trailing branch elbows off the
      // nearest trunk dot instead of floating disconnected).
      const x0 = laneX(TRUNK_LANE), x1 = laneX(BRANCH_LANE);
      const anchorRow = trunkTop == null ? i
        : i < trunkTop ? trunkTop : i > trunkBot ? trunkBot : i;
      const y0 = rowY(anchorRow);
      const isMore = r.kind === 'more';
      const [dw, c] = isMore ? orderStyle(0) : orderStyle(r.node && r.node.strahler);
      const sw = Math.max(dw, 1.4);
      if (anchorRow === i) {
        lines += '<line x1="' + x0.toFixed(1) + '" y1="' + cy.toFixed(1) +
                 '" x2="' + x1.toFixed(1) + '" y2="' + cy.toFixed(1) +
                 '" stroke="' + c + '" stroke-width="' + sw.toFixed(1) + '"' +
                 (isMore ? ' stroke-dasharray="1.6 2"' : '') + '/>';
      } else {
        // elbow: down/up from the trunk's end, then across
        lines += '<path d="M' + x0.toFixed(1) + ',' + y0.toFixed(1) +
                 ' L' + x0.toFixed(1) + ',' + cy.toFixed(1) +
                 ' L' + x1.toFixed(1) + ',' + cy.toFixed(1) + '" fill="none" ' +
                 'stroke="' + c + '" stroke-width="' + sw.toFixed(1) + '"' +
                 (isMore ? ' stroke-dasharray="1.6 2"' : '') + '/>';
      }
      if (!isMore) {
        dots += '<circle cx="' + x1.toFixed(1) + '" cy="' + cy.toFixed(1) +
                '" r="2.3" fill="' + G.BG + '" stroke="' + c + '" stroke-width="1.5"/>';
      }
    });

    graphSvg.setAttribute('width', w);
    graphSvg.setAttribute('height', h);
    graphSvg.setAttribute('viewBox', '0 0 ' + w + ' ' + h);
    graphSvg.innerHTML =
      '<g stroke-linecap="round" stroke-linejoin="round" fill="none">' + lines + '</g>' +
      '<g stroke-linecap="round">' + dots + '</g>';
  }

  // --- render = rows -> labels + graph ------------------------------------
  function render() {
    const rows = buildRows();
    labelsEl.innerHTML = rows.map(rowHtml).join('');
    drawGraph(rows);
    drawCrumbs();
    applyFilter();

    const sel = nodeById[selectedId] || root;
    let note = '';
    if (sel.n_upstream) note = ' · ' + sel.n_upstream + ' rivers upstream';
    metaExtra.textContent = note;
    if (window._catalogGeoSync) window._catalogGeoSync();
  }

  // a selection changed (from the map, a row, or a crumb)
  function focusOn(id) {
    if (!nodeById[id]) return;
    selectedId = id;
    if (window._catalogMapSelect) window._catalogMapSelect(id);
    render();
  }
  window._catalogFocusOn = focusOn;

  if (crumbEl) crumbEl.addEventListener('click', ev => {
    const a = ev.target.closest('a[data-id]');
    if (!a) return;
    ev.preventDefault();
    focusOn(a.dataset.id);
  });

  labelsEl.addEventListener('click', ev => {
    const li = ev.target.closest('.row[data-id]');
    if (li && li.dataset.id && nodeById[li.dataset.id]) focusOn(li.dataset.id);
  });

  // --- name filter (hides rows) ------------------------------------------
  function applyFilter() {
    const t = (filterEl.value || '').trim().toLowerCase();
    for (const li of labelsEl.querySelectorAll('.row[data-id]')) {
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

  render();
  if (window._catalogInitGeo) window._catalogInitGeo(data, root, meta, labelsEl, esc, focusOn);
})();
