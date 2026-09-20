import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

import requirementseeker_collector.browser as browser_module
import requirementseeker_collector.candidates.discovery as discovery_module
from requirementseeker_collector.adapters.base import ResponseShapeChanged
from requirementseeker_collector.browser import (
    BrowserLaunchConfig,
    BrowserSession,
    BrowserSessionError,
)
from requirementseeker_collector.candidates.adapters import CandidateShapeChanged
from requirementseeker_collector.candidates.discovery import (
    BrowserCandidateSource,
    CandidatePage,
    DiscoveryRequest,
    _CandidateResponseAdapter,
    _capture_candidate_payloads,
    _dom_payload,
    _search_url,
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


class UnreadableCommentPayload(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        raise AssertionError(f"comment body was read through key {key!r}")

    def __iter__(self) -> Iterator[str]:
        return iter(("comments",))

    def __len__(self) -> int:
        return 1


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
        if selector == (
            "[data-candidate-results] [data-candidate-video][data-video-key][data-title]"
        ):
            return FakeLocator(self.dom_rows)
        return FakeLocator([])


class FakeResponse:
    def __init__(self, url: str, payload: Mapping[str, object]) -> None:
        self.url = url
        self.payload = payload

    def json(self) -> Mapping[str, object]:
        return self.payload


class ElementLocator:
    def __init__(self, elements: list[dict[str, Any]]) -> None:
        self.elements = elements

    def count(self) -> int:
        return len(self.elements)

    def nth(self, index: int) -> "ElementLocator":
        return ElementLocator([self.elements[index]])

    def locator(self, selector: str) -> "ElementLocator":
        element = self.elements[0]
        return ElementLocator(list(element.get("children", {}).get(selector, [])))

    def get_attribute(self, name: str) -> str | None:
        return self.elements[0].get("attributes", {}).get(name)

    def text_content(self) -> str | None:
        return self.elements[0].get("text")


class BilibiliDomPage:
    def __init__(
        self,
        *,
        cards: list[dict[str, Any]] | None = None,
        wraps: list[dict[str, Any]] | None = None,
        footer_links: list[dict[str, Any]] | None = None,
    ) -> None:
        self.cards = cards or []
        self.wraps = wraps or []
        self.footer_links = footer_links or []
        self.locator_calls: list[str] = []

    def locator(self, selector: str) -> ElementLocator:
        self.locator_calls.append(selector)
        if selector == ".bili-video-card":
            return ElementLocator(self.cards)
        if selector == ".bili-video-card__wrap":
            return ElementLocator(self.wraps)
        if selector == "a[href]":
            return ElementLocator(self.footer_links)
        return ElementLocator([])


def element(
    *,
    href: str | None = None,
    title: str | None = None,
    text: str | None = None,
    children: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    attributes = {}
    if href is not None:
        attributes["href"] = href
    if title is not None:
        attributes["title"] = title
    return {"attributes": attributes, "text": text, "children": children or {}}


def bili_card(
    links: list[dict[str, Any]], *, title: str | None, text: str | None = None
) -> dict[str, Any]:
    headings = [] if title is None and text is None else [element(title=title, text=text)]
    return element(children={"a[href]": links, "h3": headings})


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

    def raise_if_response_failed(self) -> None:
        pass


def page(number: int, *items: tuple[str, str, int]) -> CandidatePage:
    return CandidatePage(
        page_number=number,
        source_page=(f"https://search.bilibili.com/video?keyword=efficiency%20tools&page={number}"),
        payload=bili_payload(*items),
    )


def test_bilibili_query_uses_video_search_pages() -> None:
    discovery_request = request(max_pages=2)

    assert _search_url(discovery_request, 1) == (
        "https://search.bilibili.com/video?keyword=efficiency%20tools&page=1"
    )
    assert _search_url(discovery_request, 2) == (
        "https://search.bilibili.com/video?keyword=efficiency%20tools&page=2"
    )


def test_douyin_query_uses_audited_general_search_pages() -> None:
    discovery_request = request(platform="douyin", max_pages=2)

    assert _search_url(discovery_request, 1) == (
        "https://www.douyin.com/search/efficiency%20tools?type=general&page=1"
    )
    assert _search_url(discovery_request, 2) == (
        "https://www.douyin.com/search/efficiency%20tools?type=general&page=2"
    )


@pytest.mark.parametrize(
    ("page_number", "accepted_offset", "rejected_offsets"),
    [
        (1, 10, [0, 20]),
        (2, 20, [10]),
        (3, 30, [20]),
    ],
)
def test_douyin_single_response_uses_one_based_offset_batches(
    page_number: int, accepted_offset: int, rejected_offsets: list[int]
) -> None:
    adapter = _CandidateResponseAdapter("douyin", "efficiency tools", page_number)

    def response_url(offset: int) -> str:
        return (
            "https://www.douyin.com/aweme/v1/web/general/search/single/"
            f"?keyword=efficiency%20tools&count=10&offset={offset}"
        )

    assert adapter.response_kind(response_url(accepted_offset)) == "comments"
    assert adapter.matches_current_page(response_url(accepted_offset)) is True
    for offset in rejected_offsets:
        expected_kind = None if offset == 0 else "comments"
        assert adapter.response_kind(response_url(offset)) == expected_kind
        assert adapter.matches_current_page(response_url(offset)) is False


@pytest.mark.parametrize(
    "response_url",
    [
        (
            "https://www.douyin.com/aweme/v1/web/general/search/stream/"
            "?keyword=efficiency%20tools&count=10&offset=10"
        ),
        (
            "https://www.douyin.com/aweme/v1/web/general/search/single/"
            "?keyword=other&count=10&offset=10"
        ),
        (
            "https://www.douyin.com/aweme/v1/web/general/search/single/"
            "?keyword=efficiency%20tools&keyword=other&count=10&offset=10"
        ),
        (
            "https://www.douyin.com/aweme/v1/web/general/search/single/"
            "?keyword=efficiency%20tools&count=10&count=10&offset=10"
        ),
        (
            "https://www.douyin.com/aweme/v1/web/general/search/single/"
            "?keyword=efficiency%20tools&count=10&offset=10&offset=10"
        ),
    ],
)
def test_douyin_single_response_keeps_strict_endpoint_and_query_bounds(
    response_url: str,
) -> None:
    adapter = _CandidateResponseAdapter("douyin", "efficiency tools", 1)

    assert adapter.response_kind(response_url) is None


def test_bilibili_dom_reads_only_trusted_cards_in_page_order() -> None:
    first_link = element(href="https://www.bilibili.com/video/BV1xx411c7mD?p=1")
    page = BilibiliDomPage(
        cards=[
            bili_card(
                [first_link, element(href="//www.bilibili.com/video/BV1xx411c7mD")],
                title="First title",
            ),
            bili_card(
                [element(href="https://www.bilibili.com/video/BV1Q541167Qg")],
                title=None,
                text="  Second title  ",
            ),
        ],
        footer_links=[element(href="https://www.bilibili.com/video/BV1ab411c7mE")],
    )

    payload = _dom_payload(page, "bilibili")  # type: ignore[arg-type]

    assert payload == {
        "code": 0,
        "data": {
            "result": [
                {"bvid": "BV1xx411c7mD", "title": "First title", "review": None},
                {"bvid": "BV1Q541167Qg", "title": "Second title", "review": None},
            ]
        },
    }
    assert "a[href]" not in page.locator_calls


def test_bilibili_dom_accepts_wrap_as_trusted_card_container() -> None:
    page = BilibiliDomPage(
        wraps=[
            bili_card(
                [element(href="https://www.bilibili.com/video/BV1xx411c7mD")],
                title="Wrapped title",
            )
        ]
    )

    payload = _dom_payload(page, "bilibili")  # type: ignore[arg-type]

    assert payload == {
        "code": 0,
        "data": {"result": [{"bvid": "BV1xx411c7mD", "title": "Wrapped title", "review": None}]},
    }


def test_bilibili_dom_excludes_malformed_cards_when_valid_card_exists() -> None:
    page = BilibiliDomPage(
        cards=[
            bili_card(
                [element(href="https://www.bilibili.com/video/not-a-bvid")],
                title="invalid id",
            ),
            bili_card(
                [element(href="https://example.com/video/BV1xx411c7mD")],
                title="foreign",
            ),
            bili_card(
                [element(href="https://www.bilibili.com/video/BV1xx411c7mD")],
                title=None,
            ),
            bili_card(
                [element(href="https://www.bilibili.com/video/BV1Q541167Qg")],
                title="valid",
            ),
        ]
    )

    payload = _dom_payload(page, "bilibili")  # type: ignore[arg-type]

    assert payload == {
        "code": 0,
        "data": {"result": [{"bvid": "BV1Q541167Qg", "title": "valid", "review": None}]},
    }


@pytest.mark.parametrize(
    "ambiguous_card",
    [
        element(
            children={
                "a[href]": [
                    element(href="https://www.bilibili.com/video/BV1xx411c7mD"),
                    element(href="https://www.bilibili.com/video/BV1Q541167Qg"),
                ],
                "h3": [element(title="one title")],
            }
        ),
        element(
            children={
                "a[href]": [element(href="https://www.bilibili.com/video/BV1xx411c7mD")],
                "h3": [element(title="first title"), element(text="second title")],
            }
        ),
    ],
)
def test_bilibili_dom_rejects_ambiguous_card(ambiguous_card: dict[str, Any]) -> None:
    page = BilibiliDomPage(cards=[ambiguous_card])

    assert _dom_payload(page, "bilibili") is None  # type: ignore[arg-type]


def test_bilibili_dom_accepts_duplicate_same_bv_and_title() -> None:
    page = BilibiliDomPage(
        cards=[
            element(
                children={
                    "a[href]": [
                        element(href="https://www.bilibili.com/video/BV1xx411c7mD"),
                        element(href="//www.bilibili.com/video/BV1xx411c7mD?p=1"),
                    ],
                    "h3": [element(title="same title"), element(text=" same title ")],
                }
            )
        ]
    )

    assert _dom_payload(page, "bilibili") == {  # type: ignore[arg-type]
        "code": 0,
        "data": {"result": [{"bvid": "BV1xx411c7mD", "title": "same title", "review": None}]},
    }


def test_bilibili_ambiguous_card_fails_before_publishing(tmp_path: Path) -> None:
    card = element(
        children={
            "a[href]": [
                element(href="https://www.bilibili.com/video/BV1xx411c7mD"),
                element(href="https://www.bilibili.com/video/BV1Q541167Qg"),
            ],
            "h3": [element(title="ambiguous")],
        }
    )

    class AmbiguousPage(FakePage):
        def locator(self, selector: str) -> Any:
            self.locator_calls.append(selector)
            if selector == ".bili-video-card":
                return ElementLocator([card])
            return ElementLocator([])

    page = AmbiguousPage(
        final_url="https://search.bilibili.com/video?keyword=efficiency%20tools&page=1"
    )
    source = BrowserCandidateSource(
        BrowserLaunchConfig(),
        session=FakeSession(page),  # type: ignore[arg-type]
    )

    with pytest.raises(CandidateShapeChanged, match="^candidate_shape_changed$"):
        discover(request(max_pages=1), browser=source, output_root=tmp_path)

    assert not (tmp_path / "candidates").exists()


@pytest.mark.parametrize(
    "page",
    [
        BilibiliDomPage(),
        BilibiliDomPage(
            cards=[
                bili_card(
                    [element(href="https://www.bilibili.com/video/not-a-bvid")],
                    title="unknown structure",
                )
            ]
        ),
    ],
)
def test_bilibili_dom_without_legal_candidate_fails_closed(page: BilibiliDomPage) -> None:
    assert _dom_payload(page, "bilibili") is None  # type: ignore[arg-type]


def test_bilibili_dom_rejects_generic_candidate_rows() -> None:
    page = FakePage(
        final_url="https://search.bilibili.com/video?keyword=efficiency%20tools&page=1",
        dom_rows=[
            {
                "data-video-key": "BV1xx411c7mD",
                "data-title": "generic row",
                "data-comment-count": "3",
            }
        ],
    )

    assert _dom_payload(page, "bilibili") is None  # type: ignore[arg-type]


def test_bilibili_generic_candidate_rows_fail_before_publishing(tmp_path: Path) -> None:
    page = FakePage(
        final_url="https://search.bilibili.com/video?keyword=efficiency%20tools&page=1",
        dom_rows=[
            {
                "data-video-key": "BV1xx411c7mD",
                "data-title": "generic row",
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

    assert not (tmp_path / "candidates").exists()


def test_douyin_dom_keeps_generic_candidate_row_fallback() -> None:
    page = FakePage(
        final_url="https://www.douyin.com/search/efficiency%20tools",
        dom_rows=[
            {
                "data-video-key": "7390000000000000001",
                "data-title": "generic row",
                "data-comment-count": "7",
            }
        ],
    )

    assert _dom_payload(page, "douyin") == {  # type: ignore[arg-type]
        "status_code": 0,
        "data": [
            {
                "aweme_info": {
                    "aweme_id": "7390000000000000001",
                    "desc": "generic row",
                    "statistics": {"comment_count": 7},
                }
            }
        ],
    }


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


def test_discovery_source_url_rejects_duplicate_page_parameters() -> None:
    with pytest.raises(ValidationError, match="duplicate_source_page_parameter"):
        request(
            query=None,
            source_url=("https://search.bilibili.com/all?keyword=efficiency%20tools&page=1&page=2"),
        )


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


def test_discovery_rejects_empty_douyin_api_result_before_publishing(tmp_path: Path) -> None:
    browser = FakeBrowser(
        [
            CandidatePage(
                page_number=1,
                source_page=(
                    "https://www.douyin.com/search/efficiency%20tools?type=general&page=1"
                ),
                payload=douyin_payload(),
            )
        ]
    )

    with pytest.raises(CandidateShapeChanged, match="^candidate_shape_changed$"):
        discover(
            request(platform="douyin", max_pages=1),
            browser=browser,
            output_root=tmp_path,
        )

    assert not (tmp_path / "candidates").exists()
    assert list(tmp_path.rglob("manifest.json")) == []
    assert list(tmp_path.rglob("discovery.json")) == []


def test_discovery_rejects_all_recognized_skipped_results_before_publishing(
    tmp_path: Path,
) -> None:
    payload = douyin_payload()
    payload["data"] = [
        {
            "type": 6,
            "doc_type": 108,
            "card_type": 6,
            "card_unique_name": "related_word",
        },
        {
            "type": 77,
            "doc_type": 305,
            "card_type": 0,
            "card_unique_name": "toutiao_article",
            "common_aladdin": {},
        },
        {
            "aweme_info": {
                "aweme_id": "7390000000000000099",
                "desc": None,
                "statistics": None,
            }
        },
    ]
    browser = FakeBrowser(
        [
            CandidatePage(
                page_number=1,
                source_page=(
                    "https://www.douyin.com/search/efficiency%20tools?type=general&page=1"
                ),
                payload=payload,
            )
        ]
    )

    with pytest.raises(CandidateShapeChanged, match="^candidate_shape_changed$"):
        discover(
            request(platform="douyin", max_pages=1),
            browser=browser,
            output_root=tmp_path,
        )

    assert not (tmp_path / "candidates").exists()
    assert list(tmp_path.rglob("manifest.json")) == []
    assert list(tmp_path.rglob("discovery.json")) == []


def test_discovery_allows_empty_intermediate_result_when_later_page_has_video(
    tmp_path: Path,
) -> None:
    browser = FakeBrowser(
        [
            CandidatePage(
                page_number=1,
                source_page=(
                    "https://www.douyin.com/search/efficiency%20tools?type=general&page=1"
                ),
                payload=douyin_payload(),
            ),
            CandidatePage(
                page_number=2,
                source_page=(
                    "https://www.douyin.com/search/efficiency%20tools?type=general&page=2"
                ),
                payload=douyin_payload(("7390000000000000001", "one", 1)),
            ),
        ]
    )

    result = discover(
        request(platform="douyin", max_pages=2),
        browser=browser,
        output_root=tmp_path,
    )

    assert [item.video_key for item in result.candidates] == ["7390000000000000001"]
    assert result.pages_processed == 2
    assert result.manifest_path.is_file()
    assert result.discovery_path.is_file()


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
                source_page=("https://search.bilibili.com/video?keyword=efficiency%20tools&page=2"),
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
        "https://search.bilibili.com/video?keyword=other&page=1",
        "https://search.bilibili.com/video?keyword=efficiency%20tools&page=99",
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


def test_injected_douyin_candidate_page_accepts_matching_audited_page(tmp_path: Path) -> None:
    browser = FakeBrowser(
        [
            CandidatePage(
                page_number=2,
                source_page=(
                    "https://www.douyin.com/search/efficiency%20tools?type=general&page=2"
                ),
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


def test_injected_douyin_candidate_page_cannot_claim_later_page_without_audit_parameter(
    tmp_path: Path,
) -> None:
    browser = FakeBrowser(
        [
            CandidatePage(
                page_number=2,
                source_page=("https://www.douyin.com/search/efficiency%20tools?type=general"),
                payload=douyin_payload(("7390000000000000001", "one", 1)),
            )
        ]
    )

    with pytest.raises(ValueError, match="^candidate_source_page_mismatch$"):
        discover(
            request(platform="douyin", max_pages=2),
            browser=browser,
            output_root=tmp_path,
        )

    assert not (tmp_path / "candidates").exists()


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


def test_bilibili_first_page_accepts_navigation_without_explicit_page_one(
    tmp_path: Path,
) -> None:
    page = FakePage(
        final_url="https://search.bilibili.com/video?keyword=efficiency%20tools",
        responses=[
            (
                "https://api.bilibili.com/x/web-interface/search/type"
                "?keyword=efficiency%20tools&page=1",
                bili_payload(("BV1", "one", 1)),
            )
        ],
    )
    source = BrowserCandidateSource(
        BrowserLaunchConfig(),
        session=FakeSession(page),  # type: ignore[arg-type]
    )

    result = discover(request(max_pages=1), browser=source, output_root=tmp_path)

    assert [item.video_key for item in result.candidates] == ["BV1"]


@pytest.mark.parametrize(
    ("source_url", "final_url"),
    [
        (
            "https://search.bilibili.com/all?keyword=efficiency%20tools&page=1",
            "https://search.bilibili.com/all?keyword=efficiency%20tools",
        ),
        (
            "https://www.bilibili.com/v/popular/all?page=1",
            "https://www.bilibili.com/v/popular/all",
        ),
        (
            "https://search.bilibili.com/all?keyword=efficiency%20tools&page=1",
            "https://search.bilibili.com/all?keyword=efficiency%20tools&page=1&order=click",
        ),
    ],
)
def test_bilibili_explicit_source_url_keeps_exact_query(
    tmp_path: Path, source_url: str, final_url: str
) -> None:
    page = FakePage(
        final_url=final_url,
        dom_rows=[
            {
                "data-video-key": "BV1",
                "data-title": "one",
                "data-comment-count": "1",
            }
        ],
    )
    source = BrowserCandidateSource(
        BrowserLaunchConfig(),
        session=FakeSession(page),  # type: ignore[arg-type]
    )

    with pytest.raises(CandidateShapeChanged, match="^candidate_shape_changed$"):
        discover(
            request(query=None, source_url=source_url, max_pages=1),
            browser=source,
            output_root=tmp_path,
        )

    assert page.locator_calls == []
    assert not (tmp_path / "candidates").exists()


def test_bilibili_explicit_source_url_preserves_query_pairs_when_paging(
    tmp_path: Path,
) -> None:
    source_url = (
        "https://search.bilibili.com/all?keyword=efficiency%20tools&tag=a&tag=b&empty=&page=1"
    )
    page = FakePage(final_url="about:blank")

    class RecordingSession(FakeSession):
        def __init__(self, browser_page: FakePage) -> None:
            super().__init__(browser_page)
            self.opened_urls: list[str] = []

        def open(self, url: str, adapter: Any, consume: Any) -> None:
            self.opened_urls.append(url)
            self.page.url = url
            page_number = len(self.opened_urls)
            response_url = (
                "https://api.bilibili.com/x/web-interface/search/type"
                f"?keyword=efficiency%20tools&page={page_number}"
            )
            assert adapter.response_kind(response_url) is not None
            consume(response_url, bili_payload((f"BV{page_number}", "one", 1)))

    session = RecordingSession(page)
    source = BrowserCandidateSource(
        BrowserLaunchConfig(),
        session=session,  # type: ignore[arg-type]
    )

    result = discover(
        request(query=None, source_url=source_url, max_pages=2),
        browser=source,
        output_root=tmp_path,
    )

    assert session.opened_urls == [
        source_url,
        source_url.removesuffix("page=1") + "page=2",
    ]
    assert [str(candidate.source_page) for candidate in result.candidates] == session.opened_urls


@pytest.mark.parametrize(
    "final_url",
    [
        "https://search.bilibili.com/all",
        "https://search.bilibili.com/all?keyword=other",
        "https://search.bilibili.com/account/login?keyword=efficiency%20tools",
    ],
)
def test_bilibili_first_page_rejects_other_navigation_changes(
    tmp_path: Path, final_url: str
) -> None:
    page = FakePage(
        final_url=final_url,
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
    "second_page_url",
    [
        "https://search.bilibili.com/video?keyword=efficiency%20tools",
        "https://search.bilibili.com/video?keyword=efficiency%20tools&page=3",
    ],
)
def test_bilibili_later_page_requires_exact_page_number(
    tmp_path: Path, second_page_url: str
) -> None:
    page = FakePage(final_url="about:blank")

    class SequencedSession(FakeSession):
        def __init__(self, browser_page: FakePage) -> None:
            super().__init__(browser_page)
            self.open_count = 0

        def open(self, url: str, adapter: Any, consume: Any) -> None:
            del url
            self.open_count += 1
            self.page.url = (
                "https://search.bilibili.com/video?keyword=efficiency%20tools"
                if self.open_count == 1
                else second_page_url
            )
            response_url = (
                "https://api.bilibili.com/x/web-interface/search/type"
                f"?keyword=efficiency%20tools&page={self.open_count}"
            )
            assert adapter.response_kind(response_url) is not None
            consume(response_url, bili_payload((f"BV{self.open_count}", "one", 1)))

    source = BrowserCandidateSource(
        BrowserLaunchConfig(),
        session=SequencedSession(page),  # type: ignore[arg-type]
    )

    with pytest.raises(CandidateShapeChanged, match="^candidate_shape_changed$"):
        discover(request(max_pages=2), browser=source, output_root=tmp_path)

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
        final_url="https://search.bilibili.com/video?keyword=efficiency%20tools&page=1",
        responses=[(response_url, bili_payload(("BV1", "unrelated", 1)))],
    )
    source = BrowserCandidateSource(
        BrowserLaunchConfig(),
        session=FakeSession(page),  # type: ignore[arg-type]
    )

    with pytest.raises(CandidateShapeChanged, match="^candidate_shape_changed$"):
        discover(request(max_pages=1), browser=source, output_root=tmp_path)

    assert not (tmp_path / "candidates").exists()


def test_douyin_candidate_response_must_match_first_observed_batch(tmp_path: Path) -> None:
    page = FakePage(
        final_url="https://www.douyin.com/search/efficiency%20tools?type=general",
        responses=[
            (
                "https://www.douyin.com/aweme/v1/web/general/search/single/"
                "?keyword=efficiency%20tools&offset=0&count=10",
                douyin_payload(("7390000000000000098", "zero offset", 1)),
            ),
            (
                "https://www.douyin.com/aweme/v1/web/general/search/single/"
                "?keyword=efficiency%20tools&offset=10&count=10",
                douyin_payload(("7390000000000000001", "one", 1)),
            ),
            (
                "https://www.douyin.com/aweme/v1/web/general/search/single/"
                "?keyword=efficiency%20tools&offset=20&count=10",
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


def test_douyin_query_paging_keeps_audited_urls_after_canonical_navigation(
    tmp_path: Path,
) -> None:
    page = FakePage(final_url="about:blank")

    class SequencedSession(FakeSession):
        def __init__(self, browser_page: FakePage) -> None:
            super().__init__(browser_page)
            self.opened_urls: list[str] = []

        def open(self, url: str, adapter: Any, consume: Any) -> None:
            self.opened_urls.append(url)
            self.page.url = "https://www.douyin.com/search/efficiency%20tools?type=general"
            consume(
                "https://www.douyin.com/aweme/v1/web/general/search/single/"
                "?keyword=efficiency%20tools&count=10&offset=10",
                douyin_payload(("7390000000000000001", "current", 1)),
            )

        def observe(self, adapter: Any, consume: Any) -> None:
            page_number = adapter.page_number
            if page_number == 2:
                late_url = (
                    "https://www.douyin.com/aweme/v1/web/general/search/single/"
                    "?keyword=efficiency%20tools&count=10&offset=10"
                )
                assert adapter.response_kind(late_url) == "comments"
                consume(
                    late_url,
                    douyin_payload(("7390000000000000099", "late previous page", 99)),
                )
            response_url = (
                "https://www.douyin.com/aweme/v1/web/general/search/single/"
                f"?keyword=efficiency%20tools&count=10&offset={page_number * 10}"
            )
            assert adapter.response_kind(response_url) == "comments"
            consume(
                response_url,
                douyin_payload((f"739000000000000000{page_number}", "current", page_number)),
            )

    session = SequencedSession(page)
    page.mouse = Mock()  # type: ignore[attr-defined]
    source = BrowserCandidateSource(BrowserLaunchConfig(), session=session)  # type: ignore[arg-type]

    result = discover(
        request(platform="douyin", max_pages=2),
        browser=source,
        output_root=tmp_path,
    )

    assert session.opened_urls == [
        "https://www.douyin.com/search/efficiency%20tools?type=general&page=1"
    ]
    assert [str(item.source_page) for item in result.candidates] == [
        "https://www.douyin.com/search/efficiency%20tools?type=general&page=1",
        "https://www.douyin.com/search/efficiency%20tools?type=general&page=2",
    ]


def test_douyin_query_paging_scrolls_same_page_after_rebinding_response_adapter(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    page = FakePage(final_url="about:blank")

    class ScrollingMouse:
        def __init__(self, session: "ScrollingSession") -> None:
            self.session = session

        def wheel(self, delta_x: float, delta_y: float) -> None:
            assert (delta_x, delta_y) == (0, 10000)
            events.append("scroll")
            assert len(self.session.observed_pages) == 1
            for page_number in (2, 3):
                response_url = (
                    "https://www.douyin.com/aweme/v1/web/general/search/single/"
                    f"?keyword=efficiency%20tools&count=10&offset={page_number * 10}"
                )
                assert self.session.adapter.response_kind(response_url) == "comments"
                self.session.consume(
                    response_url,
                    douyin_payload((f"739000000000000000{page_number}", "current", page_number)),
                )

    class ScrollingSession(FakeSession):
        def __init__(self, browser_page: FakePage) -> None:
            super().__init__(browser_page)
            self.opened_urls: list[str] = []
            self.observed_pages: list[int] = []

        def open(self, url: str, adapter: Any, consume: Any) -> None:
            self.opened_urls.append(url)
            events.append("open")
            self.page.url = "https://www.douyin.com/search/efficiency%20tools?type=general"
            self.adapter = adapter
            self.consume = consume
            current_url = (
                "https://www.douyin.com/aweme/v1/web/general/search/single/"
                "?keyword=efficiency%20tools&count=10&offset=10"
            )
            consume(current_url, douyin_payload(("7390000000000000001", "current", 1)))

        def observe(self, adapter: Any, consume: Any) -> None:
            events.append("observe")
            self.adapter = adapter
            self.consume = consume
            self.observed_pages.append(adapter.page_number)

    session = ScrollingSession(page)
    page.mouse = ScrollingMouse(session)  # type: ignore[attr-defined]
    source = BrowserCandidateSource(BrowserLaunchConfig(), session=session)  # type: ignore[arg-type]

    result = discover(
        request(platform="douyin", max_pages=3),
        browser=source,
        output_root=tmp_path,
    )

    assert session.opened_urls == [
        "https://www.douyin.com/search/efficiency%20tools?type=general&page=1"
    ]
    assert session.observed_pages == [2]
    assert events == ["open", "observe", "scroll"]
    assert [item.video_key for item in result.candidates] == [
        "7390000000000000001",
        "7390000000000000002",
        "7390000000000000003",
    ]
    assert [str(item.source_page) for item in result.candidates] == [
        "https://www.douyin.com/search/efficiency%20tools?type=general&page=1",
        "https://www.douyin.com/search/efficiency%20tools?type=general&page=2",
        "https://www.douyin.com/search/efficiency%20tools?type=general&page=3",
    ]


def test_douyin_future_batch_is_awaited_without_polluting_current_page(
    tmp_path: Path,
) -> None:
    page = FakePage(final_url="about:blank")

    class PrefetchSession(FakeSession):
        def __init__(self, browser_page: FakePage) -> None:
            super().__init__(browser_page)
            self.open_count = 0
            self.future: tuple[str, Any, Any] | None = None
            self.future_settled = False

        def open(self, url: str, adapter: Any, consume: Any) -> None:
            self.open_count += 1
            self.page.url = "https://www.douyin.com/search/efficiency%20tools?type=general"
            offset = self.open_count * 10
            current_url = (
                "https://www.douyin.com/aweme/v1/web/general/search/single/"
                f"?keyword=efficiency%20tools&count=10&offset={offset}"
            )
            assert adapter.response_kind(current_url) == "comments"
            consume(
                current_url,
                douyin_payload((f"739000000000000000{self.open_count}", "current", 1)),
            )
            if self.open_count == 1:
                future_url = (
                    "https://www.douyin.com/aweme/v1/web/general/search/single/"
                    "?keyword=efficiency%20tools&count=10&offset=20"
                )
                assert adapter.response_kind(future_url) == "comments"
                self.future = (future_url, adapter, consume)
            else:
                assert self.future_settled is True

        def observe(self, adapter: Any, consume: Any) -> None:
            del adapter, consume
            pytest.fail("a validated prefetched batch must not be requested again")

        def wait_for_response_processing(self, **kwargs: float) -> None:
            super().wait_for_response_processing(**kwargs)
            if self.future is not None:
                response_url, adapter, consume = self.future
                assert adapter.response_kind(response_url) == "comments"
                consume(
                    response_url,
                    douyin_payload(("7390000000000000098", "prefetched future", 98)),
                )
                self.future = None
                self.future_settled = True

    session = PrefetchSession(page)

    page.mouse = Mock()  # type: ignore[attr-defined]
    source = BrowserCandidateSource(BrowserLaunchConfig(), session=session)  # type: ignore[arg-type]

    result = discover(
        request(platform="douyin", max_pages=2),
        browser=source,
        output_root=tmp_path,
    )

    assert session.future_settled is True
    assert [item.video_key for item in result.candidates] == [
        "7390000000000000001",
        "7390000000000000098",
    ]
    page.mouse.wheel.assert_not_called()  # type: ignore[attr-defined]


def test_real_browser_session_awaits_inflight_douyin_prefetch_before_next_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = 0.0
    callbacks: dict[str, Any] = {}
    future: dict[str, Any] = {}
    navigation_count = 0
    page = Mock()
    context = Mock()
    page.url = "about:blank"
    page.on.side_effect = lambda event, callback: callbacks.__setitem__(event, callback)
    context.on.side_effect = lambda event, callback: callbacks.__setitem__(event, callback)

    def response(url: str, payload: Mapping[str, object]) -> Mock:
        value = Mock(url=url)
        value.json.return_value = payload
        return value

    def goto(url: str) -> None:
        nonlocal navigation_count
        navigation_count += 1
        if navigation_count == 2:
            assert future.get("settled") is True
        page.url = "https://www.douyin.com/search/efficiency%20tools?type=general"
        current_url = (
            "https://www.douyin.com/aweme/v1/web/general/search/single/"
            f"?keyword=efficiency%20tools&count=10&offset={navigation_count * 10}"
        )
        current_request = Mock(url=current_url)
        callbacks["request"](current_request)
        callbacks["response"](
            response(
                current_url,
                douyin_payload((f"739000000000000000{navigation_count}", "current", 1)),
            )
        )
        callbacks["requestfinished"](current_request)
        if navigation_count == 1:
            future_url = (
                "https://www.douyin.com/aweme/v1/web/general/search/single/"
                "?keyword=efficiency%20tools&count=10&offset=20"
            )
            future_request = Mock(url=future_url)
            future_response = response(
                future_url,
                douyin_payload(("7390000000000000098", "prefetched future", 98)),
            )
            future.update(
                url=future_url,
                request=future_request,
                response=future_response,
                settled=False,
            )
            callbacks["request"](future_request)

    def wait_for_timeout(timeout: float) -> None:
        nonlocal clock
        clock += timeout / 1000
        if future and not future["settled"] and clock >= 1.0:
            callbacks["response"](future["response"])
            callbacks["requestfinished"](future["request"])
            future["settled"] = True

    page.goto.side_effect = goto
    page.wait_for_timeout.side_effect = wait_for_timeout
    monkeypatch.setattr(browser_module, "monotonic", lambda: clock)

    session = BrowserSession(Mock())
    session._page = page
    session._context = context
    discovery_request = request(platform="douyin", max_pages=2)
    captured: list[list[Mapping[str, object]]] = []
    for page_number in (1, 2):
        source_page = _search_url(discovery_request, page_number)
        captured.append(
            _capture_candidate_payloads(
                page,
                discovery_request,
                source_page,
                page_number,
                session.open,
                session.wait_for_response_processing,
                session.raise_if_response_failed,
            )
        )

    assert future["settled"] is True
    future["response"].json.assert_called_once_with()
    assert [payload[0]["data"][0]["aweme_info"]["aweme_id"] for payload in captured] == [
        "7390000000000000001",
        "7390000000000000002",
    ]


def test_douyin_future_batch_shape_failure_fails_closed(tmp_path: Path) -> None:
    page = FakePage(final_url="about:blank")

    class FutureShapeFailureSession(FakeSession):
        def __init__(self, browser_page: FakePage) -> None:
            super().__init__(browser_page)
            self.delayed_responses: list[tuple[str, object]] = []

        def open(self, url: str, adapter: Any, consume: Any) -> None:
            del url
            self.page.url = "https://www.douyin.com/search/efficiency%20tools?type=general"
            self.consume = consume
            current_url = (
                "https://www.douyin.com/aweme/v1/web/general/search/single/"
                "?keyword=efficiency%20tools&count=10&offset=10"
            )
            assert adapter.response_kind(current_url) == "comments"
            consume(current_url, douyin_payload(("7390000000000000001", "current", 1)))
            future_url = (
                "https://www.douyin.com/aweme/v1/web/general/search/single/"
                "?keyword=efficiency%20tools&count=10&offset=20"
            )
            if adapter.response_kind(future_url) is not None:
                self.delayed_responses = [(future_url, [])]

        def wait_for_response_processing(self, **kwargs: float) -> None:
            assert kwargs == {"quiet_seconds": 0.75, "timeout_seconds": 2.0}
            for response_url, payload in self.delayed_responses:
                self.consume(response_url, payload)

    session = FutureShapeFailureSession(page)
    source = BrowserCandidateSource(BrowserLaunchConfig(), session=session)  # type: ignore[arg-type]

    with pytest.raises(ResponseShapeChanged, match="^response_shape_changed$"):
        discover(
            request(platform="douyin", max_pages=1),
            browser=source,
            output_root=tmp_path,
        )

    assert not (tmp_path / "candidates").exists()


@pytest.mark.parametrize(
    "future_payload",
    [
        {"status_code": 0, "unknown": []},
        {"status_code": 1, "data": []},
        {"status_code": 0, "data": {}},
        {"status_code": 0, "data": [{"unknown": {}}]},
        UnreadableCommentPayload(),
    ],
    ids=["unknown", "status", "data", "item", "comments"],
)
def test_douyin_future_mapping_must_pass_full_candidate_validation(
    tmp_path: Path, future_payload: Mapping[str, object]
) -> None:
    page = FakePage(final_url="about:blank")

    class FutureCandidateShapeSession(FakeSession):
        def open(self, url: str, adapter: Any, consume: Any) -> None:
            del url
            self.page.url = "https://www.douyin.com/search/efficiency%20tools?type=general"
            self.consume = consume
            current_url = (
                "https://www.douyin.com/aweme/v1/web/general/search/single/"
                "?keyword=efficiency%20tools&count=10&offset=10"
            )
            consume(current_url, douyin_payload(("7390000000000000001", "current", 1)))
            future_url = (
                "https://www.douyin.com/aweme/v1/web/general/search/single/"
                "?keyword=efficiency%20tools&count=10&offset=20"
            )
            assert adapter.response_kind(future_url) == "comments"
            self.delayed_responses = [(future_url, future_payload)]

        def wait_for_response_processing(self, **kwargs: float) -> None:
            assert kwargs == {"quiet_seconds": 0.75, "timeout_seconds": 2.0}
            for response_url, payload in self.delayed_responses:
                self.consume(response_url, payload)

    source = BrowserCandidateSource(
        BrowserLaunchConfig(),
        session=FutureCandidateShapeSession(page),  # type: ignore[arg-type]
    )

    with pytest.raises(CandidateShapeChanged, match="^candidate_shape_changed$"):
        discover(
            request(platform="douyin", max_pages=1),
            browser=source,
            output_root=tmp_path,
        )

    assert not (tmp_path / "candidates").exists()


def test_douyin_future_batch_json_failure_fails_closed(tmp_path: Path) -> None:
    page = FakePage(final_url="about:blank")

    class FutureJsonFailureSession(FakeSession):
        def __init__(self, browser_page: FakePage) -> None:
            super().__init__(browser_page)
            self.future_supported = False

        def open(self, url: str, adapter: Any, consume: Any) -> None:
            del url
            self.page.url = "https://www.douyin.com/search/efficiency%20tools?type=general"
            current_url = (
                "https://www.douyin.com/aweme/v1/web/general/search/single/"
                "?keyword=efficiency%20tools&count=10&offset=10"
            )
            consume(current_url, douyin_payload(("7390000000000000001", "current", 1)))
            future_url = (
                "https://www.douyin.com/aweme/v1/web/general/search/single/"
                "?keyword=efficiency%20tools&count=10&offset=20"
            )
            self.future_supported = adapter.response_kind(future_url) is not None

        def wait_for_response_processing(self, **kwargs: float) -> None:
            assert kwargs == {"quiet_seconds": 0.75, "timeout_seconds": 2.0}
            if self.future_supported:
                raise BrowserSessionError("response_processing_failed")

    source = BrowserCandidateSource(
        BrowserLaunchConfig(),
        session=FutureJsonFailureSession(page),  # type: ignore[arg-type]
    )

    with pytest.raises(BrowserSessionError, match="^response_processing_failed$"):
        discover(
            request(platform="douyin", max_pages=1),
            browser=source,
            output_root=tmp_path,
        )

    assert not (tmp_path / "candidates").exists()


def test_douyin_third_page_without_offset_thirty_fails_closed(tmp_path: Path) -> None:
    page = FakePage(final_url="about:blank")

    class MissingThirdBatchSession(FakeSession):
        def __init__(self, browser_page: FakePage) -> None:
            super().__init__(browser_page)
            self.open_count = 0

        def open(self, url: str, adapter: Any, consume: Any) -> None:
            del url
            self.page.url = "https://www.douyin.com/search/efficiency%20tools?type=general"
            self.adapter = adapter
            self.consume = consume
            self.emit_batch()

        def observe(self, adapter: Any, consume: Any) -> None:
            self.adapter = adapter
            self.consume = consume

        def emit_batch(self) -> None:
            self.open_count += 1
            observed_offset = min(self.open_count, 2) * 10
            response_url = (
                "https://www.douyin.com/aweme/v1/web/general/search/single/"
                f"?keyword=efficiency%20tools&count=10&offset={observed_offset}"
            )
            if self.adapter.response_kind(response_url) is not None:
                self.consume(
                    response_url,
                    douyin_payload((f"739000000000000000{self.open_count}", "current", 1)),
                )

    session = MissingThirdBatchSession(page)
    page.mouse = Mock()  # type: ignore[attr-defined]
    page.mouse.wheel.side_effect = lambda *_: session.emit_batch()  # type: ignore[attr-defined]
    source = BrowserCandidateSource(
        BrowserLaunchConfig(),
        session=session,  # type: ignore[arg-type]
    )

    with pytest.raises(CandidateShapeChanged, match="^candidate_shape_changed$"):
        discover(
            request(platform="douyin", max_pages=3),
            browser=source,
            output_root=tmp_path,
        )

    assert not (tmp_path / "candidates").exists()


def test_douyin_explicit_source_url_keeps_exact_page_query(tmp_path: Path) -> None:
    source_url = "https://www.douyin.com/search/efficiency%20tools?type=general&page=1"
    page = FakePage(
        final_url="https://www.douyin.com/search/efficiency%20tools?type=general",
        responses=[
            (
                "https://www.douyin.com/aweme/v1/web/general/search/single/"
                "?keyword=efficiency%20tools&count=10&offset=10",
                douyin_payload(("7390000000000000001", "one", 1)),
            )
        ],
    )
    source = BrowserCandidateSource(
        BrowserLaunchConfig(),
        session=FakeSession(page),  # type: ignore[arg-type]
    )

    with pytest.raises(CandidateShapeChanged, match="^candidate_shape_changed$"):
        discover(
            request(platform="douyin", query=None, source_url=source_url, max_pages=1),
            browser=source,
            output_root=tmp_path,
        )

    assert not (tmp_path / "candidates").exists()


def test_empty_page_fails_closed_before_publishing(tmp_path: Path) -> None:
    page = FakePage(final_url="https://search.bilibili.com/video?keyword=efficiency%20tools&page=1")
    source = BrowserCandidateSource(
        BrowserLaunchConfig(),
        session=FakeSession(page),  # type: ignore[arg-type]
    )

    with pytest.raises(CandidateShapeChanged, match="^candidate_shape_changed$"):
        discover(request(max_pages=1), browser=source, output_root=tmp_path)

    assert not (tmp_path / "candidates").exists()


def test_delayed_candidate_response_is_collected_during_bounded_wait(tmp_path: Path) -> None:
    page = FakePage(
        final_url="https://search.bilibili.com/video?keyword=efficiency%20tools&page=1",
        delayed_responses=[
            (
                "https://api.bilibili.com/x/web-interface/search/type"
                "?keyword=efficiency%20tools&page=1",
                bili_payload(("BV1", "one", 1)),
            )
        ],
    )

    class DelayedResponseSession(FakeSession):
        def open(self, url: str, adapter: Any, consume: Any) -> None:
            super().open(url, adapter, consume)

            def wait_for_timeout(timeout: float) -> None:
                assert timeout > 0
                delayed = list(self.page.delayed_responses)
                self.page.delayed_responses.clear()
                for response_url, payload in delayed:
                    if self.adapter.response_kind(response_url) is not None:
                        self.consume(response_url, payload)

            self.page.wait_for_timeout = wait_for_timeout  # type: ignore[method-assign]

    session = DelayedResponseSession(page)
    source = BrowserCandidateSource(BrowserLaunchConfig(), session=session)  # type: ignore[arg-type]

    result = discover(request(max_pages=1), browser=source, output_root=tmp_path)

    assert [item.video_key for item in result.candidates] == ["BV1"]
    assert session.open_called is True
    assert session.bounded_wait_called is True


def test_response_arriving_during_session_quiet_wait_is_collected(tmp_path: Path) -> None:
    page = FakePage(final_url="https://search.bilibili.com/video?keyword=efficiency%20tools&page=1")

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
            response_url = (
                "https://api.bilibili.com/x/web-interface/search/type"
                "?keyword=efficiency%20tools&page=1"
            )
            self.consume(response_url, bili_payload(("BV0", "initial", 1)))

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

    assert [item.video_key for item in result.candidates] == ["BV0", "BV1"]
    assert session.bounded_wait_called is True


def test_douyin_waits_for_first_candidate_response_then_collects_quiet_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Mock(return_value=0.0)
    elapsed = 0.0
    page = FakePage(final_url="https://www.douyin.com/search/efficiency%20tools?type=general")

    class DelayedFirstResponseSession(FakeSession):
        def __init__(self, browser_page: FakePage) -> None:
            super().__init__(browser_page)
            self.wait_calls = 0
            self.first_sent = False

        def open(self, url: str, adapter: Any, consume: Any) -> None:
            super().open(url, adapter, consume)

            def wait_for_timeout(timeout: float) -> None:
                nonlocal elapsed
                elapsed += timeout / 1000
                clock.return_value = elapsed
                if elapsed >= 4.0 and not self.first_sent:
                    self.first_sent = True
                    response_url = (
                        "https://www.douyin.com/aweme/v1/web/general/search/single/"
                        "?keyword=efficiency%20tools&offset=12&count=12"
                    )
                    assert self.adapter.response_kind(response_url) is not None
                    self.consume(
                        response_url,
                        douyin_payload(("7390000000000000001", "first", 1)),
                    )

            self.page.wait_for_timeout = wait_for_timeout  # type: ignore[method-assign]

        def wait_for_response_processing(self, **kwargs: float) -> None:
            self.wait_calls += 1
            assert kwargs == {"quiet_seconds": 0.75, "timeout_seconds": 2.0}
            response_url = (
                "https://www.douyin.com/aweme/v1/web/general/search/single/"
                "?keyword=efficiency%20tools&offset=12&count=12"
            )
            assert self.adapter.response_kind(response_url) is not None
            self.consume(
                response_url,
                douyin_payload(("7390000000000000002", "second", 2)),
            )

    monkeypatch.setattr(discovery_module, "monotonic", clock, raising=False)
    session = DelayedFirstResponseSession(page)
    source = BrowserCandidateSource(BrowserLaunchConfig(), session=session)  # type: ignore[arg-type]

    result = discover(
        request(platform="douyin", max_pages=1),
        browser=source,
        output_root=tmp_path,
    )

    assert [item.video_key for item in result.candidates] == [
        "7390000000000000001",
        "7390000000000000002",
    ]
    assert 4.0 <= elapsed <= 4.05
    assert session.wait_calls == 1


def test_candidate_first_response_wait_is_bounded_and_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elapsed = 0.0

    def monotonic() -> float:
        return elapsed

    page = FakePage(final_url="https://www.douyin.com/search/efficiency%20tools?type=general")

    def wait_for_timeout(timeout: float) -> None:
        nonlocal elapsed
        elapsed += timeout / 1000

    page.wait_for_timeout = wait_for_timeout  # type: ignore[method-assign]
    monkeypatch.setattr(discovery_module, "monotonic", monotonic, raising=False)
    session = FakeSession(page)
    source = BrowserCandidateSource(BrowserLaunchConfig(), session=session)  # type: ignore[arg-type]

    with pytest.raises(CandidateShapeChanged, match="^candidate_shape_changed$"):
        discover(
            request(platform="douyin", max_pages=1),
            browser=source,
            output_root=tmp_path,
        )

    assert elapsed == pytest.approx(5.0)
    assert session.bounded_wait_called is False
    assert not (tmp_path / "candidates").exists()


@pytest.mark.parametrize(
    ("failure_type", "message"),
    [
        (BrowserSessionError, "response_processing_failed"),
        (ResponseShapeChanged, "response_shape_changed"),
    ],
)
def test_delayed_matching_response_failure_is_preserved_without_artifacts(
    tmp_path: Path,
    failure_type: type[Exception],
    message: str,
) -> None:
    page = FakePage(final_url="https://www.douyin.com/search/efficiency%20tools?type=general")

    class FailedResponseSession(FakeSession):
        def __init__(self, browser_page: FakePage) -> None:
            super().__init__(browser_page)
            self.failed = False

        def open(self, url: str, adapter: Any, consume: Any) -> None:
            super().open(url, adapter, consume)

            def wait_for_timeout(timeout: float) -> None:
                assert timeout > 0
                response_url = (
                    "https://www.douyin.com/aweme/v1/web/general/search/single/"
                    "?keyword=efficiency%20tools&offset=12&count=12"
                )
                assert self.adapter.response_kind(response_url) is not None
                self.failed = True

            self.page.wait_for_timeout = wait_for_timeout  # type: ignore[method-assign]

        def raise_if_response_failed(self) -> None:
            if self.failed:
                raise failure_type(message)

    session = FailedResponseSession(page)
    source = BrowserCandidateSource(BrowserLaunchConfig(), session=session)  # type: ignore[arg-type]

    with pytest.raises(failure_type, match=f"^{message}$"):
        discover(
            request(platform="douyin", max_pages=1),
            browser=source,
            output_root=tmp_path,
        )

    assert not (tmp_path / "candidates").exists()


def test_bilibili_immediate_trusted_dom_skips_response_wait(tmp_path: Path) -> None:
    card = bili_card(
        [element(href="https://www.bilibili.com/video/BV1xx411c7mD")],
        title="available immediately",
    )

    class ImmediateDomPage(FakePage):
        def __init__(self) -> None:
            super().__init__(
                final_url=("https://search.bilibili.com/video?keyword=efficiency%20tools&page=1")
            )
            self.timeout_calls = 0

        def wait_for_timeout(self, timeout: float) -> None:
            del timeout
            self.timeout_calls += 1

        def locator(self, selector: str) -> Any:
            if selector == ".bili-video-card":
                return ElementLocator([card])
            return ElementLocator([])

    page = ImmediateDomPage()
    session = FakeSession(page)
    source = BrowserCandidateSource(BrowserLaunchConfig(), session=session)  # type: ignore[arg-type]

    result = discover(request(max_pages=1), browser=source, output_root=tmp_path)

    assert [item.video_key for item in result.candidates] == ["BV1xx411c7mD"]
    assert page.timeout_calls == 0
    assert session.bounded_wait_called is False


@pytest.mark.parametrize("use_response", [True, False])
def test_redirect_during_quiet_wait_fails_before_response_or_dom_publish(
    tmp_path: Path, use_response: bool
) -> None:
    response_url = (
        "https://api.bilibili.com/x/web-interface/search/type?keyword=efficiency%20tools&page=1"
    )
    page = FakePage(
        final_url="https://search.bilibili.com/video?keyword=efficiency%20tools&page=1",
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
        def open(self, url: str, adapter: Any, consume: Any) -> None:
            super().open(url, adapter, consume)
            if not use_response:
                self.page.wait_for_timeout = lambda timeout: setattr(  # type: ignore[method-assign]
                    self.page, "url", "https://search.bilibili.com/account/login"
                )

        def wait_for_response_processing(self, **kwargs: float) -> None:
            super().wait_for_response_processing(**kwargs)
            self.page.url = "https://search.bilibili.com/account/login"

    source = BrowserCandidateSource(
        BrowserLaunchConfig(),
        session=RedirectDuringWaitSession(page),  # type: ignore[arg-type]
    )

    with pytest.raises(CandidateShapeChanged, match="^candidate_shape_changed$"):
        discover(request(max_pages=1), browser=source, output_root=tmp_path)

    if use_response:
        assert page.locator_calls == []
    else:
        assert page.locator_calls != []
    assert not (tmp_path / "candidates").exists()


def test_late_response_from_previous_page_cannot_pollute_next_page(tmp_path: Path) -> None:
    page = FakePage(final_url="https://search.bilibili.com/video?keyword=efficiency%20tools&page=1")

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
                source_page=("https://search.bilibili.com/video?keyword=efficiency%20tools&page=1"),
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
