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
import os
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from threading import Lock, Thread
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
from sitegraph.urls import (
    InvalidURL,
    Origin,
    internal_links,
    is_loopback,
    normalize_url,
    origin_of,
)

__all__ = [
    "AUTO_WORKERS",
    "ChromiumRenderer",
    "PageResult",
    "Renderer",
    "ResumeError",
    "SharedSession",
    "ResumeState",
    "crawl",
    "resume_state",
    "worker_count",
]

#: Spec §7: viewport screenshots at 1440x900. This is what a crawl uses unless
#: `--viewport` says otherwise, and it is the size the UI's node cards are
#: proportioned for.
VIEWPORT_WIDTH = 1440
VIEWPORT_HEIGHT = 900

#: A viewport wider or taller than this is a typo, not a screen. Bounded here
#: so a bad number is a sentence rather than a browser that hangs.
MAX_VIEWPORT = 10_000

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


def validate_viewport(viewport: tuple[int, int]) -> tuple[int, int]:
    """Check a ``(width, height)`` capture size, and return it.

    Raising `ValueError` here keeps the failure in the caller's vocabulary:
    Chromium's own complaint about an impossible viewport is a protocol error
    from three layers down, several seconds after the browser started.
    """
    width, height = viewport
    if width < 1 or height < 1:
        raise ValueError(f"a viewport must be positive, not {width}x{height}")
    if width > MAX_VIEWPORT or height > MAX_VIEWPORT:
        raise ValueError(
            f"a viewport of {width}x{height} is larger than {MAX_VIEWPORT} in a "
            f"direction; that is a typo rather than a screen"
        )
    return viewport


def _first_line(exc: Exception) -> str:
    """Playwright errors are a message plus a multi-line call log; keep the message."""
    text = str(exc).strip()
    return text.splitlines()[0] if text else exc.__class__.__name__


class SharedSession:
    """One signed-in session, kept in step across the workers' cookie jars.

    Playwright's sync API offers no way to give two threads one browser
    context, so each worker has a jar of its own. That is fine until the site
    rotates its session cookie: the worker that made the request holds the new
    token and every other worker is left holding a dead one, which a real app
    answers by redirecting to the login page — so the crawl quietly records the
    login page under the URL it actually wanted.

    This is the fix: a worker takes the latest cookies before it loads a page
    and reports back whatever it ended up with afterwards, so a rotation made
    by any worker reaches the others before their next page.

    Two limits, both worth knowing. Cookies only — an app that rotates a token
    in ``localStorage`` is not covered. And two workers presenting the same
    token in the same instant can still race a server that invalidates a token
    the moment it is used; that one is inherent to fetching pages concurrently
    at all, and a browser with two tabs has it too.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._cookies: list[dict] = []
        self._version = 0
        self.rotations = 0

    def seed(self, cookies: list[dict]) -> None:
        """Start from a known session, e.g. one taken from a saved state."""
        with self._lock:
            self._cookies = [dict(cookie) for cookie in cookies]
            self._version += 1

    def adopt(self) -> tuple[list[dict], int]:
        """The cookies to load a page with, and the version they came from."""
        with self._lock:
            return [dict(cookie) for cookie in self._cookies], self._version

    def observe(self, cookies: list[dict], from_version: int) -> bool:
        """Offer *cookies* back; report whether they were taken up.

        Only accepted if the session has not moved since the caller read it. A
        worker spends a page load between reading and writing, so without that
        check a slow page's *older* token would land on top of a fast page's
        newer one and send the whole pool back to a token the server has
        already retired — which is exactly the failure this class exists to
        prevent, arriving by a different route.
        """
        with self._lock:
            if from_version != self._version:
                # Someone published while this worker was loading its page.
                # Theirs is the later word; this one is stale on arrival.
                return False
            if _fingerprint(self._cookies) == _fingerprint(cookies):
                return False
            self._cookies = [dict(cookie) for cookie in cookies]
            self._version += 1
            self.rotations += 1
            return True


def _fingerprint(cookies: list[dict]) -> set[tuple]:
    """The parts of a cookie that make it *this* session rather than another.

    Order is not part of it, and neither are the fields a browser fills in for
    itself, so a set that merely came back in a different order does not read
    as a rotation.
    """
    return {
        (cookie.get("name"), cookie.get("domain"), cookie.get("path"), cookie.get("value"))
        for cookie in cookies
    }


class ChromiumRenderer:
    """A headless Chromium that renders pages one at a time.

    One browser and one context are shared across the crawl; each visit gets a
    fresh page so that cookies and history from page N cannot affect page N+1.
    """

    def __init__(
        self,
        *,
        storage_state: Path | None = None,
        session: SharedSession | None = None,
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
        # The viewport decides both the layout and the size of the picture, so
        # a nonsense one is worth naming here rather than leaving to Chromium.
        validate_viewport(viewport)
        self._storage_state = Path(storage_state) if storage_state else None
        self._session = session
        self._session_version = 0
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
            self._adopt_session()
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

    def _adopt_session(self) -> None:
        """Take the cookies any other worker has seen since this one looked."""
        if self._session is None or self._context is None:
            return
        cookies, self._session_version = self._session.adopt()
        if not cookies:
            # Nothing learned yet. Clearing here would throw away the session
            # `storage_state` just gave this context, so leave it be.
            return
        self._context.clear_cookies()
        self._context.add_cookies(cookies)

    def _share_session(self) -> None:
        """Hand this context's cookies back to the pool."""
        if self._session is None or self._context is None:
            return
        self._session.observe(self._context.cookies(), self._session_version)

    def visit(self, url: str, screenshot_path: Path) -> PageResult:
        """Load *url* and capture it, converting any browser error into a result.

        Never raises for a page-level problem: a crawl of a real site will meet
        dead links and timeouts constantly, and each one is data to record, not
        a reason to abandon the other 499 pages.
        """
        assert self._context is not None, "visit() used outside the context manager"
        # Before the request, so it carries whatever token the last worker to
        # see a rotation came away with.
        self._adopt_session()
        page = self._context.new_page()

        try:
            try:
                response = page.goto(url, wait_until="load")
                status = response.status if response is not None else None

                # Shared the moment the response lands, not after the page has
                # settled. Waiting would put a slow page's token on the bus
                # behind a fast page's newer one, and the bus keeps whichever
                # arrived last — so the pool could go *backwards* to a token
                # that has already been rotated away.
                self._share_session()

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


#: How long the pool gets to report for duty before the crawl gives up on it.
STARTUP_TIMEOUT_S = 30.0

#: How long an interrupted crawl waits for workers to come back out of a visit
#: before abandoning them. A single visit is bounded by the page timeout, so
#: this only has to cover the tail of one already in flight.
SHUTDOWN_GRACE_S = 5.0

#: How long the writer waits for a result before checking its workers are still
#: alive. Not a per-page timeout — pages have their own, in the renderer.
POLL_SECONDS = 0.25

#: Pool size for a crawl of a machine's own service when no count was asked
#: for. Enough to cover the waiting without thrashing a laptop.
AUTO_WORKERS = 4

#: Sent to a worker to tell it to finish.
_STOP = object()


def worker_count(origin: Origin, requested: int | None = None) -> int:
    """Decide how many render workers to run.

    Concurrency is the point on a local app and a liberty anywhere else, so the
    automatic count is generous for loopback and exactly one for the open
    internet: a crawl of someone else's server opens a single connection unless
    the user asks for more.
    """
    if requested is not None:
        return max(1, requested)
    if not is_loopback(origin):
        return 1
    return min(AUTO_WORKERS, os.cpu_count() or 1)


@dataclass(slots=True)
class _Job:
    """One page waiting to be rendered, with the scratch path to render it to."""

    seq: int
    url: str
    depth: int
    scratch: Path


@dataclass(slots=True)
class _Ready:
    index: int


@dataclass(slots=True)
class _Died:
    """A worker that could not even start, reported for the handshake."""

    index: int
    error: BaseException


@dataclass(slots=True)
class _Completed:
    job: _Job
    result: PageResult


@dataclass(slots=True)
class _Crashed:
    """A worker whose renderer blew up outside the failures it reports itself."""

    job: _Job
    error: BaseException


class _RenderPool:
    """A fixed set of threads, each driving its own renderer.

    Workers share nothing: no state, no locks, no counter. Everything a crawl
    knows lives in the writer thread, which is the only thread that touches the
    `Graph`, the `CrawlStore`, or the frontier. The two queues carry work out
    and results back, and both are unbounded — back-pressure is the writer's
    dispatch window alone, so there is no interleaving in which a worker blocks
    putting a result and the writer blocks putting work.

    The renderer is built *and* destroyed inside its own thread, because
    Playwright's sync API binds its driver and event loop to the thread that
    created them.
    """

    def __init__(self, factory, count: int) -> None:
        self._factory = factory
        self._count = count
        self._work: Queue = Queue()
        self._results: Queue = Queue()
        self._threads: list[Thread] = []

    @property
    def size(self) -> int:
        """How many workers were asked for."""
        return self._count

    def start(self) -> tuple[int, list[_Died]]:
        """Run every worker's renderer up, and report which ones made it.

        Returns the number that started and the failures, so the caller can
        decide whether a partial pool is worth continuing with. Waiting for
        this before anything is written is what keeps "a browser that cannot
        launch writes nothing" true for the concurrent path too.
        """
        for index in range(self._count):
            thread = Thread(target=self._run, args=(index,), daemon=True)
            self._threads.append(thread)
            thread.start()

        live = 0
        failures: list[_Died] = []
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        for _ in range(self._count):
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                failures.append(_Died(-1, TimeoutError("renderer did not start")))
                continue
            message = self._poll(timeout)
            if isinstance(message, _Ready):
                live += 1
            elif isinstance(message, _Died):
                failures.append(message)
        return live, failures

    def _run(self, index: int) -> None:
        """A worker thread: open a renderer, serve jobs, close it again."""
        try:
            renderer = self._factory()
            renderer.__enter__()
        except BaseException as exc:  # noqa: BLE001 - reported, never swallowed
            self._results.put(_Died(index, exc))
            return

        self._results.put(_Ready(index))
        try:
            while True:
                job = self._work.get()
                if job is _STOP:
                    break
                try:
                    result = renderer.visit(job.url, job.scratch)
                except BaseException as exc:  # noqa: BLE001
                    # The loop body must always answer: a worker that dies
                    # silently would leave the writer waiting on a job that is
                    # never coming back.
                    self._results.put(_Crashed(job, exc))
                    continue
                self._results.put(_Completed(job, result))
        finally:
            # Tears down even on the crash path, and a failure here must not
            # replace whatever is unwinding.
            try:
                renderer.__exit__(None, None, None)
            except BaseException:  # noqa: BLE001
                pass

    def submit(self, job: _Job) -> None:
        self._work.put(job)

    def poll(self, timeout: float):
        """Return the next message, or ``None`` if none arrives in *time*."""
        return self._poll(timeout)

    def _poll(self, timeout: float):
        try:
            return self._results.get(timeout=timeout)
        except Empty:
            return None

    def drain(self) -> list:
        """Take everything already waiting, without blocking."""
        messages = []
        while True:
            try:
                messages.append(self._results.get_nowait())
            except Empty:
                return messages

    def any_alive(self) -> bool:
        return any(thread.is_alive() for thread in self._threads)

    def stop_and_join(self, grace: float) -> bool:
        """Tell every worker to finish; return whether any had to be abandoned.

        A worker blocked on an empty queue wakes on its sentinel at once. One
        inside a page load finishes that load first, which is why this waits a
        little rather than stopping dead.
        """
        for _ in self._threads:
            self._work.put(_STOP)

        # `join` per thread rather than a polling loop: a worker sitting on an
        # empty queue comes back immediately, and a wedged one costs the grace
        # once, not once each.
        deadline = time.monotonic() + grace
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return self.any_alive()


def _ignore_further_interrupts() -> None:
    """Make shutdown interruptible only by killing the process.

    A second Ctrl-C landing halfway through the final writes would leave the
    crawl without a summary and without its last `graph.json`, which is exactly
    the state the user is trying to get out of.
    """
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except ValueError:  # pragma: no cover - only off the main thread
        pass


def _leave_now(code: int) -> None:
    """Exit without running interpreter finalisation.

    Only for the case where a worker is still inside Playwright's greenlet
    machinery: joining it is what we gave up on, and letting CPython finalise
    around it can abort the process with a code of its own choosing.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def crawl(
    url: str,
    output: Path,
    max_pages: int,
    *,
    resume: bool = False,
    workers: int | None = None,
    viewport: tuple[int, int] | None = None,
    storage_state: Path | None = None,
    renderer: Renderer | None = None,
    renderer_factory=None,
) -> None:
    """Crawl *url*, writing graph.json, pages/, and screenshots/ under *output*.

    Stays within the origin of *url*. Results are persisted incrementally so an
    interrupted crawl still leaves usable data behind.

    *max_pages* caps the pages captured **in this run**. With *resume* that
    makes the cap a budget rather than a dead end: the same command run again
    picks up the pages the last run discovered but never reached, so a site
    larger than the cap can be walked in as many passes as it takes.

    *workers* is how many pages may be rendered at once; ``None`` picks a count
    from the start URL (see `worker_count`). Rendering is the slow part and it
    is mostly waiting, so this is where the wall clock goes. Every worker is a
    thread with its own browser; the crawl's own state never leaves this one,
    which is why nothing here needs a lock.

    *viewport* is the ``(width, height)`` the pages are rendered at, which is
    also the size of every screenshot. Smaller is not a smaller picture of the
    same page — a narrow viewport gets the responsive layout, which is the
    point of asking for one. Defaults to spec §7's 1440x900.

    *storage_state* is a session saved by `sitegraph login`, used to reach
    pages that require a sign-in, and it is why a signed-in crawl is limited to
    a single worker: separate browsers have separate cookie jars, so a session
    that rotates mid-crawl would leave all but one of them logged out and their
    pages captured as the login page under their real URLs.

    *renderer* and *renderer_factory* inject the browser for tests;
    *renderer* means exactly one worker.
    """
    root_url = normalize_url(url)
    origin = origin_of(root_url)

    if renderer is not None and renderer_factory is not None:
        raise ValueError("pass either renderer or renderer_factory, not both")
    if renderer is not None and workers is not None and workers > 1:
        raise ValueError(
            "a single renderer cannot serve more than one worker; "
            "pass renderer_factory to inject a pool"
        )

    count = worker_count(origin, 1 if renderer is not None else workers)

    # A pool of signed-in workers each gets its own cookie jar, so they share
    # one through `SharedSession` instead — see its docstring for what that
    # does and does not cover.
    session = SharedSession() if storage_state is not None else None

    size = validate_viewport(viewport or (VIEWPORT_WIDTH, VIEWPORT_HEIGHT))

    def make_renderer() -> Renderer:
        if renderer is not None:
            return renderer
        if renderer_factory is not None:
            return renderer_factory()
        return ChromiumRenderer(
            storage_state=storage_state, session=session, viewport=size
        )

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
    committed = 0

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

    # The pool starts before anything is written: a browser that cannot launch
    # — or a --storage-state path that does not exist — should fail without
    # leaving a half-made output directory behind.
    pool = _RenderPool(make_renderer, count)
    live, failures = pool.start()
    if not live:
        # Reported unchanged, so a caller sees the FileNotFoundError or
        # PlaywrightError it would have seen from a single browser.
        raise failures[0].error
    for failure in failures:
        print(
            f"warning: render worker {failure.index + 1} did not start "
            f"({_first_line(failure.error)}); continuing with {live}"
        )
    if live > 1:
        note = ", sharing one session" if session is not None else ""
        print(f"Rendering {live} pages at a time{note}.")

    store.create()
    if not resume:
        # Written before the first page is visited, so an immediate Ctrl-C
        # still leaves a valid (if empty) dataset rather than no graph.json at
        # all. It is deliberately empty rather than holding a placeholder root:
        # every node in graph.json has a page record beside it, at every moment
        # the file exists.
        store.write_graph(graph)

    # A resumed run counts on from where the last one stopped, so the numbers
    # keep climbing across passes instead of restarting at one.
    ceiling = previously + max_pages
    started = time.monotonic()

    in_flight: dict[int, _Job] = {}
    dispatched = 0
    next_seq = 0

    def absorb(message: object) -> None:
        """Commit a finished page, if that is what *message* is."""
        nonlocal committed
        if not isinstance(message, _Completed):
            return
        in_flight.pop(message.job.seq, None)
        _commit(
            store,
            graph,
            message.job,
            message.result,
            origin=origin,
            root_url=root_url,
            queued=queued,
            queue=queue,
        )
        committed += 1
        print(
            f"[{previously + committed:>4}/{ceiling}] "
            f"{message.result.status or 'ERR':>4}  "
            f"{_display(message.job.url)}"
            + (f"  ({message.result.title})" if message.result.title else "")
            + (f"  {message.result.error}" if message.result.error else "")
        )

    def teardown() -> bool:
        """Stop the workers, closing their browsers; True if one had to be left.

        Whatever the workers completed before the end is real work, and each
        page published moves the boundary `--resume` will pick up from, so it
        is all committed rather than dropped.
        """
        for message in pool.drain():
            absorb(message)
        wedged = pool.stop_and_join(SHUTDOWN_GRACE_S)
        for message in pool.drain():
            absorb(message)
        if not wedged:
            # Only safe once no worker can still be writing there.
            store.sweep_incoming()
        return wedged

    def finish(reason: str, code: int) -> None:
        _ignore_further_interrupts()
        wedged = teardown()
        store.write_graph(graph)
        print()
        _report(graph, output, committed, queue, started, len(in_flight))
        print(reason)
        if wedged:
            _leave_now(code)
        raise SystemExit(code)

    try:
        while True:
            # Fill the window. The cap counts dispatches rather than commits,
            # or the workers would overshoot it by up to a window's worth.
            while queue and len(in_flight) < live and dispatched < max_pages:
                page_url, depth = queue.popleft()
                job = _Job(
                    seq=next_seq,
                    url=page_url,
                    depth=depth,
                    scratch=store.incoming_path(next_seq),
                )
                next_seq += 1
                dispatched += 1
                # Marked as spoken for here, not when its links are discovered
                # later: a page still in flight would otherwise be re-dispatched
                # by whichever page finished first and captured twice.
                in_flight[job.seq] = job
                pool.submit(job)

            if not in_flight and (not queue or dispatched >= max_pages):
                break

            message = pool.poll(POLL_SECONDS)
            if message is None:
                if not pool.any_alive():
                    finish(
                        "The render pool stopped unexpectedly — partial results "
                        "are still usable.",
                        1,
                    )
                continue

            if isinstance(message, _Crashed):
                in_flight.pop(message.job.seq, None)
                if isinstance(message.error, KeyboardInterrupt):
                    raise KeyboardInterrupt
                # A renderer that threw outside its own error handling is a
                # failure like any other: the page is recorded as failed rather
                # than silently retried or dropped.
                absorb(
                    _Completed(
                        message.job,
                        PageResult(failed=True, error=_first_line(message.error)),
                    )
                )
            elif isinstance(message, _Died):
                live -= 1
            else:
                absorb(message)
    except KeyboardInterrupt:
        finish("Interrupted — continue with --resume.", 130)

    # The workers are idle at this point (nothing is in flight), but they are
    # still holding browsers open until they are told to stop.
    teardown()
    _report(graph, output, committed, queue, started)
    print(f"\nExplore with:  sitegraph serve --dir {output}")


def _commit(
    store: CrawlStore,
    graph: Graph,
    job: _Job,
    result: PageResult,
    *,
    origin,
    root_url: str,
    queued: set[str],
    queue: deque[tuple[str, int]],
) -> None:
    """Publish one rendered page: screenshot, record, graph, and its new links.

    The node's ID is allocated *here* rather than when the page was dispatched,
    because IDs are positional and only committed pages may hold one. That is
    what lets a worker capture a page before its number exists: the picture
    waits in the scratch directory until this rename gives it a name.
    """
    node = graph.add_page(job.url, depth=job.depth, root=job.url == root_url)
    links = internal_links(job.url, result.hrefs, origin)

    # A failed page has no thumbnail of its own; the UI draws a placeholder
    # from `failed` rather than a broken image. A page that rendered but whose
    # capture never landed gets `None` too, which is honest about the same way.
    screenshot = None
    if not result.failed and store.adopt_screenshot(job.scratch, node.id):
        screenshot = store.screenshot_rel(node.id)

    graph.add_page(
        job.url,
        title=result.title,
        depth=job.depth,
        status=result.status,
        screenshot=screenshot,
        failed=result.failed,
        error=result.error,
        links=links,
    )

    for target in links:
        if target not in queued:
            queued.add(target)
            queue.append((target, job.depth + 1))

    _persist(store, graph, node.id)


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
    abandoned: int = 0,
) -> None:
    """Print the end-of-crawl summary.

    The pages still to capture are the queue *plus* anything abandoned in
    flight when the run ended: an interrupted page was never published, so
    `--resume` will find it again from the record of whatever linked to it, and
    the number here has to match what the next run will actually do.

    *queue* also holds pages that failed to render, which is why the count can
    hold steady across resumes. Saying so beats letting a user watch a number
    refuse to fall and wonder what is wrong.
    """
    elapsed = time.monotonic() - started
    print(
        f"\nCaptured {visited} page(s) in {elapsed:.1f}s — "
        f"{len(graph)} in total, {len(graph.edges())} edge(s) → {output}"
    )

    failed = {node.url for node in graph.nodes if node.failed}
    if failed:
        print(f"{len(failed)} page(s) failed to render (kept in the graph).")

    remaining = len(queue) + abandoned
    if remaining:
        retrying = sum(1 for url, _ in queue if url in failed)
        print(
            f"{remaining} page(s) still to capture — run the same command "
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
