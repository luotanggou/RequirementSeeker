from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from requirementseeker_collector import runner
from requirementseeker_collector.adapters import BilibiliAdapter, ResponseShapeChanged
from requirementseeker_collector.artifacts import validate_generation


@dataclass
class LocalSite:
    url: str
    server: ThreadingHTTPServer
    thread: threading.Thread
    comment_requests: list[str]
    websocket_requests: list[str]


@contextmanager
def serve_site(mode: str = "supported") -> Iterator[LocalSite]:
    page = (Path(__file__).parent / "site" / "index.html").read_bytes()
    comment_payload = (
        Path(__file__).parents[1] / "fixtures" / "bilibili" / "comments.json"
    ).read_bytes()
    comment_requests: list[str] = []
    websocket_requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path in {"/", "/index.html"}:
                body = page
                content_type = "text/html; charset=utf-8"
            elif parsed.path == "/x/v2/reply/wbi/main":
                shape = parse_qs(parsed.query).get("shape", [""])[0]
                comment_requests.append(shape)
                if shape == "delayed-unknown":
                    time.sleep(0.75)
                body = comment_payload if shape == "supported" else b'{"code":0,"unknown":[]}'
                content_type = "application/json"
                if shape == "http-500":
                    self.send_response(500)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
            elif parsed.path == "/socket":
                websocket_requests.append(parsed.path)
                self.send_error(400)
                return
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    site = LocalSite(
        f"http://{host}:{port}/index.html?mode={mode}",
        server,
        thread,
        comment_requests,
        websocket_requests,
    )
    try:
        yield site
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def local_site() -> Iterator[LocalSite]:
    with serve_site() as site:
        yield site


@pytest.fixture
def local_site_without_recent() -> Iterator[LocalSite]:
    with serve_site("without-recent") as site:
        yield site


@pytest.fixture
def local_unknown_site() -> Iterator[LocalSite]:
    with serve_site("unknown") as site:
        yield site


def test_page_flow_collects_response_and_dom_metadata(
    local_site: LocalSite, tmp_path: Path
) -> None:
    result = runner.collect_from_page(
        local_site.url, BilibiliAdapter(), output_root=tmp_path, headless=True
    )

    assert result.video.title == "Synthetic Video"
    assert result.video.description == "Synthetic integration page"
    assert [item.raw_comment_id for item in result.comments] == ["11", "12"]
    assert result.collection.pages_requested == 1
    assert result.collection.pages_succeeded == 1
    assert local_site.comment_requests == ["supported"]
    assert validate_generation(tmp_path / "raw" / "bilibili" / "BV1synthetic")[0] == result.video


def test_page_flow_records_unavailable_sort_and_keeps_actual_order(
    local_site_without_recent: LocalSite, tmp_path: Path
) -> None:
    result = runner.collect_from_page(
        local_site_without_recent.url,
        BilibiliAdapter(),
        output_root=tmp_path,
        headless=True,
    )

    assert "recent" not in result.collection.sort_modes
    assert result.collection.sort_modes == ["top"]
    assert any(
        error.category == "stratum_unavailable" for error in result.collection.collection_errors
    )


def test_unknown_response_shape_stops_without_commit(
    local_unknown_site: LocalSite, tmp_path: Path
) -> None:
    with pytest.raises(ResponseShapeChanged, match="^response_shape_changed$"):
        runner.collect_from_page(
            local_unknown_site.url,
            BilibiliAdapter(),
            output_root=tmp_path,
            headless=True,
        )

    assert not (tmp_path / "raw").exists()


def test_delayed_unknown_response_cannot_race_end_marker_into_commit(tmp_path: Path) -> None:
    with serve_site("early-unknown-end") as site:
        with pytest.raises(ResponseShapeChanged, match="^response_shape_changed$"):
            runner.collect_from_page(
                site.url, BilibiliAdapter(), output_root=tmp_path, headless=True
            )

    assert not (tmp_path / "raw").exists()


def test_response_started_during_navigation_is_not_lost(tmp_path: Path) -> None:
    with serve_site("early") as site:
        result = runner.collect_from_page(
            site.url, BilibiliAdapter(), output_root=tmp_path, headless=True
        )

    assert [item.raw_comment_id for item in result.comments] == ["11", "12"]
    assert result.collection.pages_requested == result.collection.pages_succeeded == 1


def test_end_marker_stops_without_an_extra_response(tmp_path: Path) -> None:
    with serve_site("end-empty") as site:
        result = runner.collect_from_page(
            site.url, BilibiliAdapter(), output_root=tmp_path, headless=True
        )

    assert result.comments == []
    assert result.collection.pages_requested == result.collection.pages_succeeded == 0
    assert site.comment_requests == []


def test_page_timeout_does_not_commit_partial_artifacts(tmp_path: Path) -> None:
    with serve_site("stalled") as site:
        with pytest.raises(runner.BrowserSessionError, match="^page_collection_timeout$"):
            runner.collect_from_page(
                site.url, BilibiliAdapter(), output_root=tmp_path, headless=True
            )

    assert not (tmp_path / "raw").exists()


def test_page_driver_rejects_non_loopback_url_before_navigation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="^local_page_required$"):
        runner.collect_from_page(
            "http://example.com/index.html",
            BilibiliAdapter(),
            output_root=tmp_path,
            headless=True,
        )

    assert not (tmp_path / "raw").exists()


def test_local_request_policy_rejects_external_origin() -> None:
    assert not runner._local_request_allowed(
        "http://127.0.0.1:8123/index.html", "https://example.invalid/forbidden"
    )


def test_external_page_request_is_aborted_without_commit(tmp_path: Path) -> None:
    with serve_site("external-request") as site:
        with pytest.raises(runner.BrowserSessionError, match="^local_page_network_blocked$"):
            runner.collect_from_page(
                site.url, BilibiliAdapter(), output_root=tmp_path, headless=True
            )

    assert not (tmp_path / "raw").exists()


def test_cross_origin_websocket_is_blocked_before_reaching_local_server(tmp_path: Path) -> None:
    with serve_site("websocket") as site:
        with pytest.raises(runner.BrowserSessionError, match="^local_page_network_blocked$"):
            runner.collect_from_page(
                site.url, BilibiliAdapter(), output_root=tmp_path, headless=True
            )
        assert site.websocket_requests == []

    assert not (tmp_path / "raw").exists()


@pytest.mark.parametrize("video_id", ["/", "\\", ".", "..", ":", "CON"])
def test_unsafe_dom_video_id_is_rejected_before_filesystem_writes(
    tmp_path: Path, video_id: str
) -> None:
    with serve_site("end-empty") as site:
        url = f"{site.url}&video_id={quote(video_id, safe='')}"
        with pytest.raises(ResponseShapeChanged, match="^response_shape_changed$"):
            runner.collect_from_page(url, BilibiliAdapter(), output_root=tmp_path, headless=True)

    assert list(tmp_path.iterdir()) == []


def test_http_error_counts_requested_page_without_success(tmp_path: Path) -> None:
    with serve_site("http-500") as site:
        result = runner.collect_from_page(
            site.url, BilibiliAdapter(), output_root=tmp_path, headless=True
        )

    assert result.collection.pages_requested == 1
    assert result.collection.pages_succeeded == 0
    assert [error.category for error in result.collection.collection_errors].count(
        "page_request_failed"
    ) == 1


def test_local_server_is_reliably_closed() -> None:
    with serve_site() as site:
        host, port = site.server.server_address[:2]

    with socket.socket() as connection:
        connection.settimeout(0.2)
        assert connection.connect_ex((host, port)) != 0
