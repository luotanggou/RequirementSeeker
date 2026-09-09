"""Validated artifact writing and recoverable directory commits."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ValidationError

from .contracts import CollectionRecord, RawComment, RawVideo


class ArtifactValidationError(ValueError):
    """Raised when an artifact cannot be safely validated."""


class ArtifactCommitError(RuntimeError):
    """Raised when an artifact directory cannot be safely committed."""


@dataclass(frozen=True)
class CommitPaths:
    """The three distinct directories participating in a commit."""

    staging: Path
    target: Path
    backup: Path

    def __post_init__(self) -> None:
        try:
            resolved = tuple(
                path.resolve(strict=False) for path in (self.staging, self.target, self.backup)
            )
        except (OSError, RuntimeError):
            raise ArtifactValidationError("commit_paths_unresolvable") from None
        for index, left in enumerate(resolved):
            for right in resolved[index + 1 :]:
                if left == right:
                    raise ArtifactValidationError("commit_paths_not_distinct")
                if left in right.parents or right in left.parents:
                    raise ArtifactValidationError("commit_paths_not_disjoint")


def write_generation(
    staging: Path,
    video: RawVideo,
    comments: Sequence[RawComment],
    collection: CollectionRecord,
) -> None:
    """Write a complete generation beneath the staging directory."""

    video_json = video.model_dump_json()
    comments_jsonl = "".join(f"{comment.model_dump_json()}\n" for comment in comments)
    collection_json = collection.model_dump_json()
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "video.json").write_text(video_json, encoding="utf-8")
    (staging / "comments.jsonl").write_text(comments_jsonl, encoding="utf-8")
    (staging / "collection.json").write_text(collection_json, encoding="utf-8")


def read_jsonl[ModelT: BaseModel](path: Path, model: type[ModelT]) -> list[ModelT]:
    """Strictly parse and validate every non-empty JSON Lines record."""

    try:
        source = path.open("r", encoding="utf-8")
    except FileNotFoundError:
        raise ArtifactValidationError("jsonl_missing") from None
    except OSError:
        raise ArtifactValidationError("jsonl_read_error") from None

    result: list[ModelT] = []
    try:
        with source:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    raise ArtifactValidationError(f"jsonl_blank_line:line_{line_number}")
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    raise ArtifactValidationError(f"jsonl_non_json:line_{line_number}") from None
                try:
                    result.append(model.model_validate(value))
                except ValidationError as error:
                    category = (
                        "extra_field"
                        if any(item["type"] == "extra_forbidden" for item in error.errors())
                        else "schema"
                    )
                    raise ArtifactValidationError(f"jsonl_{category}:line_{line_number}") from None
    except UnicodeError:
        raise ArtifactValidationError("jsonl_encoding_error") from None
    except OSError:
        raise ArtifactValidationError("jsonl_read_error") from None
    return result


def _read_json[ModelT: BaseModel](path: Path, model: type[ModelT], category: str) -> ModelT:
    try:
        value = path.read_bytes()
    except FileNotFoundError:
        raise ArtifactValidationError(f"{category}_missing") from None
    except OSError:
        raise ArtifactValidationError(f"{category}_read_error") from None
    try:
        return model.model_validate_json(value)
    except ValidationError as error:
        error_category = (
            "bad_json"
            if any(item["type"] == "json_invalid" for item in error.errors())
            else "schema"
        )
        raise ArtifactValidationError(f"{category}_{error_category}") from None


def validate_generation(
    directory: Path,
) -> tuple[RawVideo, list[RawComment], CollectionRecord]:
    """Load and validate one complete generation and its cross-file invariants."""

    video = _read_json(directory / "video.json", RawVideo, "video")
    comments = read_jsonl(directory / "comments.jsonl", RawComment)
    collection = _read_json(directory / "collection.json", CollectionRecord, "collection")

    if collection.collected_total != len(comments):
        raise ArtifactValidationError("collected_total_mismatch")
    comment_ids = [comment.raw_comment_id for comment in comments]
    if len(comment_ids) != len(set(comment_ids)):
        raise ArtifactValidationError("duplicate_comment_id")
    if any(comment.raw_parent_comment_id == comment.raw_comment_id for comment in comments):
        raise ArtifactValidationError("self_parent_comment")
    return video, comments, collection


def _move_directory(source: Path, destination: Path) -> None:
    source.replace(destination)


def commit_generation(paths: CommitPaths) -> None:
    """Validate staging and atomically replace target with recoverable rollback."""

    validate_generation(paths.staging)
    if paths.backup.exists():
        raise ArtifactCommitError("backup_exists")
    try:
        paths.target.parent.mkdir(parents=True, exist_ok=True)
        paths.backup.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise ArtifactCommitError("parent_creation_failed") from None

    had_target = paths.target.exists()
    if had_target:
        try:
            _move_directory(paths.target, paths.backup)
        except OSError:
            raise ArtifactCommitError("target_backup_failed") from None
    try:
        _move_directory(paths.staging, paths.target)
    except OSError:
        if had_target and paths.backup.exists() and not paths.target.exists():
            try:
                _move_directory(paths.backup, paths.target)
            except OSError:
                raise ArtifactCommitError("staging_move_failed_recovery_failed") from None
        raise ArtifactCommitError("staging_move_failed") from None


def recover_interrupted_commit(paths: CommitPaths) -> bool:
    """Restore a backup only when no target currently exists."""

    if not paths.backup.exists() or paths.target.exists():
        return False
    try:
        paths.target.parent.mkdir(parents=True, exist_ok=True)
        _move_directory(paths.backup, paths.target)
    except OSError:
        raise ArtifactCommitError("backup_recovery_failed") from None
    return True
