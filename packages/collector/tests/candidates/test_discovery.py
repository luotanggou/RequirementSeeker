import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from requirementseeker_collector.browser import BrowserLaunchConfig
from requirementseeker_collector.candidates.adapters import CandidateShapeChanged
from requirementseeker_collector.candidates.discovery import (
    BrowserCandidateSource,
    CandidatePage,
    DiscoveryRequest,
    discover,
)


def request(**overrides: Any) -> DiscoveryRequest:
    values: dict[str, Any] = {
        "platform": "bilibili",
        "direction": "software_tools",
        "query": "efficiency tools",
        "max_pages": 3,
        "max_results": 50,
    }
    values.update(overrides)
    return DiscoveryRequest.model_validate(values)


def bili_payload(*items: tuple[str, str, int]) -> dict[str, object]:
    return {
        "code": 0,
        "data": {
            "result": [
                {"bvid": video_key, "title": title, "review": count}
                for video_key, title, count in items
            ]
        },
    }


def douyin_payload(*items: tuple[str, str, int]) -> dict[str, object]:
    return {
        "status_code": 0,
        "data": [
            {
                "aweme_info": {
                    "aweme_id": video_key,
                    "desc": title,
                    "statistics": {"comment_count": count},
                }
            }
            for video_key, title, count in items
        ],
    }


@dataclass
class FakeBrowser:
    supplied_pages: list[CandidatePage]
    browser: str = "chromium"
    session_mode: str = "ephemeral"

    def pages(self, discovery_request: DiscoveryRequest) -> Iterable[CandidatePage]:
        del discovery_request
        yield from self.supplied_pages


class FakeLocator:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.rows = rows

    def count(self) -> int:
        return len(self.rows)

    def nth(self, index: int) -> "FakeLocatorRow":
        return FakeLocatorRow(self.rows[index])


class FakeLocatorRow:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get_attribute(self, name: str) -> str | None:
        return self.values.get(name)


class FakePage:
    def __init__(
        self,
        *,
        final_url: str,
        responses: list[tuple[str, Mapping[str, object]]] | None = None,
        delayed_responses: list[tuple[str, Mapping[str, object]]] | None = None,
        dom_rows: list[dict[str, str]] | None = None,
    ) -> None:
        self.url = "about:blank"
        self.final_url = final_url
        self.responses = responses or []
        self.delayed_responses = delayed_responses or []
        self.dom_rows = dom_rows or []
        self.response_listener: Any = None
        self.locator_calls: list[str] = []
        self.load_waited = False

    def on(self, event: str, listener: Any) -> None:
        assert event == "response"
        self.response_listener = listener

    def goto(self, url: str) -> None:
        del url
        self.url = self.final_url
        for response_url, payload in self.responses:
            self.response_listener(FakeResponse(response_url, payload))

    def wait_for_load_state(self, state: str, *, timeout: float) -> None:
        assert state == "networkidle"
        assert timeout > 0
        self.load_waited = True
        for response_url, payload in self.delayed_responses:
            self.response_listener(FakeResponse(response_url, payload))

    def wait_for_timeout(self, timeout: float) -> None:
        assert timeout > 0

    def remove_listener(self, event: str, listener: Any) -> None:
        assert event == "response"
        assert listener is self.response_listener

    def locator(self, selector: str) -> FakeLocator:
        self.locator_calls.append(selector)
        return FakeLocator(self.dom_rows)


class FakeResponse:
    def __init__(self, url: str, payload: Mapping[str, object]) -> None:
        self.url = url
        self.payload = payload

    def json(self) -> Mapping[str, object]:
        return self.payload


class FakeSession:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.bounded_wait_called = False
        self.open_called = False
        self.adapter: Any = None
        self.consume: Any = None

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def open(self, url: str, adapter: Any, consume: Any) -> None:
        del url
        self.open_called = True
        self.page.url = self.page.final_url
        self.adapter = adapter
        self.consume = consume
        for response_url, payload in self.page.responses:
            if adapter.response_kind(response_url) is not None:
                consume(response_url, payload)

    def wait_for_response_processing(self, **kwargs: float) -> None:
        assert kwargs["timeout_seconds"] > 0
        self.bounded_wait_called = True
        for response_url, payload in self.page.delayed_responses:
            if self.adapter.response_kind(response_url) is not None:
                self.consume(response_url, payload)


def page(number: int, *items: tuple[str, str, int]) -> CandidatePage:
    return CandidatePage(
        page_number=number,
        source_page=(f"https://search.bilibili.com/all?keyword=efficiency%20tools&page={number}"),
        payload=bili_payload(*items),
    )


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"query": "tools", "source_url": "https://www.bilibili.com/v/popular/all"},
    ],
)
def test_discovery_requires_exactly_one_query_or_source_url(values: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="exactly_one_discovery_source"):
        DiscoveryRequest.model_validate(
            {"platform": "bilibili", "direction": "software_tools", **values}
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_pages", 0),
        ("max_pages", 21),
        ("max_results", 0),
        ("max_results", 501),
        ("max_results", "5"),
    ],
)
def test_discovery_caps_are_bounded_strict_integers(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        request(**{field: value})


def test_discovery_source_url_must_match_platform() -> None:
    with pytest.raises(ValidationError, match="source_url_platform_mismatch"):
        request(query=None, source_url="https://www.douyin.com/search/tools")


def test_discovery_source_url_must_be_a_supported_candidate_page() -> None:
    with pytest.raises(ValidationError, match="source_url_not_candidate_page"):
        request(query=None, source_url="https://www.bilibili.com/account/login")


@pytest.mark.parametrize("query", ["first\nsecond", "first\rsecond", "x" * 201])
def test_discovery_query_is_bounded_single_line_text(query: str) -> None:
    with pytest.raises(ValidationError):
        request(query=query)


def test_discovery_deduplicates_and_preserves_first_rank(tmp_path: Path) -> None:
    browser = FakeBrowser(
        [
            page(1, ("BV1", "one", 1), ("BV2", "two", 2)),
            page(2, ("BV1", "duplicate", 3), ("BV3", "three", 4)),
        ]
    )

    result = discover(request(), browser=browser, output_root=tmp_path)

    assert [(item.video_key, item.source_rank) for item in result.candidates] == [
        ("BV1", 1),
        ("BV2", 2),
        ("BV3", 2),
    ]
    assert result.pages_processed == 2


def test_discovery_stops_at_page_and_result_caps(tmp_path: Path) -> None:
    pages = [
        page(
            number,
            *((f"BV{number}-{rank}", "title", rank) for rank in range(1, 4)),
        )
        for number in range(1, 5)
    ]
    browser = FakeBrowser(pages)

    result = discover(request(max_pages=2, max_results=4), browser=browser, output_root=tmp_path)

    assert [item.video_key for item in result.candidates] == [
        "BV1-1",
        "BV1-2",
        "BV1-3",
        "BV2-1",
    ]
    assert result.pages_processed == 2


def test_discovery_does_not_pull_a_page_beyond_the_page_cap(tmp_path: Path) -> None:
    pulled: list[int] = []

    @dataclass
    class TrackingBrowser:
        browser: str = "chromium"
        session_mode: str = "ephemeral"

        def pages(self, discovery_request: DiscoveryRequest) -> Iterable[CandidatePage]:
            for number in range(1, discovery_request.max_pages + 1):
                pulled.append(number)
                yield page(number, (f"BV{number}", "title", number))

    discover(request(max_pages=2), browser=TrackingBrowser(), output_root=tmp_path)

    assert pulled == [1, 2]


def test_discovery_counts_navigation_pages_when_one_page_has_multiple_payloads(
    tmp_path: Path,
) -> None:
    browser = FakeBrowser(
        [
            page(1, ("BV1", "one", 1)),
            page(1, ("BV2", "two", 2)),
            page(2, ("BV3", "three", 3)),
        ]
    )

    result = discover(request(max_pages=2), browser=browser, output_root=tmp_path)

    assert [item.video_key for item in result.candidates] == ["BV1", "BV2", "BV3"]
    assert result.pages_processed == 2


def test_discovery_writes_auditable_manifest_but_never_calls_batch(tmp_path: Path) -> None:
    batch = Mock()

    result = discover(
        request(),
        browser=FakeBrowser([page(1, ("BV1", "one", 120))]),
        output_root=tmp_path,
        batch_runner=batch,
    )

    batch.assert_not_called()
    assert result.manifest_path.parent.parent == tmp_path / "candidates"
    assert result.manifest_path.name == "manifest.json"
    assert result.discovery_path == result.manifest_path.with_name("discovery.json")
    manifest = json.loads(result.manifest_path.read_text(encoding="ascii"))
    report = json.loads(result.discovery_path.read_text(encoding="ascii"))
    assert manifest == {
        "candidates": [result.candidates[0].model_dump(mode="json")],
        "manifest_version": "1.0",
    }
    assert report == {
        "browser": "chromium",
        "candidate_count": 1,
        "direction": "software_tools",
        "discovery_version": "1.0",
        "max_pages": 3,
        "max_results": 50,
        "pages_processed": 1,
        "platform": "bilibili",
        "session_mode": "ephemeral",
        "source_kind": "query",
        "status": "success",
    }
    assert "approved" not in result.manifest_path.read_text(encoding="ascii").lower()


def test_unknown_shape_fails_closed_without_candidate_artifacts(tmp_path: Path) -> None:
    browser = FakeBrowser(
        [
            page(1, ("BV1", "one", 1)),
            CandidatePage(
                page_number=2,
                source_page=("https://search.bilibili.com/all?keyword=efficiency%20tools&page=2"),
                payload={"code": 0, "data": {"unknown": []}},
            ),
        ]
    )

    with pytest.raises(CandidateShapeChanged, match="^candidate_shape_changed$"):
        discover(request(), browser=browser, output_root=tmp_path)

    assert not (tmp_path / "candidates").exists()


def test_untrusted_page_source_fails_closed_without_candidate_artifacts(tmp_path: Path) -> None:
    browser = FakeBrowser(
        [
            CandidatePage(
                page_number=1,
                source_page="https://example.com/listing",
                payload=bili_payload(("BV1", "one", 1)),
            )
        ]
    )

    with pytest.raises(ValueError, match="candidate_source_page_mismatch"):
        discover(request(), browser=browser, output_root=tmp_path)

    assert not (tmp_path / "candidates").exists()


@pytest.mark.parametrize(
    "source_page",
    [
        "https://search.bilibili.com/all?keyword=other&page=1",
        "https://search.bilibili.com/all?keyword=efficiency%20tools&page=99",
        "https://search.bilibili.com/account/login?keyword=efficiency%20tools&page=1",
    ],
)
def test_injected_candidate_page_must_match_request_semantics(
    tmp_path: Path, source_page: str
) -> None:
    browser = FakeBrowser(
        [
            CandidatePage(
                page_number=1, source_page=source_page, payload=bili_payload(("BV1", "one", 1))
            )
        ]
    )

    with pytest.raises(ValueError, match="candidate_source_page_mismatch"):
        discover(request(max_pages=1), browser=browser, output_root=tmp_path)

    assert not (tmp_path / "candidates").exists()


def test_injected_candidate_page_must_match_explicit_source_url(tmp_path: Path) -> None:
    discovery_request = request(
        query=None,
        source_url="https://search.bilibili.com/all?keyword=efficiency%20tools&page=1",
        max_pages=1,
    )
    browser = FakeBrowser(
        [
            CandidatePage(
                page_number=1,
                source_page="https://search.bilibili.com/video?keyword=efficiency%20tools&page=1",
                payload=bili_payload(("BV1", "one", 1)),
            )
        ]
    )

    with pytest.raises(ValueError, match="candidate_source_page_mismatch"):
        discover(discovery_request, browser=browser, output_root=tmp_path)

    assert not (tmp_path / "candidates").exists()


def test_injected_douyin_candidate_page_accepts_matching_offset(tmp_path: Path) -> None:
    browser = FakeBrowser(
        [
            CandidatePage(
                page_number=2,
                source_page=("https://www.douyin.com/search/efficiency%20tools?offset=12&count=12"),
                payload=douyin_payload(("7390000000000000001", "one", 1)),
            )
        ]
    )

    result = discover(
        request(platform="douyin", max_pages=2),
        browser=browser,
        output_root=tmp_path,
    )

    assert [item.video_key for item in result.candidates] == ["7390000000000000001"]
    assert result.pages_processed == 1


def test_cross_site_redirect_fails_before_dom_fallback(tmp_path: Path) -> None:
    page = FakePage(
        final_url="https://example.com/fake-results",
        dom_rows=[
            {
                "data-video-key": "BV1",
                "data-title": "fake",
                "data-comment-count": "3",
            }
        ],
    )
    session = FakeSession(page)
    source = BrowserCandidateSource(BrowserLaunchConfig(), session=session)  # type: ignore[arg-type]

    with pytest.raises(CandidateShapeChanged, match="^candidate_shape_changed$"):
        discover(request(max_pages=1), browser=source, output_root=tmp_path)

    assert page.locator_calls == []
    assert not (tmp_path / "candidates").exists()


def test_same_site_login_redirect_fails_before_dom_fallback(tmp_path: Path) -> None:
    page = FakePage(
        final_url="https://search.bilibili.com/account/login",
        dom_rows=[
            {
                "data-video-key": "BV1",
                "data-title": "fake",
                "data-comment-count": "3",
            }
        ],
    )
    source = BrowserCandidateSource(
        BrowserLaunchConfig(),
        session=FakeSession(page),  # type: ignore[arg-type]
    )

    with pytest.raises(CandidateShapeChanged, match="^candidate_shape_changed$"):
        discover(request(max_pages=1), browser=source, output_root=tmp_path)

    assert page.locator_calls == []
    assert not (tmp_path / "candidates").exists()


@pytest.mark.parametrize(
    "response_url",
    [
        "https://api.bilibili.com/x/web-interface/search/type?keyword=other&page=1",
        "https://api.bilibili.com/x/web-interface/search/type?keyword=efficiency%20tools&page=99",
    ],
)
def test_unrelated_candidate_response_is_ignored_before_publishing(
    tmp_path: Path, response_url: str
) -> None:
    page = FakePage(
        final_url="https://search.bilibili.com/all?keyword=efficiency%20tools&page=1",
        responses=[(response_url, bili_payload(("BV1", "unrelated", 1)))],
    )
    source = BrowserCandidateSource(
        BrowserLaunchConfig(),
        session=FakeSession(page),  # type: ignore[arg-type]
    )

    with pytest.raises(CandidateShapeChanged, match="^candidate_shape_changed$"):
        discover(request(max_pages=1), browser=source, output_root=tmp_path)

    assert not (tmp_path / "candidates").exists()


def test_douyin_candidate_response_must_match_query_and_page_offset(tmp_path: Path) -> None:
    page = FakePage(
        final_url="https://www.douyin.com/search/efficiency%20tools",
        responses=[
            (
                "https://www.douyin.com/aweme/v1/web/general/search/single/"
                "?keyword=efficiency%20tools&offset=0&count=12",
                douyin_payload(("7390000000000000001", "one", 1)),
            ),
            (
                "https://www.douyin.com/aweme/v1/web/general/search/single/"
                "?keyword=efficiency%20tools&offset=12&count=12",
                douyin_payload(("7390000000000000099", "wrong page", 1)),
            ),
        ],
    )
    source = BrowserCandidateSource(
        BrowserLaunchConfig(),
        session=FakeSession(page),  # type: ignore[arg-type]
    )

    result = discover(
        request(platform="douyin", max_pages=1),
        browser=source,
        output_root=tmp_path,
    )

    assert [item.video_key for item in result.candidates] == ["7390000000000000001"]


def test_empty_page_fails_closed_before_publishing(tmp_path: Path) -> None:
    page = FakePage(final_url="https://search.bilibili.com/all?keyword=efficiency%20tools&page=1")
    source = BrowserCandidateSource(
        BrowserLaunchConfig(),
        session=FakeSession(page),  # type: ignore[arg-type]
    )

    with pytest.raises(CandidateShapeChanged, match="^candidate_shape_changed$"):
        discover(request(max_pages=1), browser=source, output_root=tmp_path)

    assert not (tmp_path / "candidates").exists()


def test_delayed_candidate_response_is_collected_during_bounded_wait(tmp_path: Path) -> None:
    page = FakePage(
        final_url="https://search.bilibili.com/all?keyword=efficiency%20tools&page=1",
        delayed_responses=[
            (
                "https://api.bilibili.com/x/web-interface/search/type"
                "?keyword=efficiency%20tools&page=1",
                bili_payload(("BV1", "one", 1)),
            )
        ],
    )
    session = FakeSession(page)
    source = BrowserCandidateSource(BrowserLaunchConfig(), session=session)  # type: ignore[arg-type]

    result = discover(request(max_pages=1), browser=source, output_root=tmp_path)

    assert [item.video_key for item in result.candidates] == ["BV1"]
    assert session.open_called is True
    assert session.bounded_wait_called is True


def test_response_arriving_during_session_quiet_wait_is_collected(tmp_path: Path) -> None:
    page = FakePage(final_url="https://search.bilibili.com/all?keyword=efficiency%20tools&page=1")

    class QuietWindowSession(FakeSession):
        def __init__(self, browser_page: FakePage) -> None:
            super().__init__(browser_page)
            self.consume: Any = None
            self.adapter: Any = None

        def open(self, url: str, adapter: Any, consume: Any) -> None:
            del url
            self.page.url = self.page.final_url
            self.adapter = adapter
            self.consume = consume

        def wait_for_response_processing(self, **kwargs: float) -> None:
            super().wait_for_response_processing(**kwargs)
            response_url = (
                "https://api.bilibili.com/x/web-interface/search/type"
                "?keyword=efficiency%20tools&page=1"
            )
            assert self.adapter.response_kind(response_url) is not None
            self.consume(response_url, bili_payload(("BV1", "one", 1)))

    session = QuietWindowSession(page)
    source = BrowserCandidateSource(BrowserLaunchConfig(), session=session)  # type: ignore[arg-type]

    result = discover(request(max_pages=1), browser=source, output_root=tmp_path)

    assert [item.video_key for item in result.candidates] == ["BV1"]
    assert session.bounded_wait_called is True


@pytest.mark.parametrize("use_response", [True, False])
def test_redirect_during_quiet_wait_fails_before_response_or_dom_publish(
    tmp_path: Path, use_response: bool
) -> None:
    response_url = (
        "https://api.bilibili.com/x/web-interface/search/type?keyword=efficiency%20tools&page=1"
    )
    page = FakePage(
        final_url="https://search.bilibili.com/all?keyword=efficiency%20tools&page=1",
        responses=[(response_url, bili_payload(("BV1", "one", 1)))] if use_response else [],
        dom_rows=[]
        if use_response
        else [
            {
                "data-video-key": "BV1",
                "data-title": "one",
                "data-comment-count": "1",
            }
        ],
    )

    class RedirectDuringWaitSession(FakeSession):
        def wait_for_response_processing(self, **kwargs: float) -> None:
            super().wait_for_response_processing(**kwargs)
            self.page.url = "https://search.bilibili.com/account/login"

    source = BrowserCandidateSource(
        BrowserLaunchConfig(),
        session=RedirectDuringWaitSession(page),  # type: ignore[arg-type]
    )

    with pytest.raises(CandidateShapeChanged, match="^candidate_shape_changed$"):
        discover(request(max_pages=1), browser=source, output_root=tmp_path)

    assert page.locator_calls == []
    assert not (tmp_path / "candidates").exists()


def test_late_response_from_previous_page_cannot_pollute_next_page(tmp_path: Path) -> None:
    page = FakePage(final_url="https://search.bilibili.com/all?keyword=efficiency%20tools&page=1")

    class CrossPageSession(FakeSession):
        def __init__(self, browser_page: FakePage) -> None:
            super().__init__(browser_page)
            self.open_count = 0

        def open(self, url: str, adapter: Any, consume: Any) -> None:
            self.open_count += 1
            self.page.url = url
            current_url = (
                "https://api.bilibili.com/x/web-interface/search/type"
                f"?keyword=efficiency%20tools&page={self.open_count}"
            )
            if self.open_count == 2:
                late_url = (
                    "https://api.bilibili.com/x/web-interface/search/type"
                    "?keyword=efficiency%20tools&page=1"
                )
                assert adapter.response_kind(late_url) is None
            assert adapter.response_kind(current_url) is not None
            consume(current_url, bili_payload((f"BV{self.open_count}", "current", 1)))

    session = CrossPageSession(page)
    source = BrowserCandidateSource(BrowserLaunchConfig(), session=session)  # type: ignore[arg-type]

    result = discover(request(max_pages=2), browser=source, output_root=tmp_path)

    assert [item.video_key for item in result.candidates] == ["BV1", "BV2"]
    assert result.pages_processed == 2


def test_discovery_rejects_comment_payload_without_publishing(tmp_path: Path) -> None:
    payload: Mapping[str, object] = {"comments": [{"text": "must not be read"}]}
    browser = FakeBrowser(
        [
            CandidatePage(
                page_number=1,
                source_page=("https://search.bilibili.com/all?keyword=efficiency%20tools&page=1"),
                payload=payload,
            )
        ]
    )

    with pytest.raises(CandidateShapeChanged):
        discover(request(), browser=browser, output_root=tmp_path)

    assert not (tmp_path / "candidates").exists()


def test_discovery_rejects_redirected_candidate_output_root(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    outside = tmp_path / "outside"
    outside.mkdir()
    output_root.mkdir()
    try:
        (output_root / "candidates").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory links are unavailable")

    with pytest.raises(ValueError, match="unsafe_discovery_output_path"):
        discover(
            request(), browser=FakeBrowser([page(1, ("BV1", "one", 1))]), output_root=output_root
        )

    assert list(outside.iterdir()) == []


def test_discovery_accepts_existing_valid_dedicated_launch_config(tmp_path: Path) -> None:
    config = BrowserLaunchConfig(browser="chrome", output_root=tmp_path, platform="bilibili")

    result = discover(
        request(),
        browser=FakeBrowser([page(1, ("BV1", "one", 1))], "chrome", "dedicated"),
        launch_config=config,
        output_root=tmp_path,
    )

    report = json.loads(result.discovery_path.read_text(encoding="ascii"))
    assert report["browser"] == "chrome"
    assert report["session_mode"] == "dedicated"


def test_discovery_rejects_launch_config_for_another_platform(tmp_path: Path) -> None:
    config = BrowserLaunchConfig(browser="chrome", output_root=tmp_path, platform="douyin")

    with pytest.raises(ValueError, match="launch_config_request_mismatch"):
        discover(
            request(),
            browser=FakeBrowser([], "chrome", "dedicated"),
            launch_config=config,
            output_root=tmp_path,
        )

    assert not (tmp_path / "candidates").exists()
