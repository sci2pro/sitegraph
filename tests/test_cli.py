"""Tests for the command-line surface."""

from __future__ import annotations

import pytest

from sitegraph.cli import build_parser


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


def test_url_is_required() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["crawl"])


def test_command_is_required() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
