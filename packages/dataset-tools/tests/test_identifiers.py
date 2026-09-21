import pytest

from requirementseeker_dataset.identifiers import (
    IdentifierPseudonymizer,
    SecretConfigurationError,
)

TEST_SECRET = b"local-test-secret-at-least-32-bytes"


def test_hmac_is_stable_and_type_scoped() -> None:
    ids = IdentifierPseudonymizer(TEST_SECRET)

    assert ids.video("123") == ids.video("123")
    assert ids.video("123") != ids.comment("123")
    assert ids.video("123").startswith("video_")
    assert ids.author("123").startswith("author_")
    assert ids.comment("123").startswith("comment_")
    assert len(ids.video("123")) == len("video_") + 32


def test_secret_is_not_in_repr_or_error() -> None:
    marker = b"never-print-this-secret-value-1234"
    ids = IdentifierPseudonymizer(marker)

    assert marker.decode() not in repr(ids)
    assert repr(ids) == "IdentifierPseudonymizer(<redacted>)"


def test_short_secret_is_rejected_without_value() -> None:
    marker = b"tiny-secret-value"
    with pytest.raises(SecretConfigurationError, match="dataset_secret_too_short") as error:
        IdentifierPseudonymizer(marker)

    assert marker.decode() not in str(error.value)


def test_environment_loader_reports_only_missing_variable_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    variable_name = "RS_DATASET_MISSING_TEST_SECRET"
    monkeypatch.delenv(variable_name, raising=False)

    with pytest.raises(SecretConfigurationError, match=variable_name) as error:
        IdentifierPseudonymizer.from_environment(variable_name)

    assert str(error.value) == f"dataset_secret_missing:{variable_name}"


def test_environment_loader_uses_value_without_exposing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    variable_name = "RS_DATASET_TEST_SECRET"
    marker = TEST_SECRET.decode()
    monkeypatch.setenv(variable_name, marker)

    ids = IdentifierPseudonymizer.from_environment(variable_name)

    assert ids.comment("abc") == IdentifierPseudonymizer(TEST_SECRET).comment("abc")
    assert marker not in repr(ids)
