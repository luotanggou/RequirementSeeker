"""Deterministic validation and suggestions for the 24-video collection plan."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Literal

from pydantic import Field

from ..contracts import (
    CollectionManifest,
    CommentScale,
    Contract,
    Platform,
)
from .contracts import CandidateVideo

GapCode = Literal[
    "video_total",
    "direction_distribution",
    "scale_distribution",
    "platform_distribution",
    "platform_unavailable",
]

VIDEO_TARGET = 24
PLATFORM_TARGETS: dict[str, int] = {"bilibili": 12, "douyin": 12}
DIRECTION_TARGETS: dict[str, int] = {
    "software_tools": 6,
    "tutorial_workflow": 6,
    "life_services": 4,
    "entertainment_culture": 4,
    "ecommerce_marketing": 4,
}
SCALE_TARGETS: dict[str, int] = {
    "up_to_200": 8,
    "201_to_2000": 8,
    "over_2000": 8,
}


class CoverageGap(Contract):
    code: GapCode
    expected: int | dict[str, int]
    actual: int | dict[str, int]
    unavailable_platforms: list[Platform] = Field(default_factory=list)


class CoverageReport(Contract):
    valid: bool
    gaps: list[CoverageGap]
    suggestions: list[CandidateVideo]


def _distribution(values: Iterable[str], targets: dict[str, int]) -> dict[str, int]:
    counts = Counter(values)
    return {key: counts[key] for key in targets}


def _comment_scale(count: int | None) -> CommentScale | None:
    if count is None:
        return None
    if count <= 200:
        return "up_to_200"
    if count <= 2_000:
        return "201_to_2000"
    return "over_2000"


def _suggestions(
    manifest: CollectionManifest, candidates: Iterable[CandidateVideo]
) -> list[CandidateVideo]:
    selected = {(video.platform, video.video_key) for video in manifest.videos}
    direction_counts: Counter[str] = Counter(video.direction for video in manifest.videos)
    scale_counts: Counter[str] = Counter(video.comment_scale for video in manifest.videos)
    missing_directions = {
        direction
        for direction, target in DIRECTION_TARGETS.items()
        if direction_counts[direction] < target
    }
    missing_scales = {
        scale for scale, target in SCALE_TARGETS.items() if scale_counts[scale] < target
    }

    unselected: dict[tuple[Platform, str], CandidateVideo] = {}
    for candidate in candidates:
        key = (candidate.platform, candidate.video_key)
        if key not in selected and key not in unselected:
            unselected[key] = candidate

    def priority(candidate: CandidateVideo) -> tuple[int, int, int, str]:
        scale = _comment_scale(candidate.reported_comment_count)
        scale_priority = 2 if scale is None else int(scale not in missing_scales)
        return (
            int(candidate.direction not in missing_directions),
            scale_priority,
            candidate.source_rank,
            candidate.video_key,
        )

    return sorted(unselected.values(), key=priority)


def validate_coverage(
    manifest: CollectionManifest,
    candidates: Iterable[CandidateVideo] = (),
) -> CoverageReport:
    """Report exact coverage gaps and rank unselected candidate suggestions."""

    platform_counts = _distribution((video.platform for video in manifest.videos), PLATFORM_TARGETS)
    direction_counts = _distribution(
        (video.direction for video in manifest.videos), DIRECTION_TARGETS
    )
    scale_counts = _distribution((video.comment_scale for video in manifest.videos), SCALE_TARGETS)
    gaps: list[CoverageGap] = []

    if len(manifest.videos) != VIDEO_TARGET:
        gaps.append(
            CoverageGap(code="video_total", expected=VIDEO_TARGET, actual=len(manifest.videos))
        )
    unavailable = list(dict.fromkeys(manifest.unavailable_platforms))
    if unavailable:
        gaps.append(
            CoverageGap(
                code="platform_unavailable",
                expected=PLATFORM_TARGETS,
                actual=platform_counts,
                unavailable_platforms=unavailable,
            )
        )
    elif platform_counts != PLATFORM_TARGETS:
        gaps.append(
            CoverageGap(
                code="platform_distribution",
                expected=PLATFORM_TARGETS,
                actual=platform_counts,
            )
        )
    if direction_counts != DIRECTION_TARGETS:
        gaps.append(
            CoverageGap(
                code="direction_distribution",
                expected=DIRECTION_TARGETS,
                actual=direction_counts,
            )
        )
    if scale_counts != SCALE_TARGETS:
        gaps.append(
            CoverageGap(
                code="scale_distribution",
                expected=SCALE_TARGETS,
                actual=scale_counts,
            )
        )

    return CoverageReport(
        valid=not gaps,
        gaps=gaps,
        suggestions=_suggestions(manifest, candidates),
    )


__all__ = [
    "DIRECTION_TARGETS",
    "PLATFORM_TARGETS",
    "SCALE_TARGETS",
    "CoverageGap",
    "CoverageReport",
    "validate_coverage",
]
