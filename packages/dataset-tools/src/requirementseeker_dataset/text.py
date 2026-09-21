"""以确定性规则规范化文本、替换直接身份信息并标记疑似地址。"""

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

EMAIL_PATTERN = re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+(?![\w.-])")
PHONE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
IDENTITY_PATTERN = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
PRIVATE_HANDLE_PATTERN = re.compile(r"(?i)(?:微信|wx|vx|v信|qq|私信)[:：\s]*[a-z0-9_-]{5,32}")
EXPLICIT_ADDRESS_PATTERN = re.compile(r"地址[:：]\s*[^，,。\n]{4,80}(?=[，,。\n]|$)")
PRECISE_ADDRESS_CANDIDATE = re.compile(r"[\u4e00-\u9fff]{2,}(?:路|街|巷)\s*\d+\s*号")


@dataclass(frozen=True, slots=True)
class RedactionRule:
    name: str
    pattern: re.Pattern[str]
    replacement: str


@dataclass(frozen=True, slots=True)
class TextResult:
    text: str
    replacement_counts: dict[str, int]
    review_reasons: list[str]


RULES = (
    RedactionRule("email", EMAIL_PATTERN, "[EMAIL]"),
    RedactionRule("phone", PHONE_PATTERN, "[PHONE]"),
    RedactionRule("identity", IDENTITY_PATTERN, "[IDENTIFIER]"),
    RedactionRule("handle", PRIVATE_HANDLE_PATTERN, "[HANDLE]"),
    RedactionRule("address", EXPLICIT_ADDRESS_PATTERN, "[ADDRESS]"),
)


def sanitize_text(source: str) -> TextResult:
    """保留文本语义和内部空白，只规范化编码、换行与外层空白。"""

    normalized = source.replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFC", normalized).strip()
    counts: Counter[str] = Counter()
    for rule in RULES:
        text, count = rule.pattern.subn(rule.replacement, text)
        if count:
            counts[rule.name] += count

    reviews = ["possible_precise_address"] if PRECISE_ADDRESS_CANDIDATE.search(text) else []
    return TextResult(text=text, replacement_counts=dict(counts), review_reasons=reviews)
