"""
Validation utilities for CLI inputs and configuration.
"""

import re
from pathlib import Path
from typing import Optional, List, Tuple
from urllib.parse import urlparse

from codewiki.cli.utils.errors import ConfigurationError, RepositoryError


def validate_url(url: str, require_https: bool = True, allow_localhost: bool = True) -> str:
    """
    Validate URL format.
    
    Args:
        url: URL to validate
        require_https: Require HTTPS scheme (except localhost)
        allow_localhost: Allow localhost URLs
        
    Returns:
        Validated URL
        
    Raises:
        ConfigurationError: If URL is invalid
    """
    try:
        parsed = urlparse(url)
        
        # Check scheme
        if not parsed.scheme:
            raise ConfigurationError(f"Invalid URL (missing scheme): {url}")
        
        # Check HTTPS requirement
        if require_https and parsed.scheme != 'https':
            # Allow HTTP for localhost
            if allow_localhost and parsed.hostname in ['localhost', '127.0.0.1', '::1']:
                pass
            else:
                raise ConfigurationError(
                    f"URL must use HTTPS: {url}\n"
                    f"HTTP is only allowed for localhost"
                )
        
        # Check hostname
        if not parsed.hostname:
            raise ConfigurationError(f"Invalid URL (missing hostname): {url}")
        
        return url
    except ValueError as e:
        raise ConfigurationError(f"Invalid URL format: {url}\nError: {e}")


def validate_api_key(api_key: str, min_length: int = 10) -> str:
    """
    Validate API key format.
    
    Args:
        api_key: API key to validate
        min_length: Minimum key length
        
    Returns:
        Validated API key
        
    Raises:
        ConfigurationError: If API key is invalid
    """
    if not api_key or not api_key.strip():
        raise ConfigurationError("API key cannot be empty")
    
    api_key = api_key.strip()
    
    if len(api_key) < min_length:
        raise ConfigurationError(
            f"API key too short (minimum {min_length} characters)"
        )
    
    return api_key


def validate_model_name(model: str) -> str:
    """
    Validate model name format.
    
    Args:
        model: Model name to validate
        
    Returns:
        Validated model name
        
    Raises:
        ConfigurationError: If model name is invalid
    """
    if not model or not model.strip():
        raise ConfigurationError("Model name cannot be empty")
    
    return model.strip()


def validate_output_directory(path: str) -> Path:
    """
    Validate output directory path.
    
    Args:
        path: Directory path to validate
        
    Returns:
        Validated Path object
        
    Raises:
        ConfigurationError: If path is invalid
    """
    if not path or not path.strip():
        raise ConfigurationError("Output directory cannot be empty")
    
    try:
        resolved_path = Path(path).expanduser().resolve()
        
        # Check if path is writable (or parent is writable if path doesn't exist)
        if resolved_path.exists():
            if not resolved_path.is_dir():
                raise ConfigurationError(
                    f"Output path exists but is not a directory: {path}"
                )
        
        return resolved_path
    except Exception as e:
        raise ConfigurationError(f"Invalid output directory path: {path}\nError: {e}")


def validate_repository_path(path: Path) -> Path:
    """
    Validate repository path exists and contains code files.
    
    Args:
        path: Repository path to validate
        
    Returns:
        Validated Path object
        
    Raises:
        RepositoryError: If repository is invalid
    """
    path = Path(path).expanduser().resolve()
    
    if not path.exists():
        raise RepositoryError(f"Repository path does not exist: {path}")
    
    if not path.is_dir():
        raise RepositoryError(f"Repository path is not a directory: {path}")
    
    return path


def detect_supported_languages(directory: Path) -> List[Tuple[str, int]]:
    """
    Detect supported programming languages in a directory.
    
    Args:
        directory: Directory to scan
        
    Returns:
        List of (language, file_count) tuples
    """
    language_extensions = {
        'Python': ['.py'],
        'Java': ['.java'],
        'JavaScript': ['.js', '.jsx'],
        'TypeScript': ['.ts', '.tsx'],
        'C': ['.c', '.h'],
        'C++': ['.cpp', '.hpp', '.cc', '.hh', '.cxx', '.hxx'],
        'C#': ['.cs'],
    }
    
    # Directories to exclude from counting
    excluded_dirs = {
        'node_modules', '__pycache__', '.git', 'build', 'dist', 
        '.venv', 'venv', 'env', '.env', 'target', 'bin', 'obj',
        '.pytest_cache', '.mypy_cache', '.tox', 'coverage',
        'htmlcov', '.eggs', '*.egg-info', 'vendor', 'bower_components',
        '.idea', '.vscode', '.gradle', '.mvn'
    }
    
    def should_exclude_file(file_path: Path) -> bool:
        """Check if file is in an excluded directory."""
        parts = file_path.parts
        return any(excluded_dir in parts for excluded_dir in excluded_dirs)
    
    language_counts = {}
    
    for language, extensions in language_extensions.items():
        count = 0
        for ext in extensions:
            # Filter out files in excluded directories
            count += sum(
                1 for f in directory.rglob(f"*{ext}")
                if f.is_file() and not should_exclude_file(f)
            )
        
        if count > 0:
            language_counts[language] = count
    
    # Sort by count descending
    return sorted(language_counts.items(), key=lambda x: x[1], reverse=True)


def is_top_tier_model(model: str) -> bool:
    """
    Check if a model is considered top-tier for clustering.
    
    Args:
        model: Model name
        
    Returns:
        True if top-tier, False otherwise
    """
    top_tier_models = [
        'claude-opus',
        'claude-sonnet',
        'gpt-4',
        'gpt-5',
        'gemini-2.5',
    ]
    
    model_lower = model.lower()
    return any(tier in model_lower for tier in top_tier_models)


def mask_api_key(api_key: str, visible_chars: int = 4) -> str:
    """
    Mask API key for display, showing only first and last few characters.
    
    Args:
        api_key: API key to mask
        visible_chars: Number of visible characters at start and end
        
    Returns:
        Masked API key (e.g., "sk-1234...5678")
    """
    if not api_key:
        return "Not set"
    
    if len(api_key) <= visible_chars * 2:
        # Key too short, mask everything except edges
        return f"{api_key[:2]}...{api_key[-2:]}"
    
    return f"{api_key[:visible_chars]}...{api_key[-visible_chars:]}"

