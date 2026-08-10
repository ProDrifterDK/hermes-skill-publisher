from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

from conftest import skill_text


def _hermes_host(isolated_home, monkeypatch):
    """Install the plugin into an isolated HERMES_HOME against a real checkout."""
    source_raw = os.environ.get("HERMES_AGENT_SOURCE")
    if not source_raw:
        pytest.skip("set HERMES_AGENT_SOURCE to a compatible Hermes checkout")
    source = Path(source_raw).resolve()
    if not (source / "hermes_cli" / "plugins.py").is_file():
        pytest.skip("HERMES_AGENT_SOURCE is not a Hermes checkout")
    monkeypatch.syspath_prepend(str(source))

    repo = Path(__file__).resolve().parents[1]
    installed = isolated_home["hermes"] / "plugins" / "hermes-skill-publisher"
    shutil.copytree(repo, installed, ignore=shutil.ignore_patterns(".git", ".pi-subagents", "__pycache__", ".pytest_cache"))

    from hermes_cli.plugins import PluginManager
    import hermes_cli.plugins as plugins
    manager = PluginManager()
    monkeypatch.setattr(plugins, "_plugin_manager", manager)
    manager.discover_and_load()
    return source, manager


def _host_python(source: Path) -> str:
    override = os.environ.get("HERMES_AGENT_PYTHON")
    if override:
        return override
    hermes_python = source / ".venv" / "bin" / "python"
    return str(hermes_python if hermes_python.is_file() else Path(sys.executable))


def _run_cli(source: Path, *parts: str) -> subprocess.CompletedProcess:
    repo = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(source), str(repo), env.get("PYTHONPATH", "")])
    command = [_host_python(source), "-m", "hermes_cli.main", *parts]
    return subprocess.run(command, env=env, text=True, capture_output=True, timeout=30)


@pytest.mark.e2e
def test_real_hermes_discovery_middleware_lifecycle_and_cli(isolated_home, monkeypatch):
    source, manager = _hermes_host(isolated_home, monkeypatch)
    loaded = manager._plugins["hermes-skill-publisher"]
    assert loaded.enabled and loaded.error is None
    assert len(manager._middleware.get("tool_execution", [])) == 1
    assert all(manager._hooks.get(name) for name in ("post_tool_call", "on_session_start", "on_session_end", "pre_llm_call"))
    assert "skill-publisher" in manager._cli_commands

    from hermes_cli.middleware import run_tool_execution_middleware
    from tools.skill_manager_tool import skill_manage
    create = {"action": "create", "name": "demo-skill", "content": skill_text("demo-skill")}
    result = json.loads(run_tool_execution_middleware("skill_manage", create, lambda payload: skill_manage(**payload)))
    assert result["success"]
    support = {"action": "write_file", "name": "demo-skill", "file_path": "references/guide.md", "file_content": "guide"}
    assert json.loads(run_tool_execution_middleware("skill_manage", support, lambda payload: skill_manage(**payload)))["success"]
    assert not (isolated_home["shared"] / "demo-skill").exists()

    manager.invoke_hook("on_session_end", session_id="e2e", completed=True, interrupted=False)
    target = isolated_home["shared"] / "demo-skill"
    assert (target / "references" / "guide.md").read_text() == "guide"
    assert (isolated_home["adapter"] / "demo-skill").is_symlink()

    patch = {"action": "patch", "name": "demo-skill", "old_string": "Body", "new_string": "Updated"}
    assert json.loads(run_tool_execution_middleware("skill_manage", patch, lambda payload: skill_manage(**payload)))["success"]
    assert "Updated" in (target / "SKILL.md").read_text()

    completed = _run_cli(source, "skill-publisher", "doctor", "--json")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    blocked = _run_cli(source, "skill-publisher", "publish", "missing-skill", "--json")
    assert blocked.returncode == 2, blocked.stdout + blocked.stderr
    assert not json.loads(blocked.stdout)["ok"]


@pytest.mark.e2e
def test_real_chain_classifier_fault_never_calls_core(isolated_home, monkeypatch):
    source, manager = _hermes_host(isolated_home, monkeypatch)
    config = isolated_home["config"]
    config["plugins"]["entries"]["hermes-skill-publisher"]["require_classification"] = True
    (isolated_home["hermes"] / "config.yaml").write_text(yaml.safe_dump(config))

    from hermes_cli.middleware import run_tool_execution_middleware
    calls = []

    def terminal(payload):
        calls.append(payload)
        return json.dumps({"success": True})

    # Natural parser fault: deeply nested YAML blows the parser stack.
    nested = "---\nname: demo-skill\ndescription: t\n" + "x: " + "[" * 500 + "]" * 500 + "\n---\n"
    result = json.loads(run_tool_execution_middleware("skill_manage", {"action": "create", "name": "demo-skill", "content": nested}, terminal))
    assert result["success"] is False and not calls

    # Hostile fault: the classifier itself raises inside the real host chain.
    callback = manager._middleware["tool_execution"][0]
    module = sys.modules[callback.__module__]
    original = module.classify_content
    monkeypatch.setattr(module, "classify_content", lambda _: (_ for _ in ()).throw(RuntimeError("hostile")))
    result = json.loads(run_tool_execution_middleware("skill_manage", {"action": "create", "name": "demo-skill", "content": skill_text("demo-skill")}, terminal))
    assert result["success"] is False
    assert result["code"] == "skill_publisher.policy_unavailable"
    assert not calls


@pytest.mark.e2e
def test_real_host_all_truthy_approval_values_block_create_edit_delete(isolated_home, monkeypatch):
    _source, manager = _hermes_host(isolated_home, monkeypatch)
    from hermes_cli.middleware import run_tool_execution_middleware
    from tools.skill_manager_tool import skill_manage

    values = [True, "true", "yes", "on", "1", "approve", "enabled", "  YeS  "]
    actions = {
        "create": {"content": skill_text("demo-skill")},
        "edit": {"content": skill_text("demo-skill")},
        "delete": {},
    }
    for value in values:
        config = isolated_home["config"]
        config["skills"]["write_approval"] = value
        (isolated_home["hermes"] / "config.yaml").write_text(yaml.safe_dump(config))
        for action, extra in actions.items():
            payload = {"action": action, "name": "demo-skill", **extra}
            result = json.loads(run_tool_execution_middleware("skill_manage", payload, lambda args: skill_manage(**args)))
            assert result["success"] is False
            assert result["code"] == "skill_publisher.write_approval_incompatible"
    pending = isolated_home["hermes"] / "pending" / "skills"
    assert not pending.exists() or list(pending.iterdir()) == []
    assert not (isolated_home["local"] / "demo-skill").exists()


@pytest.mark.e2e
def test_real_host_missing_gate_bypass_capability_blocks_writes_and_doctor(isolated_home, monkeypatch):
    _source, manager = _hermes_host(isolated_home, monkeypatch)
    import tools.skill_manager_tool as skill_tool
    from hermes_cli.middleware import run_tool_execution_middleware

    monkeypatch.delattr(skill_tool, "_skill_gate_bypass")
    payload = {"action": "create", "name": "demo-skill", "content": skill_text("demo-skill")}
    result = json.loads(run_tool_execution_middleware("skill_manage", payload, lambda args: skill_tool.skill_manage(**args)))
    assert result["success"] is False
    assert result["code"] == "skill_publisher.policy_unavailable"
    assert not (isolated_home["local"] / "demo-skill").exists()

    from hermes_skill_publisher.cli import _doctor
    value, code = _doctor()
    assert code == 2
    assert any("write_gate_capability_unavailable" in error for error in value["errors"])


@pytest.mark.e2e
def test_real_host_write_approval_staging_cannot_begin(isolated_home, monkeypatch):
    source, manager = _hermes_host(isolated_home, monkeypatch)
    config = isolated_home["config"]
    config["skills"]["write_approval"] = True
    (isolated_home["hermes"] / "config.yaml").write_text(yaml.safe_dump(config))

    from hermes_cli.middleware import run_tool_execution_middleware
    from tools.skill_manager_tool import skill_manage
    create = {"action": "create", "name": "demo-skill", "content": skill_text("demo-skill")}
    result = json.loads(run_tool_execution_middleware("skill_manage", create, lambda payload: skill_manage(**payload)))
    assert result["success"] is False
    assert result["code"] == "skill_publisher.write_approval_incompatible"
    pending = isolated_home["hermes"] / "pending" / "skills"
    assert not pending.exists() or list(pending.iterdir()) == []
    assert not (isolated_home["local"] / "demo-skill").exists()

    completed = _run_cli(source, "skill-publisher", "doctor", "--json")
    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert "write_approval" in completed.stdout


@pytest.mark.e2e
def test_real_host_approval_toggle_cannot_create_replay_bypass(isolated_home, monkeypatch):
    source, manager = _hermes_host(isolated_home, monkeypatch)
    from hermes_cli.middleware import run_tool_execution_middleware
    from tools.skill_manager_tool import skill_manage
    create = {"action": "create", "name": "demo-skill", "content": skill_text("demo-skill")}
    assert json.loads(run_tool_execution_middleware("skill_manage", create, lambda payload: skill_manage(**payload)))["success"]
    manager.invoke_hook("on_session_end", session_id="e2e", completed=True, interrupted=False)
    target = isolated_home["shared"] / "demo-skill"
    original = target.joinpath("SKILL.md").read_bytes()

    callback = manager._middleware["tool_execution"][0]
    module = sys.modules[callback.__module__]
    checks = 0

    def toggle_between_checks():
        nonlocal checks
        checks += 1
        if checks == 2:
            config = isolated_home["config"]
            config["skills"]["write_approval"] = True
            (isolated_home["hermes"] / "config.yaml").write_text(yaml.safe_dump(config))
        return False

    monkeypatch.setattr(module, "skills_write_approval_enabled", toggle_between_checks)
    edit = {"action": "edit", "name": "demo-skill", "content": skill_text("demo-skill", "local")}
    result = json.loads(run_tool_execution_middleware("skill_manage", edit, lambda payload: skill_manage(**payload)))
    assert result["success"] is False
    assert result["code"] == "skill_publisher.scope_change_requires_unpublish"
    assert target.joinpath("SKILL.md").read_bytes() == original
    pending = isolated_home["hermes"] / "pending" / "skills"
    assert not pending.exists() or list(pending.iterdir()) == []


@pytest.mark.e2e
def test_real_host_same_name_shadow_blocks_managed_edit(isolated_home, monkeypatch):
    source, manager = _hermes_host(isolated_home, monkeypatch)
    from hermes_cli.middleware import run_tool_execution_middleware
    from tools.skill_manager_tool import skill_manage
    create = {"action": "create", "name": "demo-skill", "content": skill_text("demo-skill")}
    assert json.loads(run_tool_execution_middleware("skill_manage", create, lambda payload: skill_manage(**payload)))["success"]
    manager.invoke_hook("on_session_end", session_id="e2e", completed=True, interrupted=False)
    target = isolated_home["shared"] / "demo-skill"
    assert target.is_dir()

    # A local same-name shadow written directly to the profile root (local-first
    # lookup would silently edit it instead of the managed canonical copy).
    shadow = isolated_home["local"] / "demo-skill"
    shadow.mkdir()
    shadow.joinpath("SKILL.md").write_text(skill_text("demo-skill", "local"))
    original = (target / "SKILL.md").read_text()

    changed = skill_text("demo-skill", "local")
    edit = {"action": "edit", "name": "demo-skill", "content": changed}
    result = json.loads(run_tool_execution_middleware("skill_manage", edit, lambda payload: skill_manage(**payload)))
    assert result["success"] is False
    assert "shadow" in result["error"]
    assert (target / "SKILL.md").read_text() == original
    assert (isolated_home["local"] / "demo-skill" / "SKILL.md").read_text() == skill_text("demo-skill", "local")


@pytest.mark.e2e
def test_real_host_delete_vs_unpublish_is_serialized(isolated_home, monkeypatch):
    import threading
    import time
    source, manager = _hermes_host(isolated_home, monkeypatch)
    from hermes_cli.middleware import run_tool_execution_middleware
    from tools.skill_manager_tool import skill_manage
    create = {"action": "create", "name": "demo-skill", "content": skill_text("demo-skill")}
    assert json.loads(run_tool_execution_middleware("skill_manage", create, lambda payload: skill_manage(**payload)))["success"]
    manager.invoke_hook("on_session_end", session_id="e2e", completed=True, interrupted=False)
    target = isolated_home["shared"] / "demo-skill"
    assert target.is_dir()

    import tools.skill_manager_tool as skill_tool
    real_delete = skill_tool._delete_skill
    entered = threading.Event()
    release = threading.Event()

    def barrier_delete(name, absorbed_into=None):
        entered.set()
        assert release.wait(15)
        return real_delete(name, absorbed_into=absorbed_into)

    monkeypatch.setattr(skill_tool, "_delete_skill", barrier_delete)
    outcomes = {}

    def run_delete():
        outcomes["delete"] = json.loads(run_tool_execution_middleware("skill_manage", {"action": "delete", "name": "demo-skill"}, lambda payload: skill_manage(**payload)))

    def run_unpublish():
        env = os.environ.copy()
        repo = Path(__file__).resolve().parents[1]
        env["PYTHONPATH"] = os.pathsep.join([str(source), str(repo), env.get("PYTHONPATH", "")])
        completed = subprocess.run(
            [_host_python(source), "-m", "hermes_cli.main", "skill-publisher", "unpublish", "demo-skill", "--scope", "local", "--json"],
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        outcomes["unpublish_rc"] = completed.returncode
        outcomes["unpublish_out"] = completed.stdout

    delete_thread = threading.Thread(target=run_delete)
    delete_thread.start()
    assert entered.wait(15)
    unpublish_thread = threading.Thread(target=run_unpublish)
    unpublish_thread.start()
    time.sleep(1.0)
    assert unpublish_thread.is_alive()  # blocked on the continuously held locks
    release.set()
    delete_thread.join(15)
    unpublish_thread.join(30)
    assert outcomes["delete"]["success"] is True
    assert outcomes["unpublish_rc"] == 2
    assert not target.exists()
    assert not (isolated_home["local"] / "demo-skill").exists()
    assert not (isolated_home["adapter"] / "demo-skill").exists()


def _kill_after_source_parked(hermes_home: str, source_path: str):
    import os
    os.environ["HERMES_HOME"] = hermes_home
    import hermes_skill_publisher.publisher as publisher
    original = publisher._journal_phase

    def kill(journal, phase, **updates):
        original(journal, phase, **updates)
        if phase == "source_parked":
            os._exit(73)

    publisher._journal_phase = kill
    publisher.promote(Path(source_path))


@pytest.mark.e2e
def test_real_host_crash_then_resumed_turn_recovers_via_pre_llm_call(isolated_home, monkeypatch):
    import multiprocessing
    source, manager = _hermes_host(isolated_home, monkeypatch)
    from hermes_cli.middleware import run_tool_execution_middleware
    from tools.skill_manager_tool import skill_manage
    create = {"action": "create", "name": "demo-skill", "content": skill_text("demo-skill")}
    assert json.loads(run_tool_execution_middleware("skill_manage", create, lambda payload: skill_manage(**payload)))["success"]
    local_source = isolated_home["local"] / "demo-skill"
    assert local_source.is_dir()

    # Hard crash mid-promotion in a separate process (the turn never ends).
    context = multiprocessing.get_context("fork")
    process = context.Process(target=_kill_after_source_parked, args=(str(isolated_home["hermes"]), str(local_source)))
    process.start()
    process.join(20)
    assert process.exitcode == 73
    assert not (isolated_home["shared"] / "demo-skill").exists()

    # A resumed session never fires on_session_start; pre_llm_call is the
    # every-turn boundary and must run journal recovery before prompt/tool use.
    manager.invoke_hook("pre_llm_call", session_id="e2e", task_id="t", turn_id="1", user_message="hi", conversation_history=[], is_first_turn=False, model="m", platform="cli", parent_session_id="", sender_id="")
    assert (isolated_home["shared"] / "demo-skill").is_dir()
    assert (isolated_home["adapter"] / "demo-skill").is_symlink()
