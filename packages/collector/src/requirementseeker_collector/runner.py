"""Single-video orchestration and platform-isolated manifest execution."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, Self, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from .adapters import BilibiliAdapter, DouyinAdapter, PlatformAdapter, ResponseShapeChanged
from .artifacts import (
    ArtifactCommitError,
    ArtifactValidationError,
    CommitPaths,
    commit_generation,
    validate_generation,
    write_generation,
)
from .browser import BrowserSession, BrowserSessionError, perform_stratum_action
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
_WINDOWS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)


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
    """Safe normalized output from one ephemeral browser session."""

    video: RawVideo | None
    comments: list[RawComment]
    pages_requested: int
    pages_succeeded: int
    sort_modes: list[Stratum]
    collection_started_at: datetime
    collection_finished_at: datetime
    collection_errors: list[CollectionError] = field(default_factory=list)
    status: str = "success"


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
        value not in {".", ".."}
        and not value.endswith(".")
        and "/" not in value
        and "\\" not in value
        and ":" not in value
        and stem not in _WINDOWS_DEVICE_NAMES
        and all(ord(character) >= 32 and ord(character) != 127 for character in value)
    )


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
    try:
        encoded = json.dumps(
            {
                "collected_total": result.collected_total,
                "collection_finished_at": browser_result.collection_finished_at.isoformat(),
                "collection_started_at": browser_result.collection_started_at.isoformat(),
                "errors": [error.category for error in errors],
                "pages_requested": browser_result.pages_requested,
                "pages_succeeded": browser_result.pages_succeeded,
                "platform": result.platform,
                "run_version": "1.0",
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
    directory = output_root / "runs" / run_id
    temporary = directory.with_name(f".{run_id}.writing")
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


def run_pilot(
    request: PilotRequest,
    browser_result: BrowserResult,
    *,
    output_root: Path,
) -> PilotResult:
    """Select, merge, validate, and commit one browser collection result."""

    run_id = uuid4().hex

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
    previous: list[RawComment] = []
    if target.exists() or target.is_symlink():
        try:
            previous_video, previous, _ = validate_generation(target)
        except ArtifactValidationError:
            return finish(_result(request, video_key, "previous_artifacts_invalid"))
        if (
            previous_video.platform != video.platform
            or previous_video.raw_video_id != video.raw_video_id
        ):
            return finish(_result(request, video_key, "previous_artifacts_mismatch"))

    try:
        merged = merge_comments(previous, selected)
    except (CurrentRunConflict, PreviousRunConflict):
        return finish(_result(request, video_key, "comment_merge_failed"))

    errors = list(browser_result.collection_errors)
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
        staging=output_root / ".staging" / run_id / request.platform / video_key,
        target=target,
        backup=output_root / ".backup" / run_id / request.platform / video_key,
    )
    try:
        write_generation(paths.staging, video, merged.comments, collection)
        commit_generation(paths)
    except (ArtifactCommitError, ArtifactValidationError):
        return finish(_result(request, video_key, "artifact_commit_failed"), errors)

    status = browser_result.status
    if len(selected) < decision.target:
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


class BrowserVideoCollector:
    """Collect supported response families in one headed, ephemeral browser session."""

    def collect_pilot(self, request: PilotRequest, output_root: Path) -> PilotResult:
        return run_pilot(request, self._browse(request), output_root=output_root)

    def collect(self, item: ManifestVideo, output_root: Path) -> PilotResult:
        request = PilotRequest.model_validate(
            {"platform": item.platform, "url": str(item.url), "video_key": item.video_key}
        )
        return self.collect_pilot(request, output_root)

    def _browse(self, request: PilotRequest) -> BrowserResult:
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
        current_stratum: Stratum = "top"
        ranks: dict[Stratum, int] = {stratum: 1 for stratum in _STRATA}
        pending: list[tuple[Mapping[str, object], Stratum]] = []

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
            pages_succeeded += 1
            ranks[stratum] += 1
            if stratum not in sort_modes:
                sort_modes.append(stratum)

        def consume(url: str, payload: object) -> None:
            nonlocal video, pages_requested
            kind = adapter.response_kind(url)
            if kind is None or not isinstance(payload, Mapping):
                return
            if kind == "video":
                video = adapter.parse_video_response(payload).video
                while pending:
                    pending_payload, pending_stratum = pending.pop(0)
                    parse_comments(pending_payload, pending_stratum)
                return
            pages_requested += 1
            if video is None:
                pending.append((payload, current_stratum))
                return
            parse_comments(payload, current_stratum)

        status = "success"
        try:
            with BrowserSession() as session:
                session.open(str(request.url), adapter, consume)
                for stratum in _STRATA:
                    current_stratum = stratum
                    if perform_stratum_action(session.page, stratum) == "performed":
                        sort_modes.append(stratum)
                        session.page.wait_for_timeout(750)
        except ResponseShapeChanged:
            status = "response_shape_changed"
        except BrowserSessionError as error:
            status = (
                "response_shape_changed"
                if str(error) == "response_processing_failed"
                else "access_restricted"
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
        )
