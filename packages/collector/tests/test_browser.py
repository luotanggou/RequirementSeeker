import re
import traceback
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock

import pytest

from requirementseeker_collector.adapters.base import ResponseShapeChanged
from requirementseeker_collector.browser import (
    BrowserSession,
    BrowserSessionError,
    perform_stratum_action,
)


def assert_safe_exception(error, expected_type, expected_message):
    assert type(error) is expected_type
    assert str(error) == expected_message
    assert error.__context__ is None
    assert error.__cause__ is None
    assert "secret-marker" not in "".join(traceback.format_exception(error))


def fake_playwright():
    events = []
    page = Mock()
    context = Mock()
    browser = Mock()
    runtime = Mock()
    manager = Mock()
    manager.start.return_value = runtime
    runtime.chromium.launch.return_value = browser
    browser.new_context.return_value = context
    context.new_page.return_value = page
    for name, resource in [("page", page), ("context", context), ("browser", browser)]:
        resource.close.side_effect = lambda name=name: events.append(name)
    runtime.stop.side_effect = lambda: events.append("playwright")
    return SimpleNamespace(
        factory=Mock(return_value=manager),
        manager=manager,
        runtime=runtime,
        browser=browser,
        context=context,
        page=page,
        events=events,
    )


def test_browser_launch_is_headed_and_not_persistent():
    fake = fake_playwright()
    with BrowserSession(fake.factory) as session:
        assert session.page is fake.page
    fake.runtime.chromium.launch.assert_called_once_with(headless=False)
    fake.browser.new_context.assert_called_once_with()
    fake.runtime.chromium.launch_persistent_context.assert_not_called()
    assert fake.events == ["page", "context", "browser", "playwright"]


@pytest.mark.parametrize("stage", ["launch", "context", "page"])
def test_start_failure_cleans_up_acquired_resources(stage):
    fake = fake_playwright()
    calls = {
        "launch": fake.runtime.chromium.launch,
        "context": fake.browser.new_context,
        "page": fake.context.new_page,
    }
    calls[stage].side_effect = RuntimeError("secret-marker")
    with pytest.raises(BrowserSessionError) as caught:
        with BrowserSession(fake.factory):
            pytest.fail("must not enter")
    assert caught.value.__context__ is None
    assert "secret-marker" not in str(caught.value)
    assert (
        fake.events
        == {
            "launch": ["playwright"],
            "context": ["browser", "playwright"],
            "page": ["context", "browser", "playwright"],
        }[stage]
    )


def test_close_failure_does_not_prevent_other_cleanup():
    fake = fake_playwright()
    fake.page.close.side_effect = RuntimeError("secret-marker")
    with pytest.raises(BrowserSessionError) as caught:
        with BrowserSession(fake.factory):
            pass
    assert fake.events == ["context", "browser", "playwright"]
    assert caught.value.__context__ is None
    assert "secret-marker" not in str(caught.value)


@pytest.mark.parametrize("cleanup_also_fails", [False, True])
@pytest.mark.parametrize("failure_kind", ["shape", "processing"])
def test_response_failure_arriving_during_page_close_has_priority(cleanup_also_fails, failure_kind):
    fake = fake_playwright()
    response = Mock(url="https://example.test/comments")
    response.json.return_value = {"unexpected": []}
    consumer = Mock()
    expected_type = BrowserSessionError
    expected_message = "response_processing_failed"
    if failure_kind == "shape":
        consumer.side_effect = ResponseShapeChanged("secret-marker")
        expected_type = ResponseShapeChanged
        expected_message = "response_shape_changed"
    else:
        response.json.side_effect = RuntimeError("secret-marker")

    def close_page():
        fake.events.append("page")
        fake.page.on.call_args.args[1](response)
        if cleanup_also_fails:
            raise RuntimeError("cleanup-secret-marker")

    fake.page.close.side_effect = close_page

    with pytest.raises(expected_type) as caught:
        with BrowserSession(fake.factory) as session:
            session.open("https://example.test/video", Mock(), consumer)

    assert_safe_exception(caught.value, expected_type, expected_message)
    assert fake.events == ["page", "context", "browser", "playwright"]


def test_body_failure_still_closes_all_resources():
    fake = fake_playwright()
    with pytest.raises(ValueError, match="caller_failure"):
        with BrowserSession(fake.factory):
            raise ValueError("caller_failure")
    assert fake.events == ["page", "context", "browser", "playwright"]


def test_response_callback_precedes_navigation_and_only_forwards_supported_json():
    fake = fake_playwright()
    response = Mock(url="https://example.test/comments")
    response.json.return_value = {"comments": []}
    adapter = Mock()
    adapter.response_kind.side_effect = lambda url: "comments" if url == response.url else None
    consumer = Mock()
    callbacks = []
    fake.page.on.side_effect = lambda event, callback: callbacks.append((event, callback))

    def navigate(url):
        assert callbacks[0][0] == "response"
        callbacks[0][1](response)
        unknown = Mock(url="https://example.test/other")
        callbacks[0][1](unknown)
        unknown.json.assert_not_called()

    fake.page.goto.side_effect = navigate
    with BrowserSession(fake.factory) as session:
        session.open("https://example.test/video", adapter, consumer)
    consumer.assert_called_once_with(response.url, {"comments": []})
    assert response.mock_calls == [(("json"), (), {})]


def test_bad_response_json_has_safe_error():
    fake = fake_playwright()
    response = Mock(url="https://example.test/comments")
    response.json.side_effect = ValueError("secret-marker")
    with pytest.raises(BrowserSessionError) as caught:
        with BrowserSession(fake.factory) as session:
            session.open("https://example.test/video", Mock(), Mock())
            callback = fake.page.on.call_args.args[1]
            callback(response)
            session.raise_if_response_failed()
    assert caught.value.__context__ is None
    assert "secret-marker" not in str(caught.value)


def test_repeated_navigation_does_not_duplicate_consumers():
    fake = fake_playwright()
    with BrowserSession(fake.factory) as session:
        session.open("https://example.test/first", Mock(), Mock())
        first_callback = fake.page.on.call_args.args[1]
        session.open("https://example.test/second", Mock(), Mock())
        fake.page.remove_listener.assert_called_once_with("response", first_callback)


def test_open_outside_active_session_preserves_not_open_category():
    session = BrowserSession(Mock())
    with pytest.raises(BrowserSessionError) as caught:
        session.open("https://example.test/video", Mock(), Mock())
    assert_safe_exception(caught.value, BrowserSessionError, "browser_not_open")


def test_navigation_failure_has_no_sensitive_context():
    fake = fake_playwright()
    fake.page.goto.side_effect = RuntimeError("secret-marker")
    with pytest.raises(BrowserSessionError) as caught:
        with BrowserSession(fake.factory) as session:
            session.open("https://example.test/video", Mock(), Mock())
    assert caught.value.__context__ is None
    assert "secret-marker" not in str(caught.value)
    assert fake.events == ["page", "context", "browser", "playwright"]


def test_navigation_preserves_adapter_shape_failure_category():
    fake = fake_playwright()
    response = Mock(url="https://example.test/comments")
    consumer = Mock(side_effect=ResponseShapeChanged("response_shape_changed"))
    fake.page.goto.side_effect = lambda url: fake.page.on.call_args.args[1](response)
    with pytest.raises(ResponseShapeChanged):
        with BrowserSession(fake.factory) as session:
            session.open("https://example.test/video", Mock(), consumer)


@pytest.mark.parametrize("stage", ["response_url", "response_kind", "response_json", "consumer"])
@pytest.mark.parametrize("timing", ["after_open", "during_goto"])
def test_response_processing_failure_has_safe_error(stage, timing):
    fake = fake_playwright()
    response = Mock()
    type(response).url = PropertyMock(return_value="https://example.test/comments")
    adapter = Mock()
    adapter.response_kind.return_value = "comments"
    response.json.return_value = {"comments": []}
    consumer = Mock()

    if stage == "response_url":
        type(response).url = PropertyMock(side_effect=RuntimeError("secret-marker"))
    elif stage == "response_kind":
        adapter.response_kind.side_effect = RuntimeError("secret-marker")
    elif stage == "response_json":
        response.json.side_effect = RuntimeError("secret-marker")
    else:
        consumer.side_effect = RuntimeError("secret-marker")

    if timing == "during_goto":
        fake.page.goto.side_effect = lambda url: fake.page.on.call_args.args[1](response)

    with pytest.raises(BrowserSessionError) as caught:
        with BrowserSession(fake.factory) as session:
            session.open("https://example.test/video", adapter, consumer)
            if timing == "after_open":
                fake.page.on.call_args.args[1](response)
                session.raise_if_response_failed()

    assert_safe_exception(caught.value, BrowserSessionError, "response_processing_failed")


@pytest.mark.parametrize("stage", ["response_kind", "consumer"])
@pytest.mark.parametrize("timing", ["after_open", "during_goto"])
def test_adapter_shape_failure_has_fixed_category(stage, timing):
    fake = fake_playwright()
    response = Mock(url="https://example.test/comments")
    response.json.return_value = {"comments": []}
    adapter = Mock()
    adapter.response_kind.return_value = "comments"
    consumer = Mock()

    if stage == "response_kind":
        adapter.response_kind.side_effect = ResponseShapeChanged("secret-marker")
    else:
        consumer.side_effect = ResponseShapeChanged("secret-marker")

    if timing == "during_goto":
        fake.page.goto.side_effect = lambda url: fake.page.on.call_args.args[1](response)

    with pytest.raises(ResponseShapeChanged) as caught:
        with BrowserSession(fake.factory) as session:
            session.open("https://example.test/video", adapter, consumer)
            if timing == "after_open":
                fake.page.on.call_args.args[1](response)
                session.raise_if_response_failed()

    assert_safe_exception(caught.value, ResponseShapeChanged, "response_shape_changed")


@pytest.mark.parametrize(
    ("stratum", "label"),
    [
        ("top", "最热"),
        ("top", "热门"),
        ("recent", "最新"),
        ("recent", "按时间"),
        ("replies", "展开 2 条回复"),
        ("replies", "查看回复"),
    ],
)
def test_stratum_action_uses_visible_controls(stratum, label):
    page = Mock()
    control = Mock()
    control.is_visible.return_value = True

    def by_role(role, *, name):
        assert role in ("button", "tab", "link")
        assert isinstance(name, re.Pattern)
        return SimpleNamespace(all=lambda: [control] if name.fullmatch(label) else [])

    page.get_by_role.side_effect = by_role
    assert perform_stratum_action(page, stratum) == "performed"
    control.click.assert_called_once_with()


def test_missing_or_hidden_control_is_unavailable():
    page = Mock()
    hidden = Mock()
    hidden.is_visible.return_value = False
    page.get_by_role.return_value.all.return_value = [hidden]
    assert perform_stratum_action(page, "recent") == "unavailable"
    hidden.click.assert_not_called()
    page.locator.assert_not_called()


def test_long_tail_advances_scroll_order():
    page = Mock()
    assert perform_stratum_action(page, "long_tail") == "performed"
    page.mouse.wheel.assert_called_once_with(0, 600)
    page.get_by_role.assert_not_called()


@pytest.mark.parametrize("stage", ["get_by_role", "all", "is_visible", "click", "wheel"])
def test_stratum_action_failure_has_safe_error(stage):
    page = Mock()
    control = Mock()
    control.is_visible.return_value = True
    page.get_by_role.return_value.all.return_value = [control]

    if stage == "get_by_role":
        page.get_by_role.side_effect = RuntimeError("secret-marker")
    elif stage == "all":
        page.get_by_role.return_value.all.side_effect = RuntimeError("secret-marker")
    elif stage == "is_visible":
        control.is_visible.side_effect = RuntimeError("secret-marker")
    elif stage == "click":
        control.click.side_effect = RuntimeError("secret-marker")
    else:
        page.mouse.wheel.side_effect = RuntimeError("secret-marker")

    stratum = "long_tail" if stage == "wheel" else "recent"
    with pytest.raises(BrowserSessionError) as caught:
        perform_stratum_action(page, stratum)

    assert_safe_exception(caught.value, BrowserSessionError, "stratum_action_failed")
