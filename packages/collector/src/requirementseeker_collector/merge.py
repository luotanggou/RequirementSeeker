"""Idempotent merging for raw comments."""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Literal

from .contracts import RawComment

type ConflictField = Literal["raw_author_id", "text"]


@dataclass(frozen=True)
class MergeConflict:
    raw_comment_id: str
    conflict_fields: list[ConflictField]


@dataclass(frozen=True)
class CurrentRunResult:
    comments: list[RawComment]
    exact_duplicate_count: int

    def __iter__(self) -> Iterator[RawComment]:
        return iter(self.comments)


@dataclass(frozen=True)
class MergeResult:
    comments: list[RawComment]
    conflicts: list[MergeConflict]
    exact_duplicate_count: int


class CurrentRunConflict(ValueError):
    """Raised when one collection run contains conflicting comment identities."""


class PreviousRunConflict(ValueError):
    """Raised when the previous collection contains a duplicate comment ID."""


def conflict_fields(old: RawComment, new: RawComment) -> list[ConflictField]:
    fields: list[ConflictField] = []
    if old.raw_author_id != new.raw_author_id:
        fields.append("raw_author_id")
    if old.text != new.text:
        fields.append("text")
    return fields


def merge_current_run(current: Sequence[RawComment]) -> CurrentRunResult:
    unique: dict[str, RawComment] = {}
    exact_duplicate_count = 0

    for item in current:
        first = unique.get(item.raw_comment_id)
        if first is None:
            unique[item.raw_comment_id] = item
        elif first == item:
            exact_duplicate_count += 1
        elif conflict_fields(first, item):
            raise CurrentRunConflict(f"current_run_identity_conflict: {item.raw_comment_id}")

    return CurrentRunResult(list(unique.values()), exact_duplicate_count)


def merge_comments(previous: Sequence[RawComment], current: Sequence[RawComment]) -> MergeResult:
    current_result = merge_current_run(current)
    merged: dict[str, RawComment] = {}

    for item in previous:
        if item.raw_comment_id in merged:
            raise PreviousRunConflict(f"previous_run_duplicate_id: {item.raw_comment_id}")
        merged[item.raw_comment_id] = item

    conflicts: list[MergeConflict] = []
    for item in current_result:
        old = merged.get(item.raw_comment_id)
        fields = conflict_fields(old, item) if old is not None else []
        if fields:
            conflicts.append(MergeConflict(item.raw_comment_id, fields))
        else:
            merged[item.raw_comment_id] = item

    comments = sorted(merged.values(), key=lambda item: item.raw_comment_id)
    conflicts.sort(key=lambda item: item.raw_comment_id)
    return MergeResult(comments, conflicts, current_result.exact_duplicate_count)
