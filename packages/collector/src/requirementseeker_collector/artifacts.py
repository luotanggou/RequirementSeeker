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


_ARTIFACT_NAMES = frozenset({"video.json", "comments.jsonl", "collection.json"})


def _resolve_paths(paths: tuple[Path, ...]) -> tuple[Path, ...] | None:
    try:
        return tuple(path.resolve(strict=False) for path in paths)
    except (OSError, RuntimeError):
        return None


@dataclass(frozen=True)
class CommitPaths:
    """The three disjoint directories participating in a commit."""

    staging: Path
    target: Path
    backup: Path

    def __post_init__(self) -> None:
        resolved = _resolve_paths((self.staging, self.target, self.backup))
        if resolved is None:
            raise ArtifactValidationError("commit_paths_unresolvable")
        for index, left in enumerate(resolved):
            for right in resolved[index + 1 :]:
                if left == right:
                    raise ArtifactValidationError("commit_paths_not_distinct")
                if left in right.parents or right in left.parents:
                    raise ArtifactValidationError("commit_paths_not_disjoint")


def _make_directory(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return True


def _write_text(path: Path, content: str) -> bool:
    try:
        path.write_text(content, encoding="utf-8")
    except OSError:
        return False
    return True


def _discard_partial_generation(directory: Path) -> None:
    for name in _ARTIFACT_NAMES:
        try:
            (directory / name).unlink(missing_ok=True)
        except OSError:
            pass
    try:
        directory.rmdir()
    except OSError:
        pass


def _serialize_generation(
    video: RawVideo,
    comments: Sequence[RawComment],
    collection: CollectionRecord,
) -> tuple[tuple[str, str], ...] | None:
    try:
        return (
            ("video.json", video.model_dump_json()),
            ("comments.jsonl", "".join(f"{item.model_dump_json()}\n" for item in comments)),
            ("collection.json", collection.model_dump_json()),
        )
    except Exception:
        return None


def write_generation(
    staging: Path,
    video: RawVideo,
    comments: Sequence[RawComment],
    collection: CollectionRecord,
) -> None:
    """Build and validate a generation before publishing it as staging."""

    if staging.exists() or staging.is_symlink():
        raise ArtifactCommitError("staging_exists")
    temporary = staging.with_name(f".{staging.name}.writing")
    if temporary.exists() or temporary.is_symlink():
        raise ArtifactCommitError("generation_temporary_exists")
    payloads = _serialize_generation(video, comments, collection)
    if payloads is None:
        raise ArtifactValidationError("generation_serialization_failed")
    if not _make_directory(staging.parent) or not _make_directory(temporary):
        raise ArtifactCommitError("generation_directory_creation_failed")

    if not all(_write_text(temporary / name, content) for name, content in payloads):
        _discard_partial_generation(temporary)
        raise ArtifactCommitError("generation_write_failed")

    validation_error: str | None = None
    try:
        validate_generation(temporary)
    except ArtifactValidationError as error:
        validation_error = str(error)
    if validation_error is not None:
        _discard_partial_generation(temporary)
        raise ArtifactValidationError(validation_error)
    if not _try_move_directory(temporary, staging):
        _discard_partial_generation(temporary)
        raise ArtifactCommitError("generation_publish_failed")


def _read_bytes(path: Path) -> tuple[str | None, bytes]:
    try:
        return None, path.read_bytes()
    except FileNotFoundError:
        return "missing", b""
    except OSError:
        return "read_error", b""


def _decode_utf8(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeError:
        return None


def _parse_json(line: str) -> tuple[bool, object]:
    try:
        return True, json.loads(line)
    except (json.JSONDecodeError, RecursionError):
        return False, None


def _validate_model[ModelT: BaseModel](
    value: object, model: type[ModelT]
) -> tuple[ModelT | None, str | None]:
    try:
        return model.model_validate(value), None
    except ValidationError as error:
        category = (
            "extra_field"
            if any(item["type"] == "extra_forbidden" for item in error.errors())
            else "schema"
        )
        return None, category


def read_jsonl[ModelT: BaseModel](path: Path, model: type[ModelT]) -> list[ModelT]:
    """Strictly parse and validate every JSON Lines record."""

    read_error, data = _read_bytes(path)
    if read_error is not None:
        raise ArtifactValidationError(f"jsonl_{read_error}")
    text = _decode_utf8(data)
    if text is None:
        raise ArtifactValidationError("jsonl_encoding_error")

    lines = text.split("\n")
    if lines and not lines[-1]:
        lines.pop()
    result: list[ModelT] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise ArtifactValidationError(f"jsonl_blank_line:line_{line_number}")
        parsed, value = _parse_json(line)
        if not parsed:
            raise ArtifactValidationError(f"jsonl_non_json:line_{line_number}")
        item, validation_error = _validate_model(value, model)
        if validation_error is not None:
            raise ArtifactValidationError(f"jsonl_{validation_error}:line_{line_number}")
        assert item is not None
        result.append(item)
    return result


def _validate_model_json[ModelT: BaseModel](
    data: bytes, model: type[ModelT]
) -> tuple[ModelT | None, str | None]:
    try:
        return model.model_validate_json(data), None
    except ValidationError as error:
        category = (
            "bad_json"
            if any(item["type"] == "json_invalid" for item in error.errors())
            else "schema"
        )
        return None, category


def _read_json[ModelT: BaseModel](path: Path, model: type[ModelT], category: str) -> ModelT:
    read_error, data = _read_bytes(path)
    if read_error is not None:
        raise ArtifactValidationError(f"{category}_{read_error}")
    item, validation_error = _validate_model_json(data, model)
    if validation_error is not None:
        raise ArtifactValidationError(f"{category}_{validation_error}")
    assert item is not None
    return item


def _has_exact_artifact_entries(directory: Path) -> bool | None:
    try:
        entries = list(directory.iterdir())
        names = {entry.name for entry in entries}
        regular_files = all(entry.is_file() and not entry.is_symlink() for entry in entries)
    except OSError:
        return None
    return names == _ARTIFACT_NAMES and regular_files


def validate_generation(
    directory: Path,
) -> tuple[RawVideo, list[RawComment], CollectionRecord]:
    """Load and validate exactly one generation and its cross-file invariants."""

    exact_entries = _has_exact_artifact_entries(directory)
    if exact_entries is None:
        raise ArtifactValidationError("generation_directory_unreadable")
    if not exact_entries:
        raise ArtifactValidationError("unexpected_directory_entries")

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


def _try_move_directory(source: Path, destination: Path) -> bool:
    try:
        _move_directory(source, destination)
    except OSError:
        return False
    return True


def commit_generation(paths: CommitPaths) -> None:
    """Validate staging and atomically replace target with recoverable rollback."""

    validate_generation(paths.staging)
    if paths.backup.exists():
        raise ArtifactCommitError("backup_exists")
    if not _make_directory(paths.target.parent) or not _make_directory(paths.backup.parent):
        raise ArtifactCommitError("parent_creation_failed")

    had_target = paths.target.exists()
    if had_target and not _try_move_directory(paths.target, paths.backup):
        raise ArtifactCommitError("target_backup_failed")
    if _try_move_directory(paths.staging, paths.target):
        return
    if had_target and paths.backup.exists() and not paths.target.exists():
        if not _try_move_directory(paths.backup, paths.target):
            raise ArtifactCommitError("staging_move_failed_recovery_failed")
    raise ArtifactCommitError("staging_move_failed")


def recover_interrupted_commit(paths: CommitPaths) -> bool:
    """Restore a backup only when no target currently exists."""

    if not paths.backup.exists() or paths.target.exists():
        return False
    if not _make_directory(paths.target.parent):
        raise ArtifactCommitError("backup_recovery_failed")
    if not _try_move_directory(paths.backup, paths.target):
        raise ArtifactCommitError("backup_recovery_failed")
    return True
