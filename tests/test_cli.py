"""Tests for the command-line surface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sitegraph.cli import build_parser, main
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


def test_crawl_refuses_a_pool_for_a_signed_in_crawl(tmp_path: Path) -> None:
    session = tmp_path / "session.json"
    session.write_text("{}", encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "crawl",
                "http://localhost:3000",
                "--output",
                str(tmp_path / "out"),
                "--storage-state",
                str(session),
                "--workers",
                "4",
            ]
        )

    message = str(excinfo.value)
    assert "signed-in" in message
    assert "drop --workers" in message
    assert not (tmp_path / "out").exists()


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
