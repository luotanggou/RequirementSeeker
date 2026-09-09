"""Strict contracts at the raw collection boundary."""

import re
from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    HttpUrl,
    StrictBool,
    model_validator,
)

Platform = Literal["bilibili", "douyin"]
Stratum = Literal["top", "recent", "replies", "long_tail"]
Direction = Literal[
    "software_tools",
    "tutorial_workflow",
    "life_services",
    "entertainment_culture",
    "ecommerce_marketing",
]
CommentScale = Literal["up_to_200", "201_to_2000", "over_2000"]

Identifier = Annotated[str, Field(strict=True, min_length=1, max_length=256, pattern=r"^\S+$")]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _timestamp(value: object) -> object:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        raise ValueError("timestamp_must_be_rfc3339_with_timezone")
    return value


def _utc(value: datetime) -> datetime:
    try:
        return value.astimezone(UTC)
    except OverflowError:
        raise ValueError("timestamp_out_of_supported_range") from None


Timestamp = Annotated[AwareDatetime, BeforeValidator(_timestamp), AfterValidator(_utc)]


def _https_url(value: object) -> object:
    if isinstance(value, HttpUrl):
        scheme = value.scheme
    elif isinstance(value, str):
        scheme = urlsplit(value).scheme
    else:
        scheme = ""
    if scheme.lower() != "https":
        raise ValueError("url_must_use_https")
    return value


def _url_without_credentials(value: HttpUrl) -> HttpUrl:
    if value.username is not None or value.password is not None:
        raise ValueError("url_must_not_include_credentials")
    return value


PublicHttpUrl = Annotated[
    HttpUrl,
    BeforeValidator(_https_url),
    AfterValidator(_url_without_credentials),
    Field(json_schema_extra={"pattern": r"^https://(?![^/?#]*@)"}),
]


class RawVideo(Contract):
    platform: Platform
    raw_video_id: Identifier
    raw_author_id: Identifier
    title: str
    description: str
    published_at: Timestamp | None
    duration_seconds: NonNegativeInt | None
    total_comment_count: NonNegativeInt | None
    view_count: NonNegativeInt | None
    like_count: NonNegativeInt | None
    favorite_count: NonNegativeInt | None
    share_count: NonNegativeInt | None
    author_follower_count: NonNegativeInt | None
    captured_at: Timestamp


class RawComment(Contract):
    raw_comment_id: Identifier
    raw_author_id: Identifier | None
    raw_parent_comment_id: Identifier | None
    text: Annotated[str, Field(min_length=1, pattern=r"\S")]
    published_at: Timestamp | None
    collected_at: Timestamp
    like_count: NonNegativeInt | None
    reply_count: NonNegativeInt | None
    is_video_author: StrictBool | None
    source_stratum: Stratum
    source_page_or_rank: PositiveInt


class CollectionError(Contract):
    category: Identifier
    occurred_at: Timestamp
    stage: Identifier
    description: Annotated[str, Field(min_length=1, max_length=500)]
    raw_comment_id: Identifier | None = None
    conflict_fields: list[Literal["raw_author_id", "text"]] = Field(default_factory=list)


class CollectionRecord(Contract):
    reported_total: NonNegativeInt | None
    collected_total: NonNegativeInt
    pages_requested: NonNegativeInt
    pages_succeeded: NonNegativeInt
    sort_modes: list[Identifier]
    collection_started_at: Timestamp
    collection_finished_at: Timestamp
    collection_errors: list[CollectionError]

    @model_validator(mode="after")
    def integrity(self) -> Self:
        if self.pages_succeeded > self.pages_requested:
            raise ValueError("pages_succeeded_exceeds_requested")
        if self.collection_finished_at < self.collection_started_at:
            raise ValueError("collection_finished_before_started")
        return self


_PLATFORM_HOSTS: dict[Platform, tuple[str, ...]] = {
    "bilibili": ("bilibili.com", "b23.tv"),
    "douyin": ("douyin.com", "iesdouyin.com"),
}


class ManifestVideo(Contract):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"platform": {"const": "bilibili"}},
                        "required": ["platform"],
                    },
                    "then": {
                        "properties": {
                            "url": {
                                "pattern": (
                                    r"^https://(?:[^@/?#]+\.)*"
                                    r"(?:bilibili\.com|b23\.tv)(?::\d+)?(?:[/?#]|$)"
                                )
                            }
                        }
                    },
                },
                {
                    "if": {
                        "properties": {"platform": {"const": "douyin"}},
                        "required": ["platform"],
                    },
                    "then": {
                        "properties": {
                            "url": {
                                "pattern": (
                                    r"^https://(?:[^@/?#]+\.)*"
                                    r"(?:douyin\.com|iesdouyin\.com)(?::\d+)?(?:[/?#]|$)"
                                )
                            }
                        }
                    },
                },
            ]
        }
    )

    platform: Platform
    video_key: Identifier
    url: PublicHttpUrl
    direction: Direction
    comment_scale: CommentScale

    @model_validator(mode="after")
    def host_matches_platform(self) -> Self:
        host = self.url.host
        known_hosts = _PLATFORM_HOSTS[self.platform]
        if host is None or not any(
            host == root or host.endswith(f".{root}") for root in known_hosts
        ):
            raise ValueError("url_host_does_not_match_platform")
        return self


class CollectionManifest(Contract):
    manifest_version: Literal["1.0"]
    videos: list[ManifestVideo]
    unavailable_platforms: list[Platform]

    @model_validator(mode="after")
    def unique_keys(self) -> Self:
        keys = [(item.platform, item.video_key) for item in self.videos]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate_platform_video_key")
        return self
