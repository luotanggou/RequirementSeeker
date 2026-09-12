from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import cast

import pytest

import requirementseeker_collector.runner as runner
from requirementseeker_collector.adapters.base import ResponseShapeChanged
from requirementseeker_collector.artifacts import (
    ArtifactCommitError,
    read_jsonl,
    validate_generation,
)
from requirementseeker_collector.challenges import ChallengeResult, ClickAction
from requirementseeker_collector.contracts import (
    CollectionError,
    CollectionManifest,
    ManifestVideo,
    RawComment,
    RawVideo,
)
from requirementseeker_collector.runner import (
    BrowserResult,
    BrowserVideoCollector,
    CliSupervisionGate,
    PilotRequest,
    PilotResult,
    run_batch,
    run_pilot,
)

NOW = datetime(2026, 9, 9, 1, tzinfo=UTC)


def create_directory_redirect(link: Path, target: Path, kind: str) -> None:
    target.mkdir(parents=True)
    link.parent.mkdir(parents=True, exist_ok=True)
    if kind == "junction":
        if sys.platform != "win32":
            pytest.skip("Windows junction test")
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlink unavailable: {error}")


def pending_marker(root: Path, run_id: str, platform: str, video_key: str) -> Path:
    return root / ".backup" / run_id / platform / f"{video_key}.pending"


def video(platform: str = "bilibili", video_key: str = "BVfake") -> RawVideo:
    return RawVideo.model_validate(
        {
            "platform": platform,
            "raw_video_id": video_key,
            "raw_author_id": "video-author",
            "title": "synthetic video",
            "description": "offline fixture",
            "published_at": None,
            "duration_seconds": 10,
            "total_comment_count": 2,
            "view_count": None,
            "like_count": None,
            "favorite_count": None,
            "share_count": None,
            "author_follower_count": None,
            "captured_at": NOW,
        }
    )


def comment(
    comment_id: str,
    *,
    text: str | None = None,
    stratum: str = "top",
    rank: int = 1,
) -> RawComment:
    return RawComment.model_validate(
        {
            "raw_comment_id": comment_id,
            "raw_author_id": "comment-author",
            "raw_parent_comment_id": None,
            "text": text or f"comment {comment_id}",
            "published_at": None,
            "collected_at": NOW,
            "like_count": None,
            "reply_count": None,
            "is_video_author": False,
            "source_stratum": stratum,
            "source_page_or_rank": rank,
        }
    )


def request(platform: str = "bilibili", video_key: str | None = "BVfake") -> PilotRequest:
    host = "www.bilibili.com" if platform == "bilibili" else "www.douyin.com"
    return PilotRequest.model_validate(
        {"platform": platform, "url": f"https://{host}/video/example", "video_key": video_key}
    )


def browser_result(
    *,
    platform: str = "bilibili",
    video_key: str = "BVfake",
    comments: list[RawComment] | None = None,
    status: str = "success",
    errors: list[CollectionError] | None = None,
) -> BrowserResult:
    return BrowserResult(
        video=video(platform, video_key),
        comments=comments or [comment("c1"), comment("c2", stratum="recent")],
        pages_requested=2,
        pages_succeeded=2,
        sort_modes=["top", "recent"],
        collection_started_at=NOW,
        collection_finished_at=NOW,
        collection_errors=errors or [],
        status=status,
    )


def manifest_video(platform: str, video_key: str) -> ManifestVideo:
    host = "www.bilibili.com" if platform == "bilibili" else "www.douyin.com"
    return ManifestVideo.model_validate(
        {
            "platform": platform,
            "video_key": video_key,
            "url": f"https://{host}/video/{video_key}",
            "direction": "software_tools",
            "comment_scale": "up_to_200",
        }
    )


def test_pilot_writes_three_valid_files(tmp_path: Path) -> None:
    result = run_pilot(request(), browser_result(), output_root=tmp_path)

    target = tmp_path / "raw" / "bilibili" / "BVfake"
    assert result.status == "success"
    assert result.target == 2
    assert sorted(path.name for path in target.iterdir()) == [
        "collection.json",
        "comments.jsonl",
        "video.json",
    ]
    _, comments, collection = validate_generation(target)
    assert [item.raw_comment_id for item in comments] == ["c1", "c2"]
    assert collection.collected_total == 2


def test_pilot_writes_safe_compact_run_report(tmp_path: Path) -> None:
    result = run_pilot(request(), browser_result(), output_root=tmp_path)

    reports = list((tmp_path / "runs").glob("*/run.json"))
    assert result.status == "success"
    assert len(reports) == 1
    raw = reports[0].read_text(encoding="ascii")
    assert "comment c1" not in raw
    assert "https://" not in raw
    assert "\n" not in raw
    assert json.loads(raw) == {
        "collected_total": 2,
        "collection_finished_at": "2026-09-09T01:00:00+00:00",
        "collection_started_at": "2026-09-09T01:00:00+00:00",
        "errors": [],
        "pages_requested": 2,
        "pages_succeeded": 2,
        "platform": "bilibili",
        "run_version": "1.0",
        "status": "success",
        "target": 2,
        "video_key": "BVfake",
    }


@pytest.mark.parametrize("unsafe_run_id", ["../escape-run-id", "", " "])
def test_pilot_replaces_an_untrusted_browser_run_id_with_a_safe_local_id(
    tmp_path: Path, unsafe_run_id: str
) -> None:
    output_root = tmp_path / "root"
    unsafe = replace(browser_result(), run_id=unsafe_run_id)

    result = run_pilot(request(), unsafe, output_root=output_root)

    assert result.status == "success"
    assert not (output_root / "escape-run-id").exists()
    assert len(list((output_root / "runs").glob("*/run.json"))) == 1


def test_run_report_maps_unknown_error_categories_to_a_safe_fixed_value(
    tmp_path: Path,
) -> None:
    unsafe_error = CollectionError(
        category="password-secret-marker",
        occurred_at=NOW,
        stage="runner",
        description="token-secret-marker",
    )

    run_pilot(request(), browser_result(errors=[unsafe_error]), output_root=tmp_path)

    report = next((tmp_path / "runs").glob("*/run.json")).read_text(encoding="ascii")
    assert "secret-marker" not in report
    assert json.loads(report)["errors"] == ["collection_error"]


def test_pilot_uses_parsed_video_id_when_key_is_omitted(tmp_path: Path) -> None:
    result = run_pilot(request(video_key=None), browser_result(), output_root=tmp_path)

    assert result.video_key == "BVfake"
    validate_generation(tmp_path / "raw" / "bilibili" / "BVfake")


def test_pilot_rejects_platform_mismatch_without_writing(tmp_path: Path) -> None:
    result = run_pilot(request(), browser_result(platform="douyin"), output_root=tmp_path)

    assert result.status == "platform_mismatch"
    assert not (tmp_path / "raw").exists()


@pytest.mark.parametrize(
    "video_key",
    [
        ".",
        "..",
        "../escape",
        r"folder\escape",
        "C:escape",
        "bad<key",
        "bad>key",
        'bad"key',
        "bad|key",
        "bad?key",
        "bad*key",
        "name.",
        "CON",
        "com1.txt",
    ],
)
def test_pilot_rejects_unsafe_video_key_without_writing(tmp_path: Path, video_key: str) -> None:
    result = run_pilot(
        request(video_key=video_key),
        browser_result(video_key=video_key),
        output_root=tmp_path,
    )

    assert result.status == "invalid_video_key"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("video_key", ["", "two words", "unsafe\u0085key"])
def test_shared_video_key_safety_rejects_empty_or_whitespace_segments(video_key: str) -> None:
    assert runner.video_key_is_safe(video_key) is False


def test_pilot_merges_previous_comments_and_records_identity_conflict(tmp_path: Path) -> None:
    first = run_pilot(request(), browser_result(), output_root=tmp_path)
    assert first.status == "success"
    changed = browser_result(
        comments=[
            comment("c1", text="changed identity"),
            comment("c3", stratum="long_tail"),
        ]
    )

    second = run_pilot(request(), changed, output_root=tmp_path)

    assert second.status == "success"
    target = tmp_path / "raw" / "bilibili" / "BVfake"
    _, comments, collection = validate_generation(target)
    by_id = {item.raw_comment_id: item for item in comments}
    assert sorted(by_id) == ["c1", "c2", "c3"]
    assert by_id["c1"].text == "comment c1"
    conflict = next(
        error for error in collection.collection_errors if error.category == "merge_conflict"
    )
    assert conflict.raw_comment_id == "c1"
    assert conflict.conflict_fields == ["text"]
    assert "changed identity" not in conflict.description


def test_pilot_recovers_pending_valid_backup_before_merging_current_comments(
    tmp_path: Path,
) -> None:
    first = run_pilot(request(), browser_result(), output_root=tmp_path)
    assert first.status == "success"
    target = tmp_path / "raw" / "bilibili" / "BVfake"
    backup = tmp_path / ".backup" / "interrupted" / "bilibili" / "BVfake"
    backup.parent.mkdir(parents=True)
    target.replace(backup)
    pending_marker(tmp_path, "interrupted", "bilibili", "BVfake").write_bytes(b"")

    result = run_pilot(request(), browser_result(comments=[comment("c3")]), output_root=tmp_path)

    assert result.status == "partial"
    _, comments, collection = validate_generation(target)
    assert [item.raw_comment_id for item in comments] == ["c1", "c2", "c3"]
    assert any(
        error.category == "interrupted_commit_recovered" for error in collection.collection_errors
    )
    assert not backup.exists()
    assert not (tmp_path / ".backup" / "interrupted").exists()


def test_recovered_target_with_marker_cleanup_failure_reports_cleanup_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_pilot(request(), browser_result(), output_root=tmp_path).status == "success"
    target = tmp_path / "raw" / "bilibili" / "BVfake"
    backup = tmp_path / ".backup" / "interrupted" / "bilibili" / "BVfake"
    backup.parent.mkdir(parents=True)
    target.replace(backup)
    marker = pending_marker(tmp_path, "interrupted", "bilibili", "BVfake")
    marker.write_bytes(b"")
    original_unlink = Path.unlink
    deny_marker = True

    def deny_once(path: Path, *args: object, **kwargs: object) -> None:
        if deny_marker and path == marker:
            raise PermissionError("denied")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_once)
    failed = run_pilot(
        request(),
        replace(
            browser_result(comments=[comment("c3")]),
            run_id="recovery-cleanup-failed",
        ),
        output_root=tmp_path,
    )

    assert failed.status == "artifact_cleanup_failed"
    assert marker.exists()
    _, comments, _ = validate_generation(target)
    assert [item.raw_comment_id for item in comments] == ["c1", "c2"]
    failed_report = json.loads(
        (tmp_path / "runs" / "recovery-cleanup-failed" / "run.json").read_text(encoding="ascii")
    )
    assert failed_report["errors"] == [
        "interrupted_commit_recovered",
        "artifact_cleanup_failed",
    ]

    deny_marker = False
    retried = run_pilot(
        request(),
        replace(browser_result(comments=[comment("c3")]), run_id="recovery-cleanup-retry"),
        output_root=tmp_path,
    )

    assert retried.status == "partial"
    _, comments, _ = validate_generation(target)
    assert [item.raw_comment_id for item in comments] == ["c1", "c2", "c3"]
    retry_report = json.loads(
        (tmp_path / "runs" / "recovery-cleanup-retry" / "run.json").read_text(encoding="ascii")
    )
    assert "interrupted_commit_recovered" not in retry_report["errors"]


def test_pilot_does_not_restore_a_backup_over_an_existing_target(tmp_path: Path) -> None:
    run_pilot(request(), browser_result(), output_root=tmp_path)
    target = tmp_path / "raw" / "bilibili" / "BVfake"
    backup = tmp_path / ".backup" / "stale" / "bilibili" / "BVfake"
    backup.parent.mkdir(parents=True)
    backup.mkdir()
    (backup / "marker").write_text("must remain unused", encoding="utf-8")

    result = run_pilot(request(), browser_result(), output_root=tmp_path)

    assert result.status == "success"
    assert (backup / "marker").read_text(encoding="utf-8") == "must remain unused"
    validate_generation(target)


def test_pilot_fails_closed_when_multiple_pending_backup_candidates_exist(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    run_pilot(request(), browser_result(), output_root=source_root)
    source = source_root / "raw" / "bilibili" / "BVfake"
    for run_id in ("one", "two"):
        candidate = tmp_path / ".backup" / run_id / "bilibili" / "BVfake"
        candidate.parent.mkdir(parents=True)
        shutil.copytree(source, candidate)
        pending_marker(tmp_path, run_id, "bilibili", "BVfake").write_bytes(b"")
    collector_input = browser_result(comments=[comment("c3")])

    result = run_pilot(request(), collector_input, output_root=tmp_path)

    assert result.status == "backup_recovery_failed"
    assert not (tmp_path / "raw" / "bilibili" / "BVfake").exists()


@pytest.mark.parametrize("candidate_kind", ["invalid", "symlink"])
def test_pilot_fails_closed_for_invalid_or_symbolic_backup_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_kind: str,
) -> None:
    candidate = tmp_path / ".backup" / "interrupted" / "bilibili" / "BVfake"
    candidate.mkdir(parents=True)
    (candidate / "marker").write_text("not a valid generation", encoding="utf-8")
    pending_marker(tmp_path, "interrupted", "bilibili", "BVfake").write_bytes(b"")
    if candidate_kind == "symlink":
        original_is_symlink = Path.is_symlink

        def fake_is_symlink(path: Path) -> bool:
            return path == candidate or original_is_symlink(path)

        monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)

    result = run_pilot(request(), browser_result(), output_root=tmp_path)

    assert result.status == "backup_recovery_failed"
    assert not (tmp_path / "raw" / "bilibili" / "BVfake").exists()


def test_pilot_ignores_historical_backup_and_recovers_only_pending_transaction(
    tmp_path: Path,
) -> None:
    first = run_pilot(request(), replace(browser_result(), run_id="initial"), output_root=tmp_path)
    assert first.status == "success"
    second = run_pilot(
        request(),
        replace(browser_result(comments=[comment("c3")]), run_id="update"),
        output_root=tmp_path,
    )
    assert second.status == "partial"

    target = tmp_path / "raw" / "bilibili" / "BVfake"
    historical = tmp_path / ".backup" / "historical" / "bilibili" / "BVfake"
    historical.parent.mkdir(parents=True)
    shutil.copytree(target, historical)

    interrupted = tmp_path / ".backup" / "interrupted" / "bilibili" / "BVfake"
    interrupted.parent.mkdir(parents=True)
    target.replace(interrupted)
    pending_marker(tmp_path, "interrupted", "bilibili", "BVfake").write_bytes(b"")

    result = run_pilot(
        request(),
        replace(browser_result(comments=[comment("c4")]), run_id="retry"),
        output_root=tmp_path,
    )

    assert result.status == "partial"
    _, comments, collection = validate_generation(target)
    assert [item.raw_comment_id for item in comments] == ["c1", "c2", "c3", "c4"]
    assert any(
        error.category == "interrupted_commit_recovered" for error in collection.collection_errors
    )
    assert historical.exists()


def test_pilot_does_not_reuse_or_remove_a_preexisting_pending_marker(tmp_path: Path) -> None:
    assert run_pilot(request(), browser_result(), output_root=tmp_path).status == "success"
    older_marker = pending_marker(tmp_path, "older", "bilibili", "BVfake")
    older_marker.parent.mkdir(parents=True)
    older_marker.write_bytes(b"")
    marker = pending_marker(tmp_path, "collision", "bilibili", "BVfake")
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"")

    result = run_pilot(
        request(),
        replace(browser_result(comments=[comment("c3")]), run_id="collision"),
        output_root=tmp_path,
    )

    assert result.status == "artifact_cleanup_failed"
    assert marker.exists()
    assert older_marker.exists()


def test_pilot_fails_closed_for_multiple_matching_pending_markers_with_valid_target(
    tmp_path: Path,
) -> None:
    assert run_pilot(request(), browser_result(), output_root=tmp_path).status == "success"
    target = tmp_path / "raw" / "bilibili" / "BVfake"
    markers: list[Path] = []
    for run_id in ("older-one", "older-two"):
        backup = tmp_path / ".backup" / run_id / "bilibili" / "BVfake"
        backup.parent.mkdir(parents=True)
        shutil.copytree(target, backup)
        marker = pending_marker(tmp_path, run_id, "bilibili", "BVfake")
        marker.write_bytes(b"")
        markers.append(marker)

    result = run_pilot(
        request(),
        replace(browser_result(comments=[comment("c3")]), run_id="new-run"),
        output_root=tmp_path,
    )

    assert result.status == "artifact_cleanup_failed"
    assert all(marker.exists() for marker in markers)
    assert not pending_marker(tmp_path, "new-run", "bilibili", "BVfake").exists()


@pytest.mark.parametrize(
    ("foreign_platform", "foreign_key"),
    [("bilibili", "BVforeign"), ("douyin", "DYforeign")],
)
def test_pilot_preserves_foreign_pending_transaction_and_later_recovers_it(
    tmp_path: Path,
    foreign_platform: str,
    foreign_key: str,
) -> None:
    foreign_request = request(foreign_platform, foreign_key)
    foreign_initial = browser_result(platform=foreign_platform, video_key=foreign_key)
    assert run_pilot(foreign_request, foreign_initial, output_root=tmp_path).status == "success"
    foreign_target = tmp_path / "raw" / foreign_platform / foreign_key
    foreign_backup = tmp_path / ".backup" / "foreign-pending" / foreign_platform / foreign_key
    foreign_backup.parent.mkdir(parents=True)
    foreign_target.replace(foreign_backup)
    foreign_marker = pending_marker(tmp_path, "foreign-pending", foreign_platform, foreign_key)
    foreign_marker.write_bytes(b"")
    before = {path.name: path.read_bytes() for path in foreign_backup.iterdir()}

    unrelated = run_pilot(
        request(), replace(browser_result(), run_id="unrelated"), output_root=tmp_path
    )

    assert unrelated.status == "success"
    assert foreign_marker.exists()
    assert {path.name: path.read_bytes() for path in foreign_backup.iterdir()} == before

    recovered = run_pilot(
        foreign_request,
        replace(
            browser_result(
                platform=foreign_platform,
                video_key=foreign_key,
                comments=[comment("new")],
            ),
            run_id="foreign-retry",
        ),
        output_root=tmp_path,
    )

    assert recovered.status == "partial"
    _, comments, collection = validate_generation(foreign_target)
    assert [item.raw_comment_id for item in comments] == ["c1", "c2", "new"]
    assert any(
        error.category == "interrupted_commit_recovered" for error in collection.collection_errors
    )


def test_existing_pending_cleanup_failure_stops_before_new_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_pilot(request(), browser_result(), output_root=tmp_path).status == "success"
    target = tmp_path / "raw" / "bilibili" / "BVfake"
    backup = tmp_path / ".backup" / "old-pending" / "bilibili" / "BVfake"
    backup.parent.mkdir(parents=True)
    shutil.copytree(target, backup)
    marker = pending_marker(tmp_path, "old-pending", "bilibili", "BVfake")
    marker.write_bytes(b"")
    original_unlink = Path.unlink

    def deny_backup_cleanup(path: Path, *args: object, **kwargs: object) -> None:
        if path == backup / "video.json":
            raise PermissionError("denied")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_backup_cleanup)
    result = run_pilot(
        request(),
        replace(browser_result(comments=[comment("c3")]), run_id="new-run"),
        output_root=tmp_path,
    )

    assert result.status == "artifact_cleanup_failed"
    assert marker.exists()
    assert not pending_marker(tmp_path, "new-run", "bilibili", "BVfake").exists()
    _, comments, _ = validate_generation(target)
    assert [item.raw_comment_id for item in comments] == ["c1", "c2"]


@pytest.mark.parametrize("denied_entry", ["backup", "marker"])
def test_post_commit_cleanup_failure_is_retryable_without_stacking_transactions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, denied_entry: str
) -> None:
    assert run_pilot(request(), browser_result(), output_root=tmp_path).status == "success"
    backup = tmp_path / ".backup" / "cleanup-failure" / "bilibili" / "BVfake"
    marker = pending_marker(tmp_path, "cleanup-failure", "bilibili", "BVfake")
    original_unlink = Path.unlink
    fail_cleanup = True

    def deny_once(path: Path, *args: object, **kwargs: object) -> None:
        denied_path = backup / "video.json" if denied_entry == "backup" else marker
        if fail_cleanup and path == denied_path:
            raise PermissionError("denied")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_once)
    failed = run_pilot(
        request(),
        replace(browser_result(comments=[comment("c3")]), run_id="cleanup-failure"),
        output_root=tmp_path,
    )

    assert failed.status == "artifact_cleanup_failed"
    assert marker.exists()
    target = tmp_path / "raw" / "bilibili" / "BVfake"
    _, comments, _ = validate_generation(target)
    assert [item.raw_comment_id for item in comments] == ["c1", "c2", "c3"]

    fail_cleanup = False
    retried = run_pilot(
        request(),
        replace(browser_result(comments=[comment("c4")]), run_id="cleanup-retry"),
        output_root=tmp_path,
    )

    assert retried.status == "partial"
    _, comments, _ = validate_generation(target)
    assert [item.raw_comment_id for item in comments] == ["c1", "c2", "c3", "c4"]
    assert not marker.parent.exists()
    assert not marker.exists()


def test_post_commit_parent_cleanup_failure_is_reported_and_later_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_pilot(request(), browser_result(), output_root=tmp_path).status == "success"
    marker = pending_marker(tmp_path, "parent-failure", "bilibili", "BVfake")
    original_rmdir = Path.rmdir
    fail_cleanup = True

    def deny_once(path: Path) -> None:
        if fail_cleanup and path == marker.parent:
            raise PermissionError("denied")
        original_rmdir(path)

    monkeypatch.setattr(Path, "rmdir", deny_once)
    failed = run_pilot(
        request(),
        replace(browser_result(comments=[comment("c3")]), run_id="parent-failure"),
        output_root=tmp_path,
    )

    assert failed.status == "artifact_cleanup_failed"
    target = tmp_path / "raw" / "bilibili" / "BVfake"
    _, comments, _ = validate_generation(target)
    assert [item.raw_comment_id for item in comments] == ["c1", "c2", "c3"]

    fail_cleanup = False
    retried = run_pilot(
        request(),
        replace(browser_result(comments=[comment("c4")]), run_id="parent-retry"),
        output_root=tmp_path,
    )

    assert retried.status == "partial"
    _, comments, _ = validate_generation(target)
    assert [item.raw_comment_id for item in comments] == ["c1", "c2", "c3", "c4"]
    assert not marker.parent.exists()


@pytest.mark.parametrize("link_kind", ["junction", "symlink"])
@pytest.mark.parametrize("reserved", ["raw", "runs", ".staging", ".backup"])
def test_pilot_rejects_redirected_reserved_output_tree(
    tmp_path: Path,
    link_kind: str,
    reserved: str,
) -> None:
    output_root = tmp_path / "output"
    outside = tmp_path / "outside" / reserved.replace(".", "dot-")
    output_root.mkdir()
    create_directory_redirect(output_root / reserved, outside, link_kind)

    result = run_pilot(
        request(),
        replace(browser_result(), run_id="guarded-run"),
        output_root=output_root,
    )

    assert result.status != "success"
    assert list(outside.iterdir()) == []


def test_current_run_conflict_does_not_replace_previous_valid_result(tmp_path: Path) -> None:
    run_pilot(request(), browser_result(), output_root=tmp_path)
    target = tmp_path / "raw" / "bilibili" / "BVfake"
    before = {path.name: path.read_bytes() for path in target.iterdir()}
    conflicting = browser_result(comments=[comment("c1", text="one"), comment("c1", text="two")])

    result = run_pilot(request(), conflicting, output_root=tmp_path)

    assert result.status == "current_run_conflict"
    assert {path.name: path.read_bytes() for path in target.iterdir()} == before


def test_fatal_browser_result_does_not_replace_previous_valid_result(tmp_path: Path) -> None:
    run_pilot(request(), browser_result(), output_root=tmp_path)
    target = tmp_path / "raw" / "bilibili" / "BVfake"
    before = {path.name: path.read_bytes() for path in target.iterdir()}

    result = run_pilot(
        request(),
        replace(browser_result(), status="response_shape_changed", comments=[]),
        output_root=tmp_path,
    )

    assert result.status == "response_shape_changed"
    assert {path.name: path.read_bytes() for path in target.iterdir()} == before
    reports = list((tmp_path / "runs").glob("*/run.json"))
    assert len(reports) == 2
    assert {json.loads(path.read_text(encoding="ascii"))["status"] for path in reports} == {
        "success",
        "response_shape_changed",
    }


def test_commit_failure_returns_safe_status_and_preserves_previous_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_pilot(request(), browser_result(), output_root=tmp_path)
    target = tmp_path / "raw" / "bilibili" / "BVfake"
    before = {path.name: path.read_bytes() for path in target.iterdir()}

    def fail_commit(*args: object, **kwargs: object) -> None:
        raise ArtifactCommitError("secret-marker")

    monkeypatch.setattr(runner, "commit_generation", fail_commit)
    result = run_pilot(request(), browser_result(comments=[comment("c3")]), output_root=tmp_path)

    assert result.status == "artifact_commit_failed"
    assert "secret-marker" not in str(result.to_summary())
    assert {path.name: path.read_bytes() for path in target.iterdir()} == before


class FakeCollector:
    def __init__(self, statuses: dict[tuple[str, str], str] | None = None) -> None:
        self.statuses = statuses or {}
        self.calls: list[tuple[str, str]] = []

    def collect(self, item: ManifestVideo, output_root: Path) -> PilotResult:
        del output_root
        self.calls.append((item.platform, item.video_key))
        status = self.statuses.get((item.platform, item.video_key), "success")
        return PilotResult(
            platform=item.platform,
            video_key=item.video_key,
            status=status,
            target=2,
            collected_total=2 if status == "success" else 0,
        )


def test_batch_stops_only_failed_platform(tmp_path: Path) -> None:
    manifest = CollectionManifest(
        manifest_version="1.0",
        videos=[
            manifest_video("bilibili", "BV1"),
            manifest_video("bilibili", "BV2"),
            manifest_video("douyin", "DY1"),
            manifest_video("douyin", "DY2"),
        ],
        unavailable_platforms=[],
    )
    collector = FakeCollector({("bilibili", "BV1"): "login_failed"})

    result = run_batch(manifest, collector=collector, output_root=tmp_path)

    assert result.platforms["bilibili"].status == "stopped"
    assert result.platforms["douyin"].videos_succeeded == 2
    assert collector.calls == [
        ("bilibili", "BV1"),
        ("douyin", "DY1"),
        ("douyin", "DY2"),
    ]
    assert [(item.video_key, item.status) for item in result.results] == [
        ("BV1", "login_failed"),
        ("BV2", "platform_stopped"),
        ("DY1", "success"),
        ("DY2", "success"),
    ]


@pytest.mark.parametrize(
    "fatal_status",
    ["login_failed", "challenge_unresolved", "access_restricted", "response_shape_changed"],
)
def test_batch_recognizes_exact_fatal_platform_statuses(tmp_path: Path, fatal_status: str) -> None:
    manifest = CollectionManifest(
        manifest_version="1.0",
        videos=[manifest_video("douyin", "DY1"), manifest_video("douyin", "DY2")],
        unavailable_platforms=[],
    )
    collector = FakeCollector({("douyin", "DY1"): fatal_status})

    result = run_batch(manifest, collector=collector, output_root=tmp_path)

    assert [item.status for item in result.results] == [fatal_status, "platform_stopped"]


def test_batch_keeps_manifest_order_and_does_not_stop_for_nonfatal_status(
    tmp_path: Path,
) -> None:
    manifest = CollectionManifest(
        manifest_version="1.0",
        videos=[
            manifest_video("douyin", "DY2"),
            manifest_video("bilibili", "BV1"),
            manifest_video("douyin", "DY1"),
        ],
        unavailable_platforms=[],
    )
    collector = FakeCollector({("douyin", "DY2"): "partial"})

    result = run_batch(manifest, collector=collector, output_root=tmp_path)

    assert collector.calls == [("douyin", "DY2"), ("bilibili", "BV1"), ("douyin", "DY1")]
    assert [item.video_key for item in result.results] == ["DY2", "BV1", "DY1"]
    assert result.platforms["douyin"].status == "completed"


def test_batch_rejects_collector_result_for_another_manifest_item(tmp_path: Path) -> None:
    manifest = CollectionManifest(
        manifest_version="1.0",
        videos=[manifest_video("bilibili", "BV1"), manifest_video("bilibili", "BV2")],
        unavailable_platforms=[],
    )

    class MismatchedCollector:
        def collect(self, item: ManifestVideo, output_root: Path) -> PilotResult:
            del item, output_root
            return PilotResult("douyin", "wrong", "success", 2, 2)

    result = run_batch(manifest, collector=MismatchedCollector(), output_root=tmp_path)

    assert [(item.platform, item.video_key, item.status) for item in result.results] == [
        ("bilibili", "BV1", "response_shape_changed"),
        ("bilibili", "BV2", "platform_stopped"),
    ]


def test_batch_rejects_casefolded_duplicate_output_paths_before_collecting(
    tmp_path: Path,
) -> None:
    manifest = CollectionManifest(
        manifest_version="1.0",
        videos=[manifest_video("bilibili", "BVsame"), manifest_video("bilibili", "bvsame")],
        unavailable_platforms=[],
    )
    collector = FakeCollector()

    result = run_batch(manifest, collector=collector, output_root=tmp_path)

    assert collector.calls == []
    assert [item.status for item in result.results] == [
        "invalid_manifest_path",
        "invalid_manifest_path",
    ]


def test_batch_does_not_report_an_empty_manifest_as_success(tmp_path: Path) -> None:
    manifest = CollectionManifest(manifest_version="1.0", videos=[], unavailable_platforms=[])
    collector = FakeCollector()

    result = run_batch(manifest, collector=collector, output_root=tmp_path)

    assert collector.calls == []
    assert result.status == "invalid_manifest"
    assert result.results == []


def test_live_collector_buffers_supported_comments_that_arrive_before_video_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixtures = Path(__file__).parent / "fixtures" / "bilibili"
    video_payload = json.loads((fixtures / "video.json").read_text(encoding="utf-8"))
    comments_payload = json.loads((fixtures / "comments.json").read_text(encoding="utf-8"))

    class FakePage:
        def wait_for_timeout(self, milliseconds: float) -> None:
            del milliseconds

    class FakeSession:
        page = FakePage()

        def __enter__(self) -> FakeSession:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def open(self, url: str, adapter: object, consume: object) -> None:
            del url, adapter
            callback = cast(Callable[[str, object], None], consume)
            callback(
                "https://api.bilibili.com/x/v2/reply/wbi/main",
                comments_payload,
            )
            callback("https://api.bilibili.com/x/web-interface/view", video_payload)

        def raise_if_response_failed(self) -> None:
            pass

    monkeypatch.setattr(runner, "BrowserSession", FakeSession)
    monkeypatch.setattr(runner, "perform_stratum_action", lambda page, stratum: "unavailable")

    result = BrowserVideoCollector(supervisor=SequenceSupervisor("ready"))._browse(
        PilotRequest(
            platform="bilibili",
            url="https://www.bilibili.com/video/BV1synthetic",
            video_key="BV1synthetic",
        )
    )

    assert result.status == "success"
    assert result.video is not None
    assert [item.raw_comment_id for item in result.comments] == ["11", "12"]
    assert result.pages_requested == result.pages_succeeded == 1


@pytest.mark.parametrize(
    ("video_key", "url"),
    [
        ("BV1synthetic", "https://www.bilibili.com/video/BV1synthetic"),
        (None, "https://www.bilibili.com/video/BV1synthetic"),
        (None, "https://www.bilibili.com/video/BV1synthetic/?source=synthetic"),
    ],
)
def test_live_bilibili_collector_falls_back_to_standard_page_metadata(
    monkeypatch: pytest.MonkeyPatch,
    video_key: str | None,
    url: str,
) -> None:
    comments_payload = json.loads(
        (Path(__file__).parent / "fixtures/bilibili/comments.json").read_text(encoding="utf-8")
    )

    class MetaLocator:
        def __init__(self, value: str | None) -> None:
            self.value = value

        def get_attribute(self, name: str) -> str | None:
            assert name == "content"
            return self.value

    class FakePage:
        metadata = {
            'meta[property="og:title"]': "Synthetic fallback title",
            'meta[name="description"]': "Synthetic fallback description",
        }

        def locator(self, selector: str) -> MetaLocator:
            return MetaLocator(self.metadata.get(selector))

        def wait_for_timeout(self, milliseconds: float) -> None:
            del milliseconds

    class CommentOnlySession:
        page = FakePage()

        def __enter__(self) -> CommentOnlySession:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def open(self, url: str, adapter: object, consume: object) -> None:
            del url, adapter
            cast(Callable[[str, object], None], consume)(
                "https://api.bilibili.com/x/v2/reply/wbi/main",
                comments_payload,
            )

        def raise_if_response_failed(self) -> None:
            pass

    monkeypatch.setattr(runner, "BrowserSession", CommentOnlySession)
    monkeypatch.setattr(runner, "perform_stratum_action", lambda page, stratum: "unavailable")

    result = BrowserVideoCollector(supervisor=SequenceSupervisor("ready"))._browse(
        PilotRequest(
            platform="bilibili",
            url=url,
            video_key=video_key,
        )
    )

    assert result.status == "success"
    assert result.video is not None
    assert result.video.raw_video_id == "BV1synthetic"
    assert result.video.raw_author_id == "42"
    assert result.video.title == "Synthetic fallback title"
    assert result.video.description == "Synthetic fallback description"
    assert result.video.duration_seconds is None
    assert result.video.published_at is None
    assert result.video.total_comment_count == 2
    assert result.video.view_count is None
    assert [item.raw_comment_id for item in result.comments] == ["11", "12"]
    assert result.pages_requested == result.pages_succeeded == 1


@pytest.mark.parametrize(
    ("video_key", "url", "mutation"),
    [
        ("BVother", "https://www.bilibili.com/video/BV1synthetic", "none"),
        (None, "https://www.bilibili.com/video/BV1synthetic/extra", "none"),
        (None, "https://www.bilibili.com/watch/BV1synthetic", "none"),
        (None, "https://www.bilibili.com/video/CON", "none"),
        (None, "https://www.bilibili.com/video/BV1*synthetic", "none"),
        (None, "https://www.bilibili.com/video/BV1|synthetic", "none"),
        (None, 'https://www.bilibili.com/video/BV1"synthetic', "none"),
        (None, "https://www.bilibili.com/video/BV1<synthetic", "none"),
        (None, "https://www.bilibili.com/video/BV1>synthetic", "none"),
        ("BV1synthetic", "https://www.bilibili.com/video/BV1synthetic", "missing_upper"),
        ("BV1synthetic", "https://www.bilibili.com/video/BV1synthetic", "bad_total"),
        ("BV1synthetic", "https://www.bilibili.com/video/BV1synthetic", "missing_title"),
    ],
)
def test_live_bilibili_metadata_fallback_fails_closed_without_consistent_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    video_key: str | None,
    url: str,
    mutation: str,
) -> None:
    payload = json.loads(
        (Path(__file__).parent / "fixtures/bilibili/comments.json").read_text(encoding="utf-8")
    )
    data = cast(dict[str, object], payload["data"])
    if mutation == "missing_upper":
        data.pop("upper")
    elif mutation == "bad_total":
        cast(dict[str, object], data["cursor"])["all_count"] = "2"

    class MetaLocator:
        def __init__(self, value: str | None) -> None:
            self.value = value

        def get_attribute(self, name: str) -> str | None:
            del name
            return self.value

    class FakePage:
        def locator(self, selector: str) -> MetaLocator:
            if mutation == "missing_title" and selector == 'meta[property="og:title"]':
                return MetaLocator(None)
            return MetaLocator("synthetic")

        def wait_for_timeout(self, milliseconds: float) -> None:
            del milliseconds

    class CommentOnlySession:
        page = FakePage()

        def __enter__(self) -> CommentOnlySession:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def open(self, opened_url: str, adapter: object, consume: object) -> None:
            del opened_url, adapter
            cast(Callable[[str, object], None], consume)(
                "https://api.bilibili.com/x/v2/reply/wbi/main", payload
            )

        def raise_if_response_failed(self) -> None:
            pass

    monkeypatch.setattr(runner, "BrowserSession", CommentOnlySession)
    monkeypatch.setattr(runner, "perform_stratum_action", lambda page, stratum: "unavailable")

    result = BrowserVideoCollector(supervisor=SequenceSupervisor("ready"))._browse(
        PilotRequest(platform="bilibili", url=url, video_key=video_key), tmp_path
    )

    assert result.status == "response_shape_changed"
    assert result.video is None
    assert result.comments == []
    assert result.pages_succeeded == 0
    assert not (tmp_path / "raw").exists()


def test_live_bilibili_metadata_fallback_rejects_inconsistent_comment_contexts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.loads(
        (Path(__file__).parent / "fixtures/bilibili/comments.json").read_text(encoding="utf-8")
    )
    conflicting = json.loads(json.dumps(payload))
    conflict_data = cast(dict[str, object], conflicting["data"])
    cast(dict[str, object], conflict_data["upper"])["mid"] = 99

    class FakePage:
        def wait_for_timeout(self, milliseconds: float) -> None:
            del milliseconds

    class InconsistentSession:
        page = FakePage()

        def __enter__(self) -> InconsistentSession:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def open(self, url: str, adapter: object, consume: object) -> None:
            del url, adapter
            callback = cast(Callable[[str, object], None], consume)
            callback("https://api.bilibili.com/x/v2/reply/wbi/main", payload)
            callback("https://api.bilibili.com/x/v2/reply/wbi/main", conflicting)

        def raise_if_response_failed(self) -> None:
            pass

    monkeypatch.setattr(runner, "BrowserSession", InconsistentSession)

    result = BrowserVideoCollector(supervisor=SequenceSupervisor("ready"))._browse(
        PilotRequest(
            platform="bilibili",
            url="https://www.bilibili.com/video/BV1synthetic",
            video_key="BV1synthetic",
        )
    )

    assert result.status == "response_shape_changed"
    assert result.video is None
    assert result.comments == []
    assert result.pages_succeeded == 0


def test_live_bilibili_metadata_fallback_rejects_later_inconsistent_video_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixtures = Path(__file__).parent / "fixtures" / "bilibili"
    comments_payload = json.loads((fixtures / "comments.json").read_text(encoding="utf-8"))
    video_payload = json.loads((fixtures / "video.json").read_text(encoding="utf-8"))
    cast(dict[str, object], cast(dict[str, object], video_payload["data"])["owner"])["mid"] = 99

    class MetaLocator:
        def get_attribute(self, name: str) -> str:
            del name
            return "synthetic"

    class FakePage:
        def locator(self, selector: str) -> MetaLocator:
            del selector
            return MetaLocator()

        def wait_for_timeout(self, milliseconds: float) -> None:
            del milliseconds

    class InconsistentSession:
        page = FakePage()

        def __enter__(self) -> InconsistentSession:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def open(self, url: str, adapter: object, consume: object) -> None:
            del url, adapter
            callback = cast(Callable[[str, object], None], consume)
            callback("https://api.bilibili.com/x/v2/reply/wbi/main", comments_payload)
            callback("https://api.bilibili.com/x/web-interface/view", video_payload)

        def raise_if_response_failed(self) -> None:
            pass

    monkeypatch.setattr(runner, "BrowserSession", InconsistentSession)

    result = BrowserVideoCollector(supervisor=SequenceSupervisor("ready"))._browse(
        PilotRequest(
            platform="bilibili",
            url="https://www.bilibili.com/video/BV1synthetic",
            video_key="BV1synthetic",
        )
    )

    assert result.status == "response_shape_changed"
    assert result.video is None
    assert result.comments == []
    assert result.pages_succeeded == 0


def test_live_manifest_collection_revalidates_the_public_url_as_text(tmp_path: Path) -> None:
    seen: list[PilotRequest] = []

    class RecordingCollector(BrowserVideoCollector):
        def collect_pilot(self, request: PilotRequest, output_root: Path) -> PilotResult:
            del output_root
            seen.append(request)
            return PilotResult(request.platform, request.video_key or "unknown", "success", 0, 0)

    item = manifest_video("bilibili", "BV1")
    result = RecordingCollector().collect(item, tmp_path)

    assert result.status == "success"
    assert seen[0].url.host == "www.bilibili.com"


@pytest.mark.parametrize(
    ("browser_error", "expected_status"),
    [
        ("response_processing_failed", "response_shape_changed"),
        ("browser_start_failed", "collection_failed"),
        ("browser_navigation_failed", "collection_failed"),
        ("browser_cleanup_failed", "collection_failed"),
        ("stratum_action_failed", "collection_failed"),
    ],
)
def test_live_collector_does_not_invent_access_restriction_from_local_browser_errors(
    monkeypatch: pytest.MonkeyPatch,
    browser_error: str,
    expected_status: str,
) -> None:
    class FailingSession:
        def __enter__(self) -> FailingSession:
            raise runner.BrowserSessionError(browser_error)

        def __exit__(self, *args: object) -> None:
            del args

    monkeypatch.setattr(runner, "BrowserSession", FailingSession)

    result = BrowserVideoCollector()._browse(request())

    assert result.status == expected_status


def test_live_collector_teardown_response_failure_prevents_raw_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = json.loads(
        (Path(__file__).parent / "fixtures/douyin/video.json").read_text(encoding="utf-8")
    )

    class FakePage:
        def wait_for_timeout(self, milliseconds: float) -> None:
            del milliseconds

    class TeardownFailingSession:
        page = FakePage()

        def __enter__(self) -> TeardownFailingSession:
            return self

        def __exit__(self, *args: object) -> None:
            del args
            raise ResponseShapeChanged("response_shape_changed") from None

        def open(self, url: str, adapter: object, consume: object) -> None:
            del url, adapter
            cast(Callable[[str, object], None], consume)(
                "https://www.douyin.com/aweme/v1/web/aweme/detail", payload
            )

        def raise_if_response_failed(self) -> None:
            pass

    monkeypatch.setattr(runner, "BrowserSession", TeardownFailingSession)
    monkeypatch.setattr(runner, "perform_stratum_action", lambda page, stratum: "unavailable")

    result = BrowserVideoCollector(supervisor=SequenceSupervisor("ready")).collect_pilot(
        PilotRequest(
            platform="douyin",
            url="https://www.douyin.com/video/7390000000000000000",
        ),
        tmp_path,
    )

    assert result.status == "response_shape_changed"
    assert not (tmp_path / "raw").exists()
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "reply",
    ["ready", "login_failed", "challenge_unresolved", "access_restricted"],
)
def test_cli_supervision_gate_returns_only_explicit_fixed_statuses(reply: str) -> None:
    prompts: list[str] = []
    gate = CliSupervisionGate(read_status=lambda timeout: reply, write_prompt=prompts.append)

    assert gate.wait_for_ready(object(), timeout_seconds=0.1) == reply
    assert len(prompts) == 1
    assert "https://" not in prompts[0]
    assert "does not authorize any mouse action" in prompts[0]


def test_cli_supervision_gate_timeout_leaves_no_reader_and_does_not_consume_next_status() -> None:
    replies: list[str | None] = [None, "ready"]
    observed_timeouts: list[float] = []

    def read_status(timeout_seconds: float) -> str | None:
        observed_timeouts.append(timeout_seconds)
        return replies.pop(0)

    gate = CliSupervisionGate(read_status=read_status, write_prompt=lambda _: None)
    threads_before = set(threading.enumerate())
    started = monotonic()

    status = gate.wait_for_ready(object(), timeout_seconds=0.01)

    assert status == "login_failed"
    assert monotonic() - started < 0.5
    assert set(threading.enumerate()) == threads_before
    assert gate.wait_for_ready(object(), timeout_seconds=0.02) == "ready"
    assert observed_timeouts == [0.01, 0.02]


def test_cli_supervision_gate_handles_keyboard_interrupt() -> None:
    def interrupt(timeout_seconds: float) -> str | None:
        del timeout_seconds
        raise KeyboardInterrupt

    gate = CliSupervisionGate(read_status=interrupt, write_prompt=lambda _: None)

    assert gate.wait_for_ready(object(), timeout_seconds=0.1) == "login_failed"


def test_cli_supervision_gate_handles_keyboard_interrupt_while_prompting() -> None:
    def interrupt(message: str) -> None:
        del message
        raise KeyboardInterrupt

    gate = CliSupervisionGate(read_status=lambda timeout: "ready", write_prompt=interrupt)

    assert gate.wait_for_ready(object(), timeout_seconds=0.1) == "login_failed"


class SequenceSupervisor:
    def __init__(self, *statuses: str) -> None:
        self.statuses = list(statuses)
        self.calls = 0

    def wait_for_ready(self, page: object, timeout_seconds: float) -> str:
        del page
        assert timeout_seconds > 0
        self.calls += 1
        return self.statuses.pop(0)


class SupervisedFakeSession:
    def __init__(self, video_payload: object) -> None:
        class FakePage:
            def wait_for_timeout(self, milliseconds: float) -> None:
                del milliseconds

        self.page = FakePage()
        self.video_payload = video_payload

    def __enter__(self) -> SupervisedFakeSession:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def open(self, url: str, adapter: object, consume: object) -> None:
        del url, adapter
        callback = cast(Callable[[str, object], None], consume)
        callback("https://api.bilibili.com/x/web-interface/view", self.video_payload)

    def raise_if_response_failed(self) -> None:
        pass


@pytest.mark.parametrize(
    "fatal_status", ["login_failed", "challenge_unresolved", "access_restricted"]
)
def test_supervision_fatal_status_stops_before_stratum_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fatal_status: str,
) -> None:
    payload = json.loads(
        (Path(__file__).parent / "fixtures/bilibili/video.json").read_text(encoding="utf-8")
    )
    actions: list[str] = []
    monkeypatch.setattr(runner, "BrowserSession", lambda: SupervisedFakeSession(payload))
    monkeypatch.setattr(
        runner,
        "perform_stratum_action",
        lambda page, stratum: actions.append(stratum) or "performed",
    )

    result = BrowserVideoCollector(supervisor=SequenceSupervisor(fatal_status))._browse(
        request(), tmp_path
    )

    assert result.status == fatal_status
    assert actions == []


def test_supervision_ready_allows_supported_stratum_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = json.loads(
        (Path(__file__).parent / "fixtures/bilibili/video.json").read_text(encoding="utf-8")
    )
    actions: list[str] = []
    monkeypatch.setattr(runner, "BrowserSession", lambda: SupervisedFakeSession(payload))
    monkeypatch.setattr(
        runner,
        "perform_stratum_action",
        lambda page, stratum: actions.append(stratum) or "performed",
    )

    result = BrowserVideoCollector(supervisor=SequenceSupervisor("ready"))._browse(
        request(), tmp_path
    )

    assert result.status == "partial"
    assert actions == ["top", "recent", "replies", "long_tail"]


def test_explicit_challenge_action_uses_one_handler_attempt_and_rechecks_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = json.loads(
        (Path(__file__).parent / "fixtures/bilibili/video.json").read_text(encoding="utf-8")
    )
    action = ClickAction((10, 20))
    attempts: list[tuple[Path, object, object]] = []

    class FakeChallengeHandler:
        def __init__(self, directory: Path, *, confirm: Callable[[], bool]) -> None:
            assert confirm() is True
            self.directory = directory

        def attempt(self, page: object, received_action: object) -> ChallengeResult:
            attempts.append((self.directory, page, received_action))
            return ChallengeResult("attempted")

    supervisor = SequenceSupervisor("ready", "ready")
    monkeypatch.setattr(runner, "BrowserSession", lambda: SupervisedFakeSession(payload))
    monkeypatch.setattr(runner, "ChallengeHandler", FakeChallengeHandler)
    monkeypatch.setattr(runner, "perform_stratum_action", lambda page, stratum: "unavailable")

    result = BrowserVideoCollector(
        supervisor=supervisor,
        challenge_action=action,
        challenge_id="challenge-1",
        challenge_confirm=lambda: True,
    )._browse(request(), tmp_path, run_id="../escape-run")

    assert result.status == "partial"
    assert supervisor.calls == 2
    assert len(attempts) == 1
    assert attempts[0][0].resolve().is_relative_to((tmp_path / "challenges").resolve())
    assert attempts[0][2] is action


def test_ready_does_not_authorize_a_challenge_mouse_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = json.loads(
        (Path(__file__).parent / "fixtures/bilibili/video.json").read_text(encoding="utf-8")
    )
    action = ClickAction((10, 20))
    mouse_actions: list[object] = []

    class FakeChallengeHandler:
        def __init__(self, directory: Path, *, confirm: Callable[[], bool]) -> None:
            del directory
            self.confirm = confirm

        def attempt(self, page: object, received_action: object) -> ChallengeResult:
            del page
            if not self.confirm():
                return ChallengeResult("not_confirmed")
            mouse_actions.append(received_action)
            return ChallengeResult("attempted")

    supervisor = SequenceSupervisor("ready")
    monkeypatch.setattr(runner, "BrowserSession", lambda: SupervisedFakeSession(payload))
    monkeypatch.setattr(runner, "ChallengeHandler", FakeChallengeHandler)
    monkeypatch.setattr(runner, "perform_stratum_action", lambda page, stratum: "unavailable")

    result = BrowserVideoCollector(
        supervisor=supervisor,
        challenge_action=action,
        challenge_confirm=lambda: False,
    )._browse(request(), tmp_path)

    assert result.status == "challenge_unresolved"
    assert supervisor.calls == 1
    assert mouse_actions == []


@pytest.mark.parametrize("challenge_id", ["", " ", "two words", "unsafe\u0085id"])
def test_challenge_id_rejects_empty_or_whitespace_values_before_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    challenge_id: str,
) -> None:
    payload = json.loads(
        (Path(__file__).parent / "fixtures/bilibili/video.json").read_text(encoding="utf-8")
    )
    attempts: list[object] = []

    class FakeChallengeHandler:
        def __init__(self, directory: Path, *, confirm: Callable[[], bool]) -> None:
            del directory, confirm
            attempts.append(object())

    monkeypatch.setattr(runner, "BrowserSession", lambda: SupervisedFakeSession(payload))
    monkeypatch.setattr(runner, "ChallengeHandler", FakeChallengeHandler)

    result = BrowserVideoCollector(
        supervisor=SequenceSupervisor("ready"),
        challenge_action=ClickAction((10, 20)),
        challenge_id=challenge_id,
        challenge_confirm=lambda: True,
    )._browse(request(), tmp_path)

    assert result.status == "challenge_unresolved"
    assert attempts == []


@pytest.mark.parametrize("link_kind", ["junction", "symlink"])
def test_challenge_attempt_rejects_redirected_challenges_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    link_kind: str,
) -> None:
    payload = json.loads(
        (Path(__file__).parent / "fixtures/bilibili/video.json").read_text(encoding="utf-8")
    )
    output_root = tmp_path / "output"
    outside = tmp_path / "outside" / "challenges"
    output_root.mkdir()
    create_directory_redirect(output_root / "challenges", outside, link_kind)
    monkeypatch.setattr(runner, "BrowserSession", lambda: SupervisedFakeSession(payload))
    monkeypatch.setattr(runner, "perform_stratum_action", lambda page, stratum: "unavailable")

    result = BrowserVideoCollector(
        supervisor=SequenceSupervisor("ready"),
        challenge_action=ClickAction((10, 20)),
        challenge_confirm=lambda: True,
    )._browse(request(), output_root, run_id="guarded-run")

    assert result.status == "challenge_unresolved"
    assert list(outside.iterdir()) == []


def test_successful_pilot_comments_remain_strict_jsonl(tmp_path: Path) -> None:
    run_pilot(request(), browser_result(), output_root=tmp_path)

    loaded = read_jsonl(tmp_path / "raw" / "bilibili" / "BVfake" / "comments.jsonl", RawComment)
    assert len(loaded) == 2
