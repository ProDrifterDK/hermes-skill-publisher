import multiprocessing
import os
from pathlib import Path

import pytest
import yaml

from hermes_skill_publisher.config import load_config
from hermes_skill_publisher.filesystem import package_digest, rename_noreplace, safe_copy_tree
from hermes_skill_publisher.publisher import recover
from hermes_skill_publisher.state import SCHEMA_VERSION, list_journals, load_registry, save_registry, state_root, write_journal


def promotion_journal(env, source: Path, phase="staged"):
    config = load_config()
    name = source.name
    digest = package_digest(source)
    op = "demo-recovery"
    stage = config.shared_root / f".hermes-skill-publisher-stage-{op}"
    backup = source.parent / f".hermes-skill-publisher-backup-{op}"
    target = config.shared_root / name
    adapters = {"other": {"path": str(config.adapter_roots["other"] / name), "link_text": "../../.agents/skills/demo-skill"}}
    publication = {"canonical_path": str(target), "source_relpath": name, "digest": digest, "scope": "shared", "created_at": "test", "adapter_links": adapters}
    journal = {"schema_version": SCHEMA_VERSION, "operation_id": op, "operation": "promote", "phase": phase, "name": name, "digest": digest, "source_path": str(source), "source_relpath": name, "backup_path": str(backup), "stage_path": str(stage), "target_path": str(target), "adapters": adapters, "created_adapters": [], "publication": publication}
    return journal, stage, backup, target


def test_recovery_discards_matching_stage_and_retries_later(isolated_home, make_skill):
    source = make_skill(isolated_home["local"])
    journal, stage, _, _ = promotion_journal(isolated_home, source)
    safe_copy_tree(source, stage)
    write_journal(journal)
    result = recover()
    assert result[0]["result"] == "recovered"
    assert source.exists() and not stage.exists() and list_journals() == []


def test_recovery_resumes_stage_and_backup(isolated_home, make_skill):
    source = make_skill(isolated_home["local"])
    journal, stage, backup, target = promotion_journal(isolated_home, source)
    safe_copy_tree(source, stage)
    rename_noreplace(source, backup)
    journal["phase"] = "source_parked"
    write_journal(journal)
    assert recover()[0]["result"] == "recovered"
    assert target.is_dir() and not backup.exists()
    assert "demo-skill" in load_registry()["publications"]


def test_source_stage_and_backup_is_ambiguous(isolated_home, make_skill):
    source = make_skill(isolated_home["local"])
    journal, stage, backup, target = promotion_journal(isolated_home, source)
    safe_copy_tree(source, stage)
    safe_copy_tree(source, backup)
    write_journal(journal)
    result = recover()
    assert result[0]["result"] == "blocked"
    assert source.exists() and stage.exists() and backup.exists() and not target.exists()


def test_registry_drift_blocks_before_recovery_mutation(isolated_home, make_skill):
    source = make_skill(isolated_home["local"])
    journal, stage, backup, target = promotion_journal(isolated_home, source)
    safe_copy_tree(source, stage)
    rename_noreplace(source, backup)
    write_journal(journal)
    save_registry({"schema_version": SCHEMA_VERSION, "publications": {"demo-skill": {"canonical_path": str(target), "digest": "sha256:different"}}})
    result = recover()
    assert result[0]["result"] == "blocked"
    assert backup.exists() and stage.exists() and not source.exists() and not target.exists()
    assert load_registry()["publications"]["demo-skill"]["digest"] == "sha256:different"


def test_wrong_target_digest_is_never_touched(isolated_home, make_skill):
    source = make_skill(isolated_home["local"])
    journal, stage, backup, target = promotion_journal(isolated_home, source)
    safe_copy_tree(source, stage)
    rename_noreplace(source, backup)
    target.mkdir()
    (target / "external").write_text("mine")
    write_journal(journal)
    result = recover()
    assert result[0]["result"] == "blocked"
    assert (target / "external").read_text() == "mine"
    assert source.exists()
    assert list_journals()


def test_recovery_refuses_tampered_absolute_paths(isolated_home, make_skill, tmp_path):
    source = make_skill(isolated_home["local"])
    journal, stage, _, _ = promotion_journal(isolated_home, source)
    safe_copy_tree(source, stage)
    outside = tmp_path / "outside-target"
    outside.mkdir()
    (outside / "mine").write_text("external")
    journal["target_path"] = str(outside)
    write_journal(journal)
    result = recover()
    assert result[0]["result"] == "blocked"
    assert (outside / "mine").read_text() == "external"
    assert source.exists()


def test_recovery_refuses_unowned_exact_adapter(isolated_home, make_skill):
    source = make_skill(isolated_home["local"])
    journal, stage, backup, target = promotion_journal(isolated_home, source)
    safe_copy_tree(source, stage)
    rename_noreplace(source, backup)
    rename_noreplace(stage, target)
    adapter = journal["adapters"]["other"]
    Path(adapter["path"]).symlink_to(adapter["link_text"])
    write_journal(journal)
    result = recover()
    assert result[0]["result"] == "blocked"
    assert Path(adapter["path"]).is_symlink()
    assert "demo-skill" not in load_registry()["publications"]


def test_missing_target_rollback_removes_journal_owned_adapter_before_journal(isolated_home, make_skill):
    from hermes_skill_publisher.filesystem import safe_remove_tree
    source = make_skill(isolated_home["local"])
    journal, stage, backup, target = promotion_journal(isolated_home, source)
    safe_copy_tree(source, stage)
    rename_noreplace(source, backup)
    rename_noreplace(stage, target)
    adapter = journal["adapters"]["other"]
    Path(adapter["path"]).symlink_to(adapter["link_text"])
    journal["created_adapters"] = ["other"]
    safe_remove_tree(target, expected_digest=journal["digest"])
    write_journal(journal)
    assert recover()[0]["result"] == "recovered"
    assert source.is_dir() and not backup.exists()
    assert not Path(adapter["path"]).exists() and not Path(adapter["path"]).is_symlink()
    assert list_journals() == []


def test_deauthorized_root_rolls_back_to_local(isolated_home, make_skill):
    source = make_skill(isolated_home["local"])
    journal, stage, backup, _ = promotion_journal(isolated_home, source)
    safe_copy_tree(source, stage)
    rename_noreplace(source, backup)
    write_journal(journal)
    config = isolated_home["config"]
    config["skills"]["external_dirs"] = []
    (isolated_home["hermes"] / "config.yaml").write_text(yaml.safe_dump(config))
    assert recover()[0]["result"] == "recovered"
    assert source.exists() and not backup.exists() and not stage.exists()


def test_hard_exit_after_source_parked_recovers_next_boundary(isolated_home, make_skill):
    source = make_skill(isolated_home["local"])
    context = multiprocessing.get_context("fork")

    def child():
        import hermes_skill_publisher.publisher as publisher
        original = publisher._journal_phase
        def kill(journal, phase, **updates):
            original(journal, phase, **updates)
            if phase == "source_parked":
                os._exit(73)
        publisher._journal_phase = kill
        publisher.promote(source)

    process = context.Process(target=child)
    process.start()
    process.join(15)
    assert process.exitcode == 73
    assert recover()[0]["result"] == "recovered"
    assert (isolated_home["shared"] / "demo-skill").is_dir()
    assert list_journals() == []


def test_corrupt_journal_is_a_global_recovery_barrier(isolated_home, make_skill):
    source = make_skill(isolated_home["local"])
    journal, stage, _, _ = promotion_journal(isolated_home, source)
    safe_copy_tree(source, stage)
    write_journal(journal)
    transaction_root = state_root() / "transactions"
    (transaction_root / "corrupt.json").write_text("not json")
    results = recover()
    assert all(item["result"] == "blocked" for item in results)
    assert any(item["operation_id"] == "demo-recovery" for item in results)
    assert source.exists() and stage.exists()


def test_dotdot_journal_name_blocks(isolated_home, make_skill):
    source = make_skill(isolated_home["local"])
    journal, stage, _, _ = promotion_journal(isolated_home, source)
    safe_copy_tree(source, stage)
    journal["name"] = ".."
    write_journal(journal)
    assert recover()[0]["result"] == "blocked"
    assert source.exists() and stage.exists()


def test_duplicate_source_and_target_blocks(isolated_home, make_skill):
    source = make_skill(isolated_home["local"])
    journal, _, _, target = promotion_journal(isolated_home, source)
    safe_copy_tree(source, target)
    write_journal(journal)
    result = recover()
    assert result[0]["result"] == "blocked"
    assert source.exists() and target.exists()


def _deauthorize(isolated_home):
    config = isolated_home["config"]
    config["skills"]["external_dirs"] = []
    (isolated_home["hermes"] / "config.yaml").write_text(yaml.safe_dump(config))


@pytest.mark.parametrize("phase", ["planned", "staged", "source_parked", "target_committed", "adapters_committed"])
def test_deauthorized_rollback_before_registry_ownership(isolated_home, make_skill, phase):
    source = make_skill(isolated_home["local"])
    journal, stage, backup, target = promotion_journal(isolated_home, source, phase=phase)
    if phase != "planned":
        safe_copy_tree(source, stage)
    if phase in {"source_parked", "target_committed", "adapters_committed"}:
        rename_noreplace(source, backup)
    if phase in {"target_committed", "adapters_committed"}:
        rename_noreplace(stage, target)
    if phase == "adapters_committed":
        adapter = journal["adapters"]["other"]
        Path(adapter["path"]).symlink_to(adapter["link_text"])
        journal["created_adapters"] = ["other"]
    write_journal(journal)
    _deauthorize(isolated_home)
    assert recover()[0]["result"] == "recovered"
    assert source.exists()
    assert not target.exists() and not stage.exists() and not backup.exists()
    assert not Path(journal["adapters"]["other"]["path"]).exists()
    assert list_journals() == []


def test_deauthorized_registry_committed_point_of_no_return(isolated_home, make_skill):
    source = make_skill(isolated_home["local"])
    journal, stage, backup, target = promotion_journal(isolated_home, source, phase="registry_committed")
    rename_noreplace(source, backup)
    safe_copy_tree(backup, target)
    write_journal(journal)
    save_registry({"schema_version": SCHEMA_VERSION, "publications": {"demo-skill": journal["publication"]}})
    _deauthorize(isolated_home)
    result = recover()
    assert result[0]["result"] == "blocked"
    assert target.is_dir() and backup.is_dir() and not source.exists()
    assert "demo-skill" in load_registry()["publications"]
    assert list_journals()


def test_deauthorized_only_copy_after_backup_deletion_is_preserved(isolated_home, make_skill):
    source = make_skill(isolated_home["local"])
    journal, stage, backup, target = promotion_journal(isolated_home, source, phase="registry_committed")
    safe_copy_tree(source, target)
    rename_noreplace(source, backup)
    from hermes_skill_publisher.filesystem import safe_remove_tree
    safe_remove_tree(backup, expected_digest=journal["digest"])
    write_journal(journal)
    save_registry({"schema_version": SCHEMA_VERSION, "publications": {"demo-skill": journal["publication"]}})
    _deauthorize(isolated_home)
    result = recover()
    assert result[0]["result"] == "blocked"
    # The only remaining package copy, ownership, and journal all survive.
    assert target.is_dir() and not source.exists() and not backup.exists()
    assert "demo-skill" in load_registry()["publications"]
    assert list_journals()


def test_deauthorized_committed_phase_only_copy_is_preserved(isolated_home, make_skill):
    source = make_skill(isolated_home["local"])
    journal, stage, backup, target = promotion_journal(isolated_home, source, phase="committed")
    safe_copy_tree(source, target)
    rename_noreplace(source, backup)
    from hermes_skill_publisher.filesystem import safe_remove_tree
    safe_remove_tree(backup, expected_digest=journal["digest"])
    write_journal(journal)
    save_registry({"schema_version": SCHEMA_VERSION, "publications": {"demo-skill": journal["publication"]}})
    _deauthorize(isolated_home)
    assert recover()[0]["result"] == "blocked"
    assert target.is_dir() and list_journals()
    assert "demo-skill" in load_registry()["publications"]


def test_deauthorized_duplicate_source_and_target_never_chooses(isolated_home, make_skill):
    source = make_skill(isolated_home["local"])
    journal, _, _, target = promotion_journal(isolated_home, source)
    safe_copy_tree(source, target)
    write_journal(journal)
    _deauthorize(isolated_home)
    result = recover()
    assert result[0]["result"] == "blocked"
    assert source.exists() and target.exists()
    assert list_journals()


def test_deauthorized_duplicate_source_and_backup_never_chooses(isolated_home, make_skill):
    source = make_skill(isolated_home["local"])
    journal, stage, backup, _ = promotion_journal(isolated_home, source)
    safe_copy_tree(source, backup)
    write_journal(journal)
    _deauthorize(isolated_home)
    result = recover()
    assert result[0]["result"] == "blocked"
    assert source.exists() and backup.exists()
    assert list_journals()


def test_blocked_journal_blocks_new_promotion(isolated_home, make_skill):
    source = make_skill(isolated_home["local"])
    journal, _, _, target = promotion_journal(isolated_home, source)
    safe_copy_tree(source, target)  # ambiguity: source + target both exist
    write_journal(journal)
    assert recover()[0]["result"] == "blocked"
    from hermes_skill_publisher.publisher import PublisherError, promote
    with pytest.raises(PublisherError, match="transaction"):
        promote(source)
    assert source.exists() and target.exists() and list_journals()


def test_unreadable_journal_is_a_global_operation_barrier(isolated_home, make_skill):
    source = make_skill(isolated_home["local"])
    transaction_root = state_root() / "transactions"
    transaction_root.mkdir(parents=True, exist_ok=True)
    (transaction_root / "corrupt.json").write_text("not json")
    from hermes_skill_publisher.publisher import PublisherError, promote
    with pytest.raises(PublisherError, match="invalid durable transaction"):
        promote(source)
    assert source.exists()
