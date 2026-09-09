from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel, HttpUrl, ValidationError

from requirementseeker_collector.contracts import (
    CollectionError,
    CollectionManifest,
    CollectionRecord,
    Contract,
    ManifestVideo,
    RawComment,
    RawVideo,
)


def video_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "platform": "bilibili",
        "raw_video_id": "BV1test",
        "raw_author_id": "author-1",
        "title": "测试视频",
        "description": "用于契约测试",
        "published_at": "2026-09-08T02:00:00Z",
        "duration_seconds": 120,
        "total_comment_count": 20,
        "view_count": 1000,
        "like_count": 100,
        "favorite_count": 50,
        "share_count": 10,
        "author_follower_count": 5000,
        "captured_at": "2026-09-09T01:00:00Z",
    }
    data.update(overrides)
    return data


def comment_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "raw_comment_id": "comment-1",
        "raw_author_id": "author-2",
        "raw_parent_comment_id": None,
        "text": "这个功能很有用",
        "published_at": "2026-09-08T03:00:00Z",
        "collected_at": "2026-09-09T01:00:00Z",
        "like_count": 3,
        "reply_count": 1,
        "is_video_author": False,
        "source_stratum": "top",
        "source_page_or_rank": 1,
    }
    data.update(overrides)
    return data


def error_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "category": "parse_error",
        "occurred_at": "2026-09-09T01:30:00Z",
        "stage": "comments",
        "description": "评论字段解析失败",
        "raw_comment_id": None,
        "conflict_fields": [],
    }
    data.update(overrides)
    return data


def collection_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "reported_total": 20,
        "collected_total": 18,
        "pages_requested": 2,
        "pages_succeeded": 2,
        "sort_modes": ["top", "recent"],
        "collection_started_at": "2026-09-09T01:00:00Z",
        "collection_finished_at": "2026-09-09T02:00:00Z",
        "collection_errors": [error_data()],
    }
    data.update(overrides)
    return data


def manifest_video_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "platform": "bilibili",
        "video_key": "BV1test",
        "url": "https://www.bilibili.com/video/BV1test",
        "direction": "software_tools",
        "comment_scale": "up_to_200",
    }
    data.update(overrides)
    return data


def manifest_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "manifest_version": "1.0",
        "videos": [manifest_video_data()],
        "unavailable_platforms": [],
    }
    data.update(overrides)
    return data


def test_contract_does_not_validate_assignment() -> None:
    assert Contract.model_config.get("validate_assignment") is not True


def test_video_rejects_unknown_fields() -> None:
    data = video_data()
    data["cookie"] = "secret"
    with pytest.raises(ValidationError):
        RawVideo.model_validate(data)


def test_comment_keeps_missing_values_explicit() -> None:
    comment = RawComment.model_validate(comment_data(raw_author_id=None, published_at=None))
    assert comment.raw_author_id is None
    assert comment.published_at is None


def test_nullable_comment_fields_are_still_required() -> None:
    data = comment_data()
    del data["raw_author_id"]
    with pytest.raises(ValidationError):
        RawComment.model_validate(data)


def test_collection_rejects_inverted_times() -> None:
    with pytest.raises(ValidationError, match="collection_finished_before_started"):
        CollectionRecord.model_validate(
            collection_data(
                collection_started_at="2026-09-09T02:00:00Z",
                collection_finished_at="2026-09-09T01:00:00Z",
            )
        )


def test_collection_rejects_more_successes_than_requests() -> None:
    with pytest.raises(ValidationError, match="pages_succeeded_exceeds_requested"):
        CollectionRecord.model_validate(collection_data(pages_requested=1, pages_succeeded=2))


@pytest.mark.parametrize(
    ("model", "factory", "field"),
    [
        (RawVideo, video_data, "duration_seconds"),
        (RawVideo, video_data, "like_count"),
        (RawComment, comment_data, "reply_count"),
        (CollectionRecord, collection_data, "collected_total"),
        (CollectionRecord, collection_data, "pages_requested"),
    ],
)
@pytest.mark.parametrize("value", [-1, "1", True])
def test_non_negative_integers_are_strict(
    model: type[BaseModel], factory: Callable[..., dict[str, Any]], field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(factory(**{field: value}))


@pytest.mark.parametrize("value", [0, -1, "1", True])
def test_source_rank_is_a_strict_positive_integer(value: object) -> None:
    with pytest.raises(ValidationError):
        RawComment.model_validate(comment_data(source_page_or_rank=value))


@pytest.mark.parametrize("value", [0, 1, "false", "true"])
def test_is_video_author_is_a_strict_nullable_boolean(value: object) -> None:
    with pytest.raises(ValidationError):
        RawComment.model_validate(comment_data(is_video_author=value))


@pytest.mark.parametrize(
    ("model", "data"),
    [
        (RawVideo, video_data(captured_at="2026-09-09T01:00:00")),
        (RawComment, comment_data(collected_at="2026-09-09T01:00:00")),
        (CollectionError, error_data(occurred_at="2026-09-09T01:00:00")),
        (
            CollectionRecord,
            collection_data(collection_started_at="2026-09-09T01:00:00"),
        ),
    ],
)
def test_timestamps_require_timezone(model: type[BaseModel], data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="timestamp_must_be_rfc3339_with_timezone"):
        model.model_validate(data)


def test_timestamps_require_rfc3339_separator() -> None:
    with pytest.raises(ValidationError, match="timestamp_must_be_rfc3339_with_timezone"):
        RawVideo.model_validate(video_data(captured_at="2026-09-09 01:00:00Z"))


def test_timestamps_normalize_to_utc() -> None:
    video = RawVideo.model_validate(video_data(captured_at="2026-09-09T09:00:00+08:00"))
    assert video.captured_at.isoformat() == "2026-09-09T01:00:00+00:00"


@pytest.mark.parametrize("value", ["", "has space", 123])
def test_identifiers_are_nonempty_strict_and_whitespace_free(value: object) -> None:
    with pytest.raises(ValidationError):
        RawVideo.model_validate(video_data(raw_video_id=value))


def test_comment_text_requires_non_whitespace_content() -> None:
    with pytest.raises(ValidationError):
        RawComment.model_validate(comment_data(text=" \n\t"))


@pytest.mark.parametrize("description", ["", "x" * 501])
def test_collection_error_description_length_is_bounded(description: str) -> None:
    with pytest.raises(ValidationError):
        CollectionError.model_validate(error_data(description=description))


def test_collection_error_conflict_fields_are_limited() -> None:
    with pytest.raises(ValidationError):
        CollectionError.model_validate(error_data(conflict_fields=["published_at"]))


@pytest.mark.parametrize("url", ["http://www.bilibili.com/video/BV1test", "ftp://bilibili.com/x"])
def test_manifest_urls_require_https(url: str) -> None:
    with pytest.raises(ValidationError, match="url_must_use_https"):
        ManifestVideo.model_validate(manifest_video_data(url=url))


def test_manifest_urls_reject_credentials() -> None:
    with pytest.raises(ValidationError, match="url_must_not_include_credentials"):
        ManifestVideo.model_validate(
            manifest_video_data(url="https://user:secret@www.bilibili.com/video/BV1test")
        )


def test_manifest_video_python_dump_can_be_revalidated() -> None:
    video = ManifestVideo.model_validate(manifest_video_data())
    assert ManifestVideo.model_validate(video.model_dump()) == video


@pytest.mark.parametrize(
    "url",
    [
        "https://foo_bar.bilibili.com/video/BV1",
        "https://-foo.bilibili.com/video/BV1",
        "https://foo-.bilibili.com/video/BV1",
    ],
)
def test_manifest_rejects_preparsed_urls_with_invalid_dns_labels(url: str) -> None:
    with pytest.raises(ValidationError):
        ManifestVideo.model_validate(manifest_video_data(url=HttpUrl(url)))


@pytest.mark.parametrize(
    ("platform", "url"),
    [
        ("bilibili", "https://www.douyin.com/video/1"),
        ("douyin", "https://www.bilibili.com/video/BV1test"),
        ("bilibili", "https://bilibili.com.evil.example/video/BV1test"),
    ],
)
def test_manifest_url_host_must_match_platform(platform: str, url: str) -> None:
    with pytest.raises(ValidationError, match="url_host_does_not_match_platform"):
        ManifestVideo.model_validate(manifest_video_data(platform=platform, url=url))


@pytest.mark.parametrize(
    ("platform", "url"),
    [
        ("bilibili", "https://b23.tv/example"),
        ("bilibili", "https://m.bilibili.com/video/BV1test"),
        ("douyin", "https://www.douyin.com/video/1"),
        ("douyin", "https://v.douyin.com/example"),
        ("douyin", "https://www.iesdouyin.com/share/video/1"),
    ],
)
def test_manifest_accepts_known_platform_hosts(platform: str, url: str) -> None:
    video = ManifestVideo.model_validate(manifest_video_data(platform=platform, url=url))
    assert video.platform == platform


def test_manifest_rejects_duplicate_platform_video_keys() -> None:
    video = manifest_video_data()
    with pytest.raises(ValidationError, match="duplicate_platform_video_key"):
        CollectionManifest.model_validate(manifest_data(videos=[video, video.copy()]))


def test_manifest_allows_same_video_key_on_different_platforms() -> None:
    videos = [
        manifest_video_data(),
        manifest_video_data(
            platform="douyin", video_key="BV1test", url="https://www.douyin.com/video/1"
        ),
    ]
    assert len(CollectionManifest.model_validate(manifest_data(videos=videos)).videos) == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("manifest_version", "2.0"),
        ("unavailable_platforms", ["youtube"]),
    ],
)
def test_manifest_literals_are_closed(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        CollectionManifest.model_validate(manifest_data(**{field: value}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("direction", "news"),
        ("comment_scale", "huge"),
    ],
)
def test_manifest_video_literals_are_closed(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        ManifestVideo.model_validate(manifest_video_data(**{field: value}))
