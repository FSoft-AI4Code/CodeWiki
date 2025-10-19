"""
File system utilities for CLI operations.
"""

import os
import shutil
from pathlib import Path
from typing import Optional, List

from codewiki.cli.utils.errors import FileSystemError


def ensure_directory(path: Path, mode: int = 0o700) -> Path:
    """
    Ensure directory exists, create if necessary.
    
    Args:
        path: Directory path
        mode: Directory permissions (default: 0o700 - user only)
        
    Returns:
        Path to the directory
        
    Raises:
        FileSystemError: If directory cannot be created
    """
    try:
        path = Path(path).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True, mode=mode)
        return path
    except PermissionError:
        raise FileSystemError(
            f"Permission denied: Cannot create directory {path}\n"
            f"Try: chmod u+w {path.parent}"
        )
    except OSError as e:
        raise FileSystemError(f"Cannot create directory {path}: {e}")


def check_writable(path: Path) -> bool:
    """
    Check if a path is writable.
    
    Args:
        path: Path to check
        
    Returns:
        True if writable, False otherwise
    """
    path = Path(path).expanduser().resolve()
    
    if path.exists():
        return os.access(path, os.W_OK)
    else:
        # Check parent directory if path doesn't exist
        parent = path.parent
        return parent.exists() and os.access(parent, os.W_OK)


def safe_write(path: Path, content: str, encoding: str = "utf-8"):
    """
    Safely write content to a file using atomic write (temp file + rename).
    
    Args:
        path: File path
        content: Content to write
        encoding: File encoding
        
    Raises:
        FileSystemError: If write fails
    """
    path = Path(path).expanduser().resolve()
    temp_path = path.with_suffix(path.suffix + ".tmp")
    
    try:
        # Write to temp file
        with open(temp_path, "w", encoding=encoding) as f:
            f.write(content)
        
        # Atomic rename
        temp_path.replace(path)
    except Exception as e:
        # Clean up temp file if it exists
        if temp_path.exists():
            temp_path.unlink()
        raise FileSystemError(f"Cannot write to {path}: {e}")


def safe_read(path: Path, encoding: str = "utf-8") -> str:
    """
    Safely read content from a file.
    
    Args:
        path: File path
        encoding: File encoding
        
    Returns:
        File content
        
    Raises:
        FileSystemError: If read fails
    """
    path = Path(path).expanduser().resolve()
    
    try:
        with open(path, "r", encoding=encoding) as f:
            return f.read()
    except FileNotFoundError:
        raise FileSystemError(f"File not found: {path}")
    except PermissionError:
        raise FileSystemError(f"Permission denied: Cannot read {path}")
    except Exception as e:
        raise FileSystemError(f"Cannot read {path}: {e}")


def get_file_size(path: Path) -> int:
    """
    Get file size in bytes.
    
    Args:
        path: File path
        
    Returns:
        File size in bytes
    """
    return Path(path).stat().st_size


def find_files(
    directory: Path,
    extensions: Optional[List[str]] = None,
    recursive: bool = True
) -> List[Path]:
    """
    Find files in directory matching extensions.
    
    Args:
        directory: Directory to search
        extensions: List of file extensions (e.g., ['.py', '.java'])
        recursive: Search recursively
        
    Returns:
        List of matching file paths
    """
    directory = Path(directory).expanduser().resolve()
    
    if not directory.exists():
        return []
    
    pattern = "**/*" if recursive else "*"
    files = []
    
    for path in directory.glob(pattern):
        if not path.is_file():
            continue
        
        if extensions is None or path.suffix in extensions:
            files.append(path)
    
    return files


def cleanup_directory(path: Path, keep_hidden: bool = True):
    """
    Clean up a directory by removing its contents.
    
    Args:
        path: Directory to clean
        keep_hidden: Keep hidden files/directories (starting with .)
        
    Raises:
        FileSystemError: If cleanup fails
    """
    path = Path(path).expanduser().resolve()
    
    if not path.exists():
        return
    
    try:
        for item in path.iterdir():
            if keep_hidden and item.name.startswith('.'):
                continue
            
            if item.is_file():
                item.unlink()
            elif item.is_dir():
                shutil.rmtree(item)
    except Exception as e:
        raise FileSystemError(f"Cannot clean directory {path}: {e}")

