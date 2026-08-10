import os
from pathlib import Path

import pytest

from hermes_skill_publisher.config import load_config
from hermes_skill_publisher.filesystem import SafetyError, package_digest
from hermes_skill_publisher.publisher import PublisherError, promote, publish_pending, recover
from hermes_skill_publisher.state import list_journals, load_registry


def test_promotion_commits_complete_package(isolated_home, make_skill):
    source = make_skill(isolated_home["local"])
    (source / "references").mkdir()
    (source / "references" / "guide.md").write_text("guide")
    (source / "scripts").mkdir()
    script = source / "scripts" / "run.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    publication = promote(source)
    target = isolated_home["shared"] / "demo-skill"
    assert not source.exists()
    assert (target / "references" / "guide.md").read_text() == "guide"
    assert os.access(target / "scripts" / "run.sh", os.X_OK)
    link = isolated_home["adapter"] / "demo-skill"
    assert link.is_symlink() and not os.path.isabs(os.readlink(link))
    assert publication["digest"] == package_digest(target)
    assert load_registry()["publications"]["demo-skill"] == publication
    assert list_journals() == []


def test_promotion_cleans_populated_read_only_backup(isolated_home, make_skill):
    source = make_skill(isolated_home["local"])
    nested = source / "readonly"
    nested.mkdir()
    nested.joinpath("payload.txt").write_text("content", encoding="utf-8")
    nested.chmod(0o555)

    publication = promote(source)

    target = Path(publication["canonical_path"])
    assert target.joinpath("readonly", "payload.txt").read_text(encoding="utf-8") == "content"
    assert target.joinpath("readonly").stat().st_mode & 0o777 == 0o555
    assert not any(isolated_home["local"].glob(".hermes-skill-publisher-backup-*"))
    assert list_journals() == []


def test_promotion_cleanup_failure_after_permission_widening_recovers(
    isolated_home, make_skill, monkeypatch
):
    import hermes_skill_publisher.filesystem as filesystem

    source = make_skill(isolated_home["local"])
    nested = source / "readonly"
    nested.mkdir()
    nested.joinpath("payload.txt").write_text("content", encoding="utf-8")
    nested.chmod(0o555)
    original_fchmod = filesystem.os.fchmod
    failed = False

    def widen_then_fail(fd, mode):
        nonlocal failed
        original_fchmod(fd, mode)
        backups = list(isolated_home["local"].glob(".hermes-skill-publisher-backup-*"))
        if not failed and mode == 0o755 and backups:
            backup = backups[0]
            assert backup.joinpath("readonly").stat().st_mode & 0o777 == 0o755
            failed = True
            raise OSError("fault after permission widening")

    monkeypatch.setattr(filesystem.os, "fchmod", widen_then_fail)
    publication = promote(source)
    backup = next(isolated_home["local"].glob(".hermes-skill-publisher-backup-*"))
    assert failed
    assert package_digest(backup) == publication["digest"]
    assert list_journals()

    monkeypatch.setattr(filesystem.os, "fchmod", original_fchmod)
    assert recover()[0]["result"] == "recovered"
    assert not backup.exists()
    assert list_journals() == []


@pytest.mark.parametrize("collision", ["file", "directory", "broken-link"])
def test_target_collisions_never_overwritten(isolated_home, make_skill, collision):
    source = make_skill(isolated_home["local"])
    target = isolated_home["shared"] / "demo-skill"
    if collision == "file":
        target.write_text("mine")
    elif collision == "directory":
        target.mkdir()
    else:
        target.symlink_to(isolated_home["shared"] / "missing")
    with pytest.raises((PublisherError, SafetyError), match="exists"):
        promote(source)
    assert source.exists()
    assert target.exists() or target.is_symlink()


def test_unowned_same_target_adapter_is_conflict(isolated_home, make_skill):
    source = make_skill(isolated_home["local"])
    link = isolated_home["adapter"] / "demo-skill"
    link.symlink_to(os.path.relpath(isolated_home["shared"] / "demo-skill", isolated_home["adapter"]))
    with pytest.raises(PublisherError, match="unmanaged adapter"):
        promote(source)
    assert source.exists() and link.is_symlink()


def test_local_and_private_are_not_auto_published(isolated_home, make_skill):
    make_skill(isolated_home["local"], "local-skill", "local")
    make_skill(isolated_home["local"], "private-skill", "private")
    result = publish_pending()
    assert result == {"promoted": [], "blocked": []}


@pytest.mark.parametrize("phase", ["staged", "source_parked", "target_committed", "adapters_committed"])
def test_fault_after_phase_rolls_back(isolated_home, make_skill, monkeypatch, phase):
    import hermes_skill_publisher.publisher as publisher
    source = make_skill(isolated_home["local"])
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
        promote(source)
    assert source.exists()
    assert not (isolated_home["shared"] / "demo-skill").exists()
    assert not (isolated_home["adapter"] / "demo-skill").exists()
    assert list_journals() == []


@pytest.mark.parametrize("phase", ["registry_committed", "committed"])
def test_fault_after_registry_point_of_no_return_preserves_publication(isolated_home, make_skill, monkeypatch, phase):
    import hermes_skill_publisher.publisher as publisher
    source = make_skill(isolated_home["local"])
    original = publisher._journal_phase
    raised = False

    def fail(journal, current, **updates):
        nonlocal raised
        original(journal, current, **updates)
        if current == phase and not raised:
            raised = True
            raise OSError(f"fault after {phase}")

    monkeypatch.setattr(publisher, "_journal_phase", fail)
    publication = promote(source)
    target = isolated_home["shared"] / "demo-skill"
    assert target.is_dir() and not source.exists()
    assert load_registry()["publications"]["demo-skill"] == publication
    monkeypatch.setattr(publisher, "_journal_phase", original)
    recover()
    assert list_journals() == [] and target.is_dir()


def test_adapter_post_create_fsync_failure_leaves_no_orphan(isolated_home, make_skill, monkeypatch):
    import hermes_skill_publisher.filesystem as filesystem
    source = make_skill(isolated_home["local"])
    original = filesystem.fsync_dir

    def fail_adapter(path):
        if Path(path) == isolated_home["adapter"] and (isolated_home["adapter"] / "demo-skill").is_symlink():
            raise OSError("adapter fsync fault")
        return original(path)

    monkeypatch.setattr(filesystem, "fsync_dir", fail_adapter)
    with pytest.raises(PublisherError):
        promote(source)
    assert source.exists()
    assert not (isolated_home["adapter"] / "demo-skill").exists()
    assert not (isolated_home["shared"] / "demo-skill").exists()
    assert list_journals() == []


def test_existing_publication_with_duplicate_local_source_blocks(isolated_home, make_skill):
    source = make_skill(isolated_home["local"])
    promote(source)
    duplicate = make_skill(isolated_home["local"])
    with pytest.raises(PublisherError, match="both exist"):
        promote(duplicate)
    assert duplicate.exists() and (isolated_home["shared"] / "demo-skill").exists()


def test_symlinked_category_is_not_discovered(isolated_home, make_skill, tmp_path):
    external = make_skill(tmp_path, "outside-skill")
    (isolated_home["local"] / "linked").symlink_to(tmp_path, target_is_directory=True)
    result = publish_pending()
    assert result == {"promoted": [], "blocked": []}
    assert external.exists() and not (isolated_home["shared"] / "outside-skill").exists()


def test_late_write_at_park_boundary_is_never_stale_committed(isolated_home, make_skill, monkeypatch):
    import hermes_skill_publisher.publisher as publisher
    source = make_skill(isolated_home["local"])
    original = publisher._journal_phase

    def late_write(journal, phase, **updates):
        original(journal, phase, **updates)
        if phase == "staged":
            (source / "references").mkdir(exist_ok=True)
            (source / "references" / "late.md").write_text("late")

    monkeypatch.setattr(publisher, "_journal_phase", late_write)
    publication = promote(source)
    target = Path(publication["canonical_path"])
    assert (target / "references" / "late.md").read_text() == "late"
    assert publication["digest"] == package_digest(target)
    assert not source.exists() and list_journals() == []


def _late_write_child(source: str, ready: str, done: str):
    import time
    import hermes_skill_publisher.publisher as publisher
    original = publisher._journal_phase

    def hook(journal, phase, **updates):
        original(journal, phase, **updates)
        if phase == "staged":
            Path(ready).write_text("staged")
            deadline = time.monotonic() + 10
            while not Path(done).exists() and time.monotonic() < deadline:
                time.sleep(0.02)

    publisher._journal_phase = hook
    publisher.promote(Path(source))


def test_late_write_from_second_process_is_committed(isolated_home, make_skill, tmp_path):
    import multiprocessing
    import time
    source = make_skill(isolated_home["local"])
    ready = tmp_path / "ready"
    done = tmp_path / "done"
    context = multiprocessing.get_context("fork")
    process = context.Process(target=_late_write_child, args=(str(source), str(ready), str(done)))
    process.start()
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ready.exists()
    (source / "late.txt").write_text("late-from-other-process")
    done.write_text("go")
    process.join(20)
    assert process.exitcode == 0
    target = isolated_home["shared"] / "demo-skill"
    assert (target / "late.txt").read_text() == "late-from-other-process"
    assert package_digest(target) == load_registry()["publications"]["demo-skill"]["digest"]


def test_persistent_adapter_fsync_failure_preserves_journal(isolated_home, make_skill, monkeypatch):
    import hermes_skill_publisher.filesystem as filesystem
    source = make_skill(isolated_home["local"])
    original = filesystem.fsync_dir

    def always_fail(path):
        if Path(path) == isolated_home["adapter"]:
            raise OSError("persistent adapter fsync failure")
        return original(path)

    monkeypatch.setattr(filesystem, "fsync_dir", always_fail)
    with pytest.raises(PublisherError, match="rollback is blocked"):
        promote(source)
    # The durability proof of the created adapter must survive: the unlink was
    # never fsynced, so the journal remains for recovery.
    assert list_journals()
    monkeypatch.setattr(filesystem, "fsync_dir", original)
    assert recover()[0]["result"] == "recovered"
    assert (isolated_home["shared"] / "demo-skill").is_dir()
    assert list_journals() == []
