import fcntl
import multiprocessing
from pathlib import Path
import os
import time

import pytest
import yaml

from hermes_skill_publisher.filesystem import acquire_locks
from hermes_skill_publisher.state import state_lock_path


def _race_promote(source: str, queue):
    try:
        from hermes_skill_publisher.publisher import promote
        queue.put(("ok", promote(Path(source))["digest"]))
    except Exception as exc:
        queue.put(("error", str(exc)))


def test_same_skill_process_race_is_idempotent(isolated_home, make_skill):
    source = make_skill(isolated_home["local"])
    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    processes = [context.Process(target=_race_promote, args=(str(source), queue)) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0
    outcomes = [queue.get(timeout=2) for _ in processes]
    assert any(kind == "ok" for kind, _ in outcomes)
    target = isolated_home["shared"] / "demo-skill"
    assert target.is_dir()


def _profile_race(home: str, source: str, queue):
    os.environ["HERMES_HOME"] = home
    try:
        from hermes_skill_publisher.publisher import promote
        queue.put(("ok", promote(Path(source))["digest"]))
    except Exception as exc:
        queue.put(("error", str(exc)))


def test_two_profiles_sharing_root_have_one_winner(tmp_path, monkeypatch, make_skill):
    shared = tmp_path / "shared"
    shared.mkdir()
    profiles = []
    for index in range(2):
        home = tmp_path / f"profile-{index}"
        local = home / "skills"
        local.mkdir(parents=True)
        source = make_skill(local)
        (source / "marker").write_text(str(index))
        config = {
            "skills": {"external_dirs": [str(shared)]},
            "plugins": {"enabled": ["hermes-skill-publisher"], "entries": {"hermes-skill-publisher": {"shared_root": str(shared), "adapter_roots": {}}}},
        }
        (home / "config.yaml").write_text(yaml.safe_dump(config))
        profiles.append((home, source))
    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    processes = [context.Process(target=_profile_race, args=(str(home), str(source), queue)) for home, source in profiles]
    for process in processes:
        process.start()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0
    outcomes = [queue.get(timeout=2) for _ in processes]
    assert sum(kind == "ok" for kind, _ in outcomes) == 1
    assert (shared / "demo-skill" / "marker").read_text() in {"0", "1"}
    assert sum(source.exists() for _, source in profiles) == 1


def test_lock_timeout(isolated_home):
    lock = state_lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    held = lock.open("a+b")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX)
    try:
        with pytest.raises(TimeoutError):
            with acquire_locks(lock, [], timeout=0.05):
                pass
    finally:
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()
