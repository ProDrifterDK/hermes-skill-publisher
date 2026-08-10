"""Lifecycle composition: blocked recovery is a global mutation barrier."""

from pathlib import Path

import pytest

from hermes_skill_publisher import plugin
from hermes_skill_publisher.filesystem import package_digest, rename_noreplace, safe_copy_tree
from hermes_skill_publisher.publisher import recover
from hermes_skill_publisher.state import SCHEMA_VERSION, list_journals, load_registry, save_registry, write_journal


@pytest.fixture(autouse=True)
def _middleware_on(monkeypatch):
    monkeypatch.setattr(plugin, "_MIDDLEWARE_AVAILABLE", True)


def _drifted_registry_recovery(env, make_skill):
    """A blocked recovery: journal-owned stage+backup plus a drifted registry."""
    source = make_skill(env["local"])
    config_local = env["local"]
    from hermes_skill_publisher.config import load_config
    config = load_config()
    name = source.name
    digest = package_digest(source)
    op = "demo-blocked"
    stage = config.shared_root / f".hermes-skill-publisher-stage-{op}"
    backup = source.parent / f".hermes-skill-publisher-backup-{op}"
    target = config.shared_root / name
    adapters = {"other": {"path": str(config.adapter_roots["other"] / name), "link_text": "../../.agents/skills/demo-skill"}}
    publication = {"canonical_path": str(target), "source_relpath": name, "digest": digest, "scope": "shared", "created_at": "test", "adapter_links": adapters}
    journal = {"schema_version": SCHEMA_VERSION, "operation_id": op, "operation": "promote", "phase": "source_parked", "name": name, "digest": digest, "source_path": str(source), "source_relpath": name, "backup_path": str(backup), "stage_path": str(stage), "target_path": str(target), "adapters": adapters, "created_adapters": [], "publication": publication}
    safe_copy_tree(source, stage)
    rename_noreplace(source, backup)
    write_journal(journal)
    save_registry({"schema_version": SCHEMA_VERSION, "publications": {name: {"canonical_path": str(target), "digest": "sha256:different"}}})
    return journal, target


def test_session_start_blocked_recovery_bars_reconcile_and_publish(isolated_home, make_skill):
    journal, target = _drifted_registry_recovery(isolated_home, make_skill)
    assert recover()[0]["result"] == "blocked"
    # Recreate the blocked journal state (the direct recover above preserved it).
    plugin.on_session_start(session_id="s1")
    # The drifted registry record must survive: reconcile may not act after a
    # blocked recovery, and no candidate may be promoted.
    assert load_registry()["publications"]["demo-skill"]["digest"] == "sha256:different"
    assert list_journals()
    assert not target.exists()
    assert Path(journal["backup_path"]).is_dir()


def test_session_end_blocked_recovery_bars_reconcile_and_publish(isolated_home, make_skill):
    _drifted_registry_recovery(isolated_home, make_skill)
    plugin.on_session_end(session_id="s1", completed=True, interrupted=False)
    assert load_registry()["publications"]["demo-skill"]["digest"] == "sha256:different"
    assert list_journals()


def test_pre_llm_call_runs_recovery_and_returns_none(isolated_home, make_skill):
    source = make_skill(isolated_home["local"])
    from hermes_skill_publisher.config import load_config
    config = load_config()
    digest = package_digest(source)
    op = "demo-pre-llm"
    stage = config.shared_root / f".hermes-skill-publisher-stage-{op}"
    backup = source.parent / f".hermes-skill-publisher-backup-{op}"
    target = config.shared_root / "demo-skill"
    adapters = {"other": {"path": str(config.adapter_roots["other"] / "demo-skill"), "link_text": "../../.agents/skills/demo-skill"}}
    publication = {"canonical_path": str(target), "source_relpath": "demo-skill", "digest": digest, "scope": "shared", "created_at": "test", "adapter_links": adapters}
    journal = {"schema_version": SCHEMA_VERSION, "operation_id": op, "operation": "promote", "phase": "source_parked", "name": "demo-skill", "digest": digest, "source_path": str(source), "source_relpath": "demo-skill", "backup_path": str(backup), "stage_path": str(stage), "target_path": str(target), "adapters": adapters, "created_adapters": [], "publication": publication}
    safe_copy_tree(source, stage)
    rename_noreplace(source, backup)
    write_journal(journal)
    # pre_llm_call fires on every turn of a resumed session; recovery must run
    # there even though on_session_start never fires for a resumed session.
    assert plugin.on_pre_llm_call(session_id="resumed", is_first_turn=False) is None
    assert target.is_dir()
    assert list_journals() == []


def test_write_approval_bars_lifecycle_publication(isolated_home, make_skill):
    import yaml
    source = make_skill(isolated_home["local"])
    config = isolated_home["config"]
    config["skills"]["write_approval"] = True
    (isolated_home["hermes"] / "config.yaml").write_text(yaml.safe_dump(config))
    assert plugin.on_pre_llm_call(session_id="resumed", is_first_turn=False) is None
    assert source.is_dir()
    assert not (isolated_home["shared"] / "demo-skill").exists()


def test_unreadable_journal_bars_lifecycle_mutation(isolated_home, make_skill):
    from hermes_skill_publisher.state import state_root
    transactions = state_root() / "transactions"
    transactions.mkdir(parents=True, exist_ok=True)
    (transactions / "corrupt.json").write_text("not json")
    make_skill(isolated_home["local"])  # classified shared candidate
    plugin.on_session_end(session_id="s1", completed=True, interrupted=False)
    # The unreadable journal barred promotion of the pending candidate.
    assert (isolated_home["local"] / "demo-skill").is_dir()
    assert not (isolated_home["shared"] / "demo-skill").exists()
