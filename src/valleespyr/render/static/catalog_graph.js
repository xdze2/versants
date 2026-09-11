(function () {
  const data = JSON.parse(document.getElementById('catalog-data').textContent);
  const root = data.root;
  const meta = data.meta || {};

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

  // --- the two-level local view ------------------------------------------
  function rowHtml(n, cls, extraLabel) {
    const extra = extraLabel != null ? ' <span class="more">+' + extraLabel + '</span>' : '';
    return '<li class="row' + cls + '" data-id="' + esc(n.id) + '">' +
           '<span class="name">' + label(n) + '</span>' + extra + alsoHtml(n) + '</li>';
  }

  function render() {
    const sel = nodeById[selectedId] || root;
    selectedId = sel.id;
    recomputeSpine(sel.id);

    const parentId = parentOf[sel.id];
    const parent = parentId ? nodeById[parentId] : null;
    const sibs = siblingsOf[sel.id] || [sel.id];
    const idx = sibs.indexOf(sel.id);

    let out = '';

    if (parent) {
      out += '<li class="row back" data-id="' + esc(parent.id) + '">' +
             '<span class="arrow">^</span> <span class="name">' + label(parent) +
             '</span> <span class="hint">downstream — go back</span></li>';
    }

    if (idx > 1) out += '<li class="row more-sibs">…</li>';
    if (idx > 0) {
      const prev = nodeById[sibs[idx - 1]];
      out += rowHtml(prev, ' sibling');
    }

    out += '<li class="row selected-river" data-id="' + esc(sel.id) + '">' +
           '<span class="name">' + label(sel) + '</span>' + alsoHtml(sel) + '</li>';

    const kids = [sel.mainline, ...(sel.tributaries || [])].filter(Boolean);
    for (const k of kids) {
      const n = k.n_tributaries || 0;
      out += '<li class="row sub" data-id="' + esc(k.id) + '">' +
             '<span class="name">' + label(k) + '</span>' +
             (n ? ' <span class="more">+' + n + '</span>' : '') +
             alsoHtml(k) + '</li>';
    }

    if (idx >= 0 && idx < sibs.length - 1) {
      const next = nodeById[sibs[idx + 1]];
      out += rowHtml(next, ' sibling');
    }
    if (idx >= 0 && sibs.length - 1 - idx > 1) out += '<li class="row more-sibs">…</li>';

    labelsEl.innerHTML = out;
    drawCrumbs();
    applyFilter();

    const n = sel;
    let note = '';
    if (n.n_upstream) note = ' · ' + n.n_upstream + ' rivers upstream';
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
