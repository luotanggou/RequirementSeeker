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
    parse_bilibili_reported_comment_count,
    parse_douyin_candidates,
    reject_comment_payload,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"
SOURCE = "https://search.bilibili.com/all?keyword=tool"
DOUYIN_SOURCE = "https://www.douyin.com/search/tool"
NOW = datetime(2026, 9, 13, 1, tzinfo=UTC)
MISSING = object()


@pytest.mark.parametrize("count", [0, 123])
def test_bilibili_reported_comment_count_parses_exact_video(count: int) -> None:
    assert (
        parse_bilibili_reported_comment_count(
            {
                "code": 0,
                "data": {
                    "bvid": "BV1xx411c7mD",
                    "stat": {"reply": count},
                },
            },
            "BV1xx411c7mD",
        )
        == count
    )


def test_bilibili_reported_comment_count_nonzero_business_code_is_unavailable() -> None:
    assert (
        parse_bilibili_reported_comment_count(
            {"code": -404, "data": object()},
            "BV1xx411c7mD",
        )
        is None
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"code": True, "data": {}},
        {"code": "0", "data": {}},
        {"code": 0, "data": None},
        {"code": 0, "data": {"bvid": 1, "stat": {"reply": 1}}},
        {"code": 0, "data": {"bvid": "BV1Q541167Qg", "stat": {"reply": 1}}},
        {"code": 0, "data": {"bvid": "BV1xx411c7mD", "stat": None}},
        {"code": 0, "data": {"bvid": "BV1xx411c7mD", "stat": {}}},
        {"code": 0, "data": {"bvid": "BV1xx411c7mD", "stat": {"reply": -1}}},
        {"code": 0, "data": {"bvid": "BV1xx411c7mD", "stat": {"reply": True}}},
        {"code": 0, "data": {"bvid": "BV1xx411c7mD", "stat": {"reply": "1"}}},
    ],
)
def test_bilibili_reported_comment_count_rejects_invalid_success_shape(
    payload: Mapping[str, object],
) -> None:
    with pytest.raises(CandidateShapeChanged, match="^candidate_shape_changed$"):
        parse_bilibili_reported_comment_count(payload, "BV1xx411c7mD")


@pytest.mark.parametrize("expected_bvid", ["BV1xx411c7m", "av123", "BV1xx411c7m!"])
def test_bilibili_reported_comment_count_rejects_invalid_expected_bvid(
    expected_bvid: str,
) -> None:
    with pytest.raises(CandidateShapeChanged, match="^candidate_shape_changed$"):
        parse_bilibili_reported_comment_count({}, expected_bvid)


def test_bilibili_reported_comment_count_rejects_comment_bomb_without_reading_it() -> None:
    with pytest.raises(CandidateContainsComments, match="^candidate_payload_contains_comments$"):
        parse_bilibili_reported_comment_count(
            UnreadableCommentPayload(),
            "BV1xx411c7mD",
        )


class UnreadableCommentPayload(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        raise AssertionError(f"comment body was read through key {key!r}")

    def __iter__(self) -> Iterator[str]:
        return iter(("comments",))

    def __len__(self) -> int:
        return 1


class MappingWithUnreadableValue(Mapping[str, object]):
    def __init__(self, values: dict[str, object], unreadable_key: str) -> None:
        self._values = values
        self._unreadable_key = unreadable_key

    def __getitem__(self, key: str) -> object:
        if key == self._unreadable_key:
            raise AssertionError(f"forbidden value was read through key {key!r}")
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


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


def _douyin_related_word_card() -> dict[str, object]:
    return {
        "type": 6,
        "doc_type": 108,
        "card_type": 6,
        "card_unique_name": "related_word",
    }


def _douyin_common_aladdin_card() -> dict[str, object]:
    return {
        "type": 77,
        "doc_type": 305,
        "card_type": 0,
        "card_unique_name": "toutiao_article",
        "common_aladdin": {},
    }


def _douyin_unusable_aweme() -> dict[str, object]:
    return {
        "aweme_info": {
            "aweme_id": "7390000000000000099",
            "desc": None,
            "statistics": None,
            "comment_list": object(),
        }
    }


def test_douyin_candidates_skip_known_related_word_card_and_preserve_source_positions() -> None:
    payload = load_fixture("douyin/candidates.json")
    payload["data"].insert(1, _douyin_related_word_card())

    items = parse_douyin_candidates(
        payload,
        "AI工具推荐",
        DOUYIN_SOURCE,
        NOW,
        direction="software_tools",
    )

    assert [(item.video_key, item.source_rank) for item in items] == [
        ("7390000000000000001", 1),
        ("7390000000000000002", 3),
    ]


def test_douyin_candidates_skip_exact_common_aladdin_and_unusable_aweme_positions() -> None:
    payload = load_fixture("douyin/candidates.json")
    unusable = _douyin_unusable_aweme()
    detail = unusable["aweme_info"]
    assert isinstance(detail, dict)
    unusable["aweme_info"] = MappingWithUnreadableValue(detail, "comment_list")
    payload["data"][1:1] = [_douyin_common_aladdin_card(), unusable]

    items = parse_douyin_candidates(
        payload,
        "AI工具推荐",
        DOUYIN_SOURCE,
        NOW,
        direction="software_tools",
    )

    assert [(item.video_key, item.source_rank) for item in items] == [
        ("7390000000000000001", 1),
        ("7390000000000000002", 4),
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("type", 76),
        ("doc_type", 304),
        ("card_type", 1),
        ("card_unique_name", "related_word"),
        ("common_aladdin", None),
    ],
)
def test_douyin_common_aladdin_near_miss_closes(field: str, value: object) -> None:
    payload = load_fixture("douyin/candidates.json")
    card = _douyin_common_aladdin_card()
    card[field] = value
    payload["data"].insert(1, card)

    with pytest.raises(CandidateShapeChanged, match="^candidate_shape_changed$"):
        parse_douyin_candidates(
            payload,
            "AI工具推荐",
            DOUYIN_SOURCE,
            NOW,
            direction="software_tools",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("desc", "partially usable"),
        ("statistics", {}),
        ("aweme_id", ""),
        ("aweme_id", 7390000000000000099),
    ],
)
def test_douyin_unusable_aweme_near_miss_closes(field: str, value: object) -> None:
    payload = load_fixture("douyin/candidates.json")
    item = _douyin_unusable_aweme()
    detail = item["aweme_info"]
    assert isinstance(detail, dict)
    detail[field] = value
    payload["data"].insert(1, item)

    with pytest.raises(CandidateShapeChanged, match="^candidate_shape_changed$"):
        parse_douyin_candidates(
            payload,
            "AI工具推荐",
            DOUYIN_SOURCE,
            NOW,
            direction="software_tools",
        )


def test_douyin_common_aladdin_comment_bomb_is_rejected_without_reading_value() -> None:
    payload = load_fixture("douyin/candidates.json")
    card = _douyin_common_aladdin_card()
    card["common_aladdin"] = MappingWithUnreadableValue({"comments": object()}, "comments")
    payload["data"].insert(1, card)

    with pytest.raises(CandidateContainsComments, match="^candidate_payload_contains_comments$"):
        parse_douyin_candidates(
            payload,
            "AI工具推荐",
            DOUYIN_SOURCE,
            NOW,
            direction="software_tools",
        )


def test_douyin_candidate_allows_direct_aweme_preview_comment_list_without_reading_it() -> None:
    payload = load_fixture("douyin/candidates.json")
    detail = payload["data"][0]["aweme_info"]
    detail["comment_list"] = object()
    payload["data"][0]["aweme_info"] = MappingWithUnreadableValue(
        detail,
        "comment_list",
    )
    payload["data"].insert(1, _douyin_related_word_card())

    items = parse_douyin_candidates(
        payload,
        "AI工具推荐",
        DOUYIN_SOURCE,
        NOW,
        direction="software_tools",
    )

    assert [(item.video_key, item.source_rank) for item in items] == [
        ("7390000000000000001", 1),
        ("7390000000000000002", 3),
    ]
    assert all("comment_list" not in item.model_dump() for item in items)


def test_public_comment_rejection_stays_generic_for_direct_aweme_preview_position() -> None:
    payload = MappingWithUnreadableValue({"comment_list": object()}, "comment_list")

    with pytest.raises(CandidateContainsComments, match="^candidate_payload_contains_comments$"):
        reject_comment_payload({"data": [{"aweme_info": payload}]})


def test_douyin_related_word_card_with_comment_key_is_rejected_without_reading_value() -> None:
    payload = load_fixture("douyin/candidates.json")
    card = _douyin_related_word_card()
    card["comments"] = UnreadableCommentPayload()
    payload["data"].insert(1, card)

    with pytest.raises(CandidateContainsComments, match="^candidate_payload_contains_comments$"):
        parse_douyin_candidates(
            payload,
            "AI工具推荐",
            DOUYIN_SOURCE,
            NOW,
            direction="software_tools",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("type", 7),
        ("type", "6"),
        ("doc_type", 109),
        ("doc_type", "108"),
        ("card_type", 7),
        ("card_type", "6"),
        ("card_unique_name", "unknown"),
        ("card_unique_name", 6),
    ],
)
def test_douyin_candidate_unknown_non_video_card_closes(field: str, value: object) -> None:
    payload = load_fixture("douyin/candidates.json")
    card = _douyin_related_word_card()
    card[field] = value
    payload["data"].insert(1, card)

    with pytest.raises(CandidateShapeChanged, match="^candidate_shape_changed$"):
        parse_douyin_candidates(
            payload,
            "AI工具推荐",
            DOUYIN_SOURCE,
            NOW,
            direction="software_tools",
        )


@pytest.mark.parametrize("field", ["type", "doc_type", "card_type", "card_unique_name"])
def test_douyin_candidate_incomplete_related_word_marker_closes(field: str) -> None:
    payload = load_fixture("douyin/candidates.json")
    card = _douyin_related_word_card()
    del card[field]
    payload["data"].insert(1, card)

    with pytest.raises(CandidateShapeChanged, match="^candidate_shape_changed$"):
        parse_douyin_candidates(
            payload,
            "AI工具推荐",
            DOUYIN_SOURCE,
            NOW,
            direction="software_tools",
        )


@pytest.mark.parametrize(
    "card",
    [
        {"unknown": "mapping"},
        {**_douyin_related_word_card(), "aweme_info": None},
    ],
)
def test_douyin_candidate_other_missing_aweme_info_shapes_close(
    card: dict[str, object],
) -> None:
    payload = load_fixture("douyin/candidates.json")
    payload["data"].insert(1, card)

    with pytest.raises(CandidateShapeChanged, match="^candidate_shape_changed$"):
        parse_douyin_candidates(
            payload,
            "AI工具推荐",
            DOUYIN_SOURCE,
            NOW,
            direction="software_tools",
        )


@pytest.mark.parametrize(
    "cards",
    [
        [],
        [_douyin_related_word_card()],
        [_douyin_common_aladdin_card(), _douyin_unusable_aweme()],
    ],
)
def test_douyin_empty_or_all_recognized_non_video_page_keeps_empty_parser_result(
    cards: list[dict[str, object]],
) -> None:
    items = parse_douyin_candidates(
        {"status_code": 0, "data": cards},
        "AI工具推荐",
        DOUYIN_SOURCE,
        NOW,
        direction="software_tools",
    )

    assert items == []


@pytest.mark.parametrize(
    "forbidden_key",
    ["comments", "replies", "comment_list", "reply_list", "COMMENTS"],
)
def test_bilibili_candidate_payload_rejects_comment_keys_at_any_depth(
    forbidden_key: str,
) -> None:
    payload = load_fixture("bilibili/candidates.json")
    payload["outer"] = [{"nested": {forbidden_key: [{"text": "must not be read"}]}}]

    with pytest.raises(CandidateContainsComments, match="^candidate_payload_contains_comments$"):
        parse_bilibili_candidates(payload, "query", SOURCE, NOW, direction="software_tools")


def _place_douyin_forbidden_mapping(
    payload: Mapping[str, Any],
    location: str,
    forbidden_key: str,
) -> Mapping[str, Any]:
    forbidden = MappingWithUnreadableValue({forbidden_key: object()}, forbidden_key)
    if location == "root":
        values = dict(payload)
        values[forbidden_key] = object()
        return MappingWithUnreadableValue(values, forbidden_key)

    data = payload["data"]
    if location == "data_item":
        data.insert(0, forbidden)
    elif location == "related_word":
        values = _douyin_related_word_card()
        values[forbidden_key] = object()
        data.insert(0, MappingWithUnreadableValue(values, forbidden_key))
    elif location == "aweme_info":
        detail = data[0]["aweme_info"]
        detail[forbidden_key] = object()
        data[0]["aweme_info"] = MappingWithUnreadableValue(detail, forbidden_key)
    elif location == "aweme_info_nested":
        data[0]["aweme_info"]["author"] = forbidden
    elif location == "statistics":
        statistics = data[0]["aweme_info"]["statistics"]
        statistics[forbidden_key] = object()
        data[0]["aweme_info"]["statistics"] = MappingWithUnreadableValue(
            statistics,
            forbidden_key,
        )
    elif location == "other_branch":
        payload["other"] = forbidden
    else:
        raise AssertionError(f"unknown test location: {location}")
    return payload


@pytest.mark.parametrize(
    ("location", "forbidden_key"),
    [
        (location, forbidden_key)
        for location in (
            "root",
            "data_item",
            "related_word",
            "aweme_info_nested",
            "statistics",
            "other_branch",
        )
        for forbidden_key in ("comments", "replies", "comment_list", "reply_list")
    ]
    + [
        ("aweme_info", forbidden_key)
        for forbidden_key in ("comments", "replies", "reply_list", "COMMENT_LIST")
    ],
)
def test_douyin_candidate_rejects_comment_keys_outside_exact_preview_position_without_reading_value(
    location: str,
    forbidden_key: str,
) -> None:
    payload = _place_douyin_forbidden_mapping(
        load_fixture("douyin/candidates.json"),
        location,
        forbidden_key,
    )

    with pytest.raises(CandidateContainsComments, match="^candidate_payload_contains_comments$"):
        parse_douyin_candidates(
            payload,
            "query",
            DOUYIN_SOURCE,
            NOW,
            direction="software_tools",
        )


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
