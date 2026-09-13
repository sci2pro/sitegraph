"""Crawl a site: discover pages, screenshot them, and record the link graph.

The crawl is a breadth-first walk from the start URL, restricted to that URL's
origin (spec §3). Breadth-first is not incidental: it means a page's ``depth``
is the length of its *shortest* path from the root, and that ``--max-pages``
truncates the far edges of the graph rather than an arbitrary branch.

The browser lives behind the small `Renderer` protocol so the walk itself —
ordering, depth, deduplication, origin restriction, incremental persistence —
is testable without launching Chromium. `ChromiumRenderer` is the only part
that needs a real browser.

Everything is written as it is discovered, so a crawl stopped by Ctrl-C or by
``--max-pages`` still leaves a dataset that ``serve`` can open.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Self
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from sitegraph.graph import Graph
from sitegraph.store import CrawlStore
from sitegraph.urls import internal_links, normalize_url, origin_of

__all__ = ["ChromiumRenderer", "PageResult", "Renderer", "crawl"]

#: Spec §7: viewport screenshots at 1440x900.
VIEWPORT_WIDTH = 1440
VIEWPORT_HEIGHT = 900

#: WebP at q80 — a page at this size lands around 60-100 KB, roughly a quarter
#: of the PNG equivalent. Spec §7 prefers WebP to keep the dataset small.
SCREENSHOT_QUALITY = 80

#: How long to let a loaded page keep mutating before reading it. Long enough
#: for a client-rendered shell to paint, short enough that a page polling the
#: network forever (which never reaches "networkidle") costs seconds, not
#: minutes.
SETTLE_TIMEOUT_MS = 3000

PAGE_TIMEOUT_MS = 30_000

#: Raw ``href`` attributes would ignore a ``<base>`` element, so the resolved
#: ``.href`` property is preferred. SVG anchors (which ``a[href]`` also matches)
#: expose ``href`` as an object rather than a string, hence the guard — and the
#: attribute fallback, so an SVG anchor is still followed rather than dropped.
_HREF_JS = """
els => els
  .map(e => (typeof e.href === 'string' ? e.href : e.getAttribute('href')))
  .filter(h => typeof h === 'string' && h.length > 0)
"""


@dataclass(slots=True)
class PageResult:
    """What one visit to one URL produced.

    ``failed`` means the page could not be *rendered* — a dead connection, a
    navigation timeout, a crashed tab. An HTTP error status is not a failure:
    a 404 page renders perfectly well, and is exactly the kind of thing a
    developer wants to see in the graph.
    """

    status: int | None = None
    title: str = ""
    hrefs: list[str] = field(default_factory=list)
    failed: bool = False
    error: str | None = None


class Renderer(Protocol):
    """A browser that can visit a URL and report what it found."""

    def visit(self, url: str, screenshot_path: Path) -> PageResult:
        """Load *url*, write a viewport screenshot to *screenshot_path*."""
        ...

    def __enter__(self) -> Self: ...

    def __exit__(self, *exc_info: object) -> None: ...


def _first_line(exc: Exception) -> str:
    """Playwright errors are a message plus a multi-line call log; keep the message."""
    text = str(exc).strip()
    return text.splitlines()[0] if text else exc.__class__.__name__


class ChromiumRenderer:
    """A headless Chromium that renders pages one at a time.

    One browser and one context are shared across the crawl; each visit gets a
    fresh page so that cookies and history from page N cannot affect page N+1.
    """

    def __init__(
        self,
        *,
        viewport: tuple[int, int] = (VIEWPORT_WIDTH, VIEWPORT_HEIGHT),
        timeout_ms: int = PAGE_TIMEOUT_MS,
        settle_ms: int = SETTLE_TIMEOUT_MS,
    ) -> None:
        self._viewport = viewport
        self._timeout_ms = timeout_ms
        self._settle_ms = settle_ms
        self._playwright = None
        self._browser = None
        self._context = None

    def __enter__(self) -> Self:
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch()
        self._context = self._browser.new_context(
            viewport={"width": self._viewport[0], "height": self._viewport[1]},
        )
        self._context.set_default_timeout(self._timeout_ms)
        return self

    def __exit__(self, *exc_info: object) -> None:
        # Unwound innermost-first, each guarded independently: if the browser
        # already died (itself a crawl failure) an exception here would replace
        # the real error with a teardown error.
        for closer, method in (
            (self._context, "close"),
            (self._browser, "close"),
            (self._playwright, "stop"),
        ):
            if closer is not None:
                try:
                    getattr(closer, method)()
                except Exception:  # noqa: BLE001 - teardown must not mask the cause
                    pass
        self._context = self._browser = self._playwright = None

    def visit(self, url: str, screenshot_path: Path) -> PageResult:
        """Load *url* and capture it, converting any browser error into a result.

        Never raises for a page-level problem: a crawl of a real site will meet
        dead links and timeouts constantly, and each one is data to record, not
        a reason to abandon the other 499 pages.
        """
        assert self._context is not None, "visit() used outside the context manager"
        page = self._context.new_page()

        try:
            try:
                response = page.goto(url, wait_until="load")
                status = response.status if response is not None else None

                # A page that keeps polling never reaches "networkidle"; that
                # is a property of the site, not an error, so the timeout is
                # swallowed and the page is read as-is.
                try:
                    page.wait_for_load_state("networkidle", timeout=self._settle_ms)
                except PlaywrightError:
                    pass

                title = page.title()
                hrefs = page.eval_on_selector_all("a[href]", _HREF_JS)
            except PlaywrightError as exc:
                return PageResult(failed=True, error=_first_line(exc))

            # Taken last and guarded separately: the page is already fully
            # described by this point, so a capture that fails should cost the
            # thumbnail and nothing else.
            try:
                page.screenshot(
                    path=screenshot_path, type="webp", quality=SCREENSHOT_QUALITY
                )
            except PlaywrightError:
                screenshot_path.unlink(missing_ok=True)

            return PageResult(status=status, title=title, hrefs=hrefs)
        finally:
            try:
                page.close()
            except PlaywrightError:
                pass


def _display(url: str) -> str:
    """Shorten *url* to path+query for progress output."""
    parts = urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def crawl(
    url: str,
    output: Path,
    max_pages: int,
    *,
    renderer: Renderer | None = None,
) -> None:
    """Crawl *url*, writing graph.json, pages/, and screenshots/ under *output*.

    Stays within the origin of *url*. Results are persisted incrementally so an
    interrupted crawl still leaves usable data behind.
    """
    root_url = normalize_url(url)
    origin = origin_of(root_url)

    store = CrawlStore(output)
    store.create()

    graph = Graph()
    # Written before the first page is visited, so an immediate Ctrl-C still
    # leaves a valid (if empty) dataset rather than no graph.json at all. It is
    # deliberately empty rather than holding a placeholder root: every node in
    # graph.json has a page record beside it, at every moment the file exists.
    store.write_graph(graph)

    started = time.monotonic()
    queue: deque[tuple[str, int]] = deque([(root_url, 0)])
    queued: set[str] = {root_url}
    visited = 0
    failures = 0

    with (renderer or ChromiumRenderer()) as browser:
        try:
            while queue and visited < max_pages:
                page_url, depth = queue.popleft()

                # Registered before the visit so the node has an ID, which is
                # what names its screenshot. `add_page` is called again below
                # with what the visit actually found.
                node = graph.add_page(
                    page_url, depth=depth, root=page_url == root_url
                )
                result = browser.visit(page_url, store.screenshot_path(node.id))
                visited += 1

                links = internal_links(page_url, result.hrefs, origin)
                if result.failed:
                    failures += 1

                graph.add_page(
                    page_url,
                    title=result.title,
                    depth=depth,
                    status=result.status,
                    # A failed page has no thumbnail of its own; the UI draws a
                    # placeholder from `failed` rather than a broken image.
                    screenshot=None if result.failed else store.screenshot_rel(node.id),
                    failed=result.failed,
                    error=result.error,
                    links=links,
                )

                for target in links:
                    if target not in queued:
                        queued.add(target)
                        queue.append((target, depth + 1))

                print(
                    f"[{visited:>4}/{max_pages}] "
                    f"{result.status or 'ERR':>4}  "
                    f"{_display(page_url)}"
                    + (f"  ({result.title})" if result.title else "")
                    + (f"  {result.error}" if result.error else "")
                )
                _persist(store, graph, node.id)
        except KeyboardInterrupt:
            print()
            _report(graph, output, visited, len(queue), started)
            print("Interrupted — partial results are still usable.")
            raise SystemExit(130) from None

    _report(graph, output, visited, len(queue), started)
    if failures:
        print(f"{failures} page(s) failed to render (kept in the graph).")
    print(f"\nExplore with:  sitegraph serve --dir {output}")


def _persist(store: CrawlStore, graph: Graph, node_id: str) -> None:
    """Write the page record, then the graph.

    Order matters: ``graph.json`` is the index the UI loads first, so anything
    it references must already be on disk. A page record written after the
    graph could be missing at the moment the graph starts pointing at it.
    """
    node = graph.get(node_id)
    if node is not None:
        store.write_page(graph.page_dict(node))
    store.write_graph(graph)


def _report(
    graph: Graph, output: Path, visited: int, remaining: int, started: float
) -> None:
    """Print the end-of-crawl summary."""
    elapsed = time.monotonic() - started
    print(
        f"\nCrawled {visited} page(s) in {elapsed:.1f}s — "
        f"{len(graph)} node(s), {len(graph.edges())} edge(s) → {output}"
    )
    if remaining:
        print(f"{remaining} page(s) discovered but not visited (--max-pages).")
