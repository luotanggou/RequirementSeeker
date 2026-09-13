import json
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from requirementseeker_collector.candidates.adapters import (
    CandidateContainsComments,
    CandidateShapeChanged,
    parse_bilibili_candidates,
    parse_douyin_candidates,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"
SOURCE = "https://search.bilibili.com/all?keyword=tool"
DOUYIN_SOURCE = "https://www.douyin.com/search/tool"
NOW = datetime(2026, 9, 13, 1, tzinfo=UTC)
MISSING = object()


class UnreadableCommentPayload(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        raise AssertionError(f"comment body was read through key {key!r}")

    def __iter__(self) -> Iterator[str]:
        return iter(("comments",))

    def __len__(self) -> int:
        return 1


def load_fixture(name: str) -> dict[str, Any]:
    value: object = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_bilibili_candidates_map_metadata_in_page_order() -> None:
    items = parse_bilibili_candidates(
        load_fixture("bilibili/candidates.json"),
        "效率工具",
        SOURCE,
        NOW,
        direction="software_tools",
    )

    assert [item.model_dump(mode="json") for item in items] == [
        {
            "platform": "bilibili",
            "video_key": "BVfake1",
            "url": "https://www.bilibili.com/video/BVfake1",
            "title": "Synthetic Bilibili Tool One",
            "reported_comment_count": 125,
            "direction": "software_tools",
            "query": "效率工具",
            "source_page": SOURCE,
            "source_rank": 1,
            "discovered_at": "2026-09-13T01:00:00Z",
        },
        {
            "platform": "bilibili",
            "video_key": "BVfake2",
            "url": "https://www.bilibili.com/video/BVfake2",
            "title": "Synthetic Bilibili Tool Two",
            "reported_comment_count": 2400,
            "direction": "software_tools",
            "query": "效率工具",
            "source_page": SOURCE,
            "source_rank": 2,
            "discovered_at": "2026-09-13T01:00:00Z",
        },
    ]


def test_douyin_candidates_map_metadata_in_page_order_without_comment_text() -> None:
    items = parse_douyin_candidates(
        load_fixture("douyin/candidates.json"),
        None,
        DOUYIN_SOURCE,
        NOW,
        direction="tutorial_workflow",
    )

    assert [(item.video_key, item.source_rank) for item in items] == [
        ("7390000000000000001", 1),
        ("7390000000000000002", 2),
    ]
    assert [item.url.encoded_string() for item in items] == [
        "https://www.douyin.com/video/7390000000000000001",
        "https://www.douyin.com/video/7390000000000000002",
    ]
    assert [item.reported_comment_count for item in items] == [32, 420]
    assert all(item.direction == "tutorial_workflow" for item in items)
    assert all(item.query is None for item in items)
    assert all(
        "comments" not in item and "text" not in item
        for item in (candidate.model_dump() for candidate in items)
    )


@pytest.mark.parametrize("parser", [parse_bilibili_candidates, parse_douyin_candidates])
@pytest.mark.parametrize(
    "forbidden_key",
    ["comments", "replies", "comment_list", "reply_list", "COMMENTS"],
)
def test_candidate_payload_rejects_comment_keys_at_any_depth(
    parser: Any, forbidden_key: str
) -> None:
    payload = load_fixture(
        "bilibili/candidates.json"
        if parser is parse_bilibili_candidates
        else "douyin/candidates.json"
    )
    payload["outer"] = [{"nested": {forbidden_key: [{"text": "must not be read"}]}}]

    with pytest.raises(CandidateContainsComments, match="^candidate_payload_contains_comments$"):
        parser(payload, "query", SOURCE, NOW, direction="software_tools")


@pytest.mark.parametrize("parser", [parse_bilibili_candidates, parse_douyin_candidates])
def test_candidate_payload_rejects_comment_key_without_reading_its_value(parser: Any) -> None:
    with pytest.raises(CandidateContainsComments, match="^candidate_payload_contains_comments$"):
        parser(
            UnreadableCommentPayload(),
            "query",
            SOURCE,
            NOW,
            direction="software_tools",
        )


@pytest.mark.parametrize(
    ("parser", "fixture"),
    [
        (parse_bilibili_candidates, "bilibili/candidates.json"),
        (parse_douyin_candidates, "douyin/candidates.json"),
    ],
)
@pytest.mark.parametrize(
    "count_value",
    [MISSING, -1, True, "12", []],
    ids=["missing", "negative", "boolean", "string", "list"],
)
def test_candidates_keep_items_with_missing_or_unusable_comment_count(
    parser: Any, fixture: str, count_value: object
) -> None:
    payload = load_fixture(fixture)
    if parser is parse_bilibili_candidates:
        item = payload["data"]["result"][0]
        count_key = "review"
        source_page = SOURCE
    else:
        item = payload["data"][0]["aweme_info"]["statistics"]
        count_key = "comment_count"
        source_page = DOUYIN_SOURCE
    if count_value is MISSING:
        del item[count_key]
    else:
        item[count_key] = count_value

    candidates = parser(
        payload,
        "query",
        source_page,
        NOW,
        direction="software_tools",
    )

    assert len(candidates) == 2
    assert candidates[0].reported_comment_count is None
    assert [candidate.source_rank for candidate in candidates] == [1, 2]


@pytest.mark.parametrize(
    ("parser", "payload"),
    [
        (parse_bilibili_candidates, {"code": 0, "data": {"unknown": []}}),
        (parse_douyin_candidates, {"status_code": 0, "unknown": []}),
    ],
)
def test_unknown_successful_candidate_shape_closes(parser: Any, payload: dict[str, Any]) -> None:
    with pytest.raises(CandidateShapeChanged, match="^candidate_shape_changed$"):
        parser(payload, "query", SOURCE, NOW, direction="software_tools")


@pytest.mark.parametrize(
    ("parser", "fixture", "item_path", "field"),
    [
        (parse_bilibili_candidates, "bilibili/candidates.json", ("data", "result", 0), "bvid"),
        (parse_bilibili_candidates, "bilibili/candidates.json", ("data", "result", 0), "title"),
        (parse_douyin_candidates, "douyin/candidates.json", ("data", 0, "aweme_info"), "aweme_id"),
        (parse_douyin_candidates, "douyin/candidates.json", ("data", 0, "aweme_info"), "desc"),
    ],
)
def test_candidate_missing_key_or_title_closes(
    parser: Any,
    fixture: str,
    item_path: tuple[str | int, ...],
    field: str,
) -> None:
    payload: Any = load_fixture(fixture)
    item = payload
    for part in item_path:
        item = item[part]
    del item[field]

    with pytest.raises(CandidateShapeChanged, match="^candidate_shape_changed$"):
        parser(payload, "query", SOURCE, NOW, direction="software_tools")
