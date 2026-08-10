"""Operator-only `hermes skill-publisher` CLI."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .config import ConfigError, load_config, skills_write_approval_enabled
from .filesystem import SafetyError, acquire_locks, package_digest, probe_rename_noreplace
from .publisher import (
    PublisherError,
    _inspect_journals,
    _validate_publication_authority,
    discover_local,
    promote,
    status_snapshot,
    unpublish,
)
from .state import StateError, load_registry, read_audit, safe_skill_name, state_lock_path


def setup_cli(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="skill_publisher_command", required=True)
    status = sub.add_parser("status", help="Show managed and pending skills")
    status.add_argument("--json", action="store_true", dest="as_json")
    audit = sub.add_parser("audit", help="Show bounded audit events")
    audit.add_argument("--json", action="store_true", dest="as_json")
    audit.add_argument("--limit", type=int, default=100)
    doctor = sub.add_parser("doctor", help="Check publication readiness")
    doctor.add_argument("--json", action="store_true", dest="as_json")
    publish = sub.add_parser("publish", help="Publish one explicitly shared local skill")
    publish.add_argument("name")
    publish.add_argument("--json", action="store_true", dest="as_json")
    remove = sub.add_parser("unpublish", help="Return a managed skill to this profile")
    remove.add_argument("name")
    remove.add_argument("--scope", choices=("local", "private"), required=True)
    remove.add_argument("--json", action="store_true", dest="as_json")


def _envelope(*, ok: bool, warnings: list[str] | None = None, errors: list[str] | None = None, publications: Any = None, pending: Any = None, **extra: Any) -> dict[str, Any]:
    value = {
        "ok": ok,
        "warnings": warnings or [],
        "errors": errors or [],
        "publications": publications if publications is not None else {},
        "pending": pending if pending is not None else [],
    }
    value.update(extra)
    return value


def _print(value: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
        return
    print("OK" if value.get("ok") else "BLOCKED")
    for warning in value.get("warnings", []):
        print(f"warning: {warning}")
    for error in value.get("errors", []):
        print(f"error: {error}")
    publications = value.get("publications") or {}
    if isinstance(publications, dict):
        for name in sorted(publications):
            print(f"published: {name}")
    for pending in value.get("pending") or []:
        if isinstance(pending, dict):
            print(f"pending: {pending.get('name')} ({pending.get('status') or pending.get('classification')})")
    for event in value.get("events") or []:
        print(f"{event.get('timestamp', '')} {event.get('event', '')} {event.get('result', '')}")


def _doctor() -> tuple[dict[str, Any], int]:
    warnings: list[str] = []
    errors: list[str] = []
    try:
        from .plugin import middleware_available, write_gate_bypass_available
        if not middleware_available():
            errors.append("skill_publisher.middleware_unavailable: host lacks tool_execution middleware")
        if not write_gate_bypass_available():
            errors.append("skill_publisher.write_gate_capability_unavailable: host skill gate bypass capability is unavailable")
        config = load_config()
        if not config.plugin_enabled:
            errors.append("plugin is not listed in plugins.enabled")
        if skills_write_approval_enabled():
            errors.append("skills.write_approval is incompatible: approval replay bypasses tool_execution middleware")
        if config.unknown_keys:
            warnings.append(f"{len(config.unknown_keys)} unknown plugin config key(s)")
        probe_rename_noreplace(config.shared_root)
        probe_rename_noreplace(config.local_root if config.local_root.is_dir() else config.hermes_home)
        with acquire_locks(state_lock_path(), [config.shared_root, *config.adapter_roots.values()], timeout=0.25):
            registry = load_registry()
            journal_entries = _inspect_journals(config, registry)
        for entry in journal_entries:
            if not entry["valid"]:
                errors.append(f"{entry['operation_id']}: {entry['code']}")
        owned_hidden = {
            str(path)
            for entry in journal_entries
            if entry["valid"]
            for key in ("stage_path", "backup_path")
            if isinstance((path := entry["journal"].get(key)), str)
        }
        for index, (name, record) in enumerate(registry["publications"].items(), 1):
            try:
                publication, _ = _validate_publication_authority(config, name, record)
            except Exception:
                errors.append(f"registry-record-{index}: skill_publisher.registry_invalid")
                continue
            target = Path(publication["canonical_path"])
            try:
                if package_digest(target) != publication["digest"]:
                    errors.append(f"{name}: canonical digest drift")
            except Exception:
                errors.append(f"{name}: canonical package unavailable")
            configured_adapters = {label: root / name for label, root in config.adapter_roots.items()}
            for label, adapter in publication["adapter_links"].items():
                path = Path(adapter["path"])
                if label not in configured_adapters or path != configured_adapters[label]:
                    warnings.append(f"{name}: stale owned adapter requires explicit unpublish cleanup")
                try:
                    if not path.is_symlink() or os.readlink(path) != adapter["link_text"]:
                        errors.append(f"{name}: adapter ownership drift")
                except OSError:
                    errors.append(f"{name}: adapter unreadable")
        scan_roots = [config.local_root, config.shared_root, *config.adapter_roots.values()]
        for root in scan_roots:
            for directory, subdirs, files in os.walk(root, followlinks=False):
                all_names = [*subdirs, *files]
                subdirs[:] = [item for item in subdirs if not (Path(directory) / item).is_symlink()]
                for item in all_names:
                    if item.startswith((".hermes-skill-publisher-stage-", ".hermes-skill-publisher-backup-")):
                        hidden = str(Path(directory) / item)
                        if hidden not in owned_hidden:
                            errors.append("unknown hidden transaction artifact under a managed root")
        for path, classification in discover_local(config):
            if not classification.classified:
                warnings.append(f"{safe_skill_name(path.name) or 'invalid-skill-name'}: classification is {classification.status}")
        if journal_entries:
            warnings.append(f"{len(journal_entries)} durable transaction(s) await recovery")
        summaries = status_snapshot(config=config)
        result = _envelope(ok=not errors, warnings=warnings, errors=errors, publications=summaries["publications"], pending=[], transactions=summaries["transactions"])
        return result, 0 if not errors else 2
    except (ConfigError, SafetyError, StateError, TimeoutError, OSError) as exc:
        errors.append(f"skill_publisher.doctor_blocked:{type(exc).__name__}")
        return _envelope(ok=False, warnings=warnings, errors=errors), 2


def handle_cli(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "as_json", False))
    command = args.skill_publisher_command
    try:
        if command == "audit":
            if args.limit < 0:
                raise PublisherError("--limit must be non-negative")
            events = read_audit(args.limit)
            value = _envelope(ok=True, events=events)
            _print(value, as_json)
            return 0
        if command == "doctor":
            value, code = _doctor()
            _print(value, as_json)
            return code
        if command == "status":
            snapshot = status_snapshot()
            warnings = [
                f"{item['name']}: {item['reason']}"
                for item in snapshot["pending"]
                if item["status"] != "classified"
            ]
            value = _envelope(ok=True, warnings=warnings, publications=snapshot["publications"], pending=snapshot["pending"], transactions=snapshot["transactions"])
            _print(value, as_json)
            return 0

        from .plugin import middleware_available, write_gate_bypass_available
        if not middleware_available():
            raise PublisherError("skill_publisher.middleware_unavailable: mutations require tool_execution middleware")
        if not write_gate_bypass_available():
            raise PublisherError("skill_publisher.write_gate_capability_unavailable: host skill gate bypass capability is unavailable")
        config = load_config()
        if skills_write_approval_enabled():
            raise PublisherError(
                "skill_publisher.write_approval_incompatible: disable skills.write_approval before publication mutations"
            )
        if command == "publish":
            matches = [(path, classification) for path, classification in discover_local(config) if path.name == args.name]
            if len(matches) != 1:
                raise PublisherError(f"expected one local skill named {args.name!r}; found {len(matches)}")
            path, classification = matches[0]
            if classification.value != "shared" or not classification.classified:
                raise PublisherError(f"manual publish requires explicit shared metadata: {classification.reason or classification.value}")
            publication = promote(path, config=config)
            value = _envelope(ok=True, publications={args.name: publication})
        elif command == "unpublish":
            result = unpublish(args.name, args.scope, config=config)
            value = _envelope(ok=True, pending=[result])
        else:
            raise RuntimeError(f"unknown command: {command}")
        _print(value, as_json)
        return 0
    except (PublisherError, ConfigError, SafetyError, StateError, TimeoutError, ValueError) as exc:
        message = f"skill_publisher.{command}_blocked" if command in {"status", "audit"} else str(exc)
        _print(_envelope(ok=False, errors=[message]), as_json)
        return 2
    except Exception as exc:
        message = f"skill_publisher.{command}_failed:{type(exc).__name__}" if command in {"status", "audit"} else f"unexpected failure: {type(exc).__name__}"
        _print(_envelope(ok=False, errors=[message]), as_json)
        return 1
