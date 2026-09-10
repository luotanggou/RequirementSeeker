"""Pure response adapters for supported collection platforms."""

from .base import ParsedCommentPage, ParsedVideo, PlatformAdapter, ResponseShapeChanged
from .bilibili import BilibiliAdapter
from .douyin import DouyinAdapter

__all__ = [
    "BilibiliAdapter",
    "DouyinAdapter",
    "ParsedCommentPage",
    "ParsedVideo",
    "PlatformAdapter",
    "ResponseShapeChanged",
]
