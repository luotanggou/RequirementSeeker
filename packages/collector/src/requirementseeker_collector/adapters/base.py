"""Shared types and strict parsing helpers for platform responses."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from requirementseeker_collector.contracts import RawComment, RawVideo, Stratum

type ResponseKind = Literal["video", "comments", "replies"]


class ResponseShapeChanged(ValueError):
    """Raised when a successful response no longer matches a supported shape."""


@dataclass(frozen=True)
class ParsedVideo:
    video: RawVideo


@dataclass(frozen=True)
class ParsedCommentPage:
    comments: list[RawComment]
    has_more: bool
    next_cursor: str | None


class PlatformAdapter(Protocol):
    def response_kind(self, url: str) -> ResponseKind | None: ...

    def parse_video_response(self, payload: Mapping[str, Any]) -> ParsedVideo: ...

    def parse_comment_response(
        self,
        payload: Mapping[str, Any],
        stratum: Stratum,
        rank: int,
        *,
        video_author_id: str,
        parent_comment_id: str | None = None,
    ) -> ParsedCommentPage: ...


def mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResponseShapeChanged("response_shape_changed")
    return value


def sequence(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ResponseShapeChanged("response_shape_changed")
    return value


def required(source: Mapping[str, Any], key: str) -> object:
    value = source.get(key)
    if value is None:
        raise ResponseShapeChanged("response_shape_changed")
    return value


def optional_identifier(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    identifier = str(value)
    if not identifier or any(character.isspace() for character in identifier):
        return None
    return identifier


def optional_int(value: object) -> int | None:
    if type(value) is not int or value < 0:
        return None
    return value


def from_unix(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    timestamp = float(value)
    if not isfinite(timestamp):
        return None
    try:
        return datetime.fromtimestamp(timestamp, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def utc_now() -> datetime:
    return datetime.now(UTC)


def known_platform_path(url: str, roots: tuple[str, ...]) -> str | None:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme != "https" or host is None:
        return None
    if not any(host == root or host.endswith(f".{root}") for root in roots):
        return None
    return parsed.path.rstrip("/")
