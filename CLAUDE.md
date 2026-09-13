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

The `sitegraph` entry point is wired through `[project.scripts]`; `uv run python -m sitegraph` is equivalent. The three user-facing commands:

```bash
uv run sitegraph crawl http://localhost:3000
uv run sitegraph crawl http://localhost:3000 --resume    # carry on a capped crawl
uv run sitegraph login http://localhost:3000/login       # only for signed-in crawls
uv run sitegraph serve
```

## Stopping early, and carrying on

`--max-pages` caps the pages captured **in one run**. Without `--resume` that cap is a dead end: the crawl stops, and the pages it had discovered but not reached are left with nothing to pick them up. With `--resume` the cap becomes a budget, and *the same command run again* continues from where the last run stopped:

```bash
uv run sitegraph crawl http://localhost:3000 --max-pages 500            # 500 captured, 312 waiting
uv run sitegraph crawl http://localhost:3000 --max-pages 500 --resume   # 500 more, counter climbing
```

**The frontier is reconstructed, not stored.** There is no queue file: every page record already lists the links it contained, so the pages discovered but never reached are exactly the ones named in some record that have no record of their own. That is why there is no second file to keep in sync with `graph.json`, and why resuming works the same whether the previous run stopped at `--max-pages`, met Ctrl-C, or died outright — `crawl.py::resume_state` rebuilds both the graph and the queue from what is on disk.

Consequences worth knowing:

- **A resumed run never re-captures a page.** IDs, screenshots, titles and edges from earlier runs are kept exactly; new pages continue the ID sequence.
- **Pages that failed to render go back in the queue**, so a page that failed because the server was still starting up heals on the next run. A permanently dead page is retried every time, so the "still to capture" count will not reach zero — the summary says so rather than leaving the user to wonder why run five is still doing something.
- **Resuming a crawl of a different site is refused.** The stored root is compared against the URL given; a mismatch is an error, because appending to another site's graph would corrupt it.
- **`--resume` on a finished crawl opens no browser.** An empty queue prints and exits before the renderer is built, so it costs nothing and needs no Chromium.

The frontier is ordered shallowest-first, approximating the breadth-first walk a single run would have made. One known imprecision: a page reachable both through a captured page and through one that is *still* unvisited takes the depth implied by the captured page, which can be one level deeper than its true shortest path. Depth is display metadata, so this is cosmetic.

## Signed-in crawls

Pages behind a login are reached by reusing a session, not by automating one:

```bash
uv run sitegraph login http://localhost:3000/login       # opens a real window
uv run sitegraph crawl http://localhost:3000 --storage-state .sitegraph/session.json
```

`login` opens a visible Chromium and waits for **a human** to authenticate — SSO, MFA, and everything else that cannot be scripted included. sitegraph never sees a credential and never fills a form; it saves the resulting session (Playwright storage state: cookies plus localStorage) and `crawl` replays it. Spec §11 keeps login automation out of scope, and this stays on the right side of that line.

- **Treat the session file as a credential.** Live session cookies are enough for anyone holding the file to act as that user. It defaults to `.sitegraph/session.json`, which is both gitignored and unrouted by `serve` — but it is plaintext, so it should not be copied around with a crawl directory.
- **A signed-out crawl does not fail, it lies.** A protected page that redirects to a login page gets recorded under its own URL with the *login* page's content and a 200. Nothing errors, and the graph looks fine. The same URL crawled with and without a session is the only way to see the difference — `tests/test_login.py` pins exactly that pair.
- **A session that expires mid-crawl** has the same effect from that point on: the remaining pages silently become the login page. Re-run `login` and crawl again.
- **SSO redirects leave the origin**, so the crawl stops at that link rather than following it. That is the same-origin rule working as intended, not a bug.
- `user:pass@host` in the start URL also reaches Basic/Digest-protected sites, because Chromium caches the credentials for the origin. Prefer `--storage-state`: the credentials-in-URL form is written in plaintext into every node URL in `graph.json` and shows up in the inspector.

## Architecture

Four stages, deliberately decoupled — the crawler writes files, the server only reads them:

```
crawl  →  .sitegraph/  →  serve (HTTP)  →  browser UI
          (JSON + WebP)     reads only       graph view + contact sheet
```

Modules, in dependency order — each layer may import the ones above it and nothing below:

| module     | owns                                                             |
| ---------- | ---------------------------------------------------------------- |
| `urls.py`  | URL identity: normalization, resolution, origins, link filtering |
| `graph.py` | The graph model: nodes, IDs, edge derivation, serialization      |
| `store.py` | The on-disk layout and atomic JSON writes                        |
| `crawl.py` | The BFS walk and the Chromium `Renderer`                         |
| `login.py` | Interactive sign-in that saves a session for `crawl`             |
| `serve.py` | The read-only HTTP server and its route table                    |
| `ui/`      | `index.html`, `styles.css`, `app.js` — the explorer              |

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
- **A page's links live in its page record, not in the graph.** That is what makes `--resume` possible without a queue file, and it is the reason a page record must keep the *full* link list (including targets that never became nodes) rather than only the ones that did.
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
