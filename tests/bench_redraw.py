"""How long one full (uncached) redraw of the v2 renderer takes, at fit and zoomed in.

bench_ui.py's pan/zoom FPS mostly measures the cached-bitmap path (render.js transforms the last frame while the
viewport moves and redraws fully ~140 ms after it settles). This script reports the full redraw itself.
Usage: python3 tests/bench_redraw.py --url http://127.0.0.1:8745/ --sizes 10000,50000,100000
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bench_ui import GEN  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

FULL = """async () => { const t = []; for (let i = 0; i < 7; i++) { R.redraw(); await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))); t.push(R.stats.lastMs); }
  t.sort((a, b) => a - b); return { median_ms: +t[3].toFixed(1), max_ms: +t[6].toFixed(1), lod: R.stats.lod, visible_nodes: R.stats.visibleNodes, visible_edges: R.stats.visibleEdges }; }"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8745/")
    ap.add_argument("--sizes", default="10000,50000,100000")
    ap.add_argument("--out")
    a = ap.parse_args()
    res = []
    with sync_playwright() as p:
        kw = {"args": ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]}
        if os.path.exists("/usr/bin/google-chrome"):
            kw["executable_path"] = "/usr/bin/google-chrome"
        b = p.chromium.launch(**kw)
        for n in [int(x) for x in a.sizes.split(",")]:
            pg = b.new_page(viewport={"width": 1600, "height": 900})
            pg.goto(a.url)
            pg.wait_for_function("typeof cy !== 'undefined' && cy && S.schema", timeout=60000)
            pg.evaluate(f"void addGraph(({GEN})({n}))")
            pg.evaluate("() => new Promise(r => { cy.one('layoutstop', r); runLayout({ name: 'force', randomize: true }); })")
            pg.evaluate("void cy.fit(undefined, 40)")
            fit = pg.evaluate(FULL)
            pg.evaluate("void cy.zoom({ level: 1.2, renderedPosition: { x: innerWidth / 2, y: innerHeight / 2 } })")
            zoomed = pg.evaluate(FULL)
            row = {"nodes": n, "full_redraw_fit": fit, "full_redraw_zoom_1_2": zoomed}
            print(json.dumps(row), flush=True)
            res.append(row)
            pg.close()
        b.close()
    if a.out:
        with open(a.out, "w") as f:
            json.dump(res, f, indent=1)


if __name__ == "__main__":
    main()
