"""Bounded, auditable candidate discovery without automatic collection."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol, Self, cast
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from playwright.sync_api import Page
from pydantic import Field, TypeAdapter, ValidationError, model_validator

from ..adapters.base import PlatformAdapter, ResponseKind, ResponseShapeChanged
from ..browser import BrowserLaunchConfig, BrowserName, BrowserSession, SessionMode
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
        parts = urlsplit(str(request.source_url))
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        if page_number > 1:
            query["page"] = str(page_number)
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )
    assert request.query is not None
    encoded = quote(request.query, safe="")
    if request.platform == "bilibili":
        return f"https://search.bilibili.com/all?keyword={encoded}&page={page_number}"
    suffix = "" if page_number == 1 else f"?page={page_number}"
    return f"https://www.douyin.com/search/{encoded}{suffix}"


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

    def response_kind(self, url: str) -> ResponseKind | None:
        if not _is_candidate_endpoint(self.platform, url) or self.expected_query is None:
            return None
        if _one_query_value(url, "keyword") != self.expected_query:
            return None
        if self.platform == "bilibili":
            return "comments" if _one_query_value(url, "page") == str(self.page_number) else None
        raw_offset = _one_query_value(url, "offset")
        raw_count = _one_query_value(url, "count")
        try:
            offset = int(raw_offset) if raw_offset is not None else -1
            count = int(raw_count) if raw_count is not None else 0
        except ValueError:
            return None
        expected_offset = (self.page_number - 1) * count
        return "comments" if count > 0 and offset == expected_offset else None


def _dom_payload(page: Page, platform: Platform) -> CandidatePayload | None:
    """Read only explicitly labelled candidate metadata rows, never page text."""

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
        if platform == "bilibili":
            values.append({"bvid": video_key, "title": title, "review": comment_count})
        else:
            values.append(
                {
                    "aweme_info": {
                        "aweme_id": video_key,
                        "desc": title,
                        "statistics": {"comment_count": comment_count},
                    }
                }
            )
    if platform == "bilibili":
        return {"code": 0, "data": {"result": values}}
    return {"status_code": 0, "data": values}


def _validate_navigated_url(platform: Platform, requested_url: str, actual_url: str) -> None:
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
    if (
        requested.host is None
        or actual_host != requested.host
        or actual_path != requested_path
        or any(actual_query.get(key) != values for key, values in requested_query.items())
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
) -> list[CandidatePayload]:
    captured: list[CandidatePayload] = []

    def consume(response_url: str, payload: object) -> None:
        del response_url
        if not isinstance(payload, Mapping):
            raise ResponseShapeChanged("response_shape_changed")
        captured.append(payload)

    matcher = _CandidateResponseAdapter(
        request.platform,
        _expected_query(request, source_page),
        page_number,
    )
    open_page(source_page, cast(PlatformAdapter, matcher), consume)
    _validate_navigated_url(request.platform, source_page, page.url)
    wait_for_response_processing(quiet_seconds=0.75, timeout_seconds=5.0)
    _validate_navigated_url(request.platform, source_page, page.url)
    if not captured:
        dom = _dom_payload(page, request.platform)
        if dom is not None:
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
            for page_number in range(1, request.max_pages + 1):
                source_page = _search_url(request, page_number)
                for payload in _capture_candidate_payloads(
                    page,
                    request,
                    source_page,
                    page_number,
                    active.open,
                    active.wait_for_response_processing,
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
        _validate_navigated_url(request.platform, expected_page, actual_page)
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
