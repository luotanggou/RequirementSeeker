from __future__ import annotations

import json
import shutil
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import cast

import pytest

import requirementseeker_collector.runner as runner
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
    [".", "..", "../escape", r"folder\escape", "C:escape", "name.", "CON", "com1.txt"],
)
def test_pilot_rejects_unsafe_video_key_without_writing(tmp_path: Path, video_key: str) -> None:
    result = run_pilot(
        request(video_key=video_key),
        browser_result(video_key=video_key),
        output_root=tmp_path,
    )

    assert result.status == "invalid_video_key"
    assert list(tmp_path.iterdir()) == []


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


def test_pilot_recovers_unique_valid_backup_before_merging_current_comments(
    tmp_path: Path,
) -> None:
    first = run_pilot(request(), browser_result(), output_root=tmp_path)
    assert first.status == "success"
    target = tmp_path / "raw" / "bilibili" / "BVfake"
    backup = tmp_path / ".backup" / "interrupted" / "bilibili" / "BVfake"
    backup.parent.mkdir(parents=True)
    target.replace(backup)

    result = run_pilot(request(), browser_result(comments=[comment("c3")]), output_root=tmp_path)

    assert result.status == "partial"
    _, comments, collection = validate_generation(target)
    assert [item.raw_comment_id for item in comments] == ["c1", "c2", "c3"]
    assert any(
        error.category == "interrupted_commit_recovered" for error in collection.collection_errors
    )
    assert not backup.exists()


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


def test_pilot_fails_closed_when_multiple_backup_candidates_exist(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    run_pilot(request(), browser_result(), output_root=source_root)
    source = source_root / "raw" / "bilibili" / "BVfake"
    for run_id in ("one", "two"):
        candidate = tmp_path / ".backup" / run_id / "bilibili" / "BVfake"
        candidate.parent.mkdir(parents=True)
        shutil.copytree(source, candidate)
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
    if candidate_kind == "symlink":
        original_is_symlink = Path.is_symlink

        def fake_is_symlink(path: Path) -> bool:
            return path == candidate or original_is_symlink(path)

        monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)

    result = run_pilot(request(), browser_result(), output_root=tmp_path)

    assert result.status == "backup_recovery_failed"
    assert not (tmp_path / "raw" / "bilibili" / "BVfake").exists()


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


def test_successful_pilot_comments_remain_strict_jsonl(tmp_path: Path) -> None:
    run_pilot(request(), browser_result(), output_root=tmp_path)

    loaded = read_jsonl(tmp_path / "raw" / "bilibili" / "BVfake" / "comments.jsonl", RawComment)
    assert len(loaded) == 2
