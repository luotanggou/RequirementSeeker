from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from requirementseeker_collector.candidates.contracts import CandidateVideo
from requirementseeker_collector.candidates.coverage import validate_coverage
from requirementseeker_collector.contracts import CollectionManifest, ManifestVideo

DIRECTIONS = (
    ["software_tools"] * 6
    + ["tutorial_workflow"] * 6
    + ["life_services"] * 4
    + ["entertainment_culture"] * 4
    + ["ecommerce_marketing"] * 4
)
SCALES = ["up_to_200", "201_to_2000", "over_2000"] * 8
NOW = datetime(2026, 9, 13, tzinfo=UTC)


def complete_manifest() -> CollectionManifest:
    videos: list[ManifestVideo] = []
    for index, (direction, scale) in enumerate(zip(DIRECTIONS, SCALES, strict=True)):
        platform = "bilibili" if index % 2 == 0 else "douyin"
        key = f"video-{index + 1}"
        url = (
            f"https://www.bilibili.com/video/{key}"
            if platform == "bilibili"
            else f"https://www.douyin.com/video/{key}"
        )
        videos.append(
            ManifestVideo(
                platform=platform,
                video_key=key,
                url=url,
                direction=direction,
                comment_scale=scale,
            )
        )
    return CollectionManifest(manifest_version="1.0", videos=videos, unavailable_platforms=[])


def remove_video(manifest: CollectionManifest) -> CollectionManifest:
    return manifest.model_copy(update={"videos": manifest.videos[:-1]})


def move_direction(manifest: CollectionManifest) -> CollectionManifest:
    videos = list(manifest.videos)
    videos[0] = videos[0].model_copy(update={"direction": "life_services"})
    return manifest.model_copy(update={"videos": videos})


def move_scale(manifest: CollectionManifest) -> CollectionManifest:
    videos = list(manifest.videos)
    videos[0] = videos[0].model_copy(update={"comment_scale": "over_2000"})
    return manifest.model_copy(update={"videos": videos})


def drop_platform(manifest: CollectionManifest) -> CollectionManifest:
    videos = list(manifest.videos)
    videos[0] = videos[0].model_copy(
        update={
            "platform": "douyin",
            "url": "https://www.douyin.com/video/replacement",
            "video_key": "replacement",
        }
    )
    return manifest.model_copy(update={"videos": videos})


def candidate(
    key: str,
    *,
    direction: str,
    count: int | None,
    rank: int,
) -> CandidateVideo:
    return CandidateVideo(
        platform="bilibili",
        video_key=key,
        url=f"https://www.bilibili.com/video/{key}",
        title=key,
        reported_comment_count=count,
        direction=direction,
        query="AI",
        source_page="https://search.bilibili.com/all?keyword=AI",
        source_rank=rank,
        discovered_at=NOW,
    )


def test_complete_plan_has_no_gaps() -> None:
    report = validate_coverage(complete_manifest())

    assert report.valid is True
    assert report.gaps == []
    assert report.suggestions == []


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (remove_video, "video_total"),
        (move_direction, "direction_distribution"),
        (move_scale, "scale_distribution"),
        (drop_platform, "platform_distribution"),
    ],
)
def test_coverage_reports_exact_gap(
    mutation: Callable[[CollectionManifest], CollectionManifest], code: str
) -> None:
    report = validate_coverage(mutation(complete_manifest()))

    assert code in [gap.code for gap in report.gaps]


def test_unavailable_platform_is_invalid_and_quota_is_not_redistributed() -> None:
    manifest = complete_manifest().model_copy(update={"unavailable_platforms": ["douyin"]})

    report = validate_coverage(manifest)

    assert report.valid is False
    assert [gap.code for gap in report.gaps] == ["platform_unavailable"]
    assert report.gaps[0].expected == {"bilibili": 12, "douyin": 12}


def test_suggestions_prefer_missing_direction_then_scale_then_rank_and_key() -> None:
    manifest = remove_video(complete_manifest())
    candidates = [
        candidate("scale-only", direction="life_services", count=100, rank=1),
        candidate("direction-only", direction="ecommerce_marketing", count=500, rank=1),
        candidate("both-z", direction="ecommerce_marketing", count=2_001, rank=3),
        candidate("both-a", direction="ecommerce_marketing", count=2_001, rank=3),
        candidate("video-1", direction="software_tools", count=100, rank=1),
    ]
    original_manifest = manifest.model_dump()
    original_candidates = [item.model_dump() for item in candidates]

    report = validate_coverage(manifest, candidates)

    assert [item.video_key for item in report.suggestions] == [
        "both-a",
        "both-z",
        "direction-only",
        "scale-only",
    ]
    assert manifest.model_dump() == original_manifest
    assert [item.model_dump() for item in candidates] == original_candidates


def test_suggestions_infer_scale_at_exact_boundaries_and_ignore_unknown_count() -> None:
    manifest = remove_video(complete_manifest())
    candidates = [
        candidate("two-hundred", direction="ecommerce_marketing", count=200, rank=3),
        candidate("two-thousand", direction="ecommerce_marketing", count=2_000, rank=2),
        candidate("two-thousand-one", direction="ecommerce_marketing", count=2_001, rank=1),
        candidate("unknown", direction="ecommerce_marketing", count=None, rank=1),
    ]

    report = validate_coverage(manifest, candidates)

    assert [item.video_key for item in report.suggestions] == [
        "two-thousand-one",
        "two-thousand",
        "two-hundred",
        "unknown",
    ]
