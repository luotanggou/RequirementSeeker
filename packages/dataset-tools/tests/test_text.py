import pytest

from requirementseeker_dataset.text import sanitize_text


@pytest.mark.parametrize(
    ("source", "expected", "category"),
    [
        ("联系 13800138000", "联系 [PHONE]", "phone"),
        ("发到 a@example.com", "发到 [EMAIL]", "email"),
        ("微信 abc_123", "[HANDLE]", "handle"),
    ],
)
def test_direct_identifiers_are_replaced(source: str, expected: str, category: str) -> None:
    result = sanitize_text(source)

    assert result.text == expected
    assert result.replacement_counts == {category: 1}
    assert result.review_reasons == []


def test_semantic_content_and_internal_spaces_are_preserved() -> None:
    result = sanitize_text("  我需要  一个离线工具\r\n第二行  ")

    assert result.text == "我需要  一个离线工具\n第二行"
    assert result.replacement_counts == {}


def test_unicode_is_normalized_to_nfc() -> None:
    result = sanitize_text("  Cafe\u0301  ")

    assert result.text == "Café"


def test_ambiguous_address_becomes_review_item_not_deleted() -> None:
    result = sanitize_text("在幸福路 18 号见")

    assert result.text == "在幸福路 18 号见"
    assert result.review_reasons == ["possible_precise_address"]


def test_explicit_full_address_and_identity_number_are_replaced() -> None:
    result = sanitize_text("地址：上海市幸福路18号，身份证 110101199001011234")

    assert result.text == "[ADDRESS]，身份证 [IDENTIFIER]"
    assert result.replacement_counts == {"identity": 1, "address": 1}
    assert result.review_reasons == []


def test_repeated_identifier_counts_are_deterministic() -> None:
    result = sanitize_text("a@example.com 和 b@example.com")

    assert result.text == "[EMAIL] 和 [EMAIL]"
    assert result.replacement_counts == {"email": 2}
