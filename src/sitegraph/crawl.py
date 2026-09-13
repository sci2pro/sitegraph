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

import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Self
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from sitegraph.graph import Graph
from sitegraph.store import (
    PAGES_DIR,
    CrawlNotFound,
    CrawlStore,
    load_graph,
    page_filename,
)
from sitegraph.urls import InvalidURL, internal_links, normalize_url, origin_of

__all__ = [
    "ChromiumRenderer",
    "PageResult",
    "Renderer",
    "ResumeError",
    "ResumeState",
    "crawl",
    "resume_state",
]

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
        storage_state: Path | None = None,
        viewport: tuple[int, int] = (VIEWPORT_WIDTH, VIEWPORT_HEIGHT),
        timeout_ms: int = PAGE_TIMEOUT_MS,
        settle_ms: int = SETTLE_TIMEOUT_MS,
    ) -> None:
        if storage_state is not None and not Path(storage_state).is_file():
            # Checked here rather than left to Playwright, which opens the file
            # lazily and raises a bare FileNotFoundError from inside its own
            # driver — after the browser has already launched.
            raise FileNotFoundError(
                f"no session file at {storage_state} "
                f"(create one with `sitegraph login`)"
            )
        self._storage_state = Path(storage_state) if storage_state else None
        self._viewport = viewport
        self._timeout_ms = timeout_ms
        self._settle_ms = settle_ms
        self._playwright = None
        self._browser = None
        self._context = None

    def __enter__(self) -> Self:
        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch()
            self._context = self._browser.new_context(
                viewport={"width": self._viewport[0], "height": self._viewport[1]},
                # A session saved by `sitegraph login`: cookies and
                # localStorage, replayed into the context so the crawl sees
                # what a signed-in person would. Playwright wants None rather
                # than a missing path.
                storage_state=(
                    str(self._storage_state) if self._storage_state else None
                ),
            )
            self._context.set_default_timeout(self._timeout_ms)
        except BaseException:
            # `__exit__` is not called when `__enter__` raises, so whatever was
            # started above has to be torn down here. Skipping it leaks the
            # driver, and a leaked driver leaves a running event loop behind
            # that makes every later crawl in the same process fail with a
            # bewildering "Playwright Sync API inside the asyncio loop".
            self.__exit__(None, None, None)
            raise
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


class ResumeError(Exception):
    """Raised when there is no crawl to continue, or it is not this crawl."""


@dataclass(slots=True)
class ResumeState:
    """A crawl read back from disk, ready to be carried on.

    ``queue`` holds the pages that were discovered but never captured, in
    shallowest-first order so that continuing approximates the breadth-first
    walk a single run would have made.
    """

    graph: Graph
    queue: deque[tuple[str, int]]
    #: The start URL of the *stored* crawl, or ``None`` if nothing was captured.
    root_url: str | None

    @property
    def captured(self) -> int:
        return len(self.graph)


def resume_state(directory: Path) -> ResumeState:
    """Rebuild the graph and the pending queue from a previous crawl.

    The frontier is *reconstructed*, not stored: every page record already
    lists the links it contained, so the pages that were discovered but not
    reached are exactly the ones named in some record that have no record of
    their own. That means there is no second file to keep in sync with
    ``graph.json``, and resuming works the same whether the previous run
    stopped at ``--max-pages``, met Ctrl-C, or died outright.
    """
    directory = Path(directory)
    try:
        payload = load_graph(directory)
    except CrawlNotFound as exc:
        raise ResumeError(f"{exc} — nothing to resume") from None

    graph = Graph()
    root_id = payload.get("root")

    for stored in payload.get("nodes") or []:
        try:
            node = graph.add_page(
                stored["url"],
                title=stored.get("title") or "",
                depth=stored.get("depth") or 0,
                status=stored.get("status"),
                screenshot=stored.get("screenshot"),
                failed=bool(stored.get("failed")),
                root=stored.get("id") == root_id,
            )
        except (KeyError, InvalidURL) as exc:
            raise ResumeError(
                f"{directory / 'graph.json'} is not a usable crawl: {exc}"
            ) from None
        if node.id != stored.get("id"):
            # IDs are positional, so a graph.json that is missing a node or has
            # one out of order would silently renumber every reference in it.
            raise ResumeError(
                f"{directory / 'graph.json'} is not a usable crawl: expected "
                f"node {stored.get('id')!r} but rebuilt it as {node.id!r}"
            )

    _restore_links(graph, directory)

    captured = {node.url for node in graph.nodes}
    pending: dict[str, int] = {}
    for node in graph.nodes:
        for link in node.links:
            if link in captured or link in pending:
                continue
            pending[link] = node.depth + 1

    # A page that failed to render was never really captured, so it goes back
    # in the queue to be tried again rather than staying blank forever.
    for node in graph.nodes:
        if node.failed:
            pending[node.url] = node.depth

    queue: deque[tuple[str, int]] = deque(
        sorted(pending.items(), key=lambda item: item[1])
    )
    root = graph.get(root_id) if root_id else None
    return ResumeState(graph=graph, queue=queue, root_url=root.url if root else None)


def _restore_links(graph: Graph, directory: Path) -> None:
    """Put each node's ``links`` back from its page record.

    The graph alone cannot say which pages were *discovered*; that lives in the
    per-page records, which is the whole reason they are kept separate.
    """
    for node in graph.nodes:
        path = directory / PAGES_DIR / page_filename(node.id)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            print(f"warning: {path.name} is missing or unreadable; skipping its links")
            continue
        node.links = [link for link in record.get("links") or [] if isinstance(link, str)]


def crawl(
    url: str,
    output: Path,
    max_pages: int,
    *,
    resume: bool = False,
    storage_state: Path | None = None,
    renderer: Renderer | None = None,
) -> None:
    """Crawl *url*, writing graph.json, pages/, and screenshots/ under *output*.

    Stays within the origin of *url*. Results are persisted incrementally so an
    interrupted crawl still leaves usable data behind.

    *max_pages* caps the pages captured **in this run**. With *resume* that
    makes the cap a budget rather than a dead end: the same command run again
    picks up the pages the last run discovered but never reached, so a site
    larger than the cap can be walked in as many passes as it takes.

    *storage_state* is a session saved by `sitegraph login`, used to reach
    pages that require a sign-in. It is ignored when a *renderer* is supplied —
    an injected renderer brings its own browser.
    """
    root_url = normalize_url(url)
    origin = origin_of(root_url)

    store = CrawlStore(output)
    previously = 0

    if resume:
        state = resume_state(output)
        if state.root_url is not None and state.root_url != root_url:
            raise ResumeError(
                f"{output} holds a crawl of {state.root_url}, not {root_url}\n"
                f"       use --output to point at a different directory, or "
                f"drop --resume to start a fresh crawl."
            )
        graph = state.graph
        queue = state.queue
        previously = state.captured
        if not graph.nodes:  # interrupted before the first page was written
            queue = deque([(root_url, 0)])
    else:
        graph = Graph()
        queue = deque([(root_url, 0)])

    queued: set[str] = {url for url, _ in queue} | {node.url for node in graph.nodes}
    visited = 0

    if not queue:
        print(
            f"Nothing left to crawl: all {previously} captured page(s) are "
            f"already accounted for."
        )
        return

    if resume:
        print(
            f"Resuming {root_url} — {previously} page(s) captured, "
            f"{len(queue)} discovered but not yet visited."
        )

    # The renderer starts before anything is written: a browser that cannot
    # launch — or a --storage-state path that does not exist — should fail
    # without leaving a half-made output directory behind.
    with (renderer or ChromiumRenderer(storage_state=storage_state)) as browser:
        store.create()
        if not resume:
            # Written before the first page is visited, so an immediate Ctrl-C
            # still leaves a valid (if empty) dataset rather than no graph.json
            # at all. It is deliberately empty rather than holding a placeholder
            # root: every node in graph.json has a page record beside it, at
            # every moment the file exists.
            store.write_graph(graph)

        # A resumed run counts on from where the last one stopped, so the
        # numbers keep climbing across passes instead of restarting at one.
        ceiling = previously + max_pages

        started = time.monotonic()
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
                    f"[{previously + visited:>4}/{ceiling}] "
                    f"{result.status or 'ERR':>4}  "
                    f"{_display(page_url)}"
                    + (f"  ({result.title})" if result.title else "")
                    + (f"  {result.error}" if result.error else "")
                )
                _persist(store, graph, node.id)
        except KeyboardInterrupt:
            print()
            _report(graph, output, visited, queue, started)
            print("Interrupted — continue with --resume.")
            raise SystemExit(130) from None

    _report(graph, output, visited, queue, started)
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
    graph: Graph,
    output: Path,
    visited: int,
    queue: deque[tuple[str, int]],
    started: float,
) -> None:
    """Print the end-of-crawl summary.

    *queue* is what is left over, which is not the same as "pages nobody has
    looked at": a page that failed to render is put back in the queue, so the
    count can hold steady across resumes. Saying so beats letting a user watch
    a number refuse to fall and wonder what is wrong.
    """
    elapsed = time.monotonic() - started
    print(
        f"\nCaptured {visited} page(s) in {elapsed:.1f}s — "
        f"{len(graph)} in total, {len(graph.edges())} edge(s) → {output}"
    )

    failed = {node.url for node in graph.nodes if node.failed}
    if failed:
        print(f"{len(failed)} page(s) failed to render (kept in the graph).")

    if queue:
        retrying = sum(1 for url, _ in queue if url in failed)
        print(
            f"{len(queue)} page(s) still to capture — run the same command "
            f"with --resume to continue."
        )
        if retrying:
            print(
                f"  ({retrying} of them failed to render and will be retried, "
                f"so this count will not reach zero while they keep failing.)"
            )
    elif failed:
        # Nothing is queued, but a resume is still not a no-op: failed pages are
        # put back in the frontier each time, so saying nothing here would make
        # the next run look like it had invented work.
        print(
            f"Nothing is waiting; --resume retries the "
            f"{len(failed)} failed page(s)."
        )
