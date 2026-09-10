import json
from pathlib import Path
from typing import Any

import pytest

from requirementseeker_collector.adapters.base import ResponseShapeChanged
from requirementseeker_collector.adapters.bilibili import BilibiliAdapter

FIXTURES = Path(__file__).parents[1] / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    value: object = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_bilibili_parses_video_response() -> None:
    parsed = BilibiliAdapter().parse_video_response(load_fixture("bilibili/video.json"))

    assert parsed.video.platform == "bilibili"
    assert parsed.video.raw_video_id == "BV1synthetic"
    assert parsed.video.raw_author_id == "42"
    assert parsed.video.title == "Synthetic Bilibili Video"
    assert parsed.video.total_comment_count == 2
    assert parsed.video.duration_seconds == 125


def test_bilibili_parses_top_level_and_reply() -> None:
    page = BilibiliAdapter().parse_comment_response(
        load_fixture("bilibili/comments.json"), "top", 1, video_author_id="42"
    )

    assert [(item.raw_comment_id, item.raw_parent_comment_id) for item in page.comments] == [
        ("11", None),
        ("12", "11"),
    ]
    assert page.comments[0].is_video_author is True
    assert page.comments[1].is_video_author is False
    assert page.has_more is False
    assert page.next_cursor == "2"


def test_bilibili_reply_response_uses_requested_parent() -> None:
    payload = {
        "code": 0,
        "data": {
            "replies": [
                {
                    "rpid": 13,
                    "member": {},
                    "content": {"message": "Reply page item"},
                }
            ],
            "cursor": {"is_end": True},
        },
    }

    page = BilibiliAdapter().parse_comment_response(
        payload,
        "replies",
        2,
        video_author_id="42",
        parent_comment_id="11",
    )

    assert page.comments[0].raw_parent_comment_id == "11"
    assert page.comments[0].raw_author_id is None
    assert page.comments[0].like_count is None


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("https://api.bilibili.com/x/web-interface/view?bvid=BV1", "video"),
        ("https://api.bilibili.com/x/v2/reply/wbi/main?oid=1", "comments"),
        ("https://api.bilibili.com/x/v2/reply/reply?root=11", "replies"),
        ("https://api.bilibili.com/x/v2/reply/other", None),
        ("https://evil.example/x/v2/reply/wbi/main", None),
    ],
)
def test_bilibili_recognizes_only_supported_response_urls(url: str, kind: str | None) -> None:
    assert BilibiliAdapter().response_kind(url) == kind


@pytest.mark.parametrize(
    "payload",
    [
        {"unexpected": []},
        {"code": 0, "data": {"replies": [{"content": {"message": "missing id"}}]}},
        {"code": 0, "data": {"replies": [{"rpid": 11, "content": {}}]}},
    ],
)
def test_bilibili_unknown_or_incomplete_success_shape_closes(payload: dict[str, Any]) -> None:
    with pytest.raises(ResponseShapeChanged):
        BilibiliAdapter().parse_comment_response(payload, "top", 1, video_author_id="42")
