"""
Code analysis patterns for different programming languages.

This module contains patterns used to identify entry points, high-connectivity files,
and function definitions across multiple programming languages.
"""

from typing import List, Dict

DEFAULT_IGNORE_PATTERNS = {
    ".github",
    ".vscode",
    ".git",
    ".gitignore",
    ".gitmodules",
    ".gitignore",
    "examples",
    # Python
    "*.pyc",
    "*.pyo",
    "*.pyd",
    "__pycache__",
    ".pytest_cache",
    ".coverage",
    ".tox",
    ".nox",
    ".mypy_cache",
    ".ruff_cache",
    ".hypothesis",
    "poetry.lock",
    "Pipfile.lock",
    # JavaScript/FileSystemNode
    "package-lock.json",
    "yarn.lock",
    ".npm",
    ".yarn",
    ".pnpm-store",
    "bun.lock",
    "bun.lockb",
    # Java
    "*.class",
    "*.jar",
    "*.war",
    "*.ear",
    "*.nar",
    ".gradle/",
    ".settings/",
    ".classpath",
    "gradle-app.setting",
    "*.gradle",
    # IDEs and editors / Java
    ".project",
    # C/C++
    "*.o",
    "*.obj",
    "*.dll",
    "*.dylib",
    "*.exe",
    "*.lib",
    "*.out",
    "*.a",
    "*.pdb",
    # .NET/C#
    "*.suo",
    "*.user",
    "*.userosscache",
    "*.sln.docstates",
    "*.nupkg",
    # Go / .NET / C#
    "bin/",
    # Version control
    ".git",
    ".svn",
    ".hg",
    ".gitignore",
    ".gitattributes",
    ".gitmodules",
    # Images and media
    "*.svg",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.ico",
    "*.pdf",
    "*.mov",
    "*.mp4",
    "*.mp3",
    "*.wav",
    # Virtual environments
    "venv",
    ".venv",
    "env",
    ".env",
    "virtualenv",
    # IDEs and editors
    ".idea",
    ".vscode",
    ".vs",
    "*.swo",
    "*.swn",
    "*.sublime-*",
    # Temporary and cache files
    "*.log",
    "*.bak",
    "*.swp",
    "*.tmp",
    "*.temp",
    ".cache",
    ".sass-cache",
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    # Build directories and artifacts
    "*.egg-info",
    "*.egg",
    "*.whl",
    "*.so",
    # Documentation
    ".docusaurus",
    # Other common patterns
    ## Minified files
    "*.min.js",
    "*.min.css",
    ## Source maps
    "*.map",
    ## Terraform
    ".terraform",
    "*.tfstate*",
    ## Dependencies in various languages
    # Gitingest
    "digest.txt",
    "*.ini",
    "tests",
    "test",
    "Tests",
    "Test",
    "examples",
    "Examples",
}

DEFAULT_INCLUDE_PATTERNS = [
    "*.py",
    "*.js",
    "*.ts",
    "*.jsx",
    "*.tsx",
    "*.java",
    "*.cpp",
    "*.c",
    "*.h",
    "*.cs",
    "*.go",
    "*.rs",
    "*.php",
    "*.rb",
    "*.swift",
    "*.kt",
    "*.scala",
    "*.clj",
    "*.hs",
    "*.ml",
    "*.html",
    "*.css",
    "*.scss",
    "*.sass",
    "*.json",
    "*.yaml",
    "*.yml",
    "*.xml",
    "*.md",
    "*.txt",
    "*.toml",
    "*.cfg",
    "*.ini",
]

CODE_EXTENSIONS = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".jsx": "javascript",
    ".tsx": "typescript",
    ".java": "java",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".c++": "cpp",
    ".c": "c",
    ".h": "c",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".h++": "cpp",
    ".rs": "rust",
    ".go": "go",
    ".php": "php",
    ".rb": "ruby",
    ".swift": "swift",
    ".kt": "kotlin",
    ".scala": "scala",
    ".cs": "csharp",
}

# Entry point file patterns for all supported languages
ENTRY_POINT_PATTERNS = {
    # Python
    "main.py",
    "app.py",
    "server.py",
    "__main__.py",
    "run.py",
    "start.py",
    "manage.py",
    "wsgi.py",
    "asgi.py",
    "gunicorn.py",  # Django/Flask patterns
    # JavaScript/TypeScript
    "index.js",
    "app.js",
    "server.js",
    "main.js",
    "index.ts",
    "app.ts",
    "server.ts",
    "main.ts",
    "start.js",
    "start.ts",
    "bootstrap.js",
    "bootstrap.ts",
    "entry.js",
    "entry.ts",
    # Go
    "main.go",
    "cmd.go",
    "server.go",
    "app.go",
    "root.go",
    "start.go",
    # Rust
    "main.rs",
    "lib.rs",
    "server.rs",
    "app.rs",
    "start.rs",
    "bin.rs",
    # C/C++
    "main.c",
    "main.cpp",
    "main.cc",
    "main.cxx",
    "app.c",
    "app.cpp",
    "start.c",
    "start.cpp",
    "entry.c",
    "entry.cpp",
}

# Additional entry point path patterns (for when filename patterns fail)
ENTRY_POINT_PATH_PATTERNS = [
    "cmd/main",
    "cmd/root",
    "cmd/server",  # Go command patterns
    "src/main",
    "src/app",
    "src/server",  # Common src patterns
    "bin/main",
    "bin/app",
    "bin/server",  # Binary patterns
    "app/main",
    "app/server",
    "app/start",  # App directory patterns
    "scripts/start",
    "scripts/run",  # Script patterns
]

# Flexible entry point name patterns (partial matches)
ENTRY_POINT_NAME_PATTERNS = [
    "main",
    "app",
    "server",
    "start",
    "run",
    "entry",
    "bootstrap",
    "init",
    "cmd",
    "cli",
    "daemon",
    "service",
    "worker",
    "launcher",
]

# High connectivity file patterns (files likely to have many function calls)
HIGH_CONNECTIVITY_PATTERNS = {
    # General patterns
    "router",
    "controller",
    "service",
    "handler",
    "middleware",
    "api",
    "core",
    "engine",
    "manager",
    "processor",
    "client",
    # Language-specific patterns
    "mod",
    "module",  # Rust modules
    "pkg",
    "package",  # Go packages
    "lib",
    "util",
    "utils",
    "helper",
    "helpers",
    # Framework patterns
    "express",
    "fastapi",
    "gin",
    "actix",
    "rocket",  # Web frameworks
    "db",
    "database",
    "model",
    "entity",
    "repo",
    "repository",
    # Additional patterns
    "config",
    "settings",
    "constants",
    "types",
    "interfaces",
    # Generic library patterns (added for broader coverage)
    "console",
    "text",
    "style",
    "render",
    "display",
    "format",
    "parse",
    "parser",
    "convert",
    "transform",
    "process",
    "table",
    "tree",
    "list",
    "grid",
    "layout",
    "widget",
    "color",
    "theme",
    "visual",
    "graphic",
    "draw",
    "paint",
    "file",
    "io",
    "stream",
    "buffer",
    "cache",
    "store",
    "base",
    "common",
    "shared",
    "global",
    "main",
    "index",
}

# Source directory patterns across all languages
SOURCE_DIRECTORY_PATTERNS = [
    "src/",
    "lib/",
    "core/",
    "pkg/",  # General
    "cmd/",
    "internal/",  # Go specific
    "crates/",
    "modules/",  # Rust specific
    "include/",
    "source/",  # C/C++ specific
    "components/",
    "services/",
    "utils/",  # Framework patterns
]

# Function definition patterns for quick file scanning
FUNCTION_DEFINITION_PATTERNS = {
    "python": ["def {name}"],
    "javascript": ["function {name}", "const {name}", "export {name}"],
    "typescript": ["function {name}", "const {name}", "export {name}"],
    "go": ["func {name}"],
    "rust": ["fn {name}", "pub fn {name}"],
    "c": ["void {name}", "int {name}", "{name}("],
    "cpp": ["void {name}", "int {name}", "{name}("],
    "general": ["{name}("],  # Fallback pattern
}

# Critical function name patterns
CRITICAL_FUNCTION_NAMES = {"main", "index", "app", "server", "start", "init", "run", "new"}

# Export/public function patterns for critical function detection
EXPORT_PATTERNS = [
    # JavaScript/TypeScript exports
    "export default",
    "module.exports =",
    "exports.",
    # Rust public functions
    "pub fn main",
    "pub fn new",
    "pub fn",
    # Go exported functions (capitalized)
    "func main",
    "func new",
    # C/C++ main functions
    "int main",
    "void main",
    "public static void main",
    # Python special methods
    'if __name__ == "__main__"',
]

# Fallback patterns when standard patterns don't work
FALLBACK_PATTERNS = {
    "any_main_file": ["main"],  # Any file with "main" in name
    "any_app_file": ["app"],  # Any file with "app" in name
    "any_server_file": ["server", "srv"],  # Any server-related file
    "any_index_file": ["index", "idx"],  # Any index file
    "largest_files": True,  # Fall back to largest files by line count
}


def get_function_patterns_for_language(language: str) -> list:
    """
    Get function definition patterns for a specific language.

    Args:
        language: Programming language name

    Returns:
        List of function definition patterns for the language
    """
    return FUNCTION_DEFINITION_PATTERNS.get(
        language.lower(), FUNCTION_DEFINITION_PATTERNS["general"]
    )


def is_entry_point_file(filename: str) -> bool:
    """
    Check if a filename matches entry point patterns.

    Args:
        filename: Name of the file to check

    Returns:
        True if the file is likely an entry point
    """
    filename_lower = filename.lower()

    # Exact match
    if filename_lower in ENTRY_POINT_PATTERNS:
        return True

    # Partial name matching for flexibility
    for pattern in ENTRY_POINT_NAME_PATTERNS:
        if pattern in filename_lower and any(
            ext in filename_lower for ext in [".py", ".js", ".ts", ".go", ".rs", ".c", ".cpp"]
        ):
            return True

    return False


def is_entry_point_path(filepath: str) -> bool:
    """
    Check if a file path matches entry point path patterns.

    Args:
        filepath: Full path of the file to check

    Returns:
        True if the path suggests an entry point
    """
    filepath_lower = filepath.lower()

    for pattern in ENTRY_POINT_PATH_PATTERNS:
        if pattern in filepath_lower:
            return True

    return False


def has_high_connectivity_potential(filename: str, filepath: str) -> bool:
    """
    Check if a file has high connectivity potential based on name and path.

    Args:
        filename: Name of the file
        filepath: Full path of the file

    Returns:
        True if the file likely has high connectivity
    """
    filename_lower = filename.lower()
    filepath_lower = filepath.lower()

    # Check filename patterns
    if any(pattern in filename_lower for pattern in HIGH_CONNECTIVITY_PATTERNS):
        return True

    # Check filepath patterns
    if any(pattern in filepath_lower for pattern in HIGH_CONNECTIVITY_PATTERNS):
        return True

    # Check source directory patterns
    if any(pattern in filepath_lower for pattern in SOURCE_DIRECTORY_PATTERNS):
        return True

    return False


def is_critical_function(func_name: str, code_snippet: str = None) -> bool:
    """
    Check if a function is critical based on name and code patterns.

    Args:
        func_name: Name of the function
        code_snippet: Optional code snippet to analyze

    Returns:
        True if the function is considered critical
    """
    # Check critical function names
    if func_name.lower() in CRITICAL_FUNCTION_NAMES:
        return True

    # Check export patterns in code snippet
    if code_snippet:
        snippet_lower = code_snippet.lower()
        if any(pattern in snippet_lower for pattern in EXPORT_PATTERNS):
            return True

    return False


def find_fallback_entry_points(code_files: List[Dict], max_files: int = 5) -> List[Dict]:
    """
    Find fallback entry points when standard patterns don't match.

    Args:
        code_files: List of all code files
        max_files: Maximum number of fallback files to return

    Returns:
        List of files that could serve as entry points
    """
    fallback_files = []

    # Try fallback name patterns
    for file_info in code_files:
        filename = file_info["name"].lower()
        filepath = file_info["path"].lower()

        # Check for any main-like files
        if any(pattern in filename for pattern in ["main", "app", "server", "start", "index"]):
            fallback_files.append(file_info)

        # Check for entry point paths
        elif is_entry_point_path(filepath):
            fallback_files.append(file_info)

    # If still nothing, try files in root or common directories
    if not fallback_files:
        for file_info in code_files:
            filepath = file_info["path"]
            # Files in root directory or immediate subdirectories
            if filepath.count("/") <= 1:
                fallback_files.append(file_info)

    # Sort by likelihood (prefer shorter paths, common names)
    def fallback_priority(file_info):
        path = file_info["path"].lower()
        name = file_info["name"].lower()

        score = 0
        # Prefer shorter paths (closer to root)
        score -= path.count("/")
        # Prefer common entry point names
        if any(pattern in name for pattern in ["main", "app", "index"]):
            score -= 10
        # Prefer certain extensions
        if any(ext in name for ext in [".py", ".js", ".go", ".rs"]):
            score -= 5

        return score

    fallback_files.sort(key=fallback_priority)
    return fallback_files[:max_files]


def find_fallback_connectivity_files(code_files: List[Dict], max_files: int = 10) -> List[Dict]:
    """
    Find fallback high-connectivity files when standard patterns don't match.

    Args:
        code_files: List of all code files
        max_files: Maximum number of fallback files to return

    Returns:
        List of files that likely have good connectivity
    """
    fallback_files = []

    # Include all files from common source directories
    for file_info in code_files:
        filepath = file_info["path"].lower()

        # Any file in src, lib, or similar directories
        if any(pattern in filepath for pattern in ["src/", "lib/", "app/", "pkg/", "core/"]):
            fallback_files.append(file_info)

    # If still not enough, include files with certain extensions
    if len(fallback_files) < max_files:
        for file_info in code_files:
            if file_info not in fallback_files:
                name = file_info["name"].lower()
                # Include common source file extensions
                if any(ext in name for ext in [".py", ".js", ".ts", ".go", ".rs", ".c", ".cpp"]):
                    # Skip test files
                    if not any(test_pattern in name for test_pattern in ["test", "spec", "_test"]):
                        fallback_files.append(file_info)

    return fallback_files[:max_files]
