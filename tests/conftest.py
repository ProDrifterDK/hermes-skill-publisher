from __future__ import annotations

import contextvars
import os
from pathlib import Path
import sys
import types

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
HOST_SOURCE = os.environ.get("HERMES_AGENT_SOURCE")
if HOST_SOURCE and str(Path(HOST_SOURCE).resolve()) not in sys.path:
    sys.path.insert(0, str(Path(HOST_SOURCE).resolve()))


@pytest.fixture(autouse=True)
def host_gate_capability(monkeypatch: pytest.MonkeyPatch):
    """Provide only the required ContextVar in host-free unit environments."""
    try:
        from tools.skill_manager_tool import _skill_gate_bypass  # noqa: F401
    except ImportError:
        tools = types.ModuleType("tools")
        skill_tool = types.ModuleType("tools.skill_manager_tool")
        skill_tool._skill_gate_bypass = contextvars.ContextVar("test_skill_gate_bypass", default=False)
        tools.skill_manager_tool = skill_tool
        monkeypatch.setitem(sys.modules, "tools", tools)
        monkeypatch.setitem(sys.modules, "tools.skill_manager_tool", skill_tool)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    hermes = tmp_path / "hermes"
    local = hermes / "skills"
    shared = home / ".agents" / "skills"
    adapter = home / ".other" / "skills"
    for path in (home, hermes, local, shared, adapter):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    config = {
        "skills": {"external_dirs": [str(shared)]},
        "plugins": {
            "enabled": ["hermes-skill-publisher"],
            "entries": {
                "hermes-skill-publisher": {
                    "require_classification": False,
                    "shared_root": str(shared),
                    "adapter_roots": {"other": str(adapter)},
                }
            },
        },
    }
    (hermes / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    try:
        from agent.skill_utils import _external_dirs_cache_clear
        _external_dirs_cache_clear()
    except ImportError:
        pass
    yield {"home": home, "hermes": hermes, "local": local, "shared": shared, "adapter": adapter, "config": config}


def skill_text(name: str, scope: object = "shared", *, description: str = "A portable test skill.") -> str:
    metadata = {"skill-publisher-scope": scope}
    return yaml.safe_dump({"name": name, "description": description, "metadata": metadata}, sort_keys=False).join(["---\n", "---\nBody\n"])


@pytest.fixture
def make_skill():
    def make(root: Path, name: str = "demo-skill", scope: object = "shared", category: str | None = None) -> Path:
        package = root / category / name if category else root / name
        package.mkdir(parents=True)
        (package / "SKILL.md").write_text(skill_text(name, scope), encoding="utf-8")
        return package
    return make
