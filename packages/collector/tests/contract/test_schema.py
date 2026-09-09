import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, ValidationError

from requirementseeker_collector import (
    CollectionManifest,
    CollectionRecord,
    RawComment,
    RawVideo,
)
from requirementseeker_collector.schema import export_schema

ROOT = Path(__file__).parents[2]

SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "video": RawVideo,
    "comment": RawComment,
    "collection": CollectionRecord,
    "collection-manifest": CollectionManifest,
}

VALID_DOCUMENTS: dict[str, dict[str, Any]] = {
    "video": {
        "platform": "bilibili",
        "raw_video_id": "BV1test",
        "raw_author_id": "author-1",
        "title": "测试视频",
        "description": "用于契约测试",
        "published_at": None,
        "duration_seconds": 120,
        "total_comment_count": 20,
        "view_count": 1000,
        "like_count": 100,
        "favorite_count": 50,
        "share_count": 10,
        "author_follower_count": 5000,
        "captured_at": "2026-09-09T01:00:00Z",
    },
    "comment": {
        "raw_comment_id": "comment-1",
        "raw_author_id": None,
        "raw_parent_comment_id": None,
        "text": "这个功能很有用",
        "published_at": None,
        "collected_at": "2026-09-09T01:00:00Z",
        "like_count": 3,
        "reply_count": 1,
        "is_video_author": False,
        "source_stratum": "top",
        "source_page_or_rank": 1,
    },
    "collection": {
        "reported_total": 20,
        "collected_total": 18,
        "pages_requested": 2,
        "pages_succeeded": 2,
        "sort_modes": ["top", "recent"],
        "collection_started_at": "2026-09-09T01:00:00Z",
        "collection_finished_at": "2026-09-09T02:00:00Z",
        "collection_errors": [],
    },
    "collection-manifest": {
        "manifest_version": "1.0",
        "videos": [
            {
                "platform": "bilibili",
                "video_key": "BV1test",
                "url": "https://www.bilibili.com/video/BV1test",
                "direction": "software_tools",
                "comment_scale": "up_to_200",
            }
        ],
        "unavailable_platforms": [],
    },
}


def test_package_declares_typing_support() -> None:
    marker = ROOT / "src" / "requirementseeker_collector" / "py.typed"
    assert marker.is_file()


@pytest.mark.parametrize("kind", SCHEMA_MODELS)
def test_committed_schema_matches_generated_schema(kind: str) -> None:
    path = ROOT / "schemas" / f"{kind}.schema.json"
    assert path.exists(), "Versioned JSON Schema has not been exported"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved == export_schema(kind)
    assert saved == SCHEMA_MODELS[kind].model_json_schema()
    Draft202012Validator.check_schema(saved)


@pytest.mark.parametrize("kind", SCHEMA_MODELS)
def test_valid_documents_pass_standard_json_schema(kind: str) -> None:
    schema = json.loads((ROOT / "schemas" / f"{kind}.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(VALID_DOCUMENTS[kind])


@pytest.mark.parametrize(
    ("platform", "url"),
    [
        ("bilibili", "HTTPS://WWW.BILIBILI.COM/video/BV1"),
        ("bilibili", "https://WWW.BILIBILI.COM/video/BV1"),
        ("douyin", "HTTPS://WWW.DOUYIN.COM/video/1"),
        ("douyin", "https://WWW.IESDOUYIN.COM/share/video/1"),
    ],
)
def test_manifest_model_and_schema_accept_case_insensitive_urls(platform: str, url: str) -> None:
    schema = export_schema("collection-manifest")
    document = json.loads(json.dumps(VALID_DOCUMENTS["collection-manifest"]))
    document["videos"][0]["platform"] = platform
    document["videos"][0]["url"] = url
    CollectionManifest.model_validate(document)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)


def test_manifest_model_and_schema_reject_invalid_dns_label() -> None:
    schema = export_schema("collection-manifest")
    document = json.loads(json.dumps(VALID_DOCUMENTS["collection-manifest"]))
    document["videos"][0]["url"] = "https://user%40evil.bilibili.com/video/BV1"
    with pytest.raises(ValidationError):
        CollectionManifest.model_validate(document)
    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)


def manifest_document(url: str) -> dict[str, Any]:
    document = json.loads(json.dumps(VALID_DOCUMENTS["collection-manifest"]))
    document["videos"][0]["url"] = url
    return document


def model_accepts_manifest(document: dict[str, Any]) -> bool:
    try:
        CollectionManifest.model_validate(document)
    except ValidationError:
        return False
    return True


def schema_accepts_manifest(document: dict[str, Any]) -> bool:
    schema = export_schema("collection-manifest")
    try:
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
    except JsonSchemaValidationError:
        return False
    return True


@pytest.mark.parametrize(
    ("url", "accepted"),
    [
        ("https://foo_bar.bilibili.com/video/BV1", False),
        ("https://-foo.bilibili.com/video/BV1", False),
        ("https://foo-.bilibili.com/video/BV1", False),
        ("https://foo..bilibili.com/video/BV1", False),
        ("https://%62ilibili.com/video/BV1", False),
        ("https://bilibili.com:65536/video/BV1", False),
        ("https://bilibili.com:443/video/BV1", True),
        ("https://bilibili.com:65535/video/BV1", True),
    ],
)
def test_manifest_model_and_schema_agree_on_dns_labels_and_ports(url: str, accepted: bool) -> None:
    document = manifest_document(url)
    assert model_accepts_manifest(document) is accepted
    assert schema_accepts_manifest(document) is accepted


def test_manifest_model_and_schema_agree_on_leading_zero_port() -> None:
    document = manifest_document("https://bilibili.com:00080/video/BV1")
    assert model_accepts_manifest(document) is True
    assert schema_accepts_manifest(document) is True


@pytest.mark.parametrize(
    ("url", "accepted"),
    [
        (" https://bilibili.com/v", False),
        ("\x00https://bilibili.com/v", False),
        ("ht\ntps://bilibili.com/v", False),
        ("https:\n//bilibili.com/v", False),
        ("https://bilibili.com/v ", False),
        ("https://bilibili.com/v\t", False),
        ("https://bilibili.com/v\n", False),
        ("https://bilibili.com/path with space", False),
        ("https://bilibili.com/v\x7f", False),
        ("https://bilibili.com/path%20with%20space", True),
    ],
)
def test_manifest_model_and_schema_agree_on_raw_url_controls(url: str, accepted: bool) -> None:
    document = manifest_document(url)
    assert model_accepts_manifest(document) is accepted
    assert schema_accepts_manifest(document) is accepted


@pytest.mark.parametrize(
    ("platform", "url"),
    [
        ("bilibili", "http://www.bilibili.com/video/BV1test"),
        ("bilibili", "https://user:secret@www.bilibili.com/video/BV1test"),
        ("bilibili", "https://www.douyin.com/video/1"),
        ("douyin", "https://www.bilibili.com/video/BV1test"),
    ],
)
def test_manifest_schema_rejects_non_public_or_cross_platform_urls(platform: str, url: str) -> None:
    schema = json.loads(
        (ROOT / "schemas" / "collection-manifest.schema.json").read_text(encoding="utf-8")
    )
    document = json.loads(json.dumps(VALID_DOCUMENTS["collection-manifest"]))
    document["videos"][0]["platform"] = platform
    document["videos"][0]["url"] = url
    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
