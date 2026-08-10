from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import threading

import pytest
import yaml

from conftest import skill_text
from hermes_skill_publisher import plugin
from hermes_skill_publisher.cli import handle_cli, setup_cli
from hermes_skill_publisher.config import ConfigError, load_config, skills_write_approval_enabled
from hermes_skill_publisher.filesystem import package_digest, relative_link_text, rename_noreplace, safe_copy_tree
from hermes_skill_publisher.frontmatter import rewrite_scope_bytes
from hermes_skill_publisher.plugin import intercept, on_post_tool_call
from hermes_skill_publisher.publisher import PublisherError, promote, reconcile, recover, unpublish
from hermes_skill_publisher.state import SCHEMA_VERSION, list_journals, load_registry, save_registry, state_root, write_journal
from scripts.check_public_repo import requirement_errors, workflow_errors


def _parse(*parts: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    setup_cli(parser)
    return parser.parse_args(parts)


def _decode(value):
    return json.loads(value) if isinstance(value, str) else value


def _write_config(env, config) -> None:
    env["hermes"].joinpath("config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


@pytest.mark.parametrize("value", [True, "true", "yes", "on", "1", "approve", "enabled", "  YeS  ", " ENABLED "])
def test_host_truthy_write_approval_normalization_blocks(value, isolated_home):
    config = isolated_home["config"]
    config["skills"]["write_approval"] = value
    _write_config(isolated_home, config)
    assert skills_write_approval_enabled() is True
    result = _decode(intercept(
        tool_name="skill_manage",
        args={"action": "create", "name": "demo-skill", "content": skill_text("demo-skill")},
        next_call=lambda _: pytest.fail("core called"),
    ))
    assert result["code"] == "skill_publisher.write_approval_incompatible"


def test_unsupported_write_approval_type_fails_closed(isolated_home):
    config = isolated_home["config"]
    config["skills"]["write_approval"] = 1
    _write_config(isolated_home, config)
    with pytest.raises(ConfigError):
        skills_write_approval_enabled()
    result = _decode(intercept(
        tool_name="skill_manage",
        args={"action": "create", "name": "demo-skill", "content": skill_text("demo-skill")},
        next_call=lambda _: pytest.fail("core called"),
    ))
    assert result["code"] == "skill_publisher.policy_unavailable"


def test_missing_host_gate_capability_blocks_write_doctor_and_publication(isolated_home, make_skill, monkeypatch, capsys):
    import tools.skill_manager_tool as host_skill_tool

    monkeypatch.delattr(host_skill_tool, "_skill_gate_bypass")
    result = _decode(intercept(
        tool_name="skill_manage",
        args={"action": "create", "name": "demo-skill", "content": skill_text("demo-skill")},
        next_call=lambda _: pytest.fail("core called"),
    ))
    assert result["code"] == "skill_publisher.policy_unavailable"

    monkeypatch.setattr(plugin, "_MIDDLEWARE_AVAILABLE", True)
    assert handle_cli(_parse("doctor", "--json")) == 2
    assert "write_gate_capability_unavailable" in capsys.readouterr().out
    source = make_skill(isolated_home["local"])
    assert handle_cli(_parse("publish", "demo-skill", "--json")) == 2
    assert "write_gate_capability_unavailable" in capsys.readouterr().out
    assert source.is_dir() and not (isolated_home["shared"] / "demo-skill").exists()


def test_managed_delete_blocks_republication_aba(isolated_home, make_skill, monkeypatch):
    import hermes_skill_publisher.publisher as publisher

    old = promote(make_skill(isolated_home["local"]))
    old_digest = old["digest"]
    roots_captured = threading.Event()
    resume_delete = threading.Event()
    core_calls = []
    original_delete_roots = publisher.delete_lock_roots

    def paused_roots(name, config):
        roots = original_delete_roots(name, config)
        roots_captured.set()
        assert resume_delete.wait(10)
        return roots

    monkeypatch.setattr(publisher, "delete_lock_roots", paused_roots)
    outcome = {}

    def run_delete():
        outcome["delete"] = _decode(intercept(
            tool_name="skill_manage",
            args={"action": "delete", "name": "demo-skill"},
            next_call=lambda _: core_calls.append("called") or json.dumps({"success": True}),
        ))

    thread = threading.Thread(target=run_delete)
    thread.start()
    assert roots_captured.wait(10)

    restored = Path(unpublish("demo-skill", "local")["path"])
    new_adapter = isolated_home["home"] / ".replacement" / "skills"
    new_adapter.mkdir(parents=True)
    config = isolated_home["config"]
    config["plugins"]["entries"]["hermes-skill-publisher"]["adapter_roots"] = {"replacement": str(new_adapter)}
    _write_config(isolated_home, config)
    restored.joinpath("SKILL.md").write_text(skill_text("demo-skill", "shared", description="replacement publication"), encoding="utf-8")
    replacement = promote(restored)
    assert replacement["digest"] != old_digest

    resume_delete.set()
    thread.join(10)
    assert not thread.is_alive()
    assert outcome["delete"]["success"] is False
    assert "ownership changed" in outcome["delete"]["error"]
    assert core_calls == []
    assert Path(replacement["canonical_path"]).is_dir()
    assert (new_adapter / "demo-skill").is_symlink()
    assert load_registry()["publications"]["demo-skill"] == replacement


def _committed_unpublish_journal(env, publication):
    config = load_config()
    name = "demo-skill"
    target = config.shared_root / name
    local = config.local_root / name
    op_id = "hardening-unpublish"
    stage = local.parent / f".hermes-skill-publisher-stage-{op_id}"
    backup = config.shared_root / f".hermes-skill-publisher-backup-{op_id}"
    safe_copy_tree(target, stage)
    skill_md = stage / "SKILL.md"
    skill_md.write_bytes(rewrite_scope_bytes(skill_md.read_bytes(), "local"))
    local_digest = package_digest(stage)
    rename_noreplace(target, backup)
    rename_noreplace(stage, local)
    journal = {
        "schema_version": SCHEMA_VERSION,
        "operation_id": op_id,
        "operation": "unpublish",
        "phase": "target_committed",
        "name": name,
        "scope": "local",
        "target_path": str(target),
        "backup_path": str(backup),
        "local_path": str(local),
        "local_relpath": name,
        "stage_path": str(stage),
        "canonical_digest": publication["digest"],
        "local_digest": local_digest,
        "adapters": publication["adapter_links"],
        "publication": publication,
    }
    write_journal(journal)
    return journal, target, local, backup


def test_reconcile_skips_registry_cleanup_for_live_valid_journal(isolated_home, make_skill):
    publication = promote(make_skill(isolated_home["local"]))
    _committed_unpublish_journal(isolated_home, publication)
    adapter = isolated_home["adapter"] / "demo-skill"

    findings = reconcile()
    assert any(item["result"] == "deferred" for item in findings)
    assert adapter.is_symlink()
    assert "demo-skill" in load_registry()["publications"]
    assert list_journals()


def test_unpublish_recovery_rejects_unrelated_victim_root(isolated_home, make_skill):
    publication = promote(make_skill(isolated_home["local"]))
    old_adapter = isolated_home["adapter"] / "demo-skill"
    old_adapter.unlink()

    victim = isolated_home["home"] / "victim-root"
    victim.mkdir()
    target = Path(publication["canonical_path"])
    victim_link = victim / "demo-skill"
    link_text = relative_link_text(target, victim)
    victim_link.symlink_to(link_text)
    publication = dict(publication)
    publication["adapter_links"] = {"victim": {"path": str(victim_link), "link_text": link_text}}
    save_registry({"schema_version": SCHEMA_VERSION, "publications": {"demo-skill": publication}})

    journal, _, _, _ = _committed_unpublish_journal(isolated_home, publication)
    result = recover()
    assert result[0]["result"] == "blocked"
    assert victim_link.is_symlink()
    assert not victim.joinpath(".hermes-skill-publisher.lock").exists()
    assert load_registry()["publications"]["demo-skill"] == publication
    assert list_journals()[0]["operation_id"] == journal["operation_id"]


def test_structurally_invalid_journal_is_global_barrier_and_doctor_blocker(isolated_home, make_skill, monkeypatch, capsys):
    sentinel = "DO_NOT_ECHO_JOURNAL_SENTINEL"
    write_journal({
        "schema_version": SCHEMA_VERSION,
        "operation_id": "invalid-ready",
        "operation": "promote",
        "phase": "planned",
        "name": sentinel,
    })
    monkeypatch.setattr(plugin, "_MIDDLEWARE_AVAILABLE", True)
    assert handle_cli(_parse("doctor", "--json")) == 2
    assert sentinel not in capsys.readouterr().out

    source = make_skill(isolated_home["local"], name="unrelated-skill")
    with pytest.raises(PublisherError, match="invalid durable transaction"):
        reconcile()
    with pytest.raises(PublisherError, match="invalid durable transaction"):
        promote(source)
    assert source.is_dir()
    assert not (isolated_home["shared"] / "unrelated-skill").exists()

    assert handle_cli(_parse("status", "--json")) == 0
    status = capsys.readouterr().out
    assert sentinel not in status
    transaction = json.loads(status)["transactions"][0]
    assert transaction["status"] == "invalid"
    assert transaction["code"] == "skill_publisher.journal_invalid"


def test_audit_status_and_doctor_never_echo_invalid_state_or_arguments(isolated_home, monkeypatch, capsys):
    sentinels = {
        "name": "DO_NOT_ECHO_NAME_SENTINEL",
        "action": "DO_NOT_ECHO_ACTION_SENTINEL",
        "registry": "DO_NOT_ECHO_REGISTRY_SENTINEL",
        "journal": "DO_NOT_ECHO_JOURNAL_SENTINEL",
        "audit": "DO_NOT_ECHO_AUDIT_SENTINEL",
    }
    on_post_tool_call(
        tool_name="skill_manage",
        args={"name": sentinels["name"], "action": sentinels["action"]},
        status="error",
        error_message="blocked",
    )
    audit_path = state_root() / "audit.jsonl"
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"timestamp": sentinels["audit"], "event": sentinels["audit"], "result": sentinels["audit"]}) + "\n")

    record = {
        "canonical_path": sentinels["registry"],
        "source_relpath": sentinels["registry"],
        "digest": sentinels["registry"],
        "scope": "shared",
        "adapter_links": {},
    }
    save_registry({"schema_version": SCHEMA_VERSION, "publications": {sentinels["registry"]: record}})
    write_journal({
        "schema_version": SCHEMA_VERSION,
        "operation_id": "sentinel-journal",
        "operation": "promote",
        "phase": "planned",
        "name": sentinels["journal"],
    })
    monkeypatch.setattr(plugin, "_MIDDLEWARE_AVAILABLE", True)

    for command in (("audit", "--limit", "50"), ("status",), ("doctor",)):
        handle_cli(_parse(*command))
        human = capsys.readouterr().out
        handle_cli(_parse(*command, "--json"))
        machine = capsys.readouterr().out
        for sentinel in sentinels.values():
            assert sentinel not in human
            assert sentinel not in machine


def test_unpublish_retry_fsyncs_absent_adapter_before_proof_removal(isolated_home, make_skill, monkeypatch):
    import hermes_skill_publisher.filesystem as filesystem
    import hermes_skill_publisher.publisher as publisher

    publication = promote(make_skill(isolated_home["local"]))
    _committed_unpublish_journal(isolated_home, publication)
    adapter = isolated_home["adapter"] / "demo-skill"
    real_fsync = filesystem.fsync_dir
    failed = False

    def fail_after_unlink(path):
        nonlocal failed
        if Path(path) == isolated_home["adapter"] and not failed:
            failed = True
            raise OSError("injected adapter parent EIO")
        return real_fsync(path)

    monkeypatch.setattr(filesystem, "fsync_dir", fail_after_unlink)
    assert recover()[0]["result"] == "blocked"
    assert not adapter.exists() and not adapter.is_symlink()
    assert list_journals()
    assert "demo-skill" in load_registry()["publications"]

    events = []
    real_save = publisher.save_registry
    real_remove = publisher.remove_journal

    def record_fsync(path):
        if Path(path) == isolated_home["adapter"]:
            events.append("adapter_fsync")
        return real_fsync(path)

    def record_save(registry):
        events.append("registry_removed")
        return real_save(registry)

    def record_remove(op_id):
        events.append("journal_removed")
        return real_remove(op_id)

    monkeypatch.setattr(filesystem, "fsync_dir", record_fsync)
    monkeypatch.setattr(publisher, "save_registry", record_save)
    monkeypatch.setattr(publisher, "remove_journal", record_remove)
    assert recover()[0]["result"] == "recovered"
    assert events.index("adapter_fsync") < events.index("registry_removed") < events.index("journal_removed")
    assert list_journals() == []
    assert "demo-skill" not in load_registry()["publications"]


def test_digest_distinguishes_exact_execute_masks_for_files_and_directories(tmp_path):
    package = tmp_path / "skill"
    package.mkdir()
    script = package / "run.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    nested = package / "empty"
    nested.mkdir()
    masks = (0o000, 0o001, 0o010, 0o011, 0o100, 0o101, 0o110, 0o111)

    file_digests = set()
    for mask in masks:
        script.chmod(0o600 | mask)
        file_digests.add(package_digest(package))
    assert len(file_digests) == len(masks)

    script.chmod(0o600)
    directory_digests = set()
    for mask in masks:
        nested.chmod(0o600 | mask)
        try:
            directory_digests.add(package_digest(package))
        finally:
            nested.chmod(0o700)
    assert len(directory_digests) == len(masks)

    before = package_digest(package)
    os.utime(script, (1, 1))
    os.utime(nested, (2, 2))
    assert package_digest(package) == before


def test_release_hygiene_rejects_mutable_actions_and_unbounded_requirements():
    assert workflow_errors("steps:\n  - uses: actions/checkout@v4\n")
    assert workflow_errors("steps:\n  - run: python -m pip install --upgrade pip\n")
    minimum = __import__('scripts.check_public_repo', fromlist=['PINNED_HERMES_MINIMUM']).PINNED_HERMES_MINIMUM
    pinned = (
        "steps:\n  - uses: actions/checkout@" + "a" * 40 + " # v4\n"
        "    with:\n      repository: NousResearch/hermes-agent\n"
        f"      ref: {minimum}\n"
    )
    assert workflow_errors(pinned) == []

    unbounded = {
        "build-system": {"requires": ["setuptools>=68"]},
        "project": {
            "dependencies": ["PyYAML>=6"],
            "optional-dependencies": {"test": ["pytest>=8"]},
        },
    }
    errors = requirement_errors(unbounded)
    assert len(errors) == 3
    bounded = {
        "build-system": {"requires": ["setuptools>=68,<81"]},
        "project": {
            "dependencies": ["PyYAML>=6,<7"],
            "optional-dependencies": {"test": ["pytest>=8,<9"]},
        },
    }
    assert requirement_errors(bounded) == []


def test_readme_adapter_example_is_a_nested_mapping():
    readme = Path(__file__).resolve().parents[1].joinpath("README.md").read_text(encoding="utf-8")
    blocks = [block for block in readme.split("```yaml")[1:] if "adapter_roots:" in block and "claude:" in block]
    assert blocks
    document = yaml.safe_load(blocks[0].split("```", 1)[0])
    adapters = document["plugins"]["entries"]["hermes-skill-publisher"]["adapter_roots"]
    assert adapters == {
        "claude": "~/.claude/skills",
        "codex": "~/.codex/skills",
        "prime": "~/.prime/agent/skills",
    }
