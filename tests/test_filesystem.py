import os
from pathlib import Path
import socket

import pytest

from hermes_skill_publisher.filesystem import (
    SafetyError,
    package_digest,
    probe_rename_noreplace,
    rename_noreplace,
    safe_copy_tree,
    safe_remove_tree,
)


def test_rename_noreplace_never_replaces(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.write_text("source")
    target.write_text("target")
    with pytest.raises(FileExistsError):
        rename_noreplace(source, target)
    assert source.read_text() == "source"
    assert target.read_text() == "target"


def test_rename_noreplace_blocks_empty_directory(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    with pytest.raises(FileExistsError):
        rename_noreplace(source, target)
    assert source.is_dir() and target.is_dir()


def test_probe_supported_on_test_filesystem(tmp_path: Path):
    probe_rename_noreplace(tmp_path)


def test_digest_is_deterministic_and_tracks_executable_bit(tmp_path: Path):
    package = tmp_path / "skill"
    package.mkdir()
    script = package / "run.sh"
    script.write_text("#!/bin/sh\n")
    first = package_digest(package)
    os.utime(script, (1, 1))
    assert package_digest(package) == first
    script.chmod(0o755)
    assert package_digest(package) != first


def test_safe_copy_preserves_populated_read_only_directory_mode_despite_umask(tmp_path: Path):
    source = tmp_path / "source"
    nested = source / "nested"
    nested.mkdir(parents=True)
    nested.joinpath("file.txt").write_text("content", encoding="utf-8")
    nested.chmod(0o555)
    previous = os.umask(0o077)
    try:
        safe_copy_tree(source, tmp_path / "destination")
    finally:
        os.umask(previous)
    copied = tmp_path / "destination" / "nested"
    assert copied.stat().st_mode & 0o777 == 0o555
    assert copied.joinpath("file.txt").read_text(encoding="utf-8") == "content"


def test_safe_remove_tree_handles_populated_read_only_directory(tmp_path: Path):
    source = tmp_path / "source"
    nested = source / "nested"
    nested.mkdir(parents=True)
    nested.joinpath("file.txt").write_text("content", encoding="utf-8")
    nested.chmod(0o555)
    destination = tmp_path / "destination"
    digest = safe_copy_tree(source, destination)

    safe_remove_tree(destination, expected_digest=digest)

    assert not destination.exists()


def test_safe_copy_includes_support_files_and_exec_bit(tmp_path: Path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    (source / "scripts").mkdir(parents=True)
    script = source / "scripts" / "run.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    digest = safe_copy_tree(source, destination)
    assert digest == package_digest(destination)
    assert os.access(destination / "scripts" / "run.sh", os.X_OK)


@pytest.mark.parametrize("broken", [False, True])
def test_safe_copy_rejects_symlink_descendants(tmp_path: Path, broken: bool):
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "missing" if broken else tmp_path / "real"
    if not broken:
        target.write_text("x")
    (source / "link").symlink_to(target)
    with pytest.raises(SafetyError, match="symlinks"):
        safe_copy_tree(source, tmp_path / "destination")


def test_safe_copy_rejects_symlink_root(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    root = tmp_path / "root"
    root.symlink_to(real, target_is_directory=True)
    with pytest.raises(SafetyError, match="real directory"):
        safe_copy_tree(root, tmp_path / "destination")


def test_digest_rejects_fifo(tmp_path: Path):
    package = tmp_path / "skill"
    package.mkdir()
    os.mkfifo(package / "pipe")
    with pytest.raises(SafetyError, match="special files"):
        package_digest(package)


def test_digest_rejects_socket(tmp_path: Path):
    package = tmp_path / "skill"
    package.mkdir()
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.bind(str(package / "socket"))
        with pytest.raises(SafetyError, match="special files"):
            package_digest(package)
    finally:
        sock.close()


def test_package_limits(tmp_path: Path):
    package = tmp_path / "skill"
    package.mkdir()
    (package / "a").write_bytes(b"12")
    with pytest.raises(SafetyError, match="exceeds limits"):
        package_digest(package, max_files=0)
    with pytest.raises(SafetyError, match="exceeds limits"):
        package_digest(package, max_bytes=1)
