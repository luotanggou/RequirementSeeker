import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError

from requirementseeker_collector.candidates.contracts import (
    CandidateVideo,
    CollectionManifest,
    Direction,
    ManifestVideo,
)
from requirementseeker_collector.contracts import (
    CollectionManifest as RootCollectionManifest,
)
from requirementseeker_collector.contracts import (
    Direction as RootDirection,
)
from requirementseeker_collector.contracts import (
    ManifestVideo as RootManifestVideo,
)

ROOT = Path(__file__).parents[2]


def candidate_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "platform": "bilibili",
        "video_key": "BV1test",
        "url": "https://www.bilibili.com/video/BV1test",
        "title": "测试视频",
        "reported_comment_count": 120,
        "direction": "software_tools",
        "query": "效率工具",
        "source_page": "https://search.bilibili.com/all?keyword=tool",
        "source_rank": 1,
        "discovered_at": "2026-09-13T01:00:00Z",
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


def test_candidate_accepts_complete_record_and_nullable_discovery_fields() -> None:
    candidate = CandidateVideo.model_validate(
        candidate_data(reported_comment_count=None, query=None)
    )

    assert candidate.reported_comment_count is None
    assert candidate.query is None
    assert candidate.model_dump(mode="json")["discovered_at"] == "2026-09-13T01:00:00Z"


def test_candidate_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        CandidateVideo.model_validate(candidate_data(cookie="secret"))


def test_candidate_requires_a_public_platform_url() -> None:
    with pytest.raises(ValidationError, match="url_platform_mismatch"):
        CandidateVideo.model_validate(
            candidate_data(platform="bilibili", url="https://www.douyin.com/video/1")
        )


@pytest.mark.parametrize(
    ("field", "url"),
    [
        ("url", "https://user:pass@bilibili.com/video/BVfake"),
        ("source_page", "https://user:pass@search.bilibili.com/all"),
    ],
)
def test_candidate_urls_reject_credentials(field: str, url: str) -> None:
    with pytest.raises(ValidationError, match="url_must_not_include_credentials"):
        CandidateVideo.model_validate(candidate_data(**{field: url}))


@pytest.mark.parametrize("source_rank", [0, -1, "1", True])
def test_candidate_source_rank_is_a_strict_positive_integer(source_rank: object) -> None:
    with pytest.raises(ValidationError):
        CandidateVideo.model_validate(candidate_data(source_rank=source_rank))


def test_candidate_contracts_reexport_root_manifest_types() -> None:
    assert CollectionManifest is RootCollectionManifest
    assert Direction is RootDirection
    assert ManifestVideo is RootManifestVideo


def test_manifest_rejects_duplicate_platform_video_key() -> None:
    item = ManifestVideo.model_validate(manifest_video_data())
    with pytest.raises(ValidationError, match="duplicate_platform_video_key"):
        CollectionManifest(manifest_version="1.0", videos=[item, item], unavailable_platforms=[])


def test_manifest_rejects_credentials_in_url() -> None:
    with pytest.raises(ValidationError, match="url_must_not_include_credentials"):
        ManifestVideo.model_validate(
            manifest_video_data(url="https://user:pass@bilibili.com/video/BVfake")
        )


def test_candidate_schema_is_versioned_and_matches_model() -> None:
    schema_path = ROOT / "schemas" / "candidate-video.schema.json"
    assert schema_path.is_file(), "Versioned candidate JSON Schema has not been exported"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert schema == CandidateVideo.model_json_schema()
    Draft202012Validator.check_schema(schema)


def test_candidate_schema_rejects_cross_platform_url_and_unknown_fields() -> None:
    schema = CandidateVideo.model_json_schema()
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    with pytest.raises(JsonSchemaValidationError):
        validator.validate(
            candidate_data(platform="douyin", url="https://www.bilibili.com/video/BV1test")
        )
    with pytest.raises(JsonSchemaValidationError):
        validator.validate(candidate_data(cookie="secret"))


@pytest.mark.parametrize(
    "source_page",
    [
        "http://search.bilibili.com/all?keyword=tool",
        "https://user:pass@search.bilibili.com/all?keyword=tool",
    ],
)
def test_candidate_schema_rejects_non_public_source_page(source_page: str) -> None:
    validator = Draft202012Validator(
        CandidateVideo.model_json_schema(), format_checker=FormatChecker()
    )

    with pytest.raises(JsonSchemaValidationError):
        validator.validate(candidate_data(source_page=source_page))
