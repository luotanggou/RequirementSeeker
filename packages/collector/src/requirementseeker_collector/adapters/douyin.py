"""Strict parser for supported Douyin JSON response families."""

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from requirementseeker_collector.contracts import RawComment, RawVideo, Stratum

from .base import (
    ParsedCommentPage,
    ParsedVideo,
    ResponseKind,
    ResponseShapeChanged,
    from_unix,
    known_platform_path,
    mapping,
    optional_identifier,
    optional_int,
    required_identifier,
    required_text,
    sequence,
    utc_now,
)

_RESPONSE_PATHS: dict[str, ResponseKind] = {
    "/aweme/v1/web/aweme/detail": "video",
    "/aweme/v1/web/comment/list": "comments",
    "/aweme/v1/web/comment/list/reply": "replies",
}


def _comment(
    comment: Mapping[str, Any],
    stratum: Stratum,
    rank: int,
    video_author_id: str,
    parent: str | None = None,
) -> RawComment:
    user = mapping(comment.get("user"))
    author_id = optional_identifier(user.get("sec_uid")) or optional_identifier(user.get("uid"))
    return RawComment(
        raw_comment_id=required_identifier(comment.get("cid")),
        raw_author_id=author_id,
        raw_parent_comment_id=parent or optional_identifier(comment.get("reply_id")),
        text=required_text(comment.get("text")),
        published_at=from_unix(comment.get("create_time")),
        collected_at=utc_now(),
        like_count=optional_int(comment.get("digg_count")),
        reply_count=optional_int(comment.get("reply_comment_total")),
        is_video_author=(author_id == video_author_id) if author_id is not None else None,
        source_stratum=stratum,
        source_page_or_rank=rank,
    )


class DouyinAdapter:
    def response_kind(self, url: str) -> ResponseKind | None:
        path = known_platform_path(url, ("douyin.com", "iesdouyin.com"))
        return _RESPONSE_PATHS.get(path) if path is not None else None

    def parse_video_response(self, payload: Mapping[str, Any]) -> ParsedVideo:
        try:
            if payload.get("status_code") != 0:
                raise ResponseShapeChanged("response_shape_changed")
            detail = mapping(payload.get("aweme_detail"))
            author = mapping(detail.get("author"))
            stats = mapping(detail.get("statistics"))
            author_id = required_identifier(
                optional_identifier(author.get("sec_uid")) or optional_identifier(author.get("uid"))
            )
            duration_ms = optional_int(detail.get("duration"))
            description = required_text(detail.get("desc"))
            video = RawVideo(
                platform="douyin",
                raw_video_id=required_identifier(detail.get("aweme_id")),
                raw_author_id=author_id,
                title=description,
                description=description,
                published_at=from_unix(detail.get("create_time")),
                duration_seconds=duration_ms // 1000 if duration_ms is not None else None,
                total_comment_count=optional_int(stats.get("comment_count")),
                view_count=optional_int(stats.get("play_count")),
                like_count=optional_int(stats.get("digg_count")),
                favorite_count=optional_int(stats.get("collect_count")),
                share_count=optional_int(stats.get("share_count")),
                author_follower_count=optional_int(author.get("follower_count")),
                captured_at=utc_now(),
            )
        except (ResponseShapeChanged, ValidationError, TypeError, ValueError):
            raise ResponseShapeChanged("response_shape_changed") from None
        return ParsedVideo(video)

    def parse_comment_response(
        self,
        payload: Mapping[str, Any],
        stratum: Stratum,
        rank: int,
        *,
        video_author_id: str,
        parent_comment_id: str | None = None,
    ) -> ParsedCommentPage:
        try:
            if payload.get("status_code") != 0:
                raise ResponseShapeChanged("response_shape_changed")
            comments = []
            for value in sequence(payload.get("comments")):
                comment = mapping(value)
                text = comment.get("text")
                if isinstance(text, str) and not text.strip():
                    required_identifier(comment.get("cid"))
                    mapping(comment.get("user"))
                    continue
                comments.append(
                    _comment(comment, stratum, rank, video_author_id, parent_comment_id)
                )
            has_more_value = payload.get("has_more")
            if type(has_more_value) is not int or has_more_value not in {0, 1}:
                raise ResponseShapeChanged("response_shape_changed")
            has_more = has_more_value == 1
            next_cursor = optional_identifier(payload.get("cursor"))
            if has_more and next_cursor is None:
                raise ResponseShapeChanged("response_shape_changed")
        except (ResponseShapeChanged, ValidationError, TypeError, ValueError):
            raise ResponseShapeChanged("response_shape_changed") from None
        return ParsedCommentPage(comments, has_more, next_cursor)
