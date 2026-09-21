from copy import deepcopy

import pytest
from pydantic import ValidationError

from requirementseeker_dataset.contracts import (
    AdjudicationFile,
    AnnotationFile,
    ClusterLabel,
    CommentLabel,
    DatasetSplit,
    ReviewItem,
    SamplingManifest,
    SanitizationReport,
    SanitizedComment,
    SanitizedVideo,
)

VIDEO_ID = f"video_{'a' * 32}"
AUTHOR_ID = f"author_{'b' * 32}"
COMMENT_ID = f"comment_{'c' * 32}"
SECOND_COMMENT_ID = f"comment_{'d' * 32}"


def report_data() -> dict[str, object]:
    return {
        "sanitization_schema_version": "1.0",
        "video_id": VIDEO_ID,
        "rules_version": "pii-v1",
        "input_sha256": "0" * 64,
        "output_sha256": "1" * 64,
        "replacement_counts": {"phone": 1},
        "review_item_count": 0,
        "review_items": [],
        "started_at": "2026-09-21T08:00:00+08:00",
        "finished_at": "2026-09-21T08:01:00+08:00",
        "excluded": False,
        "exclusion_reason": None,
    }


def manifest_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "sampling_schema_version": "1.0",
        "manifest_id": "manifest-1",
        "platform": "bilibili",
        "video_id": VIDEO_ID,
        "captured_at": "2026-09-21T08:00:00+08:00",
        "reported_total": 120,
        "collection_target": 100,
        "collected_total": 96,
        "pages_requested": 4,
        "pages_succeeded": 4,
        "available_strata": ["top", "recent"],
        "direction": "software_tool",
        "author_id_present": 90,
        "distinct_author_count": 70,
        "exact_duplicate_count": 1,
        "normalized_duplicate_count": 2,
        "video_metrics": {"views": 5000, "likes": 250},
        "candidate_comment_ids": [COMMENT_ID, SECOND_COMMENT_ID],
        "stratum_comment_ids": {
            "top": [COMMENT_ID],
            "recent": [SECOND_COMMENT_ID],
        },
    }
    data.update(overrides)
    return data


def test_sanitization_report_rejects_raw_identifiers() -> None:
    data = report_data()
    data["raw_video_id"] = "raw-1"

    with pytest.raises(ValidationError, match="extra_forbidden"):
        SanitizationReport.model_validate(data)


def test_sanitization_report_review_count_matches_safe_items() -> None:
    data = report_data()
    data["review_item_count"] = 1

    with pytest.raises(ValidationError, match="review_item_count_mismatch"):
        SanitizationReport.model_validate(data)


def test_label_template_uses_explicit_unlabeled_values() -> None:
    item = CommentLabel.unlabeled(COMMENT_ID)

    assert item.need_signal == "unlabeled"
    assert item.signal_kind == "unlabeled"
    assert item.normalized_need is None
    assert item.noise_kind == "unlabeled"
    assert item.video_reception == "unlabeled"


def test_sanitized_records_accept_only_pseudonymous_ids() -> None:
    video = SanitizedVideo(
        platform="bilibili",
        video_id=VIDEO_ID,
        author_id=AUTHOR_ID,
        title="已脱敏标题",
        description="已脱敏描述",
        published_at=None,
        duration_seconds=30,
        total_comment_count=1,
        view_count=100,
        like_count=10,
        favorite_count=None,
        share_count=None,
        author_follower_count=None,
        captured_at="2026-09-21T08:00:00+08:00",
    )
    comment = SanitizedComment(
        comment_id=COMMENT_ID,
        author_id=AUTHOR_ID,
        parent_comment_id=None,
        text="需要离线功能",
        published_at=None,
        collected_at="2026-09-21T08:00:00+08:00",
        like_count=2,
        reply_count=0,
        is_video_author=False,
        source_stratum="top",
        source_page_or_rank=1,
    )

    assert video.video_id == VIDEO_ID
    assert comment.comment_id == COMMENT_ID
    with pytest.raises(ValidationError):
        SanitizedComment.model_validate({**comment.model_dump(), "comment_id": "raw-comment"})


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"pages_succeeded": 5}, "successful_pages_exceed_requested_pages"),
        ({"author_id_present": 97}, "author_count_exceeds_collected_total"),
        ({"distinct_author_count": 91}, "distinct_author_count_exceeds_known_authors"),
        ({"exact_duplicate_count": 97}, "duplicate_count_exceeds_collected_total"),
        (
            {"candidate_comment_ids": [COMMENT_ID, COMMENT_ID]},
            "candidate_comment_ids_must_be_unique",
        ),
        (
            {
                "collected_total": 1,
                "author_id_present": 1,
                "distinct_author_count": 1,
                "exact_duplicate_count": 0,
                "normalized_duplicate_count": 0,
            },
            "candidate_pool_exceeds_collected_total",
        ),
    ],
)
def test_sampling_manifest_rejects_impossible_counts_or_duplicates(
    override: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        SamplingManifest.model_validate(manifest_data(**override))


def test_sampling_manifest_rejects_invalid_stratum_references() -> None:
    unknown = deepcopy(manifest_data())
    unknown["stratum_comment_ids"] = {
        "top": [COMMENT_ID],
        "recent": [f"comment_{'e' * 32}"],
    }
    with pytest.raises(ValidationError, match="stratum_comment_id_not_in_candidate_pool"):
        SamplingManifest.model_validate(unknown)

    missing_key = deepcopy(manifest_data())
    missing_key["stratum_comment_ids"] = {"top": [COMMENT_ID]}
    with pytest.raises(ValidationError, match="stratum_keys_must_match_available_strata"):
        SamplingManifest.model_validate(missing_key)

    duplicate = deepcopy(manifest_data())
    duplicate["stratum_comment_ids"] = {
        "top": [COMMENT_ID, COMMENT_ID],
        "recent": [SECOND_COMMENT_ID],
    }
    with pytest.raises(ValidationError, match="stratum_comment_ids_must_be_unique"):
        SamplingManifest.model_validate(duplicate)


def test_dataset_split_rejects_repeated_or_cross_split_videos() -> None:
    with pytest.raises(ValidationError, match="dataset_split_video_ids_must_be_unique"):
        DatasetSplit(
            development=[VIDEO_ID, VIDEO_ID],
            calibration=[],
            holdout=[],
        )

    with pytest.raises(ValidationError, match="dataset_split_video_ids_must_be_disjoint"):
        DatasetSplit(
            development=[VIDEO_ID],
            calibration=[VIDEO_ID],
            holdout=[],
        )


def test_review_annotation_and_adjudication_contracts_are_explicit() -> None:
    review = ReviewItem(
        video_id=VIDEO_ID,
        comment_id=COMMENT_ID,
        field="text",
        reason="possible_precise_address",
    )
    label = CommentLabel.unlabeled(COMMENT_ID)
    cluster = ClusterLabel(
        cluster_id="cluster-1",
        normalized_need="离线处理评论",
        comment_ids=[COMMENT_ID],
    )
    annotation = AnnotationFile(
        annotation_schema_version="1.0",
        video_id=VIDEO_ID,
        annotator_id=None,
        comments=[label],
        clusters=[cluster],
        disputed=False,
        dispute_reasons=[],
        is_complete=False,
    )
    adjudication = AdjudicationFile(
        adjudication_schema_version="1.0",
        video_id=VIDEO_ID,
        annotator_ids=["annotator-a", "annotator-b"],
        adjudicator_id="adjudicator-a",
        decision=annotation,
    )

    assert review.reason == "possible_precise_address"
    assert adjudication.decision == annotation


def test_contracts_reject_unknown_fields_and_naive_timestamps() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        SanitizationReport.model_validate({**report_data(), "cookie": "secret"})
    with pytest.raises(ValidationError, match="timestamp_must_be_rfc3339_with_timezone"):
        SanitizationReport.model_validate({**report_data(), "started_at": "2026-09-21T08:00:00"})
