"""
Repository validation utilities for documentation generation.
"""

from pathlib import Path
from typing import Tuple, List
import os

from codewiki.cli.utils.errors import RepositoryError
from codewiki.cli.utils.validation import validate_repository_path, detect_supported_languages


# Supported file extensions by language
SUPPORTED_EXTENSIONS = {
    '.py',      # Python
    '.java',    # Java
    '.js',      # JavaScript
    '.jsx',     # JavaScript (React)
    '.ts',      # TypeScript
    '.tsx',     # TypeScript (React)
    '.c',       # C
    '.h',       # C headers
    '.cpp',     # C++
    '.hpp',     # C++ headers
    '.cc',      # C++
    '.hh',      # C++ headers
    '.cxx',     # C++
    '.hxx',     # C++ headers
    '.cs',      # C#
}


def validate_repository(repo_path: Path) -> Tuple[Path, List[Tuple[str, int]]]:
    """
    Validate repository for documentation generation.
    
    Checks:
    - Path exists and is a directory
    - Contains supported code files
    - Has sufficient files for meaningful documentation
    
    Args:
        repo_path: Path to repository
        
    Returns:
        Tuple of (validated_path, language_counts)
        
    Raises:
        RepositoryError: If validation fails
    """
    # Validate path exists
    repo_path = validate_repository_path(repo_path)
    
    # Detect languages
    languages = detect_supported_languages(repo_path)
    
    if not languages:
        raise RepositoryError(
            f"No supported code files found in {repo_path}\n\n"
            "CodeWiki supports: Python, Java, JavaScript, TypeScript, C, C++, C#\n\n"
            "Please navigate to a code repository or specify a custom directory:\n"
            "  cd /path/to/your/project\n"
            "  codewiki generate"
        )
    
    return repo_path, languages


def check_writable_output(output_dir: Path) -> Path:
    """
    Check if output directory is writable.
    
    Args:
        output_dir: Output directory path
        
    Returns:
        Validated output directory path
        
    Raises:
        RepositoryError: If output directory is not writable
    """
    output_dir = Path(output_dir).expanduser().resolve()
    
    # Check if output directory exists
    if output_dir.exists():
        if not output_dir.is_dir():
            raise RepositoryError(
                f"Output path exists but is not a directory: {output_dir}"
            )
        
        # Check if writable
        if not os.access(output_dir, os.W_OK):
            raise RepositoryError(
                f"Output directory is not writable: {output_dir}\n\n"
                f"Try: chmod u+w {output_dir}"
            )
    else:
        # Check if parent is writable
        parent = output_dir.parent
        if not parent.exists():
            raise RepositoryError(
                f"Parent directory does not exist: {parent}"
            )
        
        if not os.access(parent, os.W_OK):
            raise RepositoryError(
                f"Cannot create output directory (parent not writable): {parent}\n\n"
                f"Try: chmod u+w {parent}"
            )
    
    return output_dir


def is_git_repository(repo_path: Path) -> bool:
    """
    Check if path is a git repository.
    
    Args:
        repo_path: Path to check
        
    Returns:
        True if git repository, False otherwise
    """
    git_dir = repo_path / ".git"
    return git_dir.exists() and git_dir.is_dir()


def get_git_commit_hash(repo_path: Path) -> str:
    """
    Get current git commit hash.
    
    Args:
        repo_path: Repository path
        
    Returns:
        Commit hash or empty string if not a git repo
    """
    if not is_git_repository(repo_path):
        return ""
    
    try:
        import git
        repo = git.Repo(repo_path)
        return repo.head.commit.hexsha
    except Exception:
        return ""


def get_git_branch(repo_path: Path) -> str:
    """
    Get current git branch name.
    
    Args:
        repo_path: Repository path
        
    Returns:
        Branch name or empty string if not a git repo
    """
    if not is_git_repository(repo_path):
        return ""
    
    try:
        import git
        repo = git.Repo(repo_path)
        return repo.active_branch.name
    except Exception:
        return ""


def count_code_files(repo_path: Path) -> int:
    """
    Count supported code files in repository.
    
    Args:
        repo_path: Repository path
        
    Returns:
        Number of code files
    """
    count = 0
    for ext in SUPPORTED_EXTENSIONS:
        count += len(list(repo_path.rglob(f"*{ext}")))
    return count

