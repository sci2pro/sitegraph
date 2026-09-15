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
from fixture_site import _page, run_site
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from conftest import TINY_WEBP
from serving import request, running

from sitegraph.crawl import THUMB_WIDTH, VIEWPORT_HEIGHT, VIEWPORT_WIDTH, crawl
from sitegraph.store import GRAPH_FILE, PAGES_DIR, SCREENSHOTS_DIR, THUMBS_DIR

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


def measure(shot: Path) -> dict:
    """Open a screenshot in a browser and report what it actually is.

    Decoding in the browser rather than parsing the container by hand: WebP
    has several chunk layouts, and this is the same reader the UI uses.
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(shot.resolve().as_uri())
            return page.evaluate("""() => {
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
              const corner = ctx.getImageData(4, 4, 1, 1).data;
              return {width: img.naturalWidth, height: img.naturalHeight,
                      ink, total: canvas.width * canvas.height,
                      corner: [corner[0], corner[1], corner[2]]};
            }""")
        finally:
            browser.close()


def test_screenshots_are_rendered_pages_at_the_documented_viewport(
    graph: dict, crawled: Path
) -> None:
    """The bytes must be a picture of the page, at 1440x900 — not a blank frame."""
    root = graph["nodes"][0]
    measured = measure(crawled / root["screenshot"])

    assert measured["width"] == VIEWPORT_WIDTH
    assert measured["height"] == VIEWPORT_HEIGHT
    # The fixture pages are mostly white with dark text; a screenshot that
    # captured nothing at all would be uniformly blank.
    assert measured["ink"] > 0, "screenshot appears to be blank"


def test_screenshot_count_matches_rendered_pages(graph: dict, crawled: Path) -> None:
    rendered = [node for node in graph["nodes"] if not node["failed"]]
    files = sorted((crawled / SCREENSHOTS_DIR).glob("*.webp"))

    assert len(files) == len(rendered)


def test_a_custom_viewport_resizes_every_screenshot(site: str, tmp_path: Path) -> None:
    """The whole chain: `--viewport` reaches the browser, and every picture
    comes out that size rather than the hardcoded one."""
    from sitegraph.cli import main

    output = tmp_path / "narrow"
    assert main(["crawl", site, "--output", str(output), "--max-pages", "3",
                 "--viewport", "800x600"]) == 0

    graph = json.loads((output / GRAPH_FILE).read_text())
    assert len(graph["nodes"]) == 3
    for node in graph["nodes"]:
        measured = measure(output / node["screenshot"])
        assert (measured["width"], measured["height"]) == (800, 600)


def test_a_narrow_viewport_is_a_different_rendering_not_a_smaller_picture(
    tmp_path: Path,
) -> None:
    """The reason to allow a viewport at all: a narrow one gets the site's
    responsive layout, so the screenshots show what a phone would see. The page
    below paints a black square only when the media query matches, which makes
    that visible as a single pixel rather than as a guess about file size."""
    page = """<!doctype html><html><head><meta charset='utf-8'>
      <title>Responsive</title><style>
        body { margin: 0; background: #fff }
        #phone { display: none; width: 200px; height: 200px; background: #000 }
        @media (max-width: 600px) { #phone { display: block } }
      </style></head><body><div id="phone"></div></body></html>"""

    with run_site({"/": page}) as local:
        wide_dir = tmp_path / "wide"
        narrow_dir = tmp_path / "narrow"
        crawl(local, wide_dir, 1, viewport=(1440, 900), workers=1)
        crawl(local, narrow_dir, 1, viewport=(390, 844), workers=1)

    def corner_of(directory: Path) -> list[int]:
        node = json.loads((directory / GRAPH_FILE).read_text())["nodes"][0]
        return measure(directory / node["screenshot"])["corner"]

    assert corner_of(wide_dir) == [255, 255, 255], "the media query should not match"
    assert corner_of(narrow_dir) == [0, 0, 0], "390px wide should match it"


# --- one card per route -------------------------------------------------


def shop_site(rows: int = 6) -> dict[str, str]:
    """A site whose rows each get their own page, as most apps have."""
    pages = {
        "/": _page(
            "Home",
            "".join(f"<a href='/courses/{n}'>Course {n}</a>" for n in range(rows))
            + "<a href='/about'>About</a>",
        ),
        "/about": _page("About", "<a href='/'>Home</a>"),
    }
    for number in range(rows):
        pages[f"/courses/{number}"] = _page(
            f"Course {number}", "<a href='/'>Home</a>"
        )
    return pages


def test_a_route_with_many_instances_draws_one_card(tmp_path: Path) -> None:
    """The point of the fold: the graph's complexity should track the app's
    *routes*, not its rows. Eight pages, three of them distinct."""
    with run_site(shop_site()) as site:
        output = tmp_path / "shop"
        crawl(site, output, 100, workers=1)

        graph = json.loads((output / GRAPH_FILE).read_text())
        assert len(graph["nodes"]) == 8, "every page is still crawled and stored"

        with running(output) as port:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 1400, "height": 880})
                problems = watch(page)
                try:
                    page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
                    page.wait_for_selector(".node", timeout=15_000)
                    page.wait_for_timeout(2000)

                    assert page.locator(".node").count() == 3, (
                        "home, about, and one card for the course route"
                    )
                    assert page.locator(".node .count").all_inner_texts() == ["×6"]
                    assert "/courses/:id" in page.locator(".node .path").all_inner_texts()
                    assert "8 pages" in page.locator("#counts").inner_text()
                    # The edges are between routes too, not between pages.
                    assert page.locator("line.edge").count() <= 4
                finally:
                    browser.close()

    assert problems == []


def test_a_folded_route_still_reaches_every_instance(tmp_path: Path) -> None:
    """Folding must not be a way of losing pages: each one is one click away."""
    with run_site(shop_site()) as site:
        output = tmp_path / "shop-again"
        crawl(site, output, 100, workers=1)

        with running(output) as port:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 1400, "height": 880})
                problems = watch(page)
                try:
                    page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
                    page.wait_for_selector(".node", timeout=15_000)
                    page.wait_for_timeout(2000)

                    route_id = page.evaluate("""() => {
                      const card = [...document.querySelectorAll('.node')]
                        .find((n) => n.innerText.includes('/courses/:id'));
                      select(card.dataset.id, { center: false });
                      return card.dataset.id;
                    }""")
                    page.wait_for_timeout(400)

                    listed = page.locator(".insp-section.instances .links button")
                    assert listed.count() == 6, "every instance should be listed"

                    listed.nth(3).click()
                    page.wait_for_timeout(500)
                    assert "/courses/" in page.locator("#inspector .url").inner_text()
                    # ...and the card stays the route's, not the instance's.
                    assert page.locator(".node.selected").count() == 1
                    assert page.locator(".node.selected").get_attribute("data-id") == route_id
                finally:
                    browser.close()

    assert problems == []


def test_folding_is_invisible_on_a_site_without_identifiers(
    site: str, tmp_path: Path
) -> None:
    """The fixture site's URLs are all named pages, so nothing folds and the
    view is what it always was."""
    output = tmp_path / "plain"
    crawl(site, output, 100, workers=1)
    graph = json.loads((output / GRAPH_FILE).read_text())

    with running(output) as port:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1400, "height": 880})
            problems = watch(page)
            try:
                page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
                page.wait_for_selector(".node", timeout=15_000)
                page.wait_for_timeout(2500)

                assert page.locator(".node").count() == len(graph["nodes"])
                assert page.locator(".node .count").count() == 0, "nothing to badge"
                assert "pages ·" not in page.locator("#counts").inner_text()
            finally:
                browser.close()

    assert problems == []


def tall_page(blocks: int = 6) -> str:
    """A page several screens long, which is what --full-page is for."""
    body = "".join(
        f"<section style='height:600px;background:#eef'>Section {i}</section>"
        for i in range(blocks)
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Tall</title>"
        f"</head><body>{body}</body></html>"
    )


def only_shot(directory: Path) -> dict:
    node = json.loads((directory / GRAPH_FILE).read_text())["nodes"][0]
    return measure(directory / node["screenshot"])


def test_a_full_page_capture_is_as_tall_as_the_page(tmp_path: Path) -> None:
    """The whole point: a long page arrives as one tall image instead of its
    first screenful, while the viewport still decides the width."""
    with run_site({"/": tall_page(), "/short": "<!doctype html><title>Short</title>hi"}) as site:
        viewport_dir = tmp_path / "viewport"
        full_dir = tmp_path / "full"
        crawl(site, viewport_dir, 5, viewport=(800, 600), workers=1)
        crawl(site, full_dir, 5, viewport=(800, 600), workers=1, full_page=True)

    cropped = only_shot(viewport_dir)
    whole = only_shot(full_dir)

    assert (cropped["width"], cropped["height"]) == (800, 600)
    assert whole["width"] == 800, "the viewport still sets the width laid out at"
    assert whole["height"] > 3000, "the page is six screens long"
    assert whole["ink"] > cropped["ink"], "and there is more of it to look at"


def test_a_page_shorter_than_the_viewport_is_unchanged(tmp_path: Path) -> None:
    """Nothing to scroll means nothing extra: a full-page capture of a short
    page is the viewport, not a sliver."""
    with run_site({"/": "<!doctype html><title>Short</title><p>hi"}) as site:
        crawl(site, tmp_path / "out", 5, viewport=(800, 600), workers=1, full_page=True)

    assert (only_shot(tmp_path / "out")["width"], only_shot(tmp_path / "out")["height"]) == (800, 600)


def shell_page() -> str:
    """An app shell: the window is the frame, and the content scrolls inside it.

    This is what every layout that pins a sidebar or a header looks like, and it
    defeats --full-page in a way that leaves no trace in the screenshot — the
    document really is one viewport tall, so there is nothing to capture. The
    page is three screens of content; the picture is one.
    """
    blocks = "".join(
        f"<section style='height:600px;background:#efe'>Section {i}</section>"
        for i in range(6)
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Shell</title>"
        "<style>html,body{height:100%;margin:0;overflow:hidden}"
        "#scroller{height:100%;overflow:auto}</style></head>"
        f"<body><div id='scroller'>{blocks}</div></body></html>"
    )


def test_content_trapped_in_a_scroller_is_reported_not_silently_cropped(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The honest failure mode of --full-page: some layouts cannot be captured
    whole, and the result is indistinguishable from a short page — so it has to
    be said, by name, at the end of the crawl."""
    pages = {
        "/": f"<!doctype html><title>Home</title><a href='/shell'>shell</a>"
        f"<a href='/tall'>tall</a>",
        "/shell": shell_page(),
        "/tall": tall_page(),
    }
    with run_site(pages) as site:
        output = tmp_path / "out"
        crawl(site, output, 5, viewport=(800, 600), workers=1, full_page=True)

    out = capsys.readouterr().out
    assert "1 page(s) captured only down to the fold" in out
    assert "/shell" in out
    assert "/tall" not in out[out.index("down to the fold") :]

    shots = {
        node["url"].removeprefix(site): measure(output / node["screenshot"])
        for node in json.loads((output / GRAPH_FILE).read_text())["nodes"]
    }
    # Not a bug in the flag: the page it could not reach is genuinely one
    # viewport tall, and the one beside it — same crawl, same flags — is not.
    assert shots["/shell"]["height"] == 600, "nothing to scroll, nothing to capture"
    assert shots["/tall"]["height"] > 3000, "the ordinary long page still works"


def test_every_captured_page_leaves_a_card_sized_copy(tmp_path: Path) -> None:
    """The graph view draws node cards from a copy of the capture, because an
    `<img>` decodes at its intrinsic size whatever it is painted at — so a few
    hundred cards pointed at full captures make the browser decode a few
    hundred 1440x900 images to paint each one the size of a full stop."""
    with run_site() as site:
        output = tmp_path / "out"
        crawl(site, output, 100, workers=1)

    graph = json.loads((output / GRAPH_FILE).read_text())
    rendered = [node for node in graph["nodes"] if not node["failed"]]
    assert rendered

    for node in rendered:
        assert node["thumb"] == f"thumbs/{node['id']}.webp"
        assert (output / node["thumb"]).is_file()
    assert len(list((output / THUMBS_DIR).glob("*.webp"))) == len(rendered)

    # The failed page is the exception, and the reasons are the same ones that
    # leave it without a capture.
    failed = [node for node in graph["nodes"] if node["failed"]]
    assert failed and all("thumb" not in node for node in failed)

    # A miniature of the page, not a blank frame — and small enough that the
    # saving is real rather than nominal.
    sample = rendered[0]
    copy_of = measure(output / sample["thumb"])
    capture = measure(output / sample["screenshot"])
    assert copy_of["width"] == THUMB_WIDTH
    assert copy_of["ink"] > 0, "the copy appears to be blank"
    assert copy_of["height"] < capture["height"]
    assert (output / sample["thumb"]).stat().st_size < (
        output / sample["screenshot"]
    ).stat().st_size


def test_a_copy_of_a_full_page_capture_still_crops_to_the_top(tmp_path: Path) -> None:
    """The copy keeps the capture's aspect, and the card crops it — so a tall
    capture gives a tall copy rather than being squashed into a card shape."""
    with run_site({"/": tall_page()}) as site:
        output = tmp_path / "out"
        crawl(site, output, 5, viewport=(800, 600), workers=1, full_page=True)

    node = json.loads((output / GRAPH_FILE).read_text())["nodes"][0]
    copy_of = measure(output / node["thumb"])
    capture = measure(output / node["screenshot"])

    assert copy_of["width"] == THUMB_WIDTH
    assert capture["height"] > 3000, "the capture is six screens long"
    assert copy_of["height"] > 1000, "so the copy is tall too, not squashed"
    assert copy_of["ink"] > 0


def test_a_tall_capture_is_reachable_in_the_inspector(tmp_path: Path) -> None:
    """A capture taller than the box it is shown in would be cropped to a
    sliver by `cover` — which is the thing --full-page was asked for. The
    inspector lets it take its own height and scrolls; the card stays a
    thumbnail."""
    with run_site({"/": tall_page()}) as site:
        output = tmp_path / "out"
        crawl(site, output, 5, viewport=(800, 600), workers=1, full_page=True)

        with running(output) as port:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 1300, "height": 850})
                problems = watch(page)
                try:
                    page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
                    page.wait_for_selector(".node", timeout=15_000)
                    page.wait_for_timeout(1500)

                    shot = page.locator("#inspector .insp-shot")
                    assert shot.count() == 1
                    assert shot.get_attribute("class").endswith("tall")
                    box = shot.bounding_box()
                    assert box["height"] > 800, "the picture should be shown whole"

                    scrolled = page.evaluate("""() => {
                      const el = document.getElementById('inspector');
                      return [el.scrollHeight, el.clientHeight];
                    }""")
                    assert scrolled[0] > scrolled[1], "the inspector should scroll"

                    card = page.locator(".node .thumb").first.bounding_box()
                    assert card["height"] < 120, "a card is still a thumbnail"
                finally:
                    browser.close()

    assert problems == []


def test_a_real_dry_run_writes_nothing(site: str, tmp_path: Path) -> None:
    """The promise, against a real browser and a real site: the same walk, and
    not one file."""
    from sitegraph.cli import main

    output = tmp_path / "would-be"
    assert main(["crawl", site, "--output", str(output), "--dry-run",
                 "--max-pages", "20"]) == 0

    assert not output.exists(), "a dry run created its output directory"
    assert not list(tmp_path.glob("sitegraph-dry-run-*")), (
        "the throwaway directory a dry run renders into was left behind"
    )


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
                counts = page.locator("#counts").inner_text()
                assert f"{len(graph['nodes'])} routes" in counts
                assert "links" in counts
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
                counts = page.locator("#counts").inner_text()
                assert f"{len(graph['nodes'])} routes" in counts
                assert "links" in counts

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


def test_the_graph_view_draws_the_copies_and_the_inspector_the_capture(
    crawled: Path, graph: dict
) -> None:
    """The whole point of the copies, asserted where it is visible: on what the
    browser asks for. A card is a thumbnail; the inspector is the page."""
    with running(crawled) as port:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1500, "height": 940})
            problems = watch(page)
            try:
                page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
                page.wait_for_selector(".node", timeout=15_000)
                page.wait_for_timeout(2500)

                sources = page.eval_on_selector_all(
                    ".node img", "els => els.map(e => e.getAttribute('src'))"
                )
                assert sources, "no card drew a picture"
                assert all(s.startswith("thumbs/") for s in sources), sources[:3]

                # The inspector is the one place the capture itself is wanted:
                # it is showing the page, not a card standing in for one.
                shot = page.locator("#inspector .insp-shot img").first
                assert shot.get_attribute("src").startswith("screenshots/")
            finally:
                browser.close()

    assert problems == []


def test_a_crawl_without_copies_still_opens(tmp_path: Path) -> None:
    """Every directory written before copies existed names no `thumb`, and the
    fallback has to be the capture rather than a 404 at a file that was never
    written — which is what asking for the copy unconditionally would do."""
    graph = {
        "root": "000001",
        "nodes": [
            {"id": "000001", "url": "http://example.com/", "title": "Home",
             "depth": 0, "status": 200, "failed": False,
             "screenshot": "screenshots/000001.webp"},
            {"id": "000002", "url": "http://example.com/about", "title": "About",
             "depth": 1, "status": 200, "failed": False,
             "screenshot": "screenshots/000002.webp"},
        ],
        "edges": [{"source": "000001", "target": "000002"}],
    }
    (tmp_path / GRAPH_FILE).write_text(json.dumps(graph), encoding="utf-8")
    (tmp_path / SCREENSHOTS_DIR).mkdir()
    (tmp_path / PAGES_DIR).mkdir()
    for node in graph["nodes"]:
        (tmp_path / node["screenshot"]).write_bytes(TINY_WEBP)
        (tmp_path / PAGES_DIR / f"{node['id']}.json").write_text(
            json.dumps(dict(node, links=[], error=None)), encoding="utf-8"
        )

    with running(tmp_path) as port:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1500, "height": 940})
            problems = watch(page)
            try:
                page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
                page.wait_for_selector(".node", timeout=15_000)
                page.wait_for_timeout(1500)

                assert page.locator(".node").count() == 2
                sources = page.eval_on_selector_all(
                    ".node img", "els => els.map(e => e.getAttribute('src'))"
                )
                assert len(sources) == 2
                assert all(s.startswith("screenshots/") for s in sources)
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
