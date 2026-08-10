import json
from pathlib import Path

import pytest
import yaml

from conftest import skill_text
from hermes_skill_publisher.plugin import intercept
from hermes_skill_publisher.publisher import promote


def decode(value):
    return json.loads(value) if isinstance(value, str) else value


def test_non_target_passes_once():
    calls = []
    result = intercept(tool_name="other", args={"x": 1}, next_call=lambda args: calls.append(args) or "ok")
    assert result == "ok" and calls == [{"x": 1}]


def test_classified_create_passes_once(isolated_home):
    calls = []
    args = {"action": "create", "name": "demo-skill", "content": skill_text("demo-skill")}
    result = intercept(tool_name="skill_manage", args=args, next_call=lambda value: calls.append(value) or json.dumps({"success": True}))
    assert decode(result) == {"success": True}
    assert calls == [args]


def test_default_missing_runs_core_and_annotates(isolated_home):
    calls = 0
    def core(_):
        nonlocal calls
        calls += 1
        return json.dumps({"success": True, "message": "created"})
    text = "---\nname: demo-skill\ndescription: test\n---\n"
    result = decode(intercept(tool_name="skill_manage", args={"action": "create", "name": "demo-skill", "content": text}, next_call=core))
    assert calls == 1
    assert result["skill_publisher"]["code"] == "skill_publisher.classification_missing"


@pytest.mark.parametrize("scope, code", [(None, "classification_required"), ("project", "classification_invalid")])
def test_required_rejects_without_core(isolated_home, scope, code):
    config = isolated_home["config"]
    config["plugins"]["entries"]["hermes-skill-publisher"]["require_classification"] = True
    (isolated_home["hermes"] / "config.yaml").write_text(yaml.safe_dump(config))
    content = "---\nname: demo-skill\ndescription: test\n---\n" if scope is None else skill_text("demo-skill", scope)
    result = decode(intercept(tool_name="skill_manage", args={"action": "create", "name": "demo-skill", "content": content}, next_call=lambda _: pytest.fail("core called")))
    assert result["code"] == f"skill_publisher.{code}" and result["retryable"]


def test_malformed_policy_rejects_fail_closed(isolated_home):
    config = isolated_home["config"]
    config["plugins"]["entries"]["hermes-skill-publisher"]["require_classification"] = "no"
    (isolated_home["hermes"] / "config.yaml").write_text(yaml.safe_dump(config))
    result = decode(intercept(tool_name="skill_manage", args={"action": "create", "name": "demo-skill", "content": skill_text("demo-skill")}, next_call=lambda _: pytest.fail("core called")))
    assert result["code"] == "skill_publisher.policy_unavailable"


def test_published_scope_change_is_rolled_back(isolated_home, make_skill):
    publication = promote(make_skill(isolated_home["local"]))
    target = Path(publication["canonical_path"])
    original = (target / "SKILL.md").read_text()
    changed = skill_text("demo-skill", "local")
    def core(_):
        (target / "SKILL.md").write_text(changed)
        return json.dumps({"success": True})
    result = decode(intercept(tool_name="skill_manage", args={"action": "edit", "name": "demo-skill", "content": changed}, next_call=core))
    assert result["code"] == "skill_publisher.scope_change_requires_unpublish"
    assert (target / "SKILL.md").read_text() == original


def test_required_rejection_survives_audit_failure(isolated_home, monkeypatch):
    import hermes_skill_publisher.plugin as plugin
    config = isolated_home["config"]
    config["plugins"]["entries"]["hermes-skill-publisher"]["require_classification"] = True
    (isolated_home["hermes"] / "config.yaml").write_text(yaml.safe_dump(config))
    monkeypatch.setattr(plugin, "audit", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))
    text = "---\nname: demo-skill\ndescription: test\n---\n"
    result = decode(intercept(tool_name="skill_manage", args={"action": "create", "name": "demo-skill", "content": text}, next_call=lambda _: pytest.fail("core called")))
    assert result["code"] == "skill_publisher.classification_required"


@pytest.mark.parametrize("action", ["write_file", "remove_file"])
def test_published_skill_md_file_actions_are_rolled_back(isolated_home, make_skill, action):
    publication = promote(make_skill(isolated_home["local"]))
    target = Path(publication["canonical_path"])
    skill_md = target / "SKILL.md"
    original = skill_md.read_text()

    def core(_):
        if action == "write_file":
            skill_md.write_text(skill_text("demo-skill", "local"))
        else:
            skill_md.unlink()
        return json.dumps({"success": True})

    args = {"action": action, "name": "demo-skill", "file_path": "SKILL.md"}
    result = decode(intercept(tool_name="skill_manage", args=args, next_call=core))
    assert result["code"] == "skill_publisher.scope_change_requires_unpublish"
    assert skill_md.read_text() == original


def test_managed_delete_cleans_exact_adapter_and_registry(isolated_home, make_skill):
    from hermes_skill_publisher.filesystem import safe_remove_tree
    from hermes_skill_publisher.state import load_registry
    publication = promote(make_skill(isolated_home["local"]))
    target = Path(publication["canonical_path"])

    def core(_):
        safe_remove_tree(target, expected_digest=publication["digest"])
        return json.dumps({"success": True})

    result = decode(intercept(tool_name="skill_manage", args={"action": "delete", "name": "demo-skill"}, next_call=core))
    assert result["success"]
    assert not (isolated_home["adapter"] / "demo-skill").exists()
    assert "demo-skill" not in load_registry()["publications"]


def test_changed_adapter_blocks_delete_before_core(isolated_home, make_skill, tmp_path):
    promote(make_skill(isolated_home["local"]))
    link = isolated_home["adapter"] / "demo-skill"
    link.unlink()
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    link.symlink_to(replacement)
    result = decode(intercept(tool_name="skill_manage", args={"action": "delete", "name": "demo-skill"}, next_call=lambda _: pytest.fail("core called")))
    assert result["code"] == "skill_publisher.adapter_conflict"
    assert link.resolve() == replacement


def test_published_support_write_updates_registry_digest(isolated_home, make_skill):
    from hermes_skill_publisher.state import load_registry
    publication = promote(make_skill(isolated_home["local"]))
    target = Path(publication["canonical_path"])
    before = publication["digest"]
    def core(_):
        (target / "references").mkdir()
        (target / "references" / "x.md").write_text("x")
        return json.dumps({"success": True})
    intercept(tool_name="skill_manage", args={"action": "write_file", "name": "demo-skill", "file_path": "references/x.md"}, next_call=core)
    assert load_registry()["publications"]["demo-skill"]["digest"] != before


def _require_classification(isolated_home):
    config = isolated_home["config"]
    config["plugins"]["entries"]["hermes-skill-publisher"]["require_classification"] = True
    (isolated_home["hermes"] / "config.yaml").write_text(yaml.safe_dump(config))


def test_classifier_exception_never_reaches_core_in_required_mode(isolated_home, monkeypatch):
    import hermes_skill_publisher.plugin as plugin
    _require_classification(isolated_home)
    monkeypatch.setattr(plugin, "classify_content", lambda _: (_ for _ in ()).throw(RecursionError("deep")))
    result = decode(intercept(tool_name="skill_manage", args={"action": "create", "name": "demo-skill", "content": "x"}, next_call=lambda _: pytest.fail("core called")))
    assert result["code"] == "skill_publisher.policy_unavailable"


def test_deeply_nested_yaml_rejected_without_core_in_required_mode(isolated_home):
    _require_classification(isolated_home)
    content = "---\nname: demo-skill\ndescription: t\n" + "x: " + "[" * 500 + "]" * 500 + "\n---\n"
    result = decode(intercept(tool_name="skill_manage", args={"action": "create", "name": "demo-skill", "content": content}, next_call=lambda _: pytest.fail("core called")))
    assert result["code"] == "skill_publisher.classification_invalid"
    assert result["retryable"]


def _enable_write_approval(isolated_home):
    config = isolated_home["config"]
    config["skills"]["write_approval"] = True
    (isolated_home["hermes"] / "config.yaml").write_text(yaml.safe_dump(config))


def test_write_approval_blocks_create_before_staging(isolated_home):
    _enable_write_approval(isolated_home)
    result = decode(intercept(tool_name="skill_manage", args={"action": "create", "name": "demo-skill", "content": skill_text("demo-skill")}, next_call=lambda _: pytest.fail("core called")))
    assert result["code"] == "skill_publisher.write_approval_incompatible"
    assert result["retryable"] is False


def test_write_approval_blocks_managed_mutation(isolated_home, make_skill):
    publication = promote(make_skill(isolated_home["local"]))
    _enable_write_approval(isolated_home)
    result = decode(intercept(tool_name="skill_manage", args={"action": "edit", "name": "demo-skill", "content": "x"}, next_call=lambda _: pytest.fail("core called")))
    assert result["code"] == "skill_publisher.write_approval_incompatible"


def test_local_shadow_blocks_managed_edit(isolated_home, make_skill):
    publication = promote(make_skill(isolated_home["local"]))
    target = Path(publication["canonical_path"])
    original = (target / "SKILL.md").read_text()
    shadow = make_skill(isolated_home["local"], scope="local")

    def core(_):
        shadow.joinpath("SKILL.md").write_text(skill_text("demo-skill", "private"))
        return json.dumps({"success": True})

    result = decode(intercept(tool_name="skill_manage", args={"action": "edit", "name": "demo-skill", "content": "x"}, next_call=core))
    assert result["success"] is False
    assert "shadow" in result["error"]
    assert (target / "SKILL.md").read_text() == original
    assert shadow.joinpath("SKILL.md").read_text() == skill_text("demo-skill", "local")


def test_live_journal_blocks_tool_mutation(isolated_home, make_skill):
    from hermes_skill_publisher.state import SCHEMA_VERSION, write_journal
    write_journal({"schema_version": SCHEMA_VERSION, "operation_id": "inflight-1", "operation": "promote", "name": "demo-skill", "phase": "planned"})
    result = decode(intercept(tool_name="skill_manage", args={"action": "write_file", "name": "demo-skill", "file_path": "x.md", "file_content": "x"}, next_call=lambda _: pytest.fail("core called")))
    assert result["code"] == "skill_publisher.transaction_in_progress"


def test_delete_vs_unpublish_is_serialized(isolated_home, make_skill):
    import threading
    import time
    from hermes_skill_publisher.state import load_registry
    publication = promote(make_skill(isolated_home["local"]))
    target = Path(publication["canonical_path"])
    entered = threading.Event()
    release = threading.Event()

    def core(_):
        entered.set()
        assert release.wait(10)
        import shutil
        shutil.rmtree(target)
        return json.dumps({"success": True})

    outcomes = {}

    def run_delete():
        outcomes["delete"] = decode(intercept(tool_name="skill_manage", args={"action": "delete", "name": "demo-skill"}, next_call=core))

    def run_unpublish():
        from hermes_skill_publisher.publisher import unpublish
        try:
            unpublish("demo-skill", "local")
            outcomes["unpublish"] = "completed"
        except Exception as exc:
            outcomes["unpublish"] = str(exc)

    delete_thread = threading.Thread(target=run_delete)
    delete_thread.start()
    assert entered.wait(10)
    unpublish_thread = threading.Thread(target=run_unpublish)
    unpublish_thread.start()
    # The unpublish cannot interleave: the delete holds every lock across core.
    time.sleep(0.5)
    assert unpublish_thread.is_alive()
    release.set()
    delete_thread.join(10)
    unpublish_thread.join(10)
    assert outcomes["delete"]["success"] is True
    assert outcomes["unpublish"] != "completed" and "ownership" in outcomes["unpublish"]
    assert not target.exists()
    assert not (isolated_home["local"] / "demo-skill").exists()
    assert "demo-skill" not in load_registry()["publications"]


def test_unpublish_wins_before_delete_lock_and_core_is_blocked(isolated_home, make_skill, monkeypatch):
    import threading
    import hermes_skill_publisher.plugin as plugin
    from hermes_skill_publisher.publisher import unpublish
    publication = promote(make_skill(isolated_home["local"]))
    target = Path(publication["canonical_path"])
    entered = threading.Event()
    release = threading.Event()
    original_load = plugin.load_config
    calls = []

    def delayed_load():
        entered.set()
        assert release.wait(10)
        return original_load()

    monkeypatch.setattr(plugin, "load_config", delayed_load)
    outcome = {}

    def run_delete():
        outcome["delete"] = decode(intercept(
            tool_name="skill_manage",
            args={"action": "delete", "name": "demo-skill"},
            next_call=lambda _: calls.append("core") or json.dumps({"success": True}),
        ))

    thread = threading.Thread(target=run_delete)
    thread.start()
    assert entered.wait(10)
    restored = unpublish("demo-skill", "local")
    release.set()
    thread.join(10)
    assert outcome["delete"]["success"] is False
    assert "ownership changed" in outcome["delete"]["error"]
    assert calls == []
    assert Path(restored["path"]).is_dir()
    assert not target.exists()


def test_managed_edit_is_serialized_with_unpublish(isolated_home, make_skill):
    import threading
    import time
    publication = promote(make_skill(isolated_home["local"]))
    target = Path(publication["canonical_path"])
    entered = threading.Event()
    release = threading.Event()
    outcomes = {}

    def core(_):
        entered.set()
        assert release.wait(10)
        target.joinpath("SKILL.md").write_text(skill_text("demo-skill", description="edited while locked"))
        return json.dumps({"success": True})

    def run_edit():
        outcomes["edit"] = decode(intercept(tool_name="skill_manage", args={"action": "edit", "name": "demo-skill", "content": "x"}, next_call=core))

    def run_unpublish():
        from hermes_skill_publisher.publisher import unpublish
        try:
            outcomes["unpublish"] = unpublish("demo-skill", "local")
        except Exception as exc:
            outcomes["unpublish_error"] = str(exc)

    edit_thread = threading.Thread(target=run_edit)
    edit_thread.start()
    assert entered.wait(10)
    unpublish_thread = threading.Thread(target=run_unpublish)
    unpublish_thread.start()
    time.sleep(0.5)
    assert unpublish_thread.is_alive()
    release.set()
    edit_thread.join(10)
    unpublish_thread.join(10)
    assert outcomes["edit"]["success"] is True
    assert "ownership changed" in outcomes["unpublish_error"]
    assert "edited while locked" in target.joinpath("SKILL.md").read_text()
    assert not (isolated_home["local"] / "demo-skill").exists()


def test_malformed_yaml_sentinel_never_persisted(isolated_home, capsys):
    from hermes_skill_publisher.state import read_audit
    _require_classification(isolated_home)
    sentinel = "DO_NOT_LOG_THIS_SECRET"
    content = f'---\nname: demo-skill\ndescription: t\nleak: "{sentinel}\n---\n'
    result = decode(intercept(tool_name="skill_manage", args={"action": "create", "name": "demo-skill", "content": content}, next_call=lambda _: json.dumps({"success": True})))
    assert result["success"] is False
    assert sentinel not in json.dumps(result)
    from hermes_skill_publisher.plugin import on_post_tool_call
    on_post_tool_call(tool_name="skill_manage", args={"action": "create", "name": "demo-skill"}, status="error", error_message=f"core failed with {sentinel} inside")
    audit_text = json.dumps(read_audit(50))
    assert sentinel not in audit_text
