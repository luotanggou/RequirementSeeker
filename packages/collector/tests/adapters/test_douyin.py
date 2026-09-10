import json
from pathlib import Path
from typing import Any

import pytest

from requirementseeker_collector.adapters.base import ResponseShapeChanged
from requirementseeker_collector.adapters.douyin import DouyinAdapter

FIXTURES = Path(__file__).parents[1] / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    value: object = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_douyin_parses_video_response() -> None:
    parsed = DouyinAdapter().parse_video_response(load_fixture("douyin/video.json"))

    assert parsed.video.platform == "douyin"
    assert parsed.video.raw_video_id == "7390000000000000000"
    assert parsed.video.raw_author_id == "author"
    assert parsed.video.title == "Synthetic Douyin Video"
    assert parsed.video.total_comment_count == 2
    assert parsed.video.duration_seconds == 125


def test_douyin_parses_nullable_author_and_cursor() -> None:
    page = DouyinAdapter().parse_comment_response(
        load_fixture("douyin/comments.json"), "recent", 1, video_author_id="author"
    )

    assert page.comments[0].raw_author_id is None
    assert page.comments[0].is_video_author is None
    assert page.comments[1].raw_parent_comment_id == "21"
    assert page.comments[1].is_video_author is True
    assert page.has_more is True
    assert page.next_cursor == "2"


def test_douyin_reply_response_uses_requested_parent() -> None:
    payload = {
        "status_code": 0,
        "comments": [
            {
                "cid": "23",
                "user": {"sec_uid": "other"},
                "reply_id": "ignored",
                "text": "Reply page item",
            }
        ],
        "has_more": False,
    }

    page = DouyinAdapter().parse_comment_response(
        payload,
        "replies",
        2,
        video_author_id="author",
        parent_comment_id="21",
    )

    assert page.comments[0].raw_parent_comment_id == "21"


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=1", "video"),
        ("https://www.douyin.com/aweme/v1/web/comment/list/?aweme_id=1", "comments"),
        ("https://www.douyin.com/aweme/v1/web/comment/list/reply/?item_id=1", "replies"),
        ("https://www.douyin.com/aweme/v1/web/comment/other/", None),
        ("https://evil.example/aweme/v1/web/comment/list/", None),
    ],
)
def test_douyin_recognizes_only_supported_response_urls(url: str, kind: str | None) -> None:
    assert DouyinAdapter().response_kind(url) == kind


@pytest.mark.parametrize(
    "payload",
    [
        {"unexpected": []},
        {"status_code": 0, "comments": [{"user": {}, "text": "missing id"}]},
        {"status_code": 0, "comments": [{"cid": "21", "user": {}}]},
    ],
)
def test_douyin_unknown_or_incomplete_success_shape_closes(payload: dict[str, Any]) -> None:
    with pytest.raises(ResponseShapeChanged):
        DouyinAdapter().parse_comment_response(payload, "top", 1, video_author_id="author")
