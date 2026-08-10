from pathlib import Path

import pytest

from hermes_skill_publisher.frontmatter import classify_content, parse_document
from hermes_skill_publisher.publisher import PublisherError, promote, recover, unpublish
from hermes_skill_publisher.state import SCHEMA_VERSION, list_journals, load_registry, save_registry, write_journal


def test_unpublish_restores_recorded_category_and_body(isolated_home, make_skill):
    source = make_skill(isolated_home["local"], category="category")
    original = (source / "SKILL.md").read_text()
    _, body, _ = parse_document(original)
    promote(source)
    result = unpublish("demo-skill", "private")
    local = isolated_home["local"] / "category" / "demo-skill"
    assert Path(result["path"]) == local
    assert classify_content((local / "SKILL.md").read_text()).value == "private"
    assert parse_document((local / "SKILL.md").read_text())[1] == body
    assert not (isolated_home["shared"] / "demo-skill").exists()
    assert not (isolated_home["adapter"] / "demo-skill").exists()
    assert "demo-skill" not in load_registry()["publications"]


def test_unpublish_restores_skill_with_populated_read_only_directory(isolated_home, make_skill):
    source = make_skill(isolated_home["local"])
    nested = source / "readonly"
    nested.mkdir()
    nested.joinpath("payload.txt").write_text("content", encoding="utf-8")
    nested.chmod(0o555)
    promote(source)

    result = unpublish("demo-skill", "local")

    local = Path(result["path"])
    assert local.joinpath("readonly", "payload.txt").read_text(encoding="utf-8") == "content"
    assert local.joinpath("readonly").stat().st_mode & 0o777 == 0o555
    assert not (isolated_home["shared"] / "demo-skill").exists()
    assert not any(isolated_home["shared"].glob(".hermes-skill-publisher-backup-*"))
    assert not (isolated_home["adapter"] / "demo-skill").exists()
    assert "demo-skill" not in load_registry()["publications"]
    assert list_journals() == []


def test_unpublish_falls_back_flat_when_category_removed(isolated_home, make_skill):
    source = make_skill(isolated_home["local"], category="old")
    promote(source)
    (isolated_home["local"] / "old").rmdir()
    result = unpublish("demo-skill", "local")
    assert Path(result["path"]) == isolated_home["local"] / "demo-skill"


@pytest.mark.parametrize("phase", ["source_parked", "target_committed", "adapters_committed"])
def test_unpublish_fault_before_registry_rolls_back(isolated_home, make_skill, monkeypatch, phase):
    import hermes_skill_publisher.publisher as publisher
    promote(make_skill(isolated_home["local"]))
    original = publisher._journal_phase
    raised = False

    def fail(journal, current, **updates):
        nonlocal raised
        original(journal, current, **updates)
        if current == phase and not raised:
            raised = True
            raise OSError(f"fault after {phase}")

    monkeypatch.setattr(publisher, "_journal_phase", fail)
    with pytest.raises(PublisherError):
        unpublish("demo-skill", "local")
    assert (isolated_home["shared"] / "demo-skill").is_dir()
    assert not (isolated_home["local"] / "demo-skill").exists()
    assert (isolated_home["adapter"] / "demo-skill").is_symlink()
    assert "demo-skill" in load_registry()["publications"]
    assert list_journals() == []


def test_unpublish_fault_after_registry_preserves_local_commit(isolated_home, make_skill, monkeypatch):
    import hermes_skill_publisher.publisher as publisher
    promote(make_skill(isolated_home["local"]))
    original = publisher._journal_phase
    raised = False

    def fail(journal, current, **updates):
        nonlocal raised
        original(journal, current, **updates)
        if current == "registry_committed" and not raised:
            raised = True
            raise OSError("fault after registry")

    monkeypatch.setattr(publisher, "_journal_phase", fail)
    result = unpublish("demo-skill", "private")
    local = Path(result["path"])
    assert local.is_dir() and not (isolated_home["shared"] / "demo-skill").exists()
    assert "demo-skill" not in load_registry()["publications"]
    monkeypatch.setattr(publisher, "_journal_phase", original)
    recover()
    assert local.is_dir() and list_journals() == []


def test_unpublish_registry_removal_blocks_before_rollback_mutation(isolated_home, make_skill, monkeypatch):
    import hermes_skill_publisher.publisher as publisher
    promote(make_skill(isolated_home["local"]))
    original = publisher._journal_phase
    changed = False

    def remove_ownership(journal, current, **updates):
        nonlocal changed
        original(journal, current, **updates)
        if current == "source_parked" and not changed:
            changed = True
            save_registry({"schema_version": SCHEMA_VERSION, "publications": {}})
            raise OSError("registry removed")

    monkeypatch.setattr(publisher, "_journal_phase", remove_ownership)
    with pytest.raises(PublisherError, match="rollback is blocked"):
        unpublish("demo-skill", "local")
    assert "demo-skill" not in load_registry()["publications"]
    assert not (isolated_home["local"] / "demo-skill").exists()
    assert any(isolated_home["local"].glob(".hermes-skill-publisher-stage-*"))
    assert any(isolated_home["shared"].glob(".hermes-skill-publisher-backup-*"))
    assert list_journals()


def test_unpublish_registry_drift_blocks_before_rollback_mutation(isolated_home, make_skill, monkeypatch):
    import hermes_skill_publisher.publisher as publisher
    promote(make_skill(isolated_home["local"]))
    original = publisher._journal_phase
    changed = False

    def drift(journal, current, **updates):
        nonlocal changed
        original(journal, current, **updates)
        if current == "adapters_committed" and not changed:
            changed = True
            save_registry({"schema_version": SCHEMA_VERSION, "publications": {"demo-skill": {"canonical_path": journal["target_path"], "digest": "sha256:external"}}})
            raise OSError("registry drift")

    monkeypatch.setattr(publisher, "_journal_phase", drift)
    with pytest.raises(PublisherError, match="rollback is blocked"):
        unpublish("demo-skill", "local")
    assert load_registry()["publications"]["demo-skill"]["digest"] == "sha256:external"
    assert (isolated_home["local"] / "demo-skill").is_dir()
    assert any(isolated_home["shared"].glob(".hermes-skill-publisher-backup-*"))
    assert list_journals()


def test_unpublish_never_overwrites_local_destination(isolated_home, make_skill):
    promote(make_skill(isolated_home["local"]))
    collision = isolated_home["local"] / "demo-skill"
    collision.mkdir()
    with pytest.raises((PublisherError, Exception), match="exists"):
        unpublish("demo-skill", "local")
    assert collision.is_dir()
    assert (isolated_home["shared"] / "demo-skill").is_dir()


def _unpublish_journal(env, name="demo-skill", scope="local"):
    from hermes_skill_publisher.config import load_config
    from hermes_skill_publisher.filesystem import package_digest
    config = load_config()
    target = config.shared_root / name
    registry = load_registry()
    publication = registry["publications"][name]
    op = "demo-unpublish"
    local = config.local_root / name
    journal = {
        "schema_version": SCHEMA_VERSION,
        "operation_id": op,
        "operation": "unpublish",
        "phase": "source_parked",
        "name": name,
        "scope": scope,
        "target_path": str(target),
        "backup_path": str(config.shared_root / f".hermes-skill-publisher-backup-{op}"),
        "local_path": str(local),
        "local_relpath": name,
        "stage_path": str(local.parent / f".hermes-skill-publisher-stage-{op}"),
        "canonical_digest": package_digest(target),
        "local_digest": None,
        "adapters": publication["adapter_links"],
        "publication": publication,
    }
    return journal, target, local


def test_unpublish_journal_traversal_escape_is_blocked(isolated_home, make_skill, tmp_path):
    promote(make_skill(isolated_home["local"]))
    journal, target, local = _unpublish_journal(isolated_home)
    escaped = isolated_home["local"].parent / "escaped" / "demo-skill"
    journal["local_path"] = str(escaped)
    journal["local_relpath"] = "../escaped/demo-skill"
    journal["stage_path"] = str(escaped.parent / f".hermes-skill-publisher-stage-{journal['operation_id']}")
    write_journal(journal)
    result = recover()
    assert result[0]["result"] == "blocked"
    assert not escaped.exists()
    assert target.is_dir()
    assert "demo-skill" in load_registry()["publications"]
    assert list_journals()


def test_unpublish_journal_absolute_local_path_mismatch_is_blocked(isolated_home, make_skill):
    promote(make_skill(isolated_home["local"]))
    journal, target, _ = _unpublish_journal(isolated_home)
    # A serialized absolute local_path that disagrees with the safe rederived
    # destination is never trusted, even though both are textually in-root.
    journal["local_path"] = str(isolated_home["local"] / "category" / "demo-skill")
    write_journal(journal)
    assert recover()[0]["result"] == "blocked"
    assert target.is_dir() and list_journals()
    assert not (isolated_home["local"] / "category" / "demo-skill").exists()


def test_unpublish_journal_symlinked_ancestor_is_blocked(isolated_home, make_skill, tmp_path):
    promote(make_skill(isolated_home["local"]))
    journal, target, _ = _unpublish_journal(isolated_home)
    outside = tmp_path / "outside"
    outside.mkdir()
    category = isolated_home["local"] / "category"
    category.symlink_to(outside, target_is_directory=True)
    journal["local_relpath"] = "category/demo-skill"
    journal["local_path"] = str(isolated_home["local"] / "category" / "demo-skill")
    journal["stage_path"] = str(isolated_home["local"] / "category" / f".hermes-skill-publisher-stage-{journal['operation_id']}")
    write_journal(journal)
    assert recover()[0]["result"] == "blocked"
    assert not (outside / "demo-skill").exists()
    assert target.is_dir() and list_journals()


def test_unpublish_recovery_rejects_changed_backup(isolated_home, make_skill):
    from hermes_skill_publisher.filesystem import rename_noreplace, safe_copy_tree
    promote(make_skill(isolated_home["local"]))
    journal, target, local = _unpublish_journal(isolated_home)
    backup = Path(journal["backup_path"])
    rename_noreplace(target, backup)
    (backup / "SKILL.md").write_text("tampered")
    write_journal(journal)
    assert recover()[0]["result"] == "blocked"
    assert backup.exists() and not target.exists() and not local.exists()
    assert list_journals()
    assert "demo-skill" in load_registry()["publications"]


def test_unpublish_recovery_rejects_changed_stage(isolated_home, make_skill):
    from hermes_skill_publisher.filesystem import package_digest, rename_noreplace, safe_copy_tree
    promote(make_skill(isolated_home["local"]))
    journal, target, local = _unpublish_journal(isolated_home)
    backup = Path(journal["backup_path"])
    stage = Path(journal["stage_path"])
    safe_copy_tree(target, stage)
    rename_noreplace(target, backup)
    journal["local_digest"] = package_digest(stage)
    journal["phase"] = "source_parked"
    write_journal(journal)
    (stage / "SKILL.md").write_text("tampered")
    assert recover()[0]["result"] == "blocked"
    assert stage.exists() and backup.exists() and not local.exists()
    assert list_journals()


def test_unpublish_recovery_local_conflict_is_blocked(isolated_home, make_skill):
    from hermes_skill_publisher.filesystem import package_digest, rename_noreplace, safe_copy_tree
    from hermes_skill_publisher.frontmatter import rewrite_scope_bytes
    promote(make_skill(isolated_home["local"]))
    journal, target, local = _unpublish_journal(isolated_home)
    backup = Path(journal["backup_path"])
    stage = Path(journal["stage_path"])
    safe_copy_tree(target, stage)
    stage.joinpath("SKILL.md").write_bytes(rewrite_scope_bytes(stage.joinpath("SKILL.md").read_bytes(), "local"))
    rename_noreplace(target, backup)
    journal["local_digest"] = package_digest(stage)
    journal["phase"] = "source_parked"
    write_journal(journal)
    # An external object now occupies the local destination; recovery must
    # never overwrite it to commit the staged package.
    local.mkdir()
    (local / "mine").write_text("external")
    assert recover()[0]["result"] == "blocked"
    assert (local / "mine").read_text() == "external"
    assert stage.exists() and backup.exists() and list_journals()


def test_unpublish_recovery_target_reappeared_preserves_every_object(isolated_home, make_skill):
    from hermes_skill_publisher.filesystem import package_digest, rename_noreplace, safe_copy_tree
    from hermes_skill_publisher.frontmatter import rewrite_scope_bytes
    promote(make_skill(isolated_home["local"]))
    journal, target, local = _unpublish_journal(isolated_home)
    backup = Path(journal["backup_path"])
    stage = Path(journal["stage_path"])
    safe_copy_tree(target, stage)
    stage.joinpath("SKILL.md").write_bytes(rewrite_scope_bytes(stage.joinpath("SKILL.md").read_bytes(), "local"))
    journal["local_digest"] = package_digest(stage)
    journal["phase"] = "source_parked"
    rename_noreplace(target, backup)
    safe_copy_tree(backup, target)  # external reappearance creates ambiguity
    write_journal(journal)
    assert recover()[0]["result"] == "blocked"
    assert target.is_dir() and backup.is_dir() and stage.is_dir()
    assert not local.exists() and list_journals()
    assert "demo-skill" in load_registry()["publications"]


def test_unpublish_recovery_rejects_stage_scope_mismatch(isolated_home, make_skill):
    from hermes_skill_publisher.filesystem import package_digest, rename_noreplace, safe_copy_tree
    promote(make_skill(isolated_home["local"]))
    journal, target, local = _unpublish_journal(isolated_home)
    backup = Path(journal["backup_path"])
    stage = Path(journal["stage_path"])
    safe_copy_tree(target, stage)
    rename_noreplace(target, backup)
    # Stage still declares scope=shared while the journal demands local.
    journal["local_digest"] = package_digest(stage)
    journal["phase"] = "source_parked"
    write_journal(journal)
    assert recover()[0]["result"] == "blocked"
    assert stage.exists() and backup.exists() and not local.exists()
    assert list_journals()


def test_unpublish_planned_journal_survives_fault_before_copy(isolated_home, make_skill, monkeypatch):
    import hermes_skill_publisher.publisher as publisher
    promote(make_skill(isolated_home["local"]))
    monkeypatch.setattr(publisher, "safe_copy_tree", lambda *a, **k: (_ for _ in ()).throw(OSError("copy fault")))
    with pytest.raises(PublisherError):
        unpublish("demo-skill", "local")
    # In-process failure: the inverse rollback releases the planned journal and
    # leaves canonical ownership completely untouched.
    assert (isolated_home["shared"] / "demo-skill").is_dir()
    assert "demo-skill" in load_registry()["publications"]
    assert not any(isolated_home["local"].glob(".hermes-skill-publisher-stage-*"))
    assert list_journals() == []


def _unpublish_kill_child(name: str, kill_at: str):
    import os
    import hermes_skill_publisher.publisher as publisher
    if kill_at == "copy":
        original = publisher.safe_copy_tree
        def partial(target, stage):
            stage.mkdir()
            (stage / "SKILL.md").write_text("partial")
            os._exit(73)
        publisher.safe_copy_tree = partial
    elif kill_at == "rewrite":
        def die(*a, **k):
            os._exit(74)
        publisher.rewrite_scope_bytes = die
    publisher.unpublish(name, "local")


def test_unpublish_hard_kill_during_copy_blocks_and_preserves(isolated_home, make_skill):
    import multiprocessing
    promote(make_skill(isolated_home["local"]))
    context = multiprocessing.get_context("fork")
    process = context.Process(target=_unpublish_kill_child, args=("demo-skill", "copy"))
    process.start()
    process.join(15)
    assert process.exitcode == 73
    # The stage was interrupted before its digest was journaled and is not a
    # pristine canonical copy: recovery refuses to adopt or discard it.
    assert recover()[0]["result"] == "blocked"
    assert (isolated_home["shared"] / "demo-skill").is_dir()
    assert any(isolated_home["local"].glob(".hermes-skill-publisher-stage-*"))
    assert list_journals()


def test_unpublish_hard_kill_before_rewrite_recovers(isolated_home, make_skill):
    import multiprocessing
    promote(make_skill(isolated_home["local"]))
    context = multiprocessing.get_context("fork")
    process = context.Process(target=_unpublish_kill_child, args=("demo-skill", "rewrite"))
    process.start()
    process.join(15)
    assert process.exitcode == 74
    # The stage is a verifiably pristine pre-rewrite copy of the canonical
    # package, so recovery discards it and releases the journal.
    assert recover()[0]["result"] == "recovered"
    assert (isolated_home["shared"] / "demo-skill").is_dir()
    assert not any(isolated_home["local"].glob(".hermes-skill-publisher-stage-*"))
    assert "demo-skill" in load_registry()["publications"]
    assert list_journals() == []


def test_unpublish_preserves_exact_body_bytes(isolated_home, make_skill):
    from hermes_skill_publisher.frontmatter import parse_document_bytes
    bodies = [
        b"Body\r\nExact\r\n",
        b"lone\rcarriage\rreturns",
        b"no final newline",
        "nön-ascïi body with émojis \n".encode("utf-8"),
    ]
    for index, body in enumerate(bodies):
        name = f"byte-skill-{index}"
        package = isolated_home["local"] / name
        package.mkdir()
        frontmatter = (
            f"---\nname: {name}\ndescription: byte test\nmetadata:\n  skill-publisher-scope: shared\n---\n"
        ).encode("utf-8")
        (package / "SKILL.md").write_bytes(frontmatter + body)
        promote(package)
        result = unpublish(name, "local")
        restored = Path(result["path"]) / "SKILL.md"
        assert parse_document_bytes(restored.read_bytes())[1] == body
