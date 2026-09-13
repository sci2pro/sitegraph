"""Tests for the command-line surface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sitegraph.cli import DEFAULT_VIEWPORT, build_parser, main
from sitegraph.store import GRAPH_FILE


def test_crawl_defaults() -> None:
    args = build_parser().parse_args(["crawl", "http://localhost:3000"])

    assert args.command == "crawl"
    assert args.url == "http://localhost:3000"
    assert args.output == ".sitegraph"
    assert args.max_pages == 500


def test_crawl_accepts_overrides() -> None:
    args = build_parser().parse_args(
        ["crawl", "http://localhost:3000", "--output", "out", "--max-pages", "10"]
    )

    assert args.output == "out"
    assert args.max_pages == 10


def test_serve_defaults() -> None:
    args = build_parser().parse_args(["serve"])

    assert args.command == "serve"
    assert args.dir == ".sitegraph"
    assert args.port == 4777


def test_serve_accepts_overrides() -> None:
    args = build_parser().parse_args(["serve", "--dir", "out", "--port", "8080"])

    assert args.dir == "out"
    assert args.port == 8080


def test_crawl_defaults_to_no_session() -> None:
    args = build_parser().parse_args(["crawl", "http://localhost:3000"])

    assert args.storage_state is None


def test_crawl_accepts_a_session() -> None:
    args = build_parser().parse_args(
        ["crawl", "http://localhost:3000", "--storage-state", "s.json"]
    )

    assert args.storage_state == "s.json"


def test_crawl_defaults_to_a_fresh_crawl() -> None:
    args = build_parser().parse_args(["crawl", "http://localhost:3000"])

    assert args.resume is False


def test_the_documented_default_viewport_matches_the_crawler() -> None:
    """The help text spells the default out so `--help` need not import
    Playwright; this is what stops the spelling drifting from the code."""
    from sitegraph.crawl import VIEWPORT_HEIGHT, VIEWPORT_WIDTH

    assert DEFAULT_VIEWPORT == f"{VIEWPORT_WIDTH}x{VIEWPORT_HEIGHT}"


def test_crawl_defaults_to_the_documented_viewport() -> None:
    args = build_parser().parse_args(["crawl", "http://localhost:3000"])

    assert args.viewport is None, "None means 'whatever the crawler defaults to'"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1920x1080", (1920, 1080)), ("390X844", (390, 844)), ("800x600", (800, 600))],
)
def test_crawl_accepts_a_viewport(value: str, expected: tuple[int, int]) -> None:
    args = build_parser().parse_args(["crawl", "http://x/", "--viewport", value])

    assert args.viewport == expected


@pytest.mark.parametrize(
    "value",
    [
        "1920",  # one number is not a size
        "1920x",  # and neither is half of one
        "abc",
        "1920x1080x2",
        "-100x100",
        "0x100",  # nothing to render into
        "100x0",
        "99999x100",  # a typo rather than a screen
    ],
)
def test_crawl_rejects_an_impossible_viewport(value: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["crawl", "http://x/", "--viewport", value])


def test_crawl_skips_nothing_by_default() -> None:
    args = build_parser().parse_args(["crawl", "http://localhost:3000"])

    assert args.skip is None


def test_crawl_accepts_several_skip_patterns() -> None:
    args = build_parser().parse_args(
        [
            "crawl",
            "http://x/",
            "--skip",
            "/admin",
            "--skip",
            r"re:\.pdf$",
            "--skip",
            "/logout",
        ]
    )

    assert args.skip == ["/admin", r"re:\.pdf$", "/logout"]


@pytest.mark.parametrize(
    "value",
    [
        "admin",  # cannot match a path, which always starts with "/"
        "http://x/admin",  # a URL, not a path
        "re:[unclosed",  # not a regex
    ],
)
def test_crawl_rejects_an_unusable_skip_pattern(value: str) -> None:
    """Rejected at the command line rather than as a crawl that quietly
    skipped nothing."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["crawl", "http://x/", "--skip", value])


def test_crawl_captures_every_instance_by_default() -> None:
    args = build_parser().parse_args(["crawl", "http://localhost:3000"])

    assert args.per_pattern is None, "capping is lossy, so it is opt-in"


def test_crawl_accepts_a_per_pattern_cap() -> None:
    args = build_parser().parse_args(["crawl", "http://x/", "--per-pattern", "3"])

    assert args.per_pattern == 3


@pytest.mark.parametrize("value", ["0", "-1", "one"])
def test_crawl_rejects_an_impossible_pattern_cap(value: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["crawl", "http://x/", "--per-pattern", value])


def test_crawl_lets_the_host_decide_the_worker_count() -> None:
    """None rather than a number: how many workers is right depends on whether
    the target is this machine, which the parser cannot know."""
    args = build_parser().parse_args(["crawl", "http://localhost:3000"])

    assert args.workers is None


def test_crawl_accepts_a_worker_count() -> None:
    args = build_parser().parse_args(["crawl", "http://x/", "--workers", "8"])

    assert args.workers == 8


@pytest.mark.parametrize("value", ["0", "-2", "many"])
def test_crawl_rejects_an_impossible_worker_count(value: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["crawl", "http://x/", "--workers", value])


def test_crawl_accepts_a_pool_for_a_signed_in_crawl(tmp_path: Path) -> None:
    """The combination used to be refused outright; the workers share one
    session now, so it is a normal crawl. The assertion is only that the flags
    parse and are not rejected — the sharing itself is tested against a server
    that rotates its cookie, in test_login.py."""
    session = tmp_path / "session.json"
    session.write_text("{}", encoding="utf-8")

    args = build_parser().parse_args(
        [
            "crawl",
            "http://localhost:3000",
            "--storage-state",
            str(session),
            "--workers",
            "4",
        ]
    )

    assert args.workers == 4
    assert args.storage_state == str(session)


def test_crawl_accepts_resume() -> None:
    args = build_parser().parse_args(["crawl", "http://localhost:3000", "--resume"])

    assert args.resume is True


def test_crawl_reports_nothing_to_resume(tmp_path: Path) -> None:
    """--resume against a directory that holds no crawl should say so plainly,
    rather than silently starting a fresh crawl or crashing."""
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "crawl",
                "http://localhost:3000",
                "--output",
                str(tmp_path / "empty"),
                "--resume",
            ]
        )

    message = str(excinfo.value)
    assert "cannot resume" in message
    assert "drop --resume" in message


def test_crawl_resume_is_free_when_there_is_nothing_to_do(tmp_path: Path) -> None:
    """A finished crawl resumed is a no-op that never opens a browser, so it
    must not need one installed to exit cleanly."""
    output = tmp_path / ".sitegraph"
    (output / "pages").mkdir(parents=True)
    (output / GRAPH_FILE).write_text(
        json.dumps(
            {
                "root": "000001",
                "nodes": [
                    {
                        "id": "000001",
                        "url": "http://example.com/",
                        "title": "Home",
                        "depth": 0,
                        "status": 200,
                        "failed": False,
                        "screenshot": "screenshots/000001.webp",
                    }
                ],
                "edges": [],
            }
        ),
        encoding="utf-8",
    )
    (output / "pages" / "000001.json").write_text(
        json.dumps({"id": "000001", "links": []}), encoding="utf-8"
    )

    assert main(["crawl", "http://example.com/", "--output", str(output), "--resume"]) == 0


def test_login_defaults() -> None:
    args = build_parser().parse_args(["login", "http://localhost:3000/login"])

    assert args.command == "login"
    assert args.url == "http://localhost:3000/login"
    # Inside the output directory: gitignored, and not routed by `serve`.
    assert args.storage_state == ".sitegraph/session.json"


def test_login_accepts_overrides() -> None:
    args = build_parser().parse_args(
        ["login", "http://x/login", "--storage-state", "me.json"]
    )

    assert args.storage_state == "me.json"


def test_login_url_is_required() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["login"])


def test_url_is_required() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["crawl"])


def test_crawl_reports_a_missing_session_file(tmp_path: Path) -> None:
    """Better than Playwright's own error, which is a bare FileNotFoundError
    raised from inside the driver after the browser has launched."""
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "crawl",
                "http://localhost:3000",
                "--output",
                str(tmp_path / "out"),
                "--storage-state",
                str(tmp_path / "nope.json"),
            ]
        )

    assert "sitegraph login" in str(excinfo.value)
    assert not (tmp_path / "out").exists(), "nothing should be written"


def test_crawl_reports_an_unusable_url(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["crawl", "localhost:3000", "--output", str(tmp_path / "out")])

    assert "http://localhost:3000" in str(excinfo.value)


def test_command_is_required() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
