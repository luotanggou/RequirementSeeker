import json
from pathlib import Path
from typing import Any

import pytest

import requirementseeker_collector.artifacts as artifacts
from requirementseeker_collector.artifacts import (
    ArtifactCommitError,
    ArtifactValidationError,
    CommitPaths,
    commit_generation,
    read_jsonl,
    recover_interrupted_commit,
    validate_generation,
    write_generation,
)
from requirementseeker_collector.contracts import CollectionRecord, RawComment, RawVideo


def video_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "platform": "bilibili",
        "raw_video_id": "BV1test",
        "raw_author_id": "author-1",
        "title": "测试视频",
        "description": "用于制品测试",
        "published_at": "2026-09-08T02:00:00Z",
        "duration_seconds": 120,
        "total_comment_count": 2,
        "view_count": 1000,
        "like_count": 100,
        "favorite_count": 50,
        "share_count": 10,
        "author_follower_count": 5000,
        "captured_at": "2026-09-09T01:00:00Z",
    }
    data.update(overrides)
    return data


def comment_data(comment_id: str = "comment-1", **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "raw_comment_id": comment_id,
        "raw_author_id": "author-2",
        "raw_parent_comment_id": None,
        "text": f"评论 {comment_id}",
        "published_at": "2026-09-08T03:00:00Z",
        "collected_at": "2026-09-09T01:00:00Z",
        "like_count": 3,
        "reply_count": 0,
        "is_video_author": False,
        "source_stratum": "top",
        "source_page_or_rank": 1,
    }
    data.update(overrides)
    return data


def collection_data(collected_total: int = 2, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "reported_total": 2,
        "collected_total": collected_total,
        "pages_requested": 1,
        "pages_succeeded": 1,
        "sort_modes": ["top"],
        "collection_started_at": "2026-09-09T01:00:00Z",
        "collection_finished_at": "2026-09-09T02:00:00Z",
        "collection_errors": [],
    }
    data.update(overrides)
    return data


def valid_models(title: str = "测试视频") -> tuple[RawVideo, list[RawComment], CollectionRecord]:
    return (
        RawVideo.model_validate(video_data(title=title)),
        [
            RawComment.model_validate(comment_data("comment-1")),
            RawComment.model_validate(comment_data("comment-2")),
        ],
        CollectionRecord.model_validate(collection_data()),
    )


def write_valid(directory: Path, title: str = "测试视频") -> None:
    video, comments, collection = valid_models(title)
    write_generation(directory, video, comments, collection)


def test_commit_paths_must_be_distinct(tmp_path: Path) -> None:
    with pytest.raises(ArtifactValidationError, match="commit_paths_not_distinct"):
        CommitPaths(tmp_path / "same", tmp_path / "same", tmp_path / "backup")


def test_writer_creates_complete_round_trippable_generation(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    video, comments, collection = valid_models()

    write_generation(staging, video, comments, collection)

    assert RawVideo.model_validate_json((staging / "video.json").read_bytes()) == video
    assert read_jsonl(staging / "comments.jsonl", RawComment) == comments
    assert (
        CollectionRecord.model_validate_json((staging / "collection.json").read_bytes())
        == collection
    )
    assert (staging / "comments.jsonl").read_bytes().endswith(b"\n")
    validate_generation(staging)


@pytest.mark.parametrize("case", ["missing", "blank", "non_json", "extra_field"])
def test_read_jsonl_converts_input_errors_to_safe_validation_errors(
    tmp_path: Path, case: str
) -> None:
    path = tmp_path / "comments.jsonl"
    if case == "blank":
        path.write_text("\n", encoding="utf-8")
    elif case == "non_json":
        path.write_text("secret-marker not-json\n", encoding="utf-8")
    elif case == "extra_field":
        payload = comment_data(cookie="secret-marker")
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(ArtifactValidationError) as caught:
        read_jsonl(path, RawComment)

    message = str(caught.value)
    assert case in message
    assert "secret-marker" not in message
    if case != "missing":
        assert "line_1" in message


@pytest.mark.parametrize(
    "invalid", ["missing_file", "bad_json", "duplicate", "self_parent", "count"]
)
def test_validate_generation_rejects_invalid_generation(tmp_path: Path, invalid: str) -> None:
    staging = tmp_path / "staging"
    write_valid(staging)
    if invalid == "missing_file":
        (staging / "video.json").unlink()
    elif invalid == "bad_json":
        (staging / "collection.json").write_text("secret-marker", encoding="utf-8")
    elif invalid == "duplicate":
        comment = RawComment.model_validate(comment_data("comment-1"))
        (staging / "comments.jsonl").write_text(
            comment.model_dump_json() + "\n" + comment.model_dump_json() + "\n",
            encoding="utf-8",
        )
    elif invalid == "self_parent":
        comments = [
            RawComment.model_validate(comment_data("comment-1", raw_parent_comment_id="comment-1")),
            RawComment.model_validate(comment_data("comment-2")),
        ]
        video, _, collection = valid_models()
        write_generation(staging, video, comments, collection)
    else:
        (staging / "collection.json").write_text(
            CollectionRecord.model_validate(collection_data(collected_total=1)).model_dump_json(),
            encoding="utf-8",
        )

    with pytest.raises(ArtifactValidationError) as caught:
        validate_generation(staging)

    assert "secret-marker" not in str(caught.value)


def test_invalid_staging_does_not_change_existing_target(tmp_path: Path) -> None:
    paths = CommitPaths(tmp_path / "staging", tmp_path / "target", tmp_path / "backup")
    write_valid(paths.target, title="旧结果")
    before = {path.name: path.read_bytes() for path in paths.target.iterdir()}
    write_valid(paths.staging, title="新结果")
    (paths.staging / "comments.jsonl").write_text("not-json\n", encoding="utf-8")

    with pytest.raises(ArtifactValidationError):
        commit_generation(paths)

    assert {path.name: path.read_bytes() for path in paths.target.iterdir()} == before
    assert not paths.backup.exists()


def test_successful_commit_replaces_existing_target(tmp_path: Path) -> None:
    paths = CommitPaths(tmp_path / "staging", tmp_path / "target", tmp_path / "backup")
    write_valid(paths.target, title="旧结果")
    write_valid(paths.staging, title="新结果")

    commit_generation(paths)

    video, _, _ = validate_generation(paths.target)
    assert video.title == "新结果"
    assert not paths.staging.exists()
    assert paths.backup.exists()
    old_video, _, _ = validate_generation(paths.backup)
    assert old_video.title == "旧结果"


def test_first_commit_moves_staging_to_target(tmp_path: Path) -> None:
    paths = CommitPaths(
        tmp_path / "nested/staging", tmp_path / "nested/target", tmp_path / "backup"
    )
    write_valid(paths.staging)

    commit_generation(paths)

    validate_generation(paths.target)
    assert not paths.staging.exists()
    assert not paths.backup.exists()


def test_existing_backup_is_rejected_without_changes(tmp_path: Path) -> None:
    paths = CommitPaths(tmp_path / "staging", tmp_path / "target", tmp_path / "backup")
    write_valid(paths.staging, title="新结果")
    write_valid(paths.target, title="旧结果")
    paths.backup.mkdir()
    marker = paths.backup / "marker"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(ArtifactCommitError, match="backup_exists"):
        commit_generation(paths)

    assert marker.read_text(encoding="utf-8") == "keep"
    assert (
        RawVideo.model_validate_json((paths.target / "video.json").read_bytes()).title == "旧结果"
    )


@pytest.mark.parametrize("has_old_target", [False, True])
def test_failed_staging_move_restores_old_target_or_leaves_target_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_old_target: bool
) -> None:
    paths = CommitPaths(tmp_path / "staging", tmp_path / "target", tmp_path / "backup")
    write_valid(paths.staging, title="新结果")
    if has_old_target:
        write_valid(paths.target, title="旧结果")
    original_move = artifacts._move_directory

    def fail_staging_move(source: Path, destination: Path) -> None:
        if source == paths.staging:
            raise OSError("secret-marker")
        original_move(source, destination)

    monkeypatch.setattr(artifacts, "_move_directory", fail_staging_move)

    with pytest.raises(ArtifactCommitError) as caught:
        commit_generation(paths)

    assert "secret-marker" not in str(caught.value)
    assert paths.staging.exists()
    assert not paths.backup.exists()
    assert paths.target.exists() is has_old_target
    if has_old_target:
        assert (
            RawVideo.model_validate_json((paths.target / "video.json").read_bytes()).title
            == "旧结果"
        )


def test_recover_interrupted_commit_restores_only_missing_target(tmp_path: Path) -> None:
    paths = CommitPaths(tmp_path / "staging", tmp_path / "target", tmp_path / "backup")
    write_valid(paths.backup, title="旧结果")

    assert recover_interrupted_commit(paths) is True
    assert not paths.backup.exists()
    assert (
        RawVideo.model_validate_json((paths.target / "video.json").read_bytes()).title == "旧结果"
    )
    assert recover_interrupted_commit(paths) is False


def test_recovery_does_not_overwrite_existing_target(tmp_path: Path) -> None:
    paths = CommitPaths(tmp_path / "staging", tmp_path / "target", tmp_path / "backup")
    write_valid(paths.target, title="新结果")
    write_valid(paths.backup, title="旧结果")

    assert recover_interrupted_commit(paths) is False
    assert (
        RawVideo.model_validate_json((paths.target / "video.json").read_bytes()).title == "新结果"
    )
    assert paths.backup.exists()
