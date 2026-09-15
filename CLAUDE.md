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
uv run sitegraph crawl http://localhost:3000 --workers 8     # render more at once
uv run sitegraph crawl http://localhost:3000 --viewport 390x844   # capture a phone layout
uv run sitegraph crawl http://localhost:3000 --per-pattern 1   # one page per route
uv run sitegraph crawl http://localhost:3000 --skip /admin    # leave a path alone
uv run sitegraph crawl http://localhost:3000 --full-page     # the whole page, not the fold
uv run sitegraph crawl http://localhost:3000 --dry-run       # look before writing
uv run sitegraph crawl http://localhost:3000 --resume        # carry on a capped crawl
uv run sitegraph login http://localhost:3000/login           # only for signed-in crawls
uv run sitegraph serve
```

## Whole-page captures

```bash
uv run sitegraph crawl localhost:3000 --full-page
```

Spec §7 captures the *viewport* and calls full-page captures unnecessary for
the first version. `--full-page` is that, opt-in: a long page arrives as one
tall image rather than its first 900 pixels.

```
/                     1440x900      1440x900
/long                 1440x900      1440x5120     <- six screens
/short                1440x900      1440x900      <- nothing to scroll
```

`--viewport` still decides the width the page is laid out at; only the height
of the picture changes, and a page shorter than the viewport is unchanged
(Chromium will not return a picture smaller than the window it rendered in).
It is not free: the same three-page crawl went from 92 KB to 352 KB of WebP,
because a tall page is many times the pixels.

**The viewer had to learn about it, and that was not optional.** A capture
taller than it is wide is cropped to its top sliver by `object-fit: cover` in
a box shaped like a screen — so `--full-page` would have produced data the
inspector could not show, which is precisely what the flag was asked for. The
inspector's shot is now allowed its own height and the panel scrolls to it,
decided by asking the *image* rather than the graph (`thumb(..., {grow: true})`
in `app.js`), so a tall `--viewport` behaves the same way without a second
code path. Node cards keep the crop: a card is a thumbnail, and one page's
height is not a thumbnail.

**Some pages can never be captured whole, and saying so is the feature.** An
app shell — `html,body{height:100%;overflow:hidden}` with the content in an
inner `overflow:auto` box, which is what any pinned sidebar or fixed header
produces — never grows its *document* past the window, so `full_page=True` has
nothing to reach for and correctly returns one viewport. The screenshot is then
indistinguishable from that of a genuinely short page, which is how this turns
into a bug report against `--full-page`. It is not one: the page really is one
viewport tall. `_OVERFLOWS_JS` asks the page whether it is in this state (the
document is not scrollable *and* an element inside it overflows past the
document), `PageResult.scrolls_inside` carries the answer, and the summary
names the affected paths — capped at `FOLD_ONLY_SHOWN`, because a hundred
trapped pages are one fact rather than a hundred lines. Asked only when
`--full-page` was asked for, and only reported for a capture that actually
happened.

## The card-sized copies

Every captured page leaves two pictures in the crawl directory:

```
.sitegraph/
├── screenshots/000001.webp   # the capture, at --viewport
└── thumbs/000001.webp        # 336px wide, for the graph view
```

The graph and the contact sheet draw from `thumbs/`; the inspector draws from
`screenshots/`. An `<img>` decodes at its *intrinsic* size whatever it is
painted at, so pointing a few hundred node cards at full captures made the
browser decode a few hundred 1440x900 images to paint each one the size of a
full stop — and the interface stalled in bursts while it did. Measured on 500
distinct routes, with every card crossing the viewport:

| capture           | loaded  | worst frame | main thread blocked |
| ----------------- | ------- | ----------- | ------------------- |
| 1440x900          | 12.4 MB | 309 ms      | 952 ms              |
| 1440x3790 (`--full-page`) | 49.7 MB | **2167 ms** | **8426 ms** |
| 336x210 copy      | 0.5 MB  | 58 ms       | **0 ms**            |

`--full-page` is the case that hurts, which is the one to check a change
against. 336px is twice the 168px card, so a card stays sharp through a couple
of steps of zoom; 32px was measured too and is no faster, so the extra pixels
cost nothing. A copy is roughly a tenth the bytes of its capture and adds no
measurable crawl time — 61 pages took 12.6s without copies and 12.5s with them.

**Chromium makes the copy**, because it is the only image encoder this project
has: there is no imaging library in the dependency list. The worker that took
the capture hands it to a scratch page as a `data:` URL, draws it into a canvas
at `THUMB_WIDTH`, and reads the result back as WebP. A scratch page rather than
the page just visited, because a site's own CSP can forbid a `data:` image and
its scripts have no business running while we do this. Overriding the device
scale factor through CDP would be cheaper still and does not work — it left the
capture byte-identical at 1440x900.

Three properties worth keeping:

- **`thumb` is emitted only when there is one**, like `shape`. A directory
  written before copies existed produces exactly the payload it produced then,
  and the UI reads an absent `thumb` as "draw the capture" — slower, still
  correct. Asking for the copy unconditionally would 404 at a file that was
  never written, which the browser tests catch as a failed request.
- **A copy only ever accompanies a capture.** A page that failed to render has
  neither, because a copy of a picture that does not exist would be a graph
  entry pointing at nothing.
- **The copies live in `thumbs/`, not beside the screenshots**, so that
  `screenshots/` stays exactly one file per rendered page — a property the
  tests and `--resume` both count on.

`--resume` does not backfill: pages captured before copies existed keep their
capture and draw from it. Re-crawl, or leave them; both work.

## Looking before writing

```bash
uv run sitegraph crawl localhost:3000 --dry-run
```

A dry run walks the site exactly as a real crawl would — same renderer, same
frontier, same links — and writes nothing at all, not even the output
directory. Then it says what it found, by route:

```
Dry run — nothing was written.
Would capture 612 page(s) in 41.3s — 612 discovered, 1204 edge(s).

  pages  share  route
    480    78%  /courses/:id
    132    22%  /search?q=:id

/courses/:id alone is 480 of the 612 pages found.
  --per-pattern 1       capture one page of each route, not 480
  --skip /courses       leave the route out of the crawl
```

The point is to answer "is this a hundred pages or ten thousand, and if it is
ten thousand, which route is eating them" before spending the time and the
disk. Two things follow from that:

- **The workers render into a throwaway directory** (`tempfile.mkdtemp`), not
  into `--output`. `Renderer.visit` promises a path it can write a capture to,
  so handing it one under a directory nobody created would break that promise
  for every renderer except `ChromiumRenderer`, which skips the capture
  entirely when told to. The directory is removed on the way out, including
  after an interrupt.
- **Both levers are named, and neither is chosen.** The report says the route is
  large; whether that is bulk to skip or content to keep is the user's call, and
  a tool that decides for them gets it wrong on the site where it matters.

A suggestion needs the route to clear **both** thresholds — at least 5 pages
*and* at least a tenth of the crawl. The share alone would suggest leaving out a
two-page route on a four-page site; the count alone would miss a route that is
small here and enormous next door. A route parameterised from its first segment
(`/:id`) gets no `--skip` suggestion, because its shortest prefix is the whole
site — advice to skip everything is not advice.

## Leaving paths alone

```bash
uv run sitegraph crawl localhost:3000 --skip /admin --skip 're:\.pdf$'
```

`--skip` is repeatable, and `urls.py::SkipRules` owns what a pattern means:

- **A literal is a path prefix that ends on a segment edge.** `/admin` covers
  `/admin`, `/admin/` and `/admin/users`, but *not* `/administrators`, which is
  a different page that merely starts with the same letters. A plain
  `startswith` would swallow every neighbour of what you meant to skip, and you
  would only find out by noticing what went missing.
- **Anything else is a regex, marked `re:`.** The prefix removes the guesswork:
  deciding by "does it look like a regex" would make `--skip /report.html` a
  pattern whose `.` matches any character — harmless there, and a nasty
  surprise the first time it is not.
- Each kind is matched against the path (a regex against path and query, so
  `re:page=\d+` works). Neither matches the host, because a crawl is on one
  origin and repeating it in every pattern would be noise.
- A pattern that cannot match is refused at the command line, not silently
  ignored: `--skip admin` is an error naming `'/admin'` and `'re:admin'`, and
  so is a regex that does not compile.

Three consequences worth knowing, and they are the same ones `--per-pattern`
has, for the same reason:

- **It is lossy.** Nothing behind a skipped page is discovered — the fixture's
  `/deep`, reachable only through a skipped `.pdf`, does not appear at all.
- **The skipped URL is still recorded as a link.** The page that pointed at it
  did point at it, so the inspector shows it as "not crawled" rather than
  pretending the link is not there. The summary says how many were left out.
- **A pattern matching the start URL is refused**, because the alternative is
  an empty crawl that looks like a broken one.

Skipping is checked before the `--per-pattern` allowance, so a page nobody will
capture does not use up the budget of the route it happens to share.

## One card per route, not per row

A site's node count should track its *routes*, not its database rows. Five
hundred courses are one route rendered five hundred times, and drawing them all
makes the graph's complexity mirror the data rather than the application.

    /courses/123           ┐
    /courses/456           ├─▶  one card, `/courses/:id`, badged ×500
    ... 498 more           ┘

`urls.py::url_shape` is the single owner of the rule: **identifier-shaped path
segments and query values become `:id`**, where identifier-shaped means
all-digits or a UUID. Nothing else. A rule broad enough to catch `/users/alice`
would also merge `/about` with `/contact` and collapse every top-level page into
one node, so slugs are deliberately not folded; that is the known gap.

- **The payload changes by one optional field.** `to_dict` writes `"shape"`
  only for a page that is one instance of a route, so a site with no
  identifiers in its URLs produces exactly the graph it produced before shapes
  existed, and an absent `shape` means "this URL is its own shape".
- **The view folds at load** (`app.js::foldByShape`), keeping only the
  representative in `state.nodes`. That is what leaves every id-keyed structure
  below it — positions, cards, edges, selection, filtering — untouched.
  `state.byId` still holds *every* page, so the inspector can open any instance
  and links between pages still resolve.
- **Edges are folded too**, through the representative, with self-route edges
  dropped and the rest deduplicated. Otherwise the edges multiply by the
  instance count alongside the nodes.
- **The representative is the first member that rendered**, not simply the
  first, so a route does not look broken because instance one happened to 404.
  Under a pool "first" means first *committed*, which is completion order.
- **The inspector lists the instances**, so folding is never a way of losing
  pages: every one is one click away, with its own status.

`--per-pattern K` is the other half, and it is **opt-in because it is lossy**: a
page linked only from an instance that was passed over is never discovered.
Crawling the first page of each route instead of all five hundred is a real
saving on a large site and a real risk of an incomplete graph, so it is the
user's call rather than a default. The pages passed over stay in the referring
page's `links` (visible as "not crawled"), and `--resume` with a larger cap
takes them.

## Capture size

`--viewport WxH` sets both the size pages are rendered at and the size of every
screenshot; 1440x900 (spec §7) unless given. It is a *viewport*, not a resize:
a narrow one gets the site's responsive layout, so the screenshots show what
that device would see. That distinction is the whole point — `--viewport
390x844` on a site with a mobile breakpoint captures the mobile page, whereas
resizing the image afterwards would just give a smaller desktop page.

The flag reaches the browser through `crawl(viewport=...)` →
`ChromiumRenderer`. `cli.DEFAULT_VIEWPORT` spells the default out rather than
importing it, so that `--help` and shell completion do not pay for the
Playwright import that the lazy command imports exist to avoid; a test fails if
it drifts from `crawl.VIEWPORT_*`.

For a capture of the whole page rather than the fold, see `--full-page` above.

Node cards use
`object-fit: cover` with `object-position: top left`, so a portrait capture is
cropped to the top of the page — a header-shaped thumbnail — rather than
squashed. Verified by crawling the fixture at 390x844 and 1920x1080.

## Concurrency

Rendering a page is mostly waiting — for navigation, and for the network-idle
settle — so the crawler renders several at once. Measured on a 25-page local
site: 15.8 s at one worker, 4.7 s at four, 3.2 s at eight.

```bash
uv run sitegraph crawl http://localhost:3000              # a few workers: it is this machine
uv run sitegraph crawl https://staging.example.com        # one: it is someone else's
uv run sitegraph crawl https://staging.example.com --workers 4   # your call
```

The automatic count is `min(4, cores)` for a loopback host and **1 for
anything else**, so a real server gets a single connection unless you ask for
more. `--workers N` overrides it either way. `urls.py::is_loopback` decides;
`crawl.py::worker_count` applies it.

**One writer, N render workers.** The writer is the thread that calls `crawl`,
and it owns everything — the frontier, the `queued` set, the `Graph`, the
counters, and every write to disk. Workers own nothing shared: each builds its
own renderer (Playwright's sync API binds its driver to the thread that made
it), serves jobs from a queue, and closes it on the way out. That is why none
of this needs a lock, and why `graph.json` cannot be written from two places.

**IDs are allocated when a page is committed, not when it is dispatched.** A
worker needs a filename before its node has a number, so it captures to
`screenshots/.incoming/<seq>.webp` and the writer renames it into place at
commit. Registering the node early instead would put not-yet-captured pages
into `graph.json` — which is rewritten on every commit — so the file would
briefly reference page records that do not exist, and `serve` would 404 them.
The consequences of commit-time allocation are worth knowing:

- **In-flight pages are invisible on disk**, so an interrupted visit simply
  vanishes: nothing was published, and `--resume` finds the URL again in the
  record of whatever page linked to it.
- **IDs stay dense and commit-ordered**, so `resume_state`'s density check
  keeps working and an interrupted run never leaves gaps.
- **With one worker nothing changes at all** — job FIFO reproduces BFS order,
  so IDs, depths and the incremental-publish sequence are exactly what they
  were before concurrency existed. All the crawl tests that assert ordering run
  at `workers=1` for that reason, including the e2e fixture.
- **With more than one worker, IDs follow completion order** and are not
  reproducible between runs — as are the progress lines, and the depth of a
  page reachable by two paths. The *content* is identical: same URL set, same
  edges, same root. `tests/test_e2e.py` asserts both halves of that.
- **A capped crawl is the exception to that.** A page's links join the frontier
  when the page finishes, so with `--max-pages` and a pool, *which* pages make
  the last few slots is a race: a crawl of the fixture that stopped at eight
  took `/missing` in one run and `/courses/new` in another, both depth two.
  Pages are dispatched in frontier order and the cap counts dispatches, so
  nothing is overshot — but the cut line moves. Use `--workers 1` when a capped
  crawl needs to be reproducible.

**A signed-in crawl's workers share one session.** Playwright's sync API gives
no way to hand two threads one browser context, so each worker has a cookie jar
of its own — and a site that rotates its session cookie would leave all but the
first holding a dead token, which is answered with a redirect to the login page.
`crawl.py::SharedSession` keeps them in step: a worker takes the latest cookies
before it loads a page and offers back what it ended up with afterwards.

Two things about it are load-bearing and easy to undo by accident:

- **The offer is a compare-and-swap, not a write.** A worker spends a page load
  between reading the session and writing it back, so a *slow* page's older
  token would land on top of a fast page's newer one. Publishing only when the
  version has not moved makes the pool move strictly forwards. Without it, a
  pooled crawl of a rotating site intermittently captures the login page —
  which is how the flaw was found, as a test that failed one run in two.
- **A worker shares the moment the response lands**, before the network-idle
  settle, for the same reason: the later the share, the staler it is.

Measured against a fixture that rolls its session over every third request:
15/15 pages captured correctly at four workers, and the same at one — 3.3s
against 9.5s. With the sharing disabled the same crawl records the login page
under page URLs, which is what `tests/test_login.py` pins.

Cookies only. An app that rotates a token in `localStorage` is not covered, and
two workers presenting the same token in the same instant can still race a
server that invalidates on first use — inherent to fetching concurrently at
all, and a browser with two tabs has it too.

Shutdown is `_RenderPool.stop_and_join`: sentinels stop the workers, a short
grace lets any visit already in flight finish, and a worker still wedged after
that is abandoned rather than waited for (`os._exit`, because joining a thread
inside Playwright's greenlet machinery can abort with a code of its own).
Everything already completed is committed first, so Ctrl-C costs at most the
pages in flight. `tests/test_crawl_workers.py` covers the hazards concurrency
introduces, forcing the interleavings with barriers and events rather than
hoping to hit them.

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
