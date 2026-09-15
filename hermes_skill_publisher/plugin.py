"""Hermes registration, tool middleware, and lifecycle callbacks."""

from __future__ import annotations

import contextvars
import json
import os
from pathlib import Path
import secrets
import threading
from typing import Any, Callable

from .config import load_config, require_classification_policy, skills_write_approval_enabled
from .frontmatter import ALLOWED_SCOPES, FIELD, Classification, classify_content, validate_skill_file
from .state import audit, load_registry, state_lock_path, validate_publication

_MIDDLEWARE_AVAILABLE = False
_WRITE_GATE_CAPABILITY_FAILED = False
_WRITE_GATE_CAPABILITY_LOCK = threading.Lock()

# Actions that mutate skill state and therefore pass through the host
# write-approval gate when it is enabled.
_WRITE_ACTIONS = {"create", "edit", "patch", "delete", "write_file", "remove_file"}
_BATCH_ACTIONS = {"create", "patch", "delete", "write_file", "remove_file"}
_BATCH_MAX_OPS = 20


class _BatchShapeError(ValueError):
    """A batch envelope cannot be safely interpreted by this middleware."""


def _is_write_action(action: Any) -> bool:
    return isinstance(action, str) and action in _WRITE_ACTIONS


def _batch_rejection(message: str) -> str:
    return json.dumps(
        {
            "success": False,
            "error": message,
            "code": "skill_publisher.batch_unsupported",
            "retryable": True,
            "skill_publisher": {
                "retry_action": "Retry skill_manage with a supported operations array or legacy flat shape.",
            },
        },
        ensure_ascii=False,
    )


def _shape_rejection(message: str, *, flat: bool) -> str:
    """Reject a malformed mutation envelope before core.

    Batch envelopes teach the operations-array contract; the legacy flat envelope
    teaches the complete-operation contract for the one op this middleware validates.
    """
    if not flat:
        return _batch_rejection(message)
    return json.dumps(
        {
            "success": False,
            "error": message,
            "code": "skill_publisher.operation_shape_invalid",
            "retryable": True,
            "skill_publisher": {
                "retry_action": (
                    "Re-emit the flat skill_manage call with the complete operation: "
                    "write_file needs both file_path and a string file_content."
                ),
                "field": "file_content",
            },
        },
        ensure_ascii=False,
    )


def _validate_flat_write_file(args: dict[str, Any]) -> None:
    """Enforce the batch write_file contract on the legacy flat envelope.

    Flat calls bypass ``operations`` shape validation, so a dropped or non-string
    ``file_content`` (the weak-model retry pattern behind repeated content-less
    writes) would only be caught by core. Enforce it here so the publisher write
    path refuses content-less writes before anything can run.
    """
    if args.get("action") != "write_file":
        return
    if not isinstance(args.get("name"), str) or not args.get("name"):
        raise _BatchShapeError("write_file requires a string name")
    if not isinstance(args.get("file_path"), str) or not args.get("file_path"):
        raise _BatchShapeError("write_file requires file_path")
    if not isinstance(args.get("file_content"), str):
        raise _BatchShapeError("write_file requires string file_content")


def _find_skill_dir(name: str) -> dict[str, Any] | None:
    """Best-effort skill lookup through the host tooling.

    Returns the host ``_find_skill`` mapping (``{"path": Path}``) or ``None`` when
    the capability is unavailable. The empty-overwrite guard treats ``None`` as
    "cannot prove this write is destructive" and leaves the decision to core
    validation; unit lanes run without a host and inject this seam directly.
    """
    try:
        from tools import skill_manager_tool as _skill_manager
        finder = getattr(_skill_manager, "_find_skill", None)
        if callable(finder):
            found = finder(name)
            return found if isinstance(found, dict) else None
    except Exception:
        return None
    return None


def _empty_overwrite_target(operations: tuple[dict[str, Any], ...]) -> tuple[str, str] | None:
    """``(name, file_path)`` of an empty-content write_file that would blank an existing file.

    A missing ``file_content`` is a shape error; this covers the explicitly empty
    string, which core accepts and which destroys the current file content. Discovery
    is best-effort: when the skill or file cannot be resolved the guard stays out of
    the way and core's own validation proceeds — it never blocks a write it cannot
    prove is destructive.
    """
    target: tuple[str, str] | None = None
    for operation in operations:
        if operation.get("action") != "write_file" or operation.get("file_content") != "":
            continue
        op_name, op_path = operation.get("name"), operation.get("file_path")
        if not isinstance(op_name, str) or not op_name:
            continue
        if not isinstance(op_path, str) or not op_path:
            continue
        if Path(op_path).is_absolute() or ".." in Path(op_path).parts:
            continue
        target = (op_name, op_path)
        break
    if target is None:
        return None
    found = _find_skill_dir(target[0])
    if not found or not found.get("path"):
        return None
    try:
        skill_dir = Path(found["path"])
        candidate = Path(os.path.normpath(skill_dir / target[1]))
        if candidate != skill_dir and skill_dir not in candidate.parents:
            return None
        if candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_size == 0:
            return None
    except Exception:
        return None
    return target


def _empty_overwrite_rejection(name: str, file_path: str) -> str:
    return json.dumps(
        {
            "success": False,
            "error": (
                f"Refusing to overwrite non-empty file '{file_path}' of skill '{name}' with EMPTY content. "
                "An empty file_content payload is treated as a dropped write and would destroy the file. "
                "Read the file and re-send its full content, use action='patch' for a targeted edit, or "
                "action='remove_file' to delete it."
            ),
            "code": "skill_publisher.empty_overwrite_blocked",
            "retryable": True,
            "skill_publisher": {
                "retry_action": "Re-send the complete file content, patch the file, or remove it.",
                "field": "file_content",
            },
        },
        ensure_ascii=False,
    )


def _parse_skill_manage_args(
    args: Any,
) -> tuple[bool, tuple[dict[str, Any], ...], Any, Any]:
    """Extract policy metadata without changing the payload sent to Hermes.

    Current Hermes advertises only ``operations``.  Keep the legacy flat
    shape untouched, but reject ambiguous or unsafe envelopes before core is
    called.  The host owns batch execution and rollback, so this parser never
    splits or replays operations.
    """
    if not isinstance(args, dict):
        raise _BatchShapeError("skill_manage arguments must be an object")
    if "operations" not in args:
        _validate_flat_write_file(args)
        return False, (args,), args.get("action"), args.get("name")

    # The current schema has no flat fields. Empty defaults and the host's
    # explicit ``action='batch'`` marker are tolerated for compatibility,
    # while other duplicates are ambiguous.
    if any(key not in {"operations", "action", "name"} for key in args):
        raise _BatchShapeError("operations batches cannot contain flat or unknown fields")
    if args.get("action") not in (None, "", "batch") or args.get("name") not in (None, ""):
        raise _BatchShapeError("operations batches cannot mix top-level action/name with operations")

    operations = args.get("operations")
    if not isinstance(operations, list) or not operations:
        raise _BatchShapeError("operations must be a non-empty array")
    if len(operations) > _BATCH_MAX_OPS:
        raise _BatchShapeError(f"operations is capped at {_BATCH_MAX_OPS} ops per call")
    if len(operations) != 1:
        raise _BatchShapeError(
            "only single-operation batches are supported; retry each operation separately"
        )

    normalized: list[dict[str, Any]] = []
    names: list[str] = []
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            raise _BatchShapeError(f"operations[{index}] must be an object")
        if "operations" in operation:
            raise _BatchShapeError(f"operations[{index}] cannot contain a nested operations array")
        action = operation.get("action")
        name = operation.get("name")
        if not isinstance(action, str) or not action:
            raise _BatchShapeError(f"operations[{index}] requires a string action")
        if action not in _BATCH_ACTIONS:
            raise _BatchShapeError(f"operations[{index}] uses an unsupported action")
        if not isinstance(name, str) or not name:
            raise _BatchShapeError(f"operations[{index}] requires a string name")
        if operation.get("category") is not None and not isinstance(operation.get("category"), str):
            raise _BatchShapeError(f"operations[{index}] category must be a string")
        if action == "create" and not isinstance(operation.get("content"), str):
            raise _BatchShapeError(f"operations[{index}] create requires string content")
        if action == "patch":
            content = operation.get("content")
            if content is not None and not isinstance(content, str):
                raise _BatchShapeError(f"operations[{index}] patch content must be a string")
            file_path = operation.get("file_path")
            if file_path is not None and not isinstance(file_path, str):
                raise _BatchShapeError(f"operations[{index}] patch file_path must be a string")
            replace_all = operation.get("replace_all", False)
            if not isinstance(replace_all, bool):
                raise _BatchShapeError(f"operations[{index}] patch replace_all must be boolean")
            full_rewrite = bool(content)
            if full_rewrite:
                if operation.get("old_string") is not None or operation.get("new_string") is not None:
                    raise _BatchShapeError(f"operations[{index}] patch must choose content or old/new strings")
            elif not isinstance(operation.get("old_string"), str) or not operation.get("old_string"):
                raise _BatchShapeError(f"operations[{index}] patch requires old_string")
            elif not isinstance(operation.get("new_string"), str):
                raise _BatchShapeError(f"operations[{index}] patch requires string new_string")
        elif action == "write_file":
            if not isinstance(operation.get("file_path"), str) or not operation.get("file_path"):
                raise _BatchShapeError(f"operations[{index}] write_file requires file_path")
            if not isinstance(operation.get("file_content"), str):
                raise _BatchShapeError(f"operations[{index}] write_file requires string file_content")
        elif action == "remove_file":
            if not isinstance(operation.get("file_path"), str) or not operation.get("file_path"):
                raise _BatchShapeError(f"operations[{index}] remove_file requires file_path")
        elif action == "delete":
            absorbed_into = operation.get("absorbed_into")
            if absorbed_into is not None and not isinstance(absorbed_into, str):
                raise _BatchShapeError("delete absorbed_into must be a string")

        normalized.append(operation)
        names.append(name)

    if len(set(names)) != 1:
        raise _BatchShapeError("a batch must target exactly one skill")
    return True, tuple(normalized), normalized[0]["action"], names[0]


def middleware_available() -> bool:
    return _MIDDLEWARE_AVAILABLE


def _write_gate_failure_latched() -> bool:
    with _WRITE_GATE_CAPABILITY_LOCK:
        return _WRITE_GATE_CAPABILITY_FAILED


def _latch_write_gate_failure(code: str) -> None:
    """Disable future writes after a host gate capability fault."""
    global _WRITE_GATE_CAPABILITY_FAILED
    with _WRITE_GATE_CAPABILITY_LOCK:
        if _WRITE_GATE_CAPABILITY_FAILED:
            return
        _WRITE_GATE_CAPABILITY_FAILED = True
    # Keep this record bounded and content-free. The persisted audit sanitizer
    # also drops any metadata that is not an approved scalar field.
    try:
        _audit_safe("skill_publisher.policy_unavailable", result="blocked", code=code)
    except BaseException:
        pass


def _capture_write_gate() -> contextvars.ContextVar | None:
    """Capture and validate the exact host ContextVar used by skill_manage."""
    if _write_gate_failure_latched():
        return None
    try:
        from tools import skill_manager_tool
        gate = getattr(skill_manager_tool, "_skill_gate_bypass", None)
        if not isinstance(gate, contextvars.ContextVar):
            return None
        if not all(callable(getattr(gate, method, None)) for method in ("get", "set", "reset")):
            return None
        return gate
    except BaseException:
        return None


def write_gate_bypass_available() -> bool:
    """Return whether the required Hermes replay-bypass ContextVar is intact."""
    return _capture_write_gate() is not None


def _best_effort_clear_write_gate(gate: contextvars.ContextVar) -> None:
    try:
        gate.set(False)
    except BaseException:
        pass


def _reset_write_gate(gate: contextvars.ContextVar, token: Any) -> None:
    gate.reset(token)


def _handle_write_gate_failure(gate: contextvars.ContextVar, code: str) -> None:
    _best_effort_clear_write_gate(gate)
    _latch_write_gate_failure(code)


def _audit_safe(event: str, **fields: Any) -> None:
    try:
        audit(event, **fields)
    except Exception:
        # Enforcement must never fail open because diagnostics are unavailable.
        pass


def _json_result(value: Any) -> tuple[dict[str, Any] | None, bool]:
    if isinstance(value, dict):
        return dict(value), False
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None, True
        return (dict(parsed), True) if isinstance(parsed, dict) else (None, True)
    return None, False


def _return_result(value: dict[str, Any], as_string: bool) -> Any:
    return json.dumps(value, ensure_ascii=False) if as_string else value


def _rejection(classification, code: str | None = None, message: str | None = None, *, retryable: bool = True) -> str:
    if classification is None:
        assert code is not None and message is not None
        classification = Classification(None, "invalid", "")
    code = code or (
        "skill_publisher.classification_required"
        if classification.status == "missing"
        else "skill_publisher.classification_invalid"
    )
    message = message or (
        "Skill classification is required before creation."
        if classification.status == "missing"
        else f"Skill classification is invalid: {classification.reason}"
    )
    return json.dumps(
        {
            "success": False,
            "error": message,
            "code": code,
            "retryable": retryable,
            "skill_publisher": {
                "field": FIELD,
                "allowed": list(ALLOWED_SCOPES),
                "retry_action": "Retry skill_manage(create) with one allowed string value.",
            },
        },
        ensure_ascii=False,
    )


def _policy_rejection(message: str) -> str:
    return _rejection(None, "skill_publisher.policy_unavailable", message)


def _barrier_rejection(message: str) -> str:
    return json.dumps(
        {
            "success": False,
            "error": message,
            "code": "skill_publisher.transaction_in_progress",
            "retryable": True,
            "skill_publisher": {
                "retry_action": "Resolve the durable transaction (see hermes skill-publisher status/doctor), then retry.",
            },
        },
        ensure_ascii=False,
    )


def _write_approval_rejection() -> str:
    return json.dumps(
        {
            "success": False,
            "error": "skills.write_approval is incompatible with hermes-skill-publisher: approved replay bypasses the publication safety middleware. Disable skills.write_approval or disable the plugin.",
            "code": "skill_publisher.write_approval_incompatible",
            "retryable": False,
            "skill_publisher": {
                "field": "skills.write_approval",
                "retry_action": "Set skills.write_approval to false (or disable hermes-skill-publisher), then retry.",
            },
        },
        ensure_ascii=False,
    )


def _call_downstream(
    args: dict[str, Any],
    next_call: Callable[[dict[str, Any]], Any],
    *,
    force_write: bool = False,
    write_gate: contextvars.ContextVar | None = None,
) -> Any:
    """Call core without permitting a concurrent approval toggle to stage replay.

    The entry check rejects an already-enabled gate. This last-moment check
    catches ordinary config changes; the host ContextVar closes the remaining
    check/use gap so a later toggle executes directly under this middleware
    rather than creating an out-of-band pending replay. ``force_write`` is
    used for the host's operations-array envelope, whose mutation action lives
    inside the payload rather than at the top level.
    """
    if not force_write and not _is_write_action(args.get("action")):
        return next_call(args)
    try:
        if skills_write_approval_enabled():
            return _write_approval_rejection()
    except Exception:
        return _policy_rejection("Skill publication policy is unavailable; the mutation was not attempted.")
    if write_gate is None:
        write_gate = _capture_write_gate()
    if write_gate is None:
        return _policy_rejection("The host skill write-gate compatibility capability is unavailable; the mutation was not attempted.")
    try:
        token = write_gate.set(True)
    except BaseException:
        _handle_write_gate_failure(write_gate, "skill_publisher.write_gate_set_failed")
        return _policy_rejection("Skill publication policy is unavailable; the mutation was not attempted.")
    try:
        result = next_call(args)
    except BaseException:
        try:
            _reset_write_gate(write_gate, token)
        except BaseException:
            _handle_write_gate_failure(write_gate, "skill_publisher.write_gate_reset_failed")
        raise
    try:
        _reset_write_gate(write_gate, token)
    except BaseException:
        # A cleanup fault must not skip managed scope/digest reconciliation.
        # Keep the successful core result and fail closed for later writes.
        _handle_write_gate_failure(write_gate, "skill_publisher.write_gate_reset_failed")
    return result


def _operation_targets_skill_md(operation: dict[str, Any]) -> bool:
    """Return whether a batch operation can change the canonical SKILL.md."""
    action = operation.get("action")
    if action == "create":
        return True
    if action == "patch" and operation.get("content"):
        return True
    if action not in {"patch", "write_file", "remove_file"}:
        return False
    file_path = operation.get("file_path")
    if not file_path:
        return True
    return isinstance(file_path, str) and Path(file_path).name == "SKILL.md"


def _classify_batch_creates(
    operations: tuple[dict[str, Any], ...],
    *,
    required: bool,
    name: Any,
) -> tuple[list[Any], str | None]:
    """Classify every create before the host can execute any batch operation."""
    classifications: list[Any] = []
    for operation in operations:
        if operation.get("action") != "create":
            continue
        try:
            classification = classify_content(operation.get("content"))
        except Exception:
            return classifications, "Skill classification policy failed; creation was not attempted."
        classifications.append(classification)
        if required and not classification.classified:
            _audit_safe(
                "skill_publisher.create_rejected",
                result="blocked",
                error=classification.reason,
                skill_name=name,
                classification=classification.value,
                action="create",
            )
            return classifications, "classification"
    return classifications, None


def _classification_audit_code(classification: Any) -> str:
    return (
        "skill_publisher.classification_missing"
        if classification.status == "missing"
        else "skill_publisher.classification_invalid"
    )


def _annotate_batch(value: Any, classifications: list[Any], *, name: Any = None) -> Any:
    """Keep flat-style local classification guidance for a batch result."""
    for classification in classifications:
        if not classification.classified:
            _audit_safe(
                "skill_publisher.local_unclassified",
                result="local",
                error=classification.reason,
                skill_name=name,
                action="create",
                code=_classification_audit_code(classification),
            )
            return _annotate(value, classification)
    return value


def _annotate(value: Any, classification, *, degraded: str | None = None) -> Any:
    parsed, as_string = _json_result(value)
    if not parsed or parsed.get("success") is not True:
        return value
    code = (
        "skill_publisher.classification_missing"
        if classification.status == "missing"
        else "skill_publisher.classification_invalid"
    )
    message = "The skill remains local and is listed by status/audit."
    if degraded:
        code = "skill_publisher.policy_unavailable"
        message = f"The skill remains local because publisher policy is unavailable: {degraded[:500]}"
    parsed["skill_publisher"] = {
        "status": "local_unclassified" if not degraded else "local_degraded",
        "code": code,
        "message": message,
    }
    return _return_result(parsed, as_string)


def _managed_publication(name: Any) -> dict[str, Any] | None:
    if not isinstance(name, str) or not name:
        return None
    record = load_registry()["publications"].get(name)
    if record is not None:
        # Never act on a structurally unsafe record; StateError fails closed.
        validate_publication(name, record)
    return record


def _atomic_restore(path: Path, original: bytes, expected_current: bytes | None, mode: int) -> None:
    current_stat = None
    if expected_current is None:
        if path.exists() or path.is_symlink():
            raise RuntimeError("SKILL.md reappeared externally; automatic rollback stopped")
    else:
        current_stat = path.lstat()
        if path.is_symlink() or not path.is_file() or path.read_bytes() != expected_current:
            raise RuntimeError("SKILL.md changed externally; automatic rollback stopped")
    temporary = path.parent / f".hermes-skill-publisher-rollback-{secrets.token_hex(8)}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode & 0o777)
    try:
        view = memoryview(original)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    from .filesystem import fsync_dir, rename_noreplace
    if expected_current is None:
        rename_noreplace(temporary, path)
    else:
        assert current_stat is not None
        if path.lstat().st_ino != current_stat.st_ino or path.read_bytes() != expected_current:
            temporary.unlink(missing_ok=True)
            raise RuntimeError("SKILL.md identity changed externally; automatic rollback stopped")
        os.replace(temporary, path)  # managed SKILL.md rollback after identity revalidation
        fsync_dir(path.parent)


def intercept(*, tool_name: str, args: dict[str, Any], next_call: Callable[[dict[str, Any]], Any], **context: Any) -> Any:
    """Deterministic `skill_manage` execution middleware; downstream is called at most once."""
    if tool_name != "skill_manage":
        return next_call(args)
    try:
        is_batch, operations, action, name = _parse_skill_manage_args(args)
    except _BatchShapeError as exc:
        flat = isinstance(args, dict) and "operations" not in args
        blocked_name = args.get("name") if flat else None
        blocked_action = args.get("action") if flat else None
        _audit_safe(
            "skill_publisher.managed_mutation_blocked",
            result="blocked",
            error=str(exc),
            code=("skill_publisher.operation_shape_invalid" if flat else "skill_publisher.batch_unsupported"),
            skill_name=blocked_name if isinstance(blocked_name, str) else None,
            action=blocked_action if isinstance(blocked_action, str) else None,
        )
        return _shape_rejection(str(exc), flat=flat)

    # The host falls through to the core tool when a middleware callback
    # raises before next_call, so the entire policy surface is resolved inside
    # this boundary and every failure returns a fail-closed JSON result.
    write_action = is_batch or _is_write_action(action)
    try:
        required = require_classification_policy()
        approval_gate = skills_write_approval_enabled()
    except Exception as exc:
        _audit_safe("skill_publisher.policy_unavailable", result="blocked", error=str(exc), skill_name=name, action=action, code="skill_publisher.policy_unavailable")
        return _policy_rejection("Skill publication policy is unavailable; the mutation was not attempted.")

    # The private replay-bypass ContextVar is required to close the approval
    # config check/use race. Capture the same object that will set/reset the
    # bypass; host drift must block rather than call core.
    write_gate = None
    if write_action:
        write_gate = _capture_write_gate()
        if write_gate is None:
            _audit_safe("skill_publisher.policy_unavailable", result="blocked", skill_name=name, action=action, code="skill_publisher.write_gate_capability_unavailable")
            return _policy_rejection("The host skill write-gate compatibility capability is unavailable; the mutation was not attempted.")

    # Approved skill writes replay through a gate-bypass path that skips this
    # middleware, so no skill write may even stage while the gate is enabled.
    if approval_gate and write_action:
        _audit_safe("skill_publisher.write_approval_incompatible", result="blocked", skill_name=name, action=action, code="skill_publisher.write_approval_incompatible")
        return _write_approval_rejection()

    # Content-loss guard: core accepts an explicitly EMPTY write_file payload and blanks the
    # target file, so refuse before core when it would destroy existing content.
    if write_action and (overwrite_target := _empty_overwrite_target(operations)) is not None:
        blocked_name, blocked_path = overwrite_target
        _audit_safe(
            "skill_publisher.empty_overwrite_blocked",
            result="blocked",
            error="empty write_file payload would blank an existing non-empty skill file",
            skill_name=blocked_name,
            action="write_file",
            code="skill_publisher.empty_overwrite_blocked",
        )
        return _empty_overwrite_rejection(blocked_name, blocked_path)

    classifications: list[Any] = []
    if is_batch:
        classifications, classification_error = _classify_batch_creates(
            operations, required=required, name=name,
        )
        if classification_error == "classification":
            return _rejection(classifications[-1])
        if classification_error:
            _audit_safe(
                "skill_publisher.policy_unavailable",
                result="blocked",
                error=classification_error,
                skill_name=name,
                action=action,
                code="skill_publisher.policy_unavailable",
            )
            return _policy_rejection(classification_error)

    if not is_batch and action == "create":
        downstream_failed = False
        try:
            classification = classify_content(args.get("content"))
            if required and not classification.classified:
                _audit_safe("skill_publisher.create_rejected", result="blocked", error=classification.reason, skill_name=name, classification=classification.value, action="create")
                return _rejection(classification)
            from .publisher import transaction_barrier
            barrier = transaction_barrier(str(name))
            if barrier:
                _audit_safe("skill_publisher.transaction_in_progress", result="blocked", error=barrier, skill_name=name, action=action, code="skill_publisher.transaction_in_progress")
                return _barrier_rejection(barrier)
            try:
                result = _call_downstream(args, next_call, write_gate=write_gate)
            except BaseException:
                downstream_failed = True
                raise
            if not classification.classified:
                _audit_safe("skill_publisher.local_unclassified", result="local", error=classification.reason, skill_name=name, action="create", code=_classification_audit_code(classification))
                return _annotate(result, classification)
            return result
        except Exception as exc:
            if downstream_failed:
                raise
            _audit_safe("skill_publisher.policy_unavailable", result="blocked", error=str(exc), skill_name=name, action="create", code="skill_publisher.policy_unavailable")
            return _policy_rejection("Skill publication policy is unavailable; creation was not attempted.")

    try:
        publication = _managed_publication(name)
    except Exception:
        # The host would otherwise fail open on a pre-next exception. Block
        # mutations when ownership state cannot be read safely.
        return _policy_rejection("Skill publisher ownership state is unavailable; the mutation was not attempted.")

    try:
        from .publisher import PublisherError, transaction_barrier
    except Exception:
        return _policy_rejection("Skill publisher transaction support is unavailable; the mutation was not attempted.")
    if not publication:
        try:
            barrier = transaction_barrier(str(name))
        except Exception:
            return _policy_rejection("Skill publisher transaction state is unavailable; the mutation was not attempted.")
        if barrier:
            _audit_safe("skill_publisher.transaction_in_progress", result="blocked", error=barrier, skill_name=name, action=action, code="skill_publisher.transaction_in_progress")
            return _barrier_rejection(barrier)
        result = _call_downstream(args, next_call, force_write=is_batch, write_gate=write_gate)
        return _annotate_batch(result, classifications, name=name) if is_batch else result

    downstream_failed = False
    try:
        from .filesystem import acquire_locks
        from .publisher import cleanup_deleted, delete_lock_roots, preflight_delete, update_managed_digest, verify_host_target
        barrier = transaction_barrier(str(name))
        if barrier:
            _audit_safe("skill_publisher.transaction_in_progress", result="blocked", error=barrier, skill_name=name, action=action, code="skill_publisher.transaction_in_progress")
            return _barrier_rejection(barrier)
        config = load_config()
        target = Path(publication["canonical_path"])
        if action == "delete":
            roots = delete_lock_roots(str(name), config)
            # Locks stay held continuously across preflight, exact host-target
            # verification, the downstream delete, and ownership cleanup so a
            # concurrent unpublish cannot move the package in between.
            with acquire_locks(state_lock_path(), roots):
                current = preflight_delete(str(name), config=config, _locked=True)
                if current != publication:
                    raise PublisherError("registry ownership changed before managed delete")
                try:
                    result = _call_downstream(args, next_call, force_write=is_batch, write_gate=write_gate)
                except BaseException:
                    downstream_failed = True
                    raise
                parsed, _ = _json_result(result)
                if parsed and parsed.get("success") is True:
                    cleanup_deleted(str(name), current, config=config, _locked=True)
                return result

        # Serialize every managed host mutation with publication lifecycle
        # moves. Otherwise unpublish could create a local-first shadow after
        # verification but before core resolves the target.
        roots = delete_lock_roots(str(name), config)
        with acquire_locks(state_lock_path(), roots):
            current = preflight_delete(str(name), config=config, _locked=True)
            if current != publication:
                raise PublisherError("registry ownership changed before managed mutation")
            target = Path(current["canonical_path"])
            if target != config.shared_root / str(name):
                raise PublisherError("canonical ownership path drift")
            verify_host_target(str(name), target, config)
            if is_batch:
                protects_skill_md = any(
                    _operation_targets_skill_md(operation) for operation in operations
                )
            else:
                protects_skill_md = action == "edit" or (
                    action in {"patch", "write_file", "remove_file"}
                    and (not args.get("file_path") or Path(str(args.get("file_path"))).name == "SKILL.md")
                )
            original = expected = None
            original_mode = 0
            skill_md = target / "SKILL.md"
            if protects_skill_md:
                info = skill_md.lstat()
                if skill_md.is_symlink() or not skill_md.is_file():
                    raise PublisherError("Managed SKILL.md failed identity validation.")
                original = skill_md.read_bytes()
                original_mode = info.st_mode
            try:
                result = _call_downstream(args, next_call, force_write=is_batch, write_gate=write_gate)
            except BaseException:
                downstream_failed = True
                raise
            parsed, _ = _json_result(result)
            if not parsed or parsed.get("success") is not True:
                return result
            if protects_skill_md:
                expected = skill_md.read_bytes() if skill_md.exists() and not skill_md.is_symlink() else None
                try:
                    validate_skill_file(skill_md, required_scope="shared", directory_name=str(name))
                except Exception as exc:
                    assert original is not None
                    _atomic_restore(skill_md, original, expected, original_mode)
                    _audit_safe("skill_publisher.scope_change_rolled_back", result="blocked", error=str(exc), skill_name=name, action=action, code="skill_publisher.scope_change_requires_unpublish")
                    changed = expected.decode("utf-8", errors="replace") if expected is not None else ""
                    return _rejection(classify_content(changed), "skill_publisher.scope_change_requires_unpublish", "Published SKILL.md must remain valid and shared. Use `hermes skill-publisher unpublish` to change scope.")
            digest_actions = (
                {operation.get("action") for operation in operations}
                if is_batch
                else {action}
            )
            if digest_actions & {"edit", "patch", "write_file", "remove_file"}:
                update_managed_digest(str(name), config=config, _locked=True)
            return _annotate_batch(result, classifications, name=name) if is_batch else result
    except Exception as exc:
        if downstream_failed:
            raise
        code = getattr(exc, "code", "skill_publisher.policy_unavailable")
        _audit_safe("skill_publisher.managed_mutation_blocked", result="blocked", error=str(exc), skill_name=name, action=action, code=code)
        return _rejection(None, code, f"Managed skill mutation was blocked by publisher safety policy: {exc}")


def on_post_tool_call(**kwargs: Any) -> None:
    if kwargs.get("tool_name") != "skill_manage":
        return
    args = kwargs.get("args") if isinstance(kwargs.get("args"), dict) else {}
    name = args.get("name")
    action = args.get("action")
    if "operations" in args:
        try:
            _, operations, _, name = _parse_skill_manage_args(args)
            actions = {operation.get("action") for operation in operations}
            action = next(iter(actions)) if len(actions) == 1 else None
        except _BatchShapeError:
            name = None
            action = None
    # Downstream error text may embed skill content; audit only that an error
    # occurred, never the raw message. Batch operations are summarized without
    # copying any operation payload into the audit record.
    _audit_safe(
        "skill_publisher.tool_observed",
        result=str(kwargs.get("status") or "observed")[:100],
        error="downstream tool error observed" if kwargs.get("error_message") else None,
        session_id=kwargs.get("session_id"),
        skill_name=name,
        action=action if isinstance(action, str) else None,
    )


def _lifecycle_boundary(session_id: Any) -> None:
    if not write_gate_bypass_available():
        _audit_safe(
            "skill_publisher.policy_unavailable",
            result="blocked",
            error="required host write-gate capability is unavailable",
            session_id=session_id,
            code="skill_publisher.write_gate_capability_unavailable",
        )
        return
    if skills_write_approval_enabled():
        _audit_safe(
            "skill_publisher.write_approval_incompatible",
            result="blocked",
            error="lifecycle mutation suspended while skills.write_approval is enabled",
            session_id=session_id,
            code="skill_publisher.write_approval_incompatible",
        )
        return
    from .publisher import publish_pending, reconcile, recover
    findings = recover()
    blocked = [finding for finding in findings if finding.get("result") != "recovered"]
    if blocked:
        # A blocked or unreadable recovery is a global mutation barrier: no
        # reconcile or publication runs until an operator resolves it.
        _audit_safe(
            "skill_publisher.lifecycle_barrier",
            result="blocked",
            error=f"{len(blocked)} blocked durable transaction(s); reconcile and publishing suspended",
            session_id=session_id,
        )
        return
    config = load_config()
    reconcile(config=config)
    publish_pending(config=config)


def on_session_start(**kwargs: Any) -> None:
    if not middleware_available():
        _audit_safe("skill_publisher.middleware_unavailable", result="audit_only", session_id=kwargs.get("session_id"), code="skill_publisher.middleware_unavailable")
        return
    try:
        _lifecycle_boundary(kwargs.get("session_id"))
    except Exception as exc:
        _audit_safe("skill_publisher.lifecycle_blocked", result="blocked", error=str(exc), session_id=kwargs.get("session_id"))


def on_session_end(**kwargs: Any) -> None:
    if not middleware_available():
        return
    completed = kwargs.get("completed") is True
    interrupted = kwargs.get("interrupted") is True
    if not completed or interrupted:
        _audit_safe("skill_publisher.turn_pending", result="deferred", session_id=kwargs.get("session_id"))
        return
    try:
        _lifecycle_boundary(kwargs.get("session_id"))
    except Exception as exc:
        _audit_safe("skill_publisher.lifecycle_blocked", result="blocked", error=str(exc), session_id=kwargs.get("session_id"))


def on_pre_llm_call(**kwargs: Any) -> None:
    """Every-turn recovery boundary; always returns None (observer behavior).

    Current Hermes fires ``on_session_start`` only for a brand-new session, so
    a process restart that resumes a stored session never runs it. There is no
    process-start/resume hook; ``pre_llm_call`` is the strongest supported seam
    that fires before prompt/tool use on every turn, including resumed ones.
    """
    if not middleware_available():
        return None
    try:
        _lifecycle_boundary(kwargs.get("session_id"))
    except Exception as exc:
        _audit_safe("skill_publisher.lifecycle_blocked", result="blocked", error=str(exc), session_id=kwargs.get("session_id"))
    return None


def register(ctx: Any) -> None:
    """Register diagnostics first; missing middleware leaves mutation disabled."""
    global _MIDDLEWARE_AVAILABLE
    from .cli import handle_cli, setup_cli

    ctx.register_cli_command(
        name="skill-publisher",
        help="Inspect and operate shared skill publication",
        setup_fn=setup_cli,
        handler_fn=handle_cli,
        description="Safely publish explicitly shared Agent Skills",
    )
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    register_middleware = getattr(ctx, "register_middleware", None)
    _MIDDLEWARE_AVAILABLE = callable(register_middleware)
    if _MIDDLEWARE_AVAILABLE:
        register_middleware("tool_execution", intercept)
