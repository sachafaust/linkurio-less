/* Graph Explorer for Neo4j — Explore-phase recreation of a Linkurious-style workspace.
   Plain browser JS on purpose: the Python server does all Neo4j work (read-only). */
'use strict';
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const svgIcon = (name, fill = 'currentColor') => `<svg viewBox="0 0 24 24"><path fill="${fill}" d="${(window.ICONS || {})[name] || ''}"/></svg>`;
const ico = name => `<i data-icon="${name}">${svgIcon(name)}</i>`;
function paintIcons(root = document) { $$('i[data-icon]', root).forEach(i => { if (!i.firstChild) i.innerHTML = svgIcon(i.dataset.icon); }); }
function toast(msg, ms = 2200) { const t = $('#toast'); t.textContent = msg; t.classList.add('on'); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('on'), ms); }
// Logs and error text. Graph properties are left as the source returned them.
function redactSecrets(text) {
  return String(text == null ? '' : text)
    .replace(/(authorization\s*[:=]\s*"?(?:bearer|basic)\s+)([^\s"]+)/gi, '$1[redacted]')
    .replace(/(^|[^A-Za-z0-9_])((?:bearer|basic)\s+)([A-Za-z0-9\-._~+/]{8,}={0,2})/gi, '$1$2[redacted]')
    .replace(/(^|[^A-Za-z0-9_])((?:access_token|refresh_token|id_token|client_secret|password|passwd|token|secret)=)([^&\s#]+)/gi, '$1$2[redacted]')
    .replace(/("(?:access_token|refresh_token|id_token|client_secret|password|passwd|token|secret|authorization)"\s*:\s*")([^"\\]*)(")/gi, '$1[redacted]$3')
    .replace(/(^|[^A-Za-z0-9_])((?:NEO4J_PASSWORD|GRAPHQL_TOKEN|GRAPHQL_CLIENT_SECRET)\s*=\s*)(\S+)/gi, '$1$2[redacted]')
    .replace(/((?:bolt|neo4j)(?:\+s|\+ssc)?:\/\/[^/\s:@]+:)([^@\s]+)(@)/gi, '$1[redacted]$3');
}
async function api(path, body) {
  const r = await fetch(path, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {});
  const j = await r.json();
  // A connection problem is status.error on HTTP 200. Throwing on that field
  // left the server on the new source and the page on the old one.
  if (!r.ok) throw new Error(redactSecrets(j.error || r.statusText));
  return j;
}
function lighten(hex) { const n = parseInt(hex.slice(1), 16); const [r, g, b] = [n >> 16, (n >> 8) & 255, n & 255]; return (r * 299 + g * 587 + b * 114) / 1000 > 170; }
function shortId(id) { const p = String(id).split(':'); return p[p.length - 1]; }
function fmt(v) { if (v === null || v === undefined) return ''; if (Array.isArray(v)) return v.join(', '); if (typeof v === 'object') return JSON.stringify(v); return String(v); }

const S = {
  cfg: null, schema: null, queries: [],
  cat: {}, edge: {}, edgeCaptionsAlways: false,
  filters: { cats: new Set(), types: new Set(), vals: {}, nums: {} },
  prefs: JSON.parse(localStorage.getItem('gx:prefs') || '{"pinOnDrag":false,"incremental":false}'),
  layout: { name: 'force', fast: true, root: null, orientation: 'TB' },
  rules: JSON.parse(localStorage.getItem('gx:grouprules') || '[]'), activeRule: null, collapsed: new Set(), edgeGroups: new Set(),
  undo: [], redo: [], viz: { id: null, title: null, saved: false, autosave: true },
  searchKind: 'node', searchScope: 'all', selPanelOpen: false, lasso: null, lassoMode: null, clipboard: null,
  rightPanel: null, minimap: false,
  // Ceilings come from the server. The working set is gx.local.json on the server.
  budget: { serverNodes: 2000, serverEdges: 5000, nodes: 2000, edges: 5000, expand_limit: 50, cypher_limit: 500, expand_max: 50, cypher_max: 500, lru: false, resources: null, suggestion: null, reason: '' },
  db: { nodes: 0, edges: 0 }, seq: 1, canvasRefused: false,
  ready: false,
};
const DEFAULT_SIZE = 26;
let cy, R;

// ---------- styles -------------------------------------------------------------------
function initStyles(reset = false) {
  const saved = !reset && JSON.parse(localStorage.getItem('gx:styles') || 'null');
  S.cat = {}; S.edge = {};
  const pal = S.cfg.fallback_palette; let k = 0;
  const cats = new Set([...Object.keys(S.cfg.categories), ...Object.keys(S.schema?.categories || {})]);
  for (const c of cats) {
    const d = S.cfg.categories[c];
    S.cat[c] = { color: d?.color || pal[k++ % pal.length], icon: d?.icon || null, size: 100, sizeRule: null, valueColors: {}, caption: d?.caption || null };
  }
  for (const t of Object.keys(S.schema?.types || {})) S.edge[t] = { color: S.cfg.edges.colors[t] || S.cfg.edges.default_color, width: 1, widthRule: null, caption: ['type'] };
  if (saved) { Object.assign(S.cat, saved.cat || {}); Object.assign(S.edge, saved.edge || {}); S.edgeCaptionsAlways = !!saved.edgeCaptionsAlways; }
}
function catStyle(c) {
  if (!S.cat[c]) { const pal = S.cfg.fallback_palette; S.cat[c] = { color: pal[Object.keys(S.cat).length % pal.length], icon: null, size: 100, sizeRule: null, valueColors: {}, caption: null }; }
  return S.cat[c];
}
function edgeStyle(t) { if (!S.edge[t]) S.edge[t] = { color: S.cfg.edges.default_color, width: 1, widthRule: null, caption: ['type'] }; return S.edge[t]; }
function persistStyles() { localStorage.setItem('gx:styles', JSON.stringify({ cat: S.cat, edge: S.edge, edgeCaptionsAlways: S.edgeCaptionsAlways })); }
function nodeCaption(d) {
  if (d.group || d.hubgroup) return d.caption;
  const st = catStyle(d.category); const props = d.props || {};
  let keys = st.caption && st.caption.length ? st.caption : S.cfg.fallback_caption;
  let parts = keys.map(k => fmt(props[k])).filter(Boolean);
  if (!(st.caption && st.caption.length)) parts = parts.slice(0, 1);
  const s = parts.join(' - ') || d.category;
  return s.length > 48 ? s.slice(0, 46) + '…' : s;
}
function nodeColor(d) {
  if (d.group) return d.mixed ? '#9e9e9e' : catStyle(d.category).color;
  const st = catStyle(d.category);
  for (const [prop, map] of Object.entries(st.valueColors || {})) { const v = fmt(d.props?.[prop]); if (map[v]) return map[v]; }
  return st.color;
}
const iconCache = {};
function iconUri(name, color = '#fff') {
  const key = name + color; if (iconCache[key]) return iconCache[key];
  const path = (window.ICONS || {})[name]; if (!path) return 'none';
  return (iconCache[key] = 'data:image/svg+xml;utf8,' + encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="-4 -4 32 32"><path fill="${color}" d="${path}"/></svg>`));
}
const rangeCache = {};
function nodeSize(d) {
  if (d.group) return DEFAULT_SIZE * 1.6;
  if (d.hubgroup) return DEFAULT_SIZE * (1.25 + Math.min(1, Math.log10(Math.max(1, d.count)) / 4));
  const st = catStyle(d.category); let f = (st.size || 100) / 100;
  if (st.sizeRule && st.sizeRule.prop) {
    const v = Number(d.props?.[st.sizeRule.prop]);
    const r = rangeCache[d.category + '|' + st.sizeRule.prop];
    if (!isNaN(v) && r && r.max > r.min) {
      let t = st.sizeRule.scale === 'log' ? Math.log1p(v - r.min) / Math.log1p(r.max - r.min) : (v - r.min) / (r.max - r.min);
      f = 0.6 + 2.4 * Math.max(0, Math.min(1, t));
    }
  }
  return DEFAULT_SIZE * f;
}
function refreshRanges() {
  for (const k in rangeCache) delete rangeCache[k];
  for (const [c, st] of Object.entries(S.cat)) {
    if (!st.sizeRule?.prop) continue;
    const vals = cy.nodes().filter(n => n.data('category') === c).map(n => Number(n.data('props')?.[st.sizeRule.prop])).filter(v => !isNaN(v));
    if (vals.length) rangeCache[c + '|' + st.sizeRule.prop] = { min: Math.min(...vals), max: Math.max(...vals) };
  }
}
function edgeCaption(d) {
  if (d.meta) return `${d.type} (${d.count})`;
  const st = edgeStyle(d.type);
  return (st.caption || ['type']).map(k => k === 'type' ? d.type : fmt(d.props?.[k])).filter(Boolean).join(' - ');
}
function edgeWidth(d) {
  if (d.meta) return Math.min(6, 1.5 + Math.log2(d.count));
  const st = edgeStyle(d.type); let w = st.width || 1;
  if (st.widthRule?.prop) { const v = Number(d.props?.[st.widthRule.prop]); if (!isNaN(v)) w = 1 + Math.min(6, st.widthRule.scale === 'log' ? Math.log1p(Math.abs(v)) / 2 : Math.abs(v) / (st.widthRule.max || 1) * 6); }
  return w;
}
function cyStyle() {
  // cytoscape is only the graph model now (headless); drawing is done by static/render.js.
  // Sizes are kept as data(_s) so layouts know node dimensions; hidden classes drive :visible.
  return [
    { selector: 'node[_s]', style: { width: 'data(_s)', height: 'data(_s)' } },
    { selector: ':parent', style: { padding: 18 } },
    { selector: '.grp-hidden, .egrp-hidden', style: { display: 'none' } },
  ];
}
const THEME_LIGHT = { nodeBorder: '#ffffff', text: '#333333', textHalo: 'rgba(248,248,248,0.92)', halo: '#ffffff', labelBox: '#ffffff', select: '#f12a27', badge: '#454466', groupBadge: '#616161',
  groupFill: 'rgba(224,224,224,0.55)', groupStroke: '#bdbdbd', filteredNode: '#dedede', filteredEdge: '#e9e9e9', edgeText: '#5c5c5c', pulse: 'rgba(241,42,39,0.28)' };
const THEME_DARK = { nodeBorder: '#2b2e35', text: '#e6e6e6', textHalo: 'rgba(29,31,36,0.92)', halo: '#3a3e48', labelBox: '#2b2e35', select: '#ff5a52', badge: '#8a89c0', groupBadge: '#8d8d8d',
  groupFill: 'rgba(60,63,72,0.6)', groupStroke: '#5b5f6a', filteredNode: '#4a4d55', filteredEdge: '#3a3d45', edgeText: '#b8b8b8', pulse: 'rgba(255,90,82,0.3)' };
function theme() { return document.body.classList.contains('dark') ? THEME_DARK : THEME_LIGHT; }
function nodeIcon(d) {
  const ic = d.group && !d.hubgroup ? 'SelectGroup' : catStyle(d.category).icon; if (!ic) return null;
  return { name: ic, color: lighten(nodeColor(d)) ? '#303030' : '#ffffff' };
}
function syncSizes(nodes = cy.nodes()) {
  cy.batch(() => nodes.forEach(n => { const s = nodeSize(n._private.data); if (n._private.data._s !== s) n.data('_s', s); }));
}
const VISUALS = {
  theme, defaultSize: () => DEFAULT_SIZE, nodeColor, nodeSize, nodeCaption, nodeIcon,
  edgeColor: d => d.virtual ? '#9e9e9e' : edgeStyle(d.type).color, edgeWidth, edgeCaption: d => d.virtual ? '' : edgeCaption(d),
  edgeCaptionsAlways: () => S.edgeCaptionsAlways, hiddenCount: n => hiddenCount(n), highlightActive: () => cy.$(':selected').length > 0,
  groupCaption: d => d.caption || '',
};
function restyle() { refreshRanges(); syncSizes(); R.redraw(); drawMinimap(); persistStyles(); }

// ---------- graph data ---------------------------------------------------------------
function realNodes() { return cy.nodes().filter(n => !n._private.data.group && !n._private.data.hubgroup); }
function realEdges() { return cy.edges().filter(e => !e._private.data.meta && !e._private.data.virtual); }
function canvasNodes() { return cy.nodes().filter(n => !n._private.data.group).length; }
function canvasEdges() { return cy.edges().filter(e => !e._private.data.meta).length; }
function graphBody(extra = {}) {
  return Object.assign({}, extra, {
    on_screen: { nodes: canvasNodes(), edges: canvasEdges() },
    budget: { nodes: S.budget.nodes, edges: S.budget.edges, expand_limit: S.budget.expand_limit, cypher_limit: S.budget.cypher_limit },
  });
}
function updateBudgetChip() {
  const el = $('#budget-chip'); if (!el || !cy) return;
  const n = canvasNodes(), e = canvasEdges();
  el.textContent = `${n.toLocaleString()} / ${S.budget.nodes.toLocaleString()} nodes · ${e.toLocaleString()} / ${S.budget.edges.toLocaleString()} edges`;
  el.classList.toggle('warn', n >= S.budget.nodes || e >= S.budget.edges);
  const file = S.budget.configFile || 'gx.local.json';
  el.title = `Working set on screen, saved in ${file} and used the next time the server starts. This machine allows up to ${S.budget.serverNodes.toLocaleString()} nodes and ${S.budget.serverEdges.toLocaleString()} edges. The rest of the graph stays on the source.`;
}
// cytoscape's add() calls edge.parallelEdges() for every new edge only to clear caches; that scan is
// O(degree), so adding the RESOURCE fan-out of a hub costs O(degree^2) (8.5 s for 100k edges / 20 hubs).
// We never use cytoscape's parallel-edge cache (render.js indexes parallel edges itself), so skip it
// during bulk adds; the source/target caches are still cleared by cytoscape.
function addEdgesFast(edgeEls) {
  const proto = Object.getPrototypeOf(cy.collection()); const orig = proto.parallelEdges; const empty = cy.collection();
  proto.parallelEdges = function () { return empty; };
  try { return cy.add(edgeEls); } finally { proto.parallelEdges = orig; }
}
function evictOldest(needN, needE, keepIds) {
  // Least-recently-added nodes that are not pinned. Off unless the session asked for it.
  if (!S.budget.lru) return 0;
  const keep = new Set(keepIds || []);
  let dropped = 0;
  while (canvasNodes() + needN > S.budget.nodes || canvasEdges() + needE > S.budget.edges) {
    let best = null, bestSeq = Infinity;
    cy.nodes().forEach(n => {
      const d = n._private.data;
      if (d.group || d.pinned || keep.has(n.id())) return;
      const seq = d._seq ?? 0;
      if (seq < bestSeq) { bestSeq = seq; best = n; }
    });
    if (!best) break;
    best.remove();
    dropped++;
    if (dropped > S.budget.nodes + 10) break;
  }
  return dropped;
}
function offerCanvasRoom(why) {
  const filtered = cy.nodes('.filtered').filter(n => !n.data('group')).length;
  const bg = modal('Canvas is full', `<div class="warnbox">${ico('AlertCircleOutline')} ${esc(why || 'The working set is full.')}</div>
    <div class="section-d">The canvas holds at most ${S.budget.nodes.toLocaleString()} nodes and ${S.budget.edges.toLocaleString()} edges. Remove filtered nodes, or collapse the largest hub, to free room. The rest of the graph stays on the source.</div>`,
    `<button class="btn outline" data-close="1">Close</button><button class="btn outline" data-room="collapse">Collapse a hub</button><button class="btn primary" data-room="filtered" ${filtered ? '' : 'disabled'}>Remove filtered nodes (${filtered})</button>`);
  const filteredBtn = $('[data-room=filtered]', bg);
  if (filteredBtn) filteredBtn.onclick = () => { if (!filtered) return; removeFiltered(); bg.remove(); };
  $('[data-room=collapse]', bg).onclick = () => { collapseLargestHub(); bg.remove(); };
}
function collapseLargestHub() {
  const ranked = realNodes().toArray().map(n => ({ n, k: n.connectedEdges().filter(e => !e.data('virtual') && !e.data('meta')).length }))
    .filter(x => x.k > 0).sort((a, b) => b.k - a.k);
  if (!ranked.length) return toast('Nothing to collapse');
  const hub = ranked[0].n;
  const drop = hub.neighborhood('node').filter(n => !n.data('pinned') && !n.data('hubgroup') && !n.data('group'));
  const groups = cy.nodes().filter(n => n.data('hubgroup') && n.data('hub') === hub.id());
  if (!drop.length && !groups.length) return toast('The largest hub has only pinned neighbors');
  pushUndo();
  drop.remove();
  groups.connectedEdges().remove();
  groups.remove();
  afterGraphChange();
  toast(`Collapsed ${nodeCaption(hub.data())}`);
  if (hiddenCount(hub) > HUB_THRESHOLD) groupHub(hub, { undo: false });
}
function addGraph(g, around = null, opts = {}) {
  S.canvasRefused = false;
  const ext = cy.extent(); const c0 = around && around.nonempty && around.nonempty() ? around.position() : { x: (ext.x1 + ext.x2) / 2, y: (ext.y1 + ext.y2) / 2 };
  const seen = new Set();
  let fresh = (g.nodes || []).filter(n => n && !seen.has(n.id) && seen.add(n.id) && cy.getElementById(n.id).empty());
  const keep = around ? [around.id()] : [];
  const dropped = evictOldest(fresh.length, (g.edges || []).length, keep);
  if (dropped) toast(`Dropped ${dropped.toLocaleString()} oldest unpinned node${dropped === 1 ? '' : 's'}`);
  const roomN = Math.max(0, S.budget.nodes - canvasNodes());
  const roomE = Math.max(0, S.budget.edges - canvasEdges());
  const asked = fresh.length;
  let trimmed = fresh.length > roomN;
  fresh = fresh.slice(0, roomN);
  S.canvasRefused = asked > 0 && fresh.length === 0;
  const have = new Set(cy.nodes().map(n => n.id()));
  fresh.forEach(n => have.add(n.id));
  const k = fresh.length;
  const nodeEls = fresh.map((n, i) => {
    // small batches: a ring around the expanded node; big batches: a sunflower spiral (no overlaps, O(n))
    const big = k > 60; const a = big ? i * 2.399963 : (2 * Math.PI * i) / Math.max(1, k);
    const r = big ? 34 * Math.sqrt(i + (around ? 4 : 0)) : around ? 90 + 6 * Math.sqrt(k) : 30 * Math.sqrt(i);
    // Neo4j always sends a number. A partial GraphQL node keeps a null degree so
    // the first expand asks for a neighbor summary instead of treating it as isolated.
    const data = {
      id: n.id, labels: n.labels, category: n.category, props: n.props,
      degree: n.partial ? (n.degree == null ? null : n.degree) : (n.degree ?? 0),
      _seq: S.seq++,
    };
    if (n.partial) data.partial = true;
    data._s = nodeSize(data);
    return { group: 'nodes', data, position: { x: c0.x + r * Math.cos(a), y: c0.y + r * Math.sin(a) } };
  });
  let added = nodeEls.length ? cy.add(nodeEls) : cy.collection();
  for (const n of g.nodes || []) { if (!n || n.degree == null) continue; const ex = cy.getElementById(n.id); if (ex.nonempty() && ex._private.data.degree !== n.degree) ex.data('degree', n.degree); }
  const edgeEls = []; const seenE = new Set();
  for (const e of g.edges || []) {
    if (!e || seenE.has(e.id) || cy.getElementById(e.id).nonempty() || !have.has(e.source) || !have.has(e.target)) continue;
    seenE.add(e.id); edgeEls.push({ group: 'edges', data: { id: e.id, type: e.type, source: e.source, target: e.target, props: e.props } });
  }
  if (edgeEls.length > roomE) { edgeEls.length = roomE; trimmed = true; }
  if (edgeEls.length) added = added.union(addEdgesFast(edgeEls));
  if (g.degrees) Object.entries(g.degrees).forEach(([id, deg]) => { const el = cy.getElementById(id); if (el.nonempty() && deg != null) el.data('degree', deg); });
  // A partial node keeps its short property list until the selection panel opens it.
  const missingDeg = fresh.filter(n => n.degree == null && !n.partial).map(n => n.id);
  if (missingDeg.length) api('/api/nodes', { ids: missingDeg }).then(r => { r.nodes.forEach(n => { const el = cy.getElementById(n.id); if (el.nonempty()) el.data('degree', n.degree); }); R.redraw(); });
  let message = g.truncation?.message || null;
  if (trimmed && !S.canvasRefused) message = message || `Stopped at the canvas budget (${S.budget.nodes.toLocaleString()} nodes, ${S.budget.edges.toLocaleString()} edges).`;
  if (S.canvasRefused) offerCanvasRoom(message || `Canvas is full (${S.budget.nodes.toLocaleString()} nodes, ${S.budget.edges.toLocaleString()} edges).`);
  else if (message && opts.toastTruncation !== false) toast(message, 4500);
  afterGraphChange();
  return added;
}
function afterGraphChange() {
  $('#landing').classList.toggle('hidden', cy.nodes().length > 0);
  applyEdgeGrouping(); applyFilters(); if (Object.values(S.cat).some(c => c.sizeRule?.prop)) { refreshRanges(); syncSizes(); } R.indexParallel(); updateHighlight(); R.redraw(); drawMinimap();
  if (S.rightPanel) renderRightPanel(); updateBudgetChip(); markDirty();
}

// ---------- undo / redo / snapshots ----------------------------------------------------
function snapshot() {
  return { nodes: cy.nodes().filter(n => !n._private.data.group).map(n => ({ data: n.data(), position: { ...n.position() }, pinned: !!n.data('pinned') })), edges: cy.edges().filter(e => !e._private.data.meta).map(e => ({ data: e.data() })) };
}
function restore(snap) {
  cy.elements().remove();
  cy.add(snap.nodes.map(n => ({ group: 'nodes', data: { ...n.data, parent: undefined }, position: n.position })));
  cy.add(snap.edges.map(e => ({ group: 'edges', data: e.data })));
  if (S.activeRule) applyNodeGrouping(); afterGraphChange();
}
function pushUndo() { S.undo.push(snapshot()); if (S.undo.length > 50) S.undo.shift(); S.redo = []; updateUndoBtns(); }
function undo() { if (!S.undo.length) return; S.redo.push(snapshot()); restore(S.undo.pop()); updateUndoBtns(); toast('Undo'); }
function redo() { if (!S.redo.length) return; S.undo.push(snapshot()); restore(S.redo.pop()); updateUndoBtns(); toast('Redo'); }
function updateUndoBtns() { $('#btn-undo').disabled = !S.undo.length; $('#btn-redo').disabled = !S.redo.length; }

// ---------- overlay: degree badges, pins, group counts, lasso -----------------------------
const overlay = $('#overlay'); const octx = overlay.getContext('2d');
function sizeOverlay() { const r = window.devicePixelRatio || 1; overlay.width = innerWidth * r; overlay.height = innerHeight * r; overlay.style.width = innerWidth + 'px'; overlay.style.height = innerHeight + 'px'; octx.setTransform(r, 0, 0, r, 0, 0); }
let overlayQueued = false;
function drawOverlay() { if (!overlayQueued) { overlayQueued = true; requestAnimationFrame(() => { overlayQueued = false; paintOverlay(); }); } }
function hiddenCount(n) {
  const d = n._private.data; if (d.group || d.hubgroup) return 0;
  if (d.degree == null) return null;
  let shown = 0;
  for (const e of n._private.edges) { const ed = e._private.data; if (ed.virtual) { const o = ed.source === d.id ? e.target() : e.source(); shown += o._private.data.count || 0; } else if (!ed.meta) shown++; }
  return Math.max(0, (d.degree || 0) - shown);
}
function paintOverlay() {
  octx.clearRect(0, 0, innerWidth, innerHeight);
  if (S.lasso && S.lasso.pts.length > 1) {
    octx.save(); octx.beginPath(); S.lasso.pts.forEach((p, i) => i ? octx.lineTo(p.x, p.y) : octx.moveTo(p.x, p.y)); octx.closePath();
    octx.fillStyle = 'rgba(35,101,241,.08)'; octx.fill(); octx.setLineDash([5, 4]); octx.strokeStyle = '#2365f1'; octx.lineWidth = 1.5; octx.stroke(); octx.restore();
  }
}

// ---------- minimap -----------------------------------------------------------------
function drawMinimap() {
  const c = $('#minimap'); if (!S.minimap) return; const ctx = c.getContext('2d'); const W = c.width, H = c.height;
  ctx.clearRect(0, 0, W, H); const nodes = cy.nodes(':visible'); if (!nodes.length) return;
  const bb = cy.elements(':visible').boundingBox(); const ext = cy.extent();
  const x1 = Math.min(bb.x1, ext.x1), y1 = Math.min(bb.y1, ext.y1), x2 = Math.max(bb.x2, ext.x2), y2 = Math.max(bb.y2, ext.y2);
  const s = Math.min((W - 10) / (x2 - x1 || 1), (H - 10) / (y2 - y1 || 1)); const ox = 5 - x1 * s, oy = 5 - y1 * s;
  c._t = { s, ox, oy };
  ctx.strokeStyle = 'rgba(120,120,120,.35)'; ctx.lineWidth = 0.5; ctx.beginPath();
  cy.edges(':visible').forEach(e => { const a = e.source().position(), b = e.target().position(); ctx.moveTo(a.x * s + ox, a.y * s + oy); ctx.lineTo(b.x * s + ox, b.y * s + oy); }); ctx.stroke();
  nodes.forEach(n => { const p = n.position(); ctx.fillStyle = nodeColor(n.data()); ctx.beginPath(); ctx.arc(p.x * s + ox, p.y * s + oy, 2, 0, 7); ctx.fill(); });
  ctx.strokeStyle = '#2365f1'; ctx.lineWidth = 1.5; ctx.strokeRect(ext.x1 * s + ox, ext.y1 * s + oy, (ext.x2 - ext.x1) * s, (ext.y2 - ext.y1) * s);
}

// ---------- selection + highlight ---------------------------------------------------
function updateHighlight() {
  cy.elements('.hl').removeClass('hl');
  const sel = cy.$(':selected');
  if (sel.length) {
    const nodes = sel.nodes();
    nodes.closedNeighborhood().addClass('hl');
    sel.edges().addClass('hl').connectedNodes().addClass('hl');
  }
  $('#open-selpanel').classList.toggle('hidden', !sel.length || S.selPanelOpen);
  renderLeftPanel();
}
function selectOnly(eles) { cy.$(':selected').unselect(); eles.select(); }

// ---------- filters ------------------------------------------------------------------
function nodeFiltered(n) {
  const d = n.data(); if (d.group) return false;
  if (S.filters.cats.has(d.category)) return true;
  const vals = S.filters.vals[d.category];
  if (vals) for (const [p, set] of Object.entries(vals)) { const v = d.props?.[p] === undefined ? '∅' : fmt(d.props[p]); if (set.has(v)) return true; }
  const nums = S.filters.nums[d.category];
  if (nums) for (const [p, b] of Object.entries(nums)) { const v = Number(d.props?.[p]); if (isNaN(v)) { if (b.hideMissing) return true; continue; } if (b.min != null && v < b.min) return true; if (b.max != null && v > b.max) return true; }
  return false;
}
function applyFilters() {
  cy.batch(() => {
    cy.nodes().forEach(n => n.toggleClass('filtered', nodeFiltered(n)));
    cy.edges().forEach(e => e.toggleClass('filtered', S.filters.types.has(e.data('type')) || e.source().hasClass('filtered') || e.target().hasClass('filtered')));
  });
  const any = S.filters.cats.size || S.filters.types.size || Object.values(S.filters.vals).some(v => Object.values(v).some(s => s.size)) || Object.values(S.filters.nums).some(v => Object.keys(v).length);
  $('[data-panel=filters]').classList.toggle('dot', !!any);
}
function resetFilters() { S.filters = { cats: new Set(), types: new Set(), vals: {}, nums: {} }; applyFilters(); drawOverlay(); renderRightPanel(); }
function removeFiltered() { const f = cy.elements('.filtered'); if (!f.length) return toast('No filtered items'); pushUndo(); f.nodes().connectedEdges().remove(); f.remove(); afterGraphChange(); toast(`Removed ${f.nodes().length} filtered nodes`); }

// ---------- edge grouping --------------------------------------------------------------
function applyEdgeGrouping() {
  cy.edges('.emeta').remove(); cy.edges('.egrp-hidden').removeClass('egrp-hidden');
  if (!S.edgeGroups.size) return;
  const buckets = {};
  realEdges().forEach(e => { const d = e.data(); if (!S.edgeGroups.has(d.type) || e.hasClass('grp-hidden')) return; const k = `${d.source}|${d.target}|${d.type}`; (buckets[k] = buckets[k] || []).push(e); });
  for (const [k, es] of Object.entries(buckets)) {
    if (es.length < 2) continue;
    const [s, t, type] = k.split('|');
    es.forEach(e => e.addClass('egrp-hidden'));
    cy.add({ group: 'edges', classes: 'meta emeta', data: { id: 'meta:' + k, source: s, target: t, type, meta: true, count: es.length, members: es.map(e => e.id()) } });
  }
}

// ---------- node grouping ------------------------------------------------------------
function saveRules() { localStorage.setItem('gx:grouprules', JSON.stringify(S.rules)); }
function clearNodeGrouping() {
  cy.nodes().forEach(n => { if (n.data('parent')) n.move({ parent: null }); });
  cy.elements('.grp-hidden').removeClass('grp-hidden'); cy.edges('.emeta').remove(); cy.edges('.egrp-hidden').removeClass('egrp-hidden');
  const gs = cy.nodes().filter(n => n.data('group')); gs.connectedEdges().remove(); gs.remove();
}
function applyNodeGrouping() {
  clearNodeGrouping();
  const rule = S.rules.find(r => r.id === S.activeRule); if (!rule) { applyEdgeGrouping(); return; }
  const buckets = {};
  realNodes().forEach(n => { const d = n.data(); if (!rule.categories.includes(d.category)) return; const v = d.props?.[rule.prop]; if (v === undefined || v === null || v === '') return; const k = fmt(v); (buckets[k] = buckets[k] || []).push(n); });
  for (const [val, ns] of Object.entries(buckets)) {
    if (ns.length < 2) continue;
    const gid = `grp:${rule.id}:${val}`; const cats = new Set(ns.map(n => n.data('category')));
    const c = ns.reduce((a, n) => ({ x: a.x + n.position('x') / ns.length, y: a.y + n.position('y') / ns.length }), { x: 0, y: 0 });
    const collapsed = !S.collapsed.has('open:' + gid);
    const g = cy.add({ group: 'nodes', classes: 'group', data: { id: gid, group: true, count: ns.length, members: ns.map(n => n.id()), category: [...cats][0], mixed: cats.size > 1, caption: `${rule.title}: ${val}` }, position: c });
    if (collapsed) {
      ns.forEach(n => { n.addClass('grp-hidden'); n.connectedEdges().addClass('grp-hidden'); });
      const seen = new Set(); const ids = new Set(ns.map(n => n.id()));
      ns.forEach(n => n.connectedEdges().forEach(e => {
        if (e.data('meta')) return; const out = ids.has(e.data('source')); const other = out ? e.data('target') : e.data('source'); if (ids.has(other)) return;
        const k = `${out ? 'o' : 'i'}|${other}|${e.data('type')}`; if (seen.has(k)) return; seen.add(k);
        const cnt = ns.reduce((a, m) => a + m.connectedEdges().filter(x => x.data('type') === e.data('type') && (x.data('source') === other || x.data('target') === other)).length, 0);
        cy.add({ group: 'edges', classes: 'meta', data: { id: `gm:${gid}:${k}`, source: out ? gid : other, target: out ? other : gid, type: e.data('type'), meta: true, count: cnt } });
      }));
    } else {
      g.removeClass('group'); ns.forEach(n => n.move({ parent: gid }));
    }
  }
}
function toggleGroup(gid) { const k = 'open:' + gid; S.collapsed.has(k) ? S.collapsed.delete(k) : S.collapsed.add(k); applyNodeGrouping(); afterGraphChange(); }
function setAllGroups(open) { cy.nodes().filter(n => n.data('group')).forEach(g => { const k = 'open:' + g.id(); open ? S.collapsed.add(k) : S.collapsed.delete(k); }); applyNodeGrouping(); afterGraphChange(); }

// ---------- layouts ------------------------------------------------------------------
// ForceAtlas2 (worker) for every force layout: fCoSE collapsed stars in the headless model
const FA2_THRESHOLD = 0; let fa2Run = null;
function runForceLarge(eles, opts, done) {
  // ForceAtlas2 (Barnes-Hut) in a web worker; positions are streamed back into the model every few frames.
  if (fa2Run) fa2Run.stop();
  const { Graph, FA2Layout, inferSettings } = window.GXFA2;
  const nodes = eles.nodes().filter(n => !n.isParent()); const g = new Graph({ type: 'undirected' });
  if (opts.randomize) nodes.forEach((n, i) => { const a = i * 2.399963, r = 34 * Math.sqrt(i); n._private.position.x = r * Math.cos(a); n._private.position.y = r * Math.sin(a); });
  nodes.forEach(n => { const p = n._private.position; g.addNode(n.id(), { x: p.x / 10, y: p.y / 10, size: (n._private.data._s || 26) / 20 + 0.6, fixed: !!(n._private.data.pinned || n.locked() || (opts.onlyNew && !opts.onlyNew.has(n))) }); });
  eles.edges().forEach(e => { const a = e._private.data.source, b = e._private.data.target; if (a !== b && g.hasNode(a) && g.hasNode(b) && !g.hasEdge(a, b)) g.addEdge(a, b); });
  const settings = { ...inferSettings(g), barnesHutOptimize: nodes.length > 2000, barnesHutTheta: 0.9, adjustSizes: nodes.length <= 2000, scalingRatio: nodes.length <= 2000 ? 3 : 12, gravity: nodes.length <= 2000 ? 1.5 : 0.6, slowDown: 3, strongGravityMode: false, linLogMode: false };
  const L = new FA2Layout(g, { settings }); L.start(); R.fastMode = true;
  const t0 = performance.now(), budget = opts.budgetMs ?? Math.min(20000, (nodes.length < 2000 ? 1200 : 2000) + nodes.length * 0.15); let frame = 0, live = true;
  const sync = () => { nodes.forEach(n => { const a = g.getNodeAttributes(n.id()); n._private.position.x = a.x * 10; n._private.position.y = a.y * 10; }); R.positionsChanged(); };
  const tick = () => { if (!live) return; if (++frame % 4 === 0) sync(); if (performance.now() - t0 < budget) requestAnimationFrame(tick); else stop(); };
  const stop = () => { if (!live) return; live = false; L.kill(); fa2Run = null; R.fastMode = false; sync(); cy.emit('position'); done && done(); };
  fa2Run = { stop }; requestAnimationFrame(tick);
}
function runLayout(opts = {}) {
  const name = opts.name || S.layout.name; const eles = cy.elements(':visible');
  if (!eles.nodes().length) return;
  if ((name === 'force') && eles.nodes().length > FA2_THRESHOLD && window.GXFA2) {
    const only = opts.onlyNew ? new Set(opts.onlyNew.toArray()) : null;
    return runForceLarge(eles, { ...opts, onlyNew: only }, () => { if (opts.fit !== false) R.fit(cy.elements(':visible'), 60); drawMinimap(); markDirty(); cy.emit('layoutstop'); });
  }
  const pinned = cy.nodes().filter(n => n.data('pinned'));
  let lockedTmp = cy.collection();
  if (opts.onlyNew) { lockedTmp = cy.nodes().difference(opts.onlyNew).filter(n => !n.locked()); lockedTmp.lock(); }
  let L;
  if (name === 'hierarchical' || name === 'radial') {
    let root = cy.getElementById(S.layout.root || ''); if (root.empty()) root = cy.$('node:selected').first(); if (root.empty()) root = cy.nodes(':visible').max(n => n.degree()).ele;
    S.layout.root = root.id();
    if (name === 'hierarchical') {
      const o = S.layout.orientation;
      L = eles.layout({ name: 'breadthfirst', roots: root, directed: false, spacingFactor: 1.05, animate: false, avoidOverlap: true,
        transform: (n, p) => o === 'LR' ? { x: p.y, y: p.x } : o === 'RL' ? { x: -p.y, y: p.x } : o === 'BT' ? { x: p.x, y: -p.y } : p });
    } else {
      const dist = {}; const bfs = eles.bfs({ roots: root, directed: false, visit: (v, e, u, i, depth) => { dist[v.id()] = depth; } });
      const maxD = Math.max(0, ...Object.values(dist));
      L = eles.layout({ name: 'concentric', concentric: n => maxD - (dist[n.id()] ?? maxD + 1) + 1, levelWidth: () => 1, minNodeSpacing: 22, animate: false, startAngle: -Math.PI / 2 });
    }
  } else {
    L = eles.layout({ name: 'fcose', quality: opts.randomize ? (S.layout.fast ? 'draft' : 'default') : (S.layout.fast ? 'default' : 'proof'), randomize: !!opts.randomize, animate: false,
      nodeRepulsion: 22000, idealEdgeLength: 95, edgeElasticity: 0.45, nodeSeparation: 90, nestingFactor: 0.6, gravity: 0.3, numIter: S.layout.fast ? 1200 : 2500, packComponents: true, nodeDimensionsIncludeLabels: false,
      fixedNodeConstraint: pinned.length ? pinned.map(n => ({ nodeId: n.id(), position: { ...n.position() } })) : undefined, fit: !!opts.fit });
  }
  L.one('layoutstop', () => { lockedTmp.unlock(); if (opts.fit) R.animate({ fit: cy.elements(':visible'), padding: 80 }); drawOverlay(); drawMinimap(); markDirty(); });
  L.run();
}

// ---------- expand -------------------------------------------------------------------
let EXPAND_THRESHOLD = 50;
let HUB_THRESHOLD = 50; // hidden neighbours above the expand cap are grouped, not drawn (tests may lower it)
function noteTruncation(g, hiddenBefore) {
  const got = (g.nodes || []).length;
  if (!g.truncation) g.truncation = { truncated: false, reasons: [], message: null };
  const reasons = g.truncation.reasons || [];
  if (reasons.includes('node_budget') && got === 0) return g;
  if (hiddenBefore != null && got < hiddenBefore) {
    let message = `showing ${got.toLocaleString()} of ${hiddenBefore.toLocaleString()} neighbors`;
    if (reasons.includes('edge_budget')) message += `; edges stopped at ${(g.edges || []).length.toLocaleString()}`;
    g.truncation.message = message;
    g.truncation.truncated = true;
  }
  return g;
}
async function expand(nodes, { categories = null, types = null, limit = EXPAND_THRESHOLD, order = 'neighbors' } = {}) {
  nodes = nodes.filter(n => !n.data('group') && !n.data('hubgroup')); if (!nodes.length) return;
  const want = Math.min(limit || EXPAND_THRESHOLD, EXPAND_THRESHOLD);
  pushUndo();
  try {
    const dropped = S.budget.lru ? evictOldest(want, want, nodes.map(n => n.id())) : 0;
    if (dropped) toast(`Dropped ${dropped.toLocaleString()} oldest unpinned node${dropped === 1 ? '' : 's'}`);
    const page = Math.min(want, Math.max(0, S.budget.nodes - canvasNodes()));
    if (page <= 0) { S.undo.pop(); updateUndoBtns(); return offerCanvasRoom('No room for more nodes.'); }
    const hiddenBefore = nodes.length === 1 ? hiddenCount(nodes[0]) : null;
    const visible = realNodes().map(n => n.id());
    const g = noteTruncation(await api('/api/expand', graphBody({ ids: nodes.map(n => n.id()), visible, categories, types, limit: page, order })), hiddenBefore);
    const added = addGraph(g, nodes.length === 1 ? nodes[0] : null);
    const newNodes = added.nodes();
    if (S.canvasRefused || (!newNodes.length && !added.edges().length)) {
      S.undo.pop(); updateUndoBtns();
      if (!S.canvasRefused) toast(g.truncation?.message || 'No more neighbors to show');
      return;
    }
    if (S.activeRule) { applyNodeGrouping(); afterGraphChange(); }
    runLayout(S.prefs.incremental ? { name: 'force', onlyNew: newNodes } : { name: 'force' });
    if (!g.truncation?.message) toast(`Added ${newNodes.length} node${newNodes.length === 1 ? '' : 's'}`);
  } catch (e) { S.undo.pop(); updateUndoBtns(); toast('Expand failed: ' + e.message, 4000); }
}
async function groupHub(hub, opts = {}) {
  // More hidden neighbors than one expand page: one group node per label and edge type.
  const sum = await api('/api/neighbors', { ids: [hub.id()], visible: realNodes().map(n => n.id()) });
  const groups = [];
  for (const [cat, types] of Object.entries(sum.combos)) for (const [t, cnt] of Object.entries(types)) groups.push([cat, t, cnt]);
  const fresh = groups.filter(([cat, t]) => cy.getElementById(`hub:${hub.id()}:${cat}:${t}`).empty()).length;
  if (opts.undo !== false) pushUndo();
  if (canvasNodes() + fresh > S.budget.nodes && S.budget.lru) evictOldest(fresh, fresh, [hub.id()]);
  if (canvasNodes() + fresh > S.budget.nodes) {
    if (opts.undo !== false) { S.undo.pop(); updateUndoBtns(); }
    return offerCanvasRoom('No room for hub groups.');
  }
  const nodes = [], edges = []; const c = hub.position(); let k = 0;
  for (const [cat, t, cnt] of groups) {
    const id = `hub:${hub.id()}:${cat}:${t}`; const ex = cy.getElementById(id);
    const caption = `${cat}${t ? ' · ' + t : ''} ×${sum.approximate ? '~' : ''}${cnt.toLocaleString()}`;
    if (ex.nonempty()) { ex.data({ count: cnt, caption }); continue; }
    const a = (2 * Math.PI * k++) / Math.max(1, groups.length), r = 150 + 8 * groups.length;
    nodes.push({ group: 'nodes', data: { id, hubgroup: true, hub: hub.id(), category: cat, type: t, count: cnt, caption, labels: [cat], props: {}, degree: 0, _seq: S.seq++, _s: nodeSize({ hubgroup: true, count: cnt }) }, position: { x: c.x + r * Math.cos(a), y: c.y + r * Math.sin(a) } });
    edges.push({ group: 'edges', data: { id: id + ':e', source: hub.id(), target: id, type: t, virtual: true, props: {} } });
  }
  if (nodes.length) cy.add(nodes);
  if (edges.length) cy.add(edges);
  afterGraphChange();
  toast(`${sum.total.toLocaleString()} neighbors grouped by label. Double-click a group to load ${EXPAND_THRESHOLD} at a time.`, 4000);
}
async function loadHubGroup(g, n) {
  const d = g.data();
  const want = Math.max(1, Math.min(n || EXPAND_THRESHOLD, EXPAND_THRESHOLD));
  pushUndo();
  try {
    if (S.budget.lru) evictOldest(want, want, [d.hub, g.id()]);
    const page = Math.min(want, Math.max(0, S.budget.nodes - canvasNodes()));
    if (page <= 0) { S.undo.pop(); updateUndoBtns(); return offerCanvasRoom('No room for more nodes.'); }
    const r = await api('/api/expand', graphBody({ ids: [d.hub], visible: realNodes().map(x => x.id()), categories: [d.category], types: d.type ? [d.type] : null, limit: page, q: d.filter || null }));
    const added = addGraph(r, g, { toastTruncation: false });
    if (S.canvasRefused || !added.nodes().length) {
      S.undo.pop(); updateUndoBtns();
      if (!S.canvasRefused) toast(r.truncation?.message || 'No matching nodes in this group');
      return;
    }
    const got = added.nodes().length; const left = Math.max(0, d.count - got);
    if (left <= 0 || !r.more) g.remove();
    else g.data({ count: left, caption: `${d.category}${d.type ? ' · ' + d.type : ''} ×${left.toLocaleString()}` });
    afterGraphChange();
    runLayout({ name: 'force', onlyNew: added.nodes(), fit: false });
    const tail = r.truncation?.message ? ` — ${r.truncation.message}` : (left > 0 && r.more ? `, ${left.toLocaleString()} left in the group` : '');
    toast(`Loaded ${got} ${d.category} node${got === 1 ? '' : 's'}${tail}`, tail ? 4500 : 2200);
  } catch (e) { S.undo.pop(); updateUndoBtns(); toast('Load failed: ' + e.message, 4000); }
}
function openHubFilter(g) {
  const d = g.data();
  const bg = modal(`Open ${d.category}`, `<div class="section-d">${esc(d.caption)}. Filter by name, tag, id, or account, or leave the box blank to page ${EXPAND_THRESHOLD} at a time.</div>
    <div class="field">Filter<input type="text" data-hf="1" value="${esc(d.filter || '')}" placeholder="name, tag, id, or account"></div>`,
    `<button class="btn outline" data-close="1">Cancel</button><button class="btn primary" data-hgo="1">Load ${EXPAND_THRESHOLD}</button>`);
  $('[data-hgo]', bg).onclick = () => { g.data({ filter: $('[data-hf]', bg).value.trim() }); bg.remove(); loadHubGroup(g, EXPAND_THRESHOLD); };
}
async function expandDefault(node) {
  if (node.data('hubgroup')) return loadHubGroup(node);
  // Neo4j nodes already carry a degree. A GraphQL node asks for one page of
  // neighbor counts first, then uses the same hub grouping.
  if (node.data('degree') == null && !node.data('group')) {
    try {
      const sum = await api('/api/neighbors', { ids: [node.id()], visible: realNodes().map(n => n.id()) });
      if (typeof sum.degree === 'number') node.data('degree', sum.degree);
    } catch (e) { return toast('Expand failed: ' + e.message, 4000); }
  }
  const hidden = hiddenCount(node);
  if (hidden === 0) return toast('All neighbors are already displayed');
  if (hidden > HUB_THRESHOLD) return groupHub(node);
  if (hidden > EXPAND_THRESHOLD) return openSelectiveExpand(cy.collection(node));
  expand(cy.collection(node));
}

// ---------- search -------------------------------------------------------------------
let searchTimer, searchSeq = 0, searchRes = [];
function propSnippet(props, q) {
  const ql = q.toLowerCase(); const parts = [];
  for (const [k, v] of Object.entries(props || {})) {
    const s = fmt(v); const i = s.toLowerCase().indexOf(ql);
    if (ql && i >= 0) parts.unshift(`${esc(k)}: ${esc(s.slice(Math.max(0, i - 20), i))}<mark>${esc(s.substr(i, ql.length))}</mark>${esc(s.slice(i + ql.length, i + ql.length + 30))}`);
    else if (parts.length < 4 && s.length < 60 && !['firstseen', 'lastupdated'].includes(k)) parts.push(`${esc(k)}: ${esc(s)}`);
  }
  return parts.slice(0, 4).join(' | ');
}
function catChip(c, isEdge = false) {
  if (isEdge) return `<span class="cat edge" style="background:${edgeStyle(c).color}">${esc(c)}</span>`;
  const col = S.cat[c]?.color || '#9e9e9e';
  return `<span class="cat ${lighten(col) ? 'light' : ''}" style="background:${col}">${esc(c)}</span>`;
}
async function runSearch() {
  const q = $('#search-input').value.trim(); const seq = ++searchSeq; const box = $('#search-results');
  $('#search').classList.toggle('has-text', !!q);
  if (!q) { box.innerHTML = '<div class="sr-empty">Type to search every node by its properties.</div>'; return; }
  let items = [];
  if (S.searchScope === 'local') {
    const ql = q.toLowerCase();
    if (S.searchKind === 'node') items = realNodes().filter(n => Object.values(n.data('props') || {}).some(v => fmt(v).toLowerCase().includes(ql)) || nodeCaption(n.data()).toLowerCase().includes(ql)).slice(0, 30).map(n => ({ kind: 'node', node: { ...n.data() } }));
    else items = realEdges().filter(e => e.data('type').toLowerCase().includes(ql) || Object.values(e.data('props') || {}).some(v => fmt(v).toLowerCase().includes(ql))).slice(0, 30).map(e => ({ kind: 'edge', edge: e.data() }));
  } else {
    try {
      const r = await api(`/api/search?type=${S.searchKind}&limit=25&q=${encodeURIComponent(q)}`);
      if (seq !== searchSeq) return;
      items = S.searchKind === 'node' ? r.nodes.map(n => ({ kind: 'node', node: n })) : r.edges.map(e => ({ kind: 'edge', edge: e }));
    } catch (e) { box.innerHTML = `<div class="sr-empty err">${esc(e.message)}</div>`; return; }
  }
  searchRes = items;
  box.innerHTML = items.length ? items.map((it, i) => it.kind === 'node'
    ? `<div class="sr" data-i="${i}"><div class="sr-main"><div class="sr-title">${esc(nodeCaption(it.node))}</div><div class="sr-props">${propSnippet(it.node.props, q)}</div></div>${catChip(it.node.category)}</div>`
    : `<div class="sr" data-i="${i}"><div class="sr-main"><div class="sr-title">${esc(it.edge.type)}${it.edge.sourceNode ? ' · ' + esc(nodeCaption(it.edge.sourceNode)) + ' → ' + esc(nodeCaption(it.edge.targetNode)) : ''}</div><div class="sr-props">${propSnippet(it.edge.props, q)}</div></div>${catChip(it.edge.type, true)}</div>`).join('')
    : '<div class="sr-empty">No result</div>';
}
function pickSearch(i) {
  const it = searchRes[i]; if (!it) return; closeSearch();
  let target;
  if (it.kind === 'node') {
    if (cy.getElementById(it.node.id).empty()) { pushUndo(); addGraph({ nodes: [it.node], edges: [] }); fetchEdgesAmongVisible(); }
    target = cy.getElementById(it.node.id);
  } else {
    const e = it.edge;
    if (cy.getElementById(e.id).empty()) { pushUndo(); addGraph({ nodes: [e.sourceNode, e.targetNode].filter(Boolean), edges: [e] }); }
    target = cy.getElementById(e.id);
  }
  if (target.nonempty()) { selectOnly(target); const z = Math.max(cy.zoom(), 1.2); R.animate({ zoom: z, center: target.isNode() ? target.position() : target.midpoint() }, 350); }
}
async function fetchEdgesAmongVisible() {
  const ids = realNodes().map(n => n.id()); if (ids.length < 2) return;
  const r = await api('/api/edges-between', graphBody({ ids })); addGraph({ nodes: [], edges: r.edges, truncation: r.truncation });
}
function closeSearch() { $('#search-dd').classList.add('hidden'); }

// ---------- left selection panel -------------------------------------------------------
const TBL = { sort: {}, page: {}, q: '' };
function renderLeftPanel() {
  const lp = $('#leftpanel'); const sel = cy.$(':selected');
  if (!sel.length || !S.selPanelOpen) { lp.classList.add('hidden'); return; }
  lp.classList.remove('hidden');
  const side = `<div class="lp-side">
    <button data-lp="close" title="Close panel">${ico('ChevronDoubleLeft')}</button><div class="sep"></div>
    <button data-lp="locate" title="Locate">${ico('ArrowExpandAll')}</button>
    <button data-lp="expand" title="Expand">${ico('PlayOutline')}</button>
    <button data-lp="remove" title="Remove from visualization">${ico('MinusCircleOutline')}</button>
    <button data-lp="export" title="Export selection data">${ico('Download')}</button>
    <button data-lp="more" title="More">${ico('DotsHorizontal')}</button></div>`;
  let main = '';
  if (sel.length === 1) {
    const el = sel[0]; const d = el.data();
    if (el.isNode() && d.group) {
      main = `<div class="lp-head"><div class="lp-title">${esc(d.caption)}</div><div class="lp-id">x${d.count}</div></div>
        <div class="lp-cats">${catChip(d.category)} ${d.mixed ? '<span class="cat" style="background:#9e9e9e">MIXED</span>' : ''}</div>
        <div class="props">${d.members.map(id => { const m = cy.getElementById(id); return `<div class="prop" data-goto="${esc(id)}"><div class="k">${esc(m.data('category'))}</div><div class="v">${esc(nodeCaption(m.data()))}</div></div>`; }).join('')}</div>`;
    } else if (el.isNode()) {
      if (d.partial) ensureFullProps(el);
      const st = catStyle(d.category);
      main = `<div class="lp-head"><div class="lp-title" title="${esc(nodeCaption(d))}">${esc(nodeCaption(d))}</div><div class="lp-id" title="${esc(d.id)}">#${esc(shortId(d.id))}</div></div>
        <div class="lp-cats">${d.labels.map(l => l === d.category ? catChip(l) : `<span class="cat light" style="background:#e6e6e6">${esc(l)}</span>`).join('')}
          <span class="swatch" style="background:${nodeColor(d)}" data-lp="design"></span><span class="pct">${st.size}%</span></div>
        ${d.partial ? '<div class="section-d">Reading properties…</div>' : ''}${propList(d.props)}`;
    } else {
      const s = cy.getElementById(d.source), t = cy.getElementById(d.target);
      main = `<div class="lp-head"><div class="lp-title">${esc(d.type)}${d.meta ? ` (${d.count})` : ''}</div><div class="lp-id" title="${esc(d.id)}">${d.meta ? 'group' : '#' + esc(shortId(d.id))}</div></div>
        <div class="lp-cats">${catChip(d.type, true)}<span class="swatch" style="background:${edgeStyle(d.type).color}"></span></div>
        <div class="prop"><div class="k">source</div><div class="v"><a href="#" data-goto="${esc(d.source)}">${esc(nodeCaption(s.data()))}</a></div></div>
        <div class="prop"><div class="k">target</div><div class="v"><a href="#" data-goto="${esc(d.target)}">${esc(nodeCaption(t.data()))}</a></div></div>
        ${d.meta && d.members ? edgeGroupAgg(d) : propList(d.props)}`;
    }
  } else {
    main = multiTable(sel);
  }
  lp.innerHTML = `<div class="lp-main">${main}</div>${side}`;
  const inp = $('.searchbox input', lp);
  if (inp) { inp.value = TBL.q; inp.oninput = () => { TBL.q = inp.value; filterPropList(); }; filterPropList(); }
}
function ensureFullProps(el) {
  const d = el.data();
  if (!el.isNode() || !d.partial || d._propsLoading) return;
  el.data('_propsLoading', true);
  api('/api/nodes', { ids: [d.id] }).then(r => {
    const n = (r.nodes || [])[0];
    el.data('_propsLoading', false);
    if (!n) return;
    el.data({
      props: n.props || {}, partial: false,
      degree: n.degree == null ? el.data('degree') : n.degree,
      category: n.category || el.data('category'), labels: n.labels || el.data('labels'),
    });
    if (S.selPanelOpen && cy.$(':selected').length === 1 && cy.$(':selected').id() === el.id()) renderLeftPanel();
    if (window.R) R.redraw();
  }).catch(e => { el.data('_propsLoading', false); toast(e.message, 4000); });
}
function propList(props) {
  const keys = Object.keys(props || {}).sort();
  return `<div class="searchbox"><input placeholder="Search in the properties">${ico('Magnify')}</div>
    <div class="props">${keys.map(k => { const v = fmt(props[k]); const long = v.length > 180;
      return `<div class="prop" data-k="${esc(k)}"><div class="k">${esc(k)}</div><div class="v">${esc(long ? v.slice(0, 180) : v)}${long ? `… <a href="#" data-more="${esc(k)}">Show more</a>` : ''}</div></div>`; }).join('') || '<div class="empty">No properties</div>'}</div>`;
}
function filterPropList() {
  const q = TBL.q.toLowerCase();
  $$('#leftpanel .prop[data-k]').forEach(p => { const m = !q || p.textContent.toLowerCase().includes(q); p.classList.toggle('hidden', !m); p.classList.toggle('hit', !!q && m); });
  $$('#leftpanel table.sel tbody tr').forEach(tr => { tr.classList.toggle('hidden', !!q && !tr.textContent.toLowerCase().includes(q)); });
}
function edgeGroupAgg(d) {
  const es = d.members.map(id => cy.getElementById(id)).filter(e => e.nonempty());
  const agg = {};
  es.forEach(e => Object.entries(e.data('props') || {}).forEach(([k, v]) => { if (typeof v === 'number' && !['firstseen', 'lastupdated'].includes(k)) (agg[k] = agg[k] || []).push(v); }));
  const rows = Object.entries(agg).map(([k, vs]) => `<tr><td>${esc(k)} (${vs.length})</td><td>Sum ${vs.reduce((a, b) => a + b, 0)}</td><td>Avg ${(vs.reduce((a, b) => a + b, 0) / vs.length).toFixed(2)}</td><td>Min ${Math.min(...vs)}</td><td>Max ${Math.max(...vs)}</td></tr>`).join('');
  return `<div class="section-t">${es.length} grouped edges</div>${rows ? `<table class="kv">${rows}</table>` : '<div class="empty">No numeric properties to aggregate.</div>'}`;
}
function multiTable(sel) {
  const nodes = sel.nodes().filter(n => !n.data('group')), edges = sel.edges().filter(e => !e.data('meta'));
  const groups = {};
  nodes.forEach(n => (groups['n:' + n.data('category')] = groups['n:' + n.data('category')] || []).push(n));
  edges.forEach(e => (groups['e:' + e.data('type')] = groups['e:' + e.data('type')] || []).push(e));
  const head = `<div class="multi-head">${ico('CircleMultipleOutline')} ${nodes.length} NODES${edges.length ? ` · ${edges.length} EDGES` : ''}</div><div class="multi-title">Selection Information</div>
    <div class="lp-cats">${Object.keys(groups).map(k => `<a href="#" data-jump="${esc(k)}">${k[0] === 'n' ? catChip(k.slice(2)) : catChip(k.slice(2), true)}</a>`).join('')}</div>
    <div class="searchbox"><input placeholder="Search in the properties">${ico('Magnify')}</div>`;
  const body = Object.entries(groups).map(([k, els]) => {
    const isN = k[0] === 'n'; const freq = {};
    els.forEach(e => Object.keys(e.data('props') || {}).forEach(p => { if (!['firstseen', 'lastupdated'].includes(p)) freq[p] = (freq[p] || 0) + 1; }));
    const cols = Object.keys(freq).sort((a, b) => freq[b] - freq[a] || a.localeCompare(b)).slice(0, 8);
    const sort = TBL.sort[k]; let rows = els.map(e => ({ id: e.id(), cap: isN ? nodeCaption(e.data()) : e.data('type'), p: e.data('props') || {} }));
    if (sort) rows.sort((a, b) => { const va = sort.col === '#' ? shortId(a.id) : sort.col === '@' ? a.cap : fmt(a.p[sort.col]); const vb = sort.col === '#' ? shortId(b.id) : sort.col === '@' ? b.cap : fmt(b.p[sort.col]); const na = Number(va), nb = Number(vb); const c = !isNaN(na) && !isNaN(nb) ? na - nb : va.localeCompare(vb); return sort.dir * c; });
    const per = 10, pages = Math.ceil(rows.length / per), page = Math.min(TBL.page[k] || 0, pages - 1);
    const arrow = c => sort && sort.col === c ? (sort.dir > 0 ? ' ▲' : ' ▼') : '';
    return `<div data-sec="${esc(k)}"><div>${isN ? catChip(k.slice(2)) : catChip(k.slice(2), true)} (${els.length})</div>
      <div class="tbl-wrap"><table class="sel"><thead><tr><th data-sort="${esc(k)}|#">${isN ? 'Node' : 'Edge'} ID${arrow('#')}</th><th data-sort="${esc(k)}|@">${isN ? 'Node Caption' : 'Type'}${arrow('@')}</th>${cols.map(c => `<th data-sort="${esc(k)}|${esc(c)}">${esc(c)}${arrow(c)}</th>`).join('')}</tr></thead>
      <tbody>${rows.slice(page * per, page * per + per).map(r => `<tr data-row="${esc(r.id)}"><td>${esc(shortId(r.id))}</td><td>${esc(r.cap)}</td>${cols.map(c => `<td>${esc(fmt(r.p[c]))}</td>`).join('')}</tr>`).join('')}</tbody></table></div>
      ${pages > 1 ? `<div class="pager"><button data-page="${esc(k)}|${page - 1}" ${page ? '' : 'disabled'}>‹</button> ${page + 1} / ${pages} <button data-page="${esc(k)}|${page + 1}" ${page < pages - 1 ? '' : 'disabled'}>›</button></div>` : ''}</div>`;
  }).join('');
  return head + `<div class="props">${body}</div>`;
}
function pulse(ele) { ele.addClass('pulse'); setTimeout(() => ele.removeClass('pulse'), 450); setTimeout(() => ele.addClass('pulse'), 650); setTimeout(() => ele.removeClass('pulse'), 1100); }

// ---------- right panels ---------------------------------------------------------------
const RP = { filterTab: 'nodes', designTab: 'styles', designSub: 'nodes', open: new Set() };
function openPanel(name) {
  S.rightPanel = S.rightPanel === name ? null : name;
  $$('#righttb button').forEach(b => b.classList.toggle('on', b.dataset.panel === S.rightPanel));
  document.body.classList.toggle('rp-open', !!S.rightPanel);
  renderRightPanel();
}
function rpFrame(title, icon, body, foot = '') {
  return `<div class="rp-head">${icon ? ico(icon) : ''}${esc(title)}<button class="x" data-rp="close">${ico('Close')}</button></div><div class="rp-body">${body}</div>${foot ? `<div class="rp-foot">${foot}</div>` : ''}`;
}
function vizCats() { const c = {}; realNodes().forEach(n => { c[n.data('category')] = (c[n.data('category')] || 0) + 1; }); return c; }
function vizTypes() { const c = {}; realEdges().forEach(e => { c[e.data('type')] = (c[e.data('type')] || 0) + 1; }); return c; }
function propType(vals) {
  const vs = vals.filter(v => v !== undefined && v !== null);
  if (vs.length && vs.every(v => typeof v === 'boolean')) return 'bool';
  if (vs.length && vs.every(v => typeof v === 'number')) return 'num';
  return 'str';
}
const TYPE_BADGE = { str: 'Abc', num: '1₂₃', bool: 'T/F' };
function renderRightPanel() {
  const rp = $('#rightpanel');
  if (!S.rightPanel) { rp.classList.add('hidden'); return; }
  rp.classList.remove('hidden');
  const scroll = $('.rp-body', rp)?.scrollTop || 0;
  const fn = { filters: panelFilters, design: panelDesign, layout: panelLayout, nodegroup: panelNodeGroup, edgegroup: panelEdgeGroup, queries: panelQueries, timeline: panelTimeline }[S.rightPanel];
  rp.innerHTML = fn(); paintIcons(rp);
  const b = $('.rp-body', rp); if (b) b.scrollTop = scroll;
}
function sw(on, attrs) { return `<span class="switch ${on ? 'on' : ''}" ${attrs}></span>`; }
function panelFilters() {
  const tabs = `<div class="tabs-line"><button data-ftab="nodes" class="${RP.filterTab === 'nodes' ? 'on' : ''}">${ico('CircleMultipleOutline')}NODES</button><button data-ftab="edges" class="${RP.filterTab === 'edges' ? 'on' : ''}">${ico('VectorPolyline')}EDGES</button></div>`;
  const applied = [];
  S.filters.cats.forEach(c => applied.push([`Category: ${c}`, `cat|${c}`]));
  S.filters.types.forEach(t => applied.push([`Edge type: ${t}`, `type|${t}`]));
  Object.entries(S.filters.vals).forEach(([c, ps]) => Object.entries(ps).forEach(([p, set]) => set.forEach(v => applied.push([`${c}.${p} = ${v}`, `val|${c}|${p}|${v}`]))));
  Object.entries(S.filters.nums).forEach(([c, ps]) => Object.entries(ps).forEach(([p, b]) => applied.push([`${c}.${p} ∈ [${b.min ?? '−∞'}, ${b.max ?? '+∞'}]`, `num|${c}|${p}`])));
  let body = `<div class="hint">Disable categories or property values below to hide them from the graph.</div>`;
  if (applied.length) body += `<div class="row"><b style="flex:1">Applied Filters (${applied.length})</b><button class="btn link small" data-f="reset">Reset Filters</button></div><div class="applied">${applied.map(([t, k]) => `<span class="chip">${esc(t)}<b data-unf="${esc(k)}">×</b></span>`).join('')}</div>`;
  if (RP.filterTab === 'nodes') {
    const cats = vizCats();
    body += Object.keys(cats).sort().map(c => {
      const open = RP.open.has('f:' + c); const els = realNodes().filter(n => n.data('category') === c);
      let sub = '';
      if (open) {
        const props = {}; els.forEach(n => Object.entries(n.data('props') || {}).forEach(([k, v]) => (props[k] = props[k] || []).push(v)));
        sub = Object.keys(props).sort().map(p => {
          const t = propType(props[p]); const po = RP.open.has(`fp:${c}:${p}`);
          let inner = '';
          if (po && t !== 'num') {
            const counts = {}; els.forEach(n => { const v = n.data('props')?.[p]; const k = v === undefined ? '∅' : fmt(v); counts[k] = (counts[k] || 0) + 1; });
            const hidden = S.filters.vals[c]?.[p] || new Set();
            inner = Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 60).map(([v, n]) => `<div class="row sub2"><span class="name lc" title="${esc(v)}">${v === '∅' ? '<i>Missing values</i>' : esc(v)}</span><span class="cnt">${n}</span><button class="ib" data-fsel="val|${esc(c)}|${esc(p)}|${esc(v)}" title="Select">${ico('SelectionDrag')}</button>${sw(!hidden.has(v), `data-ftog="val|${esc(c)}|${esc(p)}|${esc(v)}"`)}</div>`).join('');
          } else if (po) {
            const vs = props[p].filter(v => typeof v === 'number'); const mn = Math.min(...vs), mx = Math.max(...vs); const b = S.filters.nums[c]?.[p] || {};
            const bins = new Array(16).fill(0); vs.forEach(v => bins[mx > mn ? Math.min(15, Math.floor((v - mn) / (mx - mn) * 16)) : 0]++); const bm = Math.max(...bins);
            inner = `<div class="hist">${bins.map(x => `<div style="height:${(x / bm) * 100}%"></div>`).join('')}</div><div class="numfilter">min <input type="number" data-fnum="${esc(c)}|${esc(p)}|min" value="${b.min ?? ''}" placeholder="${mn}"> max <input type="number" data-fnum="${esc(c)}|${esc(p)}|max" value="${b.max ?? ''}" placeholder="${mx}"></div><div class="numfilter">${vs.length} valid · ${els.length - vs.length} missing/invalid</div>`;
          }
          return `<div class="row sub"><span class="tw" data-open="fp:${esc(c)}:${esc(p)}">${ico(po ? 'MenuDown' : 'MenuRight')}</span><span class="name lc" data-open="fp:${esc(c)}:${esc(p)}">${esc(p)}</span><span class="ptype">${TYPE_BADGE[t]}</span></div>${inner}`;
        }).join('');
      }
      return `<div class="row"><span class="tw" data-open="f:${esc(c)}">${ico(open ? 'MenuDown' : 'MenuRight')}</span><span class="name" data-open="f:${esc(c)}">${esc(c)}</span><span class="cnt">${cats[c]}</span><button class="ib" data-fsel="cat|${esc(c)}" title="Select all">${ico('SelectionDrag')}</button>${sw(!S.filters.cats.has(c), `data-ftog="cat|${esc(c)}"`)}</div>${sub}`;
    }).join('') || '<div class="empty">No nodes in the visualization.</div>';
  } else {
    const types = vizTypes();
    body += Object.keys(types).sort().map(t => `<div class="row"><span class="tw"></span><span class="name">${esc(t)}</span><span class="cnt">${types[t]}</span><button class="ib" data-fsel="type|${esc(t)}" title="Select all">${ico('SelectionDrag')}</button>${sw(!S.filters.types.has(t), `data-ftog="type|${esc(t)}"`)}</div>`).join('') || '<div class="empty">No edges in the visualization.</div>';
  }
  return rpFrame('FILTERS', null, tabs + body, `<span></span><button class="btn outline small" data-act="help">${ico('HelpCircleOutline')} Help</button>`);
}
function panelDesign() {
  const tabs = `<div class="tabs-box"><button data-dtab="styles" class="${RP.designTab === 'styles' ? 'on' : ''}">STYLES</button><button data-dtab="captions" class="${RP.designTab === 'captions' ? 'on' : ''}">CAPTIONS</button></div>
    <div class="tabs-line red"><button data-dsub="nodes" class="${RP.designSub === 'nodes' ? 'on' : ''}">${ico('CircleMultipleOutline')}NODES</button><button data-dsub="edges" class="${RP.designSub === 'edges' ? 'on' : ''}">${ico('VectorPolyline')}EDGES</button></div>`;
  const inViz = vizCats(); const schemaCats = Object.keys(S.schema?.categories || {});
  const cats = [...new Set([...Object.keys(inViz), ...schemaCats])].sort((a, b) => (inViz[b] ? 1 : 0) - (inViz[a] ? 1 : 0) || a.localeCompare(b));
  let body = '';
  if (RP.designTab === 'styles' && RP.designSub === 'nodes') {
    body = cats.map(c => {
      const st = catStyle(c); const open = RP.open.has('d:' + c);
      let sub = '';
      if (open) {
        const props = Object.keys(S.schema?.categories?.[c]?.props || {}).sort();
        sub = props.map(p => {
          const po = RP.open.has(`dp:${c}:${p}`); let inner = '';
          if (po) {
            const vals = {}; realNodes().filter(n => n.data('category') === c).forEach(n => { const v = fmt(n.data('props')?.[p]); if (v) vals[v] = (vals[v] || 0) + 1; });
            const map = st.valueColors?.[p] || {};
            inner = Object.keys(vals).sort().slice(0, 40).map(v => `<div class="row sub2"><span class="name lc">${esc(v)}</span><span class="cnt">${vals[v]}</span><button class="swatch" style="background:${map[v] || st.color};${map[v] ? '' : 'opacity:.35'}" data-vcolor="${esc(c)}|${esc(p)}|${esc(v)}"></button></div>`).join('') || '<div class="row sub2 empty">No values in the visualization</div>';
          }
          return `<div class="row sub"><span class="tw" data-open="dp:${esc(c)}:${esc(p)}">${ico(po ? 'MenuDown' : 'MenuRight')}</span><span class="name lc" data-open="dp:${esc(c)}:${esc(p)}">${esc(p)}</span></div>${inner}`;
        }).join('');
      }
      return `<div class="row"><span class="tw" data-open="d:${esc(c)}">${ico(open ? 'MenuDown' : 'MenuRight')}</span><span class="name" data-open="d:${esc(c)}" title="${esc(c)}">${esc(c)}</span>
        <button class="ib" data-icon-pick="${esc(c)}" title="Icon">${st.icon ? ico(st.icon) : ico('Plus')}</button>
        <button class="swatch" style="background:${st.color}" data-color="${esc(c)}" title="Color"></button>
        <button class="pct" data-size="${esc(c)}" title="Size">${st.sizeRule?.prop ? '↗' + esc(st.sizeRule.prop) : st.size + '%'}</button></div>${sub}`;
    }).join('');
  } else if (RP.designTab === 'styles') {
    const types = [...new Set([...Object.keys(vizTypes()), ...Object.keys(S.schema?.types || {})])].sort();
    body = types.map(t => { const st = edgeStyle(t); return `<div class="row"><span class="tw"></span><span class="name" title="${esc(t)}">${esc(t)}</span><button class="swatch" style="background:${st.color}" data-ecolor="${esc(t)}"></button><button class="pct" data-ewidth="${esc(t)}">${st.widthRule?.prop ? '↗' + esc(st.widthRule.prop) : st.width + 'px'}</button></div>`; }).join('');
  } else if (RP.designSub === 'nodes') {
    body = cats.map(c => {
      const st = catStyle(c); const open = RP.open.has('c:' + c); const chosen = st.caption || [];
      const props = Object.keys(S.schema?.categories?.[c]?.props || {}).sort();
      return `<div class="row"><span class="tw" data-open="c:${esc(c)}">${ico(open ? 'MenuDown' : 'MenuRight')}</span><span class="name" data-open="c:${esc(c)}">${esc(c)}</span></div>
        ${open ? `<div class="cap-chosen">${chosen.map((p, i) => `<span>${i + 1}. ${esc(p)}<b data-capdel="${esc(c)}|${esc(p)}">×</b></span>`).join('') || '<i>No caption property: using defaults</i>'}</div>
        <div class="cap-list">${props.map(p => `<div class="row sub"><input type="checkbox" data-cap="${esc(c)}|${esc(p)}" ${chosen.includes(p) ? 'checked' : ''}><span class="name lc">${esc(p)}</span></div>`).join('')}</div>` : ''}`;
    }).join('');
  } else {
    const types = [...new Set([...Object.keys(vizTypes()), ...Object.keys(S.schema?.types || {})])].sort();
    body = `<div class="row"><span class="name lc">Always show edge captions</span>${sw(S.edgeCaptionsAlways, 'data-ecapall="1"')}</div>` +
      types.map(t => { const st = edgeStyle(t); const props = new Set(['type']); realEdges().filter(e => e.data('type') === t).forEach(e => Object.keys(e.data('props') || {}).forEach(k => props.add(k)));
        return `<div class="row"><span class="tw"></span><span class="name">${esc(t)}</span></div><div class="cap-list">${[...props].map(p => `<div class="row sub"><input type="checkbox" data-ecap="${esc(t)}|${esc(p)}" ${(st.caption || []).includes(p) ? 'checked' : ''}><span class="name lc">${esc(p)}</span></div>`).join('')}</div>`; }).join('');
  }
  return rpFrame('DESIGN', 'Brush', tabs + body, `<button class="ib" data-act="help" title="Help">${ico('HelpCircleOutline')}</button><button class="ib" data-d="gear" title="Settings">${ico('Cog')}</button>`);
}
function panelLayout() {
  const L = S.layout; const root = cy.getElementById(L.root || '');
  const card = (id, title, desc, extra = '') => `<div class="radio-card ${L.name === id ? 'on' : ''}" data-lay="${id}"><div class="t"><input type="radio" ${L.name === id ? 'checked' : ''}> ${title}</div><div class="d">${desc}</div>${L.name === id ? extra : ''}</div>`;
  const rootUi = `<div class="field">Root node: <b>${root.nonempty() ? esc(nodeCaption(root.data())) : 'none (selected or most connected node)'}</b><button class="btn outline small" data-lroot="1">Set the selected node as the root node</button></div>`;
  const body = `<div class="section-t">User Preferences</div><div class="section-d">These are applied to all your visualizations.</div>
    <div class="pref">${sw(S.prefs.pinOnDrag, 'data-pref="pinOnDrag"')}<div><div class="pt">Pin on drag</div><div class="pd">Pin nodes automatically on mouse drag.</div></div></div>
    <div class="pref">${sw(S.prefs.incremental, 'data-pref="incremental"')}<div><div class="pt">Incremental expand on Force-directed layout</div><div class="pd">Force-directed layout is applied only to new nodes added to the visualization.</div></div></div>
    <div class="section-t" style="border-top:1px solid var(--line);padding-top:12px">Layout</div>
    ${card('force', 'Force-directed', 'Default layout. Nodes repel, edges attract.', `<div class="pref">${sw(L.fast, 'data-lfast="1"')}<div><div class="pt">Fast mode</div><div class="pd">Turn off for better looking but slower results.</div></div></div>`)}
    ${card('hierarchical', 'Hierarchical', 'Organizes nodes in layers from a root node.', rootUi + `<div class="field">Orientation<select data-lorient="1">${[['TB', 'Top to bottom'], ['BT', 'Bottom to top'], ['LR', 'Left to right'], ['RL', 'Right to left']].map(([v, t]) => `<option value="${v}" ${L.orientation === v ? 'selected' : ''}>${t}</option>`).join('')}</select></div>`)}
    ${card('radial', 'Radial', 'Places nodes in circles around a root node by graph distance (max 1500 nodes).', rootUi)}`;
  return rpFrame('LAYOUT', null, body, `<span></span><button class="btn primary" data-lrun="1">Run layout</button>`);
}
function panelNodeGroup() {
  const body = S.rules.length ? S.rules.map(r => `<div class="row"><span class="name lc"><b>${esc(r.title)}</b><br><small>${esc(r.prop)} · ${esc(r.categories.join(', '))}</small></span><button class="ib" data-gdel="${esc(r.id)}" title="Delete">${ico('TrashCanOutline')}</button>${sw(S.activeRule === r.id, `data-gtog="${esc(r.id)}"`)}</div>`).join('')
    : `<div class="empty">No grouping rule yet. Group nodes that share a property value (for example every EC2 instance in the same region).</div>`;
  return rpFrame('NODE GROUPING', null, `<div class="section-d" style="margin-top:6px">Only one rule can be active at a time. Double-click a group to collapse or expand it.</div>` + body, `<span></span><button class="btn primary" data-gnew="1">Create rule</button>`);
}
function panelEdgeGroup() {
  const types = vizTypes();
  const body = Object.keys(types).sort().map(t => `<div class="row"><span class="name">${esc(t)}</span><span class="cnt">${types[t]}</span>${sw(S.edgeGroups.has(t), `data-egtog="${esc(t)}"`)}</div>`).join('') || '<div class="empty">No edges in the visualization.</div>';
  return rpFrame('EDGE GROUPING', null, `<div class="section-d" style="margin-top:6px">Group parallel edges of the same type and direction into one edge.</div>` + body);
}
function panelQueries() {
  const list = (arr, local) => arr.map((q, i) => `<div class="q-item" data-qrun="${local ? 'l' : 'b'}|${i}"><div class="q-name">${esc(q.name)}</div><div class="q-tags">${esc((q.tags || []).join(' · '))}${local ? ' · saved locally' : ''}</div></div>`).join('');
  if (!sourceIsNeo4j()) {
    const wiz = sourceIsGraphQL() && S.source.adapter === 'wiz';
    const intro = wiz
      ? 'Wiz graph search. Click a query to add its entities to the visualization.'
      : (sourceIsGraphQL() ? 'This GraphQL source has no saved queries.' : 'Add a data connection to see its queries.');
    const queries = wiz ? (S.queries || []) : [];
    const empty = wiz && !queries.length ? '<div class="empty">No saved queries for this connection.</div>' : '';
    return rpFrame('QUERIES', 'CodeBraces', `<div class="section-d" style="margin-top:6px">${intro}</div>${list(queries, false)}${empty}`, '<span></span>');
  }
  const saved = JSON.parse(localStorage.getItem('gx:queries') || '[]');
  return rpFrame('QUERIES', 'CodeBraces', `<div class="section-d" style="margin-top:6px">Read-only Cypher. Click a query to add its result to the visualization.</div>${list(S.queries, false)}${saved.length ? '<div class="section-t">My queries</div>' + list(saved, true) : ''}`, `<span></span><button class="btn primary" data-act="code-query">Code a query</button>`);
}
function panelTimeline() {
  return rpFrame('TIMELINE', 'ClockOutline', `<div class="empty">The timeline is not part of this Explore-phase prototype. Cartography stores <code>firstseen</code> / <code>lastupdated</code> as epoch seconds, so a timeline is feasible later; use the Filters panel number bounds on those properties for now.</div>`);
}

// ---------- context menus --------------------------------------------------------------
function zoomBy(f) { const z = Math.max(R.minZoom, Math.min(R.maxZoom, cy.zoom() * f)); const c = { x: (cy.width() / 2 - cy.pan().x) / cy.zoom(), y: (cy.height() / 2 - cy.pan().y) / cy.zoom() }; R.animate({ zoom: z, center: c }, 200); }
function closeMenus(pops = false) { $('#menus').innerHTML = ''; if (pops) $$('.pop').forEach(p => p.remove()); }
function showMenu(items, x, y, parent = null) {
  const m = document.createElement('div'); m.className = 'menu';
  m.innerHTML = items.map((it, i) => it === '-' ? '<div class="sep"></div>' : it.header ? `<div class="mh">${esc(it.header)}</div>` :
    `<div class="mi ${it.disabled ? 'disabled' : ''} ${it.icon ? '' : 'noicon'}" data-i="${i}">${it.icon ? ico(it.icon) : ''}${it.html || esc(it.label)}<span class="r">${it.count != null ? `<span class="n">${it.count}</span>` : ''}${it.right ? esc(it.right) : ''}${it.sub ? ico('ChevronRight') : ''}</span></div>`).join('');
  $('#menus').appendChild(m); S.menuEvent = window.event;
  const W = m.offsetWidth, H = m.offsetHeight;
  m.style.left = Math.min(x, innerWidth - W - 8) + 'px'; m.style.top = Math.max(8, Math.min(y, innerHeight - H - 8)) + 'px';
  m._parent = parent;
  $$('.mi', m).forEach(el => {
    const it = items[+el.dataset.i];
    el.addEventListener('mouseenter', () => {
      $$('.menu', $('#menus')).forEach(o => { if (o !== m && o._parent === m) o.remove(); });
      $$('.mi.open', m).forEach(o => o.classList.remove('open'));
      if (it.sub) { el.classList.add('open'); const r = el.getBoundingClientRect(); const sub = typeof it.sub === 'function' ? it.sub() : it.sub; Promise.resolve(sub).then(s => { if (el.classList.contains('open')) showMenu(s, r.right, r.top - 6, m); }); }
    });
    el.addEventListener('click', ev => { ev.stopPropagation(); if (it.sub) return; closeMenus(); it.act && it.act(); });
  });
  return m;
}
function selectSubmenu() {
  const nodes = realNodes(); const cats = vizCats();
  return [
    { label: 'All nodes', right: String(nodes.length), act: () => selectOnly(cy.nodes(':visible')) },
    { label: 'Leaf nodes', right: String(nodes.filter(n => n.degree() === 1).length), act: () => selectOnly(nodes.filter(n => n.degree() === 1)) },
    { label: 'Isolated nodes', right: String(nodes.filter(n => n.degree() === 0).length), act: () => selectOnly(nodes.filter(n => n.degree() === 0)) },
    '-', { header: 'Node categories' },
    ...Object.keys(cats).sort().map(c => ({ html: catChip(c), right: String(cats[c]), act: () => selectOnly(nodes.filter(n => n.data('category') === c)) })),
  ];
}
function sourceIsGraphQL() {
  return S.source && S.source.kind === 'graphql';
}
function sourceIsNeo4j() {
  return S.source && S.source.kind === 'neo4j';
}
function backgroundMenuItems() {
  const items = [
    { label: 'Select', icon: 'Select', sub: selectSubmenu },
    { label: 'Invert selection', icon: 'SwapHorizontal', act: () => { const s = cy.$(':selected'); const all = cy.elements(':visible'); s.unselect(); all.difference(s).nodes().select(); } },
    { label: 'Lasso', icon: 'Lasso', act: () => { S.lassoMode = 'node'; toast('Draw around the nodes to select'); } },
    { label: 'Edge lasso', icon: 'Lasso', act: () => { S.lassoMode = 'edge'; toast('Draw around the edges to select'); } },
    { label: 'Remove filtered items', icon: 'MinusCircleOutline', disabled: !cy.elements('.filtered').length, act: removeFiltered },
    '-', { label: 'Refresh layout', act: () => runLayout() }, { label: 'Edit layout settings', act: () => openPanel('layout') },
    '-', { label: 'Collapse all groups', disabled: !S.activeRule, act: () => setAllGroups(false) }, { label: 'Expand all groups', disabled: !S.activeRule, act: () => setAllGroups(true) },
  ];
  if (sourceIsNeo4j()) items.push('-', { label: 'Queries', icon: 'CodeBraces', sub: () => (S.queries || []).map(q => ({ label: q.name, act: () => runQueryToGraph(q.cypher) })) });
  else if (sourceIsGraphQL() && (S.queries || []).some(q => q.graphql)) items.push('-', { label: 'Queries', icon: 'CodeBraces', sub: () => S.queries.filter(q => q.graphql).map(q => ({ label: q.name, act: () => runGraphqlToGraph(q) })) });
  return items;
}
async function expandSubmenu(nodes) {
  const r = await api('/api/neighbors', { ids: nodes.map(n => n.id()), visible: realNodes().map(n => n.id()) });
  return [
    { label: 'All neighbors', right: String(r.total), act: () => r.total > HUB_THRESHOLD && nodes.length === 1 ? groupHub(nodes[0]) : r.total > EXPAND_THRESHOLD ? openSelectiveExpand(nodes, r) : expand(nodes) },
    '-',
    ...Object.entries(r.categories).sort((a, b) => b[1] - a[1]).map(([c, n]) => ({ html: esc(c), count: n, act: () => n > EXPAND_THRESHOLD ? openSelectiveExpand(nodes, r, c) : expand(nodes, { categories: [c] }) })),
    '-', { label: 'More options…', act: () => openSelectiveExpand(nodes, r) },
  ];
}
function nodeMenuItems(node) {
  if (node.data('hubgroup')) return [{ header: node.data('caption') }, { label: `Load next ${EXPAND_THRESHOLD}`, icon: 'PlayOutline', act: () => loadHubGroup(node) }, { label: 'Filter members…', icon: 'FilterVariant', act: () => openHubFilter(node) }, '-', { label: 'Remove group', icon: 'MinusCircleOutline', act: () => removeEles(node) }];
  const sel = cy.$('node:selected').filter(n => !n.data('group')); const nodes = sel.length ? sel : cy.collection(node);
  const pinned = nodes.every(n => n.data('pinned'));
  const g = node.data('group');
  const items = [
    { label: 'Expand', icon: 'ArrowExpandAll', sub: () => expandSubmenu(nodes), disabled: g },
    { label: 'Select', icon: 'Select', sub: () => { const nb = nodes.neighborhood().nodes(); const cats = {}; nb.forEach(n => { cats[n.data('category')] = (cats[n.data('category')] || 0) + 1; });
      return [{ label: 'All neighbors', right: String(nb.length), act: () => nodes.union(nb).select() }, '-', { header: 'Neighbors by node category' }, ...Object.keys(cats).sort().map(c => ({ html: catChip(c), right: String(cats[c]), act: () => nb.filter(n => n.data('category') === c).select() }))]; } },
  ];
  if (sourceIsNeo4j()) items.push({ label: 'Queries', icon: 'CodeBraces', sub: () => [{ label: 'Cypher for this selection…', act: () => openCodeQuery(`MATCH (n)-[r]-(m) WHERE elementId(n) IN ${JSON.stringify(nodes.map(n => n.id()))}\nRETURN n, r, m LIMIT 100`) }] });
  items.push(
    '-', { label: pinned ? 'Unpin' : 'Pin', icon: 'PinOutline', act: () => { nodes.forEach(n => n.data('pinned', !pinned)); R.redraw(); markDirty(); } },
    { label: 'Remove from visualization', icon: 'MinusCircleOutline', act: () => removeEles(nodes) },
  );
  if (g) items.splice(0, 1, { label: S.collapsed.has('open:' + node.id()) ? 'Collapse group' : 'Expand group', icon: 'SelectGroup', act: () => toggleGroup(node.id()) });
  items.push('-', { label: 'Export selection data', icon: 'Download', act: () => openExportData(true) });
  return items;
}
function removeEles(eles) { pushUndo(); const ns = eles.nodes(); eles.union(ns.connectedEdges()).remove(); afterGraphChange(); }

// ---------- modals ---------------------------------------------------------------------
function modal(title, body, foot, { width } = {}) {
  closeMenus(true);
  const bg = document.createElement('div'); bg.className = 'modal-bg';
  bg.innerHTML = `<div class="modal" ${width ? `style="width:${width}px"` : ''}><div class="mh">${esc(title)}<button class="x" data-close="1">${ico('Close')}</button></div><div class="mb">${body}</div>${foot ? `<div class="mf">${foot}</div>` : ''}</div>`;
  $('#modal-root').appendChild(bg); paintIcons(bg);
  bg.addEventListener('mousedown', e => { if (e.target === bg) bg.remove(); });
  $$('[data-close]', bg).forEach(b => b.onclick = () => bg.remove());
  return bg;
}
async function openSelectiveExpand(nodes, summary = null, preCat = null) {
  summary = summary || await api('/api/neighbors', { ids: nodes.map(n => n.id()), visible: realNodes().map(n => n.id()) });
  const rows = Object.entries(summary.combos).sort((a, b) => summary.categories[b[0]] - summary.categories[a[0]]).map(([c, ts], i) => `
    <div class="xp-row"><div class="l"><input type="checkbox" data-xcat="${esc(c)}" ${preCat === c || (!preCat && i === 0) ? 'checked' : ''}> ${catChip(c)} nodes via</div>
    <div class="rr"><label><input type="radio" name="xt-${i}" value="" checked> <span class="cat edge">All</span> edges <span class="cnt-pill">${summary.categories[c]}</span></label>
    ${Object.entries(ts).map(([t, n]) => `<label><input type="radio" name="xt-${i}" value="${esc(t)}"> <span class="cat edge">${esc(t)}</span> edges <span class="cnt-pill">${n}</span></label>`).join('')}</div></div>`).join('');
  const bg = modal('Selective expand', `${summary.total > EXPAND_THRESHOLD ? `<div class="warnbox">${ico('AlertCircleOutline')} Expanding more than ${EXPAND_THRESHOLD} nodes at once is overwhelming. Select one or more categories to refine the result.</div>` : ''}
    <label class="row"><input type="checkbox" data-xall="1"> <b>Select all categories</b></label><div style="max-height:46vh;overflow:auto">${rows || '<div class="empty">No hidden neighbors.</div>'}</div>
    <div class="row"><label class="row"><input type="checkbox" data-xlim="1" checked> <b>Limit to</b></label><input type="number" data-xn="1" value="${EXPAND_THRESHOLD}" style="width:70px;padding:5px"><select data-xorder="1" style="padding:5px"><option value="neighbors">neighbors</option><option value="most">most connected neighbors</option><option value="least">least connected neighbors</option></select></div>`,
    `<button class="btn outline" data-close="1">Close</button><button class="btn primary" data-xgo="1">Expand</button>`, { width: 820 });
  $('[data-xall]', bg).onchange = e => $$('[data-xcat]', bg).forEach(c => c.checked = e.target.checked);
  $('[data-xgo]', bg).onclick = () => {
    const cats = $$('[data-xcat]', bg).filter(c => c.checked).map(c => c.dataset.xcat);
    if (!cats.length) return toast('Select at least one category');
    let types = null;
    if (cats.length === 1) { const row = $(`[data-xcat="${CSS.escape(cats[0])}"]`, bg).closest('.xp-row'); const t = $('input[type=radio]:checked', row)?.value; if (t) types = [t]; }
    const cap = EXPAND_THRESHOLD;
    const lim = $('[data-xlim]', bg).checked ? Math.min(cap, +$('[data-xn]', bg).value || cap) : cap;
    bg.remove(); expand(nodes, { categories: cats, types, limit: lim, order: $('[data-xorder]', bg).value });
  };
}
function openAdvancedSearch() {
  closeSearch();
  const gql = sourceIsGraphQL();
  const entityTypes = gql && Array.isArray(S.schema?.entities) && S.schema.entities.length ? [...S.schema.entities].sort() : null;
  const gqlFilters = gql && Array.isArray(S.schema?.filters) && S.schema.filters.length ? [...S.schema.filters].sort() : null;
  const cats = entityTypes || Object.keys(S.schema?.categories || {}).sort(); const types = Object.keys(S.schema?.types || {}).sort();
  const catLabel = entityTypes ? 'Any entity type' : 'Any category';
  const bg = modal('Advanced search', `<div class="row" style="gap:12px;flex-wrap:wrap">
      <input type="text" data-aq="1" value="${esc($('#search-input').value)}" placeholder="Search text" style="flex:1;min-width:200px;padding:7px;border:1px solid #c8c8c8;border-radius:3px">
      <select data-akind="1" style="padding:6px"><option value="node">Nodes</option>${gql ? '' : '<option value="edge">Edges</option>'}</select>
      <select data-acat="1" style="padding:6px"><option value="">${esc(catLabel)}</option>${cats.map(c => `<option>${esc(c)}</option>`).join('')}</select>
      <select data-aprop="1" style="padding:6px"><option value="">Property…</option></select><input type="text" data-aval="1" placeholder="exact value" style="width:130px;padding:7px;border:1px solid #c8c8c8;border-radius:3px">
      ${gql ? '' : '<label class="row" title="Uses the full-text index if configured"><input type="checkbox" data-afz="1"> fuzzy</label>'}</div>
    <div data-ares="1" style="margin-top:10px"></div>`, `<span data-atotal="1" style="margin-right:auto;color:var(--muted);font-size:13px"></span><button class="btn outline" data-apage="-1">‹</button><button class="btn outline" data-apage="1">›</button><button class="btn outline" data-aaddsel="1">Add selected</button><button class="btn primary" data-aadd="1">Add all</button>`, { width: 900 });
  let off = 0, res = [];
  const kindSel = $('[data-akind]', bg), catSel = $('[data-acat]', bg), propSel = $('[data-aprop]', bg);
  const fillCats = () => { const k = kindSel.value; const label = k === 'node' ? catLabel : 'Any type'; catSel.innerHTML = `<option value="">${esc(label)}</option>` + (k === 'node' ? cats : types).map(c => `<option>${esc(c)}</option>`).join(''); fillProps(); };
  const fillProps = () => {
    const props = gqlFilters || (catSel.value && kindSel.value === 'node' ? Object.keys(S.schema.categories[catSel.value]?.props || {}).sort() : []);
    propSel.innerHTML = '<option value="">Property…</option>' + props.map(p => `<option>${esc(p)}</option>`).join('');
  };
  async function go() {
    const k = kindSel.value; const p = new URLSearchParams({ q: $('[data-aq]', bg).value, type: k, limit: 20, offset: off, category: catSel.value, prop: propSel.value, value: $('[data-aval]', bg).value, fuzzy: $('[data-afz]', bg)?.checked ? '1' : '0' });
    try {
      const r = await api('/api/search?' + p);
      res = k === 'node' ? r.nodes : r.edges;
      if (r.total != null) $('[data-atotal]', bg).textContent = `${r.total} result${r.total === 1 ? '' : 's'} · page ${off / 20 + 1}`; else $('[data-atotal]', bg).textContent = `page ${off / 20 + 1}`;
      $('[data-ares]', bg).innerHTML = res.length ? `<table class="sel" style="width:100%"><thead><tr><th></th><th>Caption</th><th>Category</th><th>Matching properties</th></tr></thead><tbody>${res.map((n, i) => `<tr><td><input type="checkbox" data-ai="${i}"></td><td>${esc(k === 'node' ? nodeCaption(n) : n.type)}</td><td>${k === 'node' ? catChip(n.category) : catChip(n.type, true)}</td><td style="white-space:normal;font-size:11px">${propSnippet(n.props, $('[data-aq]', bg).value)}</td></tr>`).join('')}</tbody></table>` : '<div class="empty">No result</div>';
    } catch (e) { $('[data-ares]', bg).innerHTML = `<div class="err">${esc(e.message)}</div>`; }
  }
  const deb = () => { off = 0; clearTimeout(bg._t); bg._t = setTimeout(go, 250); };
  kindSel.onchange = () => { fillCats(); deb(); }; catSel.onchange = () => { fillProps(); deb(); }; propSel.onchange = deb;
  $$('[data-aq],[data-aval]', bg).forEach(i => i.oninput = deb); const fuzzy = $('[data-afz]', bg); if (fuzzy) fuzzy.onchange = deb;
  $$('[data-apage]', bg).forEach(b => b.onclick = () => { off = Math.max(0, off + 20 * +b.dataset.apage); go(); });
  const add = pick => { if (!pick.length) return toast('Nothing selected'); pushUndo(); if (kindSel.value === 'node') addGraph({ nodes: pick, edges: [] }); else addGraph({ nodes: pick.flatMap(e => [e.sourceNode, e.targetNode]), edges: pick }); fetchEdgesAmongVisible(); bg.remove(); runLayout({ fit: true }); };
  $('[data-aadd]', bg).onclick = () => add(res);
  $('[data-aaddsel]', bg).onclick = () => add($$('[data-ai]', bg).filter(c => c.checked).map(c => res[+c.dataset.ai]));
  fillProps();
  go();
}
function openCodeQuery(text = 'MATCH (a:AWSAccount)-[r:RESOURCE]->(b:AWSS3Bucket)\nRETURN a, r, b LIMIT 50') {
  if (!sourceIsNeo4j()) {
    toast(sourceIsGraphQL()
      ? 'Cypher runs on the Neo4j source. Switch the data source to write a statement.'
      : 'Cypher runs on a Neo4j connection.');
    return;
  }
  const bg = modal('Code a query', `<div class="section-d">Read-only Cypher. Write clauses (CREATE, MERGE, SET, DELETE, …) are refused, and every query runs in a READ transaction.</div>
    <textarea class="cypher" data-cq="1" spellcheck="false">${esc(text)}</textarea><div data-cqout="1"></div>`,
    `<input type="text" data-cqname="1" placeholder="Name to save locally" style="margin-right:auto;padding:7px;border:1px solid #c8c8c8;border-radius:3px"><button class="btn outline" data-cqsave="1">Save</button><button class="btn outline" data-cqprev="1">Preview</button><button class="btn primary" data-cqadd="1">Add to visualization</button>`, { width: 820 });
  const out = $('[data-cqout]', bg); const q = () => $('[data-cq]', bg).value;
  $('[data-cqprev]', bg).onclick = async () => { try { const r = await api('/api/cypher', graphBody({ query: q() })); const note = r.truncation?.message ? ` — ${esc(r.truncation.message)}` : ''; out.innerHTML = `<div class="section-d" style="margin-top:8px">${r.nodes.length} nodes · ${r.edges.length} edges · ${r.rows.length} rows${note}</div>` + (r.columns.length ? `<div class="tbl-wrap"><table class="sel"><thead><tr>${r.columns.map(c => `<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${r.rows.slice(0, 20).map(row => `<tr>${row.map(v => `<td>${esc(fmt(v))}</td>`).join('')}</tr>`).join('')}</tbody></table></div>` : ''); } catch (e) { out.innerHTML = `<div class="err">${esc(e.message)}</div>`; } };
  $('[data-cqadd]', bg).onclick = async () => { const ok = await runQueryToGraph(q(), out); if (ok) bg.remove(); };
  $('[data-cqsave]', bg).onclick = () => { const name = $('[data-cqname]', bg).value.trim() || 'Untitled query'; const l = JSON.parse(localStorage.getItem('gx:queries') || '[]'); l.push({ name, cypher: q(), tags: ['local'] }); localStorage.setItem('gx:queries', JSON.stringify(l)); toast('Query saved locally'); if (S.rightPanel === 'queries') renderRightPanel(); };
}
async function runQueryToGraph(cypher, errEl = null) {
  let pushed = false;
  try {
    const want = Math.min(S.budget.cypher_limit || 500, S.budget.nodes);
    pushUndo();
    pushed = true;
    if (S.budget.lru) evictOldest(want, want);
    if (canvasNodes() >= S.budget.nodes) {
      S.undo.pop(); updateUndoBtns(); pushed = false;
      offerCanvasRoom('No room for query results.');
      return false;
    }
    const r = await api('/api/cypher', graphBody({ query: cypher }));
    if (!r.nodes.length) {
      S.undo.pop(); updateUndoBtns(); pushed = false;
      const msg = r.truncation?.message || 'The query returned no nodes.';
      if ((r.truncation?.reasons || []).includes('node_budget')) offerCanvasRoom(msg);
      else if (errEl) errEl.innerHTML = `<div class="err">${esc(msg)}</div>`;
      else toast(msg);
      return false;
    }
    const added = addGraph(r, null, { toastTruncation: false });
    await fetchEdgesAmongVisible();
    if (S.canvasRefused || !added.nodes().length) {
      S.undo.pop(); updateUndoBtns(); pushed = false;
      return false;
    }
    runLayout({ fit: true });
    if (!r.truncation?.message) toast(`Added ${added.nodes().length} nodes from the query`);
    else toast(r.truncation.message, 4500);
    return true;
  } catch (e) {
    if (pushed) { S.undo.pop(); updateUndoBtns(); }
    if (errEl) errEl.innerHTML = `<div class="err">${esc(e.message)}</div>`; else toast(e.message, 4000);
    return false;
  }
}
async function runGraphqlToGraph(query, errEl = null) {
  let pushed = false;
  try {
    const want = Math.min(S.budget.cypher_limit || 500, S.budget.nodes);
    pushUndo();
    pushed = true;
    if (S.budget.lru) evictOldest(want, want);
    if (canvasNodes() >= S.budget.nodes) {
      S.undo.pop(); updateUndoBtns(); pushed = false;
      offerCanvasRoom('No room for query results.');
      return false;
    }
    const r = await api('/api/graphql', graphBody({ document: query.graphql, variables: query.variables || {} }));
    if (!r.nodes.length) {
      S.undo.pop(); updateUndoBtns(); pushed = false;
      const msg = r.truncation?.message || 'The query returned no entities.';
      if ((r.truncation?.reasons || []).includes('node_budget')) offerCanvasRoom(msg);
      else if (errEl) errEl.innerHTML = `<div class="err">${esc(msg)}</div>`;
      else toast(msg);
      return false;
    }
    const added = addGraph(r, null, { toastTruncation: false });
    if (S.canvasRefused || !added.nodes().length) {
      S.undo.pop(); updateUndoBtns(); pushed = false;
      return false;
    }
    runLayout({ fit: true });
    if (!r.truncation?.message) toast(`Added ${added.nodes().length} nodes from the query`);
    else toast(r.truncation.message, 4500);
    return true;
  } catch (e) {
    if (pushed) { S.undo.pop(); updateUndoBtns(); }
    if (errEl) errEl.innerHTML = `<div class="err">${esc(e.message)}</div>`; else toast(e.message, 4000);
    return false;
  }
}
function instanceCountLine() {
  if (!S.db || (S.db.known === false && !S.db.nodesKnown)) return 'instance totals stay on the source until you search or expand';
  if (S.db.known) return `${Number(S.db.nodes || 0).toLocaleString()} nodes and ${Number(S.db.edges || 0).toLocaleString()} edges`;
  const n = Number(S.db.nodes || 0).toLocaleString();
  const nodes = S.db.nodesFloor ? `at least ${n} nodes` : `${n} nodes`;
  return `${nodes}; relationship totals stay on the source`;
}
async function openDataModel() {
  if (document.querySelector('.modal.dm')) return;
  const counts = instanceCountLine();
  const bg = modal('Understand your data model', `<div class="dm-layout"><div class="dm-stage"><div id="dm-cy"></div><div class="dm-wait" data-dm-wait role="status"><span class="land-spin" aria-hidden="true"></span><span><b>Reading the data model…</b><small class="hidden" data-dm-note>A large database can take a minute.</small></span></div></div><div data-dminfo="1"><div class="section-d">Properties appear here.</div></div></div>
    <div class="section-d" data-dm-foot style="margin-top:8px">Whole graph: ${counts}. These are counts, not elements, so this view does not grow with the graph and does not put those nodes on the canvas.</div>`, '', { width: 1100 });
  const box = $('.modal', bg);
  box.classList.add('dm');
  const grow = document.createElement('button');
  grow.className = 'x';
  grow.type = 'button';
  grow.title = 'Expand';
  grow.innerHTML = ico('ArrowExpandAll');
  $('.mh .x', bg).before(grow);
  let dm = null;
  grow.onclick = () => {
    const full = box.classList.toggle('is-full');
    box.style.width = full ? '' : '1100px';
    grow.title = full ? 'Restore' : 'Expand';
    grow.innerHTML = ico(full ? 'ArrowCollapseAll' : 'ArrowExpandAll');
    if (dm) requestAnimationFrame(() => { dm.resize(); dm.fit(undefined, 48); });
  };
  const noteTimer = setTimeout(() => { const n = $('[data-dm-note]', bg); if (n) n.classList.remove('hidden'); }, 3000);
  let sc;
  try { sc = S.schema = await api('/api/schema'); }
  catch (e) {
    clearTimeout(noteTimer);
    if (!bg.isConnected) return;
    const wait = $('[data-dm-wait]', bg);
    if (wait) wait.innerHTML = `<div class="err">${esc(e.message)}</div>`;
    return;
  }
  clearTimeout(noteTimer);
  if (!bg.isConnected) return;
  const foot = $('[data-dm-foot]', bg);
  if (foot) foot.textContent = `Whole graph: ${counts}. These are counts, not elements, so this view does not grow with the graph and does not put those nodes on the canvas. ${Object.keys(sc.categories).length} node categories · ${Object.keys(sc.types).length} relationship types. Click a category for its properties.`;
  const els = [...Object.entries(sc.categories).map(([c, v]) => ({ data: { id: 'c:' + c, label: v.count == null ? c : `${c} (${v.count})`, cat: c } })),
    ...sc.links.filter(l => l.source !== l.target).map((l, i) => ({ data: { id: 'l' + i, source: 'c:' + l.source, target: 'c:' + l.target, label: l.type } }))];
  dm = cytoscape({ container: $('#dm-cy', bg), elements: els, wheelSensitivity: 0.3, style: [
    { selector: 'node', style: { 'background-color': e => catStyle(e.data('cat')).color, label: 'data(label)', 'font-size': 9, 'text-valign': 'bottom', width: 22, height: 22, 'border-width': 1.5, 'border-color': '#fff', 'background-image': e => catStyle(e.data('cat')).icon ? iconUri(catStyle(e.data('cat')).icon) : 'none', 'background-fit': 'contain', 'text-outline-color': '#f8f8f8', 'text-outline-width': 2 } },
    { selector: 'edge', style: { width: 1, 'line-color': '#9e9e9e', 'target-arrow-shape': 'triangle', 'target-arrow-color': '#9e9e9e', 'curve-style': 'bezier', 'arrow-scale': 0.6, label: 'data(label)', 'font-size': 6, color: '#777', 'text-rotation': 'autorotate', 'min-zoomed-font-size': 6 } }],
    layout: { name: 'fcose', animate: false, nodeRepulsion: 12000, idealEdgeLength: 110 } });
  const info = $('[data-dminfo]', bg);
  const show = c => { const v = sc.categories[c]; if (!v) return; info.innerHTML = `${catChip(c)}<div class="section-d" style="margin-top:6px">Labels: ${esc(v.labels.join(', '))}</div><table class="kv">${Object.entries(v.props).sort().map(([k, t]) => `<tr><td>${esc(k)}</td><td style="color:var(--muted)">${esc(t)}</td></tr>`).join('')}</table>`; };
  dm.on('tap', 'node', e => show(e.target.data('cat')));
  const first = Object.keys(sc.categories)[0];
  if (first) show(first);
  const wait = $('[data-dm-wait]', bg);
  if (!wait) return;
  const done = () => wait.remove();
  wait.addEventListener('transitionend', done);
  wait.classList.add('is-done');
  setTimeout(done, 400);
}

// ---------- export ---------------------------------------------------------------------
function download(name, blob) { const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name; document.body.appendChild(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500); }
function csvCell(v) { const s = fmt(v); return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; }
function toCsv(rows, cols) { return [cols.join(','), ...rows.map(r => cols.map(c => csvCell(r[c])).join(','))].join('\n'); }
function exportData(selectedOnly, format) {
  const base = selectedOnly ? cy.$(':selected') : cy.elements(':visible');
  const nodes = base.nodes().filter(n => !n.data('group')), edges = base.edges().filter(e => !e.data('meta'));
  const nrows = nodes.map(n => ({ id: n.id(), categories: n.data('labels').join(';'), caption: nodeCaption(n.data()), ...n.data('props') }));
  const erows = edges.map(e => ({ id: e.id(), type: e.data('type'), source: e.data('source'), target: e.data('target'), ...e.data('props') }));
  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
  if (format === 'json') { download(`graph-${stamp}.json`, new Blob([JSON.stringify({ nodes: nrows, edges: erows }, null, 1)], { type: 'application/json' })); }
  else {
    const ncols = ['id', 'categories', 'caption', ...[...new Set(nrows.flatMap(r => Object.keys(r)))].filter(k => !['id', 'categories', 'caption'].includes(k)).sort()];
    const ecols = ['id', 'type', 'source', 'target', ...[...new Set(erows.flatMap(r => Object.keys(r)))].filter(k => !['id', 'type', 'source', 'target'].includes(k)).sort()];
    download(`nodes-${stamp}.csv`, new Blob([toCsv(nrows, ncols)], { type: 'text/csv' }));
    setTimeout(() => download(`edges-${stamp}.csv`, new Blob([toCsv(erows, ecols)], { type: 'text/csv' })), 300);
  }
  toast(`Exported ${nrows.length} nodes and ${erows.length} edges`);
}
function openExportData(selectedOnly = false) {
  const bg = modal('Export data', `<div class="field"><label><input type="radio" name="xs" value="all" ${selectedOnly ? '' : 'checked'}> All visible data</label><label><input type="radio" name="xs" value="sel" ${selectedOnly ? 'checked' : ''}> Selected data only</label></div>
    <div class="field">Format<select data-xf="1"><option value="csv">CSV (nodes.csv + edges.csv)</option><option value="json">JSON</option></select></div><div class="section-d">Exports raw data only; colors and positions are not included.</div>`,
    `<button class="btn outline" data-close="1">Cancel</button><button class="btn primary" data-xd="1">Download</button>`, { width: 480 });
  $('[data-xd]', bg).onclick = () => { exportData($('input[name=xs]:checked', bg).value === 'sel', $('[data-xf]', bg).value); bg.remove(); };
}
function exportPngBlob(opts) {
  const c = R.png({ full: opts.full, scale: opts.scale, bg: opts.bg ? getComputedStyle(document.body).getPropertyValue('--bg').trim() || '#f8f8f8' : null });
  return new Promise(res => c.toBlob(res, 'image/png'));
}
function openExportImage() {
  const bg = modal('Export image', `<div style="display:flex;gap:18px"><div style="flex:1"><img data-prev="1" style="max-width:100%;max-height:52vh;border:1px solid var(--line);background:repeating-conic-gradient(#eee 0% 25%, #fff 0% 50%) 50%/16px 16px"></div>
    <div style="width:220px"><div class="field">Area<select data-ia="1"><option value="full">Whole graph</option><option value="view">Current view</option></select></div>
    <div class="field">Size<select data-is="1"><option value="1">1x</option><option value="2" selected>2x</option><option value="3">3x</option></select></div>
    <div class="field"><label><input type="checkbox" data-ib="1" checked> Background visible</label></div><div class="section-d">Format: PNG</div></div></div>`,
    `<button class="btn outline" data-close="1">Cancel</button><button class="btn primary" data-idl="1">Export PNG</button>`, { width: 900 });
  const opts = () => ({ full: $('[data-ia]', bg).value === 'full', scale: +$('[data-is]', bg).value, bg: $('[data-ib]', bg).checked });
  const prev = () => { const o = opts(); $('[data-prev]', bg).src = R.png({ full: o.full, scale: 1, bg: o.bg ? '#f8f8f8' : null, maxWidth: 1600, maxHeight: 1200 }).toDataURL('image/png'); };
  $$('select,input', bg).forEach(i => i.onchange = prev); prev();
  $('[data-idl]', bg).onclick = async () => { download(`graph-${Date.now()}.png`, await exportPngBlob(opts())); bg.remove(); };
}

// ---------- save / open (local only) ---------------------------------------------------
function vizList() { return JSON.parse(localStorage.getItem('gx:vizlist') || '[]'); }
function vizState() { return { snap: snapshot(), filters: { cats: [...S.filters.cats], types: [...S.filters.types], vals: Object.fromEntries(Object.entries(S.filters.vals).map(([c, p]) => [c, Object.fromEntries(Object.entries(p).map(([k, s]) => [k, [...s]]))])), nums: S.filters.nums }, activeRule: S.activeRule, edgeGroups: [...S.edgeGroups], layout: S.layout, pan: cy.pan(), zoom: cy.zoom() }; }
function saveViz(asNew = false, title = null) {
  if (!S.viz.id || asNew) { S.viz.id = 'v' + Date.now(); S.viz.title = title || S.viz.title || 'Untitled visualization'; }
  else if (title) S.viz.title = title;
  const list = vizList().filter(v => v.id !== S.viz.id); list.unshift({ id: S.viz.id, title: S.viz.title, updated: new Date().toISOString(), nodes: realNodes().length });
  localStorage.setItem('gx:vizlist', JSON.stringify(list)); localStorage.setItem('gx:viz:' + S.viz.id, JSON.stringify(vizState()));
  S.viz.saved = true; S.viz.autosave = true; updateSaveChip();
}
function openSaveDialog(asNew = false) {
  const bg = modal(asNew ? 'Save as' : 'Save visualization', `<div class="field">Name<input type="text" data-vn="1" value="${esc(asNew ? (S.viz.title || 'Untitled visualization') + ' (copy)' : S.viz.title || 'Untitled visualization')}"></div><div class="section-d">Saved in this browser only (localStorage). Nothing is shared.</div>`,
    `<button class="btn outline" data-close="1">Cancel</button><button class="btn primary" data-vs="1">Save</button>`, { width: 460 });
  $('[data-vs]', bg).onclick = () => { saveViz(asNew || !S.viz.id, $('[data-vn]', bg).value.trim() || 'Untitled visualization'); bg.remove(); toast('Visualization saved'); };
}
function loadViz(id) {
  const st = JSON.parse(localStorage.getItem('gx:viz:' + id) || 'null'); if (!st) return toast('Not found');
  const meta = vizList().find(v => v.id === id);
  S.viz = { id, title: meta?.title || 'Untitled', saved: true, autosave: true };
  S.filters = { cats: new Set(st.filters.cats), types: new Set(st.filters.types), vals: Object.fromEntries(Object.entries(st.filters.vals || {}).map(([c, p]) => [c, Object.fromEntries(Object.entries(p).map(([k, a]) => [k, new Set(a)]))])), nums: st.filters.nums || {} };
  S.activeRule = st.activeRule; S.edgeGroups = new Set(st.edgeGroups || []); S.layout = st.layout || S.layout; S.undo = []; S.redo = []; updateUndoBtns();
  restore(st.snap); cy.viewport({ zoom: st.zoom, pan: st.pan }); updateSaveChip();
}
function openVizDialog() {
  const list = vizList();
  const bg = modal('Open visualization', list.length ? `<table class="sel" style="width:100%"><thead><tr><th>Name</th><th>Nodes</th><th>Updated</th><th></th></tr></thead><tbody>${list.map(v => `<tr data-vo="${esc(v.id)}"><td>${esc(v.title)}</td><td>${v.nodes}</td><td>${esc(new Date(v.updated).toLocaleString())}</td><td><button class="btn link small" data-vdel="${esc(v.id)}">Delete</button></td></tr>`).join('')}</tbody></table>` : '<div class="empty">No saved visualization in this browser yet.</div>', '', { width: 640 });
  $$('[data-vo]', bg).forEach(tr => tr.onclick = e => { if (e.target.dataset.vdel) { localStorage.removeItem('gx:viz:' + e.target.dataset.vdel); localStorage.setItem('gx:vizlist', JSON.stringify(vizList().filter(v => v.id !== e.target.dataset.vdel))); bg.remove(); openVizDialog(); return; } bg.remove(); loadViz(tr.dataset.vo); });
}
function newViz() { S.viz = { id: null, title: null, saved: false, autosave: true }; S.undo = []; S.redo = []; cy.elements().remove(); resetFilters(); afterGraphChange(); updateSaveChip(); updateUndoBtns(); }
let dirtyT;
function markDirty() { if (S.viz.saved && S.viz.autosave) { clearTimeout(dirtyT); dirtyT = setTimeout(() => saveViz(), 700); } }
function updateSaveChip() {
  const c = $('#save-chip'), t = $('#viz-title');
  t.classList.toggle('hidden', !S.viz.saved); t.textContent = S.viz.title || '';
  if (!S.viz.saved) { c.className = 'chip warn'; c.innerHTML = ico('AlertCircleOutline') + 'Not saved'; }
  else if (S.viz.autosave) { c.className = 'chip ok'; c.innerHTML = ico('CheckCircle') + 'Auto save ON'; }
  else { c.className = 'chip warn'; c.innerHTML = ico('AlertCircleOutline') + 'Auto save OFF'; }
}

// ---------- popovers (design) ----------------------------------------------------------
const PALETTE = ['#3b78bc', '#5c9bd6', '#1662dd', '#4fb8c9', '#389704', '#8bc34a', '#2e7d32', '#b5b000', '#f37805', '#f7ba75', '#ffb300', '#c62828', '#e57373', '#e377c2', '#caabde', '#9575cd', '#5e35b1', '#835543', '#bcaaa4', '#4a4a4a', '#9e9e9e', '#bdbdbd', '#24292f', '#17becf'];
function popover(anchor, html) {
  closeMenus(true); const p = document.createElement('div'); p.className = 'pop'; p.innerHTML = html; document.body.appendChild(p); paintIcons(p);
  const r = anchor.getBoundingClientRect(); p.style.left = Math.max(8, Math.min(r.left - p.offsetWidth + r.width, innerWidth - p.offsetWidth - 8)) + 'px'; p.style.top = Math.min(r.bottom + 4, innerHeight - p.offsetHeight - 8) + 'px';
  setTimeout(() => document.addEventListener('mousedown', function h(e) { if (!p.contains(e.target)) { p.remove(); document.removeEventListener('mousedown', h); } }), 0);
  return p;
}
function colorPop(anchor, current, onPick) {
  const p = popover(anchor, `<div class="palette">${PALETTE.map(c => `<button style="background:${c}" data-c="${c}"></button>`).join('')}</div><input type="text" value="${esc(current)}" style="width:100%;padding:4px" placeholder="#hex, rgb(), name"><div style="margin-top:6px;text-align:right"><button class="btn link small" data-c="__reset">Reset</button></div>`);
  $$('[data-c]', p).forEach(b => b.onclick = () => { onPick(b.dataset.c === '__reset' ? null : b.dataset.c); p.remove(); });
  $('input', p).onkeydown = e => { if (e.key === 'Enter') { onPick(e.target.value.trim()); p.remove(); } };
}
function iconPop(anchor, onPick) {
  const names = Object.keys(window.ICONS).filter(n => !/^(Menu|Arrow|Chevron|Close|Minus|Plus|Dots|Check|Sort|Refresh|Download|Content|Eye|Format|Palette|Resize|Selection|Select$)/.test(n));
  const p = popover(anchor, `<div style="font-weight:500;margin-bottom:4px">Icons library</div><div class="icon-grid">${names.map(n => `<button data-n="${n}" title="${n}">${ico(n)}</button>`).join('')}</div><div style="text-align:right"><button class="btn link small" data-n="__none">No icon</button></div>`);
  $$('[data-n]', p).forEach(b => b.onclick = () => { onPick(b.dataset.n === '__none' ? null : b.dataset.n); p.remove(); });
}
function sizePop(anchor, cat) {
  const st = catStyle(cat); const props = Object.entries(S.schema?.categories?.[cat]?.props || {}).filter(([, t]) => t === 'int' || t === 'float').map(([k]) => k).sort();
  const p = popover(anchor, `<div class="field">Static size<select data-ss="1">${[50, 75, 100, 125, 150, 200, 300].map(v => `<option ${st.size === v ? 'selected' : ''}>${v}</option>`).join('')}</select></div>
    <div class="field">Dynamic size by property<select data-sp="1"><option value="">— none —</option>${props.map(k => `<option ${st.sizeRule?.prop === k ? 'selected' : ''}>${esc(k)}</option>`).join('')}</select>
    <select data-sc="1"><option value="linear">Linear</option><option value="log" ${st.sizeRule?.scale === 'log' ? 'selected' : ''}>Logarithmic</option></select></div><button class="btn primary small" data-sok="1">Apply</button>`);
  $('[data-sok]', p).onclick = () => { st.size = +$('[data-ss]', p).value; const pr = $('[data-sp]', p).value; st.sizeRule = pr ? { prop: pr, scale: $('[data-sc]', p).value } : null; p.remove(); restyle(); renderRightPanel(); };
}
function widthPop(anchor, type) {
  const st = edgeStyle(type); const props = new Set(); realEdges().filter(e => e.data('type') === type).forEach(e => Object.entries(e.data('props') || {}).forEach(([k, v]) => { if (typeof v === 'number') props.add(k); }));
  const p = popover(anchor, `<div class="field">Static width<select data-ws="1">${[1, 2, 3, 4, 6].map(v => `<option ${st.width === v ? 'selected' : ''}>${v}</option>`).join('')}</select></div>
    <div class="field">Dynamic width by property<select data-wp="1"><option value="">— none —</option>${[...props].sort().map(k => `<option ${st.widthRule?.prop === k ? 'selected' : ''}>${esc(k)}</option>`).join('')}</select><select data-wsc="1"><option value="linear">Linear</option><option value="log">Logarithmic</option></select></div><button class="btn primary small" data-wok="1">Apply</button>`);
  $('[data-wok]', p).onclick = () => { st.width = +$('[data-ws]', p).value; const pr = $('[data-wp]', p).value; if (pr) { const vs = realEdges().map(e => Math.abs(Number(e.data('props')?.[pr]))).filter(v => !isNaN(v)); st.widthRule = { prop: pr, scale: $('[data-wsc]', p).value, max: Math.max(1, ...vs) }; } else st.widthRule = null; p.remove(); restyle(); renderRightPanel(); };
}
function openRuleDialog() {
  const cats = S.schema?.categories || {}; const props = [...new Set(Object.values(cats).flatMap(c => Object.keys(c.props)))].filter(p => !['firstseen', 'lastupdated', 'id'].includes(p)).sort();
  const bg = modal('Create a node grouping rule', `<div class="field">Title<input type="text" data-rt="1" value="Region"></div><div class="field">Group nodes sharing the same value of<select data-rp="1">${props.map(p => `<option ${p === 'region' ? 'selected' : ''}>${esc(p)}</option>`).join('')}</select></div><div class="field">Categories<div data-rc="1" style="max-height:200px;overflow:auto;border:1px solid var(--line);padding:6px;border-radius:3px"></div></div>`,
    `<button class="btn outline" data-close="1">Cancel</button><button class="btn primary" data-rok="1">Create</button>`, { width: 520 });
  const fill = () => { const p = $('[data-rp]', bg).value; $('[data-rc]', bg).innerHTML = Object.keys(cats).filter(c => p in cats[c].props).sort().map(c => `<label class="row"><input type="checkbox" value="${esc(c)}" checked> ${catChip(c)}</label>`).join('') || '<div class="empty">No category has this property.</div>'; };
  $('[data-rp]', bg).onchange = fill; fill();
  $('[data-rok]', bg).onclick = () => { const c = $$('[data-rc] input:checked', bg).map(i => i.value); if (!c.length) return toast('Pick at least one category'); S.rules.push({ id: 'r' + Date.now(), title: $('[data-rt]', bg).value || 'Group', prop: $('[data-rp]', bg).value, categories: c }); saveRules(); bg.remove(); renderRightPanel(); };
}
function openHelp() {
  const k = (a, b) => `<tr><td>${a.split('+').map(x => `<span class="kbd">${x}</span>`).join(' + ')}</td><td>${b}</td></tr>`;
  modal('Help', `<div style="display:flex;gap:24px"><div style="flex:1"><div class="section-t">Keyboard shortcuts</div><table class="kv">
    ${k('←/↑/→/↓', 'Move camera')}${k('+', 'Zoom in')}${k('-', 'Zoom out')}${k('Ctrl+click', 'Add/remove from selection')}${k('Ctrl+A', 'Select all nodes')}${k('Ctrl+Backspace', 'Remove selection from visualization')}
    ${k('Ctrl+drag', 'Lasso')}${k('Ctrl+Shift+drag', 'Edge lasso')}${k('Shift+click', 'Select direct neighbors')}${k('Ctrl+C', 'Copy selected nodes')}${k('Ctrl+V', 'Paste copied nodes')}${k('Ctrl+Z', 'Undo')}${k('Ctrl+Y', 'Redo')}${k('Double-click', 'Expand node / toggle group')}${k('Esc', 'Clear selection')}</table></div>
    <div style="flex:1"><div class="section-t">Connection</div><table class="kv" data-conn="1"><tr><td>Loading…</td></tr></table><div class="section-d" style="margin-top:10px">Unofficial Explore-phase prototype inspired by a commercial graph-investigation UI. Not affiliated with or endorsed by its vendor. Read-only. Search, expand, and the data model follow the source you pick. Cypher and saved queries run on Neo4j.</div></div></div>`, '', { width: 900 });
  api('/api/status').then(s => { const t = $('[data-conn]'); if (t) t.innerHTML = Object.entries(s).filter(([a]) => !/token|authorization|password|secret/i.test(a)).map(([a, b]) => `<tr><td>${esc(a)}</td><td>${esc(redactSecrets(b && typeof b === 'object' ? JSON.stringify(b) : String(b)))}</td></tr>`).join(''); }).catch(e => { const t = $('[data-conn]'); if (t) t.innerHTML = `<tr><td class="err">${esc(e.message)}</td></tr>`; });
}
function sourceMenuItem() {
  if (!S.sources || S.sources.length < 2) return null;
  return { label: 'Data source', icon: 'DatabaseOutline', sub: S.sources.map(s => ({ label: `${s.id === S.source?.id ? '✓ ' : ''}${s.label}`, act: () => switchSource(s.id) })) };
}
function mainMenuItems() {
  const source = sourceMenuItem();
  return [
    { label: 'New visualization', icon: 'FileOutline', act: newViz },
    { label: 'Open…', icon: 'FolderOpenOutline', act: openVizDialog },
    { label: 'Save…', icon: 'ContentSave', act: () => S.viz.id ? (saveViz(), toast('Saved')) : openSaveDialog() },
    { label: 'Save as…', icon: 'ContentCopy', act: () => openSaveDialog(true) },
    { label: S.viz.autosave ? 'Switch auto-save off' : 'Switch auto-save on', disabled: !S.viz.saved, act: () => { S.viz.autosave = !S.viz.autosave; updateSaveChip(); } },
    '-', { label: 'Export', icon: 'Download', sub: [{ label: 'Image (PNG)…', icon: 'ImageOutline', act: openExportImage }, { label: 'Data (CSV, JSON)…', icon: 'Table', act: () => openExportData(false) }] },
    ...(source ? ['-', source] : []),
    '-', { label: 'Understand your data model', icon: 'DatabaseOutline', act: openDataModel },
    { label: 'Neo4j connection…', icon: 'DatabaseOutline', act: () => openConnection('neo4j') },
    { label: 'GraphQL connection…', icon: 'GraphOutline', act: () => openConnection('graphql') },
    { label: 'Diagnostics…', icon: 'FileDocumentOutline', act: openDiagnostics },
    { label: 'Canvas budget…', icon: 'Cog', act: openBudgetSettings },
    { label: document.body.classList.contains('dark') ? 'Light theme' : 'Dark theme', icon: 'ThemeLightDark', act: toggleTheme },
    { label: 'Keyboard shortcuts & help', icon: 'KeyboardOutline', act: openHelp },
  ];
}
function toggleTheme() { document.body.classList.toggle('dark'); localStorage.setItem('gx:theme', document.body.classList.contains('dark') ? 'dark' : 'light'); R.redraw(); drawMinimap(); }

// ---------- wiring ---------------------------------------------------------------------
const ACTIONS = {
  'main-menu': el => { const r = el.getBoundingClientRect(); showMenu(mainMenuItems(), r.left, r.bottom + 6); },
  'more-menu': el => { const r = el.getBoundingClientRect(); showMenu(backgroundMenuItems(), r.left, r.bottom + 6); },
  'undo': undo, 'redo': redo, 'code-query': () => openCodeQuery(), 'explore-queries': () => { if (!sourceIsNeo4j() && !sourceIsGraphQL()) { toast('Add a data connection to open its queries.'); return; } openPanel('queries'); }, 'data-model': openDataModel, 'help': openHelp,
  'connection-neo4j': () => openConnection('neo4j'),
  'connection-graphql': () => openConnection('graphql'),
  'diagnostics': openDiagnostics,
  'focus-search': () => $('#search-input').focus(), 'advanced-search': openAdvancedSearch, 'budget': openBudgetSettings,
  'zoom-in': () => zoomBy(1.35), 'zoom-out': () => zoomBy(1 / 1.35),
  'locate': () => { const s = cy.$(':selected'); R.animate({ fit: s.length ? s.union(s.connectedNodes()) : cy.elements(':visible'), padding: 90 }); },
  'minimap': () => { S.minimap = !S.minimap; $('#minimap').classList.toggle('hidden', !S.minimap); $('#btn-minimap').classList.toggle('on', S.minimap); drawMinimap(); },
  'open-selpanel': () => { S.selPanelOpen = true; updateHighlight(); },
};
const USABLE_WHILE_BOOTING = new Set(['help', 'focus-search', 'budget', 'connection-neo4j', 'connection-graphql', 'diagnostics']);
document.addEventListener('click', e => {
  const a = e.target.closest('[data-act]');
  if (a) {
    e.preventDefault();
    if (!S.ready && !USABLE_WHILE_BOOTING.has(a.dataset.act)) { toast('Initializing…'); return; }
    ACTIONS[a.dataset.act]?.(a);
    return;
  }
  const p = e.target.closest('#righttb [data-panel]');
  if (p) { if (!S.ready) { toast('Initializing…'); return; } openPanel(p.dataset.panel); return; }
  if (!e.target.closest('.menu') && e !== S.menuEvent) closeMenus();
  if (!e.target.closest('#search')) closeSearch();
});
document.addEventListener('contextmenu', e => { if (!e.target.closest('#cy')) return; e.preventDefault(); });
$('#save-chip').onclick = () => S.viz.saved ? (S.viz.autosave = !S.viz.autosave, updateSaveChip(), toast(S.viz.autosave ? 'Auto-save ON' : 'Auto-save OFF: modifications are not saved')) : openSaveDialog();

// right panel delegation
$('#rightpanel').addEventListener('click', e => {
  const t = e.target.closest('[data-rp],[data-ftab],[data-open],[data-ftog],[data-fsel],[data-unf],[data-f],[data-dtab],[data-dsub],[data-icon-pick],[data-color],[data-size],[data-vcolor],[data-ecolor],[data-ewidth],[data-capdel],[data-ecapall],[data-d],[data-pref],[data-lay],[data-lfast],[data-lroot],[data-lrun],[data-gdel],[data-gtog],[data-gnew],[data-egtog],[data-qrun]');
  if (!t) return; const d = t.dataset;
  if (d.rp) return openPanel(S.rightPanel);
  if (d.ftab) { RP.filterTab = d.ftab; return renderRightPanel(); }
  if (d.open) { RP.open.has(d.open) ? RP.open.delete(d.open) : RP.open.add(d.open); return renderRightPanel(); }
  if (d.ftog) {
    const [k, a, b, c] = d.ftog.split('|');
    if (k === 'cat') S.filters.cats.has(a) ? S.filters.cats.delete(a) : S.filters.cats.add(a);
    if (k === 'type') S.filters.types.has(a) ? S.filters.types.delete(a) : S.filters.types.add(a);
    if (k === 'val') { const s = ((S.filters.vals[a] = S.filters.vals[a] || {})[b] = S.filters.vals[a][b] || new Set()); s.has(c) ? s.delete(c) : s.add(c); }
    applyFilters(); drawOverlay(); markDirty(); return renderRightPanel();
  }
  if (d.fsel) { const [k, a, b, c] = d.fsel.split('|'); selectOnly(k === 'type' ? realEdges().filter(x => x.data('type') === a) : realNodes().filter(n => n.data('category') === a && (k === 'cat' || (n.data('props')?.[b] === undefined ? '∅' : fmt(n.data('props')[b])) === c))); return; }
  if (d.unf) { const [k, a, b, c] = d.unf.split('|'); if (k === 'cat') S.filters.cats.delete(a); if (k === 'type') S.filters.types.delete(a); if (k === 'val') S.filters.vals[a]?.[b]?.delete(c); if (k === 'num') delete S.filters.nums[a]?.[b]; applyFilters(); drawOverlay(); return renderRightPanel(); }
  if (d.f === 'reset') return resetFilters();
  if (d.dtab) { RP.designTab = d.dtab; return renderRightPanel(); }
  if (d.dsub) { RP.designSub = d.dsub; return renderRightPanel(); }
  if (d.iconPick) return iconPop(t, n => { catStyle(d.iconPick).icon = n; restyle(); renderRightPanel(); });
  if (d.color) return colorPop(t, catStyle(d.color).color, c => { catStyle(d.color).color = c || S.cfg.categories[d.color]?.color || '#9e9e9e'; restyle(); renderRightPanel(); });
  if (d.size) return sizePop(t, d.size);
  if (d.vcolor) { const [c, p, v] = d.vcolor.split('|'); return colorPop(t, catStyle(c).color, col => { const m = ((catStyle(c).valueColors = catStyle(c).valueColors || {})[p] = catStyle(c).valueColors[p] || {}); col ? m[v] = col : delete m[v]; restyle(); renderRightPanel(); }); }
  if (d.ecolor) return colorPop(t, edgeStyle(d.ecolor).color, c => { edgeStyle(d.ecolor).color = c || S.cfg.edges.colors[d.ecolor] || S.cfg.edges.default_color; restyle(); renderRightPanel(); });
  if (d.ewidth) return widthPop(t, d.ewidth);
  if (d.capdel) { const [c, p] = d.capdel.split('|'); catStyle(c).caption = (catStyle(c).caption || []).filter(x => x !== p); restyle(); return renderRightPanel(); }
  if (d.ecapall) { S.edgeCaptionsAlways = !S.edgeCaptionsAlways; restyle(); return renderRightPanel(); }
  if (d.d === 'gear') return showMenu([
    { label: 'Neo4j connection…', icon: 'DatabaseOutline', act: () => openConnection('neo4j') },
    { label: 'GraphQL connection…', icon: 'GraphOutline', act: () => openConnection('graphql') },
    { label: 'Diagnostics…', icon: 'FileDocumentOutline', act: openDiagnostics },
    { label: 'Canvas budget…', icon: 'Cog', act: openBudgetSettings },
    { label: 'Reset styles to default', icon: 'Refresh', act: () => { localStorage.removeItem('gx:styles'); initStyles(true); restyle(); renderRightPanel(); toast('Styles reset to defaults'); } },
  ], t.getBoundingClientRect().left - 220, t.getBoundingClientRect().top - 50);
  if (d.pref) { S.prefs[d.pref] = !S.prefs[d.pref]; localStorage.setItem('gx:prefs', JSON.stringify(S.prefs)); return renderRightPanel(); }
  if (d.lay && !e.target.closest('select,button.btn,.switch')) { S.layout.name = d.lay; return renderRightPanel(); }
  if (d.lfast) { S.layout.fast = !S.layout.fast; return renderRightPanel(); }
  if (d.lroot) { const n = cy.$('node:selected').first(); if (n.empty()) return toast('Select a node first'); S.layout.root = n.id(); return renderRightPanel(); }
  if (d.lrun) { pushUndo(); return runLayout({ fit: true }); }
  if (d.gdel) { S.rules = S.rules.filter(r => r.id !== d.gdel); if (S.activeRule === d.gdel) { S.activeRule = null; applyNodeGrouping(); afterGraphChange(); } saveRules(); return renderRightPanel(); }
  if (d.gtog) { S.activeRule = S.activeRule === d.gtog ? null : d.gtog; applyNodeGrouping(); afterGraphChange(); return; }
  if (d.gnew) return openRuleDialog();
  if (d.egtog) { S.edgeGroups.has(d.egtog) ? S.edgeGroups.delete(d.egtog) : S.edgeGroups.add(d.egtog); afterGraphChange(); return; }
  if (d.qrun) { const [k, i] = d.qrun.split('|'); const q = k === 'b' ? S.queries[+i] : JSON.parse(localStorage.getItem('gx:queries') || '[]')[+i]; if (!q) return; if (q.graphql) return runGraphqlToGraph(q); return runQueryToGraph(q.cypher); }
});
$('#rightpanel').addEventListener('change', e => {
  const d = e.target.dataset;
  if (d.fnum) { const [c, p, w] = d.fnum.split('|'); const b = ((S.filters.nums[c] = S.filters.nums[c] || {})[p] = S.filters.nums[c][p] || {}); b[w] = e.target.value === '' ? null : +e.target.value; if (b.min == null && b.max == null) delete S.filters.nums[c][p]; applyFilters(); drawOverlay(); renderRightPanel(); }
  if (d.cap) { const [c, p] = d.cap.split('|'); const st = catStyle(c); st.caption = e.target.checked ? [...(st.caption || []), p] : (st.caption || []).filter(x => x !== p); restyle(); renderRightPanel(); }
  if (d.ecap) { const [t, p] = d.ecap.split('|'); const st = edgeStyle(t); st.caption = e.target.checked ? [...(st.caption || []), p] : (st.caption || []).filter(x => x !== p); restyle(); renderRightPanel(); }
  if (d.lorient) { S.layout.orientation = e.target.value; }
});

// left panel delegation
$('#leftpanel').addEventListener('click', e => {
  const t = e.target.closest('[data-lp],[data-goto],[data-more],[data-jump],[data-sort],[data-page],tr[data-row]'); if (!t) return; e.preventDefault(); const d = t.dataset;
  const sel = cy.$(':selected');
  if (d.lp === 'close') { S.selPanelOpen = false; return updateHighlight(); }
  if (d.lp === 'locate') return ACTIONS.locate();
  if (d.lp === 'expand') { const r = t.getBoundingClientRect(); const ns = sel.nodes().filter(n => !n.data('group')); if (!ns.length) return; showMenu([{ label: 'Expand', icon: 'ArrowExpandAll', sub: () => expandSubmenu(ns) }], r.right + 4, r.top); return; }
  if (d.lp === 'remove') return removeEles(sel);
  if (d.lp === 'export') return openExportData(true);
  if (d.lp === 'more') { const r = t.getBoundingClientRect(); const n = sel.nodes().first(); showMenu(n.nonempty() ? nodeMenuItems(n) : [{ label: 'Remove from visualization', icon: 'MinusCircleOutline', act: () => removeEles(sel) }], r.right + 4, r.top); return; }
  if (d.lp === 'design') { openPanel('design'); return; }
  if (d.goto) { const el = cy.getElementById(d.goto); if (el.nonempty()) { selectOnly(el); R.animate({ center: el.position(), zoom: cy.zoom() }); } return; }
  if (d.more) { const v = cy.$(':selected').first().data('props')[d.more]; t.parentElement.textContent = fmt(v); return; }
  if (d.jump) { $(`[data-sec="${CSS.escape(d.jump)}"]`)?.scrollIntoView({ behavior: 'smooth' }); return; }
  if (d.sort) { const [k, c] = d.sort.split('|'); const cur = TBL.sort[k]; TBL.sort[k] = { col: c, dir: cur && cur.col === c ? -cur.dir : 1 }; return renderLeftPanel(); }
  if (d.page) { const [k, p] = d.page.split('|'); TBL.page[k] = +p; return renderLeftPanel(); }
  if (d.row) { const el = cy.getElementById(d.row); if (e.detail >= 2) R.animate({ center: el.position(), zoom: Math.max(cy.zoom(), 1.4) }); else pulse(el); }
});

// search box
const sInput = $('#search-input');
sInput.addEventListener('focus', () => { $('#search-dd').classList.remove('hidden'); runSearch(); });
sInput.addEventListener('input', () => { $('#search-dd').classList.remove('hidden'); clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 180); });
sInput.addEventListener('keydown', e => {
  const rows = $$('#search-results .sr'); let i = rows.findIndex(r => r.classList.contains('active'));
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); rows[i]?.classList.remove('active'); i = Math.max(0, Math.min(rows.length - 1, i + (e.key === 'ArrowDown' ? 1 : -1))); rows[i]?.classList.add('active'); rows[i]?.scrollIntoView({ block: 'nearest' }); }
  if (e.key === 'Enter') { e.preventDefault(); clearTimeout(searchTimer); runSearch().then(() => pickSearch(Math.max(0, i))); }
  if (e.key === 'Escape') { closeSearch(); sInput.blur(); }
});
$('#search-results').addEventListener('click', e => { const r = e.target.closest('.sr'); if (r) pickSearch(+r.dataset.i); });
$('#search-clear').onclick = () => { sInput.value = ''; runSearch(); sInput.focus(); };
$$('.sd-head [data-kind]').forEach(a => a.onclick = () => { S.searchKind = a.dataset.kind; $$('.sd-head [data-kind]').forEach(x => x.classList.toggle('on', x === a)); runSearch(); });
$$('.sd-head [data-scope]').forEach(a => a.onclick = () => { S.searchScope = a.dataset.scope; $$('.sd-head [data-scope]').forEach(x => x.classList.toggle('on', x === a)); runSearch(); });

// keyboard
document.addEventListener('keydown', e => {
  if (e.target.closest('input,textarea,select')) return;
  const mod = e.ctrlKey || e.metaKey; const k = e.key;
  if (!S.ready) { if (k === 'Escape') { closeMenus(); $$('.modal-bg').forEach(m => m.remove()); } return; }
  if (k === 'Escape') { closeMenus(); $$('.modal-bg').forEach(m => m.remove()); cy.$(':selected').unselect(); S.lassoMode = null; return; }
  if ($('.modal-bg')) return;
  const pan = { ArrowLeft: [60, 0], ArrowRight: [-60, 0], ArrowUp: [0, 60], ArrowDown: [0, -60] }[k];
  if (pan && !mod) { e.preventDefault(); cy.panBy({ x: pan[0], y: pan[1] }); return; }
  if (!mod && (k === '+' || k === '=')) return ACTIONS['zoom-in']();
  if (!mod && k === '-') return ACTIONS['zoom-out']();
  if (mod && k.toLowerCase() === 'a') { e.preventDefault(); return cy.nodes(':visible').select(); }
  if (mod && (k === 'Backspace' || k === 'Delete')) { e.preventDefault(); const s = cy.$(':selected'); if (s.length) removeEles(s); return; }
  if (mod && k.toLowerCase() === 'z') { e.preventDefault(); return e.shiftKey ? redo() : undo(); }
  if (mod && k.toLowerCase() === 'y') { e.preventDefault(); return redo(); }
  if (mod && k.toLowerCase() === 'c') { const s = cy.$('node:selected').filter(n => !n.data('group')); if (!s.length) return; const clip = { nodes: s.map(n => ({ id: n.id(), labels: n.data('labels'), category: n.data('category'), props: n.data('props'), degree: n.data('degree') })), edges: s.edgesWith(s).map(x => ({ id: x.id(), type: x.data('type'), source: x.data('source'), target: x.data('target'), props: x.data('props') })) }; localStorage.setItem('gx:clip', JSON.stringify(clip)); toast(`Copied ${clip.nodes.length} nodes`); return; }
  if (mod && k.toLowerCase() === 'v') { const clip = JSON.parse(localStorage.getItem('gx:clip') || 'null'); if (!clip) return; pushUndo(); const a = addGraph(clip); fetchEdgesAmongVisible(); if (a.nodes().length) runLayout({ onlyNew: a.nodes(), name: 'force' }); toast(`Pasted ${a.nodes().length} nodes`); }
});

// lasso
function pointInPoly(p, poly) { let c = false; for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) { const a = poly[i], b = poly[j]; if ((a.y > p.y) !== (b.y > p.y) && p.x < (b.x - a.x) * (p.y - a.y) / (b.y - a.y) + a.x) c = !c; } return c; }
function startLasso(oe, edge) {
  S.lasso = { pts: [{ x: oe.clientX, y: oe.clientY }], edge }; cy.userPanningEnabled(false);
  const move = ev => { S.lasso.pts.push({ x: ev.clientX, y: ev.clientY }); drawOverlay(); };
  const up = () => {
    window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up);
    const poly = S.lasso.pts; const edgeMode = S.lasso.edge; S.lasso = null; S.lassoMode = null; cy.userPanningEnabled(true);
    if (poly.length > 2) {
      const hit = edgeMode ? cy.edges(':visible').filter(x => { const a = x.source().renderedPosition(), b = x.target().renderedPosition(); const cr = $('#cy').getBoundingClientRect(); return pointInPoly({ x: (a.x + b.x) / 2 + cr.left, y: (a.y + b.y) / 2 + cr.top }, poly); }) : cy.nodes(':visible').filter(n => { if (n.isParent()) return false; const p = n.renderedPosition(); const cr = $('#cy').getBoundingClientRect(); return pointInPoly({ x: p.x + cr.left, y: p.y + cr.top }, poly); });
      if (!(oe.shiftKey && !edgeMode)) cy.$(':selected').unselect(); hit.select(); toast(`${hit.length} ${edgeMode ? 'edges' : 'nodes'} selected`);
    }
    drawOverlay();
  };
  window.addEventListener('mousemove', move); window.addEventListener('mouseup', up);
}

function detectGpu() {
  try {
    const c = document.createElement('canvas');
    const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
    if (!gl) return { kind: 'unknown', renderer: '' };
    const ext = gl.getExtension('WEBGL_debug_renderer_info');
    const renderer = String(ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : (gl.getParameter(gl.RENDERER) || ''));
    const software = /swiftshader|llvmpipe|softpipe|software|basic render/i.test(renderer);
    return { kind: software ? 'software' : (renderer ? 'hardware' : 'unknown'), renderer };
  } catch (e) { return { kind: 'unknown', renderer: '' }; }
}
function clampBudget(v, ceiling, fallback) {
  const n = parseInt(v, 10);
  const pick = n > 0 ? n : fallback;
  return Math.max(1, Math.min(ceiling, pick || ceiling));
}
async function fetchSuggestion() {
  const gpu = detectGpu();
  try {
    return await api('/api/budget/suggest', {
      gpu: gpu.kind, renderer: gpu.renderer,
      cpu: navigator.hardwareConcurrency || 0,
      device_memory_gb: navigator.deviceMemory || 0,
    });
  } catch (e) {
    const s = S.budget.suggestion || {};
    return { nodes: s.nodes || S.budget.serverNodes, edges: s.edges || S.budget.serverEdges, expand_limit: s.expand_limit || S.budget.expand_max, cypher_limit: s.cypher_limit || S.budget.cypher_max, reason: s.reason || '' };
  }
}
function applyBudgetNumbers(saved) {
  const b = S.budget;
  b.nodes = clampBudget(saved.nodes, b.serverNodes, b.serverNodes);
  b.edges = clampBudget(saved.edges, b.serverEdges, b.serverEdges);
  b.expand_limit = clampBudget(saved.expand_limit, b.expand_max, Math.min(50, b.expand_max));
  b.cypher_limit = clampBudget(saved.cypher_limit, b.cypher_max, Math.min(500, b.cypher_max));
  b.lru = !!saved.lru;
  EXPAND_THRESHOLD = b.expand_limit;
  HUB_THRESHOLD = b.expand_limit;
}
async function applyServerBudget(st) {
  const b = st.budget || {};
  S.budget.serverNodes = b.max_nodes || S.budget.serverNodes;
  S.budget.serverEdges = b.max_edges || S.budget.serverEdges;
  S.budget.expand_max = b.expand_limit || S.budget.expand_max;
  S.budget.cypher_max = b.cypher_limit || S.budget.cypher_max;
  S.budget.resources = b.resources || null;
  S.budget.suggestion = b.suggestion || null;
  S.budget.configFile = b.config_file || 'gx.local.json';
  let cfg = b.config || null;
  let announce = !!b.config_created;
  // The file is written at startup from CPU and RAM. The first browser open adds the GPU.
  if (cfg && cfg.source === 'auto' && (!cfg.gpu || cfg.gpu === 'unknown')) {
    const gpu = detectGpu();
    if (gpu.kind && gpu.kind !== 'unknown') {
      try {
        const refined = await api('/api/budget/refine', {
          gpu: gpu.kind, renderer: gpu.renderer,
          cpu: navigator.hardwareConcurrency || 0,
          device_memory_gb: navigator.deviceMemory || 0,
        });
        if (refined.config) cfg = refined.config;
        if (refined.updated) announce = true;
      } catch (e) { /* keep the file the server already wrote */ }
    }
  }
  if (!cfg || !(parseInt(cfg.nodes, 10) > 0)) {
    const sug = b.suggestion || {};
    cfg = {
      nodes: sug.nodes || S.budget.serverNodes, edges: sug.edges || S.budget.serverEdges,
      expand_limit: sug.expand_limit, cypher_limit: sug.cypher_limit, lru: false, reason: sug.reason || '',
    };
    announce = true;
  }
  S.budget.reason = cfg.reason || '';
  applyBudgetNumbers(cfg);
  rememberCounts(st);
  updateBudgetChip();
  if (announce) toast(`Working set set for this machine: ${S.budget.nodes.toLocaleString()} nodes, ${S.budget.edges.toLocaleString()} edges`, 4500);
}
function openBudgetSettings() {
  const b = S.budget;
  const file = b.configFile || 'gx.local.json';
  const hw = b.resources ? `${b.resources.cpu} CPU cores, ${Number(b.resources.ram_gb).toLocaleString()} GB RAM on this machine.` : '';
  const bg = modal('Canvas budget', `<div class="section-d">Saved in ${esc(file)} and used again the next time the server starts. This server accepts up to ${b.serverNodes.toLocaleString()} nodes and ${b.serverEdges.toLocaleString()} edges. The rest of the graph stays on the source.</div>
    <div class="section-d" data-breason="1">${esc(b.reason || hw)}</div>
    <div class="field">Nodes<input type="number" data-bn="1" min="1" max="${b.serverNodes}" value="${b.nodes}"></div>
    <div class="field">Edges<input type="number" data-be="1" min="1" max="${b.serverEdges}" value="${b.edges}"></div>
    <div class="field">Neighbors per expand<input type="number" data-bx="1" min="1" max="${b.expand_max}" value="${b.expand_limit}"></div>
    <div class="field">Rows per query<input type="number" data-bc="1" min="1" max="${b.cypher_max}" value="${b.cypher_limit}"></div>
    <label class="row"><input type="checkbox" data-bl="1" ${b.lru ? 'checked' : ''}> When the canvas is full, drop the oldest nodes that are not pinned</label>`,
    `<button class="btn outline" data-bsug="1">Suggest for this machine</button><button class="btn outline" data-close="1">Cancel</button><button class="btn primary" data-bsave="1">Save</button>`, { width: 560 });
  $('[data-bsug]', bg).onclick = async () => {
    const s = await fetchSuggestion();
    bg._suggestion = s;
    $('[data-bn]', bg).value = s.nodes;
    $('[data-be]', bg).value = s.edges;
    $('[data-bx]', bg).value = s.expand_limit;
    $('[data-bc]', bg).value = s.cypher_limit;
    $('[data-breason]', bg).textContent = s.reason || '';
  };
  $('[data-bsave]', bg).onclick = async () => {
    const saved = {
      nodes: clampBudget($('[data-bn]', bg).value, b.serverNodes, b.nodes),
      edges: clampBudget($('[data-be]', bg).value, b.serverEdges, b.edges),
      expand_limit: clampBudget($('[data-bx]', bg).value, b.expand_max, b.expand_limit),
      cypher_limit: clampBudget($('[data-bc]', bg).value, b.cypher_max, b.cypher_limit),
      lru: $('[data-bl]', bg).checked,
    };
    const sug = bg._suggestion;
    const same = (x, y) => Number(x) === Number(y);
    const unchanged = same(saved.nodes, b.nodes) && same(saved.edges, b.edges) && same(saved.expand_limit, b.expand_limit) && same(saved.cypher_limit, b.cypher_limit);
    const suggested = sug && same(sug.nodes, saved.nodes) && same(sug.edges, saved.edges) && same(sug.expand_limit, saved.expand_limit) && same(sug.cypher_limit, saved.cypher_limit);
    saved.reason = suggested ? (sug.reason || '') : (unchanged ? (b.reason || '') : '');
    try {
      const res = await api('/api/budget', saved);
      const cfg = res.config || saved;
      S.budget.reason = cfg.reason || '';
      applyBudgetNumbers(cfg);
      updateBudgetChip(); bg.remove();
      toast(`Canvas budget: ${S.budget.nodes.toLocaleString()} nodes, ${S.budget.edges.toLocaleString()} edges`);
    } catch (e) { toast('Could not save the working set: ' + e.message, 4000); }
  };
}

function connectionNote(file) {
  return `Saved in ${esc(file || '.env')}, which git ignores. The canvas budget stays in gx.local.json. The password, client secret, and token are not written there.`;
}
function canvasDirty() {
  return window.cy && typeof cy.nodes === 'function' && cy.nodes().length > 0;
}
async function refreshConnectionLinks() {
  try { S.connection = await api('/api/connection'); }
  catch (e) { return; }
  const n = S.connection.neo4j || {};
  const g = S.connection.graphql || {};
  const neo = document.querySelector('[data-neo-label]');
  const gql = document.querySelector('[data-gql-label]');
  if (neo) neo.textContent = n.password_set ? 'Neo4j connection' : 'Add Neo4j';
  if (gql) gql.textContent = g.endpoint ? 'GraphQL connection' : 'Add GraphQL';
}
async function refreshAfterConnectionSave(st) {
  try {
    await applyServerBudget(st);
    try { S.queries = await api('/api/queries'); } catch { S.queries = []; }
    if (window.cy && typeof cy.nodes === 'function') {
      try { S.schema = await api('/api/schema'); }
      catch (e) { S.schema = { categories: {}, types: {}, links: [] }; }
      initStyles();
      if (window.R) restyle();
      if (S.rightPanel) renderRightPanel();
    }
    await refreshConnectionLinks();
  } catch (e) {
    toast(e.message, 4000);
  }
}
async function finishConnectionSave(bg, res) {
  if (canvasDirty()) {
    cy.elements().remove();
    S.undo = []; S.redo = [];
    afterGraphChange();
  }
  bg.remove();
  toast('Connection saved');
  const st = res.status || res;
  try { paintSource(st); } catch (e) { toast(e.message, 4000); }
  refreshAfterConnectionSave(st);
}
async function showConnectionTest(bg, body) {
  const out = $('[data-cresult]', bg);
  if (out) out.textContent = 'Testing…';
  try {
    const res = await api('/api/connection/test', body);
    if (out) out.textContent = redactSecrets(res.message || (res.ok ? 'Connected.' : 'The connection failed.'));
  } catch (e) {
    if (out) out.textContent = redactSecrets(e.message);
  }
}
function wizAuthDefault(endpoint) {
  let host = '';
  try { host = new URL(endpoint).hostname.toLowerCase(); } catch (e) { return ''; }
  if (host === 'wiz.us' || host.endsWith('.wiz.us')) return 'https://auth.app.wiz.us/oauth/token';
  if (host === 'wiz.io' || host.endsWith('.wiz.io')) return 'https://auth.app.wiz.io/oauth/token';
  return '';
}
async function openConnection(kind) {
  let cur = S.connection;
  try { cur = await api('/api/connection'); S.connection = cur; }
  catch (e) { toast(e.message, 4000); return; }
  if (kind === 'graphql') openGraphQLConnection(cur);
  else openNeo4jConnection(cur);
}
function openNeo4jConnection(cur) {
  const n = cur.neo4j || {};
  const bg = modal('Neo4j connection', `<div class="section-d">${connectionNote(cur.file)}</div>
    <div class="field">URI<input type="text" data-cu="1" value="${esc(n.uri || 'bolt://localhost:7687')}" autocomplete="off" spellcheck="false"></div>
    <div class="field">User<input type="text" data-cuser="1" value="${esc(n.user || 'neo4j')}" autocomplete="off" spellcheck="false"></div>
    <div class="field">Password<input type="password" data-cpw="1" value="" autocomplete="new-password" placeholder="${n.password_set ? 'Saved — leave blank to keep it' : 'Required'}"></div>
    <label class="row"><input type="checkbox" data-cnone="1"> No password</label>
    <div class="field">Database<input type="text" data-cdb="1" value="${esc(n.database || 'neo4j')}" autocomplete="off" spellcheck="false"></div>
    <pre class="conn-result" data-cresult></pre>`,
    `<button class="btn outline" data-close="1">Cancel</button><button class="btn outline" data-ctest="1">Test</button><button class="btn primary" data-csave="1">Save</button>`, { width: 480 });
  const none = $('[data-cnone]', bg);
  const pw = $('[data-cpw]', bg);
  none.onchange = () => { pw.disabled = none.checked; if (none.checked) pw.value = ''; };
  const payload = () => {
    const neo = {
      uri: $('[data-cu]', bg).value.trim(),
      user: $('[data-cuser]', bg).value.trim(),
      database: $('[data-cdb]', bg).value.trim(),
    };
    if (none.checked) neo.password = '';
    else if (pw.value) neo.password = pw.value;
    return { kind: 'neo4j', neo4j: neo };
  };
  $('[data-ctest]', bg).onclick = () => showConnectionTest(bg, payload());
  $('[data-csave]', bg).onclick = async () => {
    if (bg._saving) return;
    if (canvasDirty() && !confirm('Saving the connection clears the canvas.')) return;
    bg._saving = true;
    try {
      await finishConnectionSave(bg, await api('/api/connection', { neo4j: payload().neo4j }));
    } catch (e) {
      bg._saving = false;
      toast('Could not save the connection: ' + e.message, 5000);
    }
  };
}
function openGraphQLConnection(cur) {
  const g = cur.graphql || {};
  const bg = modal('GraphQL connection', `<div class="section-d">${connectionNote(cur.file)} Wiz uses the client id and client secret to request an access token. The query sends that token only.</div>
    <div class="field">Endpoint<input type="text" data-ge="1" value="${esc(g.endpoint || '')}" placeholder="https://api.us2.app.wiz.io/graphql" autocomplete="off" spellcheck="false"></div>
    <div class="field">Client ID<input type="text" data-gcid="1" value="${esc(g.client_id || '')}" autocomplete="off" spellcheck="false"></div>
    <div class="field">Client secret<input type="password" data-gsec="1" value="" autocomplete="new-password" placeholder="${g.secret_set ? 'Saved — leave blank to keep it' : 'Wiz client secret'}"></div>
    <div class="field">Auth URL<input type="text" data-gauth="1" value="${esc(g.auth_url || '')}" placeholder="Filled in for a Wiz host" autocomplete="off" spellcheck="false"></div>
    <details class="conn-more"><summary>Bearer token, label, and project</summary>
      <div class="field">Label<input type="text" data-gl="1" value="${esc(g.label || '')}" placeholder="Wiz" autocomplete="off"></div>
      <div class="field">Adapter<select data-ga="1"><option value="" ${g.adapter ? '' : 'selected'}>From the host name</option><option value="wiz" ${g.adapter === 'wiz' ? 'selected' : ''}>Wiz</option><option value="generic" ${g.adapter === 'generic' ? 'selected' : ''}>Generic</option></select></div>
      <div class="field">Bearer token<input type="password" data-gt="1" value="" autocomplete="new-password" placeholder="${g.token_set ? 'Saved — leave blank to keep it' : 'Only when you already have a token'}"></div>
      <div class="field">Token variable<input type="text" data-gtenv="1" value="${esc(g.token_env || 'GRAPHQL_TOKEN')}" autocomplete="off" spellcheck="false"></div>
      <div class="field">Project<input type="text" data-gp="1" value="${esc(g.project_id || '')}" placeholder="*" autocomplete="off" spellcheck="false"></div>
      ${g.endpoint ? '<label class="row"><input type="checkbox" data-gclear="1"> Remove this GraphQL source</label>' : ''}
    </details>
    <pre class="conn-result" data-cresult></pre>`,
    `<button class="btn outline" data-close="1">Cancel</button><button class="btn outline" data-ctest="1">Test</button><button class="btn primary" data-csave="1">Save</button>`, { width: 520 });
  const endpoint = $('[data-ge]', bg);
  const auth = $('[data-gauth]', bg);
  const fillAuth = () => {
    if (auth.dataset.touched) return;
    const suggested = wizAuthDefault(endpoint.value.trim());
    if (suggested) auth.value = suggested;
  };
  if (auth.value) auth.dataset.touched = '1';
  else fillAuth();
  endpoint.addEventListener('input', fillAuth);
  auth.addEventListener('input', () => { auth.dataset.touched = '1'; });
  const payload = () => {
    const graphql = {
      endpoint: endpoint.value.trim(),
      label: $('[data-gl]', bg).value.trim(),
      adapter: $('[data-ga]', bg).value,
      token_env: $('[data-gtenv]', bg).value.trim(),
      project_id: $('[data-gp]', bg).value.trim(),
      client_id: $('[data-gcid]', bg).value.trim(),
      auth_url: auth.value.trim(),
    };
    const token = $('[data-gt]', bg).value;
    const secret = $('[data-gsec]', bg).value;
    if (token) graphql.token = token;
    if (secret) graphql.client_secret = secret;
    const clear = $('[data-gclear]', bg);
    if (clear && clear.checked) graphql.clear = true;
    return { kind: 'graphql', graphql };
  };
  $('[data-ctest]', bg).onclick = () => showConnectionTest(bg, payload());
  $('[data-csave]', bg).onclick = async () => {
    if (bg._saving) return;
    const body = payload().graphql;
    if (!body.clear && !body.endpoint) { toast('Enter a GraphQL endpoint'); return; }
    if (canvasDirty() && !confirm('Saving the connection clears the canvas.')) return;
    bg._saving = true;
    try {
      await finishConnectionSave(bg, await api('/api/connection', { graphql: body }));
    } catch (e) {
      bg._saving = false;
      toast('Could not save the connection: ' + e.message, 5000);
    }
  };
}
async function openDiagnostics() {
  const bg = modal('Diagnostics', `<div class="section-d">Queries shows the GraphQL document, its variables, and the response. Server is the HTTP listener. Explorer is auth and connection tests. Secrets are removed. Drag the title to move this window, or expand it for a larger log. Select the text to copy it.</div>
    <div class="diag-bar"><label>Show <select data-dsource><option value="query">Queries</option><option value="">All</option><option value="server">Server</option><option value="explorer">Explorer</option></select></label><button class="btn outline small" data-drefresh="1">Refresh</button></div>
    <textarea class="diag-log" data-dlog readonly spellcheck="false"></textarea>`,
    `<button class="btn outline" data-close="1">Close</button>`, { width: 820 });
  const dlg = $('.modal', bg);
  dlg.classList.add('diag');
  const head = $('.mh', dlg);
  head.title = 'Drag to move';
  const grow = document.createElement('button');
  grow.className = 'x';
  grow.type = 'button';
  grow.title = 'Expand';
  grow.setAttribute('data-dexpand', '1');
  grow.innerHTML = ico('ArrowExpandAll');
  $('.mh .x', bg).before(grow);
  let parked = null;
  grow.onclick = () => {
    const full = dlg.classList.toggle('is-full');
    if (full) {
      parked = { left: dlg.style.left, top: dlg.style.top, width: dlg.style.width, height: dlg.style.height, moved: dlg.classList.contains('is-moved') };
      dlg.classList.remove('is-moved');
      dlg.style.left = '';
      dlg.style.top = '';
      dlg.style.width = '';
      dlg.style.height = '';
    } else if (parked) {
      dlg.style.width = parked.width || '820px';
      dlg.style.height = parked.height;
      if (parked.moved) {
        dlg.classList.add('is-moved');
        dlg.style.left = parked.left;
        dlg.style.top = parked.top;
      }
    }
    grow.title = full ? 'Restore' : 'Expand';
    head.title = full ? '' : 'Drag to move';
    grow.innerHTML = ico(full ? 'ArrowCollapseAll' : 'ArrowExpandAll');
  };
  head.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || e.target.closest('button') || dlg.classList.contains('is-full')) return;
    const rect = dlg.getBoundingClientRect();
    if (!dlg.classList.contains('is-moved')) {
      dlg.classList.add('is-moved');
      dlg.style.left = `${rect.left}px`;
      dlg.style.top = `${rect.top}px`;
    }
    const ox = e.clientX - rect.left;
    const oy = e.clientY - rect.top;
    head.setPointerCapture(e.pointerId);
    const move = (ev) => {
      const left = Math.min(Math.max(72 - dlg.offsetWidth, ev.clientX - ox), window.innerWidth - 72);
      const top = Math.min(Math.max(8, ev.clientY - oy), window.innerHeight - 40);
      dlg.style.left = `${left}px`;
      dlg.style.top = `${top}px`;
    };
    const up = () => {
      head.removeEventListener('pointermove', move);
      head.removeEventListener('pointerup', up);
      head.removeEventListener('pointercancel', up);
    };
    head.addEventListener('pointermove', move);
    head.addEventListener('pointerup', up);
    head.addEventListener('pointercancel', up);
    e.preventDefault();
  });
  const load = async () => {
    const source = $('[data-dsource]', bg).value;
    const box = $('[data-dlog]', bg);
    try {
      const res = await api('/api/diagnostics' + (source ? `?source=${encodeURIComponent(source)}` : ''));
      const lines = (res.entries || []).map(e => {
        const body = redactSecrets(e.message);
        return body.includes('\n') ? `${e.t}  ${e.source}\n${body}` : `${e.t}  ${e.source}  ${body}`;
      });
      box.value = lines.join('\n\n') || (source === 'query'
        ? 'No queries yet. Run a search or a saved query, then Refresh.'
        : 'No log lines yet.');
      box.scrollTop = box.scrollHeight;
    } catch (e) {
      box.value = redactSecrets(e.message);
    }
  };
  $('[data-dsource]', bg).onchange = load;
  $('[data-drefresh]', bg).onclick = load;
  load();
}

// ---------- boot -----------------------------------------------------------------------
function rememberCounts(st) {
  S.db = {
    nodes: st.nodes == null ? 0 : Number(st.nodes) || 0,
    edges: st.edges == null ? 0 : Number(st.edges) || 0,
    nodesKnown: st.nodes != null,
    nodesFloor: !!st.nodes_floor,
    known: st.nodes != null && st.edges != null,
  };
}
function connectionText(st) {
  const src = st.source || { label: 'No connection', kind: 'none' };
  if (src.kind === 'none') return st.error ? redactSecrets(st.error) : 'No data connection. Use Add Neo4j or Add GraphQL.';
  if (src.kind === 'graphql') {
    let counts = 'instances load as you explore';
    if (st.nodes != null && st.edges == null) {
      const n = Number(st.nodes).toLocaleString();
      counts = st.nodes_floor ? `at least ${n} nodes` : `${n} nodes`;
    } else if (st.nodes != null && st.edges != null) {
      counts = `${Number(st.nodes).toLocaleString()} nodes · ${Number(st.edges).toLocaleString()} edges`;
    }
    return `${src.label} · ${st.endpoint || ''} · ${counts} · read-only${st.error ? ' · ' + redactSecrets(st.error) : ''}`;
  }
  if (st.nodes == null) return `${st.database || 'Neo4j'} · read-only${st.error ? ' · ' + redactSecrets(st.error) : ''}`;
  return `${st.server || ''} · ${st.database || ''} · ${Number(st.nodes || 0).toLocaleString()} nodes · ${Number(st.edges || 0).toLocaleString()} edges · read-only`;
}
function paintSource(st) {
  S.status = st;
  S.source = st.source || { id: 'neo4j', label: 'Neo4j', kind: 'neo4j' };
  S.sources = st.sources || [S.source];
  rememberCounts(st);
  const text = redactSecrets(connectionText(st));
  const conn = $('#conn');
  if (conn) conn.textContent = text;
  const input = $('#search-input');
  if (input) input.title = text;
  const gql = S.source.kind === 'graphql';
  const neo = S.source.kind === 'neo4j';
  const kicker = $('#land-kicker');
  if (kicker) kicker.textContent = gql ? `Read-only ${S.source.label}` : (neo ? 'Read-only Neo4j' : 'No data connection');
  const lead = $('#land-lead');
  if (lead) lead.textContent = gql
    ? 'Search a node, open a saved query, or open the data model. Results stay in this view. Nothing is copied into Neo4j.'
    : (neo
      ? 'Search a node, open a saved query, or write Cypher. Each result adds one neighborhood to the canvas. The database stays read-only.'
      : 'Add Neo4j or GraphQL. Nothing is loaded until a connection is saved.');
  const note = $('#land-cypher-note');
  if (note) note.textContent = neo ? 'Write one read-only statement.' : 'Cypher runs on a Neo4j connection.';
  const landNodes = $('#land-nodes');
  if (landNodes) {
    landNodes.textContent = st.nodes == null ? '—' : Number(st.nodes).toLocaleString();
    landNodes.title = st.nodes == null
      ? 'Instances load when you search or expand.'
      : (st.nodes_floor ? 'The source stopped counting. This number is a lower bound.' : '');
    $('#land-edges').textContent = st.edges == null ? '—' : Number(st.edges || 0).toLocaleString();
    $('#land-db').textContent = gql ? (S.source.label || '') : (neo ? (st.database || '') : '—');
  }
  const err = $('#land-error');
  if (err) {
    const text = st.error ? redactSecrets(st.error) : '';
    err.textContent = text;
    err.classList.toggle('hidden', !text);
  }
  syncSourceActions();
  fillSourceSelects();
}
function syncSourceActions() {
  const neo = S.source && S.source.kind === 'neo4j';
  document.querySelectorAll('[data-neo4j-only]').forEach(el => el.classList.toggle('hidden', !neo));
  if (!neo && S.searchKind === 'edge') {
    S.searchKind = 'node';
    $$('.sd-head [data-kind]').forEach(x => x.classList.toggle('on', x.dataset.kind === 'node'));
  }
}
function fillSourceSelects() {
  const many = (S.sources || []).length > 1;
  for (const sel of ['#source-pick', '#source-bar'].map(s => $(s)).filter(Boolean)) {
    const current = S.source?.id || 'neo4j';
    sel.innerHTML = (S.sources || []).map(s => `<option value="${esc(s.id)}">${esc(s.label)}</option>`).join('');
    sel.value = current;
    sel.disabled = !S.ready;
    sel.classList.toggle('hidden', !many);
  }
  const lab = $('#land-source');
  if (lab) lab.classList.toggle('hidden', !many);
}
let sourceSwitching = false;
let sourceWanted = null;
async function switchSource(id) {
  if (!id) { fillSourceSelects(); return; }
  // A second change while the schema is loading used to be ignored, and the
  // picker snapped back. Keep the latest choice and apply it when this one ends.
  if (sourceSwitching) { sourceWanted = id; return; }
  if (S.source && id === S.source.id) { fillSourceSelects(); return; }
  const dirty = window.cy && typeof cy.nodes === 'function' && cy.nodes().length > 0;
  if (dirty && !confirm('Switching the data source clears the canvas. The other source is left as it is.')) {
    sourceWanted = null;
    fillSourceSelects();
    return;
  }
  sourceSwitching = true;
  const stopWait = typeof armLandWait === 'function' ? armLandWait() : () => {};
  try {
    const st = await api('/api/source', { id });
    try { S.queries = await api('/api/queries'); } catch { S.queries = []; }
    if (dirty) {
      cy.elements().remove();
      S.undo = []; S.redo = [];
      afterGraphChange();
    }
    paintSource(st);
    try { S.schema = await api('/api/schema'); }
    catch (e) { S.schema = { categories: {}, types: {}, links: [] }; toast(e.message, 5000); }
    initStyles();
    if (window.R) restyle();
    // Design reads the schema already in memory. Replace it when the source changes.
    if (S.rightPanel) renderRightPanel();
    toast(S.source.label);
  } catch (e) {
    toast(e.message, 4000);
    fillSourceSelects();
  } finally {
    sourceSwitching = false;
    stopWait();
    const next = sourceWanted;
    sourceWanted = null;
    if (next && (!S.source || next !== S.source.id)) switchSource(next);
  }
}
async function boot() {
  paintIcons(); if (localStorage.getItem('gx:theme') === 'dark') document.body.classList.add('dark');
  sizeOverlay();
  for (const sel of ['#source-pick', '#source-bar']) {
    const node = $(sel);
    if (node) node.addEventListener('change', () => switchSource(node.value));
  }
  S.cfg = await api('/api/styles');
  try { S.queries = await api('/api/queries'); } catch { S.queries = []; }
  try {
    const st = await api('/api/status');
    paintSource(st);
    await applyServerBudget(st);
    await refreshConnectionLinks();
  }
  catch (e) {
    $('#conn').innerHTML = `<span class="err">Cannot reach the data source: ${esc(e.message)}</span>`;
    const landMeta = $('#land-meta');
    if (landMeta) { landMeta.classList.add('is-msg'); landMeta.textContent = 'The data source is not reachable.'; }
  }
  const stopWait = armLandWait();
  try {
  try { S.schema = await api('/api/schema'); } catch { S.schema = { categories: {}, types: {}, links: [] }; }
  initStyles();
  cy = cytoscape({ headless: true, styleEnabled: true, elements: [], style: cyStyle(), minZoom: 0.01, maxZoom: 6 });
  window.cy = cy;
  R = GXRender.createRenderer(cy, $('#cy'), VISUALS); window.R = R; R.wheelSensitivity = 0.8;
  let hlT; cy.on('select unselect', () => { clearTimeout(hlT); hlT = setTimeout(updateHighlight, 10); });
  cy.on('viewport', () => { drawOverlay(); clearTimeout(cy._mm); cy._mm = setTimeout(drawMinimap, 80); });
  cy.on('tap', 'node', e => { if (e.originalEvent?.shiftKey) setTimeout(() => { const base = cy.$('node:selected').union(e.target); base.union(base.neighborhood().nodes()).select(); }, 0); });
  cy.on('dbltap', 'node', e => { const n = e.target; if (n.data('group')) toggleGroup(n.id()); else expandDefault(n); });
  cy.on('dragfree', 'node', e => { if (S.prefs.pinOnDrag) { e.target.data('pinned', true); drawOverlay(); } markDirty(); });
  cy.on('cxttap', e => {
    const oe = e.originalEvent; const x = oe.clientX, y = oe.clientY;
    if (e.target === cy) return showMenu(backgroundMenuItems(), x, y);
    if (e.target.isNode()) { if (!e.target.selected()) selectOnly(e.target); return showMenu(nodeMenuItems(e.target), x, y); }
    if (!e.target.selected()) selectOnly(e.target);
    showMenu([{ label: 'Select source and target', icon: 'Select', act: () => e.target.connectedNodes().select() }, { label: 'Remove from visualization', icon: 'MinusCircleOutline', act: () => removeEles(cy.$('edge:selected')) }, '-', { label: 'Export selection data', icon: 'Download', act: () => openExportData(true) }], x, y);
  });
  cy.on('tapstart', e => { if (e.target !== cy) return; const oe = e.originalEvent; if (!oe || oe.button === 2) return; if (oe.ctrlKey || oe.metaKey || S.lassoMode) startLasso(oe, S.lassoMode === 'edge' || ((oe.ctrlKey || oe.metaKey) && oe.shiftKey)); });
  $('#minimap').addEventListener('mousedown', ev => { const t = $('#minimap')._t; if (!t) return; const r = ev.target.getBoundingClientRect(); const mx = (ev.clientX - r.left - t.ox) / t.s, my = (ev.clientY - r.top - t.oy) / t.s; R.animate({ center: { x: mx, y: my }, zoom: cy.zoom() }, 200); });
  window.addEventListener('resize', () => { sizeOverlay(); R.resize(); drawOverlay(); });
  updateUndoBtns(); updateSaveChip(); afterGraphChange();
  S.ready = true;
  fillSourceSelects();
  const q = new URLSearchParams(location.search);
  if (q.get('viz')) loadViz(q.get('viz'));
  } finally {
    stopWait();
  }
}

// Shown only when the data-model read outlasts a quick start. The counts are
// already on the card; this is the stretch before the canvas exists.
function armLandWait() {
  const land = document.querySelector('.land');
  const reveal = setTimeout(() => {
    if (land) land.classList.add('is-booting');
    const el = $('#land-wait');
    if (el) el.classList.remove('hidden');
  }, 400);
  const note = setTimeout(() => {
    const n = $('#land-wait-note');
    if (n) n.classList.remove('hidden');
  }, 3000);
  return () => {
    clearTimeout(reveal);
    clearTimeout(note);
    if (land) land.classList.remove('is-booting');
    const el = $('#land-wait');
    if (el) el.classList.add('hidden');
    const n = $('#land-wait-note');
    if (n) n.classList.add('hidden');
  };
}
boot();
