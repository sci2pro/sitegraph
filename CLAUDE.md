# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

The MVP is implemented as of 2026-09-13: `crawl`, `serve`, and the browser UI all work end to end and the suite passes. `spec.md` is the authoritative contract — read it before changing behavior. It is unusually specific about intended behavior, and the sections below only summarize the parts that constrain design.

**Stack decisions (resolved):**

- `spec.md` §10 lists Node.js / Express, but that section is illustrative of *components*, not a language mandate. This is a **Python 3.13 project managed by `uv`**. Playwright means the Python package.
- The CLI uses **argparse** from the stdlib rather than a framework, since the surface is only two subcommands.
- The UI is **vanilla HTML/CSS/JS with no build step and no CDN** (`src/sitegraph/ui/`), rather than the Cytoscape.js suggested in §10. A vendored or CDN-loaded graph library would either bloat the package or break the offline guarantee; the force layout and pan/zoom are ~200 lines and the node cards render exactly as §9 sketches them, which a generic graph library makes *harder*.

## Commands

`uv` (0.8.5) owns the environment.

```bash
uv sync                      # create/refresh .venv from pyproject.toml
uv add playwright            # add a runtime dependency
uv add --dev pytest          # add a dev dependency
uv run playwright install chromium   # fetch the browser binary (required before any crawl)

uv run pytest                          # full suite (~30s: the e2e tests drive a browser)
uv run pytest -m "not e2e"             # fast unit tests only (<1s, needs no browser)
uv run pytest tests/test_urls.py::test_strips_fragment   # single test
```

The `sitegraph` entry point is wired through `[project.scripts]`; `uv run python -m sitegraph` is equivalent. The two user-facing commands:

```bash
uv run sitegraph crawl http://localhost:3000
uv run sitegraph serve
```

## Architecture

Four stages, deliberately decoupled — the crawler writes files, the server only reads them:

```
crawl  →  .sitegraph/  →  serve (HTTP)  →  browser UI
          (JSON + WebP)     reads only       graph view + contact sheet
```

Modules, in dependency order — each layer may import the ones above it and nothing below:

| module | owns |
| --- | --- |
| `urls.py` | URL identity: normalization, resolution, origins, link filtering |
| `graph.py` | The graph model: nodes, IDs, edge derivation, serialization |
| `store.py` | The on-disk layout and atomic JSON writes |
| `crawl.py` | The BFS walk and the Chromium `Renderer` |
| `serve.py` | The read-only HTTP server and its route table |
| `ui/` | `index.html`, `styles.css`, `app.js` — the explorer |

`graph.py` and `urls.py` have no browser or filesystem dependency at all, which is what makes the interesting rules directly testable. `serve.py` must not import `crawl.py` (spec §8 — the server never crawls); `tests/test_serve.py` enforces this in a subprocess.

### The on-disk layout is a public interface

`serve` and the UI depend on it, and a user may read it by hand:

```
.sitegraph/
├── graph.json          # whole graph; independent of the per-page files
├── pages/000001.json   # one record per page
└── screenshots/000001.webp
```

The HTTP routes over it are equally fixed — `GET /graph.json`, `/pages/<id>.json`, `/screenshots/<id>.webp`, plus the three static assets. The filenames live in `store.py`; the routes in `serve.py`.

`graph.json` holds `{root, nodes[], edges[]}` and is self-contained — everything the graph view needs to draw is in it. Per-page JSON files hold the richer record (`status`, `depth`, `links`). Both are written **incrementally** as the crawl proceeds, so an interrupted crawl still yields usable data.

### Invariants that are easy to get wrong

- **Node identity is a normalized URL.** Fragments are discarded (`/about#team` → `/about`) but **query strings are significant** (`/search?q=foo` and `/search?q=bar` are distinct nodes). Normalization is the core correctness concern: getting it wrong silently produces duplicate nodes, or silently merges genuinely different pages. `urls.py` is the single owner; a second normalizer anywhere else is the bug.
- **IDs are zero-padded strings** (`"000012"`), not integers. They are referenced by `edges[].source/target` and `root`. Keep them strings end-to-end — parsing `"000012"` as an int and re-serializing it corrupts the graph.
- **Node IDs are visit order (BFS), which is also ID order.** Nodes are appended, never reordered, so `nodes[]` needs no sort. Inserting a node out of order would renumber nothing but would break that assumption in `Graph.get`.
- **Edges are derived from the nodes' links when the graph is serialized, never appended.** A page's links are known when it is visited; its *targets* may not exist yet. Deriving on read means an edge can never reference a missing node.
- **A link to a page that was never captured is not an edge.** It stays in the page record's `links` (which is where `--max-pages` leftovers remain visible, and the inspector shows them as "not crawled").
- **`graph.json` never references a page record that is not on disk.** `crawl` writes the page record first, then the graph, and registers a node only once its visit completes. JSON writes go through a temp file and `os.replace`, so a killed crawl leaves parseable files.
- **Failed pages still appear in the graph**, marked `failed: true` with `screenshot: null`, rather than being dropped. Note that a *failed* page is one that could not render at all — an HTTP 404 renders fine and is recorded normally with `status: 404`.
- **`screenshot` paths are relative** to the output directory (`screenshots/000012.webp`), never absolute — they must resolve for a `serve` run rooted elsewhere.
- **Same-origin by default.** The crawl must not leave the origin of the start URL.
- **One edge per ordered pair**, even when many links point from A to B.
- **`serve` binds to 127.0.0.1 only.** It prints `localhost` for convenience but must not be reachable from the network.
- **The UI inserts page-derived strings as text nodes, never as HTML.** Titles come from a site we do not control; `innerHTML` anywhere in `app.js` would make the explorer scriptable by the crawled site.

## Scope

`spec.md` §11 enumerates what the first version explicitly must *not* attempt (auth automation, forms, modal discovery, SPA states without distinct URLs, visual regression, SEO/a11y analysis, multiple browser engines, and more). Treat that list as binding — the MVP's stated success criterion is narrow: let a developer see what pages an app contains, what they look like, and how they connect, within seconds.

## Layout

`src/`-layout — the package lives in `src/sitegraph/` and imports as `sitegraph.*`; tests mirror module names under `tests/`. Test helpers that are not test files themselves: `tests/fixture_site.py` (an in-process site, including one page that deliberately fails to render) and `tests/serving.py` (drives a real server on an ephemeral port).

The version in `src/sitegraph/__init__.py` is the single source of truth, read by hatchling via `[tool.hatch.version]`, so bump it there and not in `pyproject.toml`.

`.gitignore` excludes `.venv/`, `.sitegraph/` (crawl output, which is large and regenerable), Python caches, and local env files.
