from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from requirementseeker_collector import runner
from requirementseeker_collector.adapters import (
    BilibiliAdapter,
    DouyinAdapter,
    ResponseShapeChanged,
)
from requirementseeker_collector.adapters.base import PlatformAdapter, ResponseKind
from requirementseeker_collector.artifacts import validate_generation
from requirementseeker_collector.browser import (
    BrowserLaunchConfig,
    BrowserSession,
    BrowserSessionError,
)


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
    if mode.startswith("paginated-douyin"):
        page = (
            page.replace(
                b'<button type="button" data-rs-stratum="top">Popular</button>',
                b'<button id="top" type="button" data-rs-stratum="top">'
                b"\xe6\x9c\x80\xe7\x83\xad</button>",
            )
            .replace(
                b'<button id="recent" type="button" data-rs-stratum="recent">Recent</button>',
                b'<button id="recent" type="button" data-rs-stratum="recent">'
                b"\xe6\x9c\x80\xe6\x96\xb0</button>",
            )
            .replace(
                b'<button type="button" data-rs-stratum="replies">Replies</button>',
                b'<button id="replies" type="button" data-rs-stratum="replies">'
                b"\xe5\xb1\x95\xe5\xbc\x801\xe6\x9d\xa1\xe5\x9b\x9e\xe5\xa4\x8d</button>",
            )
            .replace(
                b'window.addEventListener("scroll", requestComments);',
                b"""
              let paginationPage = 0;
              let paginationBusy = false;
              async function requestDouyinPage() {
                if (paginationBusy || paginationPage >= 6) return;
                paginationBusy = true;
                paginationPage += 1;
                try {
                  await fetch(`/aweme/v1/web/comment/list?page=${paginationPage}`);
                } finally {
                  paginationBusy = false;
                }
              }
              fetch("/aweme/v1/web/aweme/detail");
              document.querySelector("#top").addEventListener("click", requestDouyinPage);
              document.querySelector("#recent").addEventListener("click", requestDouyinPage);
              document.querySelector("#replies").addEventListener("click", requestDouyinPage);
              window.addEventListener("scroll", requestDouyinPage);
            """,
            )
        )
        pagination_limit = 10 if mode == "paginated-douyin-stalled" else 6
        page = page.replace(
            b"paginationPage >= 6", f"paginationPage >= {pagination_limit}".encode()
        )
    comment_payload = (
        Path(__file__).parents[1] / "fixtures" / "bilibili" / "comments.json"
    ).read_bytes()
    douyin_video_payload = (
        Path(__file__).parents[1] / "fixtures" / "douyin" / "video.json"
    ).read_bytes()
    paginated_video = json.loads(douyin_video_payload)
    paginated_video["aweme_detail"]["statistics"]["comment_count"] = 3
    paginated_video_payload = json.dumps(paginated_video).encode("utf-8")
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
            elif parsed.path == "/aweme/v1/web/aweme/detail":
                body = (
                    paginated_video_payload
                    if mode.startswith("paginated-douyin")
                    else douyin_video_payload
                )
                content_type = "application/json"
            elif parsed.path == "/aweme/v1/web/comment/list":
                if mode.startswith("paginated-douyin"):
                    page_number = parse_qs(parsed.query).get("page", [""])[0]
                    comment_requests.append(page_number)
                    if mode == "paginated-douyin-stalled":
                        identifiers = [21]
                    else:
                        identifiers = {
                            "1": [21],
                            "2": [21],
                            "3": [22],
                            "4": [22],
                            "5": [23] if mode == "paginated-douyin-slow" else [22],
                            "6": [23],
                        }.get(page_number, [])
                    payload: dict[str, object] = {
                        "status_code": 0,
                        "comments": [
                            {
                                "cid": str(identifier),
                                "user": {"uid": "viewer"},
                                "text": f"comment {identifier}",
                                "create_time": 1789000000 + identifier,
                                "digg_count": 0,
                                "reply_comment_total": 0,
                            }
                            for identifier in identifiers
                        ],
                        "has_more": (
                            0
                            if page_number == "6"
                            and mode not in {"paginated-douyin-slow", "paginated-douyin-stalled"}
                            else 1
                        ),
                        "cursor": int(page_number),
                    }
                    if mode == "paginated-douyin-slow" and page_number == "5":
                        time.sleep(1.0)
                    if mode == "paginated-douyin-late-invalid" and page_number == "6":
                        time.sleep(1.0)
                        payload = {"status_code": 0, "unknown": []}
                    body = json.dumps(payload).encode("utf-8")
                else:
                    body = b'{"status_code":0,"comments":[],"has_more":2,"cursor":0}'
                content_type = "application/json"
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


def test_installed_chrome_reuses_browser_managed_profile(
    local_site: LocalSite, tmp_path: Path
) -> None:
    config = BrowserLaunchConfig(browser="chrome", output_root=tmp_path, platform="bilibili")
    try:
        with BrowserSession(launch_config=config) as session:
            session.page.goto(local_site.url)
            session.page.evaluate("localStorage.setItem('synthetic-login', 'ready')")
        with BrowserSession(launch_config=config) as session:
            session.page.goto(local_site.url)
            marker = session.page.evaluate("localStorage.getItem('synthetic-login')")
    except BrowserSessionError as error:
        if str(error) == "browser_profile_unavailable":
            pytest.skip("installed Chrome channel unavailable")
        raise

    assert marker == "ready"


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


@pytest.mark.parametrize("mode", ["douyin-invalid", "douyin-delayed-invalid"])
def test_live_collector_surfaces_async_comment_shape_failure_without_commit_or_traceback(
    mode: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with serve_site(mode) as site:
        adapter = DouyinAdapter()

        class LocalResponseAdapter:
            def response_kind(self, url: str) -> ResponseKind | None:
                path = urlsplit(url).path
                return adapter.response_kind(f"https://www.douyin.com{path}")

        class LocalBrowserSession(BrowserSession):
            def open(self, url: str, received_adapter: object, consume: object) -> None:
                del url, received_adapter
                callback = cast(Callable[[str, object], None], consume)

                def forward(local_url: str, payload: object) -> None:
                    path = urlsplit(local_url).path
                    callback(f"https://www.douyin.com{path}", payload)

                super().open(site.url, cast(PlatformAdapter, LocalResponseAdapter()), forward)

        monkeypatch.setattr(runner, "BrowserSession", LocalBrowserSession)
        monkeypatch.setattr(runner, "perform_stratum_action", lambda page, stratum: "performed")
        result = runner.BrowserVideoCollector(
            supervisor=runner.CliSupervisionGate(
                read_status=lambda timeout: "ready", write_prompt=lambda prompt: None
            )
        ).collect_pilot(
            runner.PilotRequest(
                platform="douyin",
                url="https://www.douyin.com/video/7390000000000000000",
            ),
            tmp_path,
        )

    assert result.status == "response_shape_changed"
    assert not (tmp_path / "raw").exists()
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("mode", "expected_status"),
    [
        ("paginated-douyin", "success"),
        ("paginated-douyin-late-invalid", "response_shape_changed"),
    ],
)
def test_live_collector_uses_bounded_visible_pagination_on_local_chromium(
    mode: str,
    expected_status: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with serve_site(mode) as site:
        adapter = DouyinAdapter()

        class LocalResponseAdapter:
            def response_kind(self, url: str) -> ResponseKind | None:
                path = urlsplit(url).path
                return adapter.response_kind(f"https://www.douyin.com{path}")

        class LocalBrowserSession(BrowserSession):
            def open(self, url: str, received_adapter: object, consume: object) -> None:
                del url, received_adapter
                callback = cast(Callable[[str, object], None], consume)

                def forward(local_url: str, payload: object) -> None:
                    path = urlsplit(local_url).path
                    callback(f"https://www.douyin.com{path}", payload)

                super().open(site.url, cast(PlatformAdapter, LocalResponseAdapter()), forward)

        monkeypatch.setattr(runner, "BrowserSession", LocalBrowserSession)
        result = runner.BrowserVideoCollector(
            supervisor=runner.CliSupervisionGate(
                read_status=lambda timeout: "ready", write_prompt=lambda prompt: None
            )
        ).collect_pilot(
            runner.PilotRequest(
                platform="douyin",
                url="https://www.douyin.com/video/7390000000000000000",
            ),
            tmp_path,
        )

    assert result.status == expected_status
    if expected_status == "response_shape_changed":
        assert not (tmp_path / "raw").exists()
        return
    assert site.comment_requests == ["1", "2", "3", "4", "5", "6"]
    target = tmp_path / "raw" / "douyin" / "7390000000000000000"
    _, comments, collection = validate_generation(target)
    assert [comment.raw_comment_id for comment in comments] == ["21", "22", "23"]
    assert [comment.source_stratum for comment in comments] == ["top", "replies", "long_tail"]
    assert collection.pages_requested == collection.pages_succeeded == 6


def test_live_collector_waits_for_slow_page_and_stops_before_extra_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with serve_site("paginated-douyin-slow") as site:
        adapter = DouyinAdapter()

        class LocalResponseAdapter:
            def response_kind(self, url: str) -> ResponseKind | None:
                return adapter.response_kind(f"https://www.douyin.com{urlsplit(url).path}")

        class LocalBrowserSession(BrowserSession):
            def open(self, url: str, received_adapter: object, consume: object) -> None:
                del url, received_adapter
                callback = cast(Callable[[str, object], None], consume)

                def forward(local_url: str, payload: object) -> None:
                    callback(f"https://www.douyin.com{urlsplit(local_url).path}", payload)

                super().open(site.url, cast(PlatformAdapter, LocalResponseAdapter()), forward)

        monkeypatch.setattr(runner, "BrowserSession", LocalBrowserSession)
        result = runner.BrowserVideoCollector(
            supervisor=runner.CliSupervisionGate(
                read_status=lambda timeout: "ready", write_prompt=lambda prompt: None
            )
        ).collect_pilot(
            runner.PilotRequest(
                platform="douyin",
                url="https://www.douyin.com/video/7390000000000000000",
            ),
            tmp_path,
        )

    assert result.status == "success"
    assert site.comment_requests == ["1", "2", "3", "4", "5"]
    target = tmp_path / "raw" / "douyin" / "7390000000000000000"
    _, comments, collection = validate_generation(target)
    assert [comment.raw_comment_id for comment in comments] == ["21", "22", "23"]
    assert [comment.source_stratum for comment in comments] == ["top", "replies", "replies"]
    assert collection.pages_requested == collection.pages_succeeded == 5


def test_live_collector_stalls_after_three_no_progress_rounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with serve_site("paginated-douyin-stalled") as site:
        adapter = DouyinAdapter()

        class LocalResponseAdapter:
            def response_kind(self, url: str) -> ResponseKind | None:
                return adapter.response_kind(f"https://www.douyin.com{urlsplit(url).path}")

        class LocalBrowserSession(BrowserSession):
            def open(self, url: str, received_adapter: object, consume: object) -> None:
                del url, received_adapter
                callback = cast(Callable[[str, object], None], consume)

                def forward(local_url: str, payload: object) -> None:
                    callback(f"https://www.douyin.com{urlsplit(local_url).path}", payload)

                super().open(site.url, cast(PlatformAdapter, LocalResponseAdapter()), forward)

        monkeypatch.setattr(runner, "BrowserSession", LocalBrowserSession)
        result = runner.BrowserVideoCollector(
            supervisor=runner.CliSupervisionGate(
                read_status=lambda timeout: "ready", write_prompt=lambda prompt: None
            )
        ).collect_pilot(
            runner.PilotRequest(
                platform="douyin",
                url="https://www.douyin.com/video/7390000000000000000",
            ),
            tmp_path,
        )

    assert result.status == "partial"
    assert site.comment_requests == [str(number) for number in range(1, 11)]
    target = tmp_path / "raw" / "douyin" / "7390000000000000000"
    _, comments, collection = validate_generation(target)
    assert [comment.raw_comment_id for comment in comments] == ["21"]
    assert collection.pages_requested == collection.pages_succeeded == 10
    assert [error.category for error in collection.collection_errors] == ["pagination_stalled"]


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
