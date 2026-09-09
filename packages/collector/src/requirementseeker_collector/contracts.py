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

_DNS_LABEL_PATTERN = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_DNS_HOST_PATTERN = rf"{_DNS_LABEL_PATTERN}(?:\.{_DNS_LABEL_PATTERN})*"
_PORT_PATTERN = (
    r"0*(?:[0-9]{1,4}|[1-5][0-9]{4}|6[0-4][0-9]{3}|"
    r"65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5])"
)


def _https_url(value: object) -> object:
    if not isinstance(value, (str, HttpUrl)):
        raise ValueError("url_must_be_valid")
    try:
        parsed = urlsplit(str(value))
        host = parsed.hostname
    except ValueError:
        raise ValueError("url_must_be_valid") from None
    if parsed.scheme.lower() != "https":
        raise ValueError("url_must_use_https")
    if host is None or re.fullmatch(_DNS_HOST_PATTERN, host) is None:
        raise ValueError("url_host_must_use_ascii_dns_labels")
    host_and_port = parsed.netloc.rsplit("@", 1)[-1]
    if ":" in host_and_port:
        raw_port = host_and_port.rsplit(":", 1)[-1]
        if re.fullmatch(_PORT_PATTERN, raw_port) is None:
            raise ValueError("url_port_must_be_between_0_and_65535")
    return value


def _url_without_credentials(value: HttpUrl) -> HttpUrl:
    if value.username is not None or value.password is not None:
        raise ValueError("url_must_not_include_credentials")
    return value


PublicHttpUrl = Annotated[
    HttpUrl,
    BeforeValidator(_https_url),
    AfterValidator(_url_without_credentials),
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


def _ascii_case_insensitive_literal(value: str) -> str:
    return "".join(
        f"[{character.upper()}{character.lower()}]"
        if character.isascii() and character.isalpha()
        else re.escape(character)
        for character in value
    )


def _platform_url_pattern(hosts: tuple[str, ...]) -> str:
    scheme = _ascii_case_insensitive_literal("https")
    roots = "|".join(_ascii_case_insensitive_literal(host) for host in hosts)
    return (
        rf"^{scheme}://(?:{_DNS_LABEL_PATTERN}\.)*"
        rf"(?:{roots})(?::{_PORT_PATTERN})?(?:[/?#]|$)"
    )


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
                            "url": {"pattern": _platform_url_pattern(_PLATFORM_HOSTS["bilibili"])}
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
                            "url": {"pattern": _platform_url_pattern(_PLATFORM_HOSTS["douyin"])}
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
