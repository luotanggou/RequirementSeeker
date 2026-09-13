"""Strict contracts for candidate video discovery."""

from typing import Self

from pydantic import ConfigDict, model_validator

from ..contracts import (
    _DNS_LABEL_PATTERN,
    _PLATFORM_HOSTS,
    _PORT_PATTERN,
    CollectionManifest,
    Contract,
    Direction,
    Identifier,
    ManifestVideo,
    NonNegativeInt,
    Platform,
    PositiveInt,
    PublicHttpUrl,
    Timestamp,
)

_PUBLIC_HTTPS_URL_PATTERN = (
    rf"^[Hh][Tt][Tt][Pp][Ss]://(?![^/?#]*@)"
    rf"(?:{_DNS_LABEL_PATTERN}\.)*{_DNS_LABEL_PATTERN}"
    rf"(?::{_PORT_PATTERN})?(?:[/?#][^\x00-\x20\x7f]*)?(?![\s\S])"
)


class CandidateVideo(Contract):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                *ManifestVideo.model_json_schema()["allOf"],
                {"properties": {"source_page": {"pattern": _PUBLIC_HTTPS_URL_PATTERN}}},
            ]
        }
    )

    platform: Platform
    video_key: Identifier
    url: PublicHttpUrl
    title: str
    reported_comment_count: NonNegativeInt | None
    direction: Direction
    query: str | None
    source_page: PublicHttpUrl
    source_rank: PositiveInt
    discovered_at: Timestamp

    @model_validator(mode="after")
    def url_matches_platform(self) -> Self:
        host = self.url.host
        known_hosts = _PLATFORM_HOSTS[self.platform]
        if host is None or not any(
            host == root or host.endswith(f".{root}") for root in known_hosts
        ):
            raise ValueError("url_platform_mismatch")
        return self


__all__ = ["CandidateVideo", "CollectionManifest", "Direction", "ManifestVideo"]
