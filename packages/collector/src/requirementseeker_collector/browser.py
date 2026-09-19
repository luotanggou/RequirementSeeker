"""Ephemeral headed browsing and visible, deterministic page actions."""

import os
import re
import stat
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from types import TracebackType
from typing import Literal, Protocol, Self

from playwright.sync_api import BrowserContext, Page, Playwright, Request, Response, sync_playwright

from requirementseeker_collector.adapters.base import (
    PlatformAdapter,
    ResponseKind,
    ResponseShapeChanged,
)
from requirementseeker_collector.contracts import Platform, Stratum


class BrowserSessionError(RuntimeError):
    """A fixed browser failure category without underlying response details."""


class PlaywrightStarter(Protocol):
    def start(self) -> Playwright: ...


type BrowserName = Literal["chromium", "chrome", "edge"]
type SessionMode = Literal["ephemeral", "dedicated"]


def _is_path_redirect(path: Path) -> bool:
    if path.is_symlink() or path.is_junction():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def dedicated_profile_path(
    output_root: Path, platform: Platform, browser: BrowserName
) -> Path | None:
    """Derive and validate the only profile path a dedicated session may open."""

    if platform not in {"bilibili", "douyin"} or browser not in {"chrome", "edge"}:
        return None
    profile = output_root / "browser-profiles" / platform / browser
    reserved = tuple(
        output_root / name for name in ("raw", ".staging", ".backup", "runs", "challenges")
    )
    try:
        lexical_root = Path(os.path.abspath(output_root))
        lexical_profile = Path(os.path.abspath(profile))
        relative = lexical_profile.relative_to(lexical_root)
        current = lexical_root
        if _is_path_redirect(current):
            return None
        for part in relative.parts:
            current /= part
            if _is_path_redirect(current):
                return None
        resolved_root = lexical_root.resolve(strict=False)
        resolved_profile = lexical_profile.resolve(strict=False)
        if not resolved_profile.is_relative_to(resolved_root):
            return None
        for path in reserved:
            resolved_reserved = path.resolve(strict=False)
            if resolved_profile.is_relative_to(
                resolved_reserved
            ) or resolved_reserved.is_relative_to(resolved_profile):
                return None
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved_profile


@dataclass(frozen=True)
class BrowserLaunchConfig:
    """Explicit browser selection without access to an existing personal profile."""

    browser: BrowserName = "chromium"
    output_root: Path | None = None
    platform: Platform | None = None

    def __post_init__(self) -> None:
        valid = (
            self.browser == "chromium" and self.output_root is None and self.platform is None
        ) or (
            self.browser in {"chrome", "edge"}
            and self.output_root is not None
            and self.platform is not None
            and dedicated_profile_path(self.output_root, self.platform, self.browser) is not None
        )
        if not valid:
            raise ValueError("invalid_browser_launch_config")

    @property
    def session_mode(self) -> SessionMode:
        return "ephemeral" if self.browser == "chromium" else "dedicated"

    @property
    def user_data_dir(self) -> Path | None:
        if self.output_root is None or self.platform is None:
            return None
        return dedicated_profile_path(self.output_root, self.platform, self.browser)


class BrowserSession:
    def __init__(
        self,
        playwright_factory: Callable[[], PlaywrightStarter] = sync_playwright,
        launch_config: BrowserLaunchConfig | None = None,
    ) -> None:
        self._factory = playwright_factory
        self._launch_config = launch_config or BrowserLaunchConfig()
        self._stack = ExitStack()
        self._cleanup_failed = False
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._response_callback: Callable[[Response], None] | None = None
        self._request_callback: Callable[[Request], None] | None = None
        self._request_finished_callback: Callable[[Request], None] | None = None
        self._request_failed_callback: Callable[[Request], None] | None = None
        self._supported_requests: set[int] = set()
        self._response_activity = 0
        self._response_shape_changed = False
        self._response_processing_failed = False

    @property
    def page(self) -> Page:
        if self._page is None:
            raise BrowserSessionError("browser_not_open")
        return self._page

    def _close(self, close: Callable[[], object]) -> None:
        try:
            close()
        except Exception:
            self._cleanup_failed = True

    def raise_if_response_failed(self) -> None:
        if self._response_shape_changed:
            raise ResponseShapeChanged("response_shape_changed") from None
        if self._response_processing_failed:
            raise BrowserSessionError("response_processing_failed") from None

    def wait_for_response_processing(
        self, *, quiet_seconds: float = 0.75, timeout_seconds: float = 5.0
    ) -> None:
        """Wait until supported response activity is quiet and all requests have finished."""

        deadline = monotonic() + timeout_seconds
        quiet_deadline = monotonic() + quiet_seconds
        activity = self._response_activity
        while monotonic() < deadline:
            self.page.wait_for_timeout(50)
            self.raise_if_response_failed()
            if self._response_activity != activity:
                activity = self._response_activity
                quiet_deadline = monotonic() + quiet_seconds
            if not self._supported_requests and monotonic() >= quiet_deadline:
                return
        raise BrowserSessionError("response_processing_timeout")

    def __enter__(self) -> Self:
        started = False
        try:
            runtime = self._factory().start()
            self._stack.callback(self._close, runtime.stop)
            if self._launch_config.session_mode == "dedicated":
                user_data_dir = self._launch_config.user_data_dir
                if user_data_dir is None:
                    raise ValueError("dedicated_profile_required")
                channel = "chrome" if self._launch_config.browser == "chrome" else "msedge"
                context = runtime.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    headless=False,
                    channel=channel,
                )
                self._stack.callback(self._close, context.close)
                self._context = context
                self._page = context.new_page()
            else:
                if self._launch_config.user_data_dir is not None:
                    raise ValueError("ephemeral_profile_forbidden")
                browser = runtime.chromium.launch(headless=False)
                self._stack.callback(self._close, browser.close)
                context = browser.new_context()
                self._stack.callback(self._close, context.close)
                self._context = context
                self._page = context.new_page()
                self._stack.callback(self._close, self._page.close)
            started = True
        except Exception:
            pass
        finally:
            if not started:
                self._stack.close()
        if not started:
            category = (
                "browser_profile_unavailable"
                if self._launch_config.session_mode == "dedicated"
                else "browser_start_failed"
            )
            raise BrowserSessionError(category)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stack.close()
        self._context = None
        self._page = None
        if exc_type is None:
            self.raise_if_response_failed()
            if self._cleanup_failed:
                raise BrowserSessionError("browser_cleanup_failed")

    def observe(
        self,
        adapter: PlatformAdapter,
        consume: Callable[[str, object], None],
    ) -> None:
        """Replace response observers without navigating the active page."""

        def response_kind(url: str) -> ResponseKind | None:
            try:
                return adapter.response_kind(url)
            except ResponseShapeChanged:
                self._response_shape_changed = True
                return None

        def on_request(request: Request) -> None:
            try:
                if response_kind(request.url) in {"comments", "replies"}:
                    self._supported_requests.add(id(request))
                    self._response_activity += 1
            except Exception:
                self._response_processing_failed = True

        def finish_request(request: Request) -> None:
            if id(request) in self._supported_requests:
                self._supported_requests.remove(id(request))
                self._response_activity += 1

        def on_response(response: Response) -> None:
            try:
                response_url = response.url
                if response_kind(response_url) is not None:
                    payload = response.json()
                    try:
                        consume(response_url, payload)
                    except ResponseShapeChanged:
                        self._response_shape_changed = True
            except Exception:
                self._response_processing_failed = True

        page = self.page
        context = self._context
        if context is None:
            raise BrowserSessionError("browser_not_open")
        shape_changed = False
        response_failed = False
        try:
            if self._response_callback is not None:
                page.remove_listener("response", self._response_callback)
            if self._request_callback is not None:
                context.remove_listener("request", self._request_callback)
            if self._request_finished_callback is not None:
                context.remove_listener("requestfinished", self._request_finished_callback)
            if self._request_failed_callback is not None:
                context.remove_listener("requestfailed", self._request_failed_callback)
            self._supported_requests.clear()
            page.on("response", on_response)
            context.on("request", on_request)
            context.on("requestfinished", finish_request)
            context.on("requestfailed", finish_request)
            self._request_callback = on_request
            self._response_callback = on_response
            self._request_finished_callback = finish_request
            self._request_failed_callback = finish_request
        except ResponseShapeChanged:
            shape_changed = True
        except BrowserSessionError:
            response_failed = True
        except Exception:
            response_failed = True
        if shape_changed:
            raise ResponseShapeChanged("response_shape_changed")
        if response_failed:
            raise BrowserSessionError("response_processing_failed")
        self.raise_if_response_failed()

    def open(
        self,
        url: str,
        adapter: PlatformAdapter,
        consume: Callable[[str, object], None],
    ) -> None:
        page = self.page
        self.observe(adapter, consume)
        navigated = False
        shape_changed = False
        response_failed = False
        try:
            page.goto(url)
            navigated = True
        except ResponseShapeChanged:
            shape_changed = True
        except BrowserSessionError:
            response_failed = True
        except Exception:
            pass
        if shape_changed:
            raise ResponseShapeChanged("response_shape_changed")
        if response_failed:
            raise BrowserSessionError("response_processing_failed")
        self.raise_if_response_failed()
        if not navigated:
            raise BrowserSessionError("browser_navigation_failed")


def perform_stratum_action(page: Page, stratum: Stratum) -> Literal["performed", "unavailable"]:
    failed = False
    result: Literal["performed", "unavailable"] = "unavailable"
    try:
        if stratum == "long_tail":
            page.mouse.wheel(0, 600)
            result = "performed"
        else:
            patterns = {
                "top": r"^(最热|热门)$",
                "recent": r"^(最新|按时间)$",
                "replies": r"^(展开|查看).*回复$",
            }
            label = re.compile(patterns[stratum])
            for role in ("button", "tab", "link"):
                for control in page.get_by_role(role, name=label).all():
                    try:
                        if not control.is_visible():
                            continue
                        control.click()
                    except Exception:
                        continue
                    result = "performed"
                    break
                if result == "performed":
                    break
    except Exception:
        failed = True
    if failed:
        raise BrowserSessionError("stratum_action_failed")
    return result
