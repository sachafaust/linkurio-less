/* GX renderer — a Canvas2D renderer + pointer interaction for a *headless* cytoscape model.
 *
 * Why: cytoscape's own renderer computes a full style per element and redraws everything; with
 * 10k+ elements it ran at ~1 FPS on a CPU-only machine. Here cytoscape stays the graph model
 * (collections, selectors, events, selection state, layouts) and this file draws it with
 * level-of-detail rendering:
 *   - spatial grid: only elements inside the viewport are visited
 *   - LOD by on-screen node radius: dots -> flat circles (batched per color) -> full nodes
 *     (icon, label, badges, selection ring, neighbourhood halo)
 *   - label decluttering on a screen grid; labels/arrows/curves dropped when zoomed out
 *   - "texture on viewport": while panning/zooming a big graph, the last frame is transformed
 *     and the full frame is redrawn once interaction pauses
 * Events emitted on the cytoscape model mimic cytoscape's (tap, dbltap, cxttap, tapstart,
 * grab/drag/free/dragfree, mouseover/mouseout) so app code keeps using cy.on(...).
 */
(function () {
  const TAU = Math.PI * 2;

  function createRenderer(cy, container, V) {
    const canvas = document.createElement('canvas'); canvas.className = 'gx-canvas';
    container.appendChild(canvas);
    const ctx = canvas.getContext('2d');
    const cache = document.createElement('canvas'); const cctx = cache.getContext('2d');
    let W = 0, H = 0, DPR = 1;
    const R = { canvas, minZoom: 0.01, maxZoom: 6, wheelSensitivity: 1, stats: { frames: 0, lastMs: 0, mode: '' } };

    // ---- viewport plumbing: cytoscape's headless core asks width()/height() for fit/extent
    cy.width = () => W; cy.height = () => H;
    function resize() {
      const r = container.getBoundingClientRect(); DPR = window.devicePixelRatio || 1;
      W = Math.max(1, Math.round(r.width)); H = Math.max(1, Math.round(r.height));
      for (const c of [canvas, cache]) { c.width = W * DPR; c.height = H * DPR; }
      canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
      full();
    }
    R.resize = resize;

    // ---- spatial grid over node positions (rebuilt lazily) --------------------------------
    const CELL = 160; let grid = new Map(), gridDirty = true, nodeList = [], edgeList = [], parentList = [], listsDirty = true;
    function rebuildLists() { edgeList = cy.edges().toArray(); parentList = cy.nodes(':parent').toArray(); listsDirty = false; }
    function rebuildGrid() {
      grid = new Map(); nodeList = []; if (listsDirty) rebuildLists();
      cy.nodes().forEach(n => {
        if (n.isParent()) return;
        nodeList.push(n);
        const p = n._private.position; const k = cellKey(Math.floor(p.x / CELL), Math.floor(p.y / CELL));
        let a = grid.get(k); if (!a) grid.set(k, (a = [])); a.push(n);
      });
      gridDirty = false;
    }
    function cellKey(cx, cyy) { return cx * 73856093 ^ cyy * 19349663; }
    function nodesIn(x1, y1, x2, y2) {
      if (gridDirty) rebuildGrid();
      const cx1 = Math.floor(x1 / CELL), cx2 = Math.floor(x2 / CELL), cy1 = Math.floor(y1 / CELL), cy2 = Math.floor(y2 / CELL);
      if ((cx2 - cx1 + 1) * (cy2 - cy1 + 1) > grid.size) return nodeList; // viewport covers everything
      const out = [];
      for (let cx = cx1; cx <= cx2; cx++) for (let cyy = cy1; cyy <= cy2; cyy++) { const a = grid.get(cellKey(cx, cyy)); if (a) for (const n of a) out.push(n); }
      return out;
    }
    R.positionsChanged = () => { gridDirty = true; full(); };

    // ---- draw scheduling -------------------------------------------------------------------
    let lastFullMs = 0; let queued = false, needFull = true, idleT = null, interacting = false, cacheVP = null, lastVisibleCount = 0;
    function full() { needFull = true; request(); }
    function request() { if (!queued) { queued = true; requestAnimationFrame(frame); } }
    R.redraw = full;
    function viewportChanged() {
      // big graphs: transform the cached frame while the viewport moves, redraw fully when it settles
      if ((lastVisibleCount > 3000 || lastFullMs > 12) && cacheVP) { /* adaptive: any frame slower than ~12 ms pans the cached bitmap */ interacting = true; clearTimeout(idleT); idleT = setTimeout(() => { interacting = false; full(); }, 140); request(); }
      else full();
    }
    function frame() {
      queued = false; const t0 = performance.now();
      if (interacting && cacheVP && !needFull) {
        const z = cy.zoom(), p = cy.pan(), k = z / cacheVP.z;
        ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.setTransform(k, 0, 0, k, (p.x - cacheVP.p.x * k) * DPR, (p.y - cacheVP.p.y * k) * DPR);
        ctx.drawImage(cache, 0, 0);
        R.stats.mode = 'cached';
      } else {
        needFull = false;
        drawScene(ctx, W, H, DPR, cy.zoom(), cy.pan(), false);
        cctx.setTransform(1, 0, 0, 1, 0, 0); cctx.clearRect(0, 0, cache.width, cache.height); cctx.drawImage(canvas, 0, 0);
        cacheVP = { z: cy.zoom(), p: { ...cy.pan() } }; lastFullMs = performance.now() - t0;
        R.stats.mode = 'full';
      }
      R.stats.frames++; R.stats.lastMs = performance.now() - t0;
      if (R.onFrame) R.onFrame();
    }

    // ---- helpers ---------------------------------------------------------------------------
    const iconCache = new Map();
    function iconSprite(name, color, px) {
      const b = Math.max(8, Math.ceil(px / 4) * 4), key = name + color + b; let c = iconCache.get(key);
      if (c) return c;
      const path = (window.ICONS || {})[name]; if (!path) return null;
      c = document.createElement('canvas'); c.width = c.height = b * 2; const g = c.getContext('2d');
      g.scale(b * 2 / 24, b * 2 / 24); g.fillStyle = color; g.fill(new Path2D(path)); iconCache.set(key, c);
      if (iconCache.size > 3000) iconCache.delete(iconCache.keys().next().value);
      return c;
    }
    const tw = new Map();
    function textWidth(g, s, font) { const k = font + s; let w = tw.get(k); if (w === undefined) { g.font = font; w = g.measureText(s).width; tw.set(k, w); if (tw.size > 20000) tw.clear(); } return w; }
    function visibleEle(e) { const c = e._private.classes; return !(c.has('grp-hidden') || c.has('egrp-hidden')); }
    R.isVisible = visibleEle;

    // ---- the scene -------------------------------------------------------------------------
    function drawScene(g, w, h, dpr, z, pan, exporting, bgColor) {
      const T = V.theme();
      g.setTransform(1, 0, 0, 1, 0, 0); g.clearRect(0, 0, w * dpr, h * dpr);
      if (bgColor) { g.fillStyle = bgColor; g.fillRect(0, 0, w * dpr, h * dpr); }
      g.setTransform(dpr * z, 0, 0, dpr * z, dpr * pan.x, dpr * pan.y);
      const pad = 60 / z;
      const x1 = -pan.x / z - pad, y1 = -pan.y / z - pad, x2 = (w - pan.x) / z + pad, y2 = (h - pan.y) / z + pad;
      const vis = []; for (const n of nodesIn(x1, y1, x2, y2)) { const p = n._private.position; if (p.x >= x1 && p.x <= x2 && p.y >= y1 && p.y <= y2 && visibleEle(n)) vis.push(n); }
      lastVisibleCount = vis.length;
      const visSet = new Set(vis);
      const baseR = V.defaultSize() / 2, rs = baseR * z;   // on-screen radius of a default node
      let lod = rs < 1.6 ? 0 : rs < 5 ? 1 : 2; if (R.fastMode && !exporting) lod = Math.min(lod, 1); // while a layout runs: no labels/icons
      R.stats.visibleNodes = vis.length; R.stats.lod = lod;

      // compound parents (open node groups) first
      parentList.forEach(p => {
        const ch = p.children().filter(visibleEle); if (!ch.length) return;
        let bx1 = Infinity, by1 = Infinity, bx2 = -Infinity, by2 = -Infinity;
        ch.forEach(c => { const q = c._private.position, r = V.nodeSize(c._private.data) / 2; bx1 = Math.min(bx1, q.x - r); by1 = Math.min(by1, q.y - r); bx2 = Math.max(bx2, q.x + r); by2 = Math.max(by2, q.y + r + 16); });
        const P = 18; bx1 -= P; by1 -= P; bx2 += P; by2 += P;
        p._private.gxbb = { x1: bx1, y1: by1, x2: bx2, y2: by2 };
        g.fillStyle = T.groupFill; g.strokeStyle = p.selected() ? T.select : T.groupStroke; g.lineWidth = (p.selected() ? 2 : 1) / z;
        roundRect(g, bx1, by1, bx2 - bx1, by2 - by1, 14); g.fill(); g.stroke();
        if (lod === 2) { g.fillStyle = T.text; g.font = `500 ${12 / Math.max(z, 0.8)}px Roboto, Arial, sans-serif`; g.textAlign = 'left'; g.textBaseline = 'bottom'; g.fillText(V.groupCaption(p._private.data), bx1 + 4, by1 - 4); }
      });

      // edges
      const edges = []; const seenE = new Set();
      // one pass over a cached edge array: no per-frame cytoscape collections (they made ~GBs of garbage)
      const allVis = vis.length === nodeList.length;
      for (const e of edgeList) { if (!visibleEle(e)) continue; if (allVis || visSet.has(e._private.source) || visSet.has(e._private.target)) edges.push(e); }
      R.stats.visibleEdges = edges.length;
      const hl = V.highlightActive();
      const simpleEdges = lod < 2 || edges.length > 30000;
      if (simpleEdges) {
        const byColor = new Map();
        for (const e of edges) { const d = e._private.data; const col = e.selected() ? T.select : (e._private.classes.has('filtered') ? T.filteredEdge : V.edgeColor(d)); let a = byColor.get(col); if (!a) byColor.set(col, (a = [])); a.push(e); }
        g.lineWidth = Math.max(0.5, 0.8) / z; g.globalAlpha = lod === 0 ? 0.45 : 0.7;
        for (const [col, es] of byColor) { g.strokeStyle = col; g.beginPath(); for (const e of es) { const a = e.source()._private.position, b = e.target()._private.position; g.moveTo(a.x, a.y); g.lineTo(b.x, b.y); } g.stroke(); }
        g.globalAlpha = 1;
      } else {
        // halos under highlighted edges first (white ribbon, like the reference)
        if (hl) { g.strokeStyle = T.halo; g.lineCap = 'round'; for (const e of edges) if (e._private.classes.has('hl')) { const geo = edgeGeom(e); g.lineWidth = Math.max(10 / z, baseR * 0.95); strokeGeom(g, geo); } g.lineCap = 'butt'; }
        for (const e of edges) drawEdge(g, e, z, T);
      }

      // nodes
      if (lod === 0) {
        const byColor = new Map(); for (const n of vis) { const col = nodeFill(n, T); let a = byColor.get(col); if (!a) byColor.set(col, (a = [])); a.push(n); }
        const s = Math.max(1.6, rs * 1.6) / z;
        for (const [col, ns] of byColor) { g.fillStyle = col; for (const n of ns) { const p = n._private.position; g.fillRect(p.x - s / 2, p.y - s / 2, s, s); } }
        for (const n of vis) if (n.selected()) { const p = n._private.position; g.strokeStyle = T.select; g.lineWidth = 2 / z; g.beginPath(); g.arc(p.x, p.y, 4 / z, 0, TAU); g.stroke(); }
        return;
      }
      if (lod === 1) {
        const byColor = new Map(); for (const n of vis) { const col = nodeFill(n, T); let a = byColor.get(col); if (!a) byColor.set(col, (a = [])); a.push(n); }
        for (const [col, ns] of byColor) { g.fillStyle = col; g.beginPath(); for (const n of ns) { const p = n._private.position, r = V.nodeSize(n._private.data) / 2; g.moveTo(p.x + r, p.y); g.arc(p.x, p.y, r, 0, TAU); } g.fill(); }
        for (const n of vis) if (n.selected()) { const p = n._private.position, r = V.nodeSize(n._private.data) / 2; g.strokeStyle = T.select; g.lineWidth = 2 / z; g.beginPath(); g.arc(p.x, p.y, r + 2 / z, 0, TAU); g.stroke(); }
        return;
      }
      // full detail: halos, bodies, icons, rings, badges
      if (hl) { g.fillStyle = T.halo; for (const n of vis) if (n._private.classes.has('hl')) { const p = n._private.position, r = V.nodeSize(n._private.data) / 2; g.beginPath(); g.arc(p.x, p.y, r * 2.1, 0, TAU); g.fill(); } }
      const order = vis.slice().sort((a, b) => (a.selected() - b.selected()));
      for (const n of order) drawNode(g, n, z, T);
      // labels with decluttering on a screen-space grid
      const showLabels = (vis.length <= 4000 || exporting); const allLabels = rs >= 10; // zoomed out: like Linkurious, only the selection keeps its label
      if (showLabels) {
        const occ = new Set(); const LC = 18;
        const fs = Math.min(15, Math.max(9, 12.5 * z)); // screen px
        const ranked = order.slice().reverse();          // selected first
        g.textAlign = 'center'; g.textBaseline = 'top';
        for (const n of ranked) {
          const d = n._private.data; if (n._private.classes.has('filtered')) continue;
          if (!allLabels && !n.selected()) continue;
          const cap = V.nodeCaption(d); if (!cap) continue;
          const sel = n.selected(), p = n._private.position, r = V.nodeSize(d) / 2;
          const font = `${sel ? 700 : 400} ${fs}px Roboto, Arial, sans-serif`;
          const lines = wrap(g, cap, font, 150); const lw = Math.max(...lines.map(l => textWidth(g, l, font))) / z, lh = fs * 1.2 / z;
          const tx = p.x, ty = p.y + r + 4 / z;
          if (!sel && !exporting) { // declutter
            const sx1 = Math.floor(((tx - lw / 2) * z + pan.x) / LC), sx2 = Math.floor(((tx + lw / 2) * z + pan.x) / LC), sy1 = Math.floor((ty * z + pan.y) / LC), sy2 = Math.floor(((ty + lh * lines.length) * z + pan.y) / LC);
            let busy = false; for (let a = sx1; a <= sx2 && !busy; a++) for (let b = sy1; b <= sy2; b++) if (occ.has(a * 100003 + b)) { busy = true; break; }
            if (busy) continue; for (let a = sx1; a <= sx2; a++) for (let b = sy1; b <= sy2; b++) occ.add(a * 100003 + b);
          }
          g.save(); g.setTransform(dpr, 0, 0, dpr, dpr * (tx * z + pan.x), dpr * (ty * z + pan.y));
          g.font = font;
          if (sel) { g.fillStyle = T.labelBox; roundRect(g, -lw * z / 2 - 5, -2, lw * z + 10, fs * 1.2 * lines.length + 4, 4); g.fill(); }
          else if (n._private.classes.has('hl')) { /* sits on the halo */ }
          else { g.strokeStyle = T.textHalo; g.lineWidth = 3; g.lineJoin = 'round'; lines.forEach((l, i) => g.strokeText(l, 0, i * fs * 1.2)); }
          g.fillStyle = T.text; lines.forEach((l, i) => g.fillText(l, 0, i * fs * 1.2));
          g.restore();
        }
      }
      // edge captions
      if (!simpleEdges && edges.length <= 3000) {
        const fs = Math.min(12, Math.max(8, 10 * z));
        for (const e of edges) {
          const d = e._private.data; const show = V.edgeCaptionsAlways() || d.meta || e.selected() || e._private.classes.has('hl');
          if (!show || e._private.classes.has('filtered')) continue;
          const txt = V.edgeCaption(d); if (!txt) continue;
          const geo = edgeGeom(e); const m = geoMid(geo); let ang = Math.atan2(geo.y2 - geo.y1, geo.x2 - geo.x1); if (ang > Math.PI / 2 || ang < -Math.PI / 2) ang += Math.PI;
          g.save(); g.setTransform(dpr, 0, 0, dpr, dpr * (m.x * z + pan.x), dpr * (m.y * z + pan.y)); g.rotate(ang);
          g.font = `400 ${fs}px Roboto, Arial, sans-serif`; g.textAlign = 'center'; g.textBaseline = 'bottom';
          g.strokeStyle = T.textHalo; g.lineWidth = 3; g.lineJoin = 'round'; g.strokeText(txt, 0, -2);
          g.fillStyle = e.selected() ? T.select : T.edgeText; g.fillText(txt, 0, -2); g.restore();
        }
      }
      // badges + pins (screen space)
      if (rs >= 7 && vis.length <= 6000) {
        for (const n of vis) {
          const d = n._private.data; if (n._private.classes.has('filtered')) continue;
          const p = n._private.position, r = V.nodeSize(d) / 2; const sx = p.x * z + pan.x, sy = p.y * z + pan.y, sr = r * z;
          g.save(); g.setTransform(dpr, 0, 0, dpr, 0, 0);
          const h = V.hiddenCount(n);
          if (h > 0 && !d.hubgroup) { const bc = nodeFill(n, T); badge(g, sx + sr * 0.72, sy - sr * 0.72, Math.max(6, sr * 0.4), h > 999 ? '999+' : String(h), bc, isLight(bc) ? '#303030' : '#ffffff', T.nodeBorder); }
          if (d.group && !d.hubgroup) badge(g, sx + sr * 0.72, sy - sr * 0.72, Math.max(7, sr * 0.42), 'x' + d.count, T.groupBadge, '#fff');
          if (d.pinned) { const br = Math.max(6, sr * 0.34); const bx = sx + sr * 0.72, by = sy + sr * 0.72; badge(g, bx, by, br, '', nodeFill(n, T), '#fff'); g.strokeStyle = T.nodeBorder; g.lineWidth = 1; g.stroke(); g.translate(bx - br * 0.7, by - br * 0.7); const s = br * 1.4 / 24; g.scale(s, s); g.fillStyle = '#fff'; g.fill(new Path2D(window.ICONS.Pin || window.ICONS.PinOutline)); }
          g.restore();
        }
      }
    }
    R.drawScene = drawScene;

    function nodeFill(n, T) { return n._private.classes.has('filtered') ? T.filteredNode : V.nodeColor(n._private.data); }
    function drawNode(g, n, z, T) {
      const d = n._private.data, p = n._private.position, r = V.nodeSize(d) / 2; const filtered = n._private.classes.has('filtered');
      if (n._private.classes.has('pulse')) { g.fillStyle = T.pulse; g.beginPath(); g.arc(p.x, p.y, r + 16, 0, TAU); g.fill(); }
      g.fillStyle = nodeFill(n, T); g.beginPath(); g.arc(p.x, p.y, r, 0, TAU); g.fill();
      if (r * z >= 5) { g.strokeStyle = T.nodeBorder; g.lineWidth = Math.max(1 / z, r * 0.1); g.stroke(); }
      if (d.group && !d.hubgroup) { g.strokeStyle = T.groupStroke; g.lineWidth = 4; g.stroke(); }
      if (d.hubgroup) { g.setLineDash([5, 4]); g.strokeStyle = T.text; g.lineWidth = 1.5 / z; g.beginPath(); g.arc(p.x, p.y, r + 3, 0, TAU); g.stroke(); g.setLineDash([]); }
      if (!filtered) {
        const ic = V.nodeIcon(d);
        if (ic && r * z >= 7) { const px = r * 2 * 0.58 * z * DPR; const sp = iconSprite(ic.name, ic.color, px); if (sp) { const s = r * 0.58; g.drawImage(sp, p.x - s, p.y - s, s * 2, s * 2); } }
      }
      if (n.selected()) { g.strokeStyle = T.select; g.lineWidth = Math.max(2.2 / z, r * 0.12); g.beginPath(); g.arc(p.x, p.y, r + Math.max(3 / z, r * 0.16), 0, TAU); g.stroke(); }
    }
    function isLight(c) { const m = /^#([0-9a-f]{6})$/i.exec(c); if (!m) return false; const n = parseInt(m[1], 16); return ((n >> 16) * 299 + ((n >> 8) & 255) * 587 + (n & 255) * 114) / 1000 > 170; }
    function badge(g, x, y, br, label, color, textColor, ring) {
      g.font = `500 ${Math.max(7, br * 0.95)}px Roboto, Arial, sans-serif`; const tw2 = label ? g.measureText(label).width : 0; const half = Math.max(0, tw2 / 2 + 2 - br);
      g.fillStyle = color; g.beginPath(); if (half > 0) roundRect(g, x - br - half, y - br, 2 * (br + half), 2 * br, br); else g.arc(x, y, br, 0, TAU); g.fill(); if (ring) { g.strokeStyle = ring; g.lineWidth = 1.5; g.stroke(); }
      if (br >= 6 && label) { g.fillStyle = textColor || '#fff'; g.textAlign = 'center'; g.textBaseline = 'middle'; g.fillText(label, x, y + 0.5); }
    }
    const tintCache = new Map();
    function tint(hex, k) { const key = hex + k; let c = tintCache.get(key); if (c) return c; const m = /^#([0-9a-f]{6})$/i.exec(hex); if (!m) return hex; const n = parseInt(m[1], 16); const f = v => Math.round(v + (255 - v) * k); c = `rgb(${f(n >> 16)},${f((n >> 8) & 255)},${f(n & 255)})`; tintCache.set(key, c); return c; }
    function roundRect(g, x, y, w, h, r) { g.beginPath(); g.moveTo(x + r, y); g.arcTo(x + w, y, x + w, y + h, r); g.arcTo(x + w, y + h, x, y + h, r); g.arcTo(x, y + h, x, y, r); g.arcTo(x, y, x + w, y, r); g.closePath(); }
    function wrap(g, s, font, maxW) {
      if (textWidth(g, s, font) <= maxW || !s.includes(' ')) return [s];
      const out = []; let cur = '';
      for (const w of s.split(' ')) { const t = cur ? cur + ' ' + w : w; if (textWidth(g, t, font) > maxW && cur) { out.push(cur); cur = w; } else cur = t; }
      if (cur) out.push(cur); return out.slice(0, 3);
    }
    // parallel edges between the same pair are fanned out as quadratic curves
    function edgeGeom(e) {
      const pr = e._private; const s = e.source()._private, t = e.target()._private; const a = s.position, b = t.position;
      const rs = V.nodeSize(s.data) / 2, rt = V.nodeSize(t.data) / 2;
      let dx = b.x - a.x, dy = b.y - a.y; const len = Math.hypot(dx, dy) || 1; const ux = dx / len, uy = dy / len;
      const k = pr.gxPar || 0; // signed parallel index, set by R.indexParallel()
      if (!k || e.source() === e.target()) return { x1: a.x + ux * rs, y1: a.y + uy * rs, x2: b.x - ux * rt, y2: b.y - uy * rt, ux, uy, loop: e.source() === e.target(), ax: a.x, ay: a.y, r: rs };
      const off = k * 22; const nx = -uy, ny = ux; const cx = (a.x + b.x) / 2 + nx * off * 2, cyy = (a.y + b.y) / 2 + ny * off * 2;
      const u1x = cx - a.x, u1y = cyy - a.y, l1 = Math.hypot(u1x, u1y) || 1, u2x = b.x - cx, u2y = b.y - cyy, l2 = Math.hypot(u2x, u2y) || 1;
      return { x1: a.x + u1x / l1 * rs, y1: a.y + u1y / l1 * rs, x2: b.x - u2x / l2 * rt, y2: b.y - u2y / l2 * rt, cx, cy: cyy, ux: u2x / l2, uy: u2y / l2 };
    }
    function geoMid(G) { if (G.cx === undefined) return { x: (G.x1 + G.x2) / 2, y: (G.y1 + G.y2) / 2 }; return { x: 0.25 * G.x1 + 0.5 * G.cx + 0.25 * G.x2, y: 0.25 * G.y1 + 0.5 * G.cy + 0.25 * G.y2 }; }
    function strokeGeom(g, G) {
      g.beginPath();
      if (G.loop) { g.arc(G.ax + G.r, G.ay - G.r, G.r * 0.9, 0, TAU); }
      else { g.moveTo(G.x1, G.y1); if (G.cx !== undefined) g.quadraticCurveTo(G.cx, G.cy, G.x2, G.y2); else g.lineTo(G.x2, G.y2); }
      g.stroke();
    }
    function drawEdge(g, e, z, T) {
      const d = e._private.data; const G = edgeGeom(e); const sel = e.selected(); const filtered = e._private.classes.has('filtered');
      const col = sel ? T.select : filtered ? T.filteredEdge : V.edgeColor(d);
      const w = Math.max(0.6 / z, V.edgeWidth(d) * 1.8 * (sel ? 1.3 : 1));
      g.strokeStyle = col; g.lineWidth = w; strokeGeom(g, G);
      if (G.loop) return;
      const al = Math.max(5 / z, 5 + w * 2.2), aw = al * 0.5;
      g.fillStyle = col; g.beginPath(); g.moveTo(G.x2, G.y2); g.lineTo(G.x2 - G.ux * al - G.uy * aw, G.y2 - G.uy * al + G.ux * aw); g.lineTo(G.x2 - G.ux * al + G.uy * aw, G.y2 - G.uy * al - G.ux * aw); g.closePath(); g.fill();
    }
    R.indexParallel = () => {
      const groups = new Map();
      cy.edges().forEach(e => { if (!visibleEle(e)) { e._private.gxPar = 0; return; } const s = e.data('source'), t = e.data('target'); const k = s < t ? s + '|' + t : t + '|' + s; let a = groups.get(k); if (!a) groups.set(k, (a = [])); a.push(e); });
      for (const es of groups.values()) {
        if (es.length === 1) { es[0]._private.gxPar = 0; continue; }
        es.forEach((e, i) => { const s = e.data('source') < e.data('target') ? 1 : -1; const k = (i - (es.length - 1) / 2); e._private.gxPar = s * k; });
      }
    };

    // ---- hit testing -----------------------------------------------------------------------
    function nodeAt(sx, sy) {
      const z = cy.zoom(), p = cy.pan(); const x = (sx - p.x) / z, y = (sy - p.y) / z; const tol = 3 / z;
      let best = null, bd = Infinity;
      for (const n of nodesIn(x - 60, y - 60, x + 60, y + 60)) {
        if (!visibleEle(n)) continue; const q = n._private.position; const r = V.nodeSize(n._private.data) / 2 + tol; const dd = (q.x - x) ** 2 + (q.y - y) ** 2;
        if (dd <= Math.max(r * r, (5 / z) ** 2) && (dd < bd || (n.selected() && !best?.selected()))) { best = n; bd = dd; }
      }
      if (best) return best;
      // compound parents: inside their drawn box
      let par = null; cy.nodes(':parent').forEach(n => { const b = n._private.gxbb; if (b && x >= b.x1 && x <= b.x2 && y >= b.y1 && y <= b.y2) par = n; });
      return par;
    }
    function edgeAt(sx, sy) {
      const z = cy.zoom(), p = cy.pan(); const x = (sx - p.x) / z, y = (sy - p.y) / z; const tol = 5 / z; let best = null, bd = tol;
      const near = nodesIn(x - 2000, y - 2000, x + 2000, y + 2000); const seen = new Set();
      for (const n of near) n.connectedEdges().forEach(e => {
        if (seen.has(e) || !visibleEle(e)) return; seen.add(e); const G = edgeGeom(e);
        const dd = G.cx === undefined ? segDist(x, y, G.x1, G.y1, G.x2, G.y2) : Math.min(segDist(x, y, G.x1, G.y1, (G.x1 + G.cx) / 2, (G.y1 + G.cy) / 2), segDist(x, y, (G.x1 + G.cx) / 2, (G.y1 + G.cy) / 2, geoMid(G).x, geoMid(G).y), segDist(x, y, geoMid(G).x, geoMid(G).y, (G.x2 + G.cx) / 2, (G.y2 + G.cy) / 2), segDist(x, y, (G.x2 + G.cx) / 2, (G.y2 + G.cy) / 2, G.x2, G.y2));
        if (dd < bd) { bd = dd; best = e; }
      });
      return best;
    }
    function segDist(px, py, ax, ay, bx, by) { const dx = bx - ax, dy = by - ay; const l = dx * dx + dy * dy; let t = l ? ((px - ax) * dx + (py - ay) * dy) / l : 0; t = Math.max(0, Math.min(1, t)); return Math.hypot(px - ax - t * dx, py - ay - t * dy); }
    R.nodeAt = nodeAt; R.edgeAt = edgeAt;

    // ---- pointer interaction -----------------------------------------------------------------
    let down = null, lastTap = { t: 0, target: null }, hover = null;
    function local(ev) { const r = canvas.getBoundingClientRect(); return { x: ev.clientX - r.left, y: ev.clientY - r.top }; }
    function model(pt) { const z = cy.zoom(), p = cy.pan(); return { x: (pt.x - p.x) / z, y: (pt.y - p.y) / z }; }
    function emit(target, type, ev, pt) { target.emit({ type, originalEvent: ev, position: model(pt), renderedPosition: pt }); }
    canvas.addEventListener('contextmenu', ev => ev.preventDefault());
    canvas.addEventListener('mousedown', ev => {
      const pt = local(ev); const tgt = nodeAt(pt.x, pt.y) || (ev.button === 0 ? edgeAt(pt.x, pt.y) : null) || nodeAt(pt.x, pt.y);
      const target = tgt || cy;
      if (ev.button === 2) { const t2 = nodeAt(pt.x, pt.y) || edgeAt(pt.x, pt.y) || cy; emit(t2, 'cxttap', ev, pt); return; }
      emit(target, 'tapstart', ev, pt);
      down = { pt, target, moved: false, ev, start: model(pt) };
      if (target !== cy && target.isNode() && !(ev.ctrlKey || ev.metaKey) && cy.autoungrabify() === false && !target.data('hubgroup_static')) {
        let set = target.selected() ? cy.nodes(':selected') : target; if (target.isParent()) set = set.union(target.descendants());
        down.drag = set.filter(n => !n.locked() && !n.isParent()).map(n => ({ n, x0: n._private.position.x, y0: n._private.position.y }));
      }
    });
    window.addEventListener('mousemove', ev => {
      if (!down) {
        if (ev.target !== canvas) return; const pt = local(ev); const n = nodeAt(pt.x, pt.y);
        if (n !== hover) { if (hover) emit(hover, 'mouseout', ev, pt); hover = n; if (n) emit(n, 'mouseover', ev, pt); canvas.style.cursor = n ? 'pointer' : ''; }
        return;
      }
      const pt = local(ev); const dx = pt.x - down.pt.x, dy = pt.y - down.pt.y;
      if (!down.moved && Math.hypot(dx, dy) < 3) return;
      if (!down.moved) { down.moved = true; if (down.drag) { emit(down.target, 'grab', ev, pt); } }
      if (S_lasso()) return;
      if (down.drag && down.drag.length) {
        const m = model(pt); const ox = m.x - down.start.x, oy = m.y - down.start.y;
        for (const it of down.drag) { it.n._private.position.x = it.x0 + ox; it.n._private.position.y = it.y0 + oy; }
        gridDirty = true; emit(down.target, 'drag', ev, pt); full();
      } else if (down.target === cy || down.target.isEdge()) {
        if (cy.userPanningEnabled()) { cy.panBy({ x: ev.movementX, y: ev.movementY }); }
      }
    });
    window.addEventListener('mouseup', ev => {
      if (!down) return; const d = down; down = null; const pt = local(ev);
      if (d.drag && d.moved) { d.drag.forEach(it => it.n.emit('position')); emit(d.target, 'free', ev, pt); emit(d.target, 'dragfree', ev, pt); return; }
      if (d.moved) return;
      const t = d.target; const mod = ev.ctrlKey || ev.metaKey || ev.shiftKey;
      if (t === cy) { if (!mod) cy.$(':selected').unselect(); }
      else if (t.selectable()) { if (mod) { if (ev.shiftKey) t.select(); else t.selected() ? t.unselect() : t.select(); } else if (!(t.selected() && cy.$(':selected').length === 1)) { cy.$(':selected').difference(t).unselect(); t.select(); } }
      emit(t, 'tap', ev, pt);
      const now = performance.now();
      if (now - lastTap.t < 350 && lastTap.target === t) { emit(t, 'dbltap', ev, pt); lastTap = { t: 0, target: null }; } else lastTap = { t: now, target: t };
    });
    canvas.addEventListener('wheel', ev => {
      ev.preventDefault(); const pt = local(ev);
      const f = Math.exp(-ev.deltaY * 0.0016 * R.wheelSensitivity * (ev.deltaMode === 1 ? 30 : 1));
      const z = Math.max(R.minZoom, Math.min(R.maxZoom, cy.zoom() * f));
      cy.zoom({ level: z, renderedPosition: pt });
    }, { passive: false });
    function S_lasso() { return !!(window.S && window.S.lasso); }

    // ---- viewport animation + export -------------------------------------------------------
    let anim = null;
    R.animate = (to, duration = 300) => {
      const z0 = cy.zoom(), p0 = { ...cy.pan() }; let z1 = to.zoom ?? z0, p1 = to.pan ?? p0;
      if (to.fit) { const bb = R.bounds(to.fit); const pad = to.padding ?? 60; z1 = Math.max(R.minZoom, Math.min(to.maxZoom ?? 2.2, Math.min((W - 2 * pad) / Math.max(1, bb.w), (H - 2 * pad) / Math.max(1, bb.h)))); p1 = { x: W / 2 - (bb.x1 + bb.w / 2) * z1, y: H / 2 - (bb.y1 + bb.h / 2) * z1 }; }
      if (to.center) { p1 = { x: W / 2 - to.center.x * z1, y: H / 2 - to.center.y * z1 }; }
      if (anim) cancelAnimationFrame(anim); const t0 = performance.now();
      return new Promise(res => {
        const step = now => { const k = Math.min(1, (now - t0) / Math.max(1, duration)); const e = k < .5 ? 2 * k * k : 1 - (-2 * k + 2) ** 2 / 2;
          cy.viewport({ zoom: z0 + (z1 - z0) * e, pan: { x: p0.x + (p1.x - p0.x) * e, y: p0.y + (p1.y - p0.y) * e } });
          if (k < 1) anim = requestAnimationFrame(step); else { anim = null; full(); res(); } };
        anim = requestAnimationFrame(step);
      });
    };
    R.fit = (eles, padding = 60, maxZoom = 2.2) => { const bb = R.bounds(eles); const z = Math.max(R.minZoom, Math.min(maxZoom, Math.min((W - 2 * padding) / Math.max(1, bb.w), (H - 2 * padding) / Math.max(1, bb.h)))); cy.viewport({ zoom: z, pan: { x: W / 2 - (bb.x1 + bb.w / 2) * z, y: H / 2 - (bb.y1 + bb.h / 2) * z } }); };
    R.bounds = (eles) => {
      let x1 = Infinity, y1 = Infinity, x2 = -Infinity, y2 = -Infinity;
      (eles || cy.nodes()).nodes().forEach(n => { if (n.isParent() || !visibleEle(n)) return; const p = n._private.position, r = V.nodeSize(n._private.data) / 2; x1 = Math.min(x1, p.x - r); y1 = Math.min(y1, p.y - r); x2 = Math.max(x2, p.x + r); y2 = Math.max(y2, p.y + r + 18); });
      if (x1 === Infinity) return { x1: 0, y1: 0, x2: 1, y2: 1, w: 1, h: 1 };
      return { x1, y1, x2, y2, w: x2 - x1, h: y2 - y1 };
    };
    // PNG of the whole graph (full) or of the current view
    R.png = ({ full: whole = true, scale = 1, bg = '#ffffff', maxWidth = 8000, maxHeight = 8000 } = {}) => {
      let w, h, z, pan;
      if (whole) { const bb = R.bounds(); const P = 40; z = Math.min(scale, maxWidth / (bb.w + 2 * P), maxHeight / (bb.h + 2 * P)); w = Math.ceil((bb.w + 2 * P) * z); h = Math.ceil((bb.h + 2 * P) * z); pan = { x: (P - bb.x1) * z, y: (P - bb.y1) * z }; }
      else { w = W * scale; h = H * scale; z = cy.zoom() * scale; pan = { x: cy.pan().x * scale, y: cy.pan().y * scale }; }
      const c = document.createElement('canvas'); c.width = w; c.height = h;
      drawScene(c.getContext('2d'), w, h, 1, z, pan, true, bg);
      return c;
    };

    // ---- model events -> redraw ---------------------------------------------------------------
    cy.on('viewport', viewportChanged);
    cy.on('add remove move', () => { gridDirty = true; listsDirty = true; full(); });
    cy.on('position', () => { gridDirty = true; full(); });
    cy.on('select unselect data class', () => full());
    new ResizeObserver(resize).observe(container);
    resize();
    return R;
  }
  window.GXRender = { createRenderer };
})();
