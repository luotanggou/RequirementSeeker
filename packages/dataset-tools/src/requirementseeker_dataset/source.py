"""严格读取 Collector 三文件输出，不依赖 Collector 的 Python 包。"""

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import unquote

from pydantic import ConfigDict, Field, StrictBool, ValidationError, model_validator

from .contracts import (
    Contract,
    Identifier,
    NonNegativeInt,
    Platform,
    PositiveInt,
    SamplingStratum,
    Timestamp,
)

_REQUIRED_FILES = {"video.json", "comments.jsonl", "collection.json"}
RawText = Annotated[str, Field(min_length=1, pattern=r"\S")]


class RawDatasetError(ValueError):
    """仅携带固定错误码，防止原始内容进入异常或日志。"""


class RawContract(Contract):
    """读取后的原始输入不可修改，避免绕过已完成的目录级校验。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RawVideoInput(RawContract):
    platform: Platform
    raw_video_id: Identifier
    raw_author_id: Identifier
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


class RawCommentInput(RawContract):
    raw_comment_id: Identifier
    raw_author_id: Identifier | None
    raw_parent_comment_id: Identifier | None
    text: RawText
    published_at: Timestamp | None
    collected_at: Timestamp
    like_count: NonNegativeInt | None
    reply_count: NonNegativeInt | None
    is_video_author: StrictBool | None
    source_stratum: SamplingStratum
    source_page_or_rank: PositiveInt


class CollectionErrorInput(RawContract):
    category: Identifier
    occurred_at: Timestamp
    stage: Identifier
    description: Annotated[str, Field(min_length=1, max_length=500)]
    raw_comment_id: Identifier | None = None
    conflict_fields: list[Literal["raw_author_id", "text"]] = Field(default_factory=list)


class CollectionInput(RawContract):
    reported_total: NonNegativeInt | None
    collected_total: NonNegativeInt
    pages_requested: NonNegativeInt
    pages_succeeded: NonNegativeInt
    sort_modes: list[Identifier]
    collection_started_at: Timestamp
    collection_finished_at: Timestamp
    collection_errors: list[CollectionErrorInput]

    @model_validator(mode="after")
    def integrity(self) -> Self:
        if self.pages_succeeded > self.pages_requested:
            raise ValueError("pages_succeeded_exceeds_requested")
        if self.collection_finished_at < self.collection_started_at:
            raise ValueError("collection_finished_before_started")
        return self


@dataclass(frozen=True, slots=True)
class RawVideoBundle:
    """完成目录级校验后返回的不可变原始输入。"""

    video: RawVideoInput
    comments: tuple[RawCommentInput, ...]
    collection: CollectionInput


def _read_model[RawModel: (RawVideoInput, CollectionInput)](
    path: Path, model: type[RawModel], code: str
) -> RawModel:
    try:
        return model.model_validate_json(path.read_bytes())
    except (OSError, ValidationError, ValueError):
        raise RawDatasetError(code) from None


def _read_comment_lines(path: Path) -> tuple[RawCommentInput, ...]:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise RawDatasetError("raw_comment_invalid") from None

    # JSONL 只以 LF 分隔；Unicode 行分隔符可能是评论正文的一部分。
    lines = content.split("\n")
    if lines and not lines[-1]:
        lines.pop()
    comments: list[RawCommentInput] = []
    for line in lines:
        if not line.strip():
            raise RawDatasetError("raw_comment_invalid")
        try:
            comments.append(RawCommentInput.model_validate_json(line))
        except (ValidationError, ValueError):
            raise RawDatasetError("raw_comment_invalid") from None
    return tuple(comments)


def _validate_bundle(bundle: RawVideoBundle, directory: Path) -> None:
    if directory.parent.name != bundle.video.platform:
        raise RawDatasetError("raw_platform_directory_mismatch")
    if unquote(directory.name) != directory.name:
        raise RawDatasetError("raw_video_directory_encoding_invalid")
    if directory.name != bundle.video.raw_video_id:
        raise RawDatasetError("raw_video_directory_mismatch")

    comment_ids = [comment.raw_comment_id for comment in bundle.comments]
    if len(comment_ids) != len(set(comment_ids)):
        raise RawDatasetError("raw_comment_id_duplicate")
    if any(comment.raw_parent_comment_id == comment.raw_comment_id for comment in bundle.comments):
        raise RawDatasetError("raw_comment_self_parent")
    if bundle.collection.collected_total != len(bundle.comments):
        raise RawDatasetError("collected_total_mismatch")


def read_raw_video(directory: Path) -> RawVideoBundle:
    """读取并关闭式校验一个原始视频目录。"""

    try:
        file_names = {item.name for item in directory.iterdir() if item.is_file()}
    except OSError:
        raise RawDatasetError("raw_directory_unreadable") from None
    if file_names != _REQUIRED_FILES:
        raise RawDatasetError("raw_directory_file_set_invalid")

    video = _read_model(directory / "video.json", RawVideoInput, "raw_video_invalid")
    collection = _read_model(
        directory / "collection.json", CollectionInput, "raw_collection_invalid"
    )
    comments = _read_comment_lines(directory / "comments.jsonl")

    bundle = RawVideoBundle(video=video, comments=comments, collection=collection)
    _validate_bundle(bundle, directory)
    return bundle
