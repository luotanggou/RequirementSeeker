"""Strict parsers for candidate-only Bilibili and Douyin listings."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from ..contracts import Direction
from .contracts import CandidateVideo

FORBIDDEN_CANDIDATE_KEYS = frozenset({"comments", "replies", "comment_list", "reply_list"})


class CandidateShapeChanged(ValueError):
    """Raised when a candidate response no longer matches a supported shape."""


class CandidateContainsComments(CandidateShapeChanged):
    """Raised before a payload containing comment collections is parsed."""


def reject_comment_payload(value: object) -> None:
    """Reject comment-bearing payloads without visiting the comment values."""
    if isinstance(value, Mapping):
        if any(str(key).lower() in FORBIDDEN_CANDIDATE_KEYS for key in value):
            raise CandidateContainsComments("candidate_payload_contains_comments")
        for child in value.values():
            reject_comment_payload(child)
    elif isinstance(value, list):
        for child in value:
            reject_comment_payload(child)


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CandidateShapeChanged("candidate_shape_changed")
    return value


def _sequence(value: object) -> list[object]:
    if not isinstance(value, list):
        raise CandidateShapeChanged("candidate_shape_changed")
    return value


def _identifier(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise CandidateShapeChanged("candidate_shape_changed")
    identifier = str(value)
    if not identifier or any(character.isspace() for character in identifier):
        raise CandidateShapeChanged("candidate_shape_changed")
    return identifier


def _title(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CandidateShapeChanged("candidate_shape_changed")
    return value


def _optional_count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _is_douyin_related_word_card(item: Mapping[str, Any]) -> bool:
    return (
        "aweme_info" not in item
        and type(item.get("type")) is int
        and item.get("type") == 6
        and type(item.get("doc_type")) is int
        and item.get("doc_type") == 108
        and type(item.get("card_type")) is int
        and item.get("card_type") == 6
        and type(item.get("card_unique_name")) is str
        and item.get("card_unique_name") == "related_word"
    )


def parse_bilibili_candidates(
    payload: Mapping[str, Any],
    query: str | None,
    source_page: str,
    discovered_at: datetime | str,
    *,
    direction: Direction,
) -> list[CandidateVideo]:
    """Parse the supported Bilibili search result array in page order."""
    reject_comment_payload(payload)
    try:
        if type(payload.get("code")) is not int or payload.get("code") != 0:
            raise CandidateShapeChanged("candidate_shape_changed")
        result = _sequence(_mapping(payload.get("data")).get("result"))
        candidates: list[CandidateVideo] = []
        for rank, value in enumerate(result, start=1):
            item = _mapping(value)
            video_key = _identifier(item.get("bvid"))
            candidates.append(
                CandidateVideo.model_validate(
                    {
                        "platform": "bilibili",
                        "video_key": video_key,
                        "url": f"https://www.bilibili.com/video/{video_key}",
                        "title": _title(item.get("title")),
                        "reported_comment_count": _optional_count(item.get("review")),
                        "direction": direction,
                        "query": query,
                        "source_page": source_page,
                        "source_rank": rank,
                        "discovered_at": discovered_at,
                    }
                )
            )
        return candidates
    except (CandidateShapeChanged, ValidationError, TypeError, ValueError):
        raise CandidateShapeChanged("candidate_shape_changed") from None


def parse_douyin_candidates(
    payload: Mapping[str, Any],
    query: str | None,
    source_page: str,
    discovered_at: datetime | str,
    *,
    direction: Direction,
) -> list[CandidateVideo]:
    """Parse the supported Douyin search result array in page order."""
    reject_comment_payload(payload)
    try:
        if type(payload.get("status_code")) is not int or payload.get("status_code") != 0:
            raise CandidateShapeChanged("candidate_shape_changed")
        result = _sequence(payload.get("data"))
        candidates: list[CandidateVideo] = []
        for rank, value in enumerate(result, start=1):
            item = _mapping(value)
            if _is_douyin_related_word_card(item):
                continue
            detail = _mapping(item.get("aweme_info"))
            statistics_value = detail.get("statistics")
            statistics = {} if statistics_value is None else _mapping(statistics_value)
            video_key = _identifier(detail.get("aweme_id"))
            candidates.append(
                CandidateVideo.model_validate(
                    {
                        "platform": "douyin",
                        "video_key": video_key,
                        "url": f"https://www.douyin.com/video/{video_key}",
                        "title": _title(detail.get("desc")),
                        "reported_comment_count": _optional_count(statistics.get("comment_count")),
                        "direction": direction,
                        "query": query,
                        "source_page": source_page,
                        "source_rank": rank,
                        "discovered_at": discovered_at,
                    }
                )
            )
        return candidates
    except (CandidateShapeChanged, ValidationError, TypeError, ValueError):
        raise CandidateShapeChanged("candidate_shape_changed") from None


__all__ = [
    "CandidateContainsComments",
    "CandidateShapeChanged",
    "parse_bilibili_candidates",
    "parse_douyin_candidates",
    "reject_comment_payload",
]
