from collections.abc import Callable, Sequence

import pytest

from requirementseeker_collector.contracts import RawComment, Stratum
from requirementseeker_collector.planning import (
    TargetDecision,
    allocate_quotas,
    collection_target,
    select_comments,
)


def comment(
    raw_comment_id: str, source_stratum: Stratum, rank: int, *, text: str | None = None
) -> RawComment:
    return RawComment.model_validate(
        {
            "raw_comment_id": raw_comment_id,
            "raw_author_id": None,
            "raw_parent_comment_id": None,
            "text": text if text is not None else f"comment {raw_comment_id}",
            "published_at": None,
            "collected_at": "2026-09-09T01:00:00Z",
            "like_count": None,
            "reply_count": None,
            "is_video_author": None,
            "source_stratum": source_stratum,
            "source_page_or_rank": rank,
        }
    )


def identities(comments: Sequence[RawComment]) -> list[tuple[str, Stratum, int]]:
    return [
        (item.raw_comment_id, item.source_stratum, item.source_page_or_rank) for item in comments
    ]


@pytest.mark.parametrize(
    ("total", "expected"),
    [
        (0, 0),
        (200, 200),
        (201, 200),
        (804, 201),
        (2000, 500),
        (2001, 500),
        (3600, 600),
        (10000, 1000),
    ],
)
def test_collection_target_boundaries(total: int, expected: int) -> None:
    assert collection_target(total).target == expected


def test_unknown_total_uses_audited_pilot_cap() -> None:
    assert collection_target(None) == TargetDecision(200, False, "reported_total_unavailable")


def test_negative_total_is_rejected_clearly() -> None:
    with pytest.raises(ValueError, match="total_must_be_non_negative"):
        collection_target(-1)


@pytest.mark.parametrize("operation", [allocate_quotas, lambda target: select_comments([], target)])
def test_negative_target_is_rejected_clearly(operation: Callable[[int], object]) -> None:
    with pytest.raises(ValueError, match="target_must_be_non_negative"):
        operation(-1)


def test_quota_rounding_is_deterministic() -> None:
    assert allocate_quotas(7) == {"top": 3, "recent": 2, "replies": 1, "long_tail": 1}


def test_quota_rounding_ties_use_stratum_name() -> None:
    assert allocate_quotas(3) == {"top": 1, "recent": 1, "replies": 0, "long_tail": 1}


@pytest.mark.parametrize("target", [0, 1, 2, 7, 200, 1000])
def test_quotas_sum_to_target(target: int) -> None:
    assert sum(allocate_quotas(target).values()) == target


def test_comments_are_sorted_within_each_stratum() -> None:
    candidates = [
        comment("top-c", "top", 2),
        comment("top-b", "top", 1),
        comment("top-a", "top", 1),
    ]

    assert identities(select_comments(candidates, 3)) == [
        ("top-a", "top", 1),
        ("top-b", "top", 1),
        ("top-c", "top", 2),
    ]


def test_duplicate_id_keeps_the_first_accepted_stratum_and_object() -> None:
    top_duplicate = comment("duplicate", "top", 1)
    recent_duplicate = comment("duplicate", "recent", 1)
    recent_unique = comment("recent", "recent", 2)
    candidates = [
        recent_duplicate,
        comment("long-tail", "long_tail", 1),
        recent_unique,
        comment("reply", "replies", 1),
        top_duplicate,
    ]

    selected = select_comments(candidates, 4)

    assert identities(selected) == [
        ("duplicate", "top", 1),
        ("recent", "recent", 2),
        ("reply", "replies", 1),
        ("long-tail", "long_tail", 1),
    ]
    assert selected[0] is top_duplicate


def test_shortfalls_are_filled_in_the_configured_order() -> None:
    candidates = [
        *(comment(f"top-{rank}", "top", rank) for rank in range(1, 4)),
        *(comment(f"recent-{rank}", "recent", rank) for rank in range(1, 4)),
        *(comment(f"long-{rank}", "long_tail", rank) for rank in range(1, 4)),
    ]

    assert identities(select_comments(candidates, 9)) == [
        ("top-1", "top", 1),
        ("top-2", "top", 2),
        ("top-3", "top", 3),
        ("recent-1", "recent", 1),
        ("recent-2", "recent", 2),
        ("long-1", "long_tail", 1),
        ("long-2", "long_tail", 2),
        ("long-3", "long_tail", 3),
        ("recent-3", "recent", 3),
    ]


def test_candidate_shortage_returns_every_unique_comment() -> None:
    top_duplicate = comment("duplicate", "top", 1)
    candidates = [
        comment("recent", "recent", 2),
        comment("duplicate", "recent", 1),
        comment("reply", "replies", 1),
        top_duplicate,
    ]

    selected = select_comments(candidates, 10)

    assert identities(selected) == [
        ("duplicate", "top", 1),
        ("recent", "recent", 2),
        ("reply", "replies", 1),
    ]
    assert selected[0] is top_duplicate


def test_input_order_does_not_change_selection() -> None:
    candidates = [
        comment("top-2", "top", 2),
        comment("recent-2", "recent", 2),
        comment("long-1", "long_tail", 1),
        comment("reply-1", "replies", 1),
        comment("top-1", "top", 1),
        comment("recent-1", "recent", 1),
    ]

    forward = identities(select_comments(candidates, 5))
    backward = identities(select_comments(list(reversed(candidates)), 5))

    assert forward == backward


def test_equal_primary_keys_use_a_canonical_payload_tie_break() -> None:
    first = comment("duplicate", "top", 1, text="first payload")
    second = comment("duplicate", "top", 1, text="second payload")

    forward = select_comments([first, second], 1)
    backward = select_comments([second, first], 1)

    assert [item.model_dump_json() for item in forward] == [
        item.model_dump_json() for item in backward
    ]
    assert any(forward[0] is candidate for candidate in (first, second))
    assert any(backward[0] is candidate for candidate in (first, second))
