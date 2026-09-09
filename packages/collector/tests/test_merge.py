from collections.abc import Callable

import pytest

from requirementseeker_collector.contracts import RawComment
from requirementseeker_collector.merge import (
    CurrentRunConflict,
    PreviousRunConflict,
    conflict_fields,
    merge_comments,
    merge_current_run,
)


def comment(raw_comment_id: str, **updates: object) -> RawComment:
    values: dict[str, object] = {
        "raw_comment_id": raw_comment_id,
        "raw_author_id": "author-1",
        "raw_parent_comment_id": None,
        "text": "original text",
        "published_at": None,
        "collected_at": "2026-09-09T01:00:00Z",
        "like_count": 1,
        "reply_count": 0,
        "is_video_author": False,
        "source_stratum": "top",
        "source_page_or_rank": 1,
    }
    values.update(updates)
    return RawComment.model_validate(values)


def test_new_comments_are_added_and_sorted() -> None:
    old = comment("c2")
    new = comment("c1")

    result = merge_comments([old], [new])

    assert result.comments == [new, old]
    assert result.conflicts == []
    assert result.exact_duplicate_count == 0


def test_same_comment_is_updated_once() -> None:
    old = comment("c1", like_count=1)
    new = comment(
        "c1",
        raw_parent_comment_id="parent-2",
        published_at="2026-09-08T01:00:00Z",
        collected_at="2026-09-09T02:00:00Z",
        like_count=3,
        reply_count=2,
        is_video_author=True,
        source_stratum="recent",
        source_page_or_rank=4,
    )

    result = merge_comments([old], [new])

    assert result.comments == [new]
    assert result.comments[0] is new
    assert result.comments[0].like_count == 3


@pytest.mark.parametrize("field", ["raw_author_id", "text"])
def test_identity_conflict_keeps_previous(field: str) -> None:
    old = comment("c1")
    new = old.model_copy(update={field: "changed"})

    result = merge_comments([old], [new])

    assert result.comments == [old]
    assert result.comments[0] is old
    assert result.conflicts[0].conflict_fields == [field]


def test_identity_conflict_fields_have_fixed_order() -> None:
    old = comment("c1", raw_author_id="old-author", text="old-body")
    new = comment("c1", raw_author_id="new-author", text="new-body")

    result = merge_comments([old], [new])

    assert conflict_fields(old, new) == ["raw_author_id", "text"]
    assert result.conflicts[0].conflict_fields == ["raw_author_id", "text"]


def test_conflict_structure_does_not_include_identity_values() -> None:
    old = comment("c1", raw_author_id="old-secret-author", text="old-secret-body")
    new = comment("c1", raw_author_id="new-secret-author", text="new-secret-body")

    conflict = merge_comments([old], [new]).conflicts[0]
    rendered = repr(conflict)

    assert conflict.raw_comment_id == "c1"
    assert "old-secret-author" not in rendered
    assert "new-secret-author" not in rendered
    assert "old-secret-body" not in rendered
    assert "new-secret-body" not in rendered


def test_current_exact_duplicates_are_counted_and_collapsed() -> None:
    first = comment("c1")
    duplicate = first.model_copy()

    result = merge_current_run([first, duplicate, duplicate])

    assert list(result) == [first]
    assert result.comments[0] is first
    assert result.exact_duplicate_count == 2


def test_current_metadata_variants_keep_first_without_duplicate_count() -> None:
    first = comment("c1", like_count=1, source_stratum="top", source_page_or_rank=1)
    later = comment("c1", like_count=9, source_stratum="recent", source_page_or_rank=7)

    current = merge_current_run([first, later])
    merged = merge_comments([comment("c1", like_count=0)], [first, later])

    assert list(current) == [first]
    assert current.comments[0] is first
    assert current.exact_duplicate_count == 0
    assert merged.comments == [first]
    assert merged.comments[0] is first


def test_repeated_metadata_variant_counts_as_exact_duplicate() -> None:
    first = comment("c1", like_count=1)
    variant = comment("c1", like_count=9)

    result = merge_current_run([first, variant, variant.model_copy()])

    assert list(result) == [first]
    assert result.exact_duplicate_count == 1


def test_each_repeated_metadata_version_counts_as_exact_duplicate() -> None:
    first = comment("c1", like_count=1)
    variant = comment("c1", like_count=9)

    result = merge_current_run([first, variant, first.model_copy(), variant.model_copy()])

    assert list(result) == [first]
    assert result.exact_duplicate_count == 2


def test_conflict_inside_one_run_rejects_generation() -> None:
    with pytest.raises(CurrentRunConflict, match="c1"):
        merge_current_run([comment("c1"), comment("c1", text="changed")])


def test_merge_preflights_current_run_conflicts() -> None:
    current = [comment("c1"), comment("c1", raw_author_id="changed")]

    with pytest.raises(CurrentRunConflict):
        merge_comments([comment("previous")], current)


@pytest.mark.parametrize(
    "second",
    [
        lambda first: first.model_copy(),
        lambda first: first.model_copy(update={"text": "changed"}),
    ],
)
def test_previous_duplicate_ids_are_rejected(
    second: Callable[[RawComment], RawComment],
) -> None:
    first = comment("c1")

    with pytest.raises(PreviousRunConflict, match="c1"):
        merge_comments([first, second(first)], [])


def test_comments_and_conflicts_are_sorted_by_comment_id() -> None:
    previous = [comment("c3"), comment("c1"), comment("c2")]
    current = [
        comment("c3", text="conflict-3"),
        comment("c4"),
        comment("c1", raw_author_id="conflict-1"),
    ]

    result = merge_comments(previous, current)

    assert [item.raw_comment_id for item in result.comments] == ["c1", "c2", "c3", "c4"]
    assert [item.raw_comment_id for item in result.conflicts] == ["c1", "c3"]


def test_merge_does_not_mutate_inputs() -> None:
    previous = [comment("c2", like_count=1)]
    current = [comment("c2", like_count=5), comment("c1")]
    previous_snapshot = [item.model_dump() for item in previous]
    current_snapshot = [item.model_dump() for item in current]

    merge_comments(previous, current)

    assert [item.model_dump() for item in previous] == previous_snapshot
    assert [item.model_dump() for item in current] == current_snapshot


def test_current_run_exception_does_not_leak_identity_values() -> None:
    first = comment("c1", raw_author_id="old-secret-author", text="old-secret-body")
    second = comment("c1", raw_author_id="new-secret-author", text="new-secret-body")

    with pytest.raises(CurrentRunConflict) as raised:
        merge_current_run([first, second])

    message = str(raised.value)
    assert "current_run_identity_conflict" in message
    assert "c1" in message
    assert "secret" not in message


def test_previous_run_exception_does_not_leak_identity_values() -> None:
    first = comment("c1", raw_author_id="old-secret-author", text="old-secret-body")
    second = comment("c1", raw_author_id="new-secret-author", text="new-secret-body")

    with pytest.raises(PreviousRunConflict) as raised:
        merge_comments([first, second], [])

    message = str(raised.value)
    assert "previous_run_duplicate_id" in message
    assert "c1" in message
    assert "secret" not in message
