import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from requirementseeker_dataset.source import RawDatasetError, read_raw_video

FIXTURES = Path(__file__).parent / "fixtures"
VALID = FIXTURES / "raw" / "valid" / "bilibili" / "BVfake"


def _copy_valid(tmp_path: Path, *, platform: str = "bilibili", video_key: str = "BVfake") -> Path:
    target = tmp_path / platform / video_key
    shutil.copytree(VALID, target)
    return target


def _load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _comment_lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _write_comments(path: Path, comments: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(
            f"{json.dumps(item, ensure_ascii=False, separators=(',', ':'))}\n" for item in comments
        ),
        encoding="utf-8",
    )


def _invalid_bundle(tmp_path: Path, case: str) -> Path:
    directory = _copy_valid(tmp_path / case)
    if case == "missing-file":
        (directory / "collection.json").unlink()
    elif case == "unknown-field":
        video = _load_json(directory / "video.json")
        video["cookie"] = "must-not-pass"
        _write_json(directory / "video.json", video)
    elif case == "duplicate-comment":
        comments = _comment_lines(directory / "comments.jsonl")
        _write_comments(directory / "comments.jsonl", [comments[0], comments[0]])
    elif case == "count-mismatch":
        collection = _load_json(directory / "collection.json")
        collection["collected_total"] = 1
        _write_json(directory / "collection.json", collection)
    elif case == "self-parent":
        comments = _comment_lines(directory / "comments.jsonl")
        comments[0]["raw_parent_comment_id"] = comments[0]["raw_comment_id"]
        _write_comments(directory / "comments.jsonl", comments)
    else:  # pragma: no cover - parametrization owns the closed case set.
        raise AssertionError(f"unknown test case: {case}")
    return directory


def test_source_reads_exact_three_file_directory() -> None:
    source = read_raw_video(VALID)

    assert source.video.raw_video_id == "BVfake"
    assert source.collection.collected_total == len(source.comments) == 2
    assert isinstance(source.comments, tuple)


def test_loaded_bundle_models_are_immutable() -> None:
    source = read_raw_video(VALID)

    with pytest.raises(ValidationError, match="frozen_instance"):
        source.video.title = "不得修改"


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("missing-file", "raw_directory_file_set_invalid"),
        ("unknown-field", "raw_video_invalid"),
        ("duplicate-comment", "raw_comment_id_duplicate"),
        ("count-mismatch", "collected_total_mismatch"),
        ("self-parent", "raw_comment_self_parent"),
    ],
)
def test_invalid_raw_directory_is_rejected(tmp_path: Path, case: str, code: str) -> None:
    with pytest.raises(RawDatasetError, match=code):
        read_raw_video(_invalid_bundle(tmp_path, case))


def test_platform_directory_must_match_video_contract(tmp_path: Path) -> None:
    directory = _copy_valid(tmp_path, platform="douyin")

    with pytest.raises(RawDatasetError, match="raw_platform_directory_mismatch"):
        read_raw_video(directory)


def test_video_directory_must_equal_unencoded_raw_id(tmp_path: Path) -> None:
    directory = _copy_valid(tmp_path, video_key="different")

    with pytest.raises(RawDatasetError, match="raw_video_directory_mismatch"):
        read_raw_video(directory)


def test_percent_encoded_video_directory_is_rejected(tmp_path: Path) -> None:
    directory = _copy_valid(tmp_path, video_key="BV%2Ffake")
    video = _load_json(directory / "video.json")
    video["raw_video_id"] = "BV%2Ffake"
    _write_json(directory / "video.json", video)

    with pytest.raises(RawDatasetError, match="raw_video_directory_encoding_invalid"):
        read_raw_video(directory)


def test_invalid_json_is_rejected_without_echoing_content(tmp_path: Path) -> None:
    directory = _copy_valid(tmp_path)
    marker = "private-marker-must-not-leak"
    (directory / "comments.jsonl").write_text(marker, encoding="utf-8")

    with pytest.raises(RawDatasetError, match="raw_comment_invalid") as caught:
        read_raw_video(directory)
    assert marker not in str(caught.value)


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_unicode_line_separators_inside_text_are_preserved(tmp_path: Path, separator: str) -> None:
    directory = _copy_valid(tmp_path)
    comments = _comment_lines(directory / "comments.jsonl")
    comments[0]["text"] = f"前半段{separator}后半段"
    _write_comments(directory / "comments.jsonl", comments)

    bundle = read_raw_video(directory)

    assert bundle.comments[0].text == f"前半段{separator}后半段"
