# linkurio-less

A Linkurious-style read-only graph explorer for any Neo4j database. It's Linkurious with less: the graph view and browsing. The page calls a JSON API, and that API runs read-only Cypher over Bolt. Cartography is the bundled sample graph, the default styles, and the built-in queries.

**Idea:** Linkurious-style graph explorer for Neo4j (view and browse only). Cartography is the sample graph.
**Source:** Sacha direct
**Status:** Kept. Sacha approved it at the Nov 13, 2026 demo, and it moved here from its prototype folder in [grok-tinker-lab](https://github.com/sachafaust/grok-tinker-lab/tree/main/prototypes/2026-11-11-linkurious-neo4j), with full git history.

## Run it on your machine

```bash
git clone https://github.com/sachafaust/linkurio-less.git && cd linkurio-less
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
| `--live-scale` | load the minimum supported graph: exactly 15,000,000 nodes and 60,000,000 edges, streamed in batches. About 16 GB of RAM for Docker and about 30 GB of disk. `--nodes` and `--edges` override the totals. |
| `--keep-data` | don't reload |
| `--max-nodes N --max-edges M` | safety ceiling for one response. Omit both and the ceiling follows this machine's RAM. The first launch writes a working set to `gx.local.json` from CPU and RAM. The page can add the GPU and save a new one for the next launch. That file is gitignored. |
| `--test` | install Playwright + Chromium, load `--both`, run the budget tests and the UI suite (33 checks, screenshots in `screenshots/`) plus a quick browser benchmark and an API benchmark (`.run/bench-*.json`), then keep serving (`--no-serve` to stop). With `--live-scale`, run the budget tests and the 15M/60M checks instead of the UI suite. |
| `--neo4j-port 17687 --neo4j-http-port 17474` | if 7687/7474 are taken (the script tells you) |
| `--neo4j-uri neo4j+s://host:7687` | use your own Neo4j, local or remote, instead of Docker. `NEO4J_URI` in the environment or in a gitignored `.env` does the same. Its data is left untouched and opened read-only. Password from `NEO4J_PASSWORD`, `.env`, or a prompt. Also `NEO4J_USER` and `NEO4J_DATABASE`. |
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

The same explorer opens any Neo4j 5 database. The bundled styles know Cartography's labels, and any other database still gets a display category.
- **Display category:** A node can carry several labels (Cartography uses forms such as `AWSEC2Instance:EC2Instance:ComputeInstance`). The server picks one display category per node, using a priority list in `cartography_styles.json`, then the longest non-generic label.
- **Defaults:** each category has a default color, icon and caption.
- **Built-in queries:** `queries.json` holds 9 read-only security queries, such as internet-exposed instances, cross-account role trusts, public buckets, and GitHub outside collaborators.

Why cytoscape.js: it is MIT-licensed and needs no build step, has a good graph model (selectors, traversal, compound nodes for node grouping) and the breadthfirst/concentric layouts. Its own renderer was too slow for big graphs on a CPU-only machine (about 1 FPS at 10k nodes, see below), so v2 draws the model itself:
- **`static/render.js`:** a spatial grid, three levels of detail (dots, batched circles, full detail with icons, labels and badges), label decluttering, a cached bitmap that is transformed while the view moves (used above 3,000 visible nodes or when a full frame takes more than 12 ms; a full redraw follows 140 ms after the view settles), and cytoscape-style events (tap, dbltap, cxttap, drag, hover) on the model.
- **Linkurious look:** white ring around nodes, hidden-neighbor badge at the top-right in the node color, labels only when zoomed in (the selection always keeps a bold label on a white box), white halos on the selected neighborhood, grey 2 px edges with arrows, no edge captions unless asked, no connection-status line under the search bar.
- **Layout:** ForceAtlas2 (graphology, Barnes-Hut above 2,000 nodes) in a web worker, time-boxed (1.2 s for small graphs, up to 20 s at 100k nodes), streaming positions to the canvas. fCoSE is still vendored but no longer used: in the headless model it collapsed star-shaped graphs.
- **Supernodes:** double-clicking a node with more hidden neighbors than the expand cap (50) adds one group node per (category, edge type). The caption is the label, the relationship type, and the count, for example `AWSS3Bucket · RESOURCE ×4,213`. Double-click a group, or its menu, to load the next 50 members, or filter them by name, tag, id, or account. Icons are Material Design paths (@mdi/js, Apache-2.0), built by `tools/build_icons.py`. The font is Roboto (OFL).

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
| Large graphs (Linkurious handles supernodes with an expand limit) | Done (v2) | The canvas holds a working set suggested from this machine's CPU, RAM, and GPU. The server enforces it. Hubs are grouped per label. A separate data-model view reads whole-graph counts. The renderer bench still stands: 100k nodes / 200k edges load in 6 s and pan at 60 FPS (cached bitmap) on a CPU-only box. |
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

Run it against your own Neo4j:

```bash
cd linkurio-less
cp .env.example .env                 # gitignored. Or export the same variables in the shell.
# NEO4J_URI=bolt://localhost:7687
# NEO4J_USER=neo4j                   # ideally a reader-role user
# NEO4J_PASSWORD=...
# NEO4J_DATABASE=neo4j               # optional
export SEARCH_FULLTEXT_INDEX=...     # optional: name of a full-text index for faster search
python3 server.py                    # http://127.0.0.1:8744
                                     # HOST, PORT, QUIET, EDGE_WALK_MAX (2000)
                                     # MAX_NODES, MAX_EDGES, EXPAND_LIMIT, CYPHER_ROW_LIMIT
                                     # are safety ceilings; omit them and they follow RAM
```

The environment wins over `.env`. Keep the password in one of those two places. `gx.local.json` stores the canvas working set. A bolt URI with a user or password in it is refused. `GX_ENV` points at a different env file.

### API

The page calls these JSON endpoints on the same host. Each graph read runs in a Neo4j READ transaction. `POST /api/cypher` takes one read-only Cypher statement and returns the nodes, relationships, and rows it produced.

| Method | Path | What it returns |
|---|---|---|
| GET | `/api/status` | connection, counts, the working set, and the safety ceiling |
| GET | `/api/schema` | labels, relationship types, and counts |
| GET | `/api/styles` | default colors, icons, and captions |
| GET | `/api/queries` | the built-in saved queries |
| GET | `/api/search` | nodes or relationships matching a string |
| POST | `/api/nodes` | nodes by element id |
| POST | `/api/neighbors` | neighbor counts for the selection |
| POST | `/api/expand` | one page of neighbors |
| POST | `/api/edges-between` | relationships among the nodes already on screen |
| POST | `/api/cypher` | the result of one read-only Cypher query |
| POST | `/api/budget/suggest` | a working set for the reported CPU, RAM, and GPU |
| POST | `/api/budget/refine` | writes the browser GPU into an automatic working set once |
| POST | `/api/budget` | saves the working set to `gx.local.json` for the next launch |

To try it on this machine, seed a **local** test database. The synthetic set is Cartography-shaped. The fixture snapshot is Cartography's own demo data.
- **What it creates:** 1,099 nodes and 2,430 relationships covering AWS, GCP, GitHub and Okta. The data is deterministic (seed 42), and all names and ids are fake.
- **Safety:** the seeder refuses non-localhost URIs. `--wipe` empties the database first.

```bash
NEO4J_PASSWORD=... python3 seed_cartography.py --wipe
```

`seed_cartography.py` reads the same environment and `.env`. It still refuses a URI that is not localhost.

### Real-schema Cartography data (no cloud credentials)

Does Cartography ship a Docker image with mock data? **No.** `ghcr.io/cartography-cncf/cartography` is the CLI and does a real sync. But the repo has a demo seeder (`python -m demo`, no AWS/GCP/Okta seed) and test fixtures that load mock data through Cartography's own load functions. Details, links and what failed upstream: [`/home/box/shared/rnd/linkurious-reference/CARTOGRAPHY-MOCK-DATA.md`](/home/box/shared/rnd/linkurious-reference/CARTOGRAPHY-MOCK-DATA.md).

A snapshot of that data (demo seeds plus the AWS, GCP, Okta and GitHub integration-test fixtures: 6,939 nodes / 11,054 relationships, only fixture values and AWS documentation example keys) is in `data/cartography-fixtures.json.gz`:

```bash
NEO4J_PASSWORD=... python3 seed_cartography.py --wipe --fixtures      # real Cartography schema
NEO4J_PASSWORD=... python3 seed_cartography.py --wipe --scale 100 --hub 100000   # big synthetic graph + a supernode
NEO4J_PASSWORD=... python3 seed_cartography.py --live-scale --dry-run            # print the 15M / 60M plan, no connection
```

To rebuild the fixture snapshot from a Cartography checkout, see `tools/cartography_fixtures/` (`run_demo.py`, `keep_data_plugin.py`, `export_graph.py`).

### Minimum environment

The explorer has to stay usable on a Cartography graph of at least **15,000,000 nodes and 60,000,000 relationships**. The server keeps a working set in `gx.local.json`. That file is not in git. The first launch creates it from this machine's CPU count and RAM, and the first browser open adds the GPU when the page can see one (a software GPU gets a smaller set). Canvas budget in the status bar edits it, Suggest for this machine fills the fields again, and Save writes the file so the next launch uses it. The server stops each response at the room left in that working set, and will not go past its RAM ceiling (or past `--max-nodes` / `--max-edges` when you pass them). The data-model view reads label counts and relationship counts, so its cost stays flat as the graph grows.

The default `python3 run_local.py` still loads the 6,939-node fixture snapshot. The large graph is its own command:

```bash
python3 run_local.py --live-scale
```

Neo4j is started with a 4G heap and an 8G page cache (about 16 GB of RAM for Docker, about 30 GB of disk). Nodes are created in batches of 5,000, then id indexes, then relationships. Account 0 is a supernode that owns 2,000,000 resources, so expand meets a real hub and still returns one page. An existing `gx-neo4j` container whose heap is under 2G or whose page cache is under 4G is left in place; the command stops and tells you to run `python3 run_local.py --reset` first, which deletes the local volume.

```bash
python3 run_local.py --live-scale --test --no-serve
```

runs `tests/test_budget.py` and `tests/test_live_scale.py` against that database. The 33 UI checks stay on the small synthetic estate (`python3 run_local.py --test`), because they look for the acme-prod account.

`tests/test_budget.py` checks the budget math on its own, and the plan totals (15,000,000 / 60,000,000 exactly, plus a 400-node graph whose 1,600 edges are unique). When a local Neo4j is already up it also starts a second server with a 6-node budget and checks the caps. It does not wipe. `tests/test_live_scale.py` skips unless `REQUIRE_LIVE_SCALE=1`, and then it requires the connected database to be at least this size. The full ingest was not run as part of the unit tests.

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
- **No auth on the web UI.** It binds to 127.0.0.1 by default. Don't expose it. Neo4j credentials come from `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, and `NEO4J_DATABASE`. A gitignored `.env` fills whichever of those are unset. The page and `gx.local.json` do not store the password.
- **Search speed.** Without a full-text index, search scans at most 200,000 nodes, so on a bigger database it can miss matches (the result says the total is capped). Create the index with `tools/create_fulltext_index.py --yes`.
- **Working set.** Each response stops at the room left on the canvas. With no `gx.local.json` yet, the first launch writes one from CPU cores and RAM: an 8 GB machine starts at 2,000 nodes and 5,000 edges, and a larger machine starts higher. The first browser open adds the GPU. A software GPU (SwiftShader, llvmpipe) cuts the suggestion down. Canvas budget lets you change nodes, edges, neighbors per expand, and rows per query, up to the server ceiling, and Save writes `gx.local.json` for the next launch. The file is gitignored. `GX_CONFIG` points somewhere else when a test should keep its own file. `--max-nodes`, `--max-edges`, `MAX_NODES`, `MAX_EDGES`, `EXPAND_LIMIT`, and `CYPHER_ROW_LIMIT` set that ceiling when you want a fixed one. User Cypher is wrapped in `CALL { ... LIMIT n }` so a query with no LIMIT stops at the row cap. Edge fill skips a node with more than 2,000 relationships (`EDGE_WALK_MAX`), which is the call that used to time out on a hub. When the canvas is full the page refuses the add and offers to remove filtered nodes or collapse a hub. A saved option can drop the oldest unpinned nodes instead. The status bar shows the working set.
- **Supernodes.** AWSAccount and GCPProject fan out through RESOURCE edges. Above the expand cap they become one group node per label and edge type (`AWSS3Bucket · RESOURCE ×4,213`). Opening a group loads 50 members, or filters by name, tag, id, or account. Hierarchical and radial layouts of an expanded hub still give wide, flat fans.
- **Minimum graph.** `python3 run_local.py --live-scale` loads 15,000,000 nodes and 60,000,000 edges. See "Minimum environment" above. The renderer stays Canvas2D at this working-set size.
- **Big-graph rendering** is tuned for a CPU-only machine; see the caveats under the v2 results.
- **Saving.** Saved visualizations live in the browser (localStorage). They are not shared and not on a server.
- **Not built:** the timeline, the no-code query builder, SVG/PDF/Excel/GEXF/GraphML export, and the geo mode (see the parity table).

## Status

Ready for demo — Explore-phase experiment. Point it at any Neo4j database with a reader-role user. The Cartography styles and queries are the starting set for that kind of graph.
