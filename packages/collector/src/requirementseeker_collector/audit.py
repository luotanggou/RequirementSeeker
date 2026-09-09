"""Append-only audit logging that rejects sensitive field names."""

import json
from collections.abc import Mapping
from pathlib import Path


class SensitiveAuditValue(ValueError):
    """Raised when an audit entry could expose sensitive data."""


_FORBIDDEN_KEY_FRAGMENTS = (
    "cookie",
    "token",
    "authorization",
    "password",
    "request_headers",
    "response_body",
    "storage_state",
)


def _validate_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested_value in value.items():
            if not isinstance(key, str):
                raise SensitiveAuditValue("non_string_key")
            normalized_key = key.casefold()
            for fragment in _FORBIDDEN_KEY_FRAGMENTS:
                if fragment in normalized_key:
                    raise SensitiveAuditValue(fragment)
            _validate_keys(nested_value)
    elif isinstance(value, list | tuple):
        for nested_value in value:
            _validate_keys(nested_value)


class AuditLog:
    """Write deterministic, compact JSON Lines audit entries."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, event: str, fields: Mapping[str, object]) -> None:
        _validate_keys(fields)
        line = json.dumps(
            {"event": event, "fields": fields},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="ascii", newline="") as output:
            output.write(f"{line}\n")
