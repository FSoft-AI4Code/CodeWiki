"""Explicit source-file exclusions, without importing optional parser grammars."""

from pathlib import Path

PHP_TEMPLATE_PATTERNS = {".blade.php", ".phtml", ".twig.php"}
PHP_TEMPLATE_DIRECTORIES = {"views", "templates", "resources/views"}
ZIG_SKIPPED_DIRS = {"zig-cache", ".zig-cache", "zig-out"}


def is_skipped_go_file(relative_path: str) -> bool:
    """`_test.go` files and vendored dependencies yield no components."""
    posix = Path(relative_path).as_posix()
    return posix.endswith("_test.go") or "vendor" in posix.split("/")[:-1]


def is_skipped_zig_file(relative_path: str) -> bool:
    return any(part in ZIG_SKIPPED_DIRS for part in Path(relative_path).parts[:-1])


def is_php_template_file(file_path: str) -> bool:
    file_str = str(file_path)
    return any(file_str.endswith(pattern) for pattern in PHP_TEMPLATE_PATTERNS) or any(
        f"/{directory}/" in file_str or f"\\{directory}\\" in file_str
        for directory in PHP_TEMPLATE_DIRECTORIES
    )


def is_skipped_source_file(file_info: dict, base_dir: str) -> bool:
    """Apply the same exclusions for parsing and symbol-free file retention."""
    language = file_info.get("language")
    if language == "go":
        return is_skipped_go_file(file_info["path"])
    if language == "zig":
        return is_skipped_zig_file(file_info["path"])
    if language == "php":
        return is_php_template_file(str(Path(base_dir) / file_info["path"]))
    return False
