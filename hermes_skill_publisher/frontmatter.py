"""Strict skill frontmatter parsing and portable-package validation.

Diagnostics from this module are safe to persist: parser failures report only
stable categories and bounded line/column positions, never source content.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Mapping

import yaml

ALLOWED_SCOPES = ("shared", "local", "private")
FIELD = "metadata.skill-publisher-scope"
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FRONTMATTER_RE = re.compile(rb"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)


class FrontmatterError(ValueError):
    """The skill frontmatter or package is unsafe or non-portable."""


@dataclass(frozen=True)
class Classification:
    value: str | None
    status: str
    reason: str

    @property
    def classified(self) -> bool:
        return self.status == "classified"


def _load_yaml_mapping(text: str) -> dict[str, Any]:
    """Parse frontmatter YAML without ever leaking source lines into errors."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            raise FrontmatterError(
                f"invalid YAML frontmatter at line {mark.line + 1}, column {mark.column + 1}"
            ) from exc
        raise FrontmatterError(f"invalid YAML frontmatter ({type(exc).__name__})") from exc
    except Exception as exc:
        # Deeply nested documents can raise RecursionError and friends; report
        # only the exception category, never parser state or source content.
        raise FrontmatterError(f"invalid YAML frontmatter ({type(exc).__name__})") from exc
    if not isinstance(data, dict):
        raise FrontmatterError("frontmatter must be a mapping")
    return data


def parse_document_bytes(content: bytes) -> tuple[dict[str, Any], bytes, bytes]:
    """Return parsed frontmatter, the raw body bytes, and the frontmatter span.

    The body is never newline-translated or re-encoded, so callers can splice
    it back byte-for-byte.
    """
    if not isinstance(content, (bytes, bytearray)):
        raise FrontmatterError("SKILL.md content must be bytes")
    raw = bytes(content)
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        raise FrontmatterError("SKILL.md must start with YAML frontmatter")
    try:
        text = match.group(1).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FrontmatterError("frontmatter must be valid UTF-8") from exc
    return _load_yaml_mapping(text), raw[match.end():], raw[: match.end()]


def parse_document(content: str) -> tuple[dict[str, Any], str, str]:
    """Return parsed frontmatter, body, and original frontmatter span."""
    if not isinstance(content, str):
        raise FrontmatterError("SKILL.md content must be a string")
    frontmatter, body, span = parse_document_bytes(content.encode("utf-8"))
    return frontmatter, body.decode("utf-8"), span.decode("utf-8")


def _classify_frontmatter(frontmatter: Mapping[str, Any]) -> Classification:
    metadata = frontmatter.get("metadata")
    if metadata is None:
        return Classification(None, "missing", f"{FIELD} is missing")
    if not isinstance(metadata, dict):
        return Classification(None, "invalid", "metadata must be a mapping")
    if "skill-publisher-scope" not in metadata:
        return Classification(None, "missing", f"{FIELD} is missing")
    value = metadata["skill-publisher-scope"]
    if not isinstance(value, str):
        return Classification(None, "invalid", f"{FIELD} must be a string")
    if value not in ALLOWED_SCOPES:
        return Classification(None, "invalid", f"{FIELD} must be one of: {', '.join(ALLOWED_SCOPES)}")
    return Classification(value, "classified", "")


def classify_content(content: Any) -> Classification:
    """Classify supplied SKILL.md text; never raises and never echoes content."""
    if not isinstance(content, str):
        return Classification(None, "invalid", "content must be a string")
    try:
        frontmatter, _, _ = parse_document(content)
    except Exception as exc:
        reason = str(exc) if isinstance(exc, FrontmatterError) else f"frontmatter parser failure ({type(exc).__name__})"
        return Classification(None, "invalid", reason)
    return _classify_frontmatter(frontmatter)


def validate_frontmatter(frontmatter: Mapping[str, Any], directory_name: str) -> None:
    name = frontmatter.get("name")
    if (
        not isinstance(name, str)
        or name != directory_name
        or not 1 <= len(name) <= 64
        or _NAME_RE.fullmatch(name) is None
    ):
        raise FrontmatterError("name must match the directory and satisfy Agent Skills constraints")
    description = frontmatter.get("description")
    if not isinstance(description, str) or not 1 <= len(description) <= 1024:
        raise FrontmatterError("description must be a non-empty string of at most 1024 characters")
    if "license" in frontmatter and not isinstance(frontmatter["license"], str):
        raise FrontmatterError("license must be a string")
    if "compatibility" in frontmatter:
        compatibility = frontmatter["compatibility"]
        if not isinstance(compatibility, str) or not 1 <= len(compatibility) <= 500:
            raise FrontmatterError("compatibility must be a string of 1 to 500 characters")
    metadata = frontmatter.get("metadata")
    if not isinstance(metadata, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in metadata.items()
    ):
        raise FrontmatterError("metadata must map string keys to string values")
    if "allowed-tools" in frontmatter and not isinstance(frontmatter["allowed-tools"], str):
        raise FrontmatterError("allowed-tools must be a string")


def validate_skill_file(
    skill_md: Path,
    *,
    required_scope: str | None = None,
    directory_name: str | None = None,
) -> dict[str, Any]:
    try:
        content = skill_md.read_bytes()
    except OSError as exc:
        raise FrontmatterError(f"cannot read SKILL.md: {exc}") from exc
    frontmatter, _, _ = parse_document_bytes(content)
    validate_frontmatter(frontmatter, directory_name or skill_md.parent.name)
    classification = _classify_frontmatter(frontmatter)
    if not classification.classified:
        raise FrontmatterError(classification.reason)
    if required_scope is not None and classification.value != required_scope:
        raise FrontmatterError(f"{FIELD} must be {required_scope}")
    return frontmatter


def rewrite_scope_bytes(content: bytes, scope: str) -> bytes:
    """Rewrite only the YAML scope field; the Markdown body is spliced back raw."""
    if scope not in {"local", "private"}:
        raise FrontmatterError("unpublish scope must be local or private")
    frontmatter, body, _ = parse_document_bytes(content)
    metadata = frontmatter.get("metadata")
    if not isinstance(metadata, dict):
        raise FrontmatterError("metadata must be a mapping")
    metadata["skill-publisher-scope"] = scope
    dumped = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).rstrip()
    return b"---\n" + dumped.encode("utf-8") + b"\n---\n" + body


def rewrite_scope(content: str, scope: str) -> str:
    frontmatter, body, _ = parse_document(content)
    metadata = frontmatter.get("metadata")
    if not isinstance(metadata, dict):
        raise FrontmatterError("metadata must be a mapping")
    metadata["skill-publisher-scope"] = scope
    dumped = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).rstrip()
    return f"---\n{dumped}\n---\n{body}"
