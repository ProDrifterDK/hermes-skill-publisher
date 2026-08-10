"""Runtime configuration resolution. This module never writes Hermes config."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

import yaml

PLUGIN_ID = "hermes-skill-publisher"
_ALLOWED_KEYS = {"require_classification", "shared_root", "adapter_roots"}


class ConfigError(ValueError):
    """Configuration blocks safe mutation."""


@dataclass(frozen=True)
class PublisherConfig:
    hermes_home: Path
    local_root: Path
    shared_root: Path
    adapter_roots: dict[str, Path]
    require_classification: bool
    unknown_keys: tuple[str, ...] = ()
    plugin_enabled: bool = False


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home()).expanduser().resolve()
    except ImportError:
        raw = os.environ.get("HERMES_HOME")
        if not raw:
            raise ConfigError("HERMES_HOME is unavailable outside Hermes")
        return Path(raw).expanduser().resolve()


def _read_config(home: Path) -> dict[str, Any]:
    path = home / "config.yaml"
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        raise ConfigError(f"active config contains invalid YAML{location}") from exc
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"cannot read active config ({type(exc).__name__})") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError("active config must be a mapping")
    return loaded


def _resolve_path(raw: str, home: Path) -> Path:
    expanded = Path(os.path.expanduser(os.path.expandvars(raw)))
    if not expanded.is_absolute():
        expanded = home / expanded
    # Normalize lexical components without following a configured symlink;
    # root validation must be able to reject that symlink explicitly.
    return Path(os.path.abspath(expanded))


def _real_directory(path: Path, label: str) -> None:
    try:
        if path.is_symlink():
            raise ConfigError(f"{label} must not be a symlink: {path}")
        if not path.is_dir():
            raise ConfigError(f"{label} must be a preexisting directory: {path}")
        if path.resolve(strict=True) != path:
            raise ConfigError(f"{label} must resolve to itself: {path}")
    except OSError as exc:
        raise ConfigError(f"cannot validate {label}: {exc}") from exc


def _overlap(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def require_classification_policy() -> bool:
    """Read only the enforcement bit; malformed policy always fails closed."""
    home = hermes_home()
    raw_config = _read_config(home)
    plugins = raw_config.get("plugins", {})
    if plugins is None:
        plugins = {}
    if not isinstance(plugins, dict):
        raise ConfigError("plugins config must be a mapping")
    entries = plugins.get("entries", {})
    if entries is None:
        entries = {}
    if not isinstance(entries, dict):
        raise ConfigError("plugins.entries must be a mapping")
    entry = entries.get(PLUGIN_ID, {})
    if entry is None:
        entry = {}
    if not isinstance(entry, dict):
        raise ConfigError(f"plugins.entries.{PLUGIN_ID} must be a mapping")
    value = entry.get("require_classification", False)
    if not isinstance(value, bool):
        raise ConfigError("require_classification must be a boolean")
    return value


_HOST_TRUTHY_APPROVAL = frozenset({"on", "true", "yes", "1", "approve", "enabled"})


def skills_write_approval_enabled() -> bool:
    """Mirror Hermes' write-approval normalization for supported values.

    Hermes accepts booleans and strips/case-folds strings before checking its
    truthy set. Other value types are unsupported here and block publication
    rather than being mistaken for approval-off.
    """
    home = hermes_home()
    raw_config = _read_config(home)
    skills = raw_config.get("skills", {})
    if skills is None:
        return False
    if not isinstance(skills, dict):
        raise ConfigError("skills config must be a mapping")
    value = skills.get("write_approval", False)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in _HOST_TRUTHY_APPROVAL
    raise ConfigError("skills.write_approval must be a boolean or string")


def load_config(*, validate_roots: bool = True, require_authorized: bool = True) -> PublisherConfig:
    home = hermes_home()
    raw_config = _read_config(home)
    plugins = raw_config.get("plugins", {})
    if plugins is None:
        plugins = {}
    if not isinstance(plugins, dict):
        raise ConfigError("plugins config must be a mapping")
    enabled = plugins.get("enabled", [])
    if isinstance(enabled, str):
        enabled = [enabled]
    plugin_enabled = isinstance(enabled, list) and PLUGIN_ID in enabled
    entries = plugins.get("entries", {})
    if entries is None:
        entries = {}
    if not isinstance(entries, dict):
        raise ConfigError("plugins.entries must be a mapping")
    entry = entries.get(PLUGIN_ID, {})
    if entry is None:
        entry = {}
    if not isinstance(entry, dict):
        raise ConfigError(f"plugins.entries.{PLUGIN_ID} must be a mapping")

    require_classification = entry.get("require_classification", False)
    if not isinstance(require_classification, bool):
        raise ConfigError("require_classification must be a boolean")
    shared_raw = entry.get("shared_root", "~/.agents/skills")
    if not isinstance(shared_raw, str) or not shared_raw.strip():
        raise ConfigError("shared_root must be a non-empty path string")
    adapters_raw = entry.get("adapter_roots", {})
    if not isinstance(adapters_raw, dict):
        raise ConfigError("adapter_roots must be a mapping")
    adapters: dict[str, Path] = {}
    for label, raw in adapters_raw.items():
        if not isinstance(label, str) or not label.strip():
            raise ConfigError("adapter labels must be non-empty strings")
        if not isinstance(raw, str) or not raw.strip():
            raise ConfigError(f"adapter root {label!r} must be a non-empty path string")
        if label in adapters:
            raise ConfigError(f"duplicate adapter label: {label}")
        adapters[label] = _resolve_path(raw, home)

    shared = _resolve_path(shared_raw, home)
    result = PublisherConfig(
        hermes_home=home,
        local_root=home / "skills",
        shared_root=shared,
        adapter_roots=adapters,
        require_classification=require_classification,
        unknown_keys=tuple(sorted(set(entry) - _ALLOWED_KEYS)),
        plugin_enabled=plugin_enabled,
    )
    if validate_roots:
        _real_directory(shared, "shared_root")
        for label, root in adapters.items():
            _real_directory(root, f"adapter_roots.{label}")
        roots = [("shared_root", shared), *[(f"adapter_roots.{k}", v) for k, v in adapters.items()]]
        for index, (label, root) in enumerate(roots):
            for other_label, other in roots[index + 1:]:
                if _overlap(root, other):
                    raise ConfigError(f"{label} overlaps {other_label}")
    if require_authorized:
        try:
            from agent.skill_utils import get_external_skills_dirs
            authorized = {Path(path).resolve() for path in get_external_skills_dirs()}
        except ImportError:
            raw_dirs = raw_config.get("skills", {}).get("external_dirs", []) if isinstance(raw_config.get("skills", {}), dict) else []
            if isinstance(raw_dirs, str):
                raw_dirs = [raw_dirs]
            authorized = {
                _resolve_path(item, home)
                for item in raw_dirs
                if isinstance(item, str) and _resolve_path(item, home).is_dir()
            }
        except Exception as exc:
            raise ConfigError(f"cannot resolve skills.external_dirs: {exc}") from exc
        if shared not in authorized:
            raise ConfigError("shared_root must be an existing resolved entry in skills.external_dirs")
    return result
