"""Tests for concurrent crawling.

These run against fake renderers driven through the real pool, so the
concurrency is genuine — real threads, real queues, real interleavings — while
staying deterministic and browser-free. Where a test needs a particular
interleaving it uses an event or a barrier to *force* it rather than hoping to
hit it, so a failure means the crawler is wrong rather than the machine being
busy.

The hazards covered here are the ones concurrency introduces and the
sequential crawler could not have: a page captured twice because it was still
in flight when a sibling linked to it, `--max-pages` overshot by a window's
worth, a finished page lost because it came back out of order, and an
in-flight page leaking into `graph.json` before it had a record to point at.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from test_crawl import ROOT, WEBP_MAGIC, FakeRenderer, nodes_by_url, read_graph, result

from sitegraph.crawl import PageResult, crawl
from sitegraph.store import GRAPH_FILE, INCOMING_DIR, PAGES_DIR, SCREENSHOTS_DIR

A = "http://example.com/a"
B = "http://example.com/b"
C = "http://example.com/c"
SLOW = "http://example.com/slow"


class Recorder:
    """Thread-safe notes about what the workers did, since assertions run after
    the fact and the threads are gone by then."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.visited: list[str] = []
        self.active = 0
        self.max_active = 0
        self.opened = 0
        self.closed = 0

    def visit_started(self, url: str) -> None:
        with self._lock:
            self.visited.append(url)
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def visit_ended(self) -> None:
        with self._lock:
            self.active -= 1

    def opened_one(self) -> None:
        with self._lock:
            self.opened += 1

    def closed_one(self) -> None:
        with self._lock:
            self.closed += 1


class ThreadFakeRenderer(FakeRenderer):
    """A `FakeRenderer` that reports into a shared `Recorder`.

    Two hooks, because where a test blocks decides what it can observe:

    - ``gate`` runs *before* the page is produced, so a test can hold work
      back while its siblings run.
    - ``hold`` runs *after*, with the capture already written to its scratch
      path but nothing committed yet — the state a page is really in while it
      is in flight.
    """

    def __init__(
        self, pages, recorder: Recorder, gate=None, on_visit=None, hold=None
    ) -> None:
        super().__init__(pages, on_visit=on_visit)
        self.recorder = recorder
        self.gate = gate
        self.hold = hold

    def __enter__(self) -> ThreadFakeRenderer:
        self.recorder.opened_one()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.recorder.closed_one()

    def visit(self, url: str, screenshot_path: Path) -> PageResult:
        self.recorder.visit_started(url)
        try:
            if self.gate is not None:
                self.gate(url)
            result = super().visit(url, screenshot_path)
            if self.hold is not None:
                self.hold(url)
            return result
        finally:
            self.recorder.visit_ended()


def factory(pages, recorder, gate=None, on_visit=None, hold=None, fail_first=0):
    """A renderer factory, recording each renderer it hands out."""
    made: list[ThreadFakeRenderer] = []

    def make() -> ThreadFakeRenderer:
        if len(made) < fail_first:
            made.append(None)  # type: ignore[arg-type]
            raise RuntimeError("this worker's browser would not start")
        renderer = ThreadFakeRenderer(pages, recorder, gate, on_visit, hold)
        made.append(renderer)
        return renderer

    make.made = made  # type: ignore[attr-defined]
    return make


def scratch_files(directory: Path) -> list[Path]:
    return list((directory / SCREENSHOTS_DIR / INCOMING_DIR).glob("*"))


# --- the pool actually pools -------------------------------------------


def test_pages_are_rendered_at_the_same_time(tmp_path: Path) -> None:
    """Two pages must be in flight together. Each waits at a barrier that only
    opens when both have arrived, so a sequential crawl deadlocks and the
    barrier breaks rather than the test passing by luck."""
    pages = {ROOT: result("/a", "/b"), A: result(), B: result()}
    barrier = threading.Barrier(2, timeout=5)

    def gate(url: str) -> None:
        if url != ROOT:
            barrier.wait()

    recorder = Recorder()
    crawl(ROOT, tmp_path, 10, workers=2, renderer_factory=factory(pages, recorder, gate))

    assert not any(node["failed"] for node in read_graph(tmp_path)["nodes"]), (
        "a page failed, which is what a broken barrier looks like"
    )
    assert recorder.max_active == 2, "the two visits never overlapped"


def test_one_worker_still_walks_breadth_first(tmp_path: Path) -> None:
    """The default pool is one worker, and it must behave exactly as the
    sequential crawler did: BFS order, one page at a time."""
    pages = {
        ROOT: result("/b", "/c"),
        B: result("/d"),
        C: result("/e"),
        "http://example.com/d": result(),
        "http://example.com/e": result(),
    }
    recorder = Recorder()
    crawl(ROOT, tmp_path, 100, workers=1, renderer_factory=factory(pages, recorder))

    assert recorder.visited == [
        ROOT,
        B,
        C,
        "http://example.com/d",
        "http://example.com/e",
    ]
    assert recorder.max_active == 1


# --- the hazards concurrency introduces ---------------------------------


def test_a_page_in_flight_is_not_dispatched_twice(tmp_path: Path) -> None:
    """The diamond again, but wide: while D is being rendered, both B and C
    link to it, and neither may put a second copy of D in the queue."""
    pages = {
        ROOT: result("/b", "/c"),
        B: result("/d"),
        C: result("/d"),
        "http://example.com/d": result(),
    }
    recorder = Recorder()
    gate = lambda url: time.sleep(0.05) if url == "http://example.com/d" else None
    crawl(ROOT, tmp_path, 100, workers=4, renderer_factory=factory(pages, recorder, gate))

    assert recorder.visited.count("http://example.com/d") == 1
    assert len(read_graph(tmp_path)["nodes"]) == 4


def test_max_pages_is_not_overshot_by_the_workers(tmp_path: Path) -> None:
    """The cap counts pages handed out, not pages finished, or a pool would
    run past it by a window's worth."""
    pages = {ROOT: result("/a", "/b", "/c"), A: result(), B: result(), C: result()}
    recorder = Recorder()
    crawl(ROOT, tmp_path, 3, workers=4, renderer_factory=factory(pages, recorder))

    assert len(recorder.visited) == 3
    assert len(read_graph(tmp_path)["nodes"]) == 3
    assert len(list((tmp_path / SCREENSHOTS_DIR).glob("*.webp"))) == 3


def test_results_that_finish_out_of_order_are_all_kept(tmp_path: Path) -> None:
    """Dispatch order and completion order are not the same thing, and the
    second one decides the IDs."""
    pages = {ROOT: result("/a", "/b"), A: result(), B: result()}

    def gate(url: str) -> None:
        if url == A:
            time.sleep(0.4)  # /a is handed out first and finishes last

    recorder = Recorder()
    crawl(ROOT, tmp_path, 10, workers=2, renderer_factory=factory(pages, recorder, gate))

    nodes = nodes_by_url(tmp_path)
    assert set(nodes) == {ROOT, A, B}
    assert nodes[B]["id"] == "000002", "the page that finished first took the ID"
    assert nodes[A]["id"] == "000003"
    for url, node in nodes.items():
        assert (tmp_path / node["screenshot"]).is_file(), f"{url} lost its picture"


def test_an_in_flight_page_is_invisible_on_disk(tmp_path: Path) -> None:
    """The invariant the whole design hangs on: while a page is being rendered,
    nothing on disk refers to it — no node, no page record, no screenshot."""
    pages = {ROOT: result("/slow", "/a"), SLOW: result(), A: result()}
    started, release = threading.Event(), threading.Event()

    def hold(url: str) -> None:
        # Held *after* the capture lands in the scratch directory, so the test
        # observes exactly the state a page is in while it is in flight.
        if url == SLOW:
            started.set()
            release.wait(timeout=10)

    recorder = Recorder()
    failure: list[BaseException] = []

    def run() -> None:
        try:
            crawl(
                ROOT,
                tmp_path,
                10,
                workers=2,
                renderer_factory=factory(pages, recorder, hold=hold),
            )
        except BaseException as exc:  # noqa: BLE001
            failure.append(exc)

    crawler = threading.Thread(target=run)
    crawler.start()
    try:
        assert started.wait(timeout=10), "the slow page was never dispatched"
        payload = read_graph(tmp_path)
        assert SLOW not in [node["url"] for node in payload["nodes"]]
        # The invariant is one-directional: every published node has a record,
        # but not every record is published yet — the record is written first,
        # on purpose, so a record can leg it briefly ahead of the graph.
        for node in payload["nodes"]:
            assert (tmp_path / PAGES_DIR / f"{node['id']}.json").is_file()
        assert scratch_files(tmp_path), "the capture should be staged, not published"
    finally:
        release.set()
        crawler.join(timeout=15)

    assert failure == []
    assert set(nodes_by_url(tmp_path)) == {ROOT, SLOW, A}
    assert scratch_files(tmp_path) == [], "the scratch directory should be empty again"


def test_the_graph_never_points_at_a_page_that_is_not_there(tmp_path: Path) -> None:
    """Checked from inside a worker, repeatedly, while its siblings are busy —
    the concurrent form of the sequential crawler's version of this test."""
    pages = {
        ROOT: result("/a", "/b"),
        A: result("/c"),
        B: result(),
        "http://example.com/c": result(),
    }
    problems: list[str] = []

    def check(url: str, path: Path) -> None:
        if not (tmp_path / GRAPH_FILE).is_file():
            return
        payload = json.loads((tmp_path / GRAPH_FILE).read_text(encoding="utf-8"))
        for node in payload["nodes"]:
            record = tmp_path / PAGES_DIR / f"{node['id']}.json"
            if not record.is_file():
                problems.append(f"{node['id']} has no record")
            if node["screenshot"] and not (tmp_path / node["screenshot"]).is_file():
                problems.append(f"{node['id']} has no screenshot")

    recorder = Recorder()
    crawl(
        ROOT,
        tmp_path,
        10,
        workers=3,
        renderer_factory=factory(pages, recorder, on_visit=check),
    )

    assert problems == []


def test_a_renderer_that_throws_outside_its_own_handling_fails_the_page(
    tmp_path: Path,
) -> None:
    """Playwright errors become failed pages; so must anything else, or the
    writer would wait for a result that is never coming."""
    pages = {ROOT: result("/a", "/b"), A: result(), B: result()}

    def on_visit(url: str, path: Path) -> None:
        if url == A:
            raise RuntimeError("the renderer fell over")

    recorder = Recorder()
    crawl(
        ROOT,
        tmp_path,
        10,
        workers=2,
        renderer_factory=factory(pages, recorder, on_visit=on_visit),
    )

    nodes = nodes_by_url(tmp_path)
    assert nodes[A]["failed"] is True
    assert nodes[B]["failed"] is False, "the other worker carried on"
    # The error text lives in the page record; `graph.json` carries only the
    # flag, because that is all the graph view needs to draw a placeholder.
    record = json.loads((tmp_path / PAGES_DIR / f"{nodes[A]['id']}.json").read_text())
    assert "the renderer fell over" in record["error"]


# --- starting up, and stopping ------------------------------------------


def test_no_worker_starting_writes_nothing(tmp_path: Path) -> None:
    pages = {ROOT: result()}
    recorder = Recorder()
    with pytest.raises(RuntimeError, match="would not start"):
        crawl(
            ROOT,
            tmp_path,
            10,
            workers=3,
            renderer_factory=factory(pages, recorder, fail_first=99),
        )

    assert not (tmp_path / GRAPH_FILE).exists()


def test_a_partial_pool_is_better_than_none(tmp_path: Path) -> None:
    """Losing a worker to a memory cap should not lose the crawl."""
    pages = {ROOT: result("/a"), A: result()}
    recorder = Recorder()
    crawl(
        ROOT,
        tmp_path,
        10,
        workers=3,
        renderer_factory=factory(pages, recorder, fail_first=2),
    )

    assert set(nodes_by_url(tmp_path)) == {ROOT, A}


def test_every_renderer_is_closed_when_the_crawl_ends(tmp_path: Path) -> None:
    pages = {ROOT: result("/a", "/b"), A: result(), B: result()}
    recorder = Recorder()
    crawl(ROOT, tmp_path, 10, workers=3, renderer_factory=factory(pages, recorder))

    assert recorder.opened == 3
    assert recorder.closed == recorder.opened, "a browser was left running"


def test_interrupting_stops_the_pool_and_still_publishes(tmp_path: Path) -> None:
    pages = {ROOT: result("/a", "/b", "/c"), A: result(), B: result(), C: result()}
    recorder = Recorder()
    stops: list[str] = []

    def on_visit(url: str, path: Path) -> None:
        stops.append(url)
        if len(stops) == 3:
            raise KeyboardInterrupt

    with pytest.raises(SystemExit) as excinfo:
        crawl(
            ROOT,
            tmp_path,
            100,
            workers=1,
            renderer_factory=factory(pages, recorder, on_visit=on_visit),
        )

    assert excinfo.value.code == 130
    payload = read_graph(tmp_path)  # must still parse
    for node in payload["nodes"]:
        assert (tmp_path / PAGES_DIR / f"{node['id']}.json").is_file()
        if node["screenshot"]:
            assert (tmp_path / node["screenshot"]).is_file()
    assert scratch_files(tmp_path) == [], "an abandoned capture was left behind"
    assert recorder.closed == recorder.opened


def test_an_interrupted_crawl_reports_the_abandoned_pages(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Whatever was in flight is not lost — `--resume` will find it again from
    the record of the page that linked to it, so the count must include it."""
    pages = {ROOT: result("/a", "/b"), A: result(), B: result()}
    recorder = Recorder()
    started = threading.Event()

    def gate(url: str) -> None:
        if url == A:
            started.set()
            raise KeyboardInterrupt

    with pytest.raises(SystemExit):
        crawl(
            ROOT,
            tmp_path,
            100,
            workers=1,
            renderer_factory=factory(pages, recorder, gate),
        )

    out = capsys.readouterr().out
    assert started.is_set()
    assert "1 page(s) still to capture" in out, out
    assert B not in nodes_by_url(tmp_path)


# --- argument handling ---------------------------------------------------


def test_one_renderer_cannot_serve_several_workers(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="renderer_factory"):
        crawl(ROOT, tmp_path, 10, workers=4, renderer=FakeRenderer({}))


def test_a_signed_in_crawl_is_limited_to_one_worker(tmp_path: Path) -> None:
    """Separate browsers have separate cookie jars, so a session that rotates
    mid-crawl would leave the workers behind it logged out."""
    session = tmp_path / "session.json"
    session.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="signed-in"):
        crawl(ROOT, tmp_path, 10, workers=4, storage_state=session)


def test_a_signed_in_crawl_defaults_to_one_worker(tmp_path: Path) -> None:
    """Crawling your own app behind a login is the common case, so a session
    must not be refused by the automatic pool size — it just gets one worker,
    and only an explicit --workers asks for more."""
    session = tmp_path / "session.json"
    session.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    pages = {ROOT: result("/a"), A: result()}
    recorder = Recorder()

    # No `workers=`: the default has to come out at one on a loopback host,
    # which is exactly where the automatic count would otherwise be four.
    crawl(
        ROOT,
        tmp_path,
        10,
        storage_state=session,
        renderer_factory=factory(pages, recorder),
    )

    assert set(nodes_by_url(tmp_path)) == {ROOT, A}
    assert recorder.max_active == 1, "a signed-in crawl must not run a pool"
