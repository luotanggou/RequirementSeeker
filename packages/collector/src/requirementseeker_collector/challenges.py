"""One explicitly supervised mouse action with local, masked page evidence."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from playwright.sync_api import ElementHandle, Page

from requirementseeker_collector.audit import AuditLog


def _validate_action(points: tuple[tuple[int, int], ...], duration: int) -> None:
    if type(duration) is not int or not 1 <= duration <= 10_000:
        raise ValueError("invalid_action")
    for point in points:
        if not isinstance(point, tuple) or len(point) != 2:
            raise ValueError("invalid_action")
        if any(type(value) is not int or not 0 <= value <= 100_000 for value in point):
            raise ValueError("invalid_action")


@dataclass(frozen=True)
class DragAction:
    start: tuple[int, int]
    end: tuple[int, int]
    duration: int

    def __post_init__(self) -> None:
        _validate_action((self.start, self.end), self.duration)


@dataclass(frozen=True)
class ClickAction:
    point: tuple[int, int]
    duration: int = 100

    def __post_init__(self) -> None:
        _validate_action((self.point,), self.duration)


@dataclass(frozen=True)
class ChallengeResult:
    status: Literal["not_confirmed", "already_attempted", "attempted", "failed"]


def _confirm() -> bool:
    return input("Observe the visible challenge; type exact yes for one mouse action: ") == "yes"


_MASK_STYLE = """
input[type="password"], input[autocomplete="current-password"],
input[autocomplete="new-password"], input[autocomplete="one-time-code"],
[data-sensitive] { visibility: hidden !important; }
"""


def _perform_action(page: Page, action: DragAction | ClickAction) -> bool:
    try:
        if isinstance(action, DragAction):
            page.mouse.move(*action.start)
            page.mouse.down()
            try:
                steps = max(1, action.duration // 50)
                for step in range(1, steps + 1):
                    x = action.start[0] + (action.end[0] - action.start[0]) * step / steps
                    y = action.start[1] + (action.end[1] - action.start[1]) * step / steps
                    page.mouse.move(x, y)
                    page.wait_for_timeout(action.duration / steps)
            finally:
                page.mouse.up()
        else:
            page.mouse.click(*action.point, delay=action.duration)
    except (Exception, KeyboardInterrupt):
        return False
    return True


class ChallengeHandler:
    """Own one challenge directory and allow at most one confirmed attempt."""

    def __init__(self, directory: Path, *, confirm: Callable[[], bool] = _confirm) -> None:
        self._directory = directory
        self._confirm = confirm
        self._attempted = False

    def _prepare_directory(self) -> bool:
        try:
            if str(self._directory).startswith(("\\\\", "//")):
                return False
            if self._directory.is_symlink():
                return False
            self._directory = self._directory.resolve()
            if str(self._directory).startswith(("\\\\", "//")):
                return False
            for name in ("before.png", "after.png", "actions.jsonl", ".actions.jsonl.tmp"):
                target = self._directory / name
                if target.exists() or target.is_symlink():
                    return False
            self._directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        return True

    def attempt(self, page: Page, action: DragAction | ClickAction) -> ChallengeResult:
        if self._attempted:
            return ChallengeResult("already_attempted")
        confirmed = False
        try:
            confirmed = self._confirm() is True
        except (Exception, KeyboardInterrupt):
            pass
        if not confirmed:
            return ChallengeResult("not_confirmed")
        self._attempted = True
        if type(action) not in (DragAction, ClickAction):
            return ChallengeResult("failed")
        if not self._prepare_directory():
            return ChallengeResult("failed")

        status: Literal["attempted", "failed"] = "failed"
        styles: list[ElementHandle] = []
        try:
            for frame in page.frames:
                styles.append(frame.add_style_tag(content=_MASK_STYLE))
            page.screenshot(path=self._directory / "before.png", full_page=False)
            acted = _perform_action(page, action)
            for frame in page.frames:
                styles.append(frame.add_style_tag(content=_MASK_STYLE))
            page.screenshot(path=self._directory / "after.png", full_page=False)
            status = "attempted" if acted else "failed"
        except (Exception, KeyboardInterrupt):
            pass
        finally:
            for style in styles:
                try:
                    style.evaluate("element => element.remove()")
                except Exception:
                    pass

        fields: dict[str, object] = {
            "time": datetime.now(UTC).isoformat(),
            "action_type": "drag" if isinstance(action, DragAction) else "click",
            "coordinates": [action.start, action.end]
            if isinstance(action, DragAction)
            else [action.point],
            "duration": action.duration,
            "result": status,
        }
        try:
            AuditLog(self._directory / "actions.jsonl").write("challenge_action", fields)
        except Exception:
            status = "failed"
        return ChallengeResult(status)
