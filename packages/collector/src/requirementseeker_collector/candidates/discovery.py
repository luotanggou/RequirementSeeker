"""Bounded, auditable candidate discovery without automatic collection."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Annotated, Any, Literal, Protocol, Self, cast
from urllib.parse import (
    parse_qs,
    parse_qsl,
    quote,
    unquote,
    unquote_plus,
    urlencode,
    urlsplit,
    urlunsplit,
)
from uuid import uuid4

from playwright.sync_api import Page
from pydantic import Field, TypeAdapter, ValidationError, model_validator

from ..adapters.base import PlatformAdapter, ResponseKind, ResponseShapeChanged
from ..browser import (
    BrowserLaunchConfig,
    BrowserName,
    BrowserSession,
    BrowserSessionError,
    SessionMode,
)
from ..contracts import Contract, Direction, Platform, PublicHttpUrl
from ..runner import _paths_are_safe_under
from .adapters import CandidateShapeChanged, parse_bilibili_candidates, parse_douyin_candidates
from .contracts import CandidateVideo

type CandidatePayload = Mapping[str, Any]

_PLATFORM_HOSTS: dict[Platform, tuple[str, ...]] = {
    "bilibili": ("bilibili.com", "b23.tv"),
    "douyin": ("douyin.com", "iesdouyin.com"),
}
_CANDIDATE_RESPONSE_PATHS: dict[Platform, tuple[str, ...]] = {
    "bilibili": ("/x/web-interface/search/type", "/x/web-interface/wbi/search/type"),
    "douyin": ("/aweme/v1/web/general/search/single/",),
}
_PUBLIC_URL_ADAPTER = TypeAdapter(PublicHttpUrl)
_BILIBILI_VIDEO_PATH = re.compile(r"^/video/(BV[0-9A-Za-z]{10})/?$")


def _is_candidate_page(platform: Platform, url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = parsed.hostname
    path = parsed.path.rstrip("/")
    if platform == "bilibili":
        return bool(
            (host == "search.bilibili.com" and path in {"/all", "/video"})
            or (host == "www.bilibili.com" and path.startswith("/v/popular/"))
        )
    return bool(host == "www.douyin.com" and path.startswith("/search/") and path[8:])


class DiscoveryRequest(Contract):
    """One explicit, bounded candidate discovery request."""

    platform: Platform
    direction: Direction
    query: (
        Annotated[str, Field(strict=True, min_length=1, max_length=200, pattern=r"^[^\r\n]+$")]
        | None
    ) = None
    source_url: PublicHttpUrl | None = None
    max_pages: int = Field(default=3, strict=True, ge=1, le=20)
    max_results: int = Field(default=50, strict=True, ge=1, le=500)

    @model_validator(mode="after")
    def valid_source(self) -> Self:
        has_query = self.query is not None and bool(self.query.strip())
        has_source_url = self.source_url is not None
        if has_query == has_source_url:
            raise ValueError("exactly_one_discovery_source")
        if self.query is not None and not has_query:
            raise ValueError("exactly_one_discovery_source")
        if self.source_url is not None:
            host = self.source_url.host
            if host is None or not any(
                host == root or host.endswith(f".{root}") for root in _PLATFORM_HOSTS[self.platform]
            ):
                raise ValueError("source_url_platform_mismatch")
            if not _is_candidate_page(self.platform, str(self.source_url)):
                raise ValueError("source_url_not_candidate_page")
            page_parameters = [
                value
                for key, value in parse_qsl(self.source_url.query, keep_blank_values=True)
                if key == "page"
            ]
            if len(page_parameters) > 1:
                raise ValueError("duplicate_source_page_parameter")
        return self


@dataclass(frozen=True)
class CandidatePage:
    """One supported candidate response shape from a public listing page."""

    page_number: int
    source_page: str
    payload: CandidatePayload

    def __post_init__(self) -> None:
        if type(self.page_number) is not int or self.page_number < 1:
            raise ValueError("invalid_candidate_page_number")
        if not isinstance(self.payload, Mapping):
            raise ValueError("invalid_candidate_payload")


class CandidatePageSource(Protocol):
    @property
    def browser(self) -> BrowserName: ...

    @property
    def session_mode(self) -> SessionMode: ...

    def pages(self, request: DiscoveryRequest) -> Iterable[CandidatePage]: ...


class CandidateManifest(Contract):
    manifest_version: Literal["1.0"] = "1.0"
    candidates: list[CandidateVideo]


class DiscoveryAudit(Contract):
    discovery_version: Literal["1.0"] = "1.0"
    platform: Platform
    direction: Direction
    source_kind: Literal["query", "source_url"]
    max_pages: int
    max_results: int
    pages_processed: int
    candidate_count: int
    browser: BrowserName
    session_mode: SessionMode
    status: Literal["success"] = "success"


@dataclass(frozen=True)
class DiscoveryResult:
    candidates: list[CandidateVideo]
    pages_processed: int
    manifest_path: Path
    discovery_path: Path


def deduplicate_candidates(items: Iterable[CandidateVideo]) -> list[CandidateVideo]:
    """Keep the first discovery and its provenance for every platform/video pair."""

    result: list[CandidateVideo] = []
    seen: set[tuple[Platform, str]] = set()
    for item in items:
        key = (item.platform, item.video_key)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _search_url(request: DiscoveryRequest, page_number: int) -> str:
    if request.source_url is not None:
        source_url = str(request.source_url)
        if page_number == 1:
            return source_url
        parts = urlsplit(source_url)
        query_parts = parts.query.split("&") if parts.query else []
        page_index = next(
            (
                index
                for index, item in enumerate(query_parts)
                if unquote_plus(item.partition("=")[0]) == "page"
            ),
            None,
        )
        if page_index is None:
            query_parts.append(f"page={page_number}")
        else:
            raw_key = query_parts[page_index].partition("=")[0]
            query_parts[page_index] = f"{raw_key}={page_number}"
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, "&".join(query_parts), parts.fragment)
        )
    assert request.query is not None
    encoded = quote(request.query, safe="")
    if request.platform == "bilibili":
        return f"https://search.bilibili.com/video?keyword={encoded}&page={page_number}"
    return f"https://www.douyin.com/search/{encoded}?type=general&page={page_number}"


def _is_candidate_endpoint(platform: Platform, url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
    except ValueError:
        return False
    return bool(
        host
        and any(host == root or host.endswith(f".{root}") for root in _PLATFORM_HOSTS[platform])
        and parsed.path in _CANDIDATE_RESPONSE_PATHS[platform]
    )


def _one_query_value(url: str, name: str) -> str | None:
    values = parse_qs(urlsplit(url).query, keep_blank_values=True).get(name)
    return values[0] if values is not None and len(values) == 1 else None


def _expected_query(request: DiscoveryRequest, source_page: str) -> str | None:
    if request.query is not None:
        return request.query
    if request.platform == "bilibili":
        return _one_query_value(source_page, "keyword")
    path = urlsplit(source_page).path.rstrip("/")
    prefix = "/search/"
    if path.startswith(prefix) and (encoded_query := path[len(prefix) :]):
        return unquote(encoded_query)
    return _one_query_value(source_page, "keyword")


@dataclass(frozen=True)
class _CandidateResponseAdapter:
    platform: Platform
    expected_query: str | None
    page_number: int

    @staticmethod
    def _douyin_batch(url: str) -> tuple[int, int] | None:
        raw_offset = _one_query_value(url, "offset")
        raw_count = _one_query_value(url, "count")
        try:
            offset = int(raw_offset) if raw_offset is not None else -1
            count = int(raw_count) if raw_count is not None else 0
        except ValueError:
            return None
        if count <= 0 or offset < count or offset % count:
            return None
        return offset, count

    def response_kind(self, url: str) -> ResponseKind | None:
        if not _is_candidate_endpoint(self.platform, url) or self.expected_query is None:
            return None
        if _one_query_value(url, "keyword") != self.expected_query:
            return None
        if self.platform == "bilibili":
            return "comments" if _one_query_value(url, "page") == str(self.page_number) else None
        return "comments" if self._douyin_batch(url) is not None else None

    def matches_current_page(self, url: str) -> bool:
        if self.response_kind(url) is None:
            return False
        if self.platform == "bilibili":
            return True
        batch = self._douyin_batch(url)
        assert batch is not None
        offset, count = batch
        return offset == self.page_number * count


def _bilibili_card_payload(page: Page) -> CandidatePayload | None:
    cards = page.locator(".bili-video-card")
    if cards.count() == 0:
        cards = page.locator(".bili-video-card__wrap")
    card_count = cards.count()
    if card_count == 0:
        return None

    values: list[dict[str, object]] = []
    seen: set[str] = set()
    for card_index in range(card_count):
        card = cards.nth(card_index)
        headings = card.locator("h3")
        titles = {
            title
            for heading_index in range(headings.count())
            if (
                title := (
                    headings.nth(heading_index).get_attribute("title")
                    or headings.nth(heading_index).text_content()
                    or ""
                ).strip()
            )
        }
        if len(titles) != 1:
            continue
        links = card.locator("a[href]")
        video_keys: list[str] = []
        for link_index in range(links.count()):
            href = links.nth(link_index).get_attribute("href")
            if href is None:
                continue
            normalized = f"https:{href}" if href.startswith("//") else href
            try:
                parsed = urlsplit(normalized)
            except ValueError:
                continue
            match = _BILIBILI_VIDEO_PATH.fullmatch(parsed.path)
            if parsed.scheme != "https" or parsed.hostname != "www.bilibili.com" or match is None:
                continue
            video_key = match.group(1)
            if video_key not in video_keys:
                video_keys.append(video_key)
        if len(video_keys) != 1:
            continue
        video_key = video_keys[0]
        if video_key not in seen:
            seen.add(video_key)
            values.append({"bvid": video_key, "title": next(iter(titles)), "review": None})
    if not values:
        return None
    return {"code": 0, "data": {"result": values}}


def _dom_payload(page: Page, platform: Platform) -> CandidatePayload | None:
    """Read only candidate metadata inside trusted result containers."""

    if platform == "bilibili":
        return _bilibili_card_payload(page)

    rows = page.locator(
        "[data-candidate-results] [data-candidate-video][data-video-key][data-title]"
    )
    count = rows.count()
    if count == 0:
        return None
    values: list[dict[str, object]] = []
    for index in range(count):
        row = rows.nth(index)
        video_key = row.get_attribute("data-video-key")
        title = row.get_attribute("data-title")
        raw_count = row.get_attribute("data-comment-count")
        comment_count = int(raw_count) if raw_count is not None and raw_count.isdigit() else None
        values.append(
            {
                "aweme_info": {
                    "aweme_id": video_key,
                    "desc": title,
                    "statistics": {"comment_count": comment_count},
                }
            }
        )
    return {"status_code": 0, "data": values}


def _validate_navigated_url(
    platform: Platform,
    requested_url: str,
    actual_url: str,
    *,
    allow_bilibili_implicit_page_one: bool = False,
    allow_douyin_implicit_page: bool = False,
    require_exact_query: bool = False,
) -> None:
    try:
        requested = _PUBLIC_URL_ADAPTER.validate_python(requested_url)
        actual = _PUBLIC_URL_ADAPTER.validate_python(actual_url)
    except ValidationError:
        raise CandidateShapeChanged("candidate_shape_changed") from None
    actual_host = actual.host
    requested_path = (requested.path or "/").rstrip("/")
    actual_path = (actual.path or "/").rstrip("/")
    requested_query = parse_qs(requested.query, keep_blank_values=True)
    actual_query = parse_qs(actual.query, keep_blank_values=True)
    query_mismatch = (
        actual_query != requested_query
        if require_exact_query
        else any(
            actual_query.get(key) != values
            and not (
                platform == "bilibili"
                and allow_bilibili_implicit_page_one
                and key == "page"
                and values == ["1"]
                and key not in actual_query
            )
            and not (
                platform == "douyin"
                and allow_douyin_implicit_page
                and key == "page"
                and len(values) == 1
                and key not in actual_query
            )
            for key, values in requested_query.items()
        )
    )
    if (
        requested.host is None
        or actual_host != requested.host
        or actual_path != requested_path
        or query_mismatch
        or not _is_candidate_page(platform, actual_url)
        or not any(
            actual_host == root or actual_host.endswith(f".{root}")
            for root in _PLATFORM_HOSTS[platform]
        )
    ):
        raise CandidateShapeChanged("candidate_shape_changed")


def _capture_candidate_payloads(
    page: Page,
    request: DiscoveryRequest,
    source_page: str,
    page_number: int,
    open_page: Callable[[str, PlatformAdapter, Callable[[str, object], None]], None],
    wait_for_response_processing: Callable[..., None],
    raise_if_response_failed: Callable[[], None],
    cached_douyin_batches: dict[tuple[int, int], CandidatePayload] | None = None,
) -> list[CandidatePayload]:
    captured: list[CandidatePayload] = []
    candidate_shape_changed = False
    matcher = _CandidateResponseAdapter(
        request.platform,
        _expected_query(request, source_page),
        page_number,
    )
    if cached_douyin_batches is not None:
        for batch in tuple(cached_douyin_batches):
            offset, count = batch
            if offset == page_number * count:
                captured.append(cached_douyin_batches.pop(batch))

    def consume(response_url: str, payload: object) -> None:
        nonlocal candidate_shape_changed
        if not isinstance(payload, Mapping):
            raise ResponseShapeChanged("response_shape_changed")
        if request.platform == "douyin":
            try:
                parse_douyin_candidates(
                    payload,
                    request.query,
                    source_page,
                    datetime.now(UTC),
                    direction=request.direction,
                )
            except CandidateShapeChanged:
                candidate_shape_changed = True
                raise ResponseShapeChanged("response_shape_changed") from None
        if matcher.matches_current_page(response_url):
            captured.append(payload)
        elif cached_douyin_batches is not None and matcher.response_kind(response_url) is not None:
            batch = matcher._douyin_batch(response_url)
            assert batch is not None
            offset, count = batch
            future_page = offset // count
            if page_number < future_page <= request.max_pages:
                cached_douyin_batches.setdefault(batch, payload)

    def preserve_candidate_shape(operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            result = operation(*args, **kwargs)
        except (BrowserSessionError, ResponseShapeChanged):
            if candidate_shape_changed:
                raise CandidateShapeChanged("candidate_shape_changed") from None
            raise
        if candidate_shape_changed:
            raise CandidateShapeChanged("candidate_shape_changed") from None
        return result

    allow_bilibili_implicit_page_one = (
        request.platform == "bilibili" and request.query is not None and page_number == 1
    )
    allow_douyin_implicit_page = request.platform == "douyin" and request.query is not None
    if not captured:
        preserve_candidate_shape(open_page, source_page, cast(PlatformAdapter, matcher), consume)
    _validate_navigated_url(
        request.platform,
        source_page,
        page.url,
        allow_bilibili_implicit_page_one=allow_bilibili_implicit_page_one,
        allow_douyin_implicit_page=allow_douyin_implicit_page,
        require_exact_query=request.source_url is not None,
    )
    preserve_candidate_shape(raise_if_response_failed)

    dom = None if captured else _dom_payload(page, request.platform)
    deadline = monotonic() + 5.0
    for _ in range(100):
        if captured or dom is not None:
            break
        remaining_seconds = deadline - monotonic()
        if remaining_seconds <= 0:
            break
        remaining_ms = min(50.0, remaining_seconds * 1000)
        preserve_candidate_shape(page.wait_for_timeout, remaining_ms)
        preserve_candidate_shape(raise_if_response_failed)
        _validate_navigated_url(
            request.platform,
            source_page,
            page.url,
            allow_bilibili_implicit_page_one=allow_bilibili_implicit_page_one,
            allow_douyin_implicit_page=allow_douyin_implicit_page,
            require_exact_query=request.source_url is not None,
        )
        if not captured:
            dom = _dom_payload(page, request.platform)

    if (
        not captured
        and dom is None
        and request.platform == "douyin"
        and request.query is not None
        and page_number == 1
    ):
        try:
            page.mouse.wheel(0, 10000)
        except Exception:
            raise BrowserSessionError("browser_navigation_failed") from None
        dom = _dom_payload(page, request.platform)
        deadline = monotonic() + 5.0
        for _ in range(100):
            if captured or dom is not None:
                break
            remaining_seconds = deadline - monotonic()
            if remaining_seconds <= 0:
                break
            remaining_ms = min(50.0, remaining_seconds * 1000)
            preserve_candidate_shape(page.wait_for_timeout, remaining_ms)
            preserve_candidate_shape(raise_if_response_failed)
            _validate_navigated_url(
                request.platform,
                source_page,
                page.url,
                allow_douyin_implicit_page=allow_douyin_implicit_page,
            )
            if not captured:
                dom = _dom_payload(page, request.platform)

    if captured:
        preserve_candidate_shape(
            wait_for_response_processing, quiet_seconds=0.75, timeout_seconds=2.0
        )
    preserve_candidate_shape(raise_if_response_failed)
    _validate_navigated_url(
        request.platform,
        source_page,
        page.url,
        allow_bilibili_implicit_page_one=allow_bilibili_implicit_page_one,
        allow_douyin_implicit_page=allow_douyin_implicit_page,
        require_exact_query=request.source_url is not None,
    )
    if not captured and dom is not None:
        captured.append(dom)
    if not captured:
        raise CandidateShapeChanged("candidate_shape_changed")
    return captured


@dataclass
class BrowserCandidateSource:
    """Visible Playwright source backed by the existing safe browser session."""

    launch_config: BrowserLaunchConfig
    session: BrowserSession | None = None

    @property
    def browser(self) -> BrowserName:
        return self.launch_config.browser

    @property
    def session_mode(self) -> SessionMode:
        return self.launch_config.session_mode

    def pages(self, request: DiscoveryRequest) -> Iterable[CandidatePage]:
        session = self.session or BrowserSession(launch_config=self.launch_config)
        manager = cast(AbstractContextManager[BrowserSession], session)
        with manager as active:
            page = active.page
            cached_douyin_batches: dict[tuple[int, int], CandidatePayload] | None = (
                {} if request.platform == "douyin" and request.query is not None else None
            )
            for page_number in range(1, request.max_pages + 1):
                source_page = _search_url(request, page_number)
                open_page = active.open
                if request.platform == "douyin" and request.query is not None and page_number > 1:

                    def advance_page(
                        url: str,
                        adapter: PlatformAdapter,
                        consume: Callable[[str, object], None],
                    ) -> None:
                        del url
                        active.observe(adapter, consume)
                        try:
                            page.mouse.wheel(0, 10000)
                        except Exception:
                            raise BrowserSessionError("browser_navigation_failed") from None

                    open_page = advance_page
                for payload in _capture_candidate_payloads(
                    page,
                    request,
                    source_page,
                    page_number,
                    open_page,
                    active.wait_for_response_processing,
                    active.raise_if_response_failed,
                    cached_douyin_batches,
                ):
                    yield CandidatePage(page_number, source_page, payload)


def _validate_launch_config(
    request: DiscoveryRequest,
    output_root: Path,
    source: CandidatePageSource,
    launch_config: BrowserLaunchConfig,
) -> None:
    if (
        launch_config.platform not in {None, request.platform}
        or launch_config.output_root not in {None, output_root}
        or source.browser != launch_config.browser
        or source.session_mode != launch_config.session_mode
    ):
        raise ValueError("launch_config_request_mismatch")


def _parser(
    platform: Platform,
) -> Callable[..., list[CandidateVideo]]:
    if platform == "bilibili":
        return parse_bilibili_candidates
    return parse_douyin_candidates


def _validate_source_page(request: DiscoveryRequest, candidate_page: CandidatePage) -> None:
    def without_pagination(url: str) -> str:
        parts = urlsplit(url)
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key not in {"page", "offset", "count"}
        ]
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )

    def source_page_number(url: str) -> int:
        raw_page = _one_query_value(url, "page")
        raw_offset = _one_query_value(url, "offset")
        raw_count = _one_query_value(url, "count")
        if raw_page is not None:
            if raw_offset is not None or raw_count is not None:
                raise ValueError("ambiguous_candidate_pagination")
            page_number = int(raw_page)
            if page_number < 1:
                raise ValueError("invalid_candidate_page")
            return page_number
        if raw_offset is None and raw_count is None:
            return 1
        if raw_offset is None or raw_count is None:
            raise ValueError("invalid_candidate_offset")
        offset = int(raw_offset)
        count = int(raw_count)
        if offset < 0 or count < 1 or offset % count:
            raise ValueError("invalid_candidate_offset")
        return offset // count + 1

    expected_page = without_pagination(_search_url(request, 1))
    actual_page = without_pagination(candidate_page.source_page)
    try:
        _validate_navigated_url(
            request.platform,
            expected_page,
            actual_page,
            require_exact_query=request.source_url is not None,
        )
        page_number = source_page_number(candidate_page.source_page)
    except (TypeError, ValueError):
        raise ValueError("candidate_source_page_mismatch") from None
    if page_number != candidate_page.page_number:
        raise ValueError("candidate_source_page_mismatch")


def _encode(model: Contract) -> bytes:
    return json.dumps(
        model.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _discard(directory: Path) -> None:
    for name in ("manifest.json", "discovery.json"):
        try:
            (directory / name).unlink(missing_ok=True)
        except OSError:
            pass
    try:
        directory.rmdir()
    except OSError:
        pass


def _publish(
    output_root: Path, run_id: str, manifest: CandidateManifest, audit: DiscoveryAudit
) -> tuple[Path, Path]:
    directory = output_root / "candidates" / run_id
    temporary = directory.with_name(f".{run_id}.writing")
    if not _paths_are_safe_under(output_root, directory, temporary):
        raise ValueError("unsafe_discovery_output_path")
    if directory.exists() or directory.is_symlink() or temporary.exists() or temporary.is_symlink():
        raise ValueError("discovery_output_exists")
    try:
        temporary.mkdir(parents=True)
        if not _paths_are_safe_under(output_root, directory, temporary):
            raise ValueError("unsafe_discovery_output_path")
        manifest_path = temporary / "manifest.json"
        discovery_path = temporary / "discovery.json"
        manifest_path.write_bytes(_encode(manifest))
        discovery_path.write_bytes(_encode(audit))
        CandidateManifest.model_validate_json(manifest_path.read_bytes())
        DiscoveryAudit.model_validate_json(discovery_path.read_bytes())
        temporary.replace(directory)
    except Exception:
        _discard(temporary)
        raise
    return directory / "manifest.json", directory / "discovery.json"


def discover(
    request: DiscoveryRequest,
    browser: CandidatePageSource | None = None,
    output_root: Path = Path(".local-data"),
    batch_runner: object | None = None,
    *,
    launch_config: BrowserLaunchConfig | None = None,
    browser_session: BrowserSession | None = None,
) -> DiscoveryResult:
    """Discover candidates and publish review-only artifacts after complete validation."""

    del batch_runner
    config = launch_config or BrowserLaunchConfig()
    if browser is not None and browser_session is not None:
        raise ValueError("multiple_discovery_browsers")
    source = browser or BrowserCandidateSource(config, browser_session)
    _validate_launch_config(request, output_root, source, config)

    discovered_at = datetime.now(UTC)
    candidates: list[CandidateVideo] = []
    seen: set[tuple[Platform, str]] = set()
    processed_pages: set[int] = set()
    parse = _parser(request.platform)
    for candidate_page in source.pages(request):
        if candidate_page.page_number > request.max_pages:
            break
        _validate_source_page(request, candidate_page)
        parsed = parse(
            candidate_page.payload,
            request.query,
            candidate_page.source_page,
            discovered_at,
            direction=request.direction,
        )
        processed_pages.add(candidate_page.page_number)
        for item in parsed:
            key = (item.platform, item.video_key)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(item)
            if len(candidates) == request.max_results:
                break
        if len(candidates) == request.max_results:
            break

    if not candidates:
        raise CandidateShapeChanged("candidate_shape_changed")

    manifest = CandidateManifest(candidates=candidates)
    audit = DiscoveryAudit(
        platform=request.platform,
        direction=request.direction,
        source_kind="source_url" if request.source_url is not None else "query",
        max_pages=request.max_pages,
        max_results=request.max_results,
        pages_processed=len(processed_pages),
        candidate_count=len(candidates),
        browser=source.browser,
        session_mode=source.session_mode,
    )
    manifest_path, discovery_path = _publish(output_root, uuid4().hex, manifest, audit)
    return DiscoveryResult(candidates, len(processed_pages), manifest_path, discovery_path)


__all__ = [
    "BrowserCandidateSource",
    "CandidatePage",
    "CandidatePageSource",
    "DiscoveryRequest",
    "DiscoveryResult",
    "deduplicate_candidates",
    "discover",
]
