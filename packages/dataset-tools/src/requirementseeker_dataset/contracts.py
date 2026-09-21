"""脱敏数据、采样清单和人工标注使用的严格文件契约。"""

import re
from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    model_validator,
)

Platform = Literal["bilibili", "douyin"]
SamplingStratum = Literal["top", "recent", "replies", "long_tail"]
VideoDirection = Literal[
    "software_tool",
    "tutorial_workflow",
    "life_service",
    "ecommerce_marketing",
    "entertainment_culture",
    "unknown",
]
ReviewField = Literal["title", "description", "text"]
NeedSignal = Literal["yes", "no", "uncertain", "unlabeled"]
SignalKind = Literal[
    "pain",
    "need",
    "alternative",
    "product_defect",
    "not_applicable",
    "unlabeled",
]
NoiseKind = Literal[
    "none",
    "praise",
    "joke",
    "advertising",
    "meaningless",
    "unclear",
    "other",
    "unlabeled",
]
VideoReception = Literal["positive", "negative", "mixed", "none", "unclear", "unlabeled"]

Identifier = Annotated[str, Field(strict=True, min_length=1, max_length=256, pattern=r"^\S+$")]
Text = Annotated[str, Field(strict=True, min_length=1, max_length=32000, pattern=r"\S")]
Hash = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
PseudonymousVideoId = Annotated[str, Field(strict=True, pattern=r"^video_[0-9a-f]{32}$")]
PseudonymousAuthorId = Annotated[str, Field(strict=True, pattern=r"^author_[0-9a-f]{32}$")]
PseudonymousCommentId = Annotated[str, Field(strict=True, pattern=r"^comment_[0-9a-f]{32}$")]


class Contract(BaseModel):
    """拒绝未知字段，并在赋值时继续执行契约校验。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def _timestamp(value: object) -> object:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        raise ValueError("timestamp_must_be_rfc3339_with_timezone")
    return value


def _utc(value: datetime) -> datetime:
    try:
        return value.astimezone(UTC)
    except OverflowError:
        raise ValueError("timestamp_out_of_supported_range") from None


Timestamp = Annotated[AwareDatetime, BeforeValidator(_timestamp), AfterValidator(_utc)]


class SanitizedVideo(Contract):
    """保留原始视频语义字段，但只接受稳定伪 ID。"""

    platform: Platform
    video_id: PseudonymousVideoId
    author_id: PseudonymousAuthorId
    title: str
    description: str
    published_at: Timestamp | None
    duration_seconds: NonNegativeInt | None
    total_comment_count: NonNegativeInt | None
    view_count: NonNegativeInt | None
    like_count: NonNegativeInt | None
    favorite_count: NonNegativeInt | None
    share_count: NonNegativeInt | None
    author_follower_count: NonNegativeInt | None
    captured_at: Timestamp


class SanitizedComment(Contract):
    """与原始评论字段一一对应，不保留任何原始标识符。"""

    comment_id: PseudonymousCommentId
    author_id: PseudonymousAuthorId | None
    parent_comment_id: PseudonymousCommentId | None
    text: Text
    published_at: Timestamp | None
    collected_at: Timestamp
    like_count: NonNegativeInt | None
    reply_count: NonNegativeInt | None
    is_video_author: StrictBool | None
    source_stratum: SamplingStratum
    source_page_or_rank: PositiveInt


class VideoMetrics(Contract):
    """M2 使用的辅助视频指标，不能替代评论数据质量门。"""

    views: NonNegativeInt | None = None
    likes: NonNegativeInt | None = None
    favorites: NonNegativeInt | None = None
    shares: NonNegativeInt | None = None
    author_followers: NonNegativeInt | None = None
    production_observation: Text | None = None


class SamplingManifest(Contract):
    """独立复刻 M2 1.0 采样清单，避免数据工具依赖 Agent。"""

    sampling_schema_version: Literal["1.0"]
    manifest_id: Identifier
    platform: Platform
    video_id: PseudonymousVideoId
    captured_at: Timestamp
    reported_total: NonNegativeInt | None
    collection_target: PositiveInt
    collected_total: NonNegativeInt
    pages_requested: PositiveInt
    pages_succeeded: NonNegativeInt
    available_strata: set[SamplingStratum] = Field(min_length=1, max_length=4)
    direction: VideoDirection
    author_id_present: NonNegativeInt
    distinct_author_count: NonNegativeInt
    exact_duplicate_count: NonNegativeInt
    normalized_duplicate_count: NonNegativeInt
    video_metrics: VideoMetrics | None
    candidate_comment_ids: list[PseudonymousCommentId]
    stratum_comment_ids: dict[SamplingStratum, list[PseudonymousCommentId]]

    @model_validator(mode="after")
    def integrity(self) -> Self:
        """拒绝不可能的计数和无法追溯到候选池的分层引用。"""

        if self.pages_succeeded > self.pages_requested:
            raise ValueError("successful_pages_exceed_requested_pages")
        if self.author_id_present > self.collected_total:
            raise ValueError("author_count_exceeds_collected_total")
        if self.distinct_author_count > self.author_id_present:
            raise ValueError("distinct_author_count_exceeds_known_authors")
        duplicate_count = self.exact_duplicate_count + self.normalized_duplicate_count
        if duplicate_count > self.collected_total:
            raise ValueError("duplicate_count_exceeds_collected_total")
        if len(self.candidate_comment_ids) != len(set(self.candidate_comment_ids)):
            raise ValueError("candidate_comment_ids_must_be_unique")
        if len(self.candidate_comment_ids) > self.collected_total:
            raise ValueError("candidate_pool_exceeds_collected_total")

        # 层内不可重复；同一评论可以同时属于多个可审计来源层。
        if set(self.stratum_comment_ids) != self.available_strata:
            raise ValueError("stratum_keys_must_match_available_strata")
        candidates = set(self.candidate_comment_ids)
        for values in self.stratum_comment_ids.values():
            if len(values) != len(set(values)):
                raise ValueError("stratum_comment_ids_must_be_unique")
            if any(item not in candidates for item in values):
                raise ValueError("stratum_comment_id_not_in_candidate_pool")
        return self


class ReviewItem(Contract):
    """指向脱敏后记录的人工复核原因，不包含原始值。"""

    video_id: PseudonymousVideoId
    comment_id: PseudonymousCommentId | None
    field: ReviewField
    reason: Identifier


class SanitizationReport(Contract):
    """只记录安全摘要；额外字段会阻止原始 ID 混入报告。"""

    sanitization_schema_version: Literal["1.0"]
    video_id: PseudonymousVideoId
    rules_version: Identifier
    input_sha256: Hash
    output_sha256: Hash
    replacement_counts: dict[Identifier, NonNegativeInt]
    review_item_count: NonNegativeInt
    review_items: list[ReviewItem]
    started_at: Timestamp
    finished_at: Timestamp
    excluded: StrictBool
    exclusion_reason: Identifier | None

    @model_validator(mode="after")
    def reviews_match_count(self) -> Self:
        """复核数量必须可由不含原始值的复核项直接审计。"""

        if self.review_item_count != len(self.review_items):
            raise ValueError("review_item_count_mismatch")
        return self


class DatasetSplit(Contract):
    """以视频为单位保存开发、校准和留出集合。"""

    development: list[PseudonymousVideoId]
    calibration: list[PseudonymousVideoId]
    holdout: list[PseudonymousVideoId]

    @model_validator(mode="after")
    def integrity(self) -> Self:
        groups = (self.development, self.calibration, self.holdout)
        if any(len(group) != len(set(group)) for group in groups):
            raise ValueError("dataset_split_video_ids_must_be_unique")
        if sum(len(group) for group in groups) != len(set().union(*map(set, groups))):
            raise ValueError("dataset_split_video_ids_must_be_disjoint")
        return self


class CommentLabel(Contract):
    """单条评论的人工语义标签；模板必须显式保持未标注。"""

    comment_id: PseudonymousCommentId
    text: Text
    need_signal: NeedSignal
    signal_kind: SignalKind
    normalized_need: str | None
    noise_kind: NoiseKind
    video_reception: VideoReception

    @classmethod
    def unlabeled(cls, comment_id: str, text: str) -> Self:
        return cls(
            comment_id=comment_id,
            text=text,
            need_signal="unlabeled",
            signal_kind="unlabeled",
            normalized_need=None,
            noise_kind="unlabeled",
            video_reception="unlabeled",
        )


class ClusterLabel(Contract):
    """同一视频内由人工确认的需求簇。"""

    cluster_id: Identifier
    normalized_need: Text
    comment_ids: list[PseudonymousCommentId]


class AnnotationFile(Contract):
    """单位视频的一份独立人工标注。"""

    annotation_schema_version: Literal["1.0"]
    video_id: PseudonymousVideoId
    annotator_id: Identifier | None
    comments: list[CommentLabel]
    clusters: list[ClusterLabel]
    disputed: StrictBool
    dispute_reasons: list[Identifier]
    is_complete: StrictBool


class AdjudicationFile(Contract):
    """保存两份独立标注的最终人工裁决。"""

    adjudication_schema_version: Literal["1.0"]
    video_id: PseudonymousVideoId
    annotator_ids: list[Identifier] = Field(min_length=2, max_length=2)
    adjudicator_id: Identifier
    decision: AnnotationFile | None
