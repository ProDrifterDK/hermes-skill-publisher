import os
from pathlib import Path

import pytest

from hermes_skill_publisher.publisher import PublisherError, promote, reconcile
from hermes_skill_publisher.state import load_registry


def test_missing_owned_adapter_is_repaired(isolated_home, make_skill):
    promote(make_skill(isolated_home["local"]))
    link = isolated_home["adapter"] / "demo-skill"
    text = os.readlink(link)
    link.unlink()
    findings = reconcile()
    assert link.is_symlink() and os.readlink(link) == text
    assert any(item["result"].startswith("adapter_repaired") for item in findings)


def test_changed_owned_adapter_is_never_removed_or_repaired(isolated_home, make_skill, tmp_path):
    promote(make_skill(isolated_home["local"]))
    link = isolated_home["adapter"] / "demo-skill"
    link.unlink()
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    link.symlink_to(replacement)
    findings = reconcile()
    assert link.resolve() == replacement
    assert any(item["result"] == "blocked" for item in findings)


def test_deleted_canonical_cleans_exact_broken_adapter(isolated_home, make_skill):
    from hermes_skill_publisher.filesystem import safe_remove_tree
    source = make_skill(isolated_home["local"])
    publication = promote(source)
    target = Path(publication["canonical_path"])
    safe_remove_tree(target, expected_digest=publication["digest"])
    findings = reconcile()
    assert not (isolated_home["adapter"] / "demo-skill").exists()
    assert "demo-skill" not in load_registry()["publications"]
    assert any(item["result"] == "deleted_cleanup" for item in findings)


def test_malformed_registry_record_is_never_a_deletion_deputy(isolated_home, make_skill, tmp_path):
    from hermes_skill_publisher.state import SCHEMA_VERSION, save_registry
    victim_target = tmp_path / "victim-target"
    victim_target.mkdir()
    victim = tmp_path / "victim-link"
    victim.symlink_to(victim_target)
    record = {
        "canonical_path": str(isolated_home["shared"] / "demo-skill"),
        "source_relpath": "demo-skill",
        "digest": "sha256:" + "0" * 64,
        "scope": "shared",
        "created_at": "test",
        "adapter_links": {"evil": {"path": str(victim), "link_text": "victim-target"}},
    }
    save_registry({"schema_version": SCHEMA_VERSION, "publications": {"demo-skill": record}})
    findings = reconcile()
    assert victim.is_symlink()
    assert "demo-skill" in load_registry()["publications"]
    assert any(item["result"] == "blocked" for item in findings)


def test_registry_record_with_out_of_root_adapter_basename_mismatch_blocked(isolated_home, make_skill):
    from hermes_skill_publisher.state import SCHEMA_VERSION, save_registry
    promote(make_skill(isolated_home["local"]))
    registry = load_registry()
    record = registry["publications"]["demo-skill"]
    record["adapter_links"]["other"] = {"path": str(isolated_home["adapter"] / "other-name"), "link_text": "../../.agents/skills/demo-skill"}
    save_registry(registry)
    findings = reconcile()
    assert any(item["result"] == "blocked" for item in findings)
    assert "demo-skill" in load_registry()["publications"]


def test_reconcile_fsyncs_shared_root_before_ownership_removal(isolated_home, make_skill, monkeypatch):
    from hermes_skill_publisher.filesystem import safe_remove_tree
    import hermes_skill_publisher.publisher as publisher
    publication = promote(make_skill(isolated_home["local"]))
    safe_remove_tree(Path(publication["canonical_path"]), expected_digest=publication["digest"])
    calls = []
    original_fsync = publisher.fsync_dir
    original_save = publisher.save_registry

    def fsync_probe(path):
        if Path(path) == isolated_home["shared"]:
            calls.append("shared_fsync")
        return original_fsync(path)

    def save_probe(registry):
        calls.append("save_registry")
        return original_save(registry)

    monkeypatch.setattr(publisher, "fsync_dir", fsync_probe)
    monkeypatch.setattr(publisher, "save_registry", save_probe)
    findings = reconcile()
    assert any(item["result"] == "deleted_cleanup" for item in findings)
    assert "shared_fsync" in calls and "save_registry" in calls
    assert calls.index("shared_fsync") < calls.index("save_registry")


def test_delete_cleanup_fsyncs_shared_root_before_ownership_removal(isolated_home, make_skill, monkeypatch):
    import hermes_skill_publisher.publisher as publisher
    publication = promote(make_skill(isolated_home["local"]))
    target = Path(publication["canonical_path"])
    calls = []
    original_fsync = publisher.fsync_dir
    original_save = publisher.save_registry

    def fsync_probe(path):
        if Path(path) == isolated_home["shared"]:
            calls.append("shared_fsync")
        return original_fsync(path)

    def save_probe(registry):
        calls.append("save_registry")
        return original_save(registry)

    monkeypatch.setattr(publisher, "fsync_dir", fsync_probe)
    monkeypatch.setattr(publisher, "save_registry", save_probe)

    from hermes_skill_publisher.filesystem import safe_remove_tree
    safe_remove_tree(target, expected_digest=publication["digest"])
    from hermes_skill_publisher.publisher import cleanup_deleted
    cleanup_deleted("demo-skill", publication)
    assert "demo-skill" not in load_registry()["publications"]
    assert calls.index("shared_fsync") < calls.index("save_registry")
