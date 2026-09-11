"""Strict parser for supported Bilibili JSON response families."""

from collections.abc import Mapping
from dataclasses import dataclass
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
    required,
    required_identifier,
    required_text,
    sequence,
    utc_now,
)

_RESPONSE_PATHS: dict[str, ResponseKind] = {
    "/x/web-interface/view": "video",
    "/x/v2/reply/wbi/main": "comments",
    "/x/v2/reply/reply": "replies",
}


@dataclass(frozen=True)
class BilibiliCommentContext:
    video_author_id: str
    total_comment_count: int


def _comment(
    reply: Mapping[str, Any],
    stratum: Stratum,
    rank: int,
    video_author_id: str,
    parent: str | None = None,
) -> RawComment:
    member = mapping(reply.get("member"))
    content = mapping(reply.get("content"))
    author_id = optional_identifier(member.get("mid"))
    return RawComment(
        raw_comment_id=required_identifier(reply.get("rpid")),
        raw_author_id=author_id,
        raw_parent_comment_id=parent,
        text=required_text(content.get("message")),
        published_at=from_unix(reply.get("ctime")),
        collected_at=utc_now(),
        like_count=optional_int(reply.get("like")),
        reply_count=optional_int(reply.get("rcount")),
        is_video_author=(author_id == video_author_id) if author_id is not None else None,
        source_stratum=stratum,
        source_page_or_rank=rank,
    )


class BilibiliAdapter:
    def response_kind(self, url: str) -> ResponseKind | None:
        path = known_platform_path(url, ("bilibili.com",))
        return _RESPONSE_PATHS.get(path) if path is not None else None

    def parse_video_response(self, payload: Mapping[str, Any]) -> ParsedVideo:
        try:
            if payload.get("code") != 0:
                raise ResponseShapeChanged("response_shape_changed")
            data = mapping(payload.get("data"))
            owner = mapping(data.get("owner"))
            stats = mapping(data.get("stat"))
            video = RawVideo(
                platform="bilibili",
                raw_video_id=required_identifier(data.get("bvid")),
                raw_author_id=required_identifier(owner.get("mid")),
                title=required_text(data.get("title")),
                description=required_text(data.get("desc")),
                published_at=from_unix(data.get("pubdate")),
                duration_seconds=optional_int(data.get("duration")),
                total_comment_count=optional_int(stats.get("reply")),
                view_count=optional_int(stats.get("view")),
                like_count=optional_int(stats.get("like")),
                favorite_count=optional_int(stats.get("favorite")),
                share_count=optional_int(stats.get("share")),
                author_follower_count=None,
                captured_at=utc_now(),
            )
        except (ResponseShapeChanged, ValidationError, TypeError, ValueError):
            raise ResponseShapeChanged("response_shape_changed") from None
        return ParsedVideo(video)

    def parse_comment_context(self, payload: Mapping[str, Any]) -> BilibiliCommentContext:
        try:
            if payload.get("code") != 0:
                raise ResponseShapeChanged("response_shape_changed")
            data = mapping(payload.get("data"))
            upper = mapping(data.get("upper"))
            cursor = mapping(data.get("cursor"))
            total = optional_int(cursor.get("all_count"))
            if total is None:
                raise ResponseShapeChanged("response_shape_changed")
            return BilibiliCommentContext(
                video_author_id=required_identifier(upper.get("mid")),
                total_comment_count=total,
            )
        except (ResponseShapeChanged, TypeError, ValueError):
            raise ResponseShapeChanged("response_shape_changed") from None

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
            if payload.get("code") != 0:
                raise ResponseShapeChanged("response_shape_changed")
            data = mapping(payload.get("data"))
            replies = sequence(data.get("replies"))
            comments: list[RawComment] = []
            for value in replies:
                reply = mapping(value)
                item = _comment(reply, stratum, rank, video_author_id, parent_comment_id)
                comments.append(item)
                if parent_comment_id is None:
                    nested = reply.get("replies")
                    if nested is None:
                        continue
                    for nested_value in sequence(nested):
                        comments.append(
                            _comment(
                                mapping(nested_value),
                                stratum,
                                rank,
                                video_author_id,
                                item.raw_comment_id,
                            )
                        )
            cursor = mapping(required(data, "cursor"))
            is_end = cursor.get("is_end")
            if type(is_end) is not bool:
                raise ResponseShapeChanged("response_shape_changed")
            has_more = not is_end
            next_cursor = optional_identifier(cursor.get("next"))
            if has_more and next_cursor is None:
                raise ResponseShapeChanged("response_shape_changed")
        except (ResponseShapeChanged, ValidationError, TypeError, ValueError):
            raise ResponseShapeChanged("response_shape_changed") from None
        return ParsedCommentPage(comments, has_more, next_cursor)
