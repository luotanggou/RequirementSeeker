import json
from pathlib import Path

import pytest

from requirementseeker_collector.audit import AuditLog, SensitiveAuditValue


def test_sensitive_audit_value_does_not_create_output(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "run.jsonl"

    with pytest.raises(SensitiveAuditValue, match="cookie") as caught:
        AuditLog(path).write("response", {"cookie": "secret-marker"})

    assert "secret-marker" not in str(caught.value)
    assert not path.parent.exists()
    assert not path.exists()


@pytest.mark.parametrize(
    ("fragment", "fields"),
    [
        ("cookie", {"session_cookie_value": "secret-marker"}),
        ("token", {"ApiTOKENValue": "secret-marker"}),
        ("authorization", {"authorization_data": "secret-marker"}),
        ("password", {"user_password_hash": "secret-marker"}),
        ("request_headers", {"REQUEST_HEADERS_RAW": "secret-marker"}),
        ("response_body", {"raw_response_body": "secret-marker"}),
        ("storage_state", {"browser_storage_state": "secret-marker"}),
    ],
)
def test_all_sensitive_key_fragments_are_rejected(
    tmp_path: Path, fragment: str, fields: dict[str, str]
) -> None:
    path = tmp_path / "run.jsonl"

    with pytest.raises(SensitiveAuditValue, match=fragment) as caught:
        AuditLog(path).write("response", fields)

    assert "secret-marker" not in str(caught.value)
    assert not path.exists()


@pytest.mark.parametrize(
    "fields",
    [
        {"outer": {"Password": "secret-marker"}},
        {"outer": [{"access_token_value": "secret-marker"}]},
        {"outer": ({"request_headers_copy": "secret-marker"},)},
        [{"response_body_text": "secret-marker"}],
        ({"storage_state_copy": "secret-marker"},),
    ],
)
def test_nested_containers_are_checked_before_writing(tmp_path: Path, fields: object) -> None:
    path = tmp_path / "run.jsonl"

    with pytest.raises(SensitiveAuditValue) as caught:
        AuditLog(path).write("response", fields)  # type: ignore[arg-type]

    assert "secret-marker" not in str(caught.value)
    assert not path.exists()


def test_non_string_mapping_key_is_rejected_without_value_leak(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"

    with pytest.raises(SensitiveAuditValue, match="non_string_key") as caught:
        AuditLog(path).write("response", {1: "secret-marker"})  # type: ignore[dict-item]

    assert "secret-marker" not in str(caught.value)
    assert not path.exists()


def test_legal_audit_entries_are_ascii_compact_and_appended(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "run.jsonl"
    log = AuditLog(path)

    log.write("collection", {"message": "完成", "count": 2})
    log.write("collection", {"count": 2, "message": "完成"})

    expected = (
        b'{"event":"collection","fields":{"count":2,"message":"\\u5b8c\\u6210"}}\n'
        b'{"event":"collection","fields":{"count":2,"message":"\\u5b8c\\u6210"}}\n'
    )
    assert path.read_bytes() == expected
    assert [json.loads(line) for line in path.read_text(encoding="ascii").splitlines()] == [
        {"event": "collection", "fields": {"count": 2, "message": "完成"}},
        {"event": "collection", "fields": {"count": 2, "message": "完成"}},
    ]
