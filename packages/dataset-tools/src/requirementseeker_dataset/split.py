"""按伪视频 ID 稳定划分开发、校准和留出集合。"""

from collections.abc import Sequence
from hashlib import sha256
from math import ceil, floor

from .contracts import DatasetSplit


def stable_split(video_ids: Sequence[str]) -> DatasetSplit:
    """以稳定哈希排序，确保输入顺序不会改变视频级拆分。"""

    ranked = sorted(video_ids, key=lambda value: (sha256(value.encode()).digest(), value))
    if len(ranked) == 24:
        development_count, calibration_count = 10, 7
    else:
        development_count = ceil(len(ranked) * 0.4)
        calibration_count = floor(len(ranked) * 0.3)
    calibration_end = development_count + calibration_count
    return DatasetSplit(
        development=ranked[:development_count],
        calibration=ranked[development_count:calibration_end],
        holdout=ranked[calibration_end:],
    )
