"""
HTML generator for GitHub Pages documentation viewer.
"""

import json
from pathlib import Path
from typing import Optional, Dict, Any

from codewiki.cli.utils.errors import FileSystemError
from codewiki.cli.utils.fs import safe_write, safe_read


class HTMLGenerator:
    """
    Generates static HTML documentation viewer for GitHub Pages.
    
    Creates a self-contained index.html with embedded styles, scripts,
    and configuration for client-side markdown rendering.
    """
    
    def __init__(self, template_dir: Optional[Path] = None):
        """
        Initialize HTML generator.
        
        Args:
            template_dir: Path to template directory (default: package templates)
        """
        if template_dir is None:
            # Use package templates
            template_dir = Path(__file__).parent.parent / "templates" / "github_pages"
        
        self.template_dir = Path(template_dir)
        
        # Load templates
        self.html_template = self._load_template("index.html")
        self.styles = self._load_template("styles.css")
        self.app_script = self._load_template("app.js")
    
    def _load_template(self, filename: str) -> str:
        """
        Load a template file.
        
        Args:
            filename: Template filename
            
        Returns:
            Template content
            
        Raises:
            FileSystemError: If template cannot be loaded
        """
        template_path = self.template_dir / filename
        if not template_path.exists():
            raise FileSystemError(f"Template not found: {template_path}")
        
        return safe_read(template_path)
    
    def generate(
        self,
        output_path: Path,
        title: str,
        module_tree: Dict[str, Any],
        repository_url: Optional[str] = None,
        github_pages_url: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None
    ):
        """
        Generate HTML documentation viewer.
        
        Args:
            output_path: Output file path (index.html)
            title: Documentation title
            module_tree: Module tree structure
            repository_url: GitHub repository URL
            github_pages_url: Expected GitHub Pages URL
            config: Additional configuration
        """
        # Build configuration
        full_config = {
            "title": title,
            "repository_url": repository_url or "",
            "github_pages_url": github_pages_url or "",
            "docs_path": ".",
            "default_doc": "overview.md",
            "navigation": {
                "show_file_tree": True,
                "collapsible": True,
                "default_expanded_depth": 1
            },
            "markdown": {
                "syntax_theme": "github",
                "line_numbers": True,
                "copy_button": True,
                "gfm": True
            },
            "mermaid": {
                "theme": "default",
                "flowchart": {"curve": "basis"}
            },
            "features": {
                "breadcrumbs": True,
                "table_of_contents": True,
                "search": False
            }
        }
        
        if config:
            full_config.update(config)
        
        # Replace template variables
        html = self.html_template
        html = html.replace("{{TITLE}}", title)
        html = html.replace("{{STYLES}}", self.styles)
        html = html.replace("{{APP_SCRIPT}}", self.app_script)
        html = html.replace("{{CONFIG_JSON}}", json.dumps(full_config, indent=2))
        html = html.replace("{{MODULE_TREE_JSON}}", json.dumps(module_tree, indent=2))
        
        # Handle optional repository URL (Mustache-style)
        if repository_url:
            html = html.replace("{{#REPOSITORY_URL}}", "")
            html = html.replace("{{/REPOSITORY_URL}}", "")
            html = html.replace("{{REPOSITORY_URL}}", repository_url)
        else:
            # Remove conditional block
            import re
            html = re.sub(r'\{\{#REPOSITORY_URL\}\}.*?\{\{/REPOSITORY_URL\}\}', '', html)
        
        # Write output
        safe_write(output_path, html)
    
    def detect_repository_info(self, repo_path: Path) -> Dict[str, Optional[str]]:
        """
        Detect repository information from git.
        
        Args:
            repo_path: Repository path
            
        Returns:
            Dictionary with 'name', 'url', 'github_pages_url'
        """
        info = {
            'name': repo_path.name,
            'url': None,
            'github_pages_url': None,
        }
        
        try:
            import git
            repo = git.Repo(repo_path)
            
            # Get repository name
            info['name'] = repo_path.name
            
            # Get remote URL
            if repo.remotes:
                remote_url = repo.remotes.origin.url
                
                # Clean URL
                if remote_url.startswith('git@github.com:'):
                    remote_url = remote_url.replace('git@github.com:', 'https://github.com/')
                
                remote_url = remote_url.rstrip('/').replace('.git', '')
                info['url'] = remote_url
                
                # Compute GitHub Pages URL
                if 'github.com' in remote_url:
                    parts = remote_url.split('/')
                    if len(parts) >= 2:
                        owner = parts[-2]
                        repo = parts[-1]
                        info['github_pages_url'] = f"https://{owner}.github.io/{repo}/"
        
        except Exception:
            pass
        
        return info

