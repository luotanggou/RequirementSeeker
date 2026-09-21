import shutil
from pathlib import Path

import pytest

from requirementseeker_dataset.contracts import (
    AdjudicationFile,
    AnnotationFile,
    ClusterLabel,
)
from requirementseeker_dataset.labels import (
    LabelExportResult,
    LabelValidationError,
    export_labels,
    validate_labels,
)
from requirementseeker_dataset.sanitize import sanitize_root

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "local-test-secret-at-least-32-bytes"


def _export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LabelExportResult:
    raw = tmp_path / "raw"
    shutil.copytree(FIXTURES / "raw" / "valid", raw)
    sanitized = tmp_path / "sanitized"
    monkeypatch.setenv("RS_DATASET_TEST_SECRET", SECRET)
    sanitize_root(
        raw,
        FIXTURES / "approved-manifest.json",
        sanitized,
        "RS_DATASET_TEST_SECRET",
    )
    return export_labels(sanitized, tmp_path / "labels")


def _completed(annotation: AnnotationFile, annotator: str) -> AnnotationFile:
    comments = [
        item.model_copy(
            update={
                "need_signal": "yes",
                "signal_kind": "need",
                "normalized_need": "支持离线处理",
                "noise_kind": "none",
                "video_reception": "positive",
            }
        )
        for item in annotation.comments
    ]
    return annotation.model_copy(
        update={"annotator_id": annotator, "comments": comments, "is_complete": True}
    )


def test_export_contains_text_but_no_semantic_prefill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _export(tmp_path, monkeypatch)

    assert all(item.text and item.need_signal == "unlabeled" for item in result.comments)
    assert result.clusters == []
    assert len(result.annotations) == len(result.output_files) == 1
    rendered = result.output_files[0].read_text(encoding="utf-8")
    assert "BVfake" not in rendered
    assert "comment-1" not in rendered


def test_duplicate_comments_and_cross_video_clusters_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    annotation = _export(tmp_path, monkeypatch).annotations[0]
    duplicate = annotation.model_copy(update={"comments": [annotation.comments[0]] * 2})
    foreign = f"comment_{'f' * 32}"
    cross_video = annotation.model_copy(
        update={
            "clusters": [
                ClusterLabel(
                    cluster_id="cluster-one",
                    normalized_need="离线处理",
                    comment_ids=[foreign],
                )
            ]
        }
    )

    with pytest.raises(LabelValidationError, match="duplicate_comment"):
        validate_labels(duplicate)
    with pytest.raises(LabelValidationError, match="cross_video_cluster_member"):
        validate_labels(cross_video)


def test_duplicate_cluster_membership_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    annotation = _export(tmp_path, monkeypatch).annotations[0]
    comment_id = annotation.comments[0].comment_id
    clusters = [
        ClusterLabel(cluster_id="cluster-one", normalized_need="离线", comment_ids=[comment_id]),
        ClusterLabel(cluster_id="cluster-two", normalized_need="本地", comment_ids=[comment_id]),
    ]

    with pytest.raises(LabelValidationError, match="duplicate_cluster_membership"):
        validate_labels(annotation.model_copy(update={"clusters": clusters}))


def test_claimed_complete_annotation_cannot_keep_unlabeled_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    annotation = (
        _export(tmp_path, monkeypatch)
        .annotations[0]
        .model_copy(update={"annotator_id": "annotator-one", "is_complete": True})
    )

    with pytest.raises(LabelValidationError, match="incomplete_required_fields"):
        validate_labels(annotation)


def test_dispute_requires_two_independent_annotations_and_adjudication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blank = _export(tmp_path, monkeypatch).annotations[0]
    first = _completed(blank, "annotator-one").model_copy(
        update={"disputed": True, "dispute_reasons": ["need_signal_disagreement"]}
    )
    second = _completed(blank, "annotator-two")

    with pytest.raises(LabelValidationError, match="dispute_not_adjudicated"):
        validate_labels(first)

    decision = _completed(blank, "adjudicator-one")
    adjudication = AdjudicationFile(
        adjudication_schema_version="1.0",
        video_id=blank.video_id,
        annotator_ids=["annotator-one", "annotator-two"],
        adjudicator_id="adjudicator-one",
        decision=decision,
    )

    assert validate_labels(first, second=second, adjudication=adjudication).evaluation_eligible


def test_raw_identifier_like_metadata_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    annotation = _export(tmp_path, monkeypatch).annotations[0]
    cluster = ClusterLabel(
        cluster_id="raw-comment-123",
        normalized_need="离线处理",
        comment_ids=[annotation.comments[0].comment_id],
    )

    with pytest.raises(LabelValidationError, match="raw_identifier_pattern"):
        validate_labels(annotation.model_copy(update={"clusters": [cluster]}))


def test_label_output_must_not_replace_sanitized_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _export(tmp_path, monkeypatch)
    sanitized = tmp_path / "sanitized"

    with pytest.raises(LabelValidationError, match="label_output_must_be_separate"):
        export_labels(sanitized, sanitized)
    assert result.output_files[0].is_file()
