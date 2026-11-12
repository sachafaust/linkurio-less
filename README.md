# Graph Explorer for Neo4j (Linkurious-style, Cartography data)

**Idea:** Linkurious-style graph explorer for a Cartography Neo4j (view and browse only)
**Source:** Sacha direct
**Status:** Ready for demo — Explore-phase experiment (v2: real-schema Cartography data, closer Linkurious look, large graphs)

## Run it on your machine

```bash
git pull && cd prototypes/2026-11-11-linkurious-neo4j
python3 run_local.py            # or ./run.sh
```

Needs **Python 3.9+** (macOS's built-in `/usr/bin/python3`, 3.9.6, is fine; a newer `python3.1x` or Homebrew Python is used for the venv if present, or force one with `RUN_LOCAL_PYTHON=/path/to/python`) and **Docker** (on macOS: Docker Desktop installed and running, `open -a Docker`; Apple Silicon is fine, the image is multi-arch). The first run takes a few minutes (Python packages, the `neo4j:5.26-community` image); after that about 25 s. It:
1. creates `.venv/` here and installs `requirements.txt` (just the `neo4j` driver);
2. starts Neo4j 5.26 community in Docker: container `gx-neo4j`, volume `gx-neo4j-data`, ports bound to 127.0.0.1 only (bolt 7687, Neo4j Browser 7474), test password `tinkerlab-test`. Reuses the container on later runs;
3. wipes it and loads the **Cartography fixture snapshot**, the same mock data used in development: 6,939 nodes / 11,054 relationships built with Cartography's own demo seeds and AWS/GCP/Okta/GitHub test fixtures (see "Real-schema Cartography data" below);
4. starts the explorer on http://127.0.0.1:8744 and opens your browser. Ctrl+C stops the explorer; Neo4j keeps running.

| Option | What it does |
|---|---|
| `--synthetic 50000 [--hub 100000]` | load the synthetic Cartography-shaped generator instead (about N nodes; `--hub` adds a supernode) |
| `--both` | fixtures + the synthetic estate (1,099 nodes) in one database |
| `--keep-data` | don't reload |
| `--test` | install Playwright + Chromium, load `--both`, run the UI suite (33 checks, screenshots in `screenshots/`) plus a quick browser benchmark and an API benchmark (`.run/bench-*.json`), then keep serving (`--no-serve` to stop) |
| `--neo4j-port 17687 --neo4j-http-port 17474` | if 7687/7474 are taken (the script tells you) |
| `--neo4j-uri neo4j+s://host:7687 --neo4j-user neo4j` | use your own Neo4j, local or remote, instead of Docker. Its data is left untouched and opened read-only. Password via `--neo4j-password`, `NEO4J_PASSWORD`, or a prompt. Also `--neo4j-database`. |
| `--neo4j-uri ... --load-data` | wipe that database and load the mock data (refused for non-local servers) |
| `--port 8755`, `--no-browser`, `--detach` | explorer port, no browser, run in the background |
| `--status`, `--stop`, `--reset` | show what runs; stop explorer + container; delete container + volume |

Prefer compose? `docker compose up -d neo4j` uses the same container, volume and password (see `docker-compose.yml` for the three follow-up commands). To rebuild the fixture snapshot from a Cartography checkout: `sh tools/cartography_fixtures/regenerate.sh` (clones Cartography, wipes the local Neo4j, runs the demo seeder and the integration tests, exports).

## Prompt / idea

Sacha (direct, 2026-11-11):

> apply the same methodology to also do a full clone of https://linkurious.com/ but only support Neo4j database
> connectivity - I want to use it with a neo4j database populated by https://github.com/cartography-cncf/cartography .
> this is a very hard one and a lot of research will be needed to capture all the features - I just want the graph
> database view and browsing and their fantastic ui - I do't need the advance analytics and collaboration features yet

"Same methodology" means the PO-33 approach: research the real product first, then build to that reference.
- **Research:** the user manual, YouTube demos (yt-dlp, with ffmpeg frames), and design tokens sampled from the frames.
- **Build:** match the reference, then test it with Playwright and produce side-by-side comparison images.

Follow-up from Sacha (2026-11-11, v2 of this prototype):

> does https://github.com/cartography-cncf/cartography have a test docker image with mock data that could be use to test
> things out? Also I think you need to do more work on how clean and "copy" the UI looks - it also needs to be optimized
> for very large graphs

The research is in `/home/box/shared/rnd/linkurious-reference/REFERENCE.md`, along with 309 manual images and the video frames. The videos themselves are in `/workspace/linkurious-videos/` and are not in git.

## What it does

A small Python server proxies **read-only** Cypher to Neo4j. A single-page web UI recreates the Linkurious
Enterprise visualization workspace. Since v2, [cytoscape.js](https://js.cytoscape.org/) only holds the graph model
(headless); a small Canvas2D renderer (`static/render.js`) draws it, and force layouts run ForceAtlas2 in a web worker.
- **Server:** `server.py`, stdlib `http.server` and the official `neo4j` driver.
- **UI:** `static/`. The libraries are vendored in `static/vendor`, with licenses in `vendor/LICENSES.md`.

It is built for Cartography's graph:
- **Display category:** Cartography nodes carry several labels (e.g. `AWSEC2Instance:EC2Instance:ComputeInstance`). The server picks one display category per node, using a priority list in `cartography_styles.json`, then the longest non-generic label.
- **Defaults:** each category has a default color, icon and caption.
- **Built-in queries:** `queries.json` holds 9 read-only security queries, such as internet-exposed instances, cross-account role trusts, public buckets, and GitHub outside collaborators.

Why cytoscape.js: it is MIT-licensed and needs no build step, has a good graph model (selectors, traversal, compound nodes for node grouping) and the breadthfirst/concentric layouts. Its own renderer was too slow for big graphs on a CPU-only machine (about 1 FPS at 10k nodes, see below), so v2 draws the model itself:
- **`static/render.js`:** a spatial grid, three levels of detail (dots, batched circles, full detail with icons, labels and badges), label decluttering, a cached bitmap that is transformed while the view moves (used above 3,000 visible nodes or when a full frame takes more than 12 ms; a full redraw follows 140 ms after the view settles), and cytoscape-style events (tap, dbltap, cxttap, drag, hover) on the model.
- **Linkurious look:** white ring around nodes, hidden-neighbor badge at the top-right in the node color, labels only when zoomed in (the selection always keeps a bold label on a white box), white halos on the selected neighborhood, grey 2 px edges with arrows, no edge captions unless asked, no connection-status line under the search bar.
- **Layout:** ForceAtlas2 (graphology, Barnes-Hut above 2,000 nodes) in a web worker, time-boxed (1.2 s for small graphs, up to 20 s at 100k nodes), streaming positions to the canvas. fCoSE is still vendored but no longer used: in the headless model it collapsed star-shaped graphs.
- **Supernodes:** double-clicking a node with more than 300 hidden neighbors adds one group node per (category, edge type) with its count, instead of hundreds of nodes. Double-click a group (or use its menu) to load 200, 1,000 or up to 5,000 members at a time. Icons are Material Design paths (@mdi/js, Apache-2.0), built by `tools/build_icons.py`. The font is Roboto (OFL).

### Parity with the Linkurious workspace

| Feature (from REFERENCE.md) | State | Notes |
|---|---|---|
| Workspace layout | Done | Top-left menu card (menu, undo, redo, code query, …), top search bar, right toolbar plus docked panels, left property panel, save chip, zoom controls, Help button, watermark |
| Landing "Get started with one of the options" | Done | Search, Explore queries, Code a query, Understand your data model, Help |
| No-code query builder pill | Not done | Not built |
| Quick search | Done | Nodes/Edges and All/Local, yellow highlight on matches, category chip, click to add |
| Advanced search | Partial | Category and property filters, pagination and Add all work. Fuzzy only applies with a full-text index (auto-detected `gx_search`, see `tools/create_fulltext_index.py`); otherwise search scans at most 200,000 nodes (`SEARCH_SCAN_CAP`) with `CONTAINS` |
| Expand on double-click | Done | Skips neighbors already on screen, with an expand limit |
| Expand context menu by category, with counts | Done | |
| Selective Expand modal | Done | Per category: All or one edge type; limit N; most/least connected first |
| Undisplayed-neighbor badges | Done | v2: top-right, in the node color with a white ring, as in the manual screenshots |
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
| Layouts | Done | Force (ForceAtlas2 in a web worker since v2), hierarchical (root and orientation), radial (root, ≤1500 nodes) |
| Large graphs (Linkurious handles supernodes with an expand limit) | Done (v2) | 100k nodes / 200k edges load in 6 s and pan at 60 FPS (cached bitmap) on a CPU-only box; supernode neighbors grouped per category; server queries capped and paginated |
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

The requirements are Python 3.9 or later, `pip install neo4j` (tested with driver 6.4.1 on 3.13 and 5.28.x on 3.9; driver 6 needs 3.10+, pip picks the right one), and Neo4j 5.x (tested with 5.26.0 community).

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

### Real-schema Cartography data (no cloud credentials)

Does Cartography ship a Docker image with mock data? **No.** `ghcr.io/cartography-cncf/cartography` is the CLI and does a real sync. But the repo has a demo seeder (`python -m demo`, no AWS/GCP/Okta seed) and test fixtures that load mock data through Cartography's own load functions. Details, links and what failed upstream: [`/home/box/shared/rnd/linkurious-reference/CARTOGRAPHY-MOCK-DATA.md`](/home/box/shared/rnd/linkurious-reference/CARTOGRAPHY-MOCK-DATA.md).

A snapshot of that data (demo seeds plus the AWS, GCP, Okta and GitHub integration-test fixtures: 6,939 nodes / 11,054 relationships, only fixture values and AWS documentation example keys) is in `data/cartography-fixtures.json.gz`:

```bash
NEO4J_PASSWORD=... python3 seed_cartography.py --wipe --fixtures      # real Cartography schema
NEO4J_PASSWORD=... python3 seed_cartography.py --wipe --scale 100 --hub 100000   # big synthetic graph + a supernode
```

To rebuild it from a Cartography checkout, see `tools/cartography_fixtures/` (`run_demo.py`, `keep_data_plugin.py`, `export_graph.py`).

Faster search on a big database: `python3 tools/create_fulltext_index.py` prints the `CREATE FULLTEXT INDEX gx_search ...` statement (dry run); `--yes` creates it. It is the only script that changes the database; the server picks the index up automatically.

Tests: `pip install playwright`, then run `NEO4J_PASSWORD=... python3 tests/run_ui_tests.py`.
- **What it does:** starts `server.py` on a free port, drives Chrome, and writes `tests/results.json` and `screenshots/app-*.png`.
- **Browser:** `CHROME=/path/to/chrome` overrides the browser.

Comparison images: `python3 tools/make_compare.py`; before/after (v1 vs v2 next to the manual): `python3 tools/make_before_after.py --before <v1 url> --after <v2 url>`.

Benchmarks: `python3 tests/bench_ui.py --url http://127.0.0.1:8744/ --gl none` (synthetic 10k/50k/100k-node graphs generated in the browser, each size in its own Chrome), `python3 tests/bench_redraw.py` (one full redraw), `python3 tests/bench_server.py` (API latency against whatever database the server points at).

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
- Firefox or Safari, and a GPU machine.

Bugs found and fixed along the way:
- Popovers closed straight away. The gear menu closed on the same click that opened it.
- Group cleanup was incomplete.
- fCoSE draft quality crashed without randomize.
- The test harness crashed Chrome: returning a cytoscape collection from `page.evaluate` blew the renderer past 3 GB.

## v2 results: large graphs (2026-11-11, PST)

Headless Chrome with `--disable-gpu` (CPU raster, no WebGL) on the 8-core box, 1600×900 viewport. Graphs are synthetic and Cartography-shaped: n/2,500 AWSAccount hubs, each node has a RESOURCE edge to a hub plus one nearby edge (about 2 edges per node). "Before" is v1 (commit d929a3f, cytoscape renderer + fCoSE); "after" is v2. Raw data: `tests/bench-ui-before.json`, `tests/bench-ui-after.json`, `tests/bench-redraw-after.json`.

| Nodes / edges | Version | Time to first render | Force layout | Pan FPS (fit) | Zoom FPS | Pan FPS zoomed in | JS heap after GC | Renderer RSS |
|---|---|---|---|---|---|---|---|---|
| 10k / 20k | before | 10.9 s | 8.8 s | 1.1 | 0.8 | 1.3 | 172 MB | 1,176 MB |
| 10k / 20k | after | 0.7 s | 3.7 s | 60 | 60 | 59.7 | 97 MB | 431 MB |
| 50k / 100k | before | 60.2 s | 45.0 s | 0.3 | 0.2 | 0.4 | 839 MB | 4,353 MB |
| 50k / 100k | after | 2.4 s | 10.4 s | 60 | 60 | 59.7 | 465 MB | 783 MB |
| 100k / 200k | before | did not finish in 420 s | – | – | – | – | – | – |
| 100k / 200k | after | 5.9 s | 18.2 s | 60 | 60 | 59.7 | 927 MB | 1,558 MB |

Read these carefully:
- **The 60 FPS is mostly the cached bitmap.** While the view moves, v2 transforms the last frame; the full redraw comes 140 ms after it stops. A full redraw (`bench_redraw.py`) takes 36 ms at 10k, 225 ms at 50k and 460 ms at 100k when the whole graph is in view (dots), and 6–97 ms zoomed in. So at 50k+ the picture is briefly stale (edges at the screen border) after a pan, then sharpens.
- **The layouts are not the same work.** v1's fCoSE ran to convergence; v2's ForceAtlas2 is time-boxed (2 s + 0.15 ms per node), so "layout" is a budget, not a convergence time. The 100k layout is usable but not as tidy as a converged one.
- **Load time:** most of v1's and early v2's 100k load time was cytoscape's `add()` calling `parallelEdges()` per edge, which is O(degree) and so O(degree²) for hubs (208 s for 100k nodes). v2 skips that cache-clearing call during bulk adds (`addEdgesFast` in `app.js`).
- **WebGL was tried and dropped on this box:** with SwiftShader (software WebGL), sigma.js drew 1.6 FPS at 10k nodes and cytoscape's WebGL renderer 0.3 FPS. On a machine with a GPU, WebGL would likely beat Canvas2D; not measured.
- **Not measured:** the database side on a multi-million-node Neo4j. The server changes are in place (capped search scan, optional full-text index, neighbor summaries sampled at 20,000 rows per node and cached, paginated expand without ORDER BY, UNWIND lookups) and `tests/bench_server.py` is written, but it was not run against a big seeded database.

UI fidelity: `compare-before-after.png` shows the same Cartography neighborhood (acme-prod account, 60 resources, account selected) in the Linkurious manual's force-layout screenshot, v1 and v2, at the manual's 1919×941 viewport. Screenshots: `screenshots/before-after-v1.png`, `screenshots/before-after-v2.png`. The per-feature comparisons (`compare-*.png`, `compare-all.png`) were regenerated with v2.

UI tests after v2 (2026-11-11 16:46 PST): **33/33 passed**, including the new supernode check (163 hidden neighbors → 13 group nodes; loading 10 from AWSEC2Instance left 28) and "no JavaScript errors". A first run of the suite at 16:39 PST failed from "pick a search result" onwards with `cy` undefined; it did not reproduce in a rerun or in isolation, and the cause is unknown.

## Cost

$0. No paid services. Everything runs locally: Neo4j community in a tarball, open-source libraries, synthetic data. It took about an hour of agent time (research downloads started at 13:37 PST, and the tests were green at 14:30 PST), plus an earlier research and build session.

## Limits and risks

- **Trademark.** This is an unofficial recreation for internal exploration. The UI says "Graph Explorer for Neo4j" and contains no Linkurious name or logo. The comparison images embed screenshots from the public Linkurious manual for side-by-side review only. Don't publish them.
- **Read-only safety, in two layers:**
  1. The server rejects write clauses (CREATE, MERGE, SET, DELETE, REMOVE, DROP, LOAD CSV, CALL dbms…, apoc write procedures, and others) after stripping string literals and comments.
  2. Every query runs in a READ-mode transaction with a timeout.

  A regex screen is not a parser. For real use, also connect as a Neo4j user with the `reader` role.
- **No auth on the web UI.** It binds to 127.0.0.1 by default. Don't expose it.
- **Search speed.** Without a full-text index, search scans at most 200,000 nodes, so on a bigger database it can miss matches (the result says the total is capped). Create the index with `tools/create_fulltext_index.py --yes`.
- **Supernodes.** AWSAccount and GCPProject fan out through RESOURCE edges. v2 groups them per category, but the hierarchical and radial layouts of an expanded supernode still give wide, flat fans.
- **Big-graph rendering** is tuned for a CPU-only machine; see the caveats under the v2 results.
- **Saving.** Saved visualizations live in the browser (localStorage). They are not shared and not on a server.
- **Not built:** the timeline, the no-code query builder, SVG/PDF/Excel/GEXF/GraphML export, and the geo mode (see the parity table).

## Status

Ready for demo — Explore-phase experiment. A good next step: point it at a real Cartography Neo4j, using a reader-role user, and see which default styles and queries need tuning.
