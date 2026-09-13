# Sitegraph — Minimal CLI Specification

## 1. Purpose

`sitegraph` is a local-first CLI for crawling a web application, capturing screenshots of discovered pages, recording links between pages, and presenting the resulting site structure as an interactive graph in a browser.

It is intended primarily for developers inspecting applications running on `localhost`, although any accessible HTTP/HTTPS URL may be crawled.

The MVP should do four things well:

1. Discover reachable pages.
2. Capture a screenshot of every page.
3. Record the link graph between pages.
4. Provide a browser-based interface for exploring the results.

## 2. Basic workflow

```bash
sitegraph crawl http://localhost:3000
sitegraph serve
```

The first command performs a completely headless crawl and writes its results to `.sitegraph/`.

The second command starts a local HTTP server and opens or exposes the visual explorer.

Default output:

```text
.sitegraph/
├── graph.json
├── pages/
│   ├── 000001.json
│   ├── 000002.json
│   └── ...
└── screenshots/
    ├── 000001.webp
    ├── 000002.webp
    └── ...
```

## 3. `sitegraph crawl`

Usage:

```bash
sitegraph crawl <start-url>
```

Example:

```bash
sitegraph crawl http://localhost:3000
```

The crawler should:

- launch a headless Chromium browser;
- visit the start URL;
- wait until the page is reasonably settled;
- extract all internal `<a href>` links;
- resolve relative URLs;
- normalize URLs;
- recursively visit previously unseen internal URLs;
- capture one screenshot per page;
- record page metadata;
- record directed links between pages;
- persist results incrementally.

By default, crawling must remain within the origin of the start URL.

For example, crawling:

```text
http://localhost:3000
```

may follow:

```text
http://localhost:3000/about
http://localhost:3000/courses/123
```

but not:

```text
https://example.com/
```

Minimal options:

```bash
sitegraph crawl <url> \
    --output .sitegraph \
    --max-pages 500
```

The MVP does not need sophisticated crawling configuration.

## 4. Page identity

For the MVP, a node represents a normalized URL.

Fragments should be discarded:

```text
/about#team
/about#history
```

become:

```text
/about
```

Query strings should remain significant:

```text
/search?q=foo
/search?q=bar
```

are distinct pages.

URLs should otherwise be normalized sufficiently to prevent trivial duplicates.

## 5. Page record

Each crawled page should produce a record resembling:

```json
{
  "id": "000012",
  "url": "http://localhost:3000/courses",
  "title": "Courses",
  "status": 200,
  "depth": 2,
  "screenshot": "screenshots/000012.webp",
  "links": [
    "http://localhost:3000/courses/123",
    "http://localhost:3000/courses/new"
  ]
}
```

Useful metadata for the MVP is limited to:

- URL
- document title
- HTTP status if available
- crawl depth
- screenshot path
- outgoing links

## 6. Graph representation

`graph.json` should contain the complete graph independently of the per-page files.

Example:

```json
{
  "root": "000001",
  "nodes": [
    {
      "id": "000001",
      "url": "http://localhost:3000/",
      "title": "Home",
      "screenshot": "screenshots/000001.webp"
    },
    {
      "id": "000002",
      "url": "http://localhost:3000/login",
      "title": "Login",
      "screenshot": "screenshots/000002.webp"
    }
  ],
  "edges": [
    {
      "source": "000001",
      "target": "000002"
    }
  ]
}
```

Multiple links between the same pair of pages need not create multiple edges in the MVP.

## 7. Screenshots

The crawler should capture a viewport screenshot after page rendering.

Default viewport:

```text
1440 × 900
```

The preferred output format is WebP to keep the resulting dataset reasonably small.

Full-page screenshots are not required initially.

Pages that fail to render should still appear in the graph, marked as failed.

## 8. `sitegraph serve`

Usage:

```bash
sitegraph serve
```

Optional:

```bash
sitegraph serve --dir .sitegraph --port 4777
```

Default:

```text
http://localhost:4777
```

The server should only read previously generated crawl data. It must not crawl the application.

## 9. Browser UI

The MVP needs two views.

### Graph view

Display every page as a node and every hyperlink relationship as a directed edge.

Each node should show at minimum:

```text
┌──────────────────┐
│                  │
│    screenshot    │
│                  │
├──────────────────┤
│ Courses          │
│ /courses         │
└──────────────────┘
```

Users should be able to:

- pan;
- zoom;
- select a node;
- inspect incoming links;
- inspect outgoing links;
- open the original URL.

Selecting a node should display its larger screenshot and metadata.

### Contact-sheet view

Display all discovered pages as a thumbnail grid:

```text
┌───────┐ ┌───────┐ ┌───────┐
│ image │ │ image │ │ image │
├───────┤ ├───────┤ ├───────┤
│ Home  │ │ Login │ │ Course│
└───────┘ └───────┘ └───────┘
```

Clicking a thumbnail opens the same page inspector used by the graph view.

## 10. Suggested implementation

A minimal implementation could use:

```text
CLI              Node.js
Browser          Playwright / Chromium
Persistence      JSON files
Screenshots      Playwright
Local server     Express or equivalent
Graph UI         Cytoscape.js
Frontend         static HTML/CSS/JS
```

SQLite is unnecessary for the first version unless crawl sizes become large.

## 11. Explicitly out of scope

The first version should not attempt to support:

- authentication automation;
- forms;
- buttons that change application state;
- modal discovery;
- SPA states without distinct URLs;
- visual regression testing;
- screenshot comparison;
- robots.txt compliance controls;
- SEO analysis;
- accessibility analysis;
- API discovery;
- JavaScript event-flow analysis;
- automatic login;
- multiple browser engines.

These can be layered on later.

## 12. Success criterion

Running:

```bash
sitegraph crawl http://localhost:3000
sitegraph serve
```

should allow a developer, within seconds, to visually answer:

> What pages does this application currently contain, what do they look like, and how are they connected?

That is the complete MVP.