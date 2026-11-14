"""Build docs/compare/compare-*.png (reference screenshot left, prototype right) and compare-all.png.

Reference images come from the Linkurious user manual downloaded during research
(/home/box/shared/rnd/linkurious-reference/manual/images). Run after tests/run_ui_tests.py.
"""
import os
import sys
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REF = os.environ.get("REF_DIR", "/home/box/shared/rnd/linkurious-reference/manual/images")
SHOTS = os.path.join(HERE, "docs", "screenshots")
COMPARE = os.path.join(HERE, "docs", "compare")
FONT = os.path.join(HERE, "static", "fonts", "Roboto.ttf")

PAIRS = [
    ("landing", "visualization-create__visualization_landing.png", "app-01-landing.png", "Landing / get started"),
    ("search", "search-nodes__nodes_search_visualization_result.png", "app-02-search.png", "Search with highlights"),
    ("workspace", "visualization-first__visualization_workspace.png", "app-03-property-panel.png", "Workspace + property panel"),
    ("expand-menu", "expand__expand_options_2.png", "app-04-expand-menu.png", "Expand context menu"),
    ("selective-expand", "expand__expand_selective_modal2.png", "app-05-selective-expand.png", "Selective expand"),
    ("design", "icons__icons_set.png", "app-06-design-panel.png", "Design panel (styles/icons)"),
    ("filters", "filter-panel__filters_panel_categories.png", "app-07-filters.png", "Filter panel"),
    ("multi-selection", "visualization-inspect__multiple_selection.png", "app-08-multi-selection.png", "Multi-selection table"),
    ("background-menu", "select__select_right_click.png", "app-09-background-menu.png", "Background menu / select"),
    ("hierarchical", "layout__hierarchical_layout.png", "app-10-layout-hierarchical.png", "Hierarchical layout"),
    ("radial", "layout__radial_layout.png", "app-11-layout-radial.png", "Radial layout"),
    ("node-grouping", "nodegrouping__er_collapsed_group.png", "app-13-node-grouping.png", "Node grouping"),
    ("lasso", "lasso__lasso_lasso.png", "app-14-lasso.png", "Lasso selection"),
    ("export", "visualization-export__image_export_main_window.png", "app-15-export-image.png", "Image export"),
    ("data-model", "visualization-create__data_model_example.png", "app-18-data-model.png", "Data model"),
]
H = 520


def fit(img, h=H):
    img = img.convert("RGB")
    w = int(img.width * h / img.height)
    if w > 900:
        w, h = 900, int(img.height * 900 / img.width)
    return img.resize((w, h), Image.LANCZOS)


def compose(title, ref, app):
    font = ImageFont.truetype(FONT, 22)
    small = ImageFont.truetype(FONT, 16)
    a, b = fit(ref), fit(app)
    col = max(a.width, 340)  # leave room for the caption above narrow reference images
    W = col + b.width + 60
    out = Image.new("RGB", (W, H + 80), "#ffffff")
    d = ImageDraw.Draw(out)
    d.text((20, 12), title, fill="#303030", font=font)
    d.text((20, 48), "Reference (Linkurious user manual)", fill="#7a7a7a", font=small)
    d.text((col + 40, 48), "Prototype (Neo4j / Cartography test data)", fill="#2365f1", font=small)
    out.paste(a, (20, 70))
    out.paste(b, (col + 40, 70))
    d.rectangle([19, 69, 20 + a.width, 70 + a.height], outline="#e0e0e0")
    d.rectangle([col + 39, 69, col + 40 + b.width, 70 + b.height], outline="#e0e0e0")
    return out


def main():
    made = []
    for key, ref, app, title in PAIRS:
        rp, ap = os.path.join(REF, ref), os.path.join(SHOTS, app)
        if not (os.path.exists(rp) and os.path.exists(ap)):
            print("skip", key, "(missing", rp if not os.path.exists(rp) else ap, ")")
            continue
        img = compose(title, Image.open(rp).convert("RGBA"), Image.open(ap))
        path = os.path.join(COMPARE, f"compare-{key}.png")
        img.save(path, optimize=True)
        made.append(img)
        print("wrote", path)
    if not made:
        sys.exit("no comparisons made")
    scale = 0.5
    rows = [m.resize((int(m.width * scale), int(m.height * scale)), Image.LANCZOS) for m in made]
    W = max(r.width for r in rows)
    out = Image.new("RGB", (W, sum(r.height for r in rows)), "#ffffff")
    y = 0
    for r in rows:
        out.paste(r, (0, y)); y += r.height
    out.save(os.path.join(COMPARE, "compare-all.png"), optimize=True)
    print("wrote compare-all.png", out.size)


if __name__ == "__main__":
    main()
