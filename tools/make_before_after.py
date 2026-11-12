"""Before/after screenshots of the same graph in the old (cytoscape renderer, v1) and new (render.js, v2) UI,
next to a Linkurious manual screenshot, at the reference's 1919x941 viewport.

Usage: python3 tools/make_before_after.py --before http://127.0.0.1:8750/ --after http://127.0.0.1:8745/
Writes screenshots/before-after-*.png and compare-before-after.png.
"""
import argparse
import os

from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REF = "/home/box/shared/rnd/linkurious-reference/manual/images/layout__forced_layout.png"
FONT = os.path.join(HERE, "static", "fonts", "Roboto.ttf")
QUERY = "MATCH p=(a:AWSAccount {name:'acme-prod'})-[:RESOURCE]->(r) RETURN p LIMIT 60"


def shoot(url, path):
    with sync_playwright() as p:
        kw = {"args": ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]}
        if os.path.exists("/usr/bin/google-chrome"):
            kw["executable_path"] = "/usr/bin/google-chrome"
        b = p.chromium.launch(**kw)
        pg = b.new_page(viewport={"width": 1919, "height": 941})
        pg.goto(url)
        pg.wait_for_function("typeof cy !== 'undefined' && cy && S.schema", timeout=30000)
        pg.evaluate("localStorage.clear()")
        pg.evaluate(f"void runQueryToGraph({QUERY!r})")
        pg.wait_for_timeout(2500)
        pg.evaluate("void runLayout({ name: 'force', randomize: true, fit: true })")
        pg.wait_for_timeout(7000)
        # select the account (the hub) the way a user would: click it
        pos = pg.evaluate("""() => { const n = cy.nodes().filter(n => n.data('category') === 'AWSAccount')[0];
            const p = n.renderedPosition(); const r = document.querySelector('#cy').getBoundingClientRect(); return [p.x + r.left, p.y + r.top]; }""")
        pg.mouse.click(*pos)
        pg.wait_for_timeout(1200)
        pg.screenshot(path=path)
        b.close()


def label(img, text, font):
    out = Image.new("RGB", (img.width, img.height + 44), "#ffffff")
    out.paste(img, (0, 44))
    ImageDraw.Draw(out).text((12, 10), text, fill="#303030", font=font)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", default="http://127.0.0.1:8750/")
    ap.add_argument("--after", default="http://127.0.0.1:8745/")
    a = ap.parse_args()
    shots = os.path.join(HERE, "screenshots")
    bp, apath = os.path.join(shots, "before-after-v1.png"), os.path.join(shots, "before-after-v2.png")
    shoot(a.before, bp)
    shoot(a.after, apath)
    font = ImageFont.truetype(FONT, 26)
    W = 1200
    imgs = []
    for path, text in [(REF, "Reference: Linkurious manual (force layout, node selected)"),
                       (bp, "Before (v1, cytoscape renderer)"), (apath, "After (v2, render.js)")]:
        im = Image.open(path).convert("RGB")
        im = im.resize((W, int(im.height * W / im.width)), Image.LANCZOS)
        imgs.append(label(im, text, font))
    out = Image.new("RGB", (W, sum(i.height for i in imgs)), "#ffffff")
    y = 0
    for i in imgs:
        out.paste(i, (0, y))
        y += i.height
    dst = os.path.join(HERE, "compare-before-after.png")
    out.save(dst, optimize=True)
    print("wrote", dst, bp, apath)


if __name__ == "__main__":
    main()
