"""Append-only audit logging that rejects sensitive field names."""

import json
from collections.abc import Mapping
from pathlib import Path


class SensitiveAuditValue(ValueError):
    """Raised when an audit entry could expose sensitive data."""


class AuditSerializationError(ValueError):
    """Raised when an audit entry is not strict JSON."""


class AuditWriteError(RuntimeError):
    """Raised when an audit entry cannot be committed safely."""


class _UnsafeAuditValue(ValueError):
    """Internal signal whose message is always a fixed safe category."""


_FORBIDDEN_KEY_FRAGMENTS = (
    "cookie",
    "token",
    "authorization",
    "password",
    "request_headers",
    "response_body",
    "storage_state",
)


def _snapshot_value(value: object, active_containers: set[int]) -> object:
    if isinstance(value, Mapping):
        container_id = id(value)
        if container_id in active_containers:
            raise _UnsafeAuditValue("cyclic_value")
        active_containers.add(container_id)
        try:
            snapshot: dict[str, object] = {}
            for key, nested_value in value.items():
                if not isinstance(key, str):
                    raise _UnsafeAuditValue("non_string_key")
                normalized_key = key.casefold()
                for fragment in _FORBIDDEN_KEY_FRAGMENTS:
                    if fragment in normalized_key:
                        raise _UnsafeAuditValue(fragment)
                snapshot[key] = _snapshot_value(nested_value, active_containers)
            return snapshot
        finally:
            active_containers.remove(container_id)
    if isinstance(value, list | tuple):
        container_id = id(value)
        if container_id in active_containers:
            raise _UnsafeAuditValue("cyclic_value")
        active_containers.add(container_id)
        try:
            return [_snapshot_value(item, active_containers) for item in value]
        finally:
            active_containers.remove(container_id)
    return value


def _snapshot_fields(fields: Mapping[str, object]) -> tuple[dict[str, object] | None, str | None]:
    try:
        snapshot = _snapshot_value(fields, set())
    except _UnsafeAuditValue as error:
        return None, str(error)
    except Exception:
        return None, "audit_snapshot_failed"
    if not isinstance(snapshot, dict):
        return None, "audit_fields_must_be_mapping"
    return snapshot, None


def _serialize_entry(event: str, fields: dict[str, object]) -> bytes | None:
    try:
        line = json.dumps(
            {"event": event, "fields": fields},
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, OverflowError):
        return None
    return f"{line}\n".encode("ascii")


def _make_parent(path: Path) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return True


def _read_existing(path: Path) -> tuple[bool, bytes]:
    try:
        return True, path.read_bytes()
    except FileNotFoundError:
        return True, b""
    except OSError:
        return False, b""


def _write_bytes(path: Path, data: bytes) -> bool:
    try:
        with path.open("xb") as output:
            written = output.write(data)
    except OSError:
        return False
    return written == len(data)


def _replace_file(source: Path, destination: Path) -> bool:
    try:
        source.replace(destination)
    except OSError:
        return False
    return True


def _remove_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


class AuditLog:
    """Write deterministic, compact JSON Lines audit entries."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, event: str, fields: Mapping[str, object]) -> None:
        snapshot, snapshot_error = _snapshot_fields(fields)
        if snapshot_error is not None:
            raise SensitiveAuditValue(snapshot_error)
        assert snapshot is not None
        encoded = _serialize_entry(event, snapshot)
        if encoded is None:
            raise AuditSerializationError("audit_serialization_failed")
        if not _make_parent(self.path):
            raise AuditWriteError("audit_parent_creation_failed")
        read_succeeded, existing = _read_existing(self.path)
        if not read_succeeded:
            raise AuditWriteError("audit_existing_read_failed")
        if existing and not existing.endswith(b"\n"):
            existing += b"\n"
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        if temporary.exists():
            raise AuditWriteError("audit_temporary_exists")
        if not _write_bytes(temporary, existing + encoded):
            _remove_file(temporary)
            raise AuditWriteError("audit_temp_write_failed")
        if not _replace_file(temporary, self.path):
            _remove_file(temporary)
            raise AuditWriteError("audit_replace_failed")
