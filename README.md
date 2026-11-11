# Graph Explorer for Neo4j (Linkurious-style, Cartography data)

**Idea:** Linkurious-style graph explorer for a Cartography Neo4j (view and browse only)
**Source:** Sacha direct
**Status:** Ready for demo — Explore-phase experiment

## Prompt / idea

Sacha (direct, 2026-11-11):

> apply the same methodology to also do a full clone of https://linkurious.com/ but only support Neo4j database
> connectivity - I want to use it with a neo4j database populated by https://github.com/cartography-cncf/cartography .
> this is a very hard one and a lot of research will be needed to capture all the features - I just want the graph
> database view and browsing and their fantastic ui - I do't need the advance analytics and collaboration features yet

"Same methodology" means the PO-33 approach: research the real product first, then build to that reference.
- **Research:** the user manual, YouTube demos (yt-dlp, with ffmpeg frames), and design tokens sampled from the frames.
- **Build:** match the reference, then test it with Playwright and produce side-by-side comparison images.

The research is in `/home/box/shared/rnd/linkurious-reference/REFERENCE.md`, along with 309 manual images and the video frames. The videos themselves are in `/workspace/linkurious-videos/` and are not in git.

## What it does

A small Python server proxies **read-only** Cypher to Neo4j. A single-page web UI recreates the Linkurious
Enterprise visualization workspace on top of [cytoscape.js](https://js.cytoscape.org/) and the fCoSE force layout.
- **Server:** `server.py`, stdlib `http.server` and the official `neo4j` driver.
- **UI:** `static/`. The libraries are vendored in `static/vendor`, with licenses in `vendor/LICENSES.md`.

It is built for Cartography's graph:
- **Display category:** Cartography nodes carry several labels (e.g. `AWSEC2Instance:EC2Instance:ComputeInstance`). The server picks one display category per node, using a priority list in `cartography_styles.json`, then the longest non-generic label.
- **Defaults:** each category has a default color, icon and caption.
- **Built-in queries:** `queries.json` holds 9 read-only security queries, such as internet-exposed instances, cross-account role trusts, public buckets, and GitHub outside collaborators.

Why cytoscape.js: it is MIT-licensed and needs no build step. It supports compound nodes (used for node grouping) and has good layout plugins (fCoSE, breadthfirst, concentric). Badges, pins, the lasso and the minimap are drawn on a canvas overlay. Icons are Material Design paths (@mdi/js, Apache-2.0), built by `tools/build_icons.py`. The font is Roboto (OFL).

### Parity with the Linkurious workspace

| Feature (from REFERENCE.md) | State | Notes |
|---|---|---|
| Workspace layout | Done | Top-left menu card (menu, undo, redo, code query, …), top search bar, right toolbar plus docked panels, left property panel, save chip, zoom controls, Help button, watermark |
| Landing "Get started with one of the options" | Done | Search, Explore queries, Code a query, Understand your data model, Help |
| No-code query builder pill | Not done | Not built |
| Quick search | Done | Nodes/Edges and All/Local, yellow highlight on matches, category chip, click to add |
| Advanced search | Partial | Category and property filters, pagination and Add all work. Fuzzy is an on/off checkbox and only applies with a full-text index (`SEARCH_FULLTEXT_INDEX`); otherwise search uses `CONTAINS` scans |
| Expand on double-click | Done | Skips neighbors already on screen, with an expand limit |
| Expand context menu by category, with counts | Done | |
| Selective Expand modal | Done | Per category: All or one edge type; limit N; most/least connected first |
| Undisplayed-neighbor badges | Done | Dark #454466 pill at the bottom-left of a node |
| Selection and highlight | Done | Red ring, halos on the neighborhood, others faded |
| Property panel | Done | Caption, #id, category chips, color/size, property search, locate, expand, remove, export |
| Edge panel | Done | Source and target links; aggregates for grouped edges |
| Multi-selection tables | Done | Per category, sortable, paginated, searchable |
| Background and node context menus | Done | Select ▸ (all, leaves, by category, invert); pin, hide, expand, queries |
| Design panel: per-category color, icon, size | Done | |
| Dynamic size by numeric property | Done | Linear or log |
| Value-based colors | Done | |
| Edge color and width | Done | |
| Captions | Partial | Node caption property choice and order, and edge captions, work. No per-caption font settings |
| Legend | Partial | The Design panel serves as the legend, as in Linkurious. There is no separate floating legend widget |
| Filters | Done | Categories, edge types, string values with counts, numeric bounds with a histogram, applied filters, reset, filtered items greyed out, remove filtered items |
| Layouts | Done | Force (fCoSE, fast mode), hierarchical (root and orientation), radial (root, ≤1500 nodes) |
| Layout preferences | Done | Pin on drag, incremental expand |
| Node grouping | Done | By property, with category scope; one active rule; grey compound bubble with xN; double-click to collapse or expand |
| Edge grouping | Done | Parallel edges of the same type merged, toggled per type |
| Lasso and edge lasso | Done | Ctrl+drag; Ctrl+Shift+drag for edges |
| Keyboard shortcuts | Done | Arrows, +/-, Ctrl+A, Ctrl+Backspace, Shift+click neighbors, Ctrl+Z/Y, Esc |
| Ctrl+C/V copy and paste | Built, untested | Copies the selected nodes to localStorage and pastes them into another visualization. No automated test |
| Ctrl+E edit | Not done | Excluded on purpose: the explorer is read-only |
| Undo and redo | Done | Snapshot based |
| Image export | Partial | PNG with size presets works. SVG, PDF and annotations are not done |
| Data export | Partial | CSV (nodes and edges, all visible or the selection) works. Excel, GEXF, GraphML and JSON are not done |
| Save and open visualizations | Done (local) | Saved to browser localStorage with Auto save ON, instead of server-side storage |
| Code a query | Done | Read-only Cypher editor that adds the results to the graph |
| Queries panel | Done | Built-in Cartography queries |
| Data model view | Done | Categories, relationships and properties from the live schema |
| Minimap | Done | |
| Dark theme | Done (extra) | The Linkurious manual documents no dark theme |
| Timeline | Not done | Linkurious's timeline (dates, ranges, "as at", per-item rows) is not basic. The toolbar icon only shows "not available" |
| Geo mode | Not done | |
| Alerts, cases, comments, sharing, spaces, analytics, data edition, admin, plugins | Excluded | Out of scope per Sacha |

## How to run

The requirements are Python 3.10 or later, `pip install neo4j` (tested with driver 6.4.1), and Neo4j 5.x (tested with 5.26.0 community).

Run it against your own Cartography Neo4j:

```bash
cd prototypes/2026-11-11-linkurious-neo4j
export NEO4J_URI=bolt://localhost:7687 NEO4J_USER=neo4j NEO4J_PASSWORD=...   # ideally a reader-role user
export NEO4J_DATABASE=neo4j          # optional
export SEARCH_FULLTEXT_INDEX=...     # optional: name of a full-text index for faster search
python3 server.py                    # http://127.0.0.1:8744 (HOST, PORT, EXPAND_LIMIT, QUIET env vars)
```

To try it without Cartography, seed a **local** test database with synthetic, Cartography-shaped data.
- **What it creates:** 1,099 nodes and 2,430 relationships covering AWS, GCP, GitHub and Okta. The data is deterministic (seed 42), and all names and ids are fake.
- **Safety:** the seeder refuses non-localhost URIs. `--wipe` empties the database first.

```bash
NEO4J_PASSWORD=... python3 seed_cartography.py --wipe
```

Tests: `pip install playwright`, then run `NEO4J_PASSWORD=... python3 tests/run_ui_tests.py`.
- **What it does:** starts `server.py` on a free port, drives Chrome, and writes `tests/results.json` and `screenshots/app-*.png`.
- **Browser:** `CHROME=/path/to/chrome` overrides the browser.

Comparison images: `python3 tools/make_compare.py`.

## Results

UI test run on 2026-11-11 at 14:30 PST: **32/32 checks passed**, including "no JavaScript errors" (`tests/results.json`). The checks ran against the seeded local Neo4j 5.26.0 with synthetic data.
- **Search:** full-text search with highlights; edge search; pick a result, which shows a neighbor badge of 163.
- **Expand:** Selective Expand by category and edge type; double-click expand; expand all with a limit.
- **Panels and selection:**
  - Property and edge panels.
  - Ctrl+A multi-selection tables.
  - Background menu "Select leaf nodes", and Shift+click neighbors.
- **Design:** per-category color, size, icon and caption, then reset. Value-based color (stopped instances turn grey). Log-scale size: 21 buckets from 16 to 78 px.
- **Filters:** category and value filters, remove filtered items (91→67), undo, reset. The numeric bound hid 9 of 9 expected buckets.
- **Layouts:** hierarchical (4 layers), radial, and force. A pinned node moved 0.00 px.
- **Grouping:** node grouping by region (3 groups, open and close); edge grouping (RESOURCE ×3).
- **Lasso and shortcuts:** the lasso selected 23 nodes. Keyboard: hide 91→68, undo 91, redo 68, pan, zoom.
- **Export and saving:** PNG export (800 KB), CSV export (91 node rows, 165 edge rows), local save, new and reopen.
- **Read-only Cypher:** `DETACH DELETE` was refused in the UI and by the API (HTTP 403). The database still had 1,099 nodes.
- **Other views:** built-in queries panel, data model (37 categories), minimap, dark theme.

Screenshots are in `screenshots/`. `compare-*.png` and `compare-all.png` put Linkurious manual screenshots (left) next to the prototype (right).

Not tested:
- A real Cartography database. Running Cartography needs real cloud credentials, and none were used. The synthetic data follows labels and relationship types taken from Cartography's own model introspection (`cartography.models.introspection`).
- Large graphs (100k+ nodes), and Firefox or Safari.

Bugs found and fixed along the way:
- Popovers closed straight away. The gear menu closed on the same click that opened it.
- Group cleanup was incomplete.
- fCoSE draft quality crashed without randomize.
- The test harness crashed Chrome: returning a cytoscape collection from `page.evaluate` blew the renderer past 3 GB.

## Cost

$0. No paid services. Everything runs locally: Neo4j community in a tarball, open-source libraries, synthetic data. It took about an hour of agent time (research downloads started at 13:37 PST, and the tests were green at 14:30 PST), plus an earlier research and build session.

## Limits and risks

- **Trademark.** This is an unofficial recreation for internal exploration. The UI says "Graph Explorer for Neo4j" and contains no Linkurious name or logo. The comparison images embed screenshots from the public Linkurious manual for side-by-side review only. Don't publish them.
- **Read-only safety, in two layers:**
  1. The server rejects write clauses (CREATE, MERGE, SET, DELETE, REMOVE, DROP, LOAD CSV, CALL dbms…, apoc write procedures, and others) after stripping string literals and comments.
  2. Every query runs in a READ-mode transaction with a timeout.

  A regex screen is not a parser. For real use, also connect as a Neo4j user with the `reader` role.
- **No auth on the web UI.** It binds to 127.0.0.1 by default. Don't expose it.
- **Search speed.** Search falls back to `CONTAINS` scans over the configured properties, which is slow on big Cartography graphs. Create a full-text index and set `SEARCH_FULLTEXT_INDEX`.
- **Supernodes.** AWSAccount and GCPProject fan out through RESOURCE edges. The expand limits help, but the hierarchical and radial layouts of a supernode give wide, flat fans.
- **Saving.** Saved visualizations live in the browser (localStorage). They are not shared and not on a server.
- **Not built:** the timeline, the no-code query builder, SVG/PDF/Excel/GEXF/GraphML export, and the geo mode (see the parity table).

## Status

Ready for demo — Explore-phase experiment. A good next step: point it at a real Cartography Neo4j, using a reader-role user, and see which default styles and queries need tuning.
