"""Profile-scoped durable registry, journals, and bounded audit."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
from typing import Any

from .config import hermes_home
from .filesystem import fsync_dir

SCHEMA_VERSION = 1
_SAFE_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_AUDIT_EVENTS = frozenset({
    "skill_publisher.audit_record_invalid",
    "skill_publisher.commit_cleanup_pending",
    "skill_publisher.create_rejected",
    "skill_publisher.deleted",
    "skill_publisher.invalid_event",
    "skill_publisher.lifecycle_barrier",
    "skill_publisher.lifecycle_blocked",
    "skill_publisher.local_unclassified",
    "skill_publisher.managed_mutation_blocked",
    "skill_publisher.middleware_unavailable",
    "skill_publisher.pending_blocked",
    "skill_publisher.policy_unavailable",
    "skill_publisher.promoted",
    "skill_publisher.promotion_failed",
    "skill_publisher.recovered",
    "skill_publisher.recovery_blocked",
    "skill_publisher.rollback_blocked",
    "skill_publisher.scope_change_rolled_back",
    "skill_publisher.tool_observed",
    "skill_publisher.transaction_in_progress",
    "skill_publisher.turn_pending",
    "skill_publisher.unpublish_cleanup_pending",
    "skill_publisher.unpublish_rollback_blocked",
    "skill_publisher.unpublished",
    "skill_publisher.write_approval_incompatible",
})
_AUDIT_RESULTS = frozenset({"audit_only", "blocked", "deferred", "invalid", "local", "observed", "recovery_required", "rolled_back", "success"})
_AUDIT_ACTIONS = frozenset({"create", "edit", "patch", "delete", "write_file", "remove_file"})
_AUDIT_CLASSIFICATIONS = frozenset({"shared", "local", "private"})


class StateError(RuntimeError):
    """Durable plugin state is malformed or unreadable."""


def state_root() -> Path:
    return hermes_home() / "plugin-data" / "hermes-skill-publisher"


def state_lock_path() -> Path:
    return state_root() / "state.lock"


def transactions_dir() -> Path:
    return state_root() / "transactions"


def ensure_state() -> Path:
    root = state_root()
    (root / "transactions").mkdir(parents=True, exist_ok=True)
    fsync_dir(root / "transactions")
    fsync_dir(root)
    return root


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateError(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise StateError(f"{path.name} must contain a JSON object")
    return value


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(6)}"
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)  # plugin-owned state file, never a user package path
    fsync_dir(path.parent)


def load_registry() -> dict[str, Any]:
    path = state_root() / "registry.json"
    try:
        registry = _read_json(path)
    except FileNotFoundError:
        return {"schema_version": SCHEMA_VERSION, "publications": {}}
    if registry.get("schema_version") != SCHEMA_VERSION:
        raise StateError("unsupported registry schema")
    if not isinstance(registry.get("publications"), dict):
        raise StateError("registry publications must be a mapping")
    return registry


def save_registry(registry: dict[str, Any]) -> None:
    if registry.get("schema_version") != SCHEMA_VERSION or not isinstance(registry.get("publications"), dict):
        raise StateError("refusing to write malformed registry")
    ensure_state()
    atomic_write_json(state_root() / "registry.json", registry)


def operation_id(name: str) -> str:
    safe = "".join(ch for ch in name if ch.isalnum() or ch == "-")[:40] or "skill"
    return f"{safe}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(8)}"


def journal_path(op_id: str) -> Path:
    if not op_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in op_id):
        raise StateError("invalid operation id")
    return transactions_dir() / f"{op_id}.json"


def write_journal(journal: dict[str, Any]) -> None:
    op_id = journal.get("operation_id")
    if journal.get("schema_version") != SCHEMA_VERSION or not isinstance(op_id, str):
        raise StateError("refusing to write malformed journal")
    ensure_state()
    atomic_write_json(journal_path(op_id), journal)


def list_journals() -> list[dict[str, Any]]:
    directory = transactions_dir()
    if not directory.exists():
        return []
    journals = []
    for path in sorted(directory.glob("*.json")):
        journal = _read_json(path)
        if journal.get("schema_version") != SCHEMA_VERSION or journal.get("operation_id") != path.stem:
            raise StateError(f"invalid transaction journal: {path.name}")
        journals.append(journal)
    return journals


def journals_for_recovery() -> list[tuple[dict[str, Any] | None, str, str | None]]:
    """Return every journal independently so one corrupt file cannot poison recovery."""
    directory = transactions_dir()
    if not directory.exists():
        return []
    results = []
    for path in sorted(directory.glob("*.json")):
        try:
            journal = _read_json(path)
            if journal.get("schema_version") != SCHEMA_VERSION or journal.get("operation_id") != path.stem:
                raise StateError(f"invalid transaction journal: {path.name}")
            results.append((journal, path.stem, None))
        except Exception as exc:
            results.append((None, path.stem, str(exc)))
    return results


def remove_journal(op_id: str) -> None:
    path = journal_path(op_id)
    path.unlink(missing_ok=True)
    if path.parent.exists():
        fsync_dir(path.parent)


def safe_skill_name(value: Any) -> str | None:
    if isinstance(value, str) and len(value) <= 64 and _SAFE_NAME_RE.fullmatch(value):
        return value
    return None


def safe_operation_id(value: Any) -> str | None:
    if isinstance(value, str) and len(value) <= 200 and _SAFE_TOKEN_RE.fullmatch(value):
        return value
    return None


def _safe_source_relpath(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if path.is_absolute() or any(not safe_skill_name(part) for part in path.parts):
        return None
    return path.as_posix()


def _invalid_audit_record() -> dict[str, Any]:
    return {
        "timestamp": "",
        "event": "skill_publisher.audit_record_invalid",
        "result": "invalid",
        "code": "skill_publisher.audit_record_invalid",
    }


def _sanitize_audit_record(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return _invalid_audit_record()
    timestamp = raw.get("timestamp")
    try:
        if not isinstance(timestamp, str):
            raise ValueError
        datetime.fromisoformat(timestamp)
    except ValueError:
        return _invalid_audit_record()
    event = raw.get("event")
    result = raw.get("result")
    if event not in _AUDIT_EVENTS or result not in _AUDIT_RESULTS:
        return _invalid_audit_record()
    record: dict[str, Any] = {"timestamp": timestamp, "event": event, "result": result}
    operation_id = safe_operation_id(raw.get("operation_id"))
    if operation_id:
        record["operation_id"] = operation_id
    session_id = safe_operation_id(raw.get("session_id"))
    if session_id:
        record["session_id"] = session_id
    skill_name = safe_skill_name(raw.get("skill_name"))
    if skill_name:
        record["skill_name"] = skill_name
    classification = raw.get("classification")
    if classification in _AUDIT_CLASSIFICATIONS:
        record["classification"] = classification
    source_relpath = _safe_source_relpath(raw.get("source_relpath"))
    if source_relpath:
        record["source_relpath"] = source_relpath
    action = raw.get("action")
    if action in _AUDIT_ACTIONS:
        record["action"] = action
    code = raw.get("code")
    if isinstance(code, str) and re.fullmatch(r"skill_publisher\.[a-z0-9_]+", code):
        record["code"] = code
    if raw.get("error"):
        record["error"] = "details withheld"
    return record


def audit(event: str, *, result: str, error: str | None = None, **fields: Any) -> None:
    ensure_state()
    raw: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event if event in _AUDIT_EVENTS else "skill_publisher.invalid_event",
        "result": result if result in _AUDIT_RESULTS else "observed",
        **fields,
    }
    if error:
        raw["error"] = True
    record = _sanitize_audit_record(raw)
    path = state_root() / "audit.jsonl"
    payload = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    fsync_dir(path.parent)


def read_audit(limit: int = 100) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    path = state_root() / "audit.jsonl"
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise StateError(f"cannot read audit log ({type(exc).__name__})") from exc
    records = []
    for line in lines[-limit:]:
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            raw = None
        records.append(_sanitize_audit_record(raw))
    return records


_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _safe_registry_relpath(raw: Any, what: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise StateError(f"registry {what} must be a non-empty relative path")
    relative = Path(raw)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise StateError(f"registry {what} is not a safe relative path")
    return relative


def validate_publication(name: Any, record: Any) -> dict[str, Any]:
    """Deep-validate one registry publication before it is trusted as authority.

    Raises StateError on any structural defect; callers must treat that as a
    blocker and never act on the record's paths.
    """
    if safe_skill_name(name) is None:
        raise StateError("registry publication name is invalid")
    if not isinstance(record, dict):
        raise StateError(f"registry publication {name} must be a mapping")
    canonical = record.get("canonical_path")
    if not isinstance(canonical, str) or not canonical:
        raise StateError(f"registry publication {name} has an invalid canonical path")
    canonical_path = Path(canonical)
    if not canonical_path.is_absolute() or canonical_path.name != name:
        raise StateError(f"registry publication {name} canonical path does not match the skill name")
    if _safe_registry_relpath(record.get("source_relpath"), "source path").name != name:
        raise StateError(f"registry publication {name} source path does not match the skill name")
    digest = record.get("digest")
    if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
        raise StateError(f"registry publication {name} has an invalid digest")
    if record.get("scope") != "shared":
        raise StateError(f"registry publication {name} must have shared scope")
    adapters = record.get("adapter_links")
    if not isinstance(adapters, dict):
        raise StateError(f"registry publication {name} adapters must be a mapping")
    for label, adapter in adapters.items():
        if not isinstance(label, str) or not label or not isinstance(adapter, dict):
            raise StateError(f"registry publication {name} has an invalid adapter record")
        path_raw = adapter.get("path")
        if not isinstance(path_raw, str) or not path_raw:
            raise StateError(f"registry adapter {label} for {name} has an invalid path")
        path = Path(path_raw)
        if not path.is_absolute() or path.name != name:
            raise StateError(f"registry adapter {label} for {name} path does not match the skill name")
        text = adapter.get("link_text")
        if not isinstance(text, str) or not text:
            raise StateError(f"registry adapter {label} for {name} has an invalid link payload")
        text_path = Path(text)
        if text_path.is_absolute() or text_path.name != name or any(part in {"", "."} for part in text_path.parts):
            raise StateError(f"registry adapter {label} for {name} link payload is not a safe relative target")
    return record
