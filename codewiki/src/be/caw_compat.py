"""Compatibility imports for the optional coding-agent-wrapper dependency."""

from __future__ import annotations

import importlib
import sys
import types
from typing import Any


def _windows_fcntl() -> types.ModuleType:
    import msvcrt

    module = types.ModuleType("fcntl")
    module.LOCK_SH = 1
    module.LOCK_EX = 2
    module.LOCK_NB = 4
    module.LOCK_UN = 8

    def flock(file_obj: Any, operation: int) -> None:
        position = file_obj.tell()
        try:
            file_obj.seek(0)
            if operation & module.LOCK_UN:
                msvcrt.locking(file_obj.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                mode = msvcrt.LK_NBLCK if operation & module.LOCK_NB else msvcrt.LK_LOCK
                msvcrt.locking(file_obj.fileno(), mode, 1)
        finally:
            file_obj.seek(position)

    module.flock = flock
    return module


def _windows_pty() -> types.ModuleType:
    module = types.ModuleType("pty")

    def unsupported(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("Pseudo-terminals are not available on Windows")

    module.openpty = unsupported
    module.spawn = unsupported
    return module


def _install_windows_posix_compatibility() -> None:
    sys.modules["fcntl"] = _windows_fcntl()
    # caw imports its optional interactive TUI provider eagerly. The regular
    # Claude Code provider used by CodeWiki does not need a pseudo-terminal.
    sys.modules["pty"] = _windows_pty()
    sys.modules["termios"] = types.ModuleType("termios")


def _import_with_windows_compatibility() -> Any:
    sentinel = object()
    previous = {name: sys.modules.get(name, sentinel) for name in ("fcntl", "pty", "termios")}
    _install_windows_posix_compatibility()
    try:
        return importlib.import_module("caw")
    finally:
        for name, module in previous.items():
            if module is sentinel:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def load_caw() -> tuple[Any, Any]:
    """Load caw, supplying only its missing Windows file-lock API when needed."""
    if sys.platform == "win32":
        module = _import_with_windows_compatibility()
        return module.Agent, module.ToolGroup

    module = importlib.import_module("caw")
    return module.Agent, module.ToolGroup


CawAgent, ToolGroup = load_caw()
