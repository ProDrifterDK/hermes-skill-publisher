"""Linux no-overwrite and safe package filesystem primitives."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import secrets
import stat
import time
from typing import Callable, Iterator, Sequence

MAX_FILES = 1024
MAX_BYTES = 64 * 1024 * 1024
AT_FDCWD = -100
RENAME_NOREPLACE = 1


class SafetyError(RuntimeError):
    """A filesystem identity or safety invariant failed."""


class UnsupportedFilesystem(SafetyError):
    """Required Linux no-replace semantics are unavailable."""


def _renameat2_function():
    if os.name != "posix" or not os.uname().sysname.lower().startswith("linux"):
        raise UnsupportedFilesystem("Linux renameat2(RENAME_NOREPLACE) is required")
    libc = ctypes.CDLL(None, use_errno=True)
    fn = getattr(libc, "renameat2", None)
    if fn is None:
        raise UnsupportedFilesystem("libc does not expose renameat2")
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    return fn


def rename_noreplace(source: Path, destination: Path) -> None:
    """Rename without replacing any destination object, or fail closed."""
    fn = _renameat2_function()
    result = fn(
        AT_FDCWD,
        os.fsencode(source),
        AT_FDCWD,
        os.fsencode(destination),
        RENAME_NOREPLACE,
    )
    if result == 0:
        fsync_dir(destination.parent)
        fsync_dir(source.parent)
        return
    err = ctypes.get_errno()
    if err == errno.EEXIST:
        raise FileExistsError(err, os.strerror(err), str(destination))
    if err in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTSUP}:
        raise UnsupportedFilesystem(f"renameat2(RENAME_NOREPLACE) unsupported: {os.strerror(err)}")
    raise OSError(err, os.strerror(err), f"{source} -> {destination}")


def probe_rename_noreplace(root: Path) -> None:
    token = secrets.token_hex(8)
    source = root / f".hermes-skill-publisher-probe-source-{token}"
    target = root / f".hermes-skill-publisher-probe-target-{token}"
    try:
        source.write_bytes(b"source")
        target.write_bytes(b"target")
        try:
            rename_noreplace(source, target)
        except FileExistsError:
            if source.read_bytes() != b"source" or target.read_bytes() != b"target":
                raise UnsupportedFilesystem("no-replace probe altered an existing object")
        else:
            raise UnsupportedFilesystem("no-replace probe unexpectedly replaced a target")
    finally:
        for path in (source, target):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        fsync_dir(root)


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise SafetyError(f"cannot inspect {path}: {exc}") from exc


def ensure_absent(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    raise SafetyError(f"destination already exists: {path}")


def ensure_real_directory(path: Path) -> None:
    info = _lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SafetyError(f"expected a real directory: {path}")


def _entries(root: Path) -> Iterator[tuple[str, Path, os.stat_result]]:
    ensure_real_directory(root)
    stack = [(Path("."), root)]
    while stack:
        relative_dir, directory = stack.pop()
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise SafetyError(f"cannot scan package: {exc}") from exc
        dirs: list[tuple[Path, Path]] = []
        for entry in children:
            relative = (relative_dir / entry.name) if relative_dir != Path(".") else Path(entry.name)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise SafetyError(f"cannot inspect package entry {relative}: {exc}") from exc
            path = Path(entry.path)
            if stat.S_ISLNK(info.st_mode):
                raise SafetyError(f"symlinks are not allowed in packages: {relative}")
            if stat.S_ISDIR(info.st_mode):
                yield "directory", path, info
                dirs.append((relative, path))
            elif stat.S_ISREG(info.st_mode):
                yield "file", path, info
            else:
                raise SafetyError(f"special files are not allowed in packages: {relative}")
        stack.extend(reversed(dirs))


def package_digest(root: Path, *, max_files: int = MAX_FILES, max_bytes: int = MAX_BYTES) -> str:
    digest = hashlib.sha256()
    files = 0
    total = 0
    for kind, path, info in _entries(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        execute_mask = (info.st_mode & 0o111).to_bytes(1, "big")
        digest.update(kind.encode("ascii") + b"\0" + relative + b"\0" + execute_mask + b"\0")
        if kind == "file":
            files += 1
            total += info.st_size
            if files > max_files or total > max_bytes:
                raise SafetyError(f"package exceeds limits ({max_files} files, {max_bytes} bytes)")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(path, flags)
            except OSError as exc:
                raise SafetyError(f"cannot safely open package file {relative.decode('utf-8')}: {exc}") from exc
            try:
                while chunk := os.read(fd, 1024 * 1024):
                    digest.update(chunk)
            finally:
                os.close(fd)
            digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def safe_copy_tree(source: Path, destination: Path) -> str:
    ensure_absent(destination)
    ensure_real_directory(source)
    destination.mkdir(mode=0o700)
    fsync_dir(destination.parent)
    directory_modes: list[tuple[Path, int]] = []
    try:
        for kind, path, info in _entries(source):
            relative = path.relative_to(source)
            target = destination / relative
            if kind == "directory":
                target.mkdir(mode=0o700)
                directory_modes.append((target, stat.S_IMODE(info.st_mode) & 0o777))
                continue
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            source_fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                target_fd = os.open(target, flags, stat.S_IMODE(info.st_mode) & 0o777)
                try:
                    while chunk := os.read(source_fd, 1024 * 1024):
                        os.write(target_fd, chunk)
                    os.fchmod(target_fd, stat.S_IMODE(info.st_mode) & 0o777)
                    os.fsync(target_fd)
                finally:
                    os.close(target_fd)
            finally:
                os.close(source_fd)
        # Keep destination directories writable while descendants are copied,
        # then apply and fsync exact source modes from deepest to shallowest.
        for directory, mode in sorted(directory_modes, key=lambda item: len(item[0].parts), reverse=True):
            fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fchmod(fd, mode)
                os.fsync(fd)
            finally:
                os.close(fd)
        fsync_dir(destination)
        copied = package_digest(destination)
        original = package_digest(source)
        if copied != original:
            raise SafetyError("staged package digest does not match source")
        return original
    except Exception:
        for directory, _mode in sorted(directory_modes, key=lambda item: len(item[0].parts)):
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
        try:
            safe_remove_tree(destination)
        except Exception:
            pass
        raise


def _open_child_directory(parent_fd: int, name: str, expected: os.stat_result) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(name, flags, dir_fd=parent_fd)
    actual = os.fstat(fd)
    if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
        os.close(fd)
        raise SafetyError(f"directory changed during owned-tree removal: {name}")
    return fd


def _directory_entries(fd: int) -> list[tuple[str, os.stat_result]]:
    with os.scandir(fd) as entries:
        return [(entry.name, entry.stat(follow_symlinks=False)) for entry in sorted(entries, key=lambda item: item.name)]


def _widen_owned_directories(fd: int) -> None:
    mode = stat.S_IMODE(os.fstat(fd).st_mode)
    if not mode & stat.S_IWUSR:
        os.fchmod(fd, mode | stat.S_IWUSR)
        os.fsync(fd)
    for name, info in _directory_entries(fd):
        if stat.S_ISLNK(info.st_mode):
            raise SafetyError(f"symlinks are not allowed in owned trees: {name}")
        if stat.S_ISDIR(info.st_mode):
            child_fd = _open_child_directory(fd, name, info)
            try:
                _widen_owned_directories(child_fd)
            finally:
                os.close(child_fd)
        elif not stat.S_ISREG(info.st_mode):
            raise SafetyError(f"special files are not allowed in owned trees: {name}")


def _remove_owned_contents(fd: int) -> None:
    for name, info in _directory_entries(fd):
        if stat.S_ISLNK(info.st_mode):
            raise SafetyError(f"symlinks are not allowed in owned trees: {name}")
        if stat.S_ISDIR(info.st_mode):
            child_fd = _open_child_directory(fd, name, info)
            try:
                _remove_owned_contents(child_fd)
            finally:
                os.close(child_fd)
            current = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise SafetyError(f"directory changed during owned-tree removal: {name}")
            os.rmdir(name, dir_fd=fd)
        elif stat.S_ISREG(info.st_mode):
            current = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise SafetyError(f"file changed during owned-tree removal: {name}")
            os.unlink(name, dir_fd=fd)
        else:
            raise SafetyError(f"special files are not allowed in owned trees: {name}")
    os.fsync(fd)


def safe_remove_tree(root: Path, *, expected_digest: str | None = None) -> None:
    if expected_digest is not None and package_digest(root) != expected_digest:
        raise SafetyError(f"refusing to remove changed owned package: {root}")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(root.parent, flags)
    root_fd = -1
    try:
        root_info = os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise SafetyError(f"expected a real owned directory: {root}")
        root_fd = _open_child_directory(parent_fd, root.name, root_info)
        if expected_digest is not None:
            _widen_owned_directories(root_fd)
            current = os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (root_info.st_dev, root_info.st_ino):
                raise SafetyError(f"owned root changed during removal: {root}")
            if package_digest(root) != expected_digest:
                raise SafetyError(f"refusing to remove changed owned package: {root}")
        _remove_owned_contents(root_fd)
        os.close(root_fd)
        root_fd = -1
        current = os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (root_info.st_dev, root_info.st_ino):
            raise SafetyError(f"owned root changed during removal: {root}")
        os.rmdir(root.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        os.close(parent_fd)


def relative_link_text(target: Path, link_parent: Path) -> str:
    return os.path.relpath(target, start=link_parent)


def create_relative_symlink(
    target: Path,
    link: Path,
    *,
    on_created: Callable[[str], None] | None = None,
) -> str:
    ensure_absent(link)
    text = relative_link_text(target, link.parent)
    os.symlink(text, link)
    try:
        # Let a transaction journal ownership immediately after the atomic
        # no-replace symlink syscall, before verification/fsync can fail.
        if on_created is not None:
            on_created(text)
        if not link.is_symlink() or os.readlink(link) != text:
            raise SafetyError(f"created adapter failed verification: {link}")
        fsync_dir(link.parent)
        return text
    except BaseException:
        if on_created is None:
            # Unjournaled callers must not strand a link. Journaled callers
            # leave removal to the journal-aware rollback so a cleanup fsync
            # failure can never erase the only durable ownership proof.
            try:
                if link.is_symlink() and os.readlink(link) == text:
                    link.unlink()
                    fsync_dir(link.parent)
            except OSError:
                pass
        raise


def remove_owned_symlink(link: Path, expected_text: str) -> bool:
    try:
        info = link.lstat()
    except FileNotFoundError:
        # Absence can be the process-visible result of an unlink whose parent
        # fsync failed. Confirm it durably before ownership proof is removed.
        fsync_dir(link.parent)
        return False
    if not stat.S_ISLNK(info.st_mode) or os.readlink(link) != expected_text:
        raise SafetyError(f"adapter ownership conflict: {link}")
    link.unlink()
    fsync_dir(link.parent)
    return True


@contextmanager
def acquire_locks(state_lock: Path | None, resource_roots: Sequence[Path], timeout: float = 10.0):
    """Acquire state first, then sorted resource lock files.

    Passing ``None`` is only for a nested resource-lock acquisition while the
    caller already holds the state lock.
    """
    if state_lock is not None:
        state_lock.parent.mkdir(parents=True, exist_ok=True)
    paths = ([] if state_lock is None else [state_lock]) + [
        root / ".hermes-skill-publisher.lock" for root in sorted(set(resource_roots), key=str)
    ]
    handles = []
    deadline = time.monotonic() + timeout
    try:
        for path in paths:
            handle = path.open("a+b")
            handles.append(handle)
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"timed out acquiring lock: {path}")
                    time.sleep(0.05)
        yield
    finally:
        for handle in reversed(handles):
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
