import os
from pathlib import Path


def _inside(base: Path, target: Path) -> bool:
    base_r = base.resolve()
    try:
        target_r = target.resolve()
        return target_r.is_relative_to(base_r)  # py>=3.9
    except AttributeError:
        return str(target.resolve()).startswith(str(base_r))


def assert_safe_path(base_dir: Path, target: Path):
    # Block symlinks (file or dir)
    if target.is_symlink():
        raise PermissionError(f"Symlink blocked: {target}")
    # Block paths that escape repo
    if not _inside(base_dir, target):
        raise PermissionError(f"Path escapes repo: {target} -> {target.resolve()}")


def safe_open_text(base_dir: Path, target: Path, encoding="utf-8"):
    assert_safe_path(base_dir, target)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(target), flags)
    try:
        with os.fdopen(fd, "r", encoding=encoding, errors="replace") as f:
            return f.read()
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def safe_read_head(
    base_dir: Path, target: Path, max_bytes: int, encoding="utf-8"
) -> tuple[str, int, bool]:
    """Read at most ``max_bytes`` of ``target`` with the same symlink/escape checks
    as :func:`safe_open_text`.

    Returns ``(text, total_size, is_binary)``. ``is_binary`` is True when the
    head contains a NUL byte; callers should then drop the file.
    """
    assert_safe_path(base_dir, target)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(target), flags)
    try:
        total_size = os.fstat(fd).st_size
        data = os.read(fd, max(0, int(max_bytes)))
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    if b"\0" in data:
        return "", total_size, True
    return data.decode(encoding, errors="replace"), total_size, False
