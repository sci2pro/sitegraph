"""Tests for the crawl walk itself.

These run against a `FakeRenderer` rather than Chromium: the ordering, depth,
deduplication, origin restriction and persistence rules are the crawler's
logic, and testing them through a browser would make them slow, flaky, and
hard to aim at a specific case. The real browser is covered separately by
`test_e2e.py`.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from sitegraph.crawl import PageResult, ResumeError, crawl
from sitegraph.store import GRAPH_FILE, PAGES_DIR, SCREENSHOTS_DIR, THUMBS_DIR

ROOT = "http://example.com/"

WEBP_MAGIC = b"RIFF\x00\x00\x00\x00WEBP"


class FakeRenderer:
    """Serves a fixed map of URL -> PageResult, writing real screenshot files.

    ``on_visit`` runs at the start of each visit, which is what lets a test
    observe the on-disk state mid-crawl.
    """

    def __init__(
        self,
        pages: dict[str, PageResult],
        *,
        on_visit=None,
        screenshots: bool = True,
        thumbnails: bool = True,
    ) -> None:
        self.pages = pages
        self.on_visit = on_visit
        self.screenshots = screenshots
        self.thumbnails = thumbnails
        self.visited: list[str] = []
        self.closed = False

    def __enter__(self) -> FakeRenderer:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.closed = True

    def visit(self, url: str, screenshot_path: Path) -> PageResult:
        self.visited.append(url)
        if self.on_visit is not None:
            self.on_visit(url, screenshot_path)
        result = self.pages.get(url)
        if result is None:
            return PageResult(failed=True, error="net::ERR_NAME_NOT_RESOLVED")
        if not result.failed and self.screenshots:
            screenshot_path.write_bytes(WEBP_MAGIC)
        if not result.failed and self.thumbnails and not result.thumbnail:
            # A real renderer hands back the card-sized copy in the result, so
            # the fake does too — it is the same bytes-in-memory route.
            result = dataclasses.replace(result, thumbnail=WEBP_MAGIC)
        return result


def result(
    *hrefs: str, title: str = "Page", status: int = 200, scrolls_inside: bool = False
) -> PageResult:
    return PageResult(
        status=status, title=title, hrefs=list(hrefs), scrolls_inside=scrolls_inside
    )


def summary(out: str) -> str:
    """The end-of-run notes, without the per-page progress above them.

    The progress lines name the same paths as the caveats do, so a test asking
    whether a path went *unmentioned* has to read the summary, not the stream.
    """
    start = out.rindex("Captured ")
    return out[out.index("\n", start) :]


def read_graph(directory: Path) -> dict:
    return json.loads((directory / GRAPH_FILE).read_text(encoding="utf-8"))


def nodes_by_url(directory: Path) -> dict[str, dict]:
    return {node["url"]: node for node in read_graph(directory)["nodes"]}


# --- walk ---------------------------------------------------------------


def test_visits_each_page_once(tmp_path: Path) -> None:
    """A diamond — both B and C link to D — must still visit D once."""
    pages = {
        ROOT: result("/b", "/c"),
        "http://example.com/b": result("/d"),
        "http://example.com/c": result("/d"),
        "http://example.com/d": result(),
    }
    renderer = FakeRenderer(pages)
    crawl(ROOT, tmp_path, 100, renderer=renderer)

    assert sorted(renderer.visited) == [
        "http://example.com/",
        "http://example.com/b",
        "http://example.com/c",
        "http://example.com/d",
    ]


def test_crawls_breadth_first(tmp_path: Path) -> None:
    """Order matters because it sets the IDs, and IDs are the graph's interface."""
    pages = {
        ROOT: result("/b", "/c"),
        "http://example.com/b": result("/d"),
        "http://example.com/c": result("/e"),
        "http://example.com/d": result(),
        "http://example.com/e": result(),
    }
    renderer = FakeRenderer(pages)
    crawl(ROOT, tmp_path, 100, renderer=renderer)

    assert renderer.visited == [
        ROOT,
        "http://example.com/b",
        "http://example.com/c",
        "http://example.com/d",
        "http://example.com/e",
    ]


def test_depth_is_the_shortest_path_from_the_root(tmp_path: Path) -> None:
    """A page reachable at depth 1 and depth 3 is recorded at depth 1."""
    pages = {
        ROOT: result("/a", "/long"),
        "http://example.com/a": result("/b"),
        "http://example.com/b": result("/c"),
        "http://example.com/c": result("/deep"),
        "http://example.com/long": result(),
    }
    renderer = FakeRenderer(pages)
    # max_pages is high enough that /deep would be reached if it were enqueued
    # before /long; the depth recorded is what is being asserted.
    crawl(ROOT, tmp_path, 100, renderer=renderer)

    by_url = nodes_by_url(tmp_path)
    assert by_url[ROOT]["depth"] == 0
    assert by_url["http://example.com/a"]["depth"] == 1
    assert by_url["http://example.com/b"]["depth"] == 2
    assert by_url["http://example.com/c"]["depth"] == 3


def test_max_pages_stops_the_walk(tmp_path: Path) -> None:
    pages = {
        ROOT: result("/a", "/b", "/c"),
        "http://example.com/a": result(),
        "http://example.com/b": result(),
        "http://example.com/c": result(),
    }
    renderer = FakeRenderer(pages)
    crawl(ROOT, tmp_path, 2, renderer=renderer)

    assert len(renderer.visited) == 2
    assert len(read_graph(tmp_path)["nodes"]) == 2


def test_pages_beyond_max_pages_remain_visible_as_links(tmp_path: Path) -> None:
    """Truncation must not hide what was discovered — the page record keeps the
    full link list even though only some targets became nodes."""
    pages = {
        ROOT: result("/a", "/b"),
        "http://example.com/a": result(),
    }
    crawl(ROOT, tmp_path, 1, renderer=FakeRenderer(pages))

    record = json.loads((tmp_path / PAGES_DIR / "000001.json").read_text())
    assert record["links"] == ["http://example.com/a", "http://example.com/b"]
    assert len(read_graph(tmp_path)["nodes"]) == 1


# --- origin restriction -------------------------------------------------


def test_stays_within_the_origin(tmp_path: Path) -> None:
    pages = {
        ROOT: result(
            "https://elsewhere.com/x",  # different host
            "/local",
            "http://example.com:8080/other-port",
            "https://example.com/scheme-differs",
            "mailto:someone@example.com",
        ),
        "http://example.com/local": result(),
    }
    renderer = FakeRenderer(pages)
    crawl(ROOT, tmp_path, 100, renderer=renderer)

    assert renderer.visited == [ROOT, "http://example.com/local"]


# --- links and edges ----------------------------------------------------


def test_duplicate_links_produce_one_edge(tmp_path: Path) -> None:
    pages = {
        ROOT: result("/a", "/a", "/a#fragment"),
        "http://example.com/a": result(),
    }
    crawl(ROOT, tmp_path, 100, renderer=FakeRenderer(pages))

    assert read_graph(tmp_path)["edges"] == [{"source": "000001", "target": "000002"}]


def test_self_links_are_dropped(tmp_path: Path) -> None:
    pages = {ROOT: result("/", "/#top", ROOT)}
    crawl(ROOT, tmp_path, 100, renderer=FakeRenderer(pages))

    assert read_graph(tmp_path)["edges"] == []


def test_fragments_collapse_into_one_node(tmp_path: Path) -> None:
    pages = {
        ROOT: result("/about#team", "/about#history", "/about"),
        "http://example.com/about": result(),
    }
    renderer = FakeRenderer(pages)
    crawl(ROOT, tmp_path, 100, renderer=renderer)

    assert renderer.visited == [ROOT, "http://example.com/about"]
    assert len(read_graph(tmp_path)["nodes"]) == 2


def test_query_strings_stay_distinct(tmp_path: Path) -> None:
    pages = {
        ROOT: result("/search?q=foo", "/search?q=bar"),
        "http://example.com/search?q=foo": result(),
        "http://example.com/search?q=bar": result(),
    }
    renderer = FakeRenderer(pages)
    crawl(ROOT, tmp_path, 100, renderer=renderer)

    assert sorted(renderer.visited) == [
        ROOT,
        "http://example.com/search?q=bar",
        "http://example.com/search?q=foo",
    ]


# --- failure ------------------------------------------------------------


def test_failed_pages_stay_in_the_graph(tmp_path: Path) -> None:
    """Spec §7: a page that fails to render is still a node, marked failed."""
    pages = {
        ROOT: result("/broken"),
        # /broken is missing from the map, so the FakeRenderer fails it.
    }
    crawl(ROOT, tmp_path, 100, renderer=FakeRenderer(pages))

    by_url = nodes_by_url(tmp_path)
    broken = by_url["http://example.com/broken"]
    assert broken["failed"] is True
    assert broken["screenshot"] is None
    assert broken["status"] is None
    # and it is still a link target, so it is still an edge
    assert read_graph(tmp_path)["edges"] == [{"source": "000001", "target": "000002"}]


def test_failed_page_record_keeps_the_error(tmp_path: Path) -> None:
    pages = {ROOT: result("/broken")}
    crawl(ROOT, tmp_path, 100, renderer=FakeRenderer(pages))

    record = json.loads((tmp_path / PAGES_DIR / "000002.json").read_text())
    assert record["error"] == "net::ERR_NAME_NOT_RESOLVED"
    assert record["failed"] is True


def test_http_error_status_is_not_a_render_failure(tmp_path: Path) -> None:
    """A 404 page renders perfectly well, and is worth seeing in the graph."""
    pages = {
        ROOT: result("/missing"),
        "http://example.com/missing": result(status=404, title="Not found"),
    }
    crawl(ROOT, tmp_path, 100, renderer=FakeRenderer(pages))

    missing = nodes_by_url(tmp_path)["http://example.com/missing"]
    assert missing["status"] == 404
    assert missing["failed"] is False
    assert missing["screenshot"] == "screenshots/000002.webp"


def test_crawl_continues_after_a_failure(tmp_path: Path) -> None:
    pages = {
        ROOT: result("/broken", "/fine"),
        "http://example.com/fine": result(),
    }
    renderer = FakeRenderer(pages)
    crawl(ROOT, tmp_path, 100, renderer=renderer)

    assert renderer.visited == [
        ROOT,
        "http://example.com/broken",
        "http://example.com/fine",
    ]
    assert len(read_graph(tmp_path)["nodes"]) == 3


# --- persistence --------------------------------------------------------


def test_writes_every_artifact(tmp_path: Path) -> None:
    pages = {ROOT: result("/a"), "http://example.com/a": result()}
    crawl(ROOT, tmp_path, 100, renderer=FakeRenderer(pages))

    assert (tmp_path / GRAPH_FILE).is_file()
    assert (tmp_path / PAGES_DIR / "000001.json").is_file()
    assert (tmp_path / PAGES_DIR / "000002.json").is_file()
    assert (tmp_path / SCREENSHOTS_DIR / "000001.webp").read_bytes() == WEBP_MAGIC


def test_graph_is_written_before_the_first_page_is_visited(tmp_path: Path) -> None:
    """So an immediate interruption still leaves something servable."""
    seen: list[bool] = []

    def on_visit(url: str, path: Path) -> None:
        seen.append((tmp_path / GRAPH_FILE).is_file())

    pages = {ROOT: result()}
    crawl(ROOT, tmp_path, 100, renderer=FakeRenderer(pages, on_visit=on_visit))

    assert seen == [True]


def test_results_are_persisted_incrementally(tmp_path: Path) -> None:
    """The graph on disk must gain a node per visit, not appear at the end."""
    counts: list[int] = []

    def on_visit(url: str, path: Path) -> None:
        if (tmp_path / GRAPH_FILE).is_file():
            counts.append(len(read_graph(tmp_path)["nodes"]))

    pages = {
        ROOT: result("/a", "/b"),
        "http://example.com/a": result("/c"),
        "http://example.com/b": result(),
        "http://example.com/c": result(),
    }
    crawl(ROOT, tmp_path, 100, renderer=FakeRenderer(pages, on_visit=on_visit))

    # Nothing is published until it has been captured, so the graph gains
    # exactly one node per completed visit.
    assert counts == [0, 1, 2, 3]


def test_graph_never_references_a_page_file_that_is_not_written(tmp_path: Path) -> None:
    def on_visit(url: str, path: Path) -> None:
        if not (tmp_path / GRAPH_FILE).is_file():
            return
        for node in read_graph(tmp_path)["nodes"]:
            assert (tmp_path / PAGES_DIR / f"{node['id']}.json").is_file()

    pages = {ROOT: result("/a"), "http://example.com/a": result("/b"),
             "http://example.com/b": result()}
    crawl(ROOT, tmp_path, 100, renderer=FakeRenderer(pages, on_visit=on_visit))


def test_interrupted_crawl_leaves_usable_data(tmp_path: Path) -> None:
    """Ctrl-C mid-crawl: what was finished is complete and parseable."""
    started: list[str] = []

    def on_visit(url: str, path: Path) -> None:
        started.append(url)
        if len(started) == 3:  # during the third visit, before it completes
            raise KeyboardInterrupt

    pages = {
        ROOT: result("/a", "/b", "/c"),
        "http://example.com/a": result(),
        "http://example.com/b": result(),
        "http://example.com/c": result(),
    }
    renderer = FakeRenderer(pages, on_visit=on_visit)

    with pytest.raises(SystemExit) as excinfo:
        crawl(ROOT, tmp_path, 100, renderer=renderer)

    assert excinfo.value.code == 130
    payload = read_graph(tmp_path)  # must still parse
    assert [node["url"] for node in payload["nodes"]] == [
        ROOT,
        "http://example.com/a",
    ], "the interrupted page must not appear as if it had been captured"
    for node in payload["nodes"]:
        assert (tmp_path / PAGES_DIR / f"{node['id']}.json").is_file()
    assert renderer.closed, "the browser must be shut down on the way out"


def test_renderer_is_closed_on_a_normal_finish(tmp_path: Path) -> None:
    pages = {ROOT: result()}
    renderer = FakeRenderer(pages)
    crawl(ROOT, tmp_path, 100, renderer=renderer)

    assert renderer.closed


def test_screenshot_paths_in_the_graph_are_relative(tmp_path: Path) -> None:
    pages = {ROOT: result("/a"), "http://example.com/a": result()}
    crawl(ROOT, tmp_path, 100, renderer=FakeRenderer(pages))

    for node in read_graph(tmp_path)["nodes"]:
        assert node["screenshot"] == f"screenshots/{node['id']}.webp"
        assert not node["screenshot"].startswith("/")


def test_a_captured_page_gets_a_card_sized_copy(tmp_path: Path) -> None:
    """The graph view draws node cards from the copy rather than decoding a
    full capture per card, so each rendered page leaves one behind."""
    pages = {ROOT: result("/a"), "http://example.com/a": result()}
    crawl(ROOT, tmp_path, 100, renderer=FakeRenderer(pages))

    for node in read_graph(tmp_path)["nodes"]:
        assert node["thumb"] == f"thumbs/{node['id']}.webp"
        assert (tmp_path / node["thumb"]).is_file()
        assert not node["thumb"].startswith("/")


def test_a_page_with_no_capture_gets_no_copy(tmp_path: Path) -> None:
    """A copy of a picture that does not exist would be a graph entry pointing
    at nothing."""
    pages = {ROOT: result("/broken"), "http://example.com/broken": result(title="x")}
    pages["http://example.com/broken"] = PageResult(failed=True, error="boom")
    crawl(ROOT, tmp_path, 100, renderer=FakeRenderer(pages))

    broken = nodes_by_url(tmp_path)["http://example.com/broken"]
    assert broken["failed"] is True
    assert broken["screenshot"] is None
    assert "thumb" not in broken
    assert not (tmp_path / THUMBS_DIR / "000002.webp").exists()


def test_a_crawl_that_made_no_copies_names_none(tmp_path: Path) -> None:
    """Absent means "draw the capture instead", which is what every directory
    written before copies existed has to mean."""
    pages = {ROOT: result("/a"), "http://example.com/a": result()}
    crawl(ROOT, tmp_path, 100, renderer=FakeRenderer(pages, thumbnails=False))

    for node in read_graph(tmp_path)["nodes"]:
        assert "thumb" not in node
        assert node["screenshot"]
    assert list((tmp_path / THUMBS_DIR).glob("*")) == []


def test_a_dry_run_writes_no_copies(tmp_path: Path) -> None:
    output = tmp_path / "out"
    crawl(ROOT, output, 100, dry_run=True, renderer=FakeRenderer({ROOT: result("/a")}))

    assert not (output / THUMBS_DIR).exists()


def test_resume_leaves_copies_it_already_made_alone(tmp_path: Path) -> None:
    pages = chain(*FIVE)
    crawl(ROOT, tmp_path, 2, renderer=FakeRenderer(pages))
    first = (tmp_path / THUMBS_DIR / "000001.webp").read_bytes()

    crawl(ROOT, tmp_path, 10, resume=True, renderer=FakeRenderer(pages))

    assert (tmp_path / THUMBS_DIR / "000001.webp").read_bytes() == first
    assert len(list((tmp_path / THUMBS_DIR).glob("*.webp"))) == 5


def test_output_directory_is_created(tmp_path: Path) -> None:
    out = tmp_path / "nested" / ".sitegraph"
    crawl(ROOT, out, 100, renderer=FakeRenderer({ROOT: result()}))

    assert (out / GRAPH_FILE).is_file()


# --- resume -------------------------------------------------------------


def chain(*urls: str) -> dict[str, PageResult]:
    """A site shaped as / -> /p1 -> /p2 -> ..., for capping at any length."""
    pages = {}
    for index, url in enumerate(urls):
        nxt = urls[index + 1] if index + 1 < len(urls) else None
        pages[url] = result(nxt) if nxt else result()
    return pages


FIVE = [ROOT, *[f"http://example.com/p{n}" for n in range(1, 5)]]


def test_resume_captures_the_pages_the_cap_left_behind(tmp_path: Path) -> None:
    pages = chain(*FIVE)
    crawl(ROOT, tmp_path, 2, renderer=FakeRenderer(pages))
    assert len(nodes_by_url(tmp_path)) == 2

    crawl(ROOT, tmp_path, 10, resume=True, renderer=FakeRenderer(pages))

    assert len(nodes_by_url(tmp_path)) == 5
    assert set(nodes_by_url(tmp_path)) == set(FIVE)


def test_resume_does_not_revisit_captured_pages(tmp_path: Path) -> None:
    pages = chain(*FIVE)
    crawl(ROOT, tmp_path, 2, renderer=FakeRenderer(pages))

    renderer = FakeRenderer(pages)
    crawl(ROOT, tmp_path, 10, resume=True, renderer=renderer)

    assert renderer.visited == FIVE[2:]


def test_resume_keeps_the_ids_already_handed_out(tmp_path: Path) -> None:
    """IDs are referenced by edges and by `root`, so a resumed crawl must not
    renumber anything it already published."""
    pages = chain(*FIVE)
    crawl(ROOT, tmp_path, 2, renderer=FakeRenderer(pages))
    before = {node["url"]: node["id"] for node in read_graph(tmp_path)["nodes"]}

    crawl(ROOT, tmp_path, 10, resume=True, renderer=FakeRenderer(pages))

    after = {node["url"]: node["id"] for node in read_graph(tmp_path)["nodes"]}
    for url, node_id in before.items():
        assert after[url] == node_id
    assert read_graph(tmp_path)["root"] == "000001"


def test_resume_keeps_the_screenshots_already_taken(tmp_path: Path) -> None:
    pages = chain(*FIVE)
    crawl(ROOT, tmp_path, 2, renderer=FakeRenderer(pages))
    first = (tmp_path / SCREENSHOTS_DIR / "000001.webp").read_bytes()

    crawl(ROOT, tmp_path, 10, resume=True, renderer=FakeRenderer(pages))

    assert (tmp_path / SCREENSHOTS_DIR / "000001.webp").read_bytes() == first
    assert len(list((tmp_path / SCREENSHOTS_DIR).glob("*.webp"))) == 5


def test_resume_connects_edges_across_the_boundary(tmp_path: Path) -> None:
    """The page captured last in run one still links to the page captured
    first in run two; that edge has to exist exactly once."""
    pages = {
        ROOT: result("/a"),
        "http://example.com/a": result("/b"),
        "http://example.com/b": result("/a"),
    }
    crawl(ROOT, tmp_path, 1, renderer=FakeRenderer(pages))
    crawl(ROOT, tmp_path, 10, resume=True, renderer=FakeRenderer(pages))

    edges = {(e["source"], e["target"]) for e in read_graph(tmp_path)["edges"]}
    assert edges == {("000001", "000002"), ("000002", "000003"), ("000003", "000002")}


def test_max_pages_is_a_budget_for_the_run_not_the_crawl(tmp_path: Path) -> None:
    """The same command run again keeps making progress, which is the whole
    point: 'continue' should not require editing the number."""
    pages = chain(*FIVE)
    crawl(ROOT, tmp_path, 2, renderer=FakeRenderer(pages))
    crawl(ROOT, tmp_path, 2, resume=True, renderer=FakeRenderer(pages))
    assert len(nodes_by_url(tmp_path)) == 4

    crawl(ROOT, tmp_path, 2, resume=True, renderer=FakeRenderer(pages))
    assert len(nodes_by_url(tmp_path)) == 5


def test_resume_walks_shallow_pages_first(tmp_path: Path) -> None:
    """The frontier is rebuilt from the page records, so the order has to be
    re-derived; a resumed run should still move outwards level by level."""
    pages = {
        ROOT: result("/a", "/b"),
        "http://example.com/a": result("/deep"),
        "http://example.com/b": result(),
        "http://example.com/deep": result(),
    }
    crawl(ROOT, tmp_path, 1, renderer=FakeRenderer(pages))

    renderer = FakeRenderer(pages)
    crawl(ROOT, tmp_path, 10, resume=True, renderer=renderer)

    assert renderer.visited == [
        "http://example.com/a",
        "http://example.com/b",
        "http://example.com/deep",
    ]


def test_resume_records_the_right_depth_for_new_pages(tmp_path: Path) -> None:
    pages = chain(*FIVE)
    crawl(ROOT, tmp_path, 2, renderer=FakeRenderer(pages))
    crawl(ROOT, tmp_path, 10, resume=True, renderer=FakeRenderer(pages))

    depths = {url: node["depth"] for url, node in nodes_by_url(tmp_path).items()}
    assert depths == {url: index for index, url in enumerate(FIVE)}


def test_resume_with_nothing_left_does_nothing(tmp_path: Path) -> None:
    pages = chain(*FIVE)
    crawl(ROOT, tmp_path, 10, renderer=FakeRenderer(pages))

    renderer = FakeRenderer(pages)
    crawl(ROOT, tmp_path, 10, resume=True, renderer=renderer)

    assert renderer.visited == [], "a finished crawl should not reopen a browser"


def test_resume_retries_pages_that_failed(tmp_path: Path) -> None:
    """A page that never rendered was not really captured, so it goes back in
    the queue — the same node, updated in place."""
    pages = {ROOT: result("/broken", "/fine"), "http://example.com/fine": result()}
    crawl(ROOT, tmp_path, 10, renderer=FakeRenderer(pages))

    failed = nodes_by_url(tmp_path)["http://example.com/broken"]
    assert failed["failed"] is True
    assert failed["screenshot"] is None

    pages["http://example.com/broken"] = result(title="Recovered")
    crawl(ROOT, tmp_path, 10, resume=True, renderer=FakeRenderer(pages))

    recovered = nodes_by_url(tmp_path)["http://example.com/broken"]
    assert recovered["failed"] is False
    assert recovered["title"] == "Recovered"
    assert recovered["id"] == failed["id"], "the same node, updated in place"
    assert recovered["screenshot"] == f"screenshots/{recovered['id']}.webp"
    assert (tmp_path / recovered["screenshot"]).is_file()


def test_a_page_that_keeps_failing_stays_in_the_queue(tmp_path: Path) -> None:
    pages = {ROOT: result("/broken")}
    crawl(ROOT, tmp_path, 10, renderer=FakeRenderer(pages))

    renderer = FakeRenderer(pages)
    crawl(ROOT, tmp_path, 10, resume=True, renderer=renderer)

    assert renderer.visited == ["http://example.com/broken"]
    assert nodes_by_url(tmp_path)["http://example.com/broken"]["failed"] is True


def test_resume_after_an_interruption(tmp_path: Path) -> None:
    """Ctrl-C and --max-pages leave the same recoverable state."""
    pages = chain(*FIVE)
    started: list[str] = []

    def interrupt(url: str, path: Path) -> None:
        started.append(url)
        if len(started) == 3:
            raise KeyboardInterrupt

    with pytest.raises(SystemExit):
        crawl(ROOT, tmp_path, 10, renderer=FakeRenderer(pages, on_visit=interrupt))

    assert len(nodes_by_url(tmp_path)) == 2

    renderer = FakeRenderer(pages)
    crawl(ROOT, tmp_path, 10, resume=True, renderer=renderer)

    assert renderer.visited == FIVE[2:]
    assert set(nodes_by_url(tmp_path)) == set(FIVE)


def test_the_summary_says_what_resume_would_do_next(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A finished crawl whose only failure keeps failing has an empty queue but
    a non-empty retry set, and the next resume will still do something. Saying
    so beats leaving the user to wonder why run five is doing anything."""
    pages = {ROOT: result("/broken")}
    crawl(ROOT, tmp_path, 10, renderer=FakeRenderer(pages))
    crawl(ROOT, tmp_path, 10, resume=True, renderer=FakeRenderer(pages))

    assert "Nothing is waiting; --resume retries the 1 failed page(s)." in (
        capsys.readouterr().out
    )


def test_the_summary_separates_waiting_pages_from_retries(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    pages = {ROOT: result("/a", "/b", "/c")}  # none of the three ever resolve
    crawl(ROOT, tmp_path, 1, renderer=FakeRenderer(pages))
    crawl(ROOT, tmp_path, 1, resume=True, renderer=FakeRenderer(pages))
    capsys.readouterr()

    crawl(ROOT, tmp_path, 1, resume=True, renderer=FakeRenderer(pages))

    out = capsys.readouterr().out
    assert "still to capture" in out
    assert "1 of them failed to render and will be retried" in out


def test_resume_reports_nothing_to_resume(tmp_path: Path) -> None:
    with pytest.raises(ResumeError) as excinfo:
        crawl(ROOT, tmp_path, 10, resume=True, renderer=FakeRenderer({}))

    assert "nothing to resume" in str(excinfo.value)


def test_resume_refuses_a_crawl_of_a_different_site(tmp_path: Path) -> None:
    pages = chain(*FIVE)
    crawl(ROOT, tmp_path, 10, renderer=FakeRenderer(pages))

    with pytest.raises(ResumeError) as excinfo:
        crawl(
            "http://other.example.com/",
            tmp_path,
            10,
            resume=True,
            renderer=FakeRenderer(pages),
        )

    assert "other.example.com" in str(excinfo.value)


def test_resume_accepts_an_equivalent_url(tmp_path: Path) -> None:
    """`http://host` and `http://host/` are the same crawl."""
    pages = chain(*FIVE)
    crawl(ROOT, tmp_path, 2, renderer=FakeRenderer(pages))

    renderer = FakeRenderer(pages)
    crawl(ROOT.rstrip("/"), tmp_path, 10, resume=True, renderer=renderer)

    assert len(renderer.visited) == 3


def test_resume_starts_over_if_nothing_was_captured(tmp_path: Path) -> None:
    """A crawl interrupted before its first page leaves an empty graph, which
    should be resumed as if it were new."""
    (tmp_path / GRAPH_FILE).write_text(
        json.dumps({"root": None, "nodes": [], "edges": []}), encoding="utf-8"
    )
    pages = chain(*FIVE)
    renderer = FakeRenderer(pages)
    crawl(ROOT, tmp_path, 10, resume=True, renderer=renderer)

    assert renderer.visited == FIVE
    assert len(nodes_by_url(tmp_path)) == 5


def test_resume_survives_a_missing_page_record(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A hand-deleted page record costs that page's links, not the resume."""
    pages = chain(*FIVE)
    crawl(ROOT, tmp_path, 2, renderer=FakeRenderer(pages))
    (tmp_path / PAGES_DIR / "000002.json").unlink()

    renderer = FakeRenderer(pages)
    crawl(ROOT, tmp_path, 10, resume=True, renderer=renderer)

    assert "000002.json is missing" in capsys.readouterr().out
    assert renderer.visited == [], "the only link to /p2 lived in that record"


def test_resume_rejects_a_renumbered_graph(tmp_path: Path) -> None:
    """Hand-edited IDs would silently rewrite every edge reference, so they are
    checked against the rebuilt graph instead."""
    pages = chain(*FIVE)
    crawl(ROOT, tmp_path, 2, renderer=FakeRenderer(pages))

    payload = read_graph(tmp_path)
    payload["nodes"][1]["id"] = "000009"
    (tmp_path / GRAPH_FILE).write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ResumeError) as excinfo:
        crawl(ROOT, tmp_path, 10, resume=True, renderer=FakeRenderer(pages))

    assert "not a usable crawl" in str(excinfo.value)


def test_resume_does_not_write_until_a_browser_is_running(tmp_path: Path) -> None:
    """The graph on disk is left exactly as the previous run wrote it."""
    pages = chain(*FIVE)
    crawl(ROOT, tmp_path, 2, renderer=FakeRenderer(pages))
    before = (tmp_path / GRAPH_FILE).read_bytes()

    class Exploding(FakeRenderer):
        def __enter__(self):
            raise RuntimeError("no browser today")

    with pytest.raises(RuntimeError):
        crawl(ROOT, tmp_path, 10, resume=True, renderer=Exploding(pages))

    assert (tmp_path / GRAPH_FILE).read_bytes() == before


# --- one page per route, on request -------------------------------------


def shop(count: int = 5) -> dict[str, PageResult]:
    """A site whose rows each have their own page, as a real one would."""
    pages = {ROOT: result(*[f"/courses/{n}" for n in range(count)])}
    for number in range(count):
        pages[f"http://example.com/courses/{number}"] = result("/")
    return pages


def test_without_a_cap_every_instance_is_captured(tmp_path: Path) -> None:
    crawl(ROOT, tmp_path, 100, renderer=FakeRenderer(shop()))

    assert len(read_graph(tmp_path)["nodes"]) == 6


def test_a_cap_takes_the_first_instances_of_a_route(tmp_path: Path) -> None:
    crawl(ROOT, tmp_path, 100, per_pattern=2, renderer=FakeRenderer(shop()))

    captured = set(nodes_by_url(tmp_path))
    assert captured == {ROOT, "http://example.com/courses/0", "http://example.com/courses/1"}


def test_a_cap_leaves_the_passed_over_pages_discoverable(tmp_path: Path) -> None:
    """They are not crawled, but they are not lost either: the page that
    linked to them still names them, which is what the inspector shows as
    "not crawled" — and what a later resume follows."""
    crawl(ROOT, tmp_path, 100, per_pattern=1, renderer=FakeRenderer(shop()))

    record = json.loads((tmp_path / PAGES_DIR / "000001.json").read_text())
    assert record["links"] == [f"http://example.com/courses/{n}" for n in range(5)]


def test_a_cap_can_be_lifted_by_resuming(tmp_path: Path) -> None:
    crawl(ROOT, tmp_path, 100, per_pattern=1, renderer=FakeRenderer(shop()))
    assert len(read_graph(tmp_path)["nodes"]) == 2

    crawl(ROOT, tmp_path, 100, per_pattern=5, resume=True, renderer=FakeRenderer(shop()))

    assert len(read_graph(tmp_path)["nodes"]) == 6


def test_a_cap_reports_what_it_passed_over(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Silently capturing four of five pages would read as a crawler that
    missed them."""
    crawl(ROOT, tmp_path, 100, per_pattern=1, renderer=FakeRenderer(shop()))

    out = capsys.readouterr().out
    assert "4 further page(s) of a route already seen" in out
    assert "--per-pattern" in out


def test_pages_a_full_page_capture_could_not_reach_are_named(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A shell layout puts the page inside its own scroller, so the document
    never grows past the window and --full-page has nothing to reach for. The
    screenshot then looks exactly like that of a short page, and the only
    honest thing to do is say which pages those were."""
    pages = {
        ROOT: result("/shell", "/short"),
        "http://example.com/shell": result(scrolls_inside=True),
        "http://example.com/short": result(),
    }
    crawl(ROOT, tmp_path, 100, full_page=True, renderer=FakeRenderer(pages))

    note = summary(capsys.readouterr().out)
    assert "1 page(s) captured only down to the fold" in note
    assert "  /shell" in note
    assert "/short" not in note


def test_fold_only_pages_are_listed_only_while_the_list_stays_useful(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A hundred trapped pages are one fact, not a hundred lines of output."""
    hrefs = [f"/shell/{n}" for n in range(8)]
    pages = {ROOT: result(*hrefs)}
    pages.update(
        {f"http://example.com/shell/{n}": result(scrolls_inside=True) for n in range(8)}
    )
    crawl(ROOT, tmp_path, 100, full_page=True, renderer=FakeRenderer(pages))

    note = summary(capsys.readouterr().out)
    assert "8 page(s) captured only down to the fold" in note
    assert note.count("  /shell/") == 5
    assert "…and 3 more" in note


def test_a_quiet_capture_says_nothing_about_folds(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The note is a caveat, not a status line: no trapped pages, no mention."""
    pages = {ROOT: result("/short"), "http://example.com/short": result()}
    crawl(ROOT, tmp_path, 100, full_page=True, renderer=FakeRenderer(pages))

    # The exact sentence, not the word: pytest's tmp_path is under a directory
    # called "folders", which a bare "fold" would match.
    assert "down to the fold" not in capsys.readouterr().out


def test_a_cap_does_not_touch_pages_that_are_their_own_route(tmp_path: Path) -> None:
    """Only routes with identifiers are capped; /about and /contact are two
    routes, and a cap of one must not merge them."""
    pages = {
        ROOT: result("/about", "/contact", "/courses/1", "/courses/2"),
        "http://example.com/about": result(),
        "http://example.com/contact": result(),
        "http://example.com/courses/1": result(),
        "http://example.com/courses/2": result(),
    }
    crawl(ROOT, tmp_path, 100, per_pattern=1, renderer=FakeRenderer(pages))

    captured = set(nodes_by_url(tmp_path))
    assert "http://example.com/about" in captured
    assert "http://example.com/contact" in captured
    assert "http://example.com/courses/2" not in captured


def test_a_page_linked_only_from_a_passed_over_instance_is_never_found(
    tmp_path: Path,
) -> None:
    """The cost of the cap, pinned so it is a documented property rather than a
    surprise: the link lives on an instance that was never loaded."""
    pages = {
        ROOT: result("/courses/1", "/courses/2"),
        "http://example.com/courses/1": result(),
        "http://example.com/courses/2": result("/hidden"),
        "http://example.com/hidden": result(),
    }
    crawl(ROOT, tmp_path, 100, per_pattern=1, renderer=FakeRenderer(pages))

    assert "http://example.com/hidden" not in nodes_by_url(tmp_path)
    # ...but without the cap it is found, which is what the cap costs.
    other = tmp_path / "uncapped"
    crawl(ROOT, other, 100, renderer=FakeRenderer(pages))
    assert "http://example.com/hidden" in nodes_by_url(other)


# --- leaving paths alone -------------------------------------------------


def mixed_site() -> dict[str, PageResult]:
    return {
        ROOT: result("/admin", "/admin/users", "/administrators", "/about", "/q.pdf"),
        "http://example.com/admin": result("/secret"),
        "http://example.com/admin/users": result(),
        "http://example.com/administrators": result(),
        "http://example.com/about": result(),
        "http://example.com/q.pdf": result("/deep"),
        "http://example.com/secret": result(),
        "http://example.com/deep": result(),
    }


def test_a_skipped_path_is_never_visited(tmp_path: Path) -> None:
    crawl(ROOT, tmp_path, 100, skip=["/admin"], renderer=FakeRenderer(mixed_site()))

    captured = set(nodes_by_url(tmp_path))
    assert "http://example.com/admin" not in captured
    assert "http://example.com/admin/users" not in captured
    # ...and neither is anything only reachable through it.
    assert "http://example.com/secret" not in captured


def test_skipping_leaves_the_neighbours_alone(tmp_path: Path) -> None:
    """The whole reason the literal ends on a segment edge."""
    crawl(ROOT, tmp_path, 100, skip=["/admin"], renderer=FakeRenderer(mixed_site()))

    captured = set(nodes_by_url(tmp_path))
    assert "http://example.com/administrators" in captured
    assert "http://example.com/about" in captured


def test_a_skipped_page_is_still_recorded_as_a_link(tmp_path: Path) -> None:
    """The page was never captured, but the page that pointed at it did point
    at it, and the record has to keep saying so — that is what shows up as
    "not crawled" rather than as a link that vanished."""
    crawl(ROOT, tmp_path, 100, skip=["/admin"], renderer=FakeRenderer(mixed_site()))

    record = json.loads((tmp_path / PAGES_DIR / "000001.json").read_text())
    assert "http://example.com/admin" in record["links"]
    assert "http://example.com/admin/users" in record["links"]


def test_a_regex_skips_what_a_path_cannot(tmp_path: Path) -> None:
    crawl(
        ROOT,
        tmp_path,
        100,
        skip=[r"re:\.pdf$"],
        renderer=FakeRenderer(mixed_site()),
    )

    captured = set(nodes_by_url(tmp_path))
    assert "http://example.com/q.pdf" not in captured
    assert "http://example.com/deep" not in captured, "its only link was the pdf"
    assert "http://example.com/admin" in captured


def test_several_patterns_are_a_union(tmp_path: Path) -> None:
    crawl(
        ROOT,
        tmp_path,
        100,
        skip=["/admin", r"re:\.pdf$"],
        renderer=FakeRenderer(mixed_site()),
    )

    captured = set(nodes_by_url(tmp_path))
    assert "http://example.com/admin" not in captured
    assert "http://example.com/q.pdf" not in captured
    assert "http://example.com/about" in captured


def test_a_skip_reports_what_it_left_out(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    crawl(ROOT, tmp_path, 100, skip=["/admin"], renderer=FakeRenderer(mixed_site()))

    out = capsys.readouterr().out
    assert "skipped by --skip" in out
    assert "uncaptured links" in out


def test_a_skip_does_not_use_up_a_pattern_allowance(tmp_path: Path) -> None:
    """A page nobody is going to capture should not count against the route it
    happens to share with pages that will be."""
    pages = {
        ROOT: result("/courses/1", "/courses/2", "/skipme/1"),
        "http://example.com/courses/1": result(),
        "http://example.com/courses/2": result(),
        "http://example.com/skipme/1": result(),
    }
    crawl(
        ROOT,
        tmp_path,
        100,
        per_pattern=1,
        skip=["/skipme"],
        renderer=FakeRenderer(pages),
    )

    captured = set(nodes_by_url(tmp_path))
    assert "http://example.com/courses/1" in captured
    assert "http://example.com/skipme/1" not in captured


def test_a_skip_that_would_skip_the_start_url_is_refused(tmp_path: Path) -> None:
    """An empty crawl should not need reading the output to notice."""
    with pytest.raises(ValueError) as excinfo:
        crawl(ROOT, tmp_path, 10, skip=["/"], renderer=FakeRenderer({}))

    assert "start URL" in str(excinfo.value)
    assert not (tmp_path / GRAPH_FILE).exists()


def test_skipping_nothing_is_the_same_as_not_asking(tmp_path: Path) -> None:
    with_skip = tmp_path / "a"
    without = tmp_path / "b"
    crawl(ROOT, with_skip, 100, skip=[], renderer=FakeRenderer(mixed_site()))
    crawl(ROOT, without, 100, renderer=FakeRenderer(mixed_site()))

    assert set(nodes_by_url(with_skip)) == set(nodes_by_url(without))


# --- looking before writing ---------------------------------------------


def bulk_site(rows: int = 8) -> dict[str, PageResult]:
    pages = {ROOT: result(*[f"/courses/{n}" for n in range(rows)], "/about")}
    for number in range(rows):
        pages[f"http://example.com/courses/{number}"] = result("/")
    pages["http://example.com/about"] = result()
    return pages


def test_a_dry_run_writes_nothing_at_all(tmp_path: Path) -> None:
    output = tmp_path / "never-created"
    crawl(ROOT, output, 100, dry_run=True, renderer=FakeRenderer(bulk_site()))

    assert not output.exists(), "a dry run must not even create the directory"


def test_a_dry_run_still_walks_the_whole_site(tmp_path: Path) -> None:
    """The report is only worth reading if it looked at the same pages a real
    crawl would have."""
    renderer = FakeRenderer(bulk_site())
    crawl(ROOT, tmp_path / "out", 100, dry_run=True, renderer=renderer)

    assert len(renderer.visited) == 10
    assert renderer.visited[0] == ROOT


def test_a_dry_run_reports_what_each_route_would_cost(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    crawl(ROOT, tmp_path / "out", 100, dry_run=True, renderer=FakeRenderer(bulk_site()))

    out = capsys.readouterr().out
    assert "Dry run — nothing was written." in out
    assert "Would capture 10 page(s)" in out
    assert "/courses/:id" in out
    assert "8" in out and "80%" in out


def test_a_dry_run_suggests_both_ways_to_leave_a_route_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """--per-pattern keeps one page of the route; --skip drops it. Which is
    right is the user's call, so the report does not make it."""
    crawl(ROOT, tmp_path / "out", 100, dry_run=True, renderer=FakeRenderer(bulk_site()))

    out = capsys.readouterr().out
    assert "--per-pattern 1" in out
    assert "--skip /courses" in out


def test_a_dry_run_says_nothing_to_skip_when_nothing_dominates(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A report that finds something to skip on every crawl is a report whose
    suggestions get ignored."""
    pages = {ROOT: result("/about", "/courses/1", "/courses/2")}
    for url in ("http://example.com/about", "http://example.com/courses/1",
                "http://example.com/courses/2"):
        pages[url] = result()
    crawl(ROOT, tmp_path / "out", 100, dry_run=True, renderer=FakeRenderer(pages))

    out = capsys.readouterr().out
    assert "/courses/:id" in out, "two pages is still worth listing"
    assert "--per-pattern 1" not in out, (
        "but two pages is not a bulk problem, whatever share of a small crawl "
        "they happen to be"
    )


def test_a_dry_run_never_suggests_skipping_the_whole_site(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A route parameterised from the first segment has no shorter prefix than
    everything, and suggesting `--skip /` would be advice to skip the site."""
    pages = {ROOT: result(*[f"/{n}" for n in range(1, 7)])}
    for number in range(1, 7):
        pages[f"http://example.com/{number}"] = result("/")
    crawl(ROOT, tmp_path / "out", 100, dry_run=True, renderer=FakeRenderer(pages))

    out = capsys.readouterr().out
    assert "/:id" in out
    assert "--per-pattern 1" in out, "the route still dominates"
    assert "--skip" not in out, "and there is no prefix worth suggesting"
    assert not (tmp_path / "out").exists()


def test_a_dry_run_reports_the_same_caps_a_real_one_would_apply(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The two flags compose: preview what --per-pattern would leave you."""
    crawl(
        ROOT,
        tmp_path / "out",
        100,
        dry_run=True,
        per_pattern=2,
        renderer=FakeRenderer(bulk_site()),
    )

    out = capsys.readouterr().out
    assert "Would capture 4 page(s)" in out
    assert "6 further page(s) of a route already seen" in out


def test_a_dry_run_leaves_no_scratch_directory_behind(tmp_path: Path) -> None:
    """The scratch directory is created by `create`, which a dry run skips —
    otherwise sweeping it would recreate the directory it just avoided."""
    output = tmp_path / "out"
    crawl(ROOT, output, 100, dry_run=True, renderer=FakeRenderer(bulk_site()))

    assert not (output / SCREENSHOTS_DIR).exists()


# --- input validation ---------------------------------------------------


@pytest.mark.parametrize("url", ["not a url", "localhost:3000", "ftp://example.com"])
def test_unusable_start_url_is_rejected(tmp_path: Path, url: str) -> None:
    with pytest.raises(ValueError):
        crawl(url, tmp_path, 10, renderer=FakeRenderer({}))
