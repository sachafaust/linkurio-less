"""Headless browser tests for the Neo4j graph explorer (Playwright + Chrome).

Needs a running Neo4j seeded with seed_cartography.py. Starts server.py itself on a
free port, drives the UI, writes tests/results.json and screenshots/app-*.png.

    NEO4J_PASSWORD=... python3 tests/run_ui_tests.py
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import traceback
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "screenshots"
DL = Path(os.environ.get("TMPDIR", "/tmp")) / "gx-downloads"
RESULTS = []


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def check(name, fn):
    t0 = time.time()
    try:
        detail = fn()
        RESULTS.append({"test": name, "ok": True, "detail": detail, "secs": round(time.time() - t0, 2)})
        print(f"PASS {name}: {detail}")
    except Exception as e:  # noqa: BLE001
        RESULTS.append({"test": name, "ok": False, "detail": f"{type(e).__name__}: {e}", "secs": round(time.time() - t0, 2)})
        print(f"FAIL {name}: {e}")
        traceback.print_exc(limit=2)


def main():
    SHOTS.mkdir(exist_ok=True); DL.mkdir(parents=True, exist_ok=True)
    port = free_port()
    env = {**os.environ, "PORT": str(port), "QUIET": "1"}
    srv = subprocess.Popen([sys.executable, str(ROOT / "server.py")], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = f"http://127.0.0.1:{port}/"
    for _ in range(60):
        try:
            urllib.request.urlopen(url + "api/status", timeout=2); break
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    chrome = os.environ.get("CHROME", "/usr/bin/google-chrome")
    errors = []
    try:
        with sync_playwright() as p:
            kw = dict(headless=True, args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"] + os.environ.get("CHROME_ARGS", "").split())
            if os.path.exists(chrome):
                kw["executable_path"] = chrome
            b = p.chromium.launch(**kw)
            ctx = b.new_context(viewport={"width": 1440, "height": 860}, accept_downloads=True)
            pg = ctx.new_page()
            pg.on("pageerror", lambda e: errors.append(str(e)))
            # the read-only test deliberately gets an HTTP 403 from /api/cypher; Chrome logs that as a console error
            pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and "status of 403" not in m.text else None)
            run_all(pg, url)
            b.close()
    finally:
        srv.terminate()
    RESULTS.append({"test": "no JavaScript errors during the run", "ok": not errors, "detail": errors[:5] or "none"})
    passed = sum(r["ok"] for r in RESULTS)
    out = {"passed": passed, "total": len(RESULTS), "when": time.strftime("%Y-%m-%d %H:%M %Z"), "results": RESULTS}
    (ROOT / "tests" / "results.json").write_text(json.dumps(out, indent=1))
    print(f"\n{passed}/{len(RESULTS)} passed")
    sys.exit(0 if passed == len(RESULTS) else 1)


def shot(pg, name):
    pg.wait_for_timeout(250)
    pg.screenshot(path=str(SHOTS / f"app-{name}.png"))


def ev(pg, js):
    # Never hand a cytoscape collection back to Playwright: serialising its cyclic object graph
    # made the renderer grow to >3 GB and crash ("Target crashed").
    return pg.evaluate("""async (src) => {
        const r = await (0, eval)(src);
        return (r && typeof r === 'object' && (typeof r.cy === 'function' || r instanceof Element)) ? null : r;
    }""", js)


def wait_layout(pg, ms=1600):
    pg.wait_for_timeout(ms)


def node_pos(pg, node_id):
    return ev(pg, f"(() => {{ const p = cy.getElementById({json.dumps(node_id)}).renderedPosition(); return [p.x, p.y]; }})()")


def run_all(pg, url):
    pg.goto(url)
    # wait for the app to finish booting. (window.cy alone is not enough: before boot it is the <div id="cy">
    # element via named access, so a slow first schema load let the checks start too early.)
    pg.wait_for_function("typeof cy !== 'undefined' && !!cy && typeof cy.nodes === 'function' && !!S.schema && document.querySelector('#conn').textContent.length > 0", timeout=90000)

    def t_connect():
        st = json.loads(urllib.request.urlopen(url + "api/status").read())
        assert st["connected"] and st["read_only"] and st["nodes"] > 500, st
        assert pg.is_visible("#landing")
        txt = pg.text_content("#conn")
        assert "read-only" in txt, txt
        shot(pg, "01-landing")
        return f"{st['server']} {st['nodes']} nodes / {st['edges']} edges, landing shown, '{txt}'"
    check("connect to Neo4j and show the landing screen", t_connect)

    def t_search():
        pg.click("#search-input"); pg.fill("#search-input", "acme-prod")
        pg.wait_for_selector("#search-results .sr", timeout=8000)
        n = pg.locator("#search-results .sr").count()
        first = pg.text_content("#search-results .sr .cat")
        marks = pg.locator("#search-results mark").count()
        assert n >= 5 and first.strip().upper() == "AWSACCOUNT" and marks > 0, (n, first, marks)
        shot(pg, "02-search")
        return f"{n} results, first category {first}, {marks} highlighted matches"
    check("full-text search shows ranked results with category chip and highlights", t_search)

    def t_search_edges_local():
        pg.click(".sd-head [data-kind=edge]"); pg.fill("#search-input", "CAN_WRITE"); pg.wait_for_timeout(900)
        ne = pg.locator("#search-results .sr").count()
        pg.click(".sd-head [data-kind=node]"); pg.fill("#search-input", "acme-prod"); pg.wait_for_timeout(900)
        assert ne > 0, ne
        return f"edge search for CAN_WRITE returned {ne} edges"
    check("search edges by type", t_search_edges_local)

    def t_add():
        # wait for the node results of "acme-prod" to replace the edge results of the previous check (search is
        # debounced and async; on a bigger or cold database a fixed sleep raced it and Enter picked a stale row)
        pg.wait_for_function("(() => { const c = document.querySelector('#search-results .sr .cat'); return c && c.textContent.trim().toUpperCase() === 'AWSACCOUNT'; })()", timeout=15000)
        pg.keyboard.press("Enter"); pg.wait_for_timeout(800)
        n = ev(pg, "cy.nodes().length"); sel = ev(pg, "cy.$(':selected').map(e => e.data('category'))")
        assert n == 1 and sel == ["AWSAccount"], (n, sel)
        badge = ev(pg, "hiddenCount(cy.nodes()[0])")
        assert badge > 50
        return f"added AWSAccount node, undisplayed-neighbor badge = {badge}"
    check("pick a search result adds and selects the node", t_add)

    def t_expand_menu():
        x, y = node_pos(pg, ev(pg, "cy.nodes()[0].id()"))
        pg.mouse.click(x, y, button="right"); pg.wait_for_selector(".menu .mi")
        pg.hover(".menu .mi:has-text('Expand')"); pg.wait_for_function("document.querySelectorAll('.menu').length >= 2", timeout=8000)
        items = pg.locator(".menu >> nth=1").locator(".mi").all_text_contents()
        shot(pg, "04-expand-menu")
        assert any("All neighbors" in i for i in items) and any("AWSEC2Instance" in i for i in items), items
        pg.click(".menu >> nth=1 >> .mi:has-text('More options')")
        pg.wait_for_selector(".modal .xp-row")
        rows = pg.locator(".modal .xp-row").count()
        # Pick only AWSRole nodes via RESOURCE edges
        pg.click(".modal [data-xall]"); pg.click(".modal [data-xall]")
        pg.locator(".modal [data-xcat='AWSRole']").check()
        row = pg.locator(".modal .xp-row", has=pg.locator("[data-xcat='AWSRole']"))
        row.locator("input[type=radio][value='RESOURCE']").check()
        shot(pg, "05-selective-expand")
        pg.click(".modal [data-xgo]"); wait_layout(pg, 2200)
        cats = ev(pg, "[...new Set(cy.nodes().map(n => n.data('category')))]")
        types = ev(pg, "[...new Set(cy.edges().map(e => e.data('type')))]")
        assert set(cats) == {"AWSAccount", "AWSRole"} and types == ["RESOURCE"], (cats, types)
        return f"expand menu items: {len(items)}; selective expand had {rows} category rows; result categories {cats}, edge types {types}"
    check("expand menu filtered by category and edge type (selective expand)", t_expand_menu)

    def t_dblclick_expand():
        before = ev(pg, "cy.nodes().length")
        nid = ev(pg, "cy.nodes().filter(n => n.data('category') === 'AWSRole' && hiddenCount(n) > 0 && hiddenCount(n) < 40)[0].id()")
        ev(pg, f"cy.zoom(1.4); cy.center(cy.getElementById({json.dumps(nid)}))"); pg.wait_for_timeout(200)
        x, y = node_pos(pg, nid); pg.mouse.dblclick(x, y); wait_layout(pg, 2200)
        after = ev(pg, "cy.nodes().length")
        assert after > before, (before, after)
        return f"double-click expanded an AWSRole: {before} -> {after} nodes"
    check("double-click a node expands its neighbors", t_dblclick_expand)

    def t_expand_all():
        acct = ev(pg, "cy.nodes().filter(n => n.data('category') === 'AWSAccount')[0].id()")
        ev(pg, f"expand(cy.getElementById({json.dumps(acct)}), {{limit: 60}})"); wait_layout(pg, 2600)
        ev(pg, "cy.fit(cy.elements(), 60)"); pg.wait_for_timeout(300)
        return f"{ev(pg, 'cy.nodes().length')} nodes / {ev(pg, 'cy.edges().length')} edges on screen"
    check("expand all neighbors (limit 60) and lay out", t_expand_all)

    def t_property_panel():
        nid = ev(pg, "cy.nodes().filter(n => n.data('category') === 'AWSEC2Instance')[0].id()")
        ev(pg, f"selectOnly(cy.getElementById({json.dumps(nid)}))"); pg.wait_for_timeout(200)
        assert pg.is_visible("#open-selpanel")
        pg.click("#open-selpanel"); pg.wait_for_selector("#leftpanel .lp-title")
        title = pg.text_content("#leftpanel .lp-title"); props = pg.locator("#leftpanel .prop").count()
        chips = pg.locator("#leftpanel .lp-cats .cat").all_text_contents()
        shot(pg, "03-property-panel")
        pg.fill("#leftpanel .searchbox input", "instancetype"); pg.wait_for_timeout(150)
        vis = pg.locator("#leftpanel .prop:not(.hidden)").count()
        pg.fill("#leftpanel .searchbox input", "")
        assert props > 8 and vis == 1 and "AWSEC2Instance" in chips, (props, vis, chips)
        return f"title '{title}', {props} properties, chips {chips}, property search left {vis}"
    check("property panel shows caption, id, categories and searchable properties", t_property_panel)

    def t_edge_panel():
        eid = ev(pg, "cy.edges()[0].id()")
        ev(pg, f"selectOnly(cy.getElementById({json.dumps(eid)}))"); pg.wait_for_timeout(200)
        t = pg.text_content("#leftpanel .lp-title"); src = pg.locator("#leftpanel [data-goto]").count()
        return f"edge panel '{t}', {src} source/target links"
    check("edge selection shows edge type, source and target", t_edge_panel)

    def t_multi():
        pg.mouse.click(700, 830)  # focus the page, not an input
        pg.keyboard.press("Control+a"); pg.wait_for_timeout(300)
        head = pg.text_content("#leftpanel .multi-title"); tables = pg.locator("#leftpanel table.sel").count()
        shot(pg, "08-multi-selection")
        pg.click("#leftpanel table.sel th >> nth=1"); pg.wait_for_timeout(100)
        assert head == "Selection Information" and tables >= 2, (head, tables)
        nsel = ev(pg, "cy.$(':selected').length")
        return f"{nsel} selected, {tables} per-category tables, sortable headers"
    check("multi-selection table per category (Ctrl+A)", t_multi)

    def t_bg_menu():
        ev(pg, "cy.$(':selected').unselect()")
        pg.mouse.click(1000, 790, button="right"); pg.wait_for_selector(".menu .mi")
        pg.hover(".menu .mi:has-text('Select')"); pg.wait_for_function("document.querySelectorAll('.menu').length >= 2")
        shot(pg, "09-background-menu")
        pg.click(".menu >> nth=1 >> .mi:has-text('Leaf nodes')"); pg.wait_for_timeout(200)
        leaf = ev(pg, "cy.$(':selected').length")
        assert leaf > 0
        return f"background menu Select > Leaf nodes selected {leaf}"
    check("background context menu: select leaf nodes", t_bg_menu)

    def t_shift_neighbors():
        nid = ev(pg, "cy.nodes().filter(n => n.data('category') === 'AWSAccount')[0].id()")
        ev(pg, "cy.$(':selected').unselect()")
        x, y = node_pos(pg, nid)
        pg.keyboard.down("Shift"); pg.mouse.click(x, y); pg.keyboard.up("Shift"); pg.wait_for_timeout(250)
        n = ev(pg, "cy.$('node:selected').length"); deg = ev(pg, f"cy.getElementById({json.dumps(nid)}).neighborhood().nodes().length")
        assert n == deg + 1, (n, deg)
        return f"shift+click selected the node and its {deg} neighbors"
    check("shift+click selects direct neighbors", t_shift_neighbors)

    def t_style():
        ev(pg, "cy.$(':selected').unselect()")
        pg.click("[data-panel=design]"); pg.wait_for_selector("#rightpanel .tabs-box")
        u = ev(pg, "cy.nodes().filter(n => n.data('category') === 'AWSRole')[0].id()")
        w0 = ev(pg, f"nodeSize(cy.getElementById({json.dumps(u)}).data())")
        pg.click("#rightpanel [data-color='AWSRole']"); pg.click(".pop [data-c='#17becf']"); pg.wait_for_timeout(200)
        col = ev(pg, f"nodeColor(cy.getElementById({json.dumps(u)}).data())")
        pg.click("#rightpanel [data-size='AWSRole']"); pg.select_option(".pop [data-ss]", "200"); pg.click(".pop [data-sok]"); pg.wait_for_timeout(200)
        w1 = ev(pg, f"nodeSize(cy.getElementById({json.dumps(u)}).data())")
        pg.click("#rightpanel [data-icon-pick='AWSRole']"); pg.click(".pop [data-n='Star']"); pg.wait_for_timeout(200)
        shot(pg, "06-design-panel")
        pg.click("#rightpanel [data-dtab=captions]"); pg.click("#rightpanel [data-open='c:AWSRole']")
        pg.check("#rightpanel [data-cap='AWSRole|roleid']"); pg.wait_for_timeout(200)
        cap = ev(pg, f"nodeCaption(cy.getElementById({json.dumps(u)}).data())")
        assert col.lower() == "#17becf" and abs(w1 - 2 * w0) < 0.5 and " - AROA" in cap, (col, w0, w1, cap)
        # reset back to defaults via the gear menu
        pg.click("#rightpanel [data-d=gear]"); pg.click(".menu .mi:has-text('Reset styles')"); pg.wait_for_timeout(200)
        col2 = ev(pg, f"nodeColor(cy.getElementById({json.dumps(u)}).data())")
        return f"color -> {col}, size {w0:.0f}px -> {w1:.0f}px, icon set, caption '{cap}'; reset gave {col2}"
    check("design panel: color, size, icon and caption by category", t_style)

    def t_value_color():
        # make sure some stopped instances are on screen (synthetic data: ~10% stopped)
        ev(pg, "runQueryToGraph(\"MATCH (i:EC2Instance {state: 'stopped'}) RETURN i LIMIT 3\")"); pg.wait_for_timeout(600)
        pg.click("#rightpanel [data-dtab=styles]"); pg.click("#rightpanel [data-open='d:AWSEC2Instance']")
        pg.click("#rightpanel [data-open='dp:AWSEC2Instance:state']")
        pg.click("#rightpanel [data-vcolor='AWSEC2Instance|state|stopped']"); pg.click(".pop [data-c='#9e9e9e']"); pg.wait_for_timeout(200)
        stopped = ev(pg, "cy.nodes().filter(n => n.data('props').state === 'stopped').map(n => nodeColor(n.data()))")
        running = ev(pg, "cy.nodes().filter(n => n.data('props').state === 'running').map(n => nodeColor(n.data()))[0]")
        assert stopped and all(c.lower() == "#9e9e9e" for c in stopped), stopped
        pg.click("#rightpanel [data-d=gear]"); pg.click(".menu .mi:has-text('Reset styles')")
        return f"{len(stopped)} stopped instances recolored grey, running stay {running}"
    check("design panel: color by property value", t_value_color)

    def t_dynamic_size():
        pg.click("#rightpanel [data-size='AWSS3Bucket']"); pg.select_option(".pop [data-sp]", "object_count"); pg.select_option(".pop [data-sc]", "log"); pg.click(".pop [data-sok]")
        ev(pg, "expand(cy.nodes().filter(n => n.data('category') === 'AWSAccount'), {categories: ['AWSS3Bucket'], limit: 30})"); wait_layout(pg, 2400)
        ws = ev(pg, "cy.nodes().filter(n => n.data('category') === 'AWSS3Bucket').map(n => nodeSize(n.data()))")
        pg.click("#rightpanel [data-d=gear]"); pg.click(".menu .mi:has-text('Reset styles')")
        assert ws and max(ws) > min(ws) * 1.5, ws
        return f"{len(ws)} buckets sized by log(object_count): {min(ws):.0f}px .. {max(ws):.0f}px"
    check("design panel: dynamic size by numeric property", t_dynamic_size)

    def t_filter():
        pg.click("[data-panel=filters]"); pg.wait_for_selector("#rightpanel .hint")
        n0 = ev(pg, "cy.nodes().length")
        pg.click("#rightpanel [data-ftog='cat|AWSS3Bucket']"); pg.wait_for_timeout(200)
        f = ev(pg, "cy.nodes('.filtered').length"); dot = ev(pg, "document.querySelector('[data-panel=filters]').classList.contains('dot')")
        shot(pg, "07-filters")
        pg.click("#rightpanel [data-open='f:AWSEC2Instance']"); pg.click("#rightpanel [data-open='fp:AWSEC2Instance:state']")
        pg.click("#rightpanel [data-ftog='val|AWSEC2Instance|state|stopped']"); pg.wait_for_timeout(150)
        f2 = ev(pg, "cy.nodes('.filtered').length")
        ev(pg, "removeFiltered()"); pg.wait_for_timeout(200)
        n1 = ev(pg, "cy.nodes().length")
        ev(pg, "undo()"); pg.wait_for_timeout(200)
        n2 = ev(pg, "cy.nodes().length")
        pg.click("#rightpanel [data-f=reset]")
        f3 = ev(pg, "cy.nodes('.filtered').length")
        assert f > 0 and f2 > f and n1 == n0 - f2 and n2 == n0 and f3 == 0, (n0, f, f2, n1, n2, f3)
        return f"category filter greyed {f}, + value filter {f2}, 'Remove filtered items' {n0}->{n1}, undo -> {n2}, reset -> {f3} filtered"
    check("filters: category and property value, remove filtered, undo, reset", t_filter)

    def t_num_filter():
        pg.click("#rightpanel [data-open='f:AWSS3Bucket']"); pg.click("#rightpanel [data-open='fp:AWSS3Bucket:object_count']")
        pg.fill("#rightpanel [data-fnum='AWSS3Bucket|object_count|min']", "1000000"); pg.press("#rightpanel [data-fnum='AWSS3Bucket|object_count|min']", "Enter"); pg.wait_for_timeout(200)
        f = ev(pg, "cy.nodes('.filtered').length"); exp = ev(pg, "cy.nodes().filter(n => n.data('category') === 'AWSS3Bucket' && n.data('props').object_count < 1000000).length")
        pg.click("#rightpanel [data-f=reset]")
        assert f == exp, (f, exp)
        return f"number bound object_count >= 1,000,000 hid {f} buckets (expected {exp})"
    check("filters: numeric lower bound", t_num_filter)

    def t_layouts():
        pg.click("[data-panel=layout]"); pg.wait_for_selector("#rightpanel [data-lay=hierarchical]")
        acct = ev(pg, "cy.nodes().filter(n => n.data('category') === 'AWSAccount')[0].id()")
        ev(pg, f"selectOnly(cy.getElementById({json.dumps(acct)}))")
        pg.click("#rightpanel [data-lay=hierarchical] .t"); pg.click("#rightpanel [data-lroot]"); pg.click("#rightpanel [data-lrun]"); wait_layout(pg, 1500)
        ys = ev(pg, "[...new Set(cy.nodes(':visible').map(n => Math.round(n.position('y'))))].length")
        ev(pg, "cy.$(':selected').unselect()"); shot(pg, "10-layout-hierarchical")
        pg.click("#rightpanel [data-lay=radial] .t"); pg.click("#rightpanel [data-lrun]"); wait_layout(pg, 1500)
        c = ev(pg, f"(() => {{ const r = cy.getElementById({json.dumps(acct)}).position(); const bb = cy.nodes(':visible').boundingBox(); return [r.x - (bb.x1+bb.x2)/2, r.y - (bb.y1+bb.y2)/2]; }})()")
        shot(pg, "11-layout-radial")
        pg.click("#rightpanel [data-lay=force] .t"); pg.click("#rightpanel [data-lrun]"); wait_layout(pg, 2000)
        shot(pg, "12-layout-force")
        assert 2 <= ys <= 12 and abs(c[0]) < 60 and abs(c[1]) < 60, (ys, c)
        return f"hierarchical: {ys} layers; radial: root offset from center {c[0]:.0f},{c[1]:.0f}; force re-run"
    check("layouts: hierarchical, radial, force-directed", t_layouts)

    def t_pin():
        nid = ev(pg, "cy.nodes()[3].id()")
        ev(pg, f"cy.getElementById({json.dumps(nid)}).data('pinned', true)")
        p0 = ev(pg, f"cy.getElementById({json.dumps(nid)}).position()")
        ev(pg, "runLayout({name: 'force'})"); wait_layout(pg, 1500)
        p1 = ev(pg, f"cy.getElementById({json.dumps(nid)}).position()")
        ev(pg, f"cy.getElementById({json.dumps(nid)}).data('pinned', false)")
        moved = abs(p0['x'] - p1['x']) + abs(p0['y'] - p1['y'])
        assert moved < 2, (p0, p1)
        return f"pinned node moved {moved:.2f}px during force layout"
    check("pinned node stays put during force layout", t_pin)

    def t_grouping():
        pg.click("[data-panel=nodegroup]"); pg.click("#rightpanel [data-gnew]"); pg.wait_for_selector(".modal [data-rp]")
        pg.fill(".modal [data-rt]", "Region"); pg.select_option(".modal [data-rp]", "region"); pg.click(".modal [data-rok]")
        pg.click("#rightpanel [data-gtog]"); pg.wait_for_timeout(400)
        groups = ev(pg, "cy.nodes().filter(n => n.data('group')).map(n => n.data('caption') + ' x' + n.data('count'))")
        hidden = ev(pg, "cy.nodes('.grp-hidden').length")
        ev(pg, "runLayout({name:'force'})"); wait_layout(pg, 1600)
        shot(pg, "13-node-grouping")
        gid = ev(pg, "cy.nodes().filter(n => n.data('group'))[0].id()")
        x, y = node_pos(pg, gid); pg.mouse.dblclick(x, y); pg.wait_for_timeout(500)
        kids = ev(pg, f"cy.getElementById({json.dumps(gid)}).children().length")
        pg.click("#rightpanel [data-gtog]"); pg.wait_for_timeout(300)
        left = ev(pg, "cy.nodes().filter(n => n.data('group')).length")
        assert groups and hidden > 0 and kids > 0 and left == 0, (groups, hidden, kids, left)
        return f"groups {groups}; {hidden} members collapsed; double-click opened one with {kids} members; rule off -> {left} groups"
    check("node grouping by property (collapse/expand)", t_grouping)

    def t_edge_grouping():
        # The seed has no parallel edges of the same type, so add two synthetic parallel copies client-side.
        e = ev(pg, "(() => { const e = cy.edges().filter(x => !x.data('meta'))[0]; for (const i of [1,2]) cy.add({group:'edges', data:{...e.data(), id: e.id()+'-dup'+i}}); return e.data('type'); })()")
        pg.click("[data-panel=edgegroup]"); pg.click(f"#rightpanel [data-egtog='{e}']"); pg.wait_for_timeout(300)
        meta = ev(pg, "cy.edges('.emeta').map(x => x.data('type') + ' (' + x.data('count') + ')')")
        pg.click(f"#rightpanel [data-egtog='{e}']"); ev(pg, "cy.edges().filter(x => x.id().includes('-dup')).remove(); afterGraphChange()")
        assert meta and meta[0].endswith("(3)"), meta
        return f"grouped parallel edges into {meta}"
    check("edge grouping of parallel edges (synthetic duplicates)", t_edge_grouping)

    def t_lasso():
        ev(pg, "cy.$(':selected').unselect(); cy.fit(cy.elements(':visible'), 60)"); pg.wait_for_timeout(300)
        # start on empty background left of the graph, then circle the left half of the drawing
        bb = ev(pg, "cy.nodes(':visible').renderedBoundingBox()")
        x0, x1, y0, y1 = bb["x1"] - 25, (bb["x1"] + bb["x2"]) / 2, bb["y1"] - 25, bb["y2"] + 25
        pg.keyboard.down("Control"); pg.mouse.move(x0, (y0 + y1) / 2); pg.mouse.down()
        for (x, y) in [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, (y0 + y1) / 2 - 5)]:
            pg.mouse.move(x, y, steps=8)
        pg.screenshot(path=str(SHOTS / "app-14-lasso.png"))
        pg.mouse.up(); pg.keyboard.up("Control"); pg.wait_for_timeout(200)
        n = ev(pg, "cy.$('node:selected').length")
        assert n > 0, n
        return f"ctrl+drag lasso selected {n} nodes"
    check("lasso selection (Ctrl+drag)", t_lasso)

    def t_keyboard_undo():
        n0 = ev(pg, "cy.nodes().length"); s = ev(pg, "cy.$('node:selected').length")
        pg.keyboard.press("Control+Backspace"); pg.wait_for_timeout(200); n1 = ev(pg, "cy.nodes().length")
        pg.keyboard.press("Control+z"); pg.wait_for_timeout(200); n2 = ev(pg, "cy.nodes().length")
        pg.keyboard.press("Control+y"); pg.wait_for_timeout(200); n3 = ev(pg, "cy.nodes().length")
        pg.keyboard.press("Control+z"); pg.wait_for_timeout(200)
        p0 = ev(pg, "cy.pan()"); pg.keyboard.press("ArrowLeft"); p1 = ev(pg, "cy.pan()")
        z0 = ev(pg, "cy.zoom()"); pg.keyboard.press("+"); pg.wait_for_timeout(300); z1 = ev(pg, "cy.zoom()")
        assert n1 == n0 - s and n2 == n0 and n3 == n1 and p1["x"] > p0["x"] and z1 > z0, (n0, s, n1, n2, n3, p0, p1, z0, z1)
        return f"Ctrl+Backspace {n0}->{n1}, Ctrl+Z ->{n2}, Ctrl+Y ->{n3}; arrow pans, + zooms"
    check("keyboard shortcuts: hide, undo, redo, pan, zoom", t_keyboard_undo)

    def t_export_png():
        ev(pg, "cy.$(':selected').unselect()")
        pg.click("[data-act=main-menu]"); pg.hover(".menu .mi:has-text('Export')"); pg.click(".menu >> nth=1 >> .mi:has-text('Image')")
        pg.wait_for_selector(".modal img[data-prev]"); pg.wait_for_timeout(600)
        shot(pg, "15-export-image")
        with pg.expect_download() as d:
            pg.click(".modal [data-idl]")
        f = DL / "export.png"; d.value.save_as(f); head = f.read_bytes()[:8]
        assert head == b"\x89PNG\r\n\x1a\n" and f.stat().st_size > 20000, (head, f.stat().st_size)
        return f"PNG export {f.stat().st_size // 1024} KB"
    check("export PNG image", t_export_png)

    def t_export_csv():
        pg.click("[data-act=main-menu]"); pg.hover(".menu .mi:has-text('Export')"); pg.click(".menu >> nth=1 >> .mi:has-text('Data')")
        pg.wait_for_selector(".modal [data-xd]")
        files = []
        pg.on("download", lambda dl: files.append(dl))
        pg.click(".modal [data-xd]"); pg.wait_for_timeout(1500)
        heads = []
        for dl in files:
            p = DL / dl.suggested_filename; dl.save_as(p); heads.append((dl.suggested_filename, p.read_text().splitlines()[0][:60], len(p.read_text().splitlines()) - 1))
        assert len(files) == 2 and heads[0][1].startswith("id,categories,caption") and heads[1][1].startswith("id,type,source,target"), heads
        return "; ".join(f"{a}: {c} rows" for a, b, c in heads)
    check("export CSV data (nodes + edges)", t_export_csv)

    def t_save_open():
        n0 = ev(pg, "cy.nodes().length")
        pg.click("#save-chip"); pg.fill(".modal [data-vn]", "Test viz"); pg.click(".modal [data-vs]"); pg.wait_for_timeout(300)
        chip = pg.text_content("#save-chip"); title = pg.text_content("#viz-title")
        pg.click("[data-act=main-menu]"); pg.click(".menu .mi:has-text('New visualization')"); pg.wait_for_timeout(300)
        n1 = ev(pg, "cy.nodes().length")
        pg.click("[data-act=main-menu]"); pg.click(".menu .mi:has-text('Open')"); pg.click(".modal tr[data-vo] td >> nth=0"); pg.wait_for_timeout(600)
        n2 = ev(pg, "cy.nodes().length")
        assert "Auto save ON" in chip and title == "Test viz" and n1 == 0 and n2 == n0, (chip, title, n0, n1, n2)
        return f"saved '{title}' ({chip.strip()}), new -> {n1} nodes, reopened -> {n2} nodes (localStorage)"
    check("save, new and reopen a visualization locally", t_save_open)

    def t_cypher_readonly():
        pg.click("[data-act=code-query] >> nth=1")
        pg.fill(".modal textarea.cypher", "MATCH (n) DETACH DELETE n")
        pg.click(".modal [data-cqadd]"); pg.wait_for_selector(".modal .err")
        err = pg.text_content(".modal .err")
        req = urllib.request.Request(url + "api/cypher", data=json.dumps({"query": "CREATE (x:Hack) RETURN x"}).encode(), headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req); code = 200
        except urllib.error.HTTPError as e:
            code = e.code
        pg.fill(".modal textarea.cypher", "MATCH p=(u:GitHubUser)-[r]->(o:GitHubOrganization) WHERE u.has_2fa_enabled = false RETURN p")
        pg.click(".modal [data-cqprev]"); pg.wait_for_timeout(800); shot(pg, "16-code-query")
        n0 = ev(pg, "cy.nodes().length")
        pg.click(".modal [data-cqadd]"); wait_layout(pg, 1800); n1 = ev(pg, "cy.nodes().length")
        total = json.loads(urllib.request.urlopen(url + "api/status").read())["nodes"]
        assert "not allowed" in err and code == 403 and n1 > n0, (err, code, n0, n1)
        return f"write refused in UI ('{err.strip()}') and API (HTTP {code}); read query added {n1 - n0} nodes; DB still {total} nodes"
    check("read-only Cypher: writes refused, reads added to the graph", t_cypher_readonly)

    def t_queries_panel():
        pg.click("[data-panel=queries]"); pg.wait_for_selector("#rightpanel .q-item")
        n0 = ev(pg, "cy.nodes().length")
        pg.click("#rightpanel .q-item:has-text('Okta groups mapped')"); wait_layout(pg, 1800)
        n1 = ev(pg, "cy.nodes().length")
        shot(pg, "17-queries-panel")
        assert n1 > n0, (n0, n1)
        return f"built-in query added {n1 - n0} nodes"
    check("built-in Cartography queries panel", t_queries_panel)

    def t_data_model():
        pg.click("[data-panel=queries]")
        ev(pg, "openDataModel()"); pg.wait_for_selector("#dm-cy canvas", timeout=15000); pg.wait_for_timeout(1500)
        cats = ev(pg, "Object.keys(S.schema.categories).length")
        shot(pg, "18-data-model"); pg.click(".modal [data-close]")
        assert cats >= 30
        return f"data model shows {cats} categories"
    check("understand your data model view", t_data_model)

    def t_minimap_dark():
        pg.click("#btn-minimap"); pg.wait_for_timeout(300)
        vis = pg.is_visible("#minimap")
        ev(pg, "toggleTheme()"); pg.wait_for_timeout(400)
        dark = ev(pg, "document.body.classList.contains('dark')")
        shot(pg, "19-dark-minimap")
        ev(pg, "toggleTheme()"); pg.click("#btn-minimap")
        assert vis and dark
        return "minimap shown; dark theme toggled"
    check("minimap and dark theme", t_minimap_dark)

    def t_hub_groups():
        # Supernodes: above HUB_THRESHOLD hidden neighbours, double-click shows one group node per category
        # instead of hundreds of nodes; the group loads its members in pages.
        ev(pg, "newViz()")
        ev(pg, "runQueryToGraph(\"MATCH (a:AWSAccount {name:'acme-prod'}) RETURN a\")"); pg.wait_for_timeout(1200)
        hub = ev(pg, "cy.nodes()[0].id()"); hidden = ev(pg, "hiddenCount(cy.nodes()[0])")
        ev(pg, f"HUB_THRESHOLD = 100; expandDefault(cy.getElementById({json.dumps(hub)}))"); pg.wait_for_timeout(1500)
        groups = ev(pg, "cy.nodes().filter(n => n.data('hubgroup')).map(n => [n.data('category'), n.data('type'), n.data('count')])")
        real0 = ev(pg, "realNodes().length")
        shot(pg, "21-hub-groups")
        big = max(groups, key=lambda g: g[2])
        gid = f"hub:{hub}:{big[0]}:{big[1]}"
        ev(pg, f"loadHubGroup(cy.getElementById({json.dumps(gid)}), 10)"); pg.wait_for_timeout(1500)
        real1 = ev(pg, "realNodes().length"); left = ev(pg, f"cy.getElementById({json.dumps(gid)}).data('count')")
        ev(pg, "HUB_THRESHOLD = 300")
        assert hidden > 100 and len(groups) >= 2 and sum(g[2] for g in groups) == hidden and real0 == 1 and real1 == 11 and left == big[2] - 10, (hidden, groups, real0, real1, left)
        return f"hub with {hidden} hidden neighbours -> {len(groups)} group nodes; loading 10 from {big[0]} left {left}"
    check("supernode expand: neighbours grouped per category, paged loading", t_hub_groups)

    def t_workspace_shot():
        # A clean overview shot for the comparison sheet: Cartography IAM neighborhood with property panel.
        ev(pg, "newViz()")
        ev(pg, "runQueryToGraph(\"MATCH p=(a:AWSAccount {name:'acme-prod'})-[:RESOURCE]->(r:AWSRole)<-[:STS_ASSUMEROLE_ALLOW]-(x) RETURN p LIMIT 40\")"); wait_layout(pg, 2600)
        rid = ev(pg, "cy.nodes().filter(n => n.data('category') === 'AWSRole').max(n => n.degree()).ele.id()")
        ev(pg, f"selectOnly(cy.getElementById({json.dumps(rid)}))"); pg.wait_for_timeout(200)
        if pg.is_visible("#open-selpanel"):
            pg.click("#open-selpanel")
        ev(pg, "cy.fit(cy.elements(), 70); cy.panBy({x: 200, y: 0})"); pg.wait_for_timeout(400)
        shot(pg, "20-workspace")
        return f"{ev(pg, 'cy.nodes().length')} nodes in the IAM workspace shot"
    check("workspace overview screenshot", t_workspace_shot)


if __name__ == "__main__":
    main()
