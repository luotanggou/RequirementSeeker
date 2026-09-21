"""Single-video orchestration and platform-isolated manifest execution."""

from __future__ import annotations

import json
import os
import selectors
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep
from typing import Literal, Protocol, Self, cast
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from playwright.sync_api import (
    Page,
    Request,
    Response,
    Route,
    WebSocket,
    WebSocketRoute,
    sync_playwright,
)
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from .adapters import BilibiliAdapter, DouyinAdapter, PlatformAdapter, ResponseShapeChanged
from .adapters.bilibili import BilibiliCommentContext
from .artifacts import (
    ArtifactCommitError,
    ArtifactValidationError,
    CommitPaths,
    commit_generation,
    recover_interrupted_commit,
    validate_generation,
    write_generation,
)
from .browser import (
    BrowserLaunchConfig,
    BrowserName,
    BrowserSession,
    BrowserSessionError,
    SessionMode,
    dedicated_profile_path,
    perform_stratum_action,
)
from .challenges import ChallengeHandler, ClickAction, DragAction
from .contracts import (
    CollectionError,
    CollectionManifest,
    CollectionRecord,
    Identifier,
    ManifestVideo,
    Platform,
    PublicHttpUrl,
    RawComment,
    RawVideo,
    Stratum,
)
from .merge import CurrentRunConflict, PreviousRunConflict, merge_comments, merge_current_run
from .planning import collection_target, select_comments

FATAL_PLATFORM_STATUSES = frozenset(
    {"login_failed", "challenge_unresolved", "access_restricted", "response_shape_changed"}
)

_PLATFORM_HOSTS: dict[Platform, tuple[str, ...]] = {
    "bilibili": ("bilibili.com", "b23.tv"),
    "douyin": ("douyin.com", "iesdouyin.com"),
}
_STRATA: tuple[Stratum, ...] = ("top", "recent", "replies", "long_tail")
_PAGINATION_STRATA: tuple[Stratum, ...] = ("replies", "long_tail")
_WINDOWS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)
_WINDOWS_RESERVED_FILENAME_CHARACTERS = frozenset('<>:"/\\|?*')
_REPORT_ERROR_CATEGORIES = frozenset(
    {
        "artifact_cleanup_failed",
        "interrupted_commit_recovered",
        "merge_conflict",
        "no_comments_collected",
        "pagination_round_limit",
        "pagination_stalled",
        "reported_total_unavailable",
    }
)

type SupervisionStatus = Literal[
    "ready", "login_failed", "challenge_unresolved", "access_restricted"
]
type ChallengeAction = DragAction | ClickAction
type RecoveryError = Literal["backup_recovery_failed", "artifact_cleanup_failed"]
_SUPERVISION_STATUSES = frozenset(
    {"ready", "login_failed", "challenge_unresolved", "access_restricted"}
)
_PENDING_MARKER_NAME = ".pending"
_GENERATION_FILES = ("video.json", "comments.jsonl", "collection.json")


class PilotRequest(BaseModel):
    """Validated public input for one visible-browser pilot."""

    model_config = ConfigDict(extra="forbid")

    platform: Platform
    url: PublicHttpUrl
    video_key: Identifier | None = None

    @model_validator(mode="after")
    def host_matches_platform(self) -> Self:
        host = self.url.host
        if host is None or not any(
            host == root or host.endswith(f".{root}") for root in _PLATFORM_HOSTS[self.platform]
        ):
            raise ValueError("url_host_does_not_match_platform")
        return self


@dataclass(frozen=True)
class BrowserResult:
    """Safe normalized output from one browser session."""

    video: RawVideo | None
    comments: list[RawComment]
    pages_requested: int
    pages_succeeded: int
    sort_modes: list[Stratum]
    collection_started_at: datetime
    collection_finished_at: datetime
    collection_errors: list[CollectionError] = field(default_factory=list)
    status: str = "success"
    run_id: str | None = None
    browser: BrowserName = "chromium"
    session_mode: SessionMode = "ephemeral"

    def __post_init__(self) -> None:
        valid = (self.browser == "chromium" and self.session_mode == "ephemeral") or (
            self.browser in {"chrome", "edge"} and self.session_mode == "dedicated"
        )
        if not valid:
            raise ValueError("invalid_browser_session_audit")


@dataclass(frozen=True)
class PageCollectionResult:
    """Validated artifacts produced by the local Playwright integration driver."""

    video: RawVideo
    comments: list[RawComment]
    collection: CollectionRecord


@dataclass(frozen=True)
class PilotResult:
    platform: Platform
    video_key: str
    status: str
    target: int
    collected_total: int

    @classmethod
    def skipped(cls, item: ManifestVideo, status: str = "platform_stopped") -> Self:
        return cls(item.platform, item.video_key, status, 0, 0)

    def to_summary(self) -> dict[str, object]:
        return {
            "collected_total": self.collected_total,
            "platform": self.platform,
            "status": self.status,
            "target": self.target,
            "video_key": self.video_key,
        }


@dataclass(frozen=True)
class PlatformBatchResult:
    status: str
    videos_succeeded: int

    def to_summary(self) -> dict[str, object]:
        return {"status": self.status, "videos_succeeded": self.videos_succeeded}


@dataclass(frozen=True)
class BatchResult:
    results: list[PilotResult]
    platforms: dict[Platform, PlatformBatchResult]
    status: str

    @classmethod
    def from_results(cls, results: Sequence[PilotResult]) -> Self:
        platforms: dict[Platform, PlatformBatchResult] = {}
        for platform in cast(tuple[Platform, ...], ("bilibili", "douyin")):
            platform_results = [item for item in results if item.platform == platform]
            if not platform_results:
                continue
            stopped = any(item.status in FATAL_PLATFORM_STATUSES for item in platform_results)
            platforms[platform] = PlatformBatchResult(
                "stopped" if stopped else "completed",
                sum(item.status == "success" for item in platform_results),
            )
        status = "success" if all(item.status == "success" for item in results) else "partial"
        return cls(list(results), platforms, status)

    def to_summary(self) -> dict[str, object]:
        return {
            "platforms": {
                platform: result.to_summary() for platform, result in self.platforms.items()
            },
            "status": self.status,
            "videos_succeeded": sum(item.status == "success" for item in self.results),
        }


class VideoCollector(Protocol):
    def collect(self, item: ManifestVideo, output_root: Path) -> PilotResult: ...


def video_key_is_safe(value: str) -> bool:
    stem = value.split(".", 1)[0].casefold()
    return (
        bool(value)
        and value not in {".", ".."}
        and not value.endswith(".")
        and stem not in _WINDOWS_DEVICE_NAMES
        and all(
            ord(character) >= 32
            and ord(character) != 127
            and not character.isspace()
            and character not in _WINDOWS_RESERVED_FILENAME_CHARACTERS
            for character in value
        )
    )


def _run_id_is_safe(value: str) -> bool:
    return bool(value) and value.strip() == value and video_key_is_safe(value)


def _challenge_id_is_safe(value: str) -> bool:
    return (
        bool(value)
        and not any(character.isspace() for character in value)
        and video_key_is_safe(value)
    )


def _is_path_redirect(path: Path) -> bool:
    if path.is_symlink() or path.is_junction():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _path_is_safe_under(output_root: Path, target: Path) -> bool:
    """Reject targets escaping through lexical paths or existing filesystem redirects."""

    try:
        lexical_root = Path(os.path.abspath(output_root))
        lexical_target = Path(os.path.abspath(target))
        relative = lexical_target.relative_to(lexical_root)
        current = lexical_root
        if _is_path_redirect(current):
            return False
        for part in relative.parts:
            current /= part
            if _is_path_redirect(current):
                return False
        resolved_root = lexical_root.resolve(strict=False)
        resolved_target = lexical_target.resolve(strict=False)
        return resolved_target == resolved_root or resolved_target.is_relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return False


def _paths_are_safe_under(output_root: Path, *targets: Path) -> bool:
    return all(_path_is_safe_under(output_root, target) for target in targets)


def browser_profile_path(output_root: Path, platform: str, browser: str) -> Path | None:
    """Return the fixed dedicated profile path when every boundary is safe."""

    if platform not in _PLATFORM_HOSTS or browser not in {"chrome", "edge"}:
        return None
    return dedicated_profile_path(output_root, platform, cast(BrowserName, browser))


def manifest_paths_are_safe(manifest: CollectionManifest) -> bool:
    """Reject unsafe or case-insensitively colliding output directory keys."""

    keys: set[tuple[Platform, str]] = set()
    for item in manifest.videos:
        key = (item.platform, item.video_key.casefold())
        if not video_key_is_safe(item.video_key) or key in keys:
            return False
        keys.add(key)
    return True


def _result(request: PilotRequest, video_key: str, status: str) -> PilotResult:
    return PilotResult(request.platform, video_key, status, 0, 0)


def _collection_error(
    category: str, when: datetime, *, comment_id: str | None = None, fields: list[str] | None = None
) -> CollectionError:
    return CollectionError.model_validate(
        {
            "category": category,
            "occurred_at": when,
            "stage": "runner",
            "description": category,
            "raw_comment_id": comment_id,
            "conflict_fields": fields or [],
        }
    )


def _write_run_report(
    output_root: Path,
    run_id: str,
    result: PilotResult,
    browser_result: BrowserResult,
    errors: Sequence[CollectionError],
) -> bool:
    directory = output_root / "runs" / run_id
    temporary = directory.with_name(f".{run_id}.writing")
    if not _paths_are_safe_under(output_root, directory, temporary):
        return False
    try:
        encoded = json.dumps(
            {
                "collected_total": result.collected_total,
                "collection_finished_at": browser_result.collection_finished_at.isoformat(),
                "collection_started_at": browser_result.collection_started_at.isoformat(),
                "errors": [
                    error.category
                    if error.category in _REPORT_ERROR_CATEGORIES
                    else "collection_error"
                    for error in errors
                ],
                "pages_requested": browser_result.pages_requested,
                "pages_succeeded": browser_result.pages_succeeded,
                "platform": result.platform,
                "browser": browser_result.browser,
                "run_version": "1.0",
                "session_mode": browser_result.session_mode,
                "status": result.status,
                "target": result.target,
                "video_key": result.video_key,
            },
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, OverflowError):
        return False
    try:
        if directory.exists() or temporary.exists():
            return False
        temporary.mkdir(parents=True)
        (temporary / "run.json").write_bytes(encoded)
        temporary.replace(directory)
    except OSError:
        try:
            (temporary / "run.json").unlink(missing_ok=True)
            temporary.rmdir()
        except OSError:
            pass
        return False
    return True


def _pending_marker(
    output_root: Path,
    run_id: str,
    platform: Platform,
    video_key: str,
) -> Path:
    return output_root / ".backup" / run_id / platform / f"{video_key}{_PENDING_MARKER_NAME}"


def _create_pending_marker(output_root: Path, marker: Path) -> bool:
    if not _path_is_safe_under(output_root, marker):
        return False
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        if not _path_is_safe_under(output_root, marker):
            return False
        with marker.open("xb") as output:
            return output.write(b"") == 0
    except (OSError, KeyboardInterrupt):
        return False


def _pending_transactions(
    output_root: Path,
    platform: Platform,
    video_key: str,
) -> tuple[list[tuple[Path, Path]], bool]:
    backups_root = output_root / ".backup"
    if not _path_is_safe_under(output_root, backups_root):
        return [], True
    try:
        if not backups_root.exists():
            return [], False
        if not backups_root.is_dir():
            return [], True
        run_directories = list(backups_root.iterdir())
    except OSError:
        return [], True

    transactions: list[tuple[Path, Path]] = []
    for run_directory in run_directories:
        try:
            if (
                not _run_id_is_safe(run_directory.name)
                or not _path_is_safe_under(output_root, run_directory)
                or not run_directory.is_dir()
            ):
                return [], True
            platform_directory = run_directory / platform
            if not platform_directory.exists():
                continue
            marker = platform_directory / f"{video_key}{_PENDING_MARKER_NAME}"
            candidate = platform_directory / video_key
            if (
                not _paths_are_safe_under(output_root, platform_directory, marker, candidate)
                or not platform_directory.is_dir()
            ):
                return [], True
            if not marker.exists():
                continue
            if not marker.is_file() or marker.stat().st_size != 0:
                return [], True
        except OSError:
            return [], True
        transactions.append((candidate, marker))
    return transactions, False


def _remove_empty_parents(path: Path, stop: Path) -> bool:
    current = path
    while current != stop:
        try:
            if not current.exists():
                current = current.parent
                continue
            if any(current.iterdir()):
                return True
            current.rmdir()
        except OSError:
            return False
        current = current.parent
    return True


def _discard_historical_backup(output_root: Path, backup: Path) -> bool:
    if not _path_is_safe_under(output_root, backup):
        return False
    try:
        if not backup.exists():
            return True
        if not backup.is_dir():
            return False
        entries = list(backup.iterdir())
        if any(
            entry.name not in _GENERATION_FILES or not entry.is_file() or _is_path_redirect(entry)
            for entry in entries
        ):
            return False
        if len(entries) == len(_GENERATION_FILES):
            validate_generation(backup)
        for entry in entries:
            entry.unlink()
        backup.rmdir()
    except (ArtifactValidationError, OSError):
        return False
    return True


def _finish_pending_transaction(output_root: Path, backup: Path, marker: Path) -> bool:
    if not _paths_are_safe_under(output_root, backup, marker):
        return False
    if not _discard_historical_backup(output_root, backup):
        return False
    try:
        marker.unlink()
    except OSError:
        return False
    return _remove_empty_parents(marker.parent, output_root / ".backup")


def _cleanup_empty_transaction_parents(output_root: Path, platform: Platform) -> bool:
    backups_root = output_root / ".backup"
    try:
        if not backups_root.exists():
            return True
        run_directories = list(backups_root.iterdir())
    except OSError:
        return False
    for run_directory in run_directories:
        platform_directory = run_directory / platform
        if not _paths_are_safe_under(output_root, run_directory, platform_directory):
            return False
        if not _remove_empty_parents(platform_directory, backups_root):
            return False
    return True


def _clear_completed_transactions(
    output_root: Path,
    platform: Platform,
    video_key: str,
    active_marker: Path,
) -> bool:
    transactions, failed = _pending_transactions(output_root, platform, video_key)
    if failed:
        return False
    if len(transactions) > 1:
        return False
    if any(marker == active_marker for _, marker in transactions):
        return False
    for backup, marker in transactions:
        if not _finish_pending_transaction(output_root, backup, marker):
            return False
    return _cleanup_empty_transaction_parents(output_root, platform)


def _recover_previous_generation(
    output_root: Path,
    platform: Platform,
    video_key: str,
    target: Path,
    staging: Path,
) -> tuple[bool, RecoveryError | None]:
    """Return recovery state and a safe error category for an absent target."""

    if not _paths_are_safe_under(output_root, target, staging):
        return False, "backup_recovery_failed"
    transactions, failed = _pending_transactions(output_root, platform, video_key)
    if failed:
        return False, "backup_recovery_failed"
    if not transactions:
        return False, None
    if len(transactions) != 1:
        return False, "backup_recovery_failed"
    candidate, marker = transactions[0]
    try:
        if not candidate.is_dir():
            return False, "backup_recovery_failed"
        backup_video, _, _ = validate_generation(candidate)
    except (OSError, ArtifactValidationError):
        return False, "backup_recovery_failed"
    if backup_video.platform != platform or backup_video.raw_video_id != video_key:
        return False, "backup_recovery_failed"
    try:
        recovered = recover_interrupted_commit(
            CommitPaths(staging=staging, target=target, backup=candidate)
        )
        if not recovered:
            return False, "backup_recovery_failed"
        validate_generation(target)
    except (ArtifactCommitError, ArtifactValidationError):
        return False, "backup_recovery_failed"
    if not _finish_pending_transaction(output_root, candidate, marker):
        return True, "artifact_cleanup_failed"
    return True, None


def run_pilot(
    request: PilotRequest,
    browser_result: BrowserResult,
    *,
    output_root: Path,
) -> PilotResult:
    """Select, merge, validate, and commit one browser collection result."""

    supplied_run_id = browser_result.run_id
    run_id = (
        supplied_run_id
        if supplied_run_id is not None and _run_id_is_safe(supplied_run_id)
        else uuid4().hex
    )

    def finish(
        result: PilotResult,
        errors: Sequence[CollectionError] = browser_result.collection_errors,
    ) -> PilotResult:
        if _write_run_report(output_root, run_id, result, browser_result, errors):
            return result
        if result.status in {"success", "partial"}:
            return PilotResult(
                result.platform,
                result.video_key,
                "report_write_failed",
                result.target,
                result.collected_total,
            )
        return result

    requested_key = request.video_key or (
        browser_result.video.raw_video_id if browser_result.video is not None else "unknown"
    )
    if browser_result.status in FATAL_PLATFORM_STATUSES:
        return finish(_result(request, requested_key, browser_result.status))
    if browser_result.status not in {"success", "partial"}:
        return finish(_result(request, requested_key, "collection_failed"))
    video = browser_result.video
    if video is None:
        return finish(_result(request, requested_key, "response_shape_changed"))
    if video.platform != request.platform:
        return finish(_result(request, requested_key, "platform_mismatch"))
    if request.video_key is not None and request.video_key != video.raw_video_id:
        return finish(_result(request, requested_key, "video_key_mismatch"))
    video_key = request.video_key or video.raw_video_id
    if not video_key_is_safe(video_key):
        return _result(request, video_key, "invalid_video_key")

    decision = collection_target(video.total_comment_count)
    try:
        current = merge_current_run(browser_result.comments)
    except CurrentRunConflict:
        return finish(_result(request, video_key, "current_run_conflict"))
    selected = select_comments(current.comments, decision.target)

    target = output_root / "raw" / request.platform / video_key
    staging = output_root / ".staging" / run_id / request.platform / video_key
    backup = output_root / ".backup" / run_id / request.platform / video_key
    marker = _pending_marker(output_root, run_id, request.platform, video_key)
    errors = list(browser_result.collection_errors)
    if not _paths_are_safe_under(output_root, target, staging, backup, marker):
        return finish(_result(request, video_key, "artifact_commit_failed"))
    recovered = False
    if not target.exists():
        recovered, recovery_error = _recover_previous_generation(
            output_root, request.platform, video_key, target, staging
        )
        if recovered:
            errors.append(
                _collection_error(
                    "interrupted_commit_recovered", browser_result.collection_finished_at
                )
            )
        if recovery_error is not None:
            if recovered:
                errors.append(
                    _collection_error(
                        "artifact_cleanup_failed", browser_result.collection_finished_at
                    )
                )
            return finish(_result(request, video_key, recovery_error), errors)
    previous: list[RawComment] = []
    if target.exists() or target.is_symlink():
        if target.is_symlink():
            return finish(_result(request, video_key, "previous_artifacts_invalid"), errors)
        try:
            previous_video, previous, _ = validate_generation(target)
        except ArtifactValidationError:
            return finish(_result(request, video_key, "previous_artifacts_invalid"))
        if (
            previous_video.platform != video.platform
            or previous_video.raw_video_id != video.raw_video_id
        ):
            return finish(_result(request, video_key, "previous_artifacts_mismatch"), errors)
        if not _clear_completed_transactions(output_root, request.platform, video_key, marker):
            return finish(_result(request, video_key, "artifact_cleanup_failed"), errors)

    try:
        merged = merge_comments(previous, selected)
    except (CurrentRunConflict, PreviousRunConflict):
        return finish(_result(request, video_key, "comment_merge_failed"), errors)

    if decision.reason is not None:
        errors.append(_collection_error(decision.reason, browser_result.collection_finished_at))
    for conflict in merged.conflicts:
        errors.append(
            _collection_error(
                "merge_conflict",
                browser_result.collection_finished_at,
                comment_id=conflict.raw_comment_id,
                fields=list(conflict.conflict_fields),
            )
        )
    try:
        collection = CollectionRecord(
            reported_total=video.total_comment_count,
            collected_total=len(merged.comments),
            pages_requested=browser_result.pages_requested,
            pages_succeeded=browser_result.pages_succeeded,
            sort_modes=list(dict.fromkeys(browser_result.sort_modes)),
            collection_started_at=browser_result.collection_started_at,
            collection_finished_at=browser_result.collection_finished_at,
            collection_errors=errors,
        )
    except ValidationError:
        return finish(_result(request, video_key, "collection_invalid"), errors)

    paths = CommitPaths(
        staging=staging,
        target=target,
        backup=backup,
    )
    had_target = target.exists()
    cleanup_succeeded = True
    try:
        write_generation(paths.staging, video, merged.comments, collection)
        if had_target and not _create_pending_marker(output_root, marker):
            raise ArtifactCommitError("pending_marker_creation_failed")
        commit_generation(paths)
        validate_generation(paths.target)
    except (ArtifactCommitError, ArtifactValidationError):
        return finish(_result(request, video_key, "artifact_commit_failed"), errors)
    if had_target:
        cleanup_succeeded = _finish_pending_transaction(output_root, paths.backup, marker)

    status = "artifact_cleanup_failed" if not cleanup_succeeded else browser_result.status
    if status != "artifact_cleanup_failed" and len(selected) < decision.target:
        status = "partial"
    result = PilotResult(request.platform, video_key, status, decision.target, len(merged.comments))
    return finish(result, errors)


def run_batch(
    manifest: CollectionManifest,
    collector: VideoCollector,
    output_root: Path,
) -> BatchResult:
    """Collect in manifest order, isolating fatal stops to their platform."""

    if not manifest.videos:
        return BatchResult([], {}, "invalid_manifest")
    if not manifest_paths_are_safe(manifest):
        return BatchResult.from_results(
            [
                PilotResult(item.platform, item.video_key, "invalid_manifest_path", 0, 0)
                for item in manifest.videos
            ]
        )
    stopped: set[Platform] = set()
    results: list[PilotResult] = []
    for item in manifest.videos:
        if item.platform in stopped:
            results.append(PilotResult.skipped(item))
            continue
        try:
            result = collector.collect(item, output_root)
        except Exception:
            result = PilotResult(item.platform, item.video_key, "collection_failed", 0, 0)
        if result.platform != item.platform or result.video_key != item.video_key:
            result = PilotResult(item.platform, item.video_key, "response_shape_changed", 0, 0)
        results.append(result)
        if result.status in FATAL_PLATFORM_STATUSES:
            stopped.add(item.platform)
    return BatchResult.from_results(results)


class SupervisionGate(Protocol):
    def wait_for_ready(self, page: object, timeout_seconds: float) -> SupervisionStatus: ...


def _stderr_prompt(message: str) -> None:
    sys.stderr.write(f"{message}\n")
    sys.stderr.flush()


def _read_windows_terminal_status(timeout_seconds: float) -> str | None:
    import msvcrt

    deadline = monotonic() + timeout_seconds
    characters: list[str] = []
    while monotonic() < deadline:
        if not msvcrt.kbhit():
            sleep(min(0.05, max(0.0, deadline - monotonic())))
            continue
        character = msvcrt.getwch()
        if character in {"\r", "\n"}:
            return "".join(characters)
        if character == "\x03":
            return None
        if character == "\b":
            if characters:
                characters.pop()
            continue
        if character in {"\x00", "\xe0"}:
            if msvcrt.kbhit():
                msvcrt.getwch()
            continue
        characters.append(character)
    return None


def _read_posix_terminal_status(timeout_seconds: float) -> str | None:
    selector = selectors.DefaultSelector()
    try:
        selector.register(sys.stdin, selectors.EVENT_READ)
        if not selector.select(timeout_seconds):
            return None
        line = sys.stdin.readline()
    finally:
        selector.close()
    return line.rstrip("\r\n") if line else None


def _read_terminal_status(timeout_seconds: float) -> str | None:
    """Read one terminal line before the deadline without leaving a reader behind."""

    try:
        if timeout_seconds <= 0 or not sys.stdin.isatty():
            return None
        if sys.platform == "win32":
            return _read_windows_terminal_status(timeout_seconds)
        return _read_posix_terminal_status(timeout_seconds)
    except (Exception, KeyboardInterrupt):
        return None


class CliSupervisionGate:
    """Wait once for a fixed operator status without observing browser keyboard events."""

    def __init__(
        self,
        *,
        read_status: Callable[[float], str | None] = _read_terminal_status,
        write_prompt: Callable[[str], None] = _stderr_prompt,
    ) -> None:
        self._read_status = read_status
        self._write_prompt = write_prompt

    def wait_for_ready(self, page: object, timeout_seconds: float) -> SupervisionStatus:
        del page
        if timeout_seconds <= 0:
            return "login_failed"
        try:
            self._write_prompt(
                "Complete visible login or manual challenge handling, then enter one status: "
                "ready, login_failed, challenge_unresolved, or access_restricted "
                f"(timeout {timeout_seconds:g}s). The ready status only permits another page "
                "check and does not authorize any mouse action."
            )
            answer = self._read_status(timeout_seconds)
        except (Exception, KeyboardInterrupt):
            return "login_failed"
        return (
            cast(SupervisionStatus, answer) if answer in _SUPERVISION_STATUSES else "login_failed"
        )


def _local_page_platform(adapter: PlatformAdapter) -> Platform:
    if isinstance(adapter, BilibiliAdapter):
        return "bilibili"
    if isinstance(adapter, DouyinAdapter):
        return "douyin"
    raise ValueError("unsupported_adapter")


def _local_adapter_url(adapter: PlatformAdapter, response_url: str) -> str:
    path = urlsplit(response_url).path
    host = "api.bilibili.com" if isinstance(adapter, BilibiliAdapter) else "www.douyin.com"
    return f"https://{host}{path}"


def _local_request_allowed(local_url: str, request_url: str) -> bool:
    try:
        local = urlsplit(local_url)
        request = urlsplit(request_url)
        local_port = local.port or 80
        request_port = request.port or 80
    except ValueError:
        return False
    return (
        local.scheme == request.scheme == "http"
        and local.hostname in {"127.0.0.1", "::1"}
        and request.hostname == local.hostname
        and request_port == local_port
        and request.username is None
        and request.password is None
    )


def _meta_content(page: Page, name: str) -> str:
    value = page.locator(f'meta[name="{name}"]').get_attribute("content")
    if value is None:
        raise ResponseShapeChanged("response_shape_changed")
    return value


def _video_from_local_page(page: Page, platform: Platform) -> RawVideo:
    try:
        reported = _meta_content(page, "rs-total-comment-count")
        if not reported.isdecimal():
            raise ResponseShapeChanged("response_shape_changed")
        return RawVideo(
            platform=platform,
            raw_video_id=_meta_content(page, "rs-video-id"),
            raw_author_id=_meta_content(page, "rs-author-id"),
            title=_meta_content(page, "rs-title"),
            description=_meta_content(page, "rs-description"),
            published_at=None,
            duration_seconds=None,
            total_comment_count=int(reported),
            view_count=None,
            like_count=None,
            favorite_count=None,
            share_count=None,
            author_follower_count=None,
            captured_at=datetime.now(UTC),
        )
    except (ResponseShapeChanged, ValidationError, ValueError):
        raise ResponseShapeChanged("response_shape_changed") from None


def _bilibili_video_from_page(
    page: Page, request: PilotRequest, context: BilibiliCommentContext
) -> RawVideo:
    try:
        path_parts = urlsplit(str(request.url)).path.split("/")
        if len(path_parts) == 4 and path_parts[-1] == "":
            path_parts.pop()
        if len(path_parts) != 3 or path_parts[:2] != ["", "video"]:
            raise ResponseShapeChanged("response_shape_changed")
        extracted_key = path_parts[2]
        requested_key = request.video_key
        if (
            unquote(extracted_key) != extracted_key
            or not extracted_key
            or len(extracted_key) > 256
            or any(character.isspace() for character in extracted_key)
            or not video_key_is_safe(extracted_key)
            or (requested_key is not None and requested_key != extracted_key)
        ):
            raise ResponseShapeChanged("response_shape_changed")
        video_key = requested_key or extracted_key
        title = page.locator('meta[property="og:title"]').get_attribute("content")
        description = page.locator('meta[name="description"]').get_attribute("content")
        if not isinstance(title, str) or not isinstance(description, str):
            raise ResponseShapeChanged("response_shape_changed")
        return RawVideo(
            platform="bilibili",
            raw_video_id=video_key,
            raw_author_id=context.video_author_id,
            title=title,
            description=description,
            published_at=None,
            duration_seconds=None,
            total_comment_count=context.total_comment_count,
            view_count=None,
            like_count=None,
            favorite_count=None,
            share_count=None,
            author_follower_count=None,
            captured_at=datetime.now(UTC),
        )
    except Exception:
        raise ResponseShapeChanged("response_shape_changed") from None


def _douyin_note_video_from_page(
    page: Page, request: PilotRequest, adapter: DouyinAdapter
) -> RawVideo:
    try:
        parsed_url = urlsplit(str(getattr(page, "url", "")))
        requested_url = urlsplit(str(request.url))
        path_parts = parsed_url.path.split("/")
        if len(path_parts) == 4 and path_parts[-1] == "":
            path_parts.pop()
        requested_parts = requested_url.path.split("/")
        if len(requested_parts) == 4 and requested_parts[-1] == "":
            requested_parts.pop()
        if (
            parsed_url.scheme != "https"
            or parsed_url.hostname not in {"douyin.com", "www.douyin.com"}
            or len(path_parts) != 3
            or path_parts[:2] != ["", "note"]
            or requested_url.scheme != "https"
            or requested_url.hostname not in {"douyin.com", "www.douyin.com"}
            or len(requested_parts) != 3
            or requested_parts[1] not in {"video", "note"}
        ):
            raise ResponseShapeChanged("response_shape_changed")
        extracted_key = path_parts[2]
        original_key = requested_parts[2]
        requested_key = request.video_key
        if (
            unquote(extracted_key) != extracted_key
            or not video_key_is_safe(extracted_key)
            or unquote(original_key) != original_key
            or not video_key_is_safe(original_key)
            or original_key != extracted_key
            or (requested_key is not None and requested_key != extracted_key)
        ):
            raise ResponseShapeChanged("response_shape_changed")
        video_key = requested_key or extracted_key
        candidates: list[RawVideo] = []
        prefix = "self.__pace_f.push("
        for script in page.locator("script").all():
            text = script.text_content()
            if not isinstance(text, str) or video_key not in text or not text.startswith(prefix):
                continue
            if not text.endswith(")"):
                raise ResponseShapeChanged("response_shape_changed")
            outer = json.loads(text[len(prefix) : -1])
            if (
                not isinstance(outer, list)
                or len(outer) != 2
                or type(outer[0]) is not int
                or outer[0] != 1
                or not isinstance(outer[1], str)
            ):
                raise ResponseShapeChanged("response_shape_changed")
            label, separator, encoded_component = outer[1].partition(":")
            if separator != ":" or not label.isdecimal():
                continue
            component = json.loads(encoded_component)
            if not isinstance(component, list) or len(component) != 4:
                continue
            props = component[3]
            if not isinstance(props, Mapping) or props.get("awemeId") != video_key:
                continue
            aweme = props.get("aweme")
            if (
                not isinstance(aweme, Mapping)
                or type(aweme.get("statusCode")) is not int
                or aweme.get("statusCode") != 0
            ):
                raise ResponseShapeChanged("response_shape_changed")
            detail = aweme.get("detail")
            if not isinstance(detail, Mapping) or detail.get("awemeId") != video_key:
                raise ResponseShapeChanged("response_shape_changed")
            author = detail.get("authorInfo")
            stats = detail.get("stats")
            if not isinstance(author, Mapping) or not isinstance(stats, Mapping):
                raise ResponseShapeChanged("response_shape_changed")
            normalized = {
                "status_code": 0,
                "aweme_detail": {
                    "aweme_id": detail.get("awemeId"),
                    "author": {
                        "sec_uid": author.get("secUid"),
                        "uid": author.get("uid"),
                        "follower_count": author.get("followerCount"),
                    },
                    "statistics": {
                        "comment_count": stats.get("commentCount"),
                        "play_count": stats.get("playCount"),
                        "digg_count": stats.get("diggCount"),
                        "collect_count": stats.get("collectCount"),
                        "share_count": stats.get("shareCount"),
                    },
                    "desc": detail.get("desc"),
                    "create_time": detail.get("createTime"),
                    "duration": None,
                },
            }
            candidates.append(adapter.parse_video_response(normalized).video)
        if len(candidates) != 1:
            raise ResponseShapeChanged("response_shape_changed")
        return candidates[0]
    except Exception:
        raise ResponseShapeChanged("response_shape_changed") from None


def collect_from_page(
    local_url: str,
    adapter: PlatformAdapter,
    output_root: Path,
    headless: bool = True,
) -> PageCollectionResult:
    """Exercise collection against one checked-in loopback page and commit on success."""

    try:
        parsed_url = urlsplit(local_url)
    except ValueError:
        raise ValueError("local_page_required") from None
    if (
        parsed_url.scheme != "http"
        or parsed_url.hostname not in {"127.0.0.1", "::1"}
        or parsed_url.username is not None
        or parsed_url.password is not None
    ):
        raise ValueError("local_page_required")

    platform = _local_page_platform(adapter)
    started = datetime.now(UTC)
    video: RawVideo | None = None
    comments: list[RawComment] = []
    pages_requested = 0
    pages_succeeded = 0
    active_stratum: Stratum = "top"
    sort_modes: list[str] = []
    collection_errors: list[CollectionError] = []
    response_shape_changed = False
    network_boundary_failed = False
    pending_responses: list[tuple[Mapping[str, object], Stratum]] = []
    in_flight_requests: dict[int, Request] = {}
    observed_comment_requests: dict[int, Request] = {}

    def raise_if_response_failed() -> None:
        if response_shape_changed:
            raise ResponseShapeChanged("response_shape_changed")
        if network_boundary_failed:
            raise BrowserSessionError("local_page_network_blocked")

    def guard_request(route: Route) -> None:
        nonlocal network_boundary_failed
        try:
            if _local_request_allowed(local_url, route.request.url):
                route.continue_()
                return
        except Exception:
            pass
        network_boundary_failed = True
        try:
            route.abort()
        except Exception:
            pass

    def observe_request(request: Request) -> None:
        nonlocal network_boundary_failed, pages_requested
        in_flight_requests[id(request)] = request
        if not _local_request_allowed(local_url, request.url):
            network_boundary_failed = True
            return
        try:
            kind = adapter.response_kind(_local_adapter_url(adapter, request.url))
        except Exception:
            kind = None
        request_identity = id(request)
        if kind in {"comments", "replies"} and request_identity not in observed_comment_requests:
            observed_comment_requests[request_identity] = request
            pages_requested += 1

    def finish_request(request: Request) -> None:
        in_flight_requests.pop(id(request), None)

    def block_websocket(websocket: WebSocketRoute) -> None:
        nonlocal network_boundary_failed
        del websocket
        network_boundary_failed = True

    def observe_websocket(websocket: WebSocket) -> None:
        nonlocal network_boundary_failed
        del websocket
        network_boundary_failed = True

    def settle_requests(page: Page) -> None:
        page.wait_for_timeout(250)
        deadline = monotonic() + 2.0
        while in_flight_requests and monotonic() < deadline:
            page.wait_for_timeout(50)
        if in_flight_requests:
            raise BrowserSessionError("local_page_request_timeout")
        raise_if_response_failed()

    def parse_comment_payload(payload: Mapping[str, object], stratum: Stratum) -> None:
        nonlocal pages_succeeded
        if video is None:
            pending_responses.append((payload, stratum))
            return
        parsed = adapter.parse_comment_response(
            payload,
            stratum,
            pages_succeeded + 1,
            video_author_id=video.raw_author_id,
        )
        comments.extend(parsed.comments)
        pages_succeeded += 1

    def consume_response(response: Response) -> None:
        nonlocal response_shape_changed
        try:
            normalized_url = _local_adapter_url(adapter, response.url)
            kind = adapter.response_kind(normalized_url)
            if kind not in {"comments", "replies"}:
                return
            if not 200 <= response.status < 300:
                collection_errors.append(
                    _collection_error("page_request_failed", datetime.now(UTC))
                )
                return
            payload = response.json()
            if not isinstance(payload, Mapping):
                raise ResponseShapeChanged("response_shape_changed")
            parse_comment_payload(payload, active_stratum)
        except Exception:
            response_shape_changed = True

    try:
        with sync_playwright() as runtime:
            browser = runtime.chromium.launch(headless=headless)
            context = browser.new_context(service_workers="block")
            try:
                context.route("**/*", guard_request)
                context.route_web_socket("**/*", block_websocket)
                page = context.new_page()
                page.on("request", observe_request)
                page.on("response", consume_response)
                page.on("requestfinished", finish_request)
                page.on("requestfailed", finish_request)
                page.on("websocket", observe_websocket)
                page.goto(local_url)
                raise_if_response_failed()
                video = _video_from_local_page(page, platform)
                if not video_key_is_safe(video.raw_video_id):
                    raise ResponseShapeChanged("response_shape_changed")
                page.wait_for_load_state("networkidle")
                while pending_responses:
                    payload, stratum = pending_responses.pop(0)
                    parse_comment_payload(payload, stratum)
                raise_if_response_failed()
                unavailable_at = datetime.now(UTC)
                for stratum in cast(tuple[Stratum, ...], ("top", "recent", "replies")):
                    control = page.locator(f'[data-rs-stratum="{stratum}"]')
                    if control.count() == 0 or not control.first.is_visible():
                        collection_errors.append(
                            _collection_error("stratum_unavailable", unavailable_at)
                        )

                top_control = page.locator('[data-rs-stratum="top"]')
                if top_control.count() > 0 and top_control.first.is_visible():
                    top_control.first.click()
                    sort_modes.append("top")

                target = collection_target(video.total_comment_count).target
                scrolls = 0
                while True:
                    raise_if_response_failed()
                    if len(comments) >= target:
                        settle_requests(page)
                        break
                    end_marker = page.locator('[data-rs-end="true"]')
                    if end_marker.count() > 0 and end_marker.first.is_visible():
                        settle_requests(page)
                        break
                    page.mouse.wheel(0, 900)
                    page.wait_for_timeout(250)
                    scrolls += 1
                    raise_if_response_failed()
                    if scrolls >= 20:
                        raise BrowserSessionError("page_collection_timeout")
                raise_if_response_failed()
            finally:
                context.close()
                browser.close()
    except ResponseShapeChanged:
        raise ResponseShapeChanged("response_shape_changed") from None
    except BrowserSessionError:
        raise
    except Exception:
        raise BrowserSessionError("page_collection_failed") from None

    assert video is not None
    finished = datetime.now(UTC)
    collection = CollectionRecord(
        reported_total=video.total_comment_count,
        collected_total=len(comments),
        pages_requested=pages_requested,
        pages_succeeded=pages_succeeded,
        sort_modes=sort_modes,
        collection_started_at=started,
        collection_finished_at=finished,
        collection_errors=collection_errors,
    )
    run_id = uuid4().hex
    paths = CommitPaths(
        staging=output_root / ".staging" / run_id / platform / video.raw_video_id,
        target=output_root / "raw" / platform / video.raw_video_id,
        backup=output_root / ".backup" / run_id / platform / video.raw_video_id,
    )
    if not _paths_are_safe_under(output_root, paths.staging, paths.target, paths.backup):
        raise ArtifactCommitError("artifact_paths_invalid")
    raise_if_response_failed()
    try:
        write_generation(paths.staging, video, comments, collection)
        commit_generation(paths)
        validate_generation(paths.target)
    except (ArtifactCommitError, ArtifactValidationError):
        raise ArtifactCommitError("artifact_commit_failed") from None
    return PageCollectionResult(video, comments, collection)


class BrowserVideoCollector:
    """Collect supported response families in one headed browser session."""

    def __init__(
        self,
        *,
        supervisor: SupervisionGate | None = None,
        supervision_timeout_seconds: float = 120.0,
        challenge_action: ChallengeAction | None = None,
        challenge_id: str = "supervised",
        challenge_confirm: Callable[[], bool] | None = None,
        browser: BrowserName = "chromium",
        reuse_login: bool = False,
    ) -> None:
        if not (
            (browser == "chromium" and not reuse_login)
            or (browser in {"chrome", "edge"} and reuse_login)
        ):
            raise ValueError("invalid_browser_mode")
        self._supervisor = supervisor or CliSupervisionGate()
        self._supervision_timeout_seconds = supervision_timeout_seconds
        self._challenge_action = challenge_action
        self._challenge_id = challenge_id
        self._challenge_confirm = challenge_confirm
        self._browser = browser
        self._reuse_login = reuse_login

    def collect_pilot(self, request: PilotRequest, output_root: Path) -> PilotResult:
        run_id = uuid4().hex
        browser_result = self._browse(request, output_root, run_id=run_id)
        return run_pilot(request, browser_result, output_root=output_root)

    def collect(self, item: ManifestVideo, output_root: Path) -> PilotResult:
        request = PilotRequest.model_validate(
            {"platform": item.platform, "url": str(item.url), "video_key": item.video_key}
        )
        return self.collect_pilot(request, output_root)

    def _browse(
        self,
        request: PilotRequest,
        output_root: Path = Path(".local-data/m2-real"),
        *,
        run_id: str | None = None,
    ) -> BrowserResult:
        run_id = run_id if run_id is not None and _run_id_is_safe(run_id) else uuid4().hex
        started = datetime.now(UTC)
        adapter: PlatformAdapter = (
            BilibiliAdapter() if request.platform == "bilibili" else DouyinAdapter()
        )
        video: RawVideo | None = None
        comments: list[RawComment] = []
        errors: list[CollectionError] = []
        pages_requested = 0
        pages_succeeded = 0
        sort_modes: list[Stratum] = []
        performed_strata: set[Stratum] = set()
        current_stratum: Stratum = "top"
        ranks: dict[Stratum, int] = {stratum: 1 for stratum in _STRATA}
        latest_pages: dict[Stratum, tuple[bool, str | None]] = {}
        unique_comment_ids: set[str] = set()
        pending: list[tuple[Mapping[str, object], Stratum]] = []
        comment_context: BilibiliCommentContext | None = None

        def parse_comments(payload: Mapping[str, object], stratum: Stratum) -> None:
            nonlocal pages_succeeded
            assert video is not None
            parsed = adapter.parse_comment_response(
                payload,
                stratum,
                ranks[stratum],
                video_author_id=video.raw_author_id,
            )
            comments.extend(parsed.comments)
            unique_comment_ids.update(comment.raw_comment_id for comment in parsed.comments)
            latest_pages[stratum] = (parsed.has_more, parsed.next_cursor)
            pages_succeeded += 1
            ranks[stratum] += 1
            if stratum not in sort_modes:
                sort_modes.append(stratum)

        def consume(url: str, payload: object) -> None:
            nonlocal comment_context, video, pages_requested
            kind = adapter.response_kind(url)
            if kind is None or not isinstance(payload, Mapping):
                return
            if kind == "video":
                parsed_video = adapter.parse_video_response(payload).video
                if (
                    comment_context is not None
                    and parsed_video.raw_author_id != comment_context.video_author_id
                ):
                    raise ResponseShapeChanged("response_shape_changed")
                if video is not None and (
                    parsed_video.raw_video_id != video.raw_video_id
                    or parsed_video.raw_author_id != video.raw_author_id
                ):
                    raise ResponseShapeChanged("response_shape_changed")
                video = parsed_video
                while pending:
                    pending_payload, pending_stratum = pending.pop(0)
                    parse_comments(pending_payload, pending_stratum)
                return
            pages_requested += 1
            if isinstance(adapter, BilibiliAdapter) and kind == "comments":
                parsed_context = adapter.parse_comment_context(payload)
                if (comment_context is not None and comment_context != parsed_context) or (
                    video is not None and video.raw_author_id != parsed_context.video_author_id
                ):
                    raise ResponseShapeChanged("response_shape_changed")
                comment_context = parsed_context
            if video is None:
                pending.append((payload, current_stratum))
                return
            parse_comments(payload, current_stratum)

        def establish_bilibili_fallback(page: Page) -> None:
            nonlocal video
            if (
                video is not None
                or not isinstance(adapter, BilibiliAdapter)
                or comment_context is None
            ):
                return
            video = _bilibili_video_from_page(page, request, comment_context)
            while pending:
                pending_payload, pending_stratum = pending.pop(0)
                parse_comments(pending_payload, pending_stratum)

        def establish_page_fallback(page: Page, *, allow_douyin_note: bool = False) -> None:
            nonlocal video
            establish_bilibili_fallback(page)
            if not allow_douyin_note or video is not None or not isinstance(adapter, DouyinAdapter):
                return
            try:
                page_path = urlsplit(str(getattr(page, "url", ""))).path
            except ValueError:
                return
            if not page_path.startswith("/note/"):
                return
            video = _douyin_note_video_from_page(page, request, adapter)
            while pending:
                pending_payload, pending_stratum = pending.pop(0)
                parse_comments(pending_payload, pending_stratum)

        def explicitly_exhausted() -> bool:
            return bool(performed_strata) and all(
                stratum in latest_pages and not latest_pages[stratum][0]
                for stratum in performed_strata
            )

        status = "success"
        try:
            launch_config: BrowserLaunchConfig | None = None
            if self._reuse_login:
                profile = browser_profile_path(output_root, request.platform, self._browser)
                if profile is None:
                    raise BrowserSessionError("browser_profile_unavailable")
                launch_config = BrowserLaunchConfig(
                    browser=self._browser,
                    output_root=output_root,
                    platform=request.platform,
                )
            browser_session = (
                BrowserSession()
                if launch_config is None
                else BrowserSession(launch_config=launch_config)
            )
            with browser_session as session:
                session.open(str(request.url), adapter, consume)
                session.raise_if_response_failed()
                establish_page_fallback(session.page)
                supervision_status = self._supervisor.wait_for_ready(
                    session.page, self._supervision_timeout_seconds
                )
                session.raise_if_response_failed()
                if supervision_status != "ready":
                    status = supervision_status
                elif self._challenge_action is None:
                    establish_page_fallback(session.page, allow_douyin_note=True)
                if status == "success" and self._challenge_action is not None:
                    if not _challenge_id_is_safe(self._challenge_id):
                        status = "challenge_unresolved"
                    else:
                        challenge_directory = (
                            output_root / "challenges" / run_id / self._challenge_id
                        )
                        if not _path_is_safe_under(output_root, challenge_directory):
                            status = "challenge_unresolved"
                        else:
                            handler = (
                                ChallengeHandler(challenge_directory)
                                if self._challenge_confirm is None
                                else ChallengeHandler(
                                    challenge_directory, confirm=self._challenge_confirm
                                )
                            )
                            challenge = handler.attempt(session.page, self._challenge_action)
                            session.raise_if_response_failed()
                            if challenge.status != "attempted":
                                status = "challenge_unresolved"
                            else:
                                status = self._supervisor.wait_for_ready(
                                    session.page, self._supervision_timeout_seconds
                                )
                                session.raise_if_response_failed()
                                if status == "ready":
                                    establish_page_fallback(session.page, allow_douyin_note=True)
                if status == "success" or status == "ready":
                    status = "success"
                    for stratum in _STRATA:
                        current_stratum = stratum
                        action_status = perform_stratum_action(session.page, stratum)
                        session.raise_if_response_failed()
                        if action_status == "performed":
                            performed_strata.add(stratum)
                            sort_modes.append(stratum)
                            session.wait_for_response_processing()
                            establish_page_fallback(session.page, allow_douyin_note=True)
                    establish_page_fallback(session.page, allow_douyin_note=True)
                    if video is not None and latest_pages:
                        target = collection_target(video.total_comment_count).target
                        no_progress_rounds = 0
                        for round_number in range(1, 101):
                            if len(unique_comment_ids) >= target:
                                break
                            if explicitly_exhausted():
                                status = "partial"
                                break
                            before = len(unique_comment_ids)
                            for stratum in _PAGINATION_STRATA:
                                current_stratum = stratum
                                action_status = perform_stratum_action(session.page, stratum)
                                session.raise_if_response_failed()
                                if action_status == "performed":
                                    performed_strata.add(stratum)
                                    if stratum not in sort_modes:
                                        sort_modes.append(stratum)
                                    session.wait_for_response_processing()
                                    establish_page_fallback(session.page, allow_douyin_note=True)
                                    if len(unique_comment_ids) >= target:
                                        break
                                    if explicitly_exhausted():
                                        break
                            if len(unique_comment_ids) == before:
                                no_progress_rounds += 1
                            else:
                                no_progress_rounds = 0
                            if len(unique_comment_ids) >= target:
                                break
                            if explicitly_exhausted():
                                status = "partial"
                                break
                            if no_progress_rounds == 3:
                                status = "partial"
                                errors.append(
                                    _collection_error("pagination_stalled", datetime.now(UTC))
                                )
                                break
                            if round_number == 100:
                                status = "partial"
                                errors.append(
                                    _collection_error("pagination_round_limit", datetime.now(UTC))
                                )
                session.raise_if_response_failed()
        except ResponseShapeChanged:
            status = "response_shape_changed"
        except BrowserSessionError as error:
            status = (
                "response_shape_changed"
                if str(error) == "response_processing_failed"
                else "collection_failed"
            )
        except Exception:
            status = "collection_failed"
        finished = datetime.now(UTC)
        if status == "success" and video is not None and not comments:
            status = "partial"
            errors.append(_collection_error("no_comments_collected", finished))
        if status == "success" and video is None:
            status = "response_shape_changed"
        return BrowserResult(
            video=video,
            comments=comments,
            pages_requested=pages_requested,
            pages_succeeded=pages_succeeded,
            sort_modes=sort_modes,
            collection_started_at=started,
            collection_finished_at=finished,
            collection_errors=errors,
            status=status,
            run_id=run_id,
            browser=self._browser,
            session_mode="dedicated" if self._reuse_login else "ephemeral",
        )
