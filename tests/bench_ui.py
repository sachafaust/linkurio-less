"""Large-graph benchmark for the browser side (headless Chrome, no GPU on the box: WebGL runs on SwiftShader).

For each size it injects a synthetic Cartography-like graph straight into the page (same shape as an
/api/expand answer, so no database is involved), then measures:
  * load: addGraph() until two animation frames have been painted (time to first render)
  * layout: the app's force layout until it reports layoutstop (capped by --layout-timeout)
  * pan / zoom: rAF frame intervals while panning, then zooming, every frame for 3 s (mean FPS and p95 frame time)
  * memory: JS heap (performance.memory) and the renderer process RSS

Usage: python3 tests/bench_ui.py --url http://127.0.0.1:8744/ --sizes 10000,50000,100000 --out tests/bench-ui.json
"""
import argparse
import json
import os
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

GEN = r"""
(n) => {
  // deterministic PRNG
  let s = 42; const rnd = () => (s = (s * 1664525 + 1013904223) >>> 0) / 4294967296;
  const cats = ['AWSEC2Instance','AWSRole','AWSUser','AWSS3Bucket','AWSLambda','AWSEC2SecurityGroup','AWSEC2Subnet','AWSPolicy','GitHubRepository','OktaUser','GCPInstance','AWSKMSKey'];
  const types = ['RESOURCE','MEMBER_OF_EC2_SECURITY_GROUP','PART_OF_SUBNET','POLICY','STS_ASSUMEROLE_ALLOW','CAN_READ','ENCRYPTED_BY'];
  const hubs = Math.max(4, Math.round(n / 2500));
  const nodes = [], edges = [];
  for (let i = 0; i < n; i++) {
    const hub = i < hubs; const c = hub ? 'AWSAccount' : cats[Math.floor(rnd() * cats.length)];
    nodes.push({ id: 'b' + i, labels: [c], category: c, degree: 0, props: { id: 'b' + i, name: (hub ? 'account-' : c.toLowerCase().slice(3, 9) + '-') + i, region: ['us-east-1','us-west-2','eu-west-1'][i % 3] } });
  }
  let k = 0;
  const add = (a, b, t) => { edges.push({ id: 'e' + (k++), type: t, source: 'b' + a, target: 'b' + b, props: {} }); nodes[a].degree++; nodes[b].degree++; };
  // clustered structure: every resource belongs to an account, and links to ~1 nearby resource
  for (let i = hubs; i < n; i++) {
    add(Math.floor(rnd() * hubs), i, 'RESOURCE');
    const j = Math.max(hubs, Math.min(n - 1, i + 1 + Math.floor(rnd() * 40)));
    if (j !== i) add(i, j, types[1 + Math.floor(rnd() * (types.length - 1))]);
  }
  return { nodes, edges };
}
"""

MEASURE_FRAMES = r"""
async ({ mode, ms }) => {
  const iv = []; let last = performance.now(); const t0 = last; let i = 0;
  await new Promise(res => {
    function f(now) {
      iv.push(now - last); last = now; i++;
      if (mode === 'pan') cy.panBy({ x: (i % 120 < 60 ? 6 : -6), y: 3 * Math.sin(i / 10) });
      else cy.zoom({ level: cy.zoom() * (i % 90 < 45 ? 1.02 : 1 / 1.02), renderedPosition: { x: innerWidth / 2, y: innerHeight / 2 } });
      if (now - t0 < ms) requestAnimationFrame(f); else res();
    }
    requestAnimationFrame(f);
  });
  iv.shift(); iv.sort((a, b) => a - b);
  const mean = iv.reduce((a, b) => a + b, 0) / iv.length;
  return { frames: iv.length, fps: +(1000 / mean).toFixed(1), mean_ms: +mean.toFixed(1), p95_ms: +iv[Math.floor(iv.length * 0.95)].toFixed(1), max_ms: +iv[iv.length - 1].toFixed(1) };
}
"""


def renderer_rss_mb(b):
    """RSS of this browser's renderer processes (ids from CDP, so other Chrome instances on the box don't count)."""
    try:
        cdp = b.new_browser_cdp_session()
        procs = cdp.send("SystemInfo.getProcessInfo")["processInfo"]
        best = 0
        for pr in procs:
            if pr.get("type") == "renderer":
                try:
                    with open(f"/proc/{pr['id']}/status") as f:
                        best = max(best, int(f.read().split("VmRSS:")[1].split()[0]) // 1024)
                except OSError:
                    pass
        return best or None
    except Exception:  # noqa: BLE001
        return None


def bench_size(b, url, n, layout_timeout):
    ctx = b.new_context(viewport={"width": 1600, "height": 900})
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(url)
    pg.wait_for_function("typeof cy !== 'undefined' && cy && S.schema", timeout=60000)
    pg.evaluate("localStorage.clear()")
    res = {"nodes": n}
    pg.evaluate(f"window.__g = ({GEN})({n})")
    res["edges"] = pg.evaluate("__g.edges.length")
    t = pg.evaluate("""async () => {
        const t0 = performance.now();
        addGraph(__g);
        const t1 = performance.now();
        await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
        return { add_ms: t1 - t0, first_render_ms: performance.now() - t0 };
    }""")
    res["load_ms"] = round(t["first_render_ms"])
    res["add_ms"] = round(t["add_ms"])
    res["on_screen"] = pg.evaluate("cy.nodes(':visible').length")
    try:
        pg.evaluate("void cy.fit(undefined, 40)")
    except Exception:  # noqa: BLE001
        pass
    res["pan_preLayout"] = pg.evaluate(MEASURE_FRAMES, {"mode": "pan", "ms": 3000})
    # layout
    t0 = time.time()
    try:
        pg.evaluate(f"""() => new Promise((res, rej) => {{
            const to = setTimeout(() => rej(new Error('timeout')), {layout_timeout * 1000});
            cy.one('layoutstop', () => {{ clearTimeout(to); res(); }});
            runLayout({{ name: 'force', randomize: true }});
        }})""")
        res["layout_s"] = round(time.time() - t0, 1)
    except Exception as e:  # noqa: BLE001
        res["layout_s"] = f">{layout_timeout} (timed out)" if "timeout" in str(e) else f"error: {str(e)[:80]}"
    pg.evaluate("void cy.fit(undefined, 40)")
    pg.wait_for_timeout(500)
    res["pan"] = pg.evaluate(MEASURE_FRAMES, {"mode": "pan", "ms": 3000})
    res["zoom"] = pg.evaluate(MEASURE_FRAMES, {"mode": "zoom", "ms": 3000})
    # zoomed in (labels and icons drawn): pan around one region
    pg.evaluate("void cy.zoom({ level: 1.2, renderedPosition: { x: innerWidth / 2, y: innerHeight / 2 } })")
    pg.wait_for_timeout(300)
    res["pan_zoomed_in"] = pg.evaluate(MEASURE_FRAMES, {"mode": "pan", "ms": 3000})
    res["js_heap_mb"] = pg.evaluate("Math.round(performance.memory.usedJSHeapSize / 1048576)")
    res["js_heap_after_gc_mb"] = pg.evaluate("(gc(), Math.round(performance.memory.usedJSHeapSize / 1048576))")
    res["renderer_rss_mb"] = renderer_rss_mb(b)
    res["webgl"] = pg.evaluate("!!(cy.renderer && cy.renderer().webgl)")
    res["errors"] = errors[:3]
    ctx.close()
    return res


def run_one(url, n, layout_timeout, gl):
    chrome = os.environ.get("CHROME", "/usr/bin/google-chrome")
    args = ["--no-sandbox", "--disable-dev-shm-usage", "--enable-precise-memory-info", "--js-flags=--expose-gc"]
    args += ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"] if gl else ["--disable-gpu"]
    with sync_playwright() as p:
        kw = dict(headless=True, args=args)
        if os.path.exists(chrome):
            kw["executable_path"] = chrome
        b = p.chromium.launch(**kw)
        try:
            return bench_size(b, url, n, layout_timeout)
        finally:
            b.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8744/")
    ap.add_argument("--sizes", default="10000,50000,100000")
    ap.add_argument("--layout-timeout", type=int, default=120)
    ap.add_argument("--size-timeout", type=int, default=420, help="give up on one size after this many seconds")
    ap.add_argument("--gl", choices=["swiftshader", "none"], default="swiftshader",
                    help="swiftshader = WebGL/accelerated canvas in software; none = --disable-gpu (CPU raster canvas, no WebGL)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--label", default="")
    ap.add_argument("--one", type=int, help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.one:
        print("RESULT " + json.dumps(run_one(args.url, args.one, args.layout_timeout, args.gl == "swiftshader")), flush=True)
        return
    out = {"label": args.label, "when": time.strftime("%Y-%m-%d %H:%M %Z"),
           "browser": f"headless Chrome on a CPU-only box (gl={args.gl})", "results": []}
    for n in [int(x) for x in args.sizes.split(",")]:
        cmd = [sys.executable, __file__, "--url", args.url, "--one", str(n), "--layout-timeout", str(args.layout_timeout), "--gl", args.gl]
        try:
            pr = subprocess.run(cmd, capture_output=True, text=True, timeout=args.size_timeout)
            line = next((l for l in pr.stdout.splitlines() if l.startswith("RESULT ")), None)
            r = json.loads(line[7:]) if line else {"nodes": n, "error": (pr.stderr or pr.stdout)[-300:]}
        except subprocess.TimeoutExpired:
            subprocess.run("pkill -f 'chrome.*enable-precise-memory-info'", shell=True)
            r = {"nodes": n, "error": f"did not finish within {args.size_timeout}s"}
        print(json.dumps(r), flush=True)
        out["results"].append(r)
    if args.out:
        with open(args.out, "w") as f:
            json.dump(out, f, indent=1)


if __name__ == "__main__":
    main()
