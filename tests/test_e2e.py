"""End-to-end tests: real Chromium against a real site, then the real UI.

Everything here is deliberately slow and browser-bound. The point is to cover
what unit tests structurally cannot: that Playwright captures what we think it
captures, that the WebP files are real and the right size, and that the served
page actually renders a graph in a browser without errors — which is the only
claim the MVP's success criterion (spec §12) actually makes.

Skipped wholesale when the Chromium binary is not installed. Run
`uv run playwright install chromium` to enable.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fixture_site import run_site
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from serving import request, running

from sitegraph.crawl import VIEWPORT_HEIGHT, VIEWPORT_WIDTH, crawl
from sitegraph.store import GRAPH_FILE, PAGES_DIR, SCREENSHOTS_DIR

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module", autouse=True)
def browser_installed() -> None:
    """Skip the module when Chromium is missing.

    A fixture rather than an import-time `skipif` for two reasons: the check
    costs about a second, and it must not run at all when these tests are
    deselected with `-m "not e2e"`. Starting and stopping Playwright without
    launching anything also makes it emit asyncio teardown noise, so the probe
    launches a browser (the operation actually being tested) and gets out.
    """
    try:
        with sync_playwright() as playwright:
            playwright.chromium.launch().close()
    except Exception as exc:  # noqa: BLE001 - a missing install raises several types
        pytest.skip(f"chromium is not installed ({exc})")


@pytest.fixture(scope="module")
def site():
    """The fixture site, alive for the whole module."""
    with run_site() as base:
        yield base


@pytest.fixture(scope="module")
def crawled(site: str, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real crawl of *site*, written to a temporary directory.

    One worker, deliberately. Queue order decides ID order, and ID order is
    only defined at width one — a pool assigns IDs in completion order, which
    is exactly the thing these tests must not depend on. Concurrency has its
    own test below, asserting the properties that do not care about order.
    """
    output = tmp_path_factory.mktemp("crawl") / ".sitegraph"
    crawl(site, output, 100, workers=1)
    return output


@pytest.fixture(scope="module")
def graph(crawled: Path) -> dict:
    return json.loads((crawled / GRAPH_FILE).read_text(encoding="utf-8"))


def by_path(graph: dict) -> dict[str, dict]:
    """Nodes keyed by path plus query, for readable assertions."""
    keyed = {}
    for node in graph["nodes"]:
        parts = urlsplit(node["url"])
        keyed[parts.path + (f"?{parts.query}" if parts.query else "")] = node
    return keyed


def url_set(graph: dict) -> list[str]:
    return sorted(node["url"] for node in graph["nodes"])


def url_edges(graph: dict) -> list[tuple[str, str]]:
    """Edges as URL pairs, so they can be compared across two crawls whose IDs
    were handed out in different orders."""
    by_id = {node["id"]: node["url"] for node in graph["nodes"]}
    return sorted({(by_id[edge["source"]], by_id[edge["target"]]) for edge in graph["edges"]})


def root_url(graph: dict) -> str | None:
    for node in graph["nodes"]:
        if node["id"] == graph["root"]:
            return node["url"]
    return None


# --- what got crawled ---------------------------------------------------


def test_discovers_the_whole_site(graph: dict) -> None:
    assert sorted(by_path(graph)) == sorted(
        [
            "/",
            "/about",
            "/about/",
            "/boom",
            "/courses",
            "/courses/123",
            "/courses/new",
            "/login",
            "/missing",
            "/search?q=bar",
            "/search?q=foo",
        ]
    )


def test_fragments_collapse_but_query_strings_do_not(graph: dict) -> None:
    urls = [node["url"] for node in graph["nodes"]]

    about = [url for url in urls if "/about" in url]
    assert len(about) == 2, "the #team link must not create a third /about node"
    assert not any("#" in url for url in urls)

    searches = [url for url in urls if "/search" in url]
    assert len(searches) == 2, "?q=foo and ?q=bar are different pages"


def test_off_origin_pages_are_not_crawled(graph: dict) -> None:
    assert not any("example.com" in node["url"] for node in graph["nodes"])


def test_depths_follow_the_link_graph(graph: dict) -> None:
    nodes = by_path(graph)

    assert nodes["/"]["depth"] == 0
    assert nodes["/about"]["depth"] == 1
    assert nodes["/courses"]["depth"] == 1
    assert nodes["/courses/123"]["depth"] == 2
    assert nodes["/search?q=foo"]["depth"] == 2


def test_edges_are_one_per_ordered_pair(graph: dict) -> None:
    pairs = [(edge["source"], edge["target"]) for edge in graph["edges"]]

    assert len(pairs) == len(set(pairs))
    assert ("000001", "000002") in pairs, "home -> about"


def test_every_edge_endpoint_exists(graph: dict) -> None:
    ids = {node["id"] for node in graph["nodes"]}

    for edge in graph["edges"]:
        assert edge["source"] in ids
        assert edge["target"] in ids


def test_http_error_page_is_captured_normally(graph: dict) -> None:
    """A 404 renders fine — it is a page, not a failure."""
    missing = by_path(graph)["/missing"]

    assert missing["status"] == 404
    assert missing["failed"] is False
    assert missing["screenshot"]


def test_unrenderable_page_is_recorded_as_failed(graph: dict, crawled: Path) -> None:
    """Spec §7: it still appears in the graph, marked as failed."""
    boom = by_path(graph)["/boom"]

    assert boom["failed"] is True
    assert boom["status"] is None
    assert boom["screenshot"] is None
    assert (crawled / PAGES_DIR / f"{boom['id']}.json").is_file()


def test_page_records_match_the_graph(graph: dict, crawled: Path) -> None:
    for node in graph["nodes"]:
        record = json.loads((crawled / PAGES_DIR / f"{node['id']}.json").read_text())
        assert record["url"] == node["url"]
        assert record["title"] == node["title"]
        assert record["depth"] == node["depth"]


def test_graph_links_are_consistent_with_page_records(
    graph: dict, crawled: Path
) -> None:
    """Every edge must be backed by a link in the source page's record."""
    id_of_url = {node["url"]: node["id"] for node in graph["nodes"]}
    url_of_id = {node["id"]: node["url"] for node in graph["nodes"]}

    for edge in graph["edges"]:
        record = json.loads(
            (crawled / PAGES_DIR / f"{edge['source']}.json").read_text()
        )
        assert url_of_id[edge["target"]] in record["links"]


# --- screenshots --------------------------------------------------------


def test_every_rendered_page_has_a_screenshot(graph: dict, crawled: Path) -> None:
    for node in graph["nodes"]:
        path = crawled / node["screenshot"] if node["screenshot"] else None
        if node["failed"]:
            assert node["screenshot"] is None
            continue
        assert path is not None and path.is_file(), f"{node['url']} has no screenshot"
        data = path.read_bytes()
        assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def test_screenshots_are_rendered_pages_at_the_documented_viewport(
    graph: dict, crawled: Path
) -> None:
    """The bytes must be a picture of the page, at 1440x900 — not a blank frame."""
    root = graph["nodes"][0]
    shot = (crawled / root["screenshot"]).resolve()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(shot.as_uri())
            measured = page.evaluate("""() => {
              const img = document.querySelector('img');
              const canvas = document.createElement('canvas');
              canvas.width = img.naturalWidth;
              canvas.height = img.naturalHeight;
              const ctx = canvas.getContext('2d');
              ctx.drawImage(img, 0, 0);
              const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
              let ink = 0;
              for (let i = 0; i < data.length; i += 4) {
                if (data[i] < 200 || data[i+1] < 200 || data[i+2] < 200) ink++;
              }
              return {width: img.naturalWidth, height: img.naturalHeight,
                      ink, total: canvas.width * canvas.height};
            }""")
        finally:
            browser.close()

    assert measured["width"] == VIEWPORT_WIDTH
    assert measured["height"] == VIEWPORT_HEIGHT
    # The fixture pages are mostly white with dark text; a screenshot that
    # captured nothing at all would be uniformly blank.
    assert measured["ink"] > 0, "screenshot appears to be blank"


def test_screenshot_count_matches_rendered_pages(graph: dict, crawled: Path) -> None:
    rendered = [node for node in graph["nodes"] if not node["failed"]]
    files = sorted((crawled / SCREENSHOTS_DIR).glob("*.webp"))

    assert len(files) == len(rendered)


# --- the browser UI -----------------------------------------------------


def watch(page) -> list[str]:
    problems: list[str] = []
    page.on(
        "console",
        lambda message: problems.append(f"console.{message.type}: {message.text}")
        if message.type == "error"
        else None,
    )
    page.on("pageerror", lambda error: problems.append(f"pageerror: {error}"))
    page.on(
        "requestfailed",
        lambda request: problems.append(f"requestfailed: {request.url}"),
    )
    return problems


def test_ui_renders_the_graph(crawled: Path, graph: dict) -> None:
    with running(crawled) as port:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1500, "height": 940})
            problems = watch(page)
            try:
                page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
                page.wait_for_selector(".node", timeout=15_000)
                page.wait_for_timeout(2500)  # let the layout pass finish

                assert page.locator(".node").count() == len(graph["nodes"])
                assert page.locator("line.edge").count() == len(graph["edges"])
                assert "pages" in page.locator("#counts").inner_text()
            finally:
                browser.close()

    assert problems == []


def test_ui_node_selection_and_inspector(crawled: Path) -> None:
    with running(crawled) as port:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1500, "height": 940})
            problems = watch(page)
            try:
                page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
                page.wait_for_selector(".node", timeout=15_000)
                page.wait_for_timeout(2500)

                # Clicking a node in the graph opens the inspector with that
                # page's metadata.
                page.locator(".node").filter(has_text="Courses").first.click()
                page.wait_for_timeout(500)
                assert page.locator("#inspector").is_visible()
                assert "Courses" in page.locator("#inspector h2").inner_text()
                assert "/courses" in page.locator("#inspector .url").inner_text()

                # Outgoing links resolve to real pages and navigate the
                # selection; incoming links are drawn from the edges.
                assert page.locator(".insp-section.out .links button").count() > 0
                assert page.locator(".insp-section.in .links button").count() > 0
                assert page.locator(".insp-section.in .links li.uncrawled").count() == 0

                page.locator(".insp-section.out .links button").first.click()
                page.wait_for_timeout(400)
                assert page.locator(".node.selected").count() == 1

                page.keyboard.press("Escape")
                page.wait_for_timeout(200)
                assert page.locator("#inspector").is_hidden()
            finally:
                browser.close()

    assert problems == []


def test_ui_shows_failed_pages_distinctly(crawled: Path) -> None:
    with running(crawled) as port:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1500, "height": 940})
            try:
                page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
                page.wait_for_selector(".node", timeout=15_000)
                page.wait_for_timeout(2000)

                failed = page.locator(".node.failed")
                assert failed.count() == 1
                assert "failed" in failed.first.inner_text()
                placeholder = failed.first.locator(".shot-missing")
                assert "failed to render" in placeholder.inner_text()

                # The original URL is offered even for a page that never loaded.
                failed.first.click()
                page.wait_for_timeout(400)
                assert page.locator("#inspector .chip.bad").count() >= 1
            finally:
                browser.close()


def test_ui_contact_sheet_and_filter(crawled: Path, graph: dict) -> None:
    with running(crawled) as port:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1500, "height": 940})
            problems = watch(page)
            try:
                page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
                page.wait_for_selector(".node", timeout=15_000)

                page.click("#tab-sheet")
                page.wait_for_timeout(600)
                assert page.locator(".tile").count() == len(graph["nodes"])

                page.fill("#filter", "course")
                page.wait_for_timeout(300)
                visible = page.locator(".tile:visible").count()
                assert 0 < visible < len(graph["nodes"])
                assert "pages" in page.locator("#counts").inner_text()

                # A tile opens the same inspector the graph uses.
                page.locator(".tile:visible").first.click()
                page.wait_for_timeout(400)
                assert page.locator("#inspector").is_visible()

                page.fill("#filter", "")
                page.wait_for_timeout(300)
                assert page.locator(".tile:visible").count() == len(graph["nodes"])
            finally:
                browser.close()

    assert problems == []


def test_ui_node_dragging_moves_only_that_node(crawled: Path) -> None:
    with running(crawled) as port:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            try:
                page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
                page.wait_for_selector(".node", timeout=15_000)
                page.wait_for_timeout(2500)

                other_before = (
                    page.locator(".node").filter(has_text="Login").first.bounding_box()
                )
                card = page.locator(".node").filter(has_text="Courses").first
                box = card.bounding_box()

                # Press the card's centre: the fitted zoom shrinks the cards, so
                # a fixed offset would land on the background and pan instead.
                def centre(rect):
                    return rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2

                start_x, start_y = centre(box)
                page.mouse.move(start_x, start_y)
                page.mouse.down()
                page.mouse.move(start_x - 180, start_y + 120, steps=8)
                page.mouse.up()
                page.wait_for_timeout(400)

                moved = card.bounding_box()
                assert moved["x"] < box["x"] - 100
                assert moved["y"] > box["y"] + 50
                # Dragging one node must not disturb the rest of the layout.
                other_after = (
                    page.locator(".node").filter(has_text="Login").first.bounding_box()
                )
                assert other_after == other_before

                # A drag is not a click: it must not hijack the selection.
                page.keyboard.press("Escape")
                page.wait_for_timeout(200)
                click_x, click_y = centre(moved)
                page.mouse.move(click_x, click_y)
                page.mouse.down()
                page.mouse.up()
                page.wait_for_timeout(400)
                assert "Courses" in page.locator(".node.selected").inner_text()
            finally:
                browser.close()


def test_ui_pan_and_zoom(crawled: Path) -> None:
    with running(crawled) as port:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1500, "height": 940})
            try:
                page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
                page.wait_for_selector(".node", timeout=15_000)
                page.wait_for_timeout(2000)

                before = page.locator("#world").bounding_box()
                page.mouse.move(500, 400)
                page.mouse.down()
                page.mouse.move(650, 520, steps=5)
                page.mouse.up()
                after = page.locator("#world").bounding_box()
                assert (after["x"], after["y"]) != (before["x"], before["y"])

                page.mouse.move(500, 400)
                page.mouse.wheel(0, -240)
                page.wait_for_timeout(300)
                zoomed = page.locator("#world").bounding_box()
                assert zoomed["width"] > after["width"]

                page.click('[data-zoom="reset"]')
                page.wait_for_timeout(300)
                assert page.locator("#world").bounding_box()["width"] != zoomed["width"]
            finally:
                browser.close()


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        (None, "could not load"),  # no crawl here at all -> 404
        ("null", "not a sitegraph graph"),  # valid JSON, wrong shape
        ('{"nodes": "nope"}', "not a sitegraph graph"),
    ],
)
def test_ui_explains_an_unusable_graph(
    tmp_path: Path, contents: str | None, expected: str
) -> None:
    """A blank page is the worst possible error message."""
    if contents is not None:
        (tmp_path / GRAPH_FILE).write_text(contents, encoding="utf-8")

    with running(tmp_path) as port:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            try:
                page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
                page.wait_for_selector("#empty:not([hidden])", timeout=10_000)
                assert expected in page.locator("#empty").inner_text().lower()
            finally:
                browser.close()


# --- resume, end to end -------------------------------------------------


def test_resuming_a_capped_crawl_finishes_it(
    site: str, tmp_path: Path, graph: dict
) -> None:
    """The user-facing promise: a crawl cut short by --max-pages can be picked
    up again, in the same directory, without re-capturing anything."""
    output = tmp_path / "resumed"

    crawl(site, output, 3)
    partial = json.loads((output / GRAPH_FILE).read_text())
    assert len(partial["nodes"]) == 3
    assert len(partial["nodes"]) < len(graph["nodes"])

    # Three pages a run, the same command each time, until it runs dry.
    for _ in range(10):
        crawl(site, output, 3, resume=True)
        if len(json.loads((output / GRAPH_FILE).read_text())["nodes"]) >= len(
            graph["nodes"]
        ):
            break

    finished = json.loads((output / GRAPH_FILE).read_text())
    # Compared as URLs rather than IDs: a resumed run picks up with a pool, so
    # its IDs follow completion order, but what the crawl *found* must be
    # identical to a crawl that ran to the end in one go.
    assert url_set(finished) == url_set(graph)
    assert url_edges(finished) == url_edges(graph)
    assert root_url(finished) == root_url(graph)


def test_resume_does_not_recapture_screenshots(site: str, tmp_path: Path) -> None:
    output = tmp_path / "kept"
    crawl(site, output, 3)

    kept = sorted(p.name for p in (output / SCREENSHOTS_DIR).glob("*.webp"))
    stamps = {
        path.name: path.stat().st_mtime_ns
        for path in (output / SCREENSHOTS_DIR).glob("*.webp")
    }

    crawl(site, output, 3, resume=True)

    after = {
        path.name: path.stat().st_mtime_ns
        for path in (output / SCREENSHOTS_DIR).glob("*.webp")
    }
    for name in kept:
        assert after[name] == stamps[name], f"{name} was captured twice"
    assert len(after) > len(kept), "the second run captured new pages"


def test_a_concurrent_crawl_finds_the_same_site(
    site: str, tmp_path: Path, graph: dict
) -> None:
    """The point of the pool is that it is faster, not that it is different:
    the same site, crawled four pages at a time, must produce the same graph
    apart from the order the nodes were numbered in."""
    output = tmp_path / "concurrent"
    crawl(site, output, 100, workers=4)
    concurrent = json.loads((output / GRAPH_FILE).read_text())

    assert url_set(concurrent) == url_set(graph)
    assert url_edges(concurrent) == url_edges(graph)
    assert root_url(concurrent) == root_url(graph)
    assert len(concurrent["nodes"]) == len(graph["nodes"])

    for node in concurrent["nodes"]:
        if node["failed"]:
            assert node["screenshot"] is None
            continue
        assert node["screenshot"] == f"screenshots/{node['id']}.webp"
        assert (output / node["screenshot"]).is_file()
    assert not list((output / SCREENSHOTS_DIR / ".incoming").glob("*"))


def test_cli_resume_round_trip(tmp_path: Path, site: str) -> None:
    """The two commands a user actually types."""
    from sitegraph.cli import main

    output = tmp_path / ".sitegraph"
    assert main(["crawl", site, "--output", str(output), "--max-pages", "2"]) == 0
    first = json.loads((output / GRAPH_FILE).read_text())
    assert len(first["nodes"]) == 2

    assert (
        main(
            [
                "crawl",
                site,
                "--output",
                str(output),
                "--max-pages",
                "2",
                "--resume",
            ]
        )
        == 0
    )

    second = json.loads((output / GRAPH_FILE).read_text())
    assert len(second["nodes"]) == 4
    assert [node["id"] for node in second["nodes"][:2]] == [
        node["id"] for node in first["nodes"]
    ]


# --- the CLI, end to end ------------------------------------------------


def test_cli_crawl_then_serve(tmp_path: Path, site: str) -> None:
    """The exact two commands from spec §2, against a live site."""
    from sitegraph.cli import main

    output = tmp_path / ".sitegraph"
    assert main(["crawl", site, "--output", str(output), "--max-pages", "5"]) == 0

    graph = json.loads((output / GRAPH_FILE).read_text())
    assert 0 < len(graph["nodes"]) <= 5

    with running(output) as port:
        _, _, body = request(port, "/graph.json")
        assert json.loads(body) == graph


def test_crawl_reports_a_bad_url(tmp_path: Path) -> None:
    from sitegraph.cli import main

    with pytest.raises(SystemExit) as excinfo:
        main(["crawl", "localhost:3000", "--output", str(tmp_path)])

    assert "http://localhost:3000" in str(excinfo.value)


def test_crawl_survives_an_unreachable_site(tmp_path: Path) -> None:
    """A start URL that refuses connections is a recorded failure, not a crash."""
    output = tmp_path / ".sitegraph"
    try:
        crawl("http://127.0.0.1:9/", output, 5)
    except PlaywrightError as exc:  # pragma: no cover - the point is it does not
        pytest.fail(f"crawl raised instead of recording the failure: {exc}")

    graph = json.loads((output / GRAPH_FILE).read_text())
    assert len(graph["nodes"]) == 1
    assert graph["nodes"][0]["failed"] is True
    assert graph["root"] == graph["nodes"][0]["id"]
