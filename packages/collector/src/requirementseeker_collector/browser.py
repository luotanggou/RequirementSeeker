"""Ephemeral headed browsing and visible, deterministic page actions."""

import re
from collections.abc import Callable
from contextlib import ExitStack
from types import TracebackType
from typing import Literal, Protocol, Self

from playwright.sync_api import Page, Playwright, Response, sync_playwright

from requirementseeker_collector.adapters.base import PlatformAdapter, ResponseShapeChanged
from requirementseeker_collector.contracts import Stratum


class BrowserSessionError(RuntimeError):
    """A fixed browser failure category without underlying response details."""


class PlaywrightStarter(Protocol):
    def start(self) -> Playwright: ...


class BrowserSession:
    def __init__(
        self, playwright_factory: Callable[[], PlaywrightStarter] = sync_playwright
    ) -> None:
        self._factory = playwright_factory
        self._stack = ExitStack()
        self._cleanup_failed = False
        self._page: Page | None = None
        self._response_callback: Callable[[Response], None] | None = None
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

    def __enter__(self) -> Self:
        started = False
        try:
            runtime = self._factory().start()
            self._stack.callback(self._close, runtime.stop)
            browser = runtime.chromium.launch(headless=False)
            self._stack.callback(self._close, browser.close)
            context = browser.new_context()
            self._stack.callback(self._close, context.close)
            self._page = context.new_page()
            self._stack.callback(self._close, self._page.close)
            started = True
        except Exception:
            pass
        finally:
            if not started:
                self._stack.close()
        if not started:
            raise BrowserSessionError("browser_start_failed")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stack.close()
        self._page = None
        if self._cleanup_failed and exc_type is None:
            raise BrowserSessionError("browser_cleanup_failed")

    def open(
        self,
        url: str,
        adapter: PlatformAdapter,
        consume: Callable[[str, object], None],
    ) -> None:
        def on_response(response: Response) -> None:
            try:
                response_url = response.url
                try:
                    response_kind = adapter.response_kind(response_url)
                except ResponseShapeChanged:
                    response_kind = None
                    self._response_shape_changed = True
                if response_kind is not None:
                    payload = response.json()
                    try:
                        consume(response_url, payload)
                    except ResponseShapeChanged:
                        self._response_shape_changed = True
            except Exception:
                self._response_processing_failed = True

        page = self.page
        navigated = False
        shape_changed = False
        response_failed = False
        try:
            if self._response_callback is not None:
                page.remove_listener("response", self._response_callback)
            page.on("response", on_response)
            self._response_callback = on_response
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
                    if control.is_visible():
                        control.click()
                        result = "performed"
                        break
                if result == "performed":
                    break
    except Exception:
        failed = True
    if failed:
        raise BrowserSessionError("stratum_action_failed")
    return result
