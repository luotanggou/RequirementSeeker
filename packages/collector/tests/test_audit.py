import json
import math
from pathlib import Path

import pytest

import requirementseeker_collector.audit as audit_module
from requirementseeker_collector.audit import (
    AuditLog,
    AuditSerializationError,
    AuditWriteError,
    SensitiveAuditValue,
)


class ChangingItemsDict(dict[str, object]):
    def __init__(self) -> None:
        super().__init__({"cookie": "secret-marker"})
        self.calls = 0

    def items(self):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls == 1:
            return {"safe": "value"}.items()
        return super().items()


class RaisingItemsDict(dict[str, object]):
    def items(self):  # type: ignore[no-untyped-def]
        raise SensitiveAuditValue("secret-marker")


def test_sensitive_audit_value_does_not_create_output(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "run.jsonl"

    with pytest.raises(SensitiveAuditValue, match="cookie") as caught:
        AuditLog(path).write("response", {"cookie": "secret-marker"})

    assert "secret-marker" not in str(caught.value)
    assert not path.parent.exists()
    assert not path.exists()


def test_audit_serializes_only_the_checked_mapping_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    fields = ChangingItemsDict()

    AuditLog(path).write("response", fields)

    assert fields.calls == 1
    assert path.read_bytes() == b'{"event":"response","fields":{"safe":"value"}}\n'


def test_audit_rejects_cycles_without_recursion_error(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    fields: dict[str, object] = {}
    fields["nested"] = fields

    with pytest.raises(SensitiveAuditValue, match="cyclic_value"):
        AuditLog(path).write("response", fields)

    assert not path.exists()


def test_audit_does_not_trust_exception_text_from_mapping(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"

    with pytest.raises(SensitiveAuditValue, match="audit_snapshot_failed") as caught:
        AuditLog(path).write("response", RaisingItemsDict())

    assert "secret-marker" not in repr(caught.value.args)
    assert caught.value.__context__ is None
    assert not path.exists()


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_audit_rejects_non_standard_json_numbers(tmp_path: Path, value: float) -> None:
    path = tmp_path / "run.jsonl"

    with pytest.raises(AuditSerializationError, match="audit_serialization_failed"):
        AuditLog(path).write("response", {"ratio": value})

    assert not path.exists()


def test_audit_parent_error_is_safe(tmp_path: Path) -> None:
    blocker = tmp_path / "secret-marker"
    blocker.write_text("file", encoding="utf-8")

    with pytest.raises(AuditWriteError, match="audit_parent_creation_failed") as caught:
        AuditLog(blocker / "nested" / "run.jsonl").write("response", {"safe": 1})

    assert "secret-marker" not in repr(caught.value.args)
    assert caught.value.__context__ is None


def test_audit_failed_rewrite_keeps_previous_complete_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "run.jsonl"
    log = AuditLog(path)
    log.write("first", {"safe": 1})
    before = path.read_bytes()

    monkeypatch.setattr(audit_module, "_write_bytes", lambda _path, _data: False)

    with pytest.raises(AuditWriteError, match="audit_temp_write_failed"):
        log.write("second", {"safe": 2})

    assert path.read_bytes() == before


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


def test_audit_appends_after_existing_final_line_without_newline(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    path.write_bytes(b'{"event":"old","fields":{}}')

    AuditLog(path).write("new", {"safe": 1})

    assert [json.loads(line) for line in path.read_text(encoding="ascii").splitlines()] == [
        {"event": "old", "fields": {}},
        {"event": "new", "fields": {"safe": 1}},
    ]
