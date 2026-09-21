from requirementseeker_dataset.split import stable_split


def _video_ids(count: int) -> list[str]:
    return [f"video_{index:032x}" for index in range(count)]


def test_24_videos_split_10_7_7_deterministically() -> None:
    values = _video_ids(24)

    first = stable_split(values)
    second = stable_split(list(reversed(values)))

    assert [len(first.development), len(first.calibration), len(first.holdout)] == [10, 7, 7]
    assert first == second
    assert len(set(first.development + first.calibration + first.holdout)) == 24


def test_nonstandard_size_uses_40_30_remainder_split() -> None:
    split = stable_split(_video_ids(7))

    assert [len(split.development), len(split.calibration), len(split.holdout)] == [3, 2, 2]
