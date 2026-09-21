"""将批准的 Collector 原始目录原子转换为可交接的脱敏数据集。"""

import json
import shutil
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from math import ceil, sqrt
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, HttpUrl, ValidationError, model_validator

from .contracts import (
    Contract,
    Identifier,
    Platform,
    ReviewItem,
    SamplingManifest,
    SanitizationReport,
    SanitizedComment,
    SanitizedVideo,
    VideoDirection,
    VideoMetrics,
)
from .identifiers import IdentifierPseudonymizer
from .source import RawVideoBundle, read_raw_video
from .split import stable_split
from .text import TextResult, sanitize_text

_RULES_VERSION = "pii-v1"
_DIRECTION_MAP: dict[str, VideoDirection] = {
    "software_tools": "software_tool",
    "tutorial_workflow": "tutorial_workflow",
    "life_services": "life_service",
    "ecommerce_marketing": "ecommerce_marketing",
    "entertainment_culture": "entertainment_culture",
}


class SanitizationError(ValueError):
    """脱敏失败的固定错误码；异常不得回显原始数据。"""


class _PlanVideo(Contract):
    platform: Platform
    video_key: Identifier
    url: HttpUrl
    direction: str = Field(strict=True, min_length=1, max_length=64)
    comment_scale: Literal["up_to_200", "201_to_2000", "over_2000"]

    @model_validator(mode="after")
    def safe_key(self) -> Self:
        if self.video_key in {".", ".."} or any(value in self.video_key for value in ("/", "\\")):
            raise ValueError("unsafe_video_key")
        if self.direction not in _DIRECTION_MAP:
            raise ValueError("unknown_direction")
        return self


class _ApprovedPlan(Contract):
    manifest_version: Literal["1.0"]
    videos: list[_PlanVideo]
    unavailable_platforms: list[Platform]

    @model_validator(mode="after")
    def unique_videos(self) -> Self:
        keys = [(item.platform, item.video_key) for item in self.videos]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate_approved_video")
        return self


class _SafeCollection(Contract):
    """保留采集统计，但不复制可能含原始标识符的错误描述。"""

    reported_total: int | None = Field(ge=0)
    collected_total: int = Field(ge=0)
    pages_requested: int = Field(ge=0)
    pages_succeeded: int = Field(ge=0)
    sort_modes: list[Identifier]
    collection_started_at: datetime
    collection_finished_at: datetime
    collection_error_count: int = Field(ge=0)


@dataclass(frozen=True, slots=True)
class SanitizationResult:
    output_files: tuple[Path, ...]
    sampling_manifests: tuple[Path, ...]
    sanitization_reports: tuple[Path, ...]
    replacement_candidates: Path
    excluded_raw_directory_count: int


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _digest(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _json_value(model: Contract) -> dict[str, object]:
    return model.model_dump(mode="json")


def _read_plan(path: Path) -> _ApprovedPlan:
    try:
        return _ApprovedPlan.model_validate_json(path.read_bytes())
    except (OSError, ValidationError, ValueError):
        raise SanitizationError("approved_plan_invalid") from None


def _collection_target(total: int | None) -> int:
    """独立复刻已批准的 Collector 三段采集公式。"""

    if total is None:
        return 200
    if total <= 200:
        return total
    if total <= 2000:
        return min(500, max(200, ceil(total * 0.25)))
    if total >= 10000:
        return 1000
    return min(1000, max(500, ceil(10 * sqrt(total))))


def _merge_text_result(
    result: TextResult,
    counts: Counter[str],
    reviews: list[ReviewItem],
    video_id: str,
    field: Literal["title", "description", "text"],
    comment_id: str | None = None,
) -> str:
    counts.update(result.replacement_counts)
    reviews.extend(
        ReviewItem(video_id=video_id, comment_id=comment_id, field=field, reason=reason)
        for reason in result.review_reasons
    )
    return result.text


def _sanitize_bundle(
    bundle: RawVideoBundle,
    plan_video: _PlanVideo,
    pseudonymizer: IdentifierPseudonymizer,
) -> tuple[SanitizedVideo, list[SanitizedComment], _SafeCollection, Counter[str], list[ReviewItem]]:
    video_id = pseudonymizer.video(bundle.video.raw_video_id)
    counts: Counter[str] = Counter()
    reviews: list[ReviewItem] = []
    video = SanitizedVideo(
        platform=bundle.video.platform,
        video_id=video_id,
        author_id=pseudonymizer.author(bundle.video.raw_author_id),
        title=_merge_text_result(
            sanitize_text(bundle.video.title), counts, reviews, video_id, "title"
        ),
        description=_merge_text_result(
            sanitize_text(bundle.video.description), counts, reviews, video_id, "description"
        ),
        published_at=bundle.video.published_at,
        duration_seconds=bundle.video.duration_seconds,
        total_comment_count=bundle.video.total_comment_count,
        view_count=bundle.video.view_count,
        like_count=bundle.video.like_count,
        favorite_count=bundle.video.favorite_count,
        share_count=bundle.video.share_count,
        author_follower_count=bundle.video.author_follower_count,
        captured_at=bundle.video.captured_at,
    )
    comments: list[SanitizedComment] = []
    for raw in bundle.comments:
        comment_id = pseudonymizer.comment(raw.raw_comment_id)
        text = _merge_text_result(
            sanitize_text(raw.text), counts, reviews, video_id, "text", comment_id
        )
        comments.append(
            SanitizedComment(
                comment_id=comment_id,
                author_id=(
                    pseudonymizer.author(raw.raw_author_id)
                    if raw.raw_author_id is not None
                    else None
                ),
                parent_comment_id=(
                    pseudonymizer.comment(raw.raw_parent_comment_id)
                    if raw.raw_parent_comment_id is not None
                    else None
                ),
                text=text,
                published_at=raw.published_at,
                collected_at=raw.collected_at,
                like_count=raw.like_count,
                reply_count=raw.reply_count,
                is_video_author=raw.is_video_author,
                source_stratum=raw.source_stratum,
                source_page_or_rank=raw.source_page_or_rank,
            )
        )
    collection = _SafeCollection(
        reported_total=bundle.collection.reported_total,
        collected_total=len(comments),
        pages_requested=bundle.collection.pages_requested,
        pages_succeeded=bundle.collection.pages_succeeded,
        sort_modes=bundle.collection.sort_modes,
        collection_started_at=bundle.collection.collection_started_at,
        collection_finished_at=bundle.collection.collection_finished_at,
        collection_error_count=len(bundle.collection.collection_errors),
    )
    # 此参数参与清单方向映射；在这里保留可读的显式一致性检查。
    if plan_video.platform != video.platform:
        raise SanitizationError("approved_raw_platform_mismatch")
    return video, comments, collection, counts, reviews


def _sampling_manifest(
    video: SanitizedVideo,
    comments: list[SanitizedComment],
    bundle: RawVideoBundle,
    plan_video: _PlanVideo,
) -> SamplingManifest:
    exact_duplicates = sum(
        error.category == "exact_duplicate_merged" for error in bundle.collection.collection_errors
    )
    normalized = Counter(" ".join(comment.text.casefold().split()) for comment in comments)
    normalized_duplicates = sum(count - 1 for count in normalized.values())
    authors = [comment.author_id for comment in comments if comment.author_id is not None]
    strata = {comment.source_stratum for comment in comments}
    stratum_ids = {
        stratum: [comment.comment_id for comment in comments if comment.source_stratum == stratum]
        for stratum in sorted(strata)
    }
    return SamplingManifest(
        sampling_schema_version="1.0",
        manifest_id=f"manifest_{video.video_id.removeprefix('video_')}",
        platform=video.platform,
        video_id=video.video_id,
        captured_at=video.captured_at,
        reported_total=bundle.collection.reported_total,
        collection_target=_collection_target(bundle.collection.reported_total),
        collected_total=len(comments) + exact_duplicates,
        pages_requested=bundle.collection.pages_requested,
        pages_succeeded=bundle.collection.pages_succeeded,
        available_strata=strata,
        direction=_DIRECTION_MAP[plan_video.direction],
        author_id_present=len(authors),
        distinct_author_count=len(set(authors)),
        exact_duplicate_count=exact_duplicates,
        normalized_duplicate_count=normalized_duplicates,
        video_metrics=VideoMetrics(
            views=video.view_count,
            likes=video.like_count,
            favorites=video.favorite_count,
            shares=video.share_count,
            author_followers=video.author_follower_count,
        ),
        candidate_comment_ids=[comment.comment_id for comment in comments],
        stratum_comment_ids=stratum_ids,
    )


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


def _write_video(
    staging: Path,
    bundle: RawVideoBundle,
    plan_video: _PlanVideo,
    pseudonymizer: IdentifierPseudonymizer,
) -> tuple[str, bool]:
    started = datetime.now(UTC)
    video, comments, collection, counts, reviews = _sanitize_bundle(
        bundle, plan_video, pseudonymizer
    )
    directory = staging / video.platform / video.video_id
    directory.mkdir(parents=True)
    input_value = {
        "video": bundle.video.model_dump(mode="json"),
        "comments": [item.model_dump(mode="json") for item in bundle.comments],
        "collection": bundle.collection.model_dump(mode="json"),
    }
    excluded_reason = (
        "zero_comments"
        if not comments
        else "zero_successful_pages"
        if collection.pages_succeeded == 0
        else None
    )
    output_value: dict[str, object] = {}
    if excluded_reason is None:
        video_value = _json_value(video)
        comment_values = [_json_value(item) for item in comments]
        collection_value = _json_value(collection)
        output_value = {
            "video": video_value,
            "comments": comment_values,
            "collection": collection_value,
        }
        _write_json(directory / "video.json", video_value)
        (directory / "comments.jsonl").write_text(
            "".join(
                json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                for item in comment_values
            ),
            encoding="utf-8",
        )
        _write_json(directory / "collection.json", collection_value)
        manifest = _sampling_manifest(video, comments, bundle, plan_video)
        manifest_value = _json_value(manifest)
        _write_json(directory / "sampling-manifest.json", manifest_value)
        output_value["sampling_manifest"] = manifest_value

    report = SanitizationReport(
        sanitization_schema_version="1.0",
        video_id=video.video_id,
        rules_version=_RULES_VERSION,
        input_sha256=_digest(input_value),
        output_sha256=_digest(output_value),
        replacement_counts=dict(sorted(counts.items())),
        review_item_count=len(reviews),
        review_items=reviews,
        started_at=started,
        finished_at=datetime.now(UTC),
        excluded=excluded_reason is not None,
        exclusion_reason=excluded_reason,
    )
    _write_json(directory / "sanitization.json", _json_value(report))
    return video.video_id, excluded_reason is not None


def _count_unapproved_directories(raw_root: Path, plan: _ApprovedPlan) -> int:
    approved = {(item.platform, item.video_key) for item in plan.videos}
    count = 0
    for platform in ("bilibili", "douyin"):
        platform_root = raw_root / platform
        try:
            entries = list(platform_root.iterdir()) if platform_root.is_dir() else []
        except OSError:
            raise SanitizationError("raw_root_unreadable") from None
        count += sum(entry.is_dir() and (platform, entry.name) not in approved for entry in entries)
    return count


def _commit_generation(staging: Path, target: Path, backup: Path) -> None:
    if staging.is_symlink() or target.is_symlink() or backup.exists() or backup.is_symlink():
        raise SanitizationError("output_commit_path_invalid")
    moved_old = False
    try:
        if target.exists():
            target.replace(backup)
            moved_old = True
        staging.replace(target)
    except OSError:
        if moved_old and backup.exists() and not target.exists():
            try:
                backup.replace(target)
            except OSError:
                raise SanitizationError("output_commit_failed_recovery_failed") from None
        raise SanitizationError("output_commit_failed") from None
    if moved_old:
        try:
            shutil.rmtree(backup)
        except OSError:
            raise SanitizationError("output_backup_cleanup_failed") from None


def sanitize_root(
    raw_root: Path,
    approved_plan: Path,
    output_root: Path,
    secret_environment_name: str,
) -> SanitizationResult:
    """只处理批准白名单，并以一个目录事务发布完整脱敏结果。"""

    plan = _read_plan(approved_plan)
    pseudonymizer = IdentifierPseudonymizer.from_environment(secret_environment_name)
    try:
        raw_resolved = raw_root.resolve(strict=True)
        output_resolved = output_root.resolve(strict=False)
    except OSError:
        raise SanitizationError("dataset_path_invalid") from None
    if (
        raw_resolved == output_resolved
        or raw_resolved in output_resolved.parents
        or output_resolved in raw_resolved.parents
    ):
        raise SanitizationError("output_must_be_outside_raw_root")

    # 在创建 staging 前完成所有原始输入校验，失败时不会触碰现有输出。
    approved: list[tuple[_PlanVideo, RawVideoBundle]] = []
    for item in plan.videos:
        directory = raw_root / item.platform / item.video_key
        if not directory.is_dir() or directory.is_symlink():
            raise SanitizationError("approved_raw_directory_missing")
        bundle = read_raw_video(directory)
        if bundle.video.platform != item.platform or bundle.video.raw_video_id != item.video_key:
            raise SanitizationError("approved_raw_directory_mismatch")
        approved.append((item, bundle))

    excluded_count = _count_unapproved_directories(raw_root, plan)
    staging = output_root.with_name(f".{output_root.name}.staging")
    backup = output_root.with_name(f".{output_root.name}.backup")
    if staging.exists() or staging.is_symlink() or backup.exists() or backup.is_symlink():
        raise SanitizationError("output_transaction_already_exists")
    try:
        staging.mkdir(parents=True)
        video_ids: list[str] = []
        replacements: list[str] = []
        for item, bundle in approved:
            video_id, excluded = _write_video(staging, bundle, item, pseudonymizer)
            if excluded:
                replacements.append(video_id)
            else:
                video_ids.append(video_id)
        _write_json(
            staging / "replacement-candidates.json",
            {"count": len(replacements), "video_ids": sorted(replacements)},
        )
        _write_json(staging / "dataset-split.json", _json_value(stable_split(video_ids)))
        _write_json(
            staging / "sanitization-summary.json",
            {
                "approved_video_count": len(approved),
                "excluded_raw_directory_count": excluded_count,
                "replacement_candidate_count": len(replacements),
            },
        )
        _commit_generation(staging, output_root, backup)
    except Exception:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging, ignore_errors=True)
        raise

    output_files = tuple(sorted(path for path in output_root.rglob("*") if path.is_file()))
    manifests = tuple(path for path in output_files if path.name == "sampling-manifest.json")
    reports = tuple(path for path in output_files if path.name == "sanitization.json")
    return SanitizationResult(
        output_files=output_files,
        sampling_manifests=manifests,
        sanitization_reports=reports,
        replacement_candidates=output_root / "replacement-candidates.json",
        excluded_raw_directory_count=excluded_count,
    )
