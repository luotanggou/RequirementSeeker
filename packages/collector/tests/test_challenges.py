import json
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from requirementseeker_collector.challenges import ChallengeHandler, ClickAction, DragAction


def fake_challenge_page():
    page = Mock()
    events = []

    def style(*, content):
        assert 'input[type="password"]' in content
        assert "visibility: hidden !important" in content
        events.append("masked")
        return Mock()

    def screenshot(*, path, full_page):
        assert events[-1] == "masked"
        assert full_page is False
        Path(path).write_bytes(b"fake-page-image")
        events.append(Path(path).name)

    page.add_style_tag.side_effect = style
    page.frames = [page]
    page.screenshot.side_effect = screenshot
    return page


def test_challenge_requires_live_confirmation(tmp_path):
    page = fake_challenge_page()
    handler = ChallengeHandler(tmp_path, confirm=lambda: False)
    assert handler.attempt(page, DragAction((10, 20), (80, 20), 750)).status == "not_confirmed"
    assert not page.mock_calls
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("error", [EOFError(), KeyboardInterrupt(), RuntimeError("secret-marker")])
def test_confirmation_failure_has_no_action_artifacts_or_sensitive_error(tmp_path, error):
    page = fake_challenge_page()
    handler = ChallengeHandler(tmp_path, confirm=Mock(side_effect=error))
    assert handler.attempt(page, ClickAction((10, 20))).status == "not_confirmed"
    assert not page.mock_calls
    assert list(tmp_path.iterdir()) == []


def test_network_output_path_is_rejected_without_filesystem_or_page_access(monkeypatch):
    page = fake_challenge_page()
    resolve = Mock(side_effect=AssertionError("must not access network path"))
    monkeypatch.setattr(Path, "resolve", resolve)
    handler = ChallengeHandler(Path("//server/share/challenge"), confirm=lambda: True)
    assert handler.attempt(page, ClickAction((10, 20))).status == "failed"
    resolve.assert_not_called()
    assert not page.mock_calls


@pytest.mark.parametrize("answer", ["no", "YES", " yes", "yes ", "", "y"])
def test_cli_confirmation_requires_exact_yes(tmp_path, monkeypatch, answer):
    page = fake_challenge_page()
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    assert ChallengeHandler(tmp_path).attempt(page, ClickAction((10, 20))).status == "not_confirmed"
    assert not page.mock_calls
    assert list(tmp_path.iterdir()) == []


def test_challenge_attempts_once_and_writes_safe_audit(tmp_path, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    page = fake_challenge_page()
    handler = ChallengeHandler(tmp_path)
    action = DragAction((10, 20), (80, 20), 750)
    assert handler.attempt(page, action).status == "attempted"
    assert handler.attempt(page, action).status == "already_attempted"
    page.mouse.down.assert_called_once_with()
    page.mouse.up.assert_called_once_with()
    assert page.mouse.move.call_args_list[0].args == (10, 20)
    assert page.mouse.move.call_args_list[-1].args == (80, 20)
    assert sum(call.args[0] for call in page.wait_for_timeout.call_args_list) == 750
    assert not hasattr(handler, "retry")
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "actions.jsonl",
        "after.png",
        "before.png",
    ]
    entry = json.loads((tmp_path / "actions.jsonl").read_text())
    assert entry["event"] == "challenge_action"
    assert set(entry["fields"]) == {"time", "action_type", "coordinates", "duration", "result"}
    assert datetime.fromisoformat(entry["fields"]["time"]).tzinfo is not None
    assert entry["fields"]["result"] == "attempted"
    assert entry["fields"]["coordinates"] == [[10, 20], [80, 20]]
    page.on.assert_not_called()


def test_click_is_one_action(tmp_path):
    page = fake_challenge_page()
    handler = ChallengeHandler(tmp_path, confirm=lambda: True)
    assert handler.attempt(page, ClickAction((10, 20), 100)).status == "attempted"
    page.mouse.click.assert_called_once_with(10, 20, delay=100)
    page.mouse.down.assert_not_called()


@pytest.mark.parametrize("stage", ["mask", "before", "mouse", "after", "audit"])
def test_failure_is_safe_and_never_retried(tmp_path, monkeypatch, stage):
    page = fake_challenge_page()
    failure = RuntimeError("password=secret-marker; token=secret-marker")
    if stage == "mask":
        page.add_style_tag.side_effect = failure
    elif stage in ("before", "after"):
        original = page.screenshot.side_effect

        def screenshot(**kwargs):
            if Path(kwargs["path"]).name == f"{stage}.png":
                raise failure
            return original(**kwargs)

        page.screenshot.side_effect = screenshot
    elif stage == "mouse":
        page.mouse.move.side_effect = [None, failure]
    else:
        monkeypatch.setattr(
            "requirementseeker_collector.challenges.AuditLog.write", Mock(side_effect=failure)
        )
    handler = ChallengeHandler(tmp_path, confirm=lambda: True)
    action = DragAction((10, 20), (80, 20), 750)
    result = handler.attempt(page, action)
    assert result.status == "failed"
    assert "secret-marker" not in repr(result)
    assert handler.attempt(page, action).status == "already_attempted"
    for path in tmp_path.iterdir():
        assert b"secret-marker" not in path.read_bytes()
    if stage in ("mask", "before"):
        assert not page.mouse.mock_calls
    if stage == "mouse":
        page.mouse.up.assert_called_once_with()
        assert (tmp_path / "after.png").exists()


def test_masks_passwords_in_child_frames_before_both_screenshots(tmp_path):
    page = fake_challenge_page()
    child_frame = Mock()
    page.frames.append(child_frame)
    result = ChallengeHandler(tmp_path, confirm=lambda: True).attempt(page, ClickAction((10, 20)))
    assert result.status == "attempted"
    assert child_frame.add_style_tag.call_count == 2
    for call in child_frame.add_style_tag.call_args_list:
        assert 'input[type="password"]' in call.kwargs["content"]


def test_unknown_action_is_rejected_without_artifacts(tmp_path):
    page = fake_challenge_page()
    result = ChallengeHandler(tmp_path, confirm=lambda: True).attempt(page, Mock())
    assert result.status == "failed"
    assert not page.mock_calls
    assert list(tmp_path.iterdir()) == []


def test_existing_artifact_is_not_overwritten_or_followed(tmp_path):
    existing = tmp_path / "before.png"
    existing.write_bytes(b"prior")
    page = fake_challenge_page()
    result = ChallengeHandler(tmp_path, confirm=lambda: True).attempt(page, ClickAction((10, 20)))
    assert result.status == "failed"
    assert existing.read_bytes() == b"prior"
    assert not page.mock_calls


def test_output_directory_can_be_created_but_no_child_path_is_caller_controlled(tmp_path):
    target = tmp_path / "challenge-1"
    page = fake_challenge_page()
    assert (
        ChallengeHandler(target, confirm=lambda: True).attempt(page, ClickAction((10, 20))).status
        == "attempted"
    )
    assert {p.name for p in target.iterdir()} == {"before.png", "after.png", "actions.jsonl"}


@pytest.mark.parametrize("point", [(-1, 20), (True, 20), (float("nan"), 20)])
def test_invalid_coordinates_are_rejected(point):
    with pytest.raises(ValueError, match="invalid_action"):
        ClickAction(point)


@pytest.mark.parametrize("duration", [0, -1, True, float("inf"), 10**400])
def test_invalid_or_unbounded_duration_is_rejected(duration):
    with pytest.raises(ValueError, match="invalid_action"):
        DragAction((10, 20), (80, 20), duration)
