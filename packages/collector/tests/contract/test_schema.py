import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel

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
        ("bilibili", "http://www.bilibili.com/video/BV1test"),
        ("bilibili", "https://user:secret@www.bilibili.com/video/BV1test"),
        ("bilibili", "https://www.douyin.com/video/1"),
        ("douyin", "https://www.bilibili.com/video/BV1test"),
    ],
)
def test_manifest_schema_rejects_non_public_or_cross_platform_urls(
    platform: str, url: str
) -> None:
    schema = json.loads(
        (ROOT / "schemas" / "collection-manifest.schema.json").read_text(encoding="utf-8")
    )
    document = json.loads(json.dumps(VALID_DOCUMENTS["collection-manifest"]))
    document["videos"][0]["platform"] = platform
    document["videos"][0]["url"] = url
    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
