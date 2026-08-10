"""Transactional promotion, recovery, reconciliation, and unpublish."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import stat
from typing import Any

from .config import ConfigError, PublisherConfig, load_config
from .filesystem import (
    SafetyError,
    acquire_locks,
    create_relative_symlink,
    ensure_absent,
    ensure_real_directory,
    fsync_dir,
    package_digest,
    relative_link_text,
    remove_owned_symlink,
    rename_noreplace,
    safe_copy_tree,
    safe_remove_tree,
)
from .frontmatter import FrontmatterError, classify_content, rewrite_scope_bytes, validate_skill_file
from .state import (
    SCHEMA_VERSION,
    StateError,
    audit,
    journals_for_recovery,
    load_registry,
    operation_id,
    remove_journal,
    safe_operation_id,
    safe_skill_name,
    save_registry,
    state_lock_path,
    validate_publication,
    write_journal,
)


class PublisherError(RuntimeError):
    def __init__(self, message: str, code: str = "skill_publisher.safety_blocker") -> None:
        super().__init__(message)
        self.code = code


def _audit_safe(event: str, **fields: Any) -> None:
    try:
        audit(event, **fields)
    except Exception:
        pass


def _exists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _is_symlink(path: Path) -> bool:
    try:
        return stat.S_ISLNK(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def _result_success(result: Any) -> bool:
    if isinstance(result, dict):
        return result.get("success") is True
    if isinstance(result, str):
        import json
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            return False
        return isinstance(parsed, dict) and parsed.get("success") is True
    return False


def _ancestors_real(root: Path, relative: Path) -> bool:
    """True when every existing ancestor of ``root / relative`` below root is a real directory."""
    current = root
    for part in relative.parent.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return True
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return False
    return True


def _source_relpath(source: Path, local_root: Path) -> str:
    try:
        relative = source.relative_to(local_root)
    except ValueError as exc:
        raise PublisherError("source is outside the active local skills root") from exc
    if source.is_symlink() or any(part in {"", ".", ".."} for part in relative.parts):
        raise PublisherError("source path is not a real package below the local root")
    if local_root.exists() and local_root.is_symlink():
        raise PublisherError("local skills root must not be a symlink")
    if not _ancestors_real(local_root, relative):
        raise PublisherError("source ancestors below the local root must be real directories")
    return relative.as_posix()


def transaction_barrier(name: str) -> str | None:
    """Block on any invalid journal, or a valid journal for this skill."""
    config = load_config(validate_roots=False, require_authorized=False)
    registry = load_registry()
    entries = _inspect_journals(config, registry)
    if any(not entry["valid"] for entry in entries):
        return "an invalid durable transaction exists; operator resolution is required"
    if any(entry["journal"]["name"] == name for entry in entries):
        return "a durable transaction for this skill still exists; recovery must finish first"
    return None


def resolve_host_skill_path(name: str, config: PublisherConfig) -> Path | None:
    """Resolve the exact path current Hermes would act on for this skill name.

    Hermes searches the local skills root first, then configured external
    roots. The host lookup is used verbatim when importable so local-shadow
    and external-duplicate detection matches real tool behavior.
    """
    try:
        from tools.skill_manager_tool import _find_skill
        found = _find_skill(name)
        return Path(found["path"]) if found else None
    except ImportError:
        pass
    try:
        from agent.skill_utils import get_all_skills_dirs
        roots = [Path(path) for path in get_all_skills_dirs()]
    except ImportError:
        roots = [config.local_root, config.shared_root]
    for root in roots:
        if not root.is_dir():
            continue
        for skill_md in root.rglob("SKILL.md"):
            if skill_md.parent.name == name:
                return skill_md.parent
    return None


def verify_host_target(name: str, canonical: Path, config: PublisherConfig) -> None:
    """Block when the host would act on anything but the managed canonical path."""
    resolved = resolve_host_skill_path(name, config)
    if resolved is None:
        raise PublisherError(f"the host cannot resolve managed skill {name!r}; refusing mutation")
    if os.path.realpath(resolved) != os.path.realpath(canonical):
        raise PublisherError(f"a local or external duplicate shadows the managed canonical package: {resolved}")


def _lock_roots(config: PublisherConfig) -> list[Path]:
    return [config.shared_root, *config.adapter_roots.values()]


def _expected_adapters(config: PublisherConfig, name: str) -> dict[str, dict[str, str]]:
    result = {}
    target = config.shared_root / name
    for label, root in sorted(config.adapter_roots.items()):
        path = root / name
        result[label] = {"path": str(path), "link_text": relative_link_text(target, root)}
    return result


def _safe_relpath(raw: Any) -> Path:
    if not isinstance(raw, str):
        raise PublisherError("journal/source relative path must be a string")
    relative = Path(raw)
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise PublisherError("journal/source relative path is unsafe")
    return relative


_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_PROMOTE_PHASES = frozenset({"planned", "staged", "source_parked", "target_committed", "adapters_committed", "registry_committed", "committed"})
_UNPUBLISH_PHASES = frozenset({"planned", "staged", "source_parked", "target_committed", "adapters_committed", "registry_committed"})


def _validate_publication_authority(
    config: PublisherConfig,
    name: Any,
    record: Any,
) -> tuple[dict[str, Any], list[Path]]:
    """Validate paths and adapter payloads before using a record as authority."""
    try:
        publication = validate_publication(name, record)
    except StateError as exc:
        raise PublisherError("publication record is structurally invalid") from exc
    target = config.shared_root / name
    if Path(publication["canonical_path"]) != target:
        raise PublisherError("publication canonical path is outside the active shared root")
    current_roots = set(config.adapter_roots.values())
    parents: list[Path] = []
    for adapter in publication["adapter_links"].values():
        path = Path(adapter["path"])
        parent = path.parent
        ensure_real_directory(parent)
        if adapter["link_text"] != relative_link_text(target, parent):
            raise PublisherError("publication adapter payload does not derive from the canonical target")
        if parent not in current_roots:
            # Compatibility contract for adapters removed from config: every
            # previously managed root has the persistent lock file created by
            # the original publication lock acquisition. Never create it here.
            marker = parent / ".hermes-skill-publisher.lock"
            try:
                marker_info = marker.lstat()
            except FileNotFoundError as exc:
                raise PublisherError("out-of-config adapter root lacks prior ownership marker") from exc
            if stat.S_ISLNK(marker_info.st_mode) or not stat.S_ISREG(marker_info.st_mode):
                raise PublisherError("out-of-config adapter ownership marker is invalid")
        if parent not in parents:
            parents.append(parent)
    return publication, parents


def _validate_journal(
    config: PublisherConfig,
    journal: dict[str, Any],
    registry: dict[str, Any],
) -> list[Path]:
    """Deep-validate a journal and every durable authority it references."""
    name = journal.get("name")
    op_id = journal.get("operation_id")
    if safe_skill_name(name) is None:
        raise PublisherError("journal skill name is invalid")
    if safe_operation_id(op_id) is None:
        raise PublisherError("journal operation id is invalid")
    if journal.get("schema_version") != SCHEMA_VERSION:
        raise PublisherError("journal schema is invalid")
    operation = journal.get("operation")
    phase = journal.get("phase")
    target = config.shared_root / name
    if Path(str(journal.get("target_path", ""))) != target:
        raise PublisherError("journal target is outside the active shared root")

    publication, adapter_parents = _validate_publication_authority(config, name, journal.get("publication"))
    if publication["adapter_links"] != journal.get("adapters"):
        raise PublisherError("journal adapters do not match publication ownership")

    if operation == "promote":
        if phase not in _PROMOTE_PHASES:
            raise PublisherError("promotion journal phase is invalid")
        relative = _safe_relpath(journal.get("source_relpath"))
        if relative.name != name:
            raise PublisherError("journal source does not match the skill name")
        source = config.local_root / relative
        expected_stage = config.shared_root / f".hermes-skill-publisher-stage-{op_id}"
        expected_backup = source.parent / f".hermes-skill-publisher-backup-{op_id}"
        if Path(str(journal.get("source_path", ""))) != source:
            raise PublisherError("journal source is outside the active local root")
        if Path(str(journal.get("stage_path", ""))) != expected_stage or Path(str(journal.get("backup_path", ""))) != expected_backup:
            raise PublisherError("journal hidden paths do not match this operation")
        expected_adapters = _expected_adapters(config, name)
        if journal.get("adapters") != expected_adapters:
            raise PublisherError("promotion journal adapters do not match active configured roots")
        if publication["source_relpath"] != relative.as_posix() or publication["digest"] != journal.get("digest"):
            raise PublisherError("promotion journal publication identity is invalid")
        created = journal.get("created_adapters")
        if not isinstance(created, list) or len(created) != len(set(created)) or any(label not in expected_adapters for label in created):
            raise PublisherError("promotion journal created-adapter proof is invalid")
    elif operation == "unpublish":
        if phase not in _UNPUBLISH_PHASES:
            raise PublisherError("unpublish journal phase is invalid")
        raw_relpath = journal.get("local_relpath")
        if raw_relpath is None:
            legacy = Path(str(journal.get("local_path", "")))
            try:
                raw_relpath = legacy.relative_to(config.local_root).as_posix()
            except ValueError as exc:
                raise PublisherError("journal local destination is outside the active local root") from exc
        relative = _safe_relpath(raw_relpath)
        if relative.name != name:
            raise PublisherError("journal local destination does not match the skill name")
        if config.local_root.exists() and config.local_root.is_symlink():
            raise PublisherError("local skills root must not be a symlink")
        if not _ancestors_real(config.local_root, relative):
            raise PublisherError("journal local ancestors must be real directories")
        local = config.local_root / relative
        if Path(str(journal.get("local_path", ""))) != local:
            raise PublisherError("journal local destination is not the rederived safe path")
        expected_stage = local.parent / f".hermes-skill-publisher-stage-{op_id}"
        expected_backup = config.shared_root / f".hermes-skill-publisher-backup-{op_id}"
        if Path(str(journal.get("stage_path", ""))) != expected_stage or Path(str(journal.get("backup_path", ""))) != expected_backup:
            raise PublisherError("journal hidden paths do not match this operation")
        if journal.get("scope") not in {"local", "private"}:
            raise PublisherError("journal unpublish scope is invalid")
        canonical_digest = journal.get("canonical_digest")
        local_digest = journal.get("local_digest")
        if not isinstance(canonical_digest, str) or _DIGEST_RE.fullmatch(canonical_digest) is None or canonical_digest != publication["digest"]:
            raise PublisherError("journal canonical digest is invalid")
        if local_digest is not None and (not isinstance(local_digest, str) or _DIGEST_RE.fullmatch(local_digest) is None):
            raise PublisherError("journal local digest is invalid")
        owned = registry.get("publications", {}).get(name)
        if owned is not None:
            _validate_publication_authority(config, name, owned)
            if publication != owned:
                raise PublisherError("journal publication does not match registry ownership")
        elif phase not in {"adapters_committed", "registry_committed"} or not _exists(local):
            raise PublisherError("unpublish journal lacks matching registry ownership")
    else:
        raise PublisherError("journal operation is invalid")
    return adapter_parents


def _inspect_journals(config: PublisherConfig, registry: dict[str, Any]) -> list[dict[str, Any]]:
    """Use the deep validator for recovery, barriers, status, and doctor."""
    entries: list[dict[str, Any]] = []
    for index, (journal, _op_id, _parse_error) in enumerate(journals_for_recovery(), 1):
        fallback_id = f"transaction-{index}"
        if journal is None:
            entries.append({"valid": False, "operation_id": fallback_id, "code": "skill_publisher.journal_unreadable", "journal": None, "roots": []})
            continue
        try:
            roots = _validate_journal(config, journal, registry)
        except Exception:
            entries.append({"valid": False, "operation_id": fallback_id, "code": "skill_publisher.journal_invalid", "journal": None, "roots": []})
            continue
        entries.append({
            "valid": True,
            "operation_id": journal["operation_id"],
            "code": None,
            "journal": journal,
            "roots": roots,
            "summary": {
                "operation_id": journal["operation_id"],
                "operation": journal["operation"],
                "phase": journal["phase"],
                "name": journal["name"],
                "status": "valid",
            },
        })
    return entries


def _preflight_adapters(
    expected: dict[str, dict[str, str]],
    *,
    owned: dict[str, dict[str, str]] | None = None,
) -> None:
    owned = owned or {}
    for label, adapter in expected.items():
        path = Path(adapter["path"])
        if not _exists(path):
            continue
        record = owned.get(label)
        if not record or record != adapter:
            raise PublisherError(f"unmanaged adapter object exists: {path}", "skill_publisher.adapter_conflict")
        if not _is_symlink(path) or os.readlink(path) != adapter["link_text"]:
            raise PublisherError(f"owned adapter changed externally: {path}", "skill_publisher.adapter_conflict")


def _publication(name: str, source_relpath: str, digest: str, target: Path, adapters: dict[str, dict[str, str]]) -> dict[str, Any]:
    return {
        "canonical_path": str(target),
        "source_relpath": source_relpath,
        "digest": digest,
        "scope": "shared",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "adapter_links": adapters,
    }


def _journal_phase(journal: dict[str, Any], phase: str, **updates: Any) -> None:
    journal.update(updates)
    journal["phase"] = phase
    write_journal(journal)


def _remove_created_adapters(journal: dict[str, Any]) -> None:
    expected = journal.get("adapters", {})
    for label in reversed(journal.get("created_adapters", [])):
        adapter = expected.get(label)
        if adapter:
            remove_owned_symlink(Path(adapter["path"]), adapter["link_text"])


def _rollback_promotion(journal: dict[str, Any], registry: dict[str, Any] | None = None) -> None:
    source = Path(journal["source_path"])
    backup = Path(journal["backup_path"])
    stage = Path(journal["stage_path"])
    target = Path(journal["target_path"])
    digest = journal["digest"]
    # Full-state preflight before the first mutation: never choose between
    # duplicates, never touch a changed object, and never cross durable
    # registry ownership (the point of no return), even when deauthorized.
    if _exists(source) and _exists(backup):
        raise PublisherError("source and journal backup both exist; rollback is ambiguous")
    if _exists(source) and _exists(target):
        raise PublisherError("source and canonical target both exist; rollback is ambiguous")
    if _exists(target) and package_digest(target) != digest:
        raise PublisherError("canonical target changed; rollback stopped")
    if _exists(stage) and package_digest(stage) != digest:
        raise PublisherError("journal stage digest differs; rollback stopped")
    if _exists(backup) and package_digest(backup) != digest:
        raise PublisherError("local backup changed; rollback stopped")
    if registry is not None and registry["publications"].get(journal["name"]) is not None:
        raise PublisherError("durable registry ownership is the point of no return; rollback refused")
    _remove_created_adapters(journal)
    if _exists(target):
        ensure_absent(stage)
        rename_noreplace(target, stage)
    if _exists(backup):
        ensure_absent(source)
        rename_noreplace(backup, source)
    if _exists(stage):
        safe_remove_tree(stage, expected_digest=digest)
    remove_journal(journal["operation_id"])


def promote(source: Path, *, config: PublisherConfig | None = None) -> dict[str, Any]:
    """Promote one explicitly shared local package using a durable journal."""
    config = config or load_config()
    source = Path(source)
    name = source.name
    source_relpath = _source_relpath(source, config.local_root)
    if not name or "/" in name or name in {".", ".."}:
        raise PublisherError("invalid skill directory name")
    with acquire_locks(state_lock_path(), _lock_roots(config)):
        barrier = transaction_barrier(name)
        if barrier:
            raise PublisherError(barrier, "skill_publisher.transaction_in_progress")
        registry = load_registry()
        existing_record = registry["publications"].get(name)
        target = config.shared_root / name
        if existing_record:
            existing_record, _ = _validate_publication_authority(config, name, existing_record)
            if _exists(source):
                raise PublisherError("local source and managed canonical target both exist; refusing to choose")
            if existing_record.get("canonical_path") != str(target) or not _exists(target):
                raise PublisherError("registry ownership drift blocks promotion")
            current = package_digest(target)
            if current != existing_record.get("digest"):
                raise PublisherError("managed canonical package digest drift")
            reconcile(config=config, _locked=True)
            return existing_record

        validate_skill_file(source / "SKILL.md", required_scope="shared", directory_name=name)
        digest = package_digest(source)
        ensure_absent(target)
        adapters = _expected_adapters(config, name)
        _preflight_adapters(adapters)
        op_id = operation_id(name)
        stage = config.shared_root / f".hermes-skill-publisher-stage-{op_id}"
        backup = source.parent / f".hermes-skill-publisher-backup-{op_id}"
        ensure_absent(stage)
        ensure_absent(backup)
        publication = _publication(name, source_relpath, digest, target, adapters)
        journal: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "operation_id": op_id,
            "operation": "promote",
            "phase": "planned",
            "name": name,
            "digest": digest,
            "source_path": str(source),
            "source_relpath": source_relpath,
            "backup_path": str(backup),
            "stage_path": str(stage),
            "target_path": str(target),
            "adapters": adapters,
            "created_adapters": [],
            "publication": publication,
        }
        write_journal(journal)
        try:
            # A concurrent local write can land between staging and parking.
            # Rehash the parked backup before committing; on drift restore the
            # newest source and restage. The stale stage is never committed.
            for _attempt in range(3):
                digest = package_digest(source)
                if digest != journal["digest"]:
                    publication = _publication(name, source_relpath, digest, target, adapters)
                    journal["digest"] = digest
                    journal["publication"] = publication
                    _journal_phase(journal, "planned")
                staged_digest = safe_copy_tree(source, stage)
                validate_skill_file(stage / "SKILL.md", required_scope="shared", directory_name=name)
                if staged_digest != digest:
                    raise PublisherError("source changed during staging")
                _journal_phase(journal, "staged")

                rename_noreplace(source, backup)
                _journal_phase(journal, "source_parked")

                if package_digest(backup) == digest:
                    break
                rename_noreplace(backup, source)
                safe_remove_tree(stage, expected_digest=digest)
            else:
                raise PublisherError("local source kept changing during promotion")

            ensure_absent(target)
            rename_noreplace(stage, target)
            if package_digest(target) != digest:
                raise PublisherError("committed target digest mismatch")
            _journal_phase(journal, "target_committed")

            for label, adapter in adapters.items():
                def record_created(text: str, *, current_label: str = label) -> None:
                    if text != adapter["link_text"]:
                        raise PublisherError("adapter payload mismatch")
                    journal["created_adapters"].append(current_label)
                    _journal_phase(journal, "target_committed")

                create_relative_symlink(target, Path(adapter["path"]), on_created=record_created)
            _journal_phase(journal, "adapters_committed")

            registry["publications"][name] = publication
            save_registry(registry)
            _journal_phase(journal, "registry_committed")

            safe_remove_tree(backup, expected_digest=digest)
            _journal_phase(journal, "committed")
            remove_journal(op_id)
            _audit_safe("skill_publisher.promoted", result="success", operation_id=op_id, skill_name=name, classification="shared", source_relpath=source_relpath)
            return publication
        except Exception as exc:
            # Registry ownership is the point of no return. Once durable, leave
            # canonical state in place and let recovery finish backup/journal cleanup.
            try:
                durable = load_registry()["publications"].get(name) == publication
                durable = durable and _exists(target) and package_digest(target) == digest
            except Exception:
                durable = False
            if durable:
                _audit_safe("skill_publisher.commit_cleanup_pending", result="recovery_required", error=str(exc), operation_id=op_id, skill_name=name)
                return publication
            try:
                _rollback_promotion(journal, load_registry())
                outcome = "rolled_back"
            except Exception as rollback_exc:
                outcome = "recovery_required"
                _audit_safe("skill_publisher.rollback_blocked", result=outcome, error=f"{exc}; rollback: {rollback_exc}", operation_id=op_id, skill_name=name)
                raise PublisherError(f"promotion failed and rollback is blocked: {rollback_exc}") from exc
            _audit_safe("skill_publisher.promotion_failed", result=outcome, error=str(exc), operation_id=op_id, skill_name=name)
            if isinstance(exc, PublisherError):
                raise
            if isinstance(exc, (SafetyError, FrontmatterError, OSError)):
                raise PublisherError(str(exc)) from exc
            raise


def discover_local(config: PublisherConfig | None = None) -> list[tuple[Path, Any]]:
    config = config or load_config()
    root = config.local_root
    if not root.exists() or root.is_symlink() or not root.is_dir():
        return []
    skill_files: list[Path] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError:
            continue
        skill_entry = next((entry for entry in entries if entry.name == "SKILL.md"), None)
        if skill_entry is not None:
            try:
                info = skill_entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                skill_files.append(Path(skill_entry.path))
            continue
        for entry in reversed(entries):
            if entry.name.startswith(".hermes-skill-publisher-"):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                stack.append(Path(entry.path))
    found = []
    for skill_md in sorted(skill_files):
        package = skill_md.parent
        try:
            relative = package.relative_to(root)
        except ValueError:
            continue
        try:
            classification = classify_content(skill_md.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            continue
        found.append((package, classification))
    return found


def publish_pending(*, config: PublisherConfig | None = None) -> dict[str, list[dict[str, Any]]]:
    config = config or load_config()
    promoted: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for path, classification in discover_local(config):
        if classification.value != "shared" or not classification.classified:
            continue
        try:
            promoted.append(promote(path, config=config))
        except Exception as exc:
            blocked.append({"name": path.name, "error": str(exc)[:1000]})
            audit("skill_publisher.pending_blocked", result="blocked", error=str(exc), skill_name=path.name, classification="shared", source_relpath=str(path.relative_to(config.local_root)))
    return {"promoted": promoted, "blocked": blocked}


def _finish_promotion(journal: dict[str, Any], registry: dict[str, Any]) -> None:
    name = journal["name"]
    source = Path(journal["source_path"])
    backup = Path(journal["backup_path"])
    stage = Path(journal["stage_path"])
    target = Path(journal["target_path"])
    digest = journal["digest"]
    current_registry = registry["publications"].get(name)
    if current_registry is not None and current_registry != journal.get("publication"):
        raise PublisherError("registry ownership differs from promotion journal")

    if _exists(source) and _exists(backup):
        raise PublisherError("source and journal backup both exist; recovery is ambiguous")
    if _exists(source) and _exists(target):
        raise PublisherError("source and canonical target both exist; recovery is ambiguous")
    if _exists(target) and package_digest(target) != digest:
        if _exists(backup) and not _exists(source) and package_digest(backup) == digest:
            rename_noreplace(backup, source)
        raise PublisherError("canonical target digest differs; external target left untouched")
    if _exists(stage) and package_digest(stage) != digest:
        raise PublisherError("journal stage digest differs")
    if _exists(backup) and package_digest(backup) != digest:
        raise PublisherError("journal backup digest differs")

    if _exists(source) and _exists(stage) and not _exists(backup) and not _exists(target):
        if package_digest(source) != digest:
            raise PublisherError("source changed while stage exists")
        safe_remove_tree(stage, expected_digest=digest)
        remove_journal(journal["operation_id"])
        return
    if _exists(source) and not _exists(backup) and not _exists(target):
        if _exists(stage):
            safe_remove_tree(stage, expected_digest=digest)
        remove_journal(journal["operation_id"])
        return
    if _exists(backup) and not _exists(target):
        if _exists(stage):
            rename_noreplace(stage, target)
        elif current_registry is not None:
            # Durable registry ownership is the point of no return. The backup
            # is the only surviving package copy, so restore the canonical side.
            rename_noreplace(backup, target)
        else:
            # Rollback must durably remove every journal-owned adapter before
            # discarding the only ownership proof.
            _remove_created_adapters(journal)
            rename_noreplace(backup, source)
            remove_journal(journal["operation_id"])
            return
    if not _exists(target):
        raise PublisherError("transaction has no recoverable source, target, or stage")

    created = set(journal.get("created_adapters", []))
    already_owned = registry["publications"].get(name)
    for label, adapter in journal.get("adapters", {}).items():
        path = Path(adapter["path"])
        if _exists(path):
            if not _is_symlink(path) or os.readlink(path) != adapter["link_text"]:
                raise PublisherError(f"adapter changed during recovery: {path}")
            registry_proves = already_owned is not None and already_owned.get("adapter_links", {}).get(label) == adapter
            if label not in created and not registry_proves:
                raise PublisherError(f"unowned existing adapter cannot be adopted during recovery: {path}")
        else:
            def record_created(text: str, *, current_label: str = label) -> None:
                if text != adapter["link_text"]:
                    raise PublisherError("adapter payload mismatch during recovery")
                created.add(current_label)
                journal["created_adapters"] = sorted(created)
                _journal_phase(journal, "target_committed")

            create_relative_symlink(target, path, on_created=record_created)
        created.add(label)
        journal["created_adapters"] = sorted(created)
        _journal_phase(journal, "target_committed")
    registry["publications"][name] = journal["publication"]
    save_registry(registry)
    _journal_phase(journal, "registry_committed")
    if _exists(backup):
        safe_remove_tree(backup, expected_digest=digest)
    if _exists(stage):
        safe_remove_tree(stage, expected_digest=digest)
    remove_journal(journal["operation_id"])


def _finish_unpublish(journal: dict[str, Any], registry: dict[str, Any]) -> None:
    name = journal["name"]
    target = Path(journal["target_path"])
    backup = Path(journal["backup_path"])
    local = Path(journal["local_path"])
    stage = Path(journal["stage_path"])
    canonical_digest = journal["canonical_digest"]
    local_digest = journal.get("local_digest")
    scope = journal.get("scope")
    current_registry = registry["publications"].get(name)
    if current_registry is not None and current_registry != journal.get("publication"):
        raise PublisherError("registry ownership differs from unpublish journal")
    if current_registry is None and not _exists(local):
        raise PublisherError("registry ownership disappeared before unpublish committed")

    # Verify every extant object before any mutation. Changed hidden objects
    # are never renamed into a live namespace, and mismatches keep evidence.
    target_exists = _exists(target)
    backup_exists = _exists(backup)
    local_exists = _exists(local)
    stage_exists = _exists(stage)
    if target_exists and local_exists:
        raise PublisherError("canonical and local packages both exist; recovery is ambiguous")
    if target_exists and backup_exists:
        raise PublisherError("canonical target and backup both exist; recovery is ambiguous")
    if local_exists and stage_exists:
        raise PublisherError("local destination and stage both exist; recovery is ambiguous")
    if target_exists and package_digest(target) != canonical_digest:
        raise PublisherError("canonical target digest differs; recovery stopped")
    if _exists(backup) and package_digest(backup) != canonical_digest:
        raise PublisherError("canonical backup digest differs; recovery stopped")
    if _exists(local) and (not isinstance(local_digest, str) or package_digest(local) != local_digest):
        raise PublisherError("local recovered package changed")
    if _exists(stage):
        if isinstance(local_digest, str):
            if package_digest(stage) != local_digest:
                raise PublisherError("local stage digest differs; recovery stopped")
            validate_skill_file(stage / "SKILL.md", required_scope=scope, directory_name=name)
        else:
            # Staging was interrupted before its digest was journaled; only a
            # pristine pre-rewrite copy of the canonical package is verifiably
            # journal-owned and safe to discard.
            if package_digest(stage) != canonical_digest:
                raise PublisherError("unpublish stage is unverifiable; recovery stopped")
            safe_remove_tree(stage, expected_digest=canonical_digest)

    if _exists(local):
        owned = registry["publications"].get(name)
        if owned is not None:
            if owned.get("adapter_links", {}) != journal.get("adapters", {}):
                raise PublisherError("registry adapters changed during unpublish recovery")
            for adapter in journal.get("adapters", {}).values():
                remove_owned_symlink(Path(adapter["path"]), adapter["link_text"])
        registry["publications"].pop(name, None)
        save_registry(registry)
        if _exists(backup):
            safe_remove_tree(backup, expected_digest=canonical_digest)
        if _exists(stage):
            safe_remove_tree(stage, expected_digest=local_digest)
        remove_journal(journal["operation_id"])
        return
    if _exists(backup) and _exists(stage) and not _exists(target):
        rename_noreplace(stage, local)
        return _finish_unpublish(journal, registry)
    if _exists(backup) and not _exists(target):
        rename_noreplace(backup, target)
        remove_journal(journal["operation_id"])
        return
    if _exists(target) and _exists(stage):
        safe_remove_tree(stage, expected_digest=local_digest if isinstance(local_digest, str) else canonical_digest)
        remove_journal(journal["operation_id"])
        return
    if _exists(target) and not _exists(backup) and not _exists(stage):
        # planned phase: nothing was staged or parked yet.
        remove_journal(journal["operation_id"])
        return
    raise PublisherError("unpublish transaction cannot be recovered safely")


def recover(*, config: PublisherConfig | None = None) -> list[dict[str, str]]:
    authorized = True
    if config is None:
        try:
            config = load_config()
        except ConfigError:
            config = load_config(require_authorized=False)
            authorized = False
    findings: list[dict[str, str]] = []
    # Hold state continuously while deriving every validated recorded adapter
    # parent, then acquire the complete resource set in global sorted order.
    with acquire_locks(state_lock_path(), []):
        registry = load_registry()
        initial = _inspect_journals(config, registry)
        if any(not entry["valid"] for entry in initial):
            for entry in initial:
                code = entry["code"] or "skill_publisher.journal_barrier"
                findings.append({"operation_id": entry["operation_id"], "result": "blocked", "error": code})
                _audit_safe("skill_publisher.recovery_blocked", result="blocked", error=code, operation_id=entry["operation_id"], code=code)
            return findings

        roots = list(_lock_roots(config))
        for entry in initial:
            for root in entry["roots"]:
                if root not in roots:
                    roots.append(root)
        with acquire_locks(None, roots):
            # Re-read and deep-revalidate after every required root lock is held.
            registry = load_registry()
            current = _inspect_journals(config, registry)
            if len(current) != len(initial) or any(
                not now["valid"] or now["journal"] != before["journal"]
                for before, now in zip(initial, current)
            ):
                return [{"operation_id": "transaction-state", "result": "blocked", "error": "skill_publisher.journal_changed"}]
            for entry in current:
                journal = entry["journal"]
                assert journal is not None
                try:
                    operation = journal["operation"]
                    if not authorized:
                        if operation != "promote":
                            raise PublisherError("shared root authorization was removed; unpublish recovery is blocked")
                        _rollback_promotion(journal, registry)
                    elif operation == "promote":
                        _finish_promotion(journal, registry)
                    else:
                        _finish_unpublish(journal, registry)
                    findings.append({"operation_id": journal["operation_id"], "result": "recovered"})
                    _audit_safe("skill_publisher.recovered", result="success", operation_id=journal["operation_id"], skill_name=journal["name"])
                except Exception:
                    findings.append({"operation_id": journal["operation_id"], "result": "blocked", "error": "skill_publisher.recovery_blocked"})
                    _audit_safe("skill_publisher.recovery_blocked", result="blocked", error="details withheld", operation_id=journal["operation_id"], skill_name=journal["name"])
    return findings


def reconcile(*, config: PublisherConfig | None = None, _locked: bool = False) -> list[dict[str, str]]:
    config = config or load_config()
    findings: list[dict[str, str]] = []

    def checked(name: Any, record: Any) -> dict[str, Any] | None:
        try:
            publication, _ = _validate_publication_authority(config, name, record)
        except Exception:
            findings.append({"name": safe_skill_name(name) or "invalid-skill-name", "result": "blocked", "error": "registry record is unsafe"})
            return None
        return publication

    def run() -> None:
        registry = load_registry()
        journal_entries = _inspect_journals(config, registry)
        if any(not entry["valid"] for entry in journal_entries):
            raise PublisherError("an invalid durable transaction exists; operator resolution is required")
        journal_names = {entry["journal"]["name"] for entry in journal_entries}
        changed = False
        for name, record in list(registry["publications"].items()):
            if name in journal_names:
                findings.append({"name": name, "result": "deferred", "error": "durable transaction recovery pending"})
                continue
            publication = checked(name, record)
            if publication is None:
                continue
            target = Path(publication["canonical_path"])
            adapters = publication["adapter_links"]
            if not _exists(target):
                # Make the observed canonical deletion durable before any
                # ownership proof (adapter links, registry record) is removed.
                fsync_dir(config.shared_root)
                conflict = False
                for adapter in adapters.values():
                    path = Path(adapter["path"])
                    if _exists(path):
                        try:
                            remove_owned_symlink(path, adapter["link_text"])
                        except SafetyError as exc:
                            conflict = True
                            findings.append({"name": name, "result": "blocked", "error": str(exc)})
                if not conflict:
                    registry["publications"].pop(name)
                    changed = True
                    findings.append({"name": name, "result": "deleted_cleanup"})
                continue
            try:
                current = package_digest(target)
            except SafetyError as exc:
                findings.append({"name": name, "result": "blocked", "error": str(exc)})
                continue
            if current != publication.get("digest"):
                findings.append({"name": name, "result": "drift", "error": "canonical digest changed"})
                continue
            for label, adapter in adapters.items():
                path = Path(adapter["path"])
                if not _exists(path):
                    try:
                        create_relative_symlink(target, path)
                        findings.append({"name": name, "result": f"adapter_repaired:{label}"})
                    except Exception as exc:
                        findings.append({"name": name, "result": "blocked", "error": str(exc)})
                elif not _is_symlink(path) or os.readlink(path) != adapter["link_text"]:
                    findings.append({"name": name, "result": "blocked", "error": f"adapter changed: {path}"})
        if changed:
            save_registry(registry)

    if _locked:
        run()
    else:
        roots = list(_lock_roots(config))
        try:
            preview = load_registry()
        except StateError:
            preview = {"publications": {}}
        # Recorded stale adapter parents must be locked before any mutation,
        # but only after their structural validation and real-directory check.
        for rec_name, record in preview.get("publications", {}).items():
            try:
                _, parents = _validate_publication_authority(config, rec_name, record)
            except Exception:
                continue
            for parent in parents:
                if parent not in roots:
                    roots.append(parent)
        with acquire_locks(state_lock_path(), roots):
            run()
    return findings


def delete_lock_roots(name: str, config: PublisherConfig) -> list[Path]:
    """Roots that must stay locked across a managed delete and its cleanup."""
    roots = list(_lock_roots(config))
    publication = load_registry()["publications"].get(name)
    if publication is not None:
        _, parents = _validate_publication_authority(config, name, publication)
        for parent in parents:
            if parent not in roots:
                roots.append(parent)
    return roots


def preflight_delete(name: str, *, config: PublisherConfig | None = None, _locked: bool = False) -> dict[str, Any] | None:
    config = config or load_config()

    def run() -> dict[str, Any] | None:
        barrier = transaction_barrier(name)
        if barrier:
            raise PublisherError(barrier, "skill_publisher.transaction_in_progress")
        registry = load_registry()
        publication = registry["publications"].get(name)
        if not publication:
            return None
        publication, _ = _validate_publication_authority(config, name, publication)
        target = Path(publication["canonical_path"])
        verify_host_target(name, target, config)
        expected = publication.get("adapter_links", {})
        _preflight_adapters(expected, owned=expected)
        return publication

    if _locked:
        return run()
    with acquire_locks(state_lock_path(), _lock_roots(config)):
        return run()


def cleanup_deleted(name: str, publication: dict[str, Any], *, config: PublisherConfig | None = None, _locked: bool = False) -> None:
    config = config or load_config()

    def run() -> None:
        registry = load_registry()
        current = registry["publications"].get(name)
        if current != publication:
            raise PublisherError("registry changed during delete cleanup")
        _validate_publication_authority(config, name, current)
        if _exists(Path(publication["canonical_path"])):
            raise PublisherError("canonical package still exists after delete")
        # Hermes core removed the canonical tree without fsyncing the shared
        # root. Make that deletion durable before ownership proof is removed.
        fsync_dir(config.shared_root)
        for adapter in publication.get("adapter_links", {}).values():
            remove_owned_symlink(Path(adapter["path"]), adapter["link_text"])
        registry["publications"].pop(name)
        save_registry(registry)
        audit("skill_publisher.deleted", result="success", skill_name=name)

    if _locked:
        run()
        return
    _, recorded_parents = _validate_publication_authority(config, name, publication)
    roots = list(_lock_roots(config))
    for parent in recorded_parents:
        if parent not in roots:
            roots.append(parent)
    with acquire_locks(state_lock_path(), roots):
        run()


def update_managed_digest(name: str, *, config: PublisherConfig | None = None, _locked: bool = False) -> None:
    config = config or load_config()

    def run() -> None:
        registry = load_registry()
        publication = registry["publications"].get(name)
        if not publication:
            return
        publication, _ = _validate_publication_authority(config, name, publication)
        target = Path(publication["canonical_path"])
        validate_skill_file(target / "SKILL.md", required_scope="shared", directory_name=name)
        publication["digest"] = package_digest(target)
        save_registry(registry)

    if _locked:
        run()
    else:
        with acquire_locks(state_lock_path(), _lock_roots(config)):
            run()


def unpublish(name: str, scope: str, *, config: PublisherConfig | None = None) -> dict[str, Any]:
    if scope not in {"local", "private"}:
        raise PublisherError("unpublish scope must be local or private")
    config = config or load_config()
    preview_registry = load_registry()
    preview = preview_registry["publications"].get(name)
    if not preview:
        raise PublisherError(f"skill is not owned by this profile: {name}")
    preview, owned_roots = _validate_publication_authority(config, name, preview)
    with acquire_locks(state_lock_path(), [*_lock_roots(config), *owned_roots]):
        barrier = transaction_barrier(name)
        if barrier:
            raise PublisherError(barrier, "skill_publisher.transaction_in_progress")
        registry = load_registry()
        publication = registry["publications"].get(name)
        if publication != preview:
            raise PublisherError("registry ownership changed before unpublish")
        publication, _ = _validate_publication_authority(config, name, publication)
        target = Path(publication["canonical_path"])
        if target != config.shared_root / name or not _exists(target):
            raise PublisherError("canonical ownership drift blocks unpublish")
        validate_skill_file(target / "SKILL.md", required_scope="shared", directory_name=name)
        canonical_digest = package_digest(target)
        if canonical_digest != publication.get("digest"):
            raise PublisherError("canonical digest drift blocks unpublish")
        adapters = publication["adapter_links"]
        _preflight_adapters(adapters, owned=adapters)

        relative = _safe_relpath(publication.get("source_relpath", name))
        if config.local_root.exists() and config.local_root.is_symlink():
            raise PublisherError("local skills root must not be a symlink")
        local = config.local_root / relative
        if not _ancestors_real(config.local_root, relative) or not local.parent.is_dir():
            local = config.local_root / name
        local_relative = local.relative_to(config.local_root)
        local.parent.mkdir(parents=True, exist_ok=True)
        ensure_real_directory(local.parent)
        ensure_absent(local)
        op_id = operation_id(name)
        stage = local.parent / f".hermes-skill-publisher-stage-{op_id}"
        backup = config.shared_root / f".hermes-skill-publisher-backup-{op_id}"
        ensure_absent(stage)
        ensure_absent(backup)
        journal = {
            "schema_version": SCHEMA_VERSION,
            "operation_id": op_id,
            "operation": "unpublish",
            "phase": "planned",
            "name": name,
            "scope": scope,
            "target_path": str(target),
            "backup_path": str(backup),
            "local_path": str(local),
            "local_relpath": local_relative.as_posix(),
            "stage_path": str(stage),
            "canonical_digest": canonical_digest,
            "local_digest": None,
            "adapters": adapters,
            "publication": publication,
        }
        # Durable ownership before any hidden staging exists; a stranded stage
        # without a journal could never be identified or cleaned safely.
        write_journal(journal)
        try:
            safe_copy_tree(target, stage)
            rewritten = rewrite_scope_bytes((stage / "SKILL.md").read_bytes(), scope)
            skill_md = stage / "SKILL.md"
            fd = os.open(skill_md, os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0))
            try:
                view = memoryview(rewritten)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            validate_skill_file(skill_md, required_scope=scope, directory_name=name)
            local_digest = package_digest(stage)
            _journal_phase(journal, "staged", local_digest=local_digest)
            rename_noreplace(target, backup)
            _journal_phase(journal, "source_parked")
            rename_noreplace(stage, local)
            _journal_phase(journal, "target_committed")
            for adapter in adapters.values():
                remove_owned_symlink(Path(adapter["path"]), adapter["link_text"])
            _journal_phase(journal, "adapters_committed")
            registry["publications"].pop(name)
            save_registry(registry)
            _journal_phase(journal, "registry_committed")
            safe_remove_tree(backup, expected_digest=canonical_digest)
            remove_journal(op_id)
            _audit_safe("skill_publisher.unpublished", result="success", operation_id=op_id, skill_name=name, classification=scope)
            return {"name": name, "scope": scope, "path": str(local), "digest": local_digest}
        except Exception as exc:
            local_digest = journal.get("local_digest")
            # Registry removal is the point of no return. Once durable, leave
            # local state in place and let recovery finish backup/journal cleanup.
            try:
                durable = name not in load_registry()["publications"]
                durable = durable and isinstance(local_digest, str) and _exists(local) and package_digest(local) == local_digest and not _exists(target)
            except Exception:
                durable = False
            if durable:
                _audit_safe("skill_publisher.unpublish_cleanup_pending", result="recovery_required", error=str(exc), operation_id=op_id, skill_name=name)
                return {"name": name, "scope": scope, "path": str(local), "digest": local_digest}
            # Conservative inverse rollback; retain the journal if any identity is ambiguous.
            try:
                current_before_rollback = load_registry()["publications"].get(name)
                if current_before_rollback != publication:
                    raise PublisherError("registry ownership changed before rollback")
                if _exists(local):
                    if not isinstance(local_digest, str) or package_digest(local) != local_digest:
                        raise PublisherError("local destination changed during rollback")
                    rename_noreplace(local, stage)
                if _exists(backup):
                    if package_digest(backup) != canonical_digest:
                        raise PublisherError("canonical backup changed during rollback")
                    rename_noreplace(backup, target)
                for adapter in adapters.values():
                    path = Path(adapter["path"])
                    if not _exists(path):
                        create_relative_symlink(target, path)
                    elif not _is_symlink(path) or os.readlink(path) != adapter["link_text"]:
                        raise PublisherError("adapter changed during rollback")
                if _exists(stage):
                    if isinstance(local_digest, str):
                        safe_remove_tree(stage, expected_digest=local_digest)
                    else:
                        safe_remove_tree(stage, expected_digest=canonical_digest)
                current_registry = load_registry()
                current = current_registry["publications"].get(name)
                if current != publication:
                    raise PublisherError("registry ownership changed during rollback")
                remove_journal(op_id)
            except Exception as rollback_exc:
                _audit_safe("skill_publisher.unpublish_rollback_blocked", result="recovery_required", error=f"{exc}; rollback: {rollback_exc}", operation_id=op_id, skill_name=name)
                raise PublisherError(f"unpublish failed and rollback is blocked: {rollback_exc}") from exc
            raise PublisherError(str(exc)) from exc


def status_snapshot(*, config: PublisherConfig | None = None) -> dict[str, Any]:
    config = config or load_config(validate_roots=False, require_authorized=False)
    registry = load_registry()
    publications: dict[str, Any] = {}
    for index, (name, record) in enumerate(registry["publications"].items(), 1):
        try:
            publication, _ = _validate_publication_authority(config, name, record)
        except Exception:
            publications[f"invalid-record-{index}"] = {
                "status": "invalid",
                "code": "skill_publisher.registry_invalid",
            }
            continue
        publications[name] = {
            "status": "managed",
            "scope": "shared",
            "digest": publication["digest"],
            "adapter_count": len(publication["adapter_links"]),
        }

    pending = []
    for path, classification in discover_local(config):
        name = safe_skill_name(path.name) or "invalid-skill-name"
        status = classification.status if classification.status in {"classified", "missing", "invalid"} else "invalid"
        pending.append({
            "name": name,
            "classification": classification.value if classification.value in {"shared", "local", "private"} else None,
            "status": status,
            "reason": "classification is valid" if status == "classified" else f"classification is {status}",
        })

    transactions = []
    for entry in _inspect_journals(config, registry):
        if entry["valid"]:
            transactions.append(entry["summary"])
        else:
            transactions.append({
                "operation_id": entry["operation_id"],
                "status": "invalid",
                "code": entry["code"],
            })
    return {"publications": publications, "pending": pending, "transactions": transactions}
