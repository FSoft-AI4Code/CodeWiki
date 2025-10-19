"""
Analysis Service

Centralized service for repository analysis with support for multiple languages.
Handles the orchestration of repository cloning, structure analysis, and multi-language
AST parsing for call graph generation.
"""

import logging
from typing import Dict, List, Optional, Any
from pathlib import Path
from codewiki.src.be.dependency_analyzer.utils.security import safe_open_text, assert_safe_path
from codewiki.src.be.dependency_analyzer.analysis.repo_analyzer import RepoAnalyzer
from codewiki.src.be.dependency_analyzer.analysis.call_graph_analyzer import CallGraphAnalyzer
from codewiki.src.be.dependency_analyzer.analysis.cloning import clone_repository, cleanup_repository, parse_github_url
from codewiki.src.be.dependency_analyzer.models.analysis import AnalysisResult
from codewiki.src.be.dependency_analyzer.models.core import Repository


logger = logging.getLogger(__name__)


class AnalysisService:
    """
    Centralized analysis service supporting multiple programming languages.

    This service orchestrates the complete analysis workflow:
    1. Repository cloning and validation
    2. File structure analysis with filtering
    3. Multi-language AST parsing and call graph generation
    4. Result consolidation and cleanup

    """

    def __init__(self):
        """Initialize the analysis service with language-specific analyzers."""
        self.call_graph_analyzer = CallGraphAnalyzer()
        self._temp_directories = []

    def analyze_local_repository(
        self,
        repo_path: str,
        max_files: int = 100,
        languages: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Analyze a local repository folder.
        
        Args:
            repo_path: Path to local repository folder
            max_files: Maximum number of files to analyze
            languages: List of languages to include (e.g., ['python', 'javascript'])
            
        Returns:
            Dict with analysis results including nodes and relationships
        """
        try:
            logger.debug(f"Analyzing local repository at {repo_path}")
            
            # Get repo analyzer to find files
            repo_analyzer = RepoAnalyzer()
            structure_result = repo_analyzer.analyze_repository_structure(repo_path)
            
            # Extract code files
            code_files = self.call_graph_analyzer.extract_code_files(structure_result["file_tree"])
            
            # Filter by languages if specified
            if languages:
                code_files = [f for f in code_files if f.get("language") in languages]
            
            # Limit number of files
            if len(code_files) > max_files:
                code_files = code_files[:max_files]
                logger.debug(f"Limited analysis to {max_files} files")
            
            logger.debug(f"Analyzing {len(code_files)} files")
            
            # Analyze files
            result = self.call_graph_analyzer.analyze_code_files(code_files, repo_path)
            
            return {
                "nodes": result.get("functions", {}),
                "relationships": result.get("relationships", []),
                "summary": {
                    "total_files": len(code_files),
                    "total_nodes": len(result.get("functions", {})),
                    "total_relationships": len(result.get("relationships", []))
                }
            }
            
        except Exception as e:
            logger.error(f"Local repository analysis failed: {str(e)}", exc_info=True)
            raise RuntimeError(f"Analysis failed: {str(e)}")

    def analyze_repository_full(
        self,
        github_url: str,
        include_patterns: Optional[List[str]] = None,
        exclude_patterns: Optional[List[str]] = None,
    ) -> AnalysisResult:
        """
        Perform complete repository analysis including call graph generation.

        Args:
            github_url: GitHub repository URL to analyze
            include_patterns: File patterns to include (e.g., ['*.py', '*.js'])
            exclude_patterns: Additional patterns to exclude

        Returns:
            AnalysisResult: Complete analysis with functions, relationships, and visualization

        Raises:
            ValueError: If GitHub URL is invalid
            RuntimeError: If analysis fails
        """
        temp_dir = None
        try:
            logger.debug(f"Starting full analysis of {github_url}")

            temp_dir = self._clone_repository(github_url)
            repo_info = self._parse_repository_info(github_url)

            logger.debug("Analyzing repository file structure...")
            structure_result = self._analyze_structure(temp_dir, include_patterns, exclude_patterns)
            logger.debug(f"Found {structure_result['summary']['total_files']} files to analyze.")

            logger.debug("Starting call graph analysis...")
            call_graph_result = self._analyze_call_graph(structure_result["file_tree"], temp_dir)
            logger.debug(
                f"Call graph analysis complete. Found {call_graph_result['call_graph']['total_functions']} functions."
            )

            readme_content = self._read_readme_file(temp_dir)

            analysis_result = AnalysisResult(
                repository=Repository(
                    url=repo_info["url"],
                    name=repo_info["name"],
                    clone_path=temp_dir,
                    analysis_id=f"{repo_info['owner']}-{repo_info['name']}",
                ),
                functions=call_graph_result["functions"],
                relationships=call_graph_result["relationships"],
                file_tree=structure_result["file_tree"],
                summary={
                    **structure_result["summary"],
                    **call_graph_result["call_graph"],
                    "analysis_type": "full",
                    "languages_analyzed": call_graph_result["call_graph"]["languages_found"],
                },
                visualization=call_graph_result["visualization"],
                readme_content=readme_content,
            )

            logger.debug(f"Cleaning up temporary repository directory: {temp_dir}")
            self._cleanup_repository(temp_dir)

            logger.debug(
                f"Analysis completed: {analysis_result.summary['total_functions']} functions found"
            )
            return analysis_result

        except Exception as e:
            logger.error(f"Analysis failed: {str(e)}", exc_info=True)
            if "temp_dir" in locals() and Path(temp_dir).exists():
                self._cleanup_repository(temp_dir)
            raise RuntimeError(f"Repository analysis failed: {str(e)}")

    def analyze_repository_structure_only(
        self,
        github_url: str,
        include_patterns: Optional[List[str]] = None,
        exclude_patterns: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Perform lightweight structure-only analysis without call graph generation.

        Args:
            github_url: GitHub repository URL to analyze
            include_patterns: File patterns to include
            exclude_patterns: Additional patterns to exclude

        Returns:
            Dict: Repository structure with file tree and summary statistics
        """
        temp_dir = None
        try:
            logger.debug(f"Starting structure analysis of {github_url}")

            temp_dir = self._clone_repository(github_url)
            repo_info = self._parse_repository_info(github_url)

            structure_result = self._analyze_structure(temp_dir, include_patterns, exclude_patterns)

            result = {
                "repository": repo_info,
                "file_tree": structure_result["file_tree"],
                "file_summary": {
                    **structure_result["summary"],
                    "analysis_type": "structure_only",
                },
            }

            self._cleanup_repository(temp_dir)

            logger.debug(
                f"Structure analysis completed: {result['file_summary']['total_files']} files found"
            )
            return result

        except Exception as e:
            if temp_dir:
                self._cleanup_repository(temp_dir)
            logger.error(f"Structure analysis failed for {github_url}: {str(e)}")
            raise RuntimeError(f"Structure analysis failed: {str(e)}") from e

    def _clone_repository(self, github_url: str) -> str:
        """Clone repository and return temp dir path."""
        logger.debug(f"Cloning {github_url}...")
        temp_dir = clone_repository(github_url)
        logger.debug(f"Repository cloned to {temp_dir}")
        self._temp_directories.append(temp_dir)
        return temp_dir

    def _parse_repository_info(self, github_url: str) -> Dict[str, str]:
        """Parse GitHub URL and extract repository metadata."""
        return parse_github_url(github_url)

    def _analyze_structure(
        self,
        repo_dir: str,
        include_patterns: Optional[List[str]],
        exclude_patterns: Optional[List[str]],
    ) -> Dict[str, Any]:
        """Analyze repository file structure with filtering."""
        logger.debug(
            f"Initializing RepoAnalyzer with include: {include_patterns}, exclude: {exclude_patterns}"
        )
        repo_analyzer = RepoAnalyzer(include_patterns, exclude_patterns)
        return repo_analyzer.analyze_repository_structure(repo_dir)

    def _read_readme_file(self, repo_dir: str) -> Optional[str]:
        """Find and read the README file from the repository root."""
        # possible_readme_names = ["README.md", "README", "readme.md", "README.txt"]
        # for name in possible_readme_names:
        #     readme_path = Path(repo_dir) / name
        #     if readme_path.exists():
        #         try:
        #             logger.debug(f"Found README file at {readme_path}")
        #             return readme_path.read_text(encoding="utf-8")
        #         except Exception as e:
        #             logger.warning(f"Could not read README file at {readme_path}: {e}")
        #             return None
        # logger.debug("No README file found in repository root.")
        # return None
        base = Path(repo_dir)
        possible_readme_names = ["README.md", "README", "readme.md", "README.txt"]
        for name in possible_readme_names:
            p = base / name
            if p.exists():
                try:
                    assert_safe_path(base, p)
                    logger.debug(f"Found README file at {p}")
                    return safe_open_text(base, p, encoding="utf-8")
                except Exception as e:
                    logger.warning(f"Skipping unsafe/ unreadable README at {p}: {e}")
                    return None
        logger.debug("No README file found in repository root.")
        return None

    def _analyze_call_graph(self, file_tree: Dict[str, Any], repo_dir: str) -> Dict[str, Any]:
        """
        Perform multi-language call graph analysis.

        This method will be expanded to handle:
        - Python AST analysis (current)
        - JavaScript/TypeScript AST analysis (planned)
        - Additional language support (future)
        """
        logger.debug("Extracting code files from file tree...")
        code_files = self.call_graph_analyzer.extract_code_files(file_tree)

        logger.debug(f"Found {len(code_files)} total code files. Filtering for supported languages.")
        supported_files = self._filter_supported_languages(code_files)
        logger.debug(f"Analyzing {len(supported_files)} supported files.")

        result = self.call_graph_analyzer.analyze_code_files(supported_files, repo_dir)

        result["call_graph"]["supported_languages"] = self._get_supported_languages()
        result["call_graph"]["unsupported_files"] = len(code_files) - len(supported_files)

        return result

    def _filter_supported_languages(self, code_files: List[Dict]) -> List[Dict]:
        """
        Filter code files to only include supported languages.

        Supports Python, JavaScript, TypeScript, C, C++, Go, and Rust.
        """
        supported_languages = {
            "python",
            "javascript", 
            "typescript",
            "java",
            "csharp",
            "c",
            "cpp",
            "go",
            "rust",
        }

        return [
            file_info
            for file_info in code_files
            if file_info.get("language") in supported_languages
        ]

    def _get_supported_languages(self) -> List[str]:
        """Get list of currently supported languages for analysis."""
        return ["python", "javascript", "typescript", "java", "csharp", "c", "cpp"]

    def _cleanup_repository(self, temp_dir: str):
        """Clean up cloned repository."""
        logger.debug(f"Attempting to clean up {temp_dir}")
        cleanup_repository(temp_dir)
        if temp_dir in self._temp_directories:
            self._temp_directories.remove(temp_dir)

    def cleanup_all(self):
        """Clean up all tracked temporary directories."""
        for temp_dir in self._temp_directories[:]:
            self._cleanup_repository(temp_dir)

    def __del__(self):
        """Ensure cleanup on service destruction."""
        self.cleanup_all()


def analyze_repository(
    github_url: str, include_patterns=None, exclude_patterns=None
) -> tuple[AnalysisResult, None]:
    """
    Backward compatibility function.

    Returns:
        tuple: (AnalysisResult, None) - None instead of temp_dir since cleanup is handled internally
    """
    service = AnalysisService()
    result = service.analyze_repository_full(github_url, include_patterns, exclude_patterns)
    return result, None


def analyze_repository_structure_only(
    github_url: str, include_patterns=None, exclude_patterns=None
) -> tuple[Dict, None]:
    """
    Backward compatibility function.

    Returns:
        tuple: (structure_result, None) - None instead of temp_dir since cleanup is handled internally
    """
    service = AnalysisService()
    result = service.analyze_repository_structure_only(
        github_url, include_patterns, exclude_patterns
    )
    return result, None
