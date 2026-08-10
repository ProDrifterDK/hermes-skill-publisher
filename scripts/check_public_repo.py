#!/usr/bin/env python3
"""Fail when public release inputs contain private data or generated artifacts."""

from __future__ import annotations

import ast
from pathlib import Path
import re
import subprocess
import sys
import tomllib
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {
    "plugin.yaml", "__init__.py", "pyproject.toml", "README.md", "LICENSE",
    "CHANGELOG.md", "RELEASE_NOTES.md", ".gitignore",
    "docs/ARCHITECTURE.md", "docs/SAFETY.md", "docs/LIMITATIONS.md",
    "docs/OPERATIONS.md", "docs/EXAMPLES.md", ".github/workflows/test.yml",
    "scripts/check_public_repo.py",
    "hermes_skill_publisher/__init__.py", "hermes_skill_publisher/plugin.py",
    "hermes_skill_publisher/config.py", "hermes_skill_publisher/frontmatter.py",
    "hermes_skill_publisher/filesystem.py", "hermes_skill_publisher/state.py",
    "hermes_skill_publisher/publisher.py", "hermes_skill_publisher/cli.py",
}
GENERATED_PARTS = {
    "__pycache__", ".pytest_cache", ".codegraph", ".gitnexus", ".pi-subagents",
    "plugin-data", "htmlcov", "build", "dist", ".mypy_cache", ".ruff_cache",
    ".venv", "venv", ".hermes-skill-publisher-state",
}
GENERATED_NAMES = {".coverage", "coverage.xml", ".coveragerc"}
PRIVATE_WORDS = ("Re" + "syst", "CM" + "PC", "Alan" + " Gárate", "Vault" + " Sync")
HOME_PATH = re.compile(r"/(?:home|Users)/[^/\s)]+/")
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")
ACTION_USE = re.compile(r"\buses:\s*[^@\s]+@([^\s#]+)")
HERMES_CHECKOUT = re.compile(r"repository:\s*NousResearch/hermes-agent(?:(?!\n\s*-\s+uses:).)*?\n\s*ref:\s*([^\s#]+)", re.DOTALL)
PINNED_HERMES_MINIMUM = "d25e2dbdbc40b49808c0a0e9cfed21cc90cffab3"
SECRET_PATTERNS = [
    re.compile("AK" + "IA[0-9A-Z]{16}"),
    re.compile("gh" + "p_[A-Za-z0-9]{20,}"),
    re.compile("-----BEGIN " + "(?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
    re.compile(r"(?i)(?:api[_-]?key|secret|token|password)\s*[:=]\s*['\"][A-Za-z0-9_./+=-]{12,}['\"]"),
]
TEXT_SUFFIXES = {".py", ".md", ".toml", ".yaml", ".yml", ".json", ".txt"}


def is_generated(relative: Path) -> bool:
    if any(part in GENERATED_PARTS or part.endswith(".egg-info") for part in relative.parts):
        return True
    name = relative.name
    if name in GENERATED_NAMES or name.startswith(".coverage."):
        return True
    return relative.suffix in {".pyc", ".log"}


def release_files(root: Path) -> list[Path]:
    command = ["git", "ls-files", "--cached", "--others", "--exclude-standard"]
    completed = subprocess.run(command, cwd=root, text=True, capture_output=True, check=True)
    return [root / line for line in completed.stdout.splitlines() if line]


def content_errors(path: Path, text: str) -> list[str]:
    errors = []
    if HOME_PATH.search(text):
        errors.append("contains a private absolute home path")
    for word in PRIVATE_WORDS:
        if word.casefold() in text.casefold():
            errors.append(f"contains private identifier {word!r}")
    if any(pattern.search(text) for pattern in SECRET_PATTERNS):
        errors.append("contains a credential-shaped literal")
    return [f"{path}: {message}" for message in errors]


def markdown_link_errors(path: Path, text: str) -> list[str]:
    errors = []
    for raw in MARKDOWN_LINK.findall(text):
        target = raw.split("#", 1)[0].strip().strip("<>")
        if not target or "://" in target or target.startswith(("#", "mailto:")):
            continue
        resolved = (path.parent / target).resolve()
        if ROOT not in resolved.parents and resolved != ROOT:
            errors.append(f"{path}: relative link escapes repository: {raw}")
        elif not resolved.exists():
            errors.append(f"{path}: broken relative link: {raw}")
    return errors


def workflow_errors(text: str) -> list[str]:
    errors = []
    for ref in ACTION_USE.findall(text):
        if re.fullmatch(r"[0-9a-f]{40}", ref) is None:
            errors.append(f"GitHub Action uses mutable ref: {ref}")
    if re.search(r"pip\s+install[^\n]*(?:--upgrade[^\n]*\bpip\b|\bpip\b[^\n]*--upgrade)", text):
        errors.append("workflow upgrades pip without a version pin")
    hermes_refs = HERMES_CHECKOUT.findall(text)
    if PINNED_HERMES_MINIMUM not in hermes_refs:
        errors.append("minimum Hermes E2E input is not pinned to the verified commit")
    if any(ref not in {PINNED_HERMES_MINIMUM, "main"} for ref in hermes_refs):
        errors.append("Hermes E2E checkout uses an unsupported mutable ref")
    return errors


def _bounded_requirement(requirement: str) -> bool:
    spec = requirement.split(";", 1)[0]
    return bool(re.search(r"(?:===|==|~=|<=|<)", spec))


def requirement_errors(pyproject: dict[str, Any]) -> list[str]:
    groups = {
        "build-system": pyproject.get("build-system", {}).get("requires", []),
        "runtime": pyproject.get("project", {}).get("dependencies", []),
        "test": pyproject.get("project", {}).get("optional-dependencies", {}).get("test", []),
    }
    errors = []
    for group, requirements in groups.items():
        if not isinstance(requirements, list):
            errors.append(f"{group} requirements must be a list")
            continue
        for requirement in requirements:
            if not isinstance(requirement, str) or not _bounded_requirement(requirement):
                errors.append(f"unbounded {group} requirement: {requirement}")
    return errors


def import_errors(files: list[Path]) -> list[str]:
    errors = []
    allowed_third_party = {"yaml"}
    host_modules = {"agent", "hermes_cli", "hermes_constants", "tools"}
    stdlib = set(sys.stdlib_module_names)
    for path in files:
        if path.suffix != ".py" or "tests" in path.parts or path.name == Path(__file__).name:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            errors.append(f"{path}: cannot parse imports: {exc}")
            continue
        for node in ast.walk(tree):
            module = node.module.split(".")[0] if isinstance(node, ast.ImportFrom) and node.module else None
            names = [alias.name.split(".")[0] for alias in node.names] if isinstance(node, ast.Import) else ([module] if module else [])
            for name in names:
                if name in stdlib or name in allowed_third_party or name in host_modules or name == "hermes_skill_publisher" or (isinstance(node, ast.ImportFrom) and node.level):
                    continue
                errors.append(f"{path}: undeclared/non-stdlib import: {name}")
    return errors


def check(root: Path = ROOT) -> list[str]:
    errors = []
    for required in sorted(REQUIRED):
        if not (root / required).is_file():
            errors.append(f"missing required file: {required}")
    files = release_files(root)
    for path in files:
        relative = path.relative_to(root)
        if is_generated(relative):
            errors.append(f"generated artifact is release-visible: {relative}")
            continue
        if path.suffix in TEXT_SUFFIXES or path.name in {"LICENSE", ".gitignore"}:
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeError:
                errors.append(f"non-UTF-8 public text file: {relative}")
                continue
            errors.extend(content_errors(relative, text))
            if path.suffix == ".md":
                errors.extend(markdown_link_errors(path, text))
    errors.extend(import_errors(files))
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8")) if (root / "pyproject.toml").exists() else {}
    workflow = root / ".github" / "workflows" / "test.yml"
    if workflow.is_file():
        errors.extend(workflow_errors(workflow.read_text(encoding="utf-8")))
    errors.extend(requirement_errors(pyproject))
    dependencies = pyproject.get("project", {}).get("dependencies", [])
    if not any(str(item).lower().startswith("pyyaml") for item in dependencies):
        errors.append("pyproject.toml must declare PyYAML")
    test_deps = pyproject.get("project", {}).get("optional-dependencies", {}).get("test", [])
    if not any(str(item).lower().startswith("pytest") for item in test_deps):
        errors.append("pyproject.toml must declare pytest test dependency")
    return errors


def main() -> int:
    errors = check()
    if errors:
        print("Public repository hygiene failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Public repository hygiene passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
