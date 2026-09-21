import json
import os
import shutil
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest

from requirementseeker_dataset.contracts import SamplingManifest, SanitizationReport
from requirementseeker_dataset.sanitize import SanitizationError, sanitize_root

FIXTURES = Path(__file__).parent / "fixtures"
RAW_FIXTURE = FIXTURES / "raw" / "valid"
PLAN_FIXTURE = FIXTURES / "approved-manifest.json"
SECRET = "local-test-secret-at-least-32-bytes"


def _prepare_raw(tmp_path: Path) -> Path:
    raw = tmp_path / "raw"
    shutil.copytree(RAW_FIXTURE, raw)
    return raw


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _tree_digest(root: Path) -> str:
    digest = sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_sanitize_writes_no_raw_ids_or_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _prepare_raw(tmp_path)
    output = tmp_path / "sanitized"
    before = _tree_digest(raw)
    monkeypatch.setenv("RS_DATASET_TEST_SECRET", SECRET)

    result = sanitize_root(raw, PLAN_FIXTURE, output, "RS_DATASET_TEST_SECRET")
    rendered = "".join(path.read_text("utf-8") for path in result.output_files)

    assert "BVfake" not in rendered
    assert "video-author" not in rendered
    assert "author-1" not in rendered
    assert "comment-1" not in rendered
    assert SECRET not in rendered
    assert _tree_digest(raw) == before


def test_sanitize_emits_m2_sampling_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _prepare_raw(tmp_path)
    monkeypatch.setenv("RS_DATASET_TEST_SECRET", SECRET)

    result = sanitize_root(raw, PLAN_FIXTURE, tmp_path / "sanitized", "RS_DATASET_TEST_SECRET")
    manifest = SamplingManifest.model_validate_json(result.sampling_manifests[0].read_bytes())

    assert manifest.direction == "software_tool"
    assert manifest.collection_target == 2
    assert manifest.collected_total == 2
    assert manifest.video_id.startswith("video_")
    assert manifest.available_strata == {"top", "replies"}
    assert all(
        item.startswith("comment_")
        for values in manifest.stratum_comment_ids.values()
        for item in values
    )


def test_only_approved_directories_are_processed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _prepare_raw(tmp_path)
    shutil.copytree(raw / "bilibili" / "BVfake", raw / "bilibili" / "pilot-extra")
    pilot_video = _read_json(raw / "bilibili" / "pilot-extra" / "video.json")
    pilot_video["raw_video_id"] = "pilot-extra"
    (raw / "bilibili" / "pilot-extra" / "video.json").write_text(
        json.dumps(pilot_video, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setenv("RS_DATASET_TEST_SECRET", SECRET)

    result = sanitize_root(raw, PLAN_FIXTURE, tmp_path / "sanitized", "RS_DATASET_TEST_SECRET")
    rendered = "".join(path.read_text("utf-8") for path in result.output_files)

    assert result.excluded_raw_directory_count == 1
    assert "pilot-extra" not in rendered
    assert len(result.sampling_manifests) == 1


def test_zero_successful_pages_is_excluded_and_reported_for_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _prepare_raw(tmp_path)
    collection_path = raw / "bilibili" / "BVfake" / "collection.json"
    collection = _read_json(collection_path)
    collection["pages_succeeded"] = 0
    collection_path.write_text(json.dumps(collection, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("RS_DATASET_TEST_SECRET", SECRET)

    result = sanitize_root(raw, PLAN_FIXTURE, tmp_path / "sanitized", "RS_DATASET_TEST_SECRET")
    report = SanitizationReport.model_validate_json(result.sanitization_reports[0].read_bytes())
    replacements = _read_json(result.replacement_candidates)

    assert result.sampling_manifests == ()
    assert report.excluded is True
    assert report.exclusion_reason == "zero_successful_pages"
    assert replacements["count"] == 1
    assert replacements["video_ids"] == [report.video_id]


def test_missing_approved_input_preserves_existing_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    output = tmp_path / "sanitized"
    output.mkdir()
    marker = output / "previous.txt"
    marker.write_text("keep", encoding="utf-8")
    monkeypatch.setenv("RS_DATASET_TEST_SECRET", SECRET)

    with pytest.raises(SanitizationError, match="approved_raw_directory_missing"):
        sanitize_root(raw, PLAN_FIXTURE, output, "RS_DATASET_TEST_SECRET")

    assert marker.read_text(encoding="utf-8") == "keep"
    assert not output.with_name(".sanitized.staging").exists()
    assert not output.with_name(".sanitized.backup").exists()


def test_failed_publish_restores_previous_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _prepare_raw(tmp_path)
    output = tmp_path / "sanitized"
    output.mkdir()
    marker = output / "previous.txt"
    marker.write_text("keep", encoding="utf-8")
    staging = output.with_name(".sanitized.staging")
    original_replace = Path.replace

    def fail_staging_publish(path: Path, target: Path) -> Path:
        if path == staging:
            raise OSError("simulated publish failure")
        return original_replace(path, target)

    monkeypatch.setenv("RS_DATASET_TEST_SECRET", SECRET)
    monkeypatch.setattr(Path, "replace", fail_staging_publish)

    with pytest.raises(SanitizationError, match="output_commit_failed"):
        sanitize_root(raw, PLAN_FIXTURE, output, "RS_DATASET_TEST_SECRET")

    assert marker.read_text(encoding="utf-8") == "keep"
    assert not staging.exists()
    assert not output.with_name(".sanitized.backup").exists()


def test_output_cannot_contain_or_replace_raw_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _prepare_raw(tmp_path / "output-parent")
    monkeypatch.setenv("RS_DATASET_TEST_SECRET", SECRET)

    with pytest.raises(SanitizationError, match="output_must_be_outside_raw_root"):
        sanitize_root(raw, PLAN_FIXTURE, raw / "sanitized", "RS_DATASET_TEST_SECRET")
    with pytest.raises(SanitizationError, match="output_must_be_outside_raw_root"):
        sanitize_root(raw, PLAN_FIXTURE, raw.parent, "RS_DATASET_TEST_SECRET")


def test_text_redaction_and_duplicate_statistics_are_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _prepare_raw(tmp_path)
    directory = raw / "bilibili" / "BVfake"
    comments = [
        json.loads(line)
        for line in (directory / "comments.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    comments[0]["text"] = "联系 test@example.com 后到中山路12号"
    comments[1]["text"] = "  联系 test@example.com 后到中山路12号  "
    (directory / "comments.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in comments),
        encoding="utf-8",
    )
    monkeypatch.setenv("RS_DATASET_TEST_SECRET", SECRET)

    result = sanitize_root(raw, PLAN_FIXTURE, tmp_path / "sanitized", "RS_DATASET_TEST_SECRET")
    manifest = SamplingManifest.model_validate_json(result.sampling_manifests[0].read_bytes())
    report = SanitizationReport.model_validate_json(result.sanitization_reports[0].read_bytes())
    rendered = "".join(path.read_text("utf-8") for path in result.output_files)

    assert "test@example.com" not in rendered
    assert manifest.normalized_duplicate_count == 1
    assert report.replacement_counts["email"] == 2
    assert report.review_item_count == 2
    assert {item.reason for item in report.review_items} == {"possible_precise_address"}


def test_sampling_manifest_is_stable_across_hash_seeds(tmp_path: Path) -> None:
    raw = _prepare_raw(tmp_path)
    outputs = [tmp_path / "seed-one", tmp_path / "seed-two"]
    for seed, output in zip(("1", "2"), outputs, strict=True):
        environment = {
            **os.environ,
            "PYTHONHASHSEED": seed,
            "RS_DATASET_TEST_SECRET": SECRET,
        }
        subprocess.run(
            [
                sys.executable,
                "-m",
                "requirementseeker_dataset.cli",
                "sanitize",
                "--raw",
                str(raw),
                "--plan",
                str(PLAN_FIXTURE),
                "--output",
                str(output),
                "--secret-env",
                "RS_DATASET_TEST_SECRET",
            ],
            check=True,
            capture_output=True,
            env=environment,
            text=True,
        )

    manifests = [next(output.rglob("sampling-manifest.json")).read_bytes() for output in outputs]
    assert manifests[0] == manifests[1]
