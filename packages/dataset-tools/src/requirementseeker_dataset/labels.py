"""导出不含语义预填的人工标注材料，并验证独立标注与裁决。"""

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from .contracts import (
    AdjudicationFile,
    AnnotationFile,
    ClusterLabel,
    CommentLabel,
    SamplingManifest,
    SanitizedComment,
)

_RAW_IDENTIFIER = re.compile(r"(?i)^(?:raw[-_]|BV[0-9A-Za-z]|\d{15,})")


class LabelValidationError(ValueError):
    """标注材料无效时只返回固定错误码。"""


@dataclass(frozen=True, slots=True)
class LabelExportResult:
    annotations: tuple[AnnotationFile, ...]
    output_files: tuple[Path, ...]

    @property
    def comments(self) -> list[CommentLabel]:
        """提供跨视频的只读式汇总，便于检查模板没有语义预填。"""

        return [item for annotation in self.annotations for item in annotation.comments]

    @property
    def clusters(self) -> list[ClusterLabel]:
        return [item for annotation in self.annotations for item in annotation.clusters]


@dataclass(frozen=True, slots=True)
class LabelValidationResult:
    evaluation_eligible: bool


@dataclass(frozen=True, slots=True)
class LabelRootValidationResult:
    annotation_count: int
    evaluation_eligible_count: int


def _read_sampling_manifest(path: Path) -> SamplingManifest:
    try:
        return SamplingManifest.model_validate_json(path.read_bytes())
    except (OSError, ValidationError, ValueError):
        raise LabelValidationError("sampling_manifest_invalid") from None


def _read_comments(path: Path) -> list[SanitizedComment]:
    try:
        lines = path.read_text(encoding="utf-8").split("\n")
    except (OSError, UnicodeError):
        raise LabelValidationError("sanitized_comments_invalid") from None
    if lines and not lines[-1]:
        lines.pop()
    comments: list[SanitizedComment] = []
    for line in lines:
        if not line.strip():
            raise LabelValidationError("sanitized_comments_invalid")
        try:
            comments.append(SanitizedComment.model_validate_json(line))
        except (ValidationError, ValueError):
            raise LabelValidationError("sanitized_comments_invalid") from None
    return comments


def _write_annotation(path: Path, annotation: AnnotationFile) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            annotation.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _commit_label_root(staging: Path, output: Path, backup: Path) -> None:
    moved_old = False
    try:
        if output.exists():
            output.replace(backup)
            moved_old = True
        staging.replace(output)
    except OSError:
        if moved_old and backup.exists() and not output.exists():
            try:
                backup.replace(output)
            except OSError:
                raise LabelValidationError("label_output_recovery_failed") from None
        raise LabelValidationError("label_output_commit_failed") from None
    if moved_old:
        try:
            shutil.rmtree(backup)
        except OSError:
            raise LabelValidationError("label_backup_cleanup_failed") from None


def export_labels(sanitized_root: Path, output_root: Path) -> LabelExportResult:
    """为每个有效采样清单导出含脱敏正文的空白人工标注文件。"""

    try:
        sanitized_resolved = sanitized_root.resolve(strict=True)
        output_resolved = output_root.resolve(strict=False)
    except OSError:
        raise LabelValidationError("label_path_invalid") from None
    if (
        sanitized_resolved == output_resolved
        or sanitized_resolved in output_resolved.parents
        or output_resolved in sanitized_resolved.parents
    ):
        raise LabelValidationError("label_output_must_be_separate")

    staging = output_root.with_name(f".{output_root.name}.staging")
    backup = output_root.with_name(f".{output_root.name}.backup")
    if (
        staging.exists()
        or staging.is_symlink()
        or backup.exists()
        or backup.is_symlink()
        or output_root.is_symlink()
    ):
        raise LabelValidationError("label_output_path_invalid")

    manifests = sorted(sanitized_root.rglob("sampling-manifest.json"))
    if not manifests:
        raise LabelValidationError("sampling_manifest_missing")
    annotations: list[AnnotationFile] = []
    relative_outputs: list[Path] = []
    try:
        staging.mkdir(parents=True)
        for manifest_path in manifests:
            manifest = _read_sampling_manifest(manifest_path)
            directory = manifest_path.parent
            if directory.name != manifest.video_id:
                raise LabelValidationError("sampling_manifest_directory_mismatch")
            comments = _read_comments(directory / "comments.jsonl")
            comment_ids = [item.comment_id for item in comments]
            if len(comment_ids) != len(set(comment_ids)):
                raise LabelValidationError("duplicate_comment")
            if comment_ids != manifest.candidate_comment_ids:
                raise LabelValidationError("sampling_manifest_comment_mismatch")
            annotation = AnnotationFile(
                annotation_schema_version="1.0",
                video_id=manifest.video_id,
                annotator_id=None,
                comments=[CommentLabel.unlabeled(item.comment_id, item.text) for item in comments],
                clusters=[],
                disputed=False,
                dispute_reasons=[],
                is_complete=False,
            )
            relative = Path(manifest.platform) / manifest.video_id / "annotation.json"
            _write_annotation(staging / relative, annotation)
            annotations.append(annotation)
            relative_outputs.append(relative)
        _commit_label_root(staging, output_root, backup)
    except Exception:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging, ignore_errors=True)
        raise

    return LabelExportResult(
        annotations=tuple(annotations),
        output_files=tuple(output_root / relative for relative in relative_outputs),
    )


def _reject_raw_metadata(annotation: AnnotationFile) -> None:
    identifiers = [
        annotation.annotator_id,
        *(cluster.cluster_id for cluster in annotation.clusters),
    ]
    if any(value is not None and _RAW_IDENTIFIER.match(value) for value in identifiers):
        raise LabelValidationError("raw_identifier_pattern")


def _validate_structure(annotation: AnnotationFile) -> None:
    _reject_raw_metadata(annotation)
    comment_ids = [item.comment_id for item in annotation.comments]
    if len(comment_ids) != len(set(comment_ids)):
        raise LabelValidationError("duplicate_comment")
    known = set(comment_ids)
    memberships: set[str] = set()
    for cluster in annotation.clusters:
        if len(cluster.comment_ids) != len(set(cluster.comment_ids)):
            raise LabelValidationError("duplicate_cluster_member")
        if any(comment_id not in known for comment_id in cluster.comment_ids):
            raise LabelValidationError("cross_video_cluster_member")
        if memberships.intersection(cluster.comment_ids):
            raise LabelValidationError("duplicate_cluster_membership")
        memberships.update(cluster.comment_ids)
    if annotation.disputed != bool(annotation.dispute_reasons):
        raise LabelValidationError("dispute_reason_state_mismatch")


def _fields_complete(annotation: AnnotationFile) -> bool:
    if annotation.annotator_id is None:
        return False
    for item in annotation.comments:
        if (
            item.need_signal == "unlabeled"
            or item.signal_kind == "unlabeled"
            or item.noise_kind == "unlabeled"
            or item.video_reception == "unlabeled"
        ):
            return False
        if item.need_signal == "yes" and not item.normalized_need:
            return False
    return True


def _require_complete(annotation: AnnotationFile) -> None:
    if not annotation.is_complete or not _fields_complete(annotation):
        raise LabelValidationError("incomplete_required_fields")


def validate_labels(
    annotation: AnnotationFile,
    *,
    second: AnnotationFile | None = None,
    adjudication: AdjudicationFile | None = None,
) -> LabelValidationResult:
    """验证单份标注；争议样本必须带独立第二份标注和最终裁决。"""

    _validate_structure(annotation)
    if annotation.is_complete and not _fields_complete(annotation):
        raise LabelValidationError("incomplete_required_fields")
    if not annotation.disputed:
        return LabelValidationResult(evaluation_eligible=annotation.is_complete)
    if second is None or adjudication is None or adjudication.decision is None:
        raise LabelValidationError("dispute_not_adjudicated")

    _validate_structure(second)
    _validate_structure(adjudication.decision)
    _require_complete(annotation)
    _require_complete(second)
    _require_complete(adjudication.decision)
    first_author = annotation.annotator_id
    second_author = second.annotator_id
    if first_author == second_author:
        raise LabelValidationError("independent_annotators_required")
    if second.video_id != annotation.video_id or adjudication.video_id != annotation.video_id:
        raise LabelValidationError("adjudication_video_mismatch")
    if [(item.comment_id, item.text) for item in second.comments] != [
        (item.comment_id, item.text) for item in annotation.comments
    ]:
        raise LabelValidationError("independent_annotation_source_mismatch")
    if set(adjudication.annotator_ids) != {first_author, second_author}:
        raise LabelValidationError("adjudication_annotators_mismatch")
    if (
        adjudication.decision.video_id != annotation.video_id
        or adjudication.decision.disputed
        or adjudication.decision.annotator_id != adjudication.adjudicator_id
    ):
        raise LabelValidationError("adjudication_decision_invalid")
    if [(item.comment_id, item.text) for item in adjudication.decision.comments] != [
        (item.comment_id, item.text) for item in annotation.comments
    ]:
        raise LabelValidationError("adjudication_decision_source_mismatch")
    if _RAW_IDENTIFIER.match(adjudication.adjudicator_id):
        raise LabelValidationError("raw_identifier_pattern")
    return LabelValidationResult(evaluation_eligible=True)


def _read_annotation(path: Path) -> AnnotationFile:
    try:
        return AnnotationFile.model_validate_json(path.read_bytes())
    except (OSError, ValidationError, ValueError):
        raise LabelValidationError("annotation_file_invalid") from None


def _read_adjudication(path: Path) -> AdjudicationFile:
    try:
        return AdjudicationFile.model_validate_json(path.read_bytes())
    except (OSError, ValidationError, ValueError):
        raise LabelValidationError("adjudication_file_invalid") from None


def validate_label_root(label_root: Path) -> LabelRootValidationResult:
    """验证目录中的主标注，以及同目录下可选的第二标注和裁决文件。"""

    annotation_paths = sorted(label_root.rglob("annotation.json"))
    if not annotation_paths:
        raise LabelValidationError("annotation_files_missing")
    seen_videos: set[str] = set()
    eligible = 0
    for path in annotation_paths:
        annotation = _read_annotation(path)
        if annotation.video_id in seen_videos:
            raise LabelValidationError("duplicate_annotation_video")
        seen_videos.add(annotation.video_id)
        second_path = path.with_name("annotation-secondary.json")
        adjudication_path = path.with_name("adjudication.json")
        second = _read_annotation(second_path) if second_path.is_file() else None
        adjudication = (
            _read_adjudication(adjudication_path) if adjudication_path.is_file() else None
        )
        if not annotation.disputed and (second is not None or adjudication is not None):
            raise LabelValidationError("unexpected_dispute_files")
        result = validate_labels(annotation, second=second, adjudication=adjudication)
        eligible += result.evaluation_eligible
    return LabelRootValidationResult(
        annotation_count=len(annotation_paths),
        evaluation_eligible_count=eligible,
    )
