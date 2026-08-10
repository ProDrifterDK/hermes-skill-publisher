"""Hermes registration, tool middleware, and lifecycle callbacks."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
from typing import Any, Callable

from .config import load_config, require_classification_policy, skills_write_approval_enabled
from .frontmatter import ALLOWED_SCOPES, FIELD, Classification, classify_content, validate_skill_file
from .state import audit, load_registry, state_lock_path, validate_publication

_MIDDLEWARE_AVAILABLE = False

# Actions that mutate skill state and therefore pass through the host
# write-approval gate when it is enabled.
_WRITE_ACTIONS = {"create", "edit", "patch", "delete", "write_file", "remove_file"}


def middleware_available() -> bool:
    return _MIDDLEWARE_AVAILABLE


def write_gate_bypass_available() -> bool:
    """Return whether the required Hermes replay-bypass ContextVar is intact."""
    try:
        from tools import skill_manager_tool
        gate = getattr(skill_manager_tool, "_skill_gate_bypass", None)
    except Exception:
        return False
    return all(callable(getattr(gate, method, None)) for method in ("get", "set", "reset"))


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


def _call_downstream(args: dict[str, Any], next_call: Callable[[dict[str, Any]], Any]) -> Any:
    """Call core without permitting a concurrent approval toggle to stage replay.

    The entry check rejects an already-enabled gate. This last-moment check
    catches ordinary config changes; the host ContextVar closes the remaining
    check/use gap so a later toggle executes directly under this middleware
    rather than creating an out-of-band pending replay.
    """
    if args.get("action") not in _WRITE_ACTIONS:
        return next_call(args)
    if skills_write_approval_enabled():
        return _write_approval_rejection()
    if not write_gate_bypass_available():
        return _policy_rejection("The host skill write-gate compatibility capability is unavailable; the mutation was not attempted.")
    from tools.skill_manager_tool import _skill_gate_bypass
    token = _skill_gate_bypass.set(True)
    try:
        return next_call(args)
    finally:
        _skill_gate_bypass.reset(token)


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
    action = args.get("action")
    name = args.get("name")

    # The host falls through to the core tool when a middleware callback
    # raises before next_call, so the entire policy surface is resolved inside
    # this boundary and every failure returns a fail-closed JSON result.
    try:
        required = require_classification_policy()
        approval_gate = skills_write_approval_enabled()
    except Exception as exc:
        _audit_safe("skill_publisher.policy_unavailable", result="blocked", error=str(exc), skill_name=name, action=action, code="skill_publisher.policy_unavailable")
        return _policy_rejection("Skill publication policy is unavailable; the mutation was not attempted.")

    # The private replay-bypass ContextVar is required to close the approval
    # config check/use race. Host drift must block rather than call core.
    if action in _WRITE_ACTIONS and not write_gate_bypass_available():
        _audit_safe("skill_publisher.policy_unavailable", result="blocked", skill_name=name, action=action, code="skill_publisher.write_gate_capability_unavailable")
        return _policy_rejection("The host skill write-gate compatibility capability is unavailable; the mutation was not attempted.")

    # Approved skill writes replay through a gate-bypass path that skips this
    # middleware, so no skill write may even stage while the gate is enabled.
    if approval_gate and action in _WRITE_ACTIONS:
        _audit_safe("skill_publisher.write_approval_incompatible", result="blocked", skill_name=name, action=action, code="skill_publisher.write_approval_incompatible")
        return _write_approval_rejection()

    if action == "create":
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
                result = _call_downstream(args, next_call)
            except BaseException:
                downstream_failed = True
                raise
            if not classification.classified:
                _audit_safe("skill_publisher.local_unclassified", result="local", error=classification.reason, skill_name=name, action="create", code=("skill_publisher.classification_missing" if classification.status == "missing" else "skill_publisher.classification_invalid"))
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
        return _call_downstream(args, next_call)

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
                    result = _call_downstream(args, next_call)
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
                result = _call_downstream(args, next_call)
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
            if action in {"edit", "patch", "write_file", "remove_file"}:
                update_managed_digest(str(name), config=config, _locked=True)
            return result
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
    # Downstream error text may embed skill content; audit only that an error
    # occurred, never the raw message.
    _audit_safe(
        "skill_publisher.tool_observed",
        result=str(kwargs.get("status") or "observed")[:100],
        error="downstream tool error observed" if kwargs.get("error_message") else None,
        session_id=kwargs.get("session_id"),
        skill_name=args.get("name"),
        action=args.get("action"),
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
