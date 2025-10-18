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
        
    
    def load_module_tree(self, docs_dir: Path) -> Dict[str, Any]:
        """
        Load module tree from documentation directory.
        
        Args:
            docs_dir: Documentation directory path
            
        Returns:
            Module tree structure
        """
        module_tree_path = docs_dir / "module_tree.json"
        if not module_tree_path.exists():
            # Fallback to a simple structure
            return {
                "Overview": {
                    "description": "Repository overview",
                    "components": [],
                    "children": {}
                }
            }
        
        try:
            content = safe_read(module_tree_path)
            return json.loads(content)
        except Exception as e:
            raise FileSystemError(f"Failed to load module tree: {e}")
    
    def load_metadata(self, docs_dir: Path) -> Optional[Dict[str, Any]]:
        """
        Load metadata from documentation directory.
        
        Args:
            docs_dir: Documentation directory path
            
        Returns:
            Metadata dictionary or None if not found
        """
        metadata_path = docs_dir / "metadata.json"
        if not metadata_path.exists():
            return None
        
        try:
            content = safe_read(metadata_path)
            return json.loads(content)
        except Exception:
            # Non-critical, return None
            return None
    
    
    def generate(
        self,
        output_path: Path,
        title: str,
        module_tree: Optional[Dict[str, Any]] = None,
        repository_url: Optional[str] = None,
        github_pages_url: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        docs_dir: Optional[Path] = None,
        metadata: Optional[Dict[str, Any]] = None
    ):
        """
        Generate HTML documentation viewer.
        
        Args:
            output_path: Output file path (index.html)
            title: Documentation title
            module_tree: Module tree structure (auto-loaded from docs_dir if not provided)
            repository_url: GitHub repository URL
            github_pages_url: Expected GitHub Pages URL
            config: Additional configuration
            docs_dir: Documentation directory (for auto-loading module_tree and metadata)
            metadata: Metadata dictionary (auto-loaded from docs_dir if not provided)
        """
        # Auto-load module tree and metadata if docs_dir provided
        if docs_dir and module_tree is None:
            module_tree = self.load_module_tree(docs_dir)
        
        if docs_dir and metadata is None:
            metadata = self.load_metadata(docs_dir)
        
        # Default values
        if module_tree is None:
            module_tree = {}
        
        if config is None:
            config = {}
        
        # Generate HTML content
        html_content = self._generate_html_template(
            title=title,
            module_tree=module_tree,
            repository_url=repository_url,
            github_pages_url=github_pages_url,
            metadata=metadata,
            config=config
        )
        
        # Write to file
        safe_write(output_path, html_content)
    
    def _generate_html_template(
        self,
        title: str,
        module_tree: Dict[str, Any],
        repository_url: Optional[str],
        github_pages_url: Optional[str],
        metadata: Optional[Dict[str, Any]],
        config: Dict[str, Any]
    ) -> str:
        """
        Generate the complete HTML template with embedded styles and scripts.
        
        Args:
            title: Documentation title
            module_tree: Module tree structure
            repository_url: GitHub repository URL
            github_pages_url: GitHub Pages URL
            metadata: Metadata dictionary
            config: Additional configuration
            
        Returns:
            Complete HTML string
        """
        # Escape and prepare JSON data for embedding
        module_tree_json = json.dumps(module_tree)
        metadata_json = json.dumps(metadata) if metadata else "null"
        
        html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{self._escape_html(title)}</title>
    
    <!-- Markdown rendering library -->
    <script src="https://cdn.jsdelivr.net/npm/marked@11.1.0/marked.min.js"></script>
    
    <!-- Mermaid diagrams -->
    <script src="https://cdn.jsdelivr.net/npm/mermaid@11.9.0/dist/mermaid.min.js"></script>
    
    <style>
        :root {{
            --primary-color: #2563eb;
            --secondary-color: #f1f5f9;
            --text-color: #334155;
            --border-color: #e2e8f0;
            --hover-color: #f8fafc;
        }}
        
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            line-height: 1.6;
            color: var(--text-color);
            background-color: #ffffff;
        }}
        
        .container {{
            display: flex;
            min-height: 100vh;
        }}
        
        .sidebar {{
            width: 300px;
            background-color: var(--secondary-color);
            border-right: 1px solid var(--border-color);
            padding: 20px;
            overflow-y: auto;
            position: fixed;
            height: 100vh;
        }}
        
        .content {{
            flex: 1;
            margin-left: 300px;
            padding: 40px 60px;
            max-width: calc(100% - 300px);
        }}
        
        .logo {{
            font-size: 24px;
            font-weight: bold;
            color: var(--primary-color);
            margin-bottom: 30px;
            display: block;
            text-decoration: none;
            cursor: pointer;
        }}
        
        .logo:hover {{
            color: #1d4ed8;
        }}
        
        .metadata-box {{
            margin: 20px 0;
            padding: 15px;
            background: #f8fafc;
            border-radius: 8px;
            border: 1px solid #e2e8f0;
        }}
        
        .metadata-box h4 {{
            margin: 0 0 10px 0;
            font-size: 12px;
            color: #64748b;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}
        
        .metadata-box div {{
            font-size: 11px;
            color: #475569;
            line-height: 1.4;
            margin-bottom: 4px;
        }}
        
        .nav-section {{
            margin-bottom: 25px;
        }}
        
        .nav-section-header {{
            font-size: 14px;
            font-weight: 600;
            color: #64748b;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 10px;
            padding: 8px 12px;
        }}
        
        .nav-item {{
            display: block;
            padding: 8px 12px;
            color: var(--text-color);
            text-decoration: none;
            border-radius: 6px;
            font-size: 14px;
            transition: all 0.2s ease;
            margin-bottom: 2px;
            cursor: pointer;
        }}
        
        .nav-item:hover {{
            background-color: var(--hover-color);
            color: var(--primary-color);
        }}
        
        .nav-item.active {{
            background-color: var(--primary-color);
            color: white;
        }}
        
        .nav-subsection {{
            margin-left: 15px;
            margin-top: 8px;
        }}
        
        .nav-subsection .nav-item {{
            font-size: 13px;
            color: #64748b;
        }}
        
        .nav-subsection .nav-subsection {{
            margin-left: 20px;
        }}
        
        .nav-subsection .nav-subsection .nav-item {{
            font-size: 12px;
        }}
        
        .markdown-content {{
            max-width: none;
        }}
        
        .markdown-content h1 {{
            font-size: 2.5rem;
            font-weight: 700;
            color: #1e293b;
            margin-bottom: 1rem;
            border-bottom: 2px solid var(--border-color);
            padding-bottom: 0.5rem;
        }}
        
        .markdown-content h2 {{
            font-size: 2rem;
            font-weight: 600;
            color: #334155;
            margin-top: 2rem;
            margin-bottom: 1rem;
        }}
        
        .markdown-content h3 {{
            font-size: 1.5rem;
            font-weight: 600;
            color: #475569;
            margin-top: 1.5rem;
            margin-bottom: 0.75rem;
        }}
        
        .markdown-content h4 {{
            font-size: 1.25rem;
            font-weight: 600;
            color: #475569;
            margin-top: 1.25rem;
            margin-bottom: 0.5rem;
        }}
        
        .markdown-content p {{
            margin-bottom: 1rem;
            color: #475569;
        }}
        
        .markdown-content ul, .markdown-content ol {{
            margin-bottom: 1rem;
            padding-left: 1.5rem;
        }}
        
        .markdown-content li {{
            margin-bottom: 0.5rem;
            color: #475569;
        }}
        
        .markdown-content code {{
            background-color: #f1f5f9;
            padding: 0.25rem 0.5rem;
            border-radius: 0.25rem;
            font-family: 'Fira Code', 'Consolas', monospace;
            font-size: 0.875rem;
        }}
        
        .markdown-content pre {{
            background-color: #f8fafc;
            border: 1px solid var(--border-color);
            border-radius: 0.5rem;
            padding: 1rem;
            overflow-x: auto;
            margin-bottom: 1rem;
        }}
        
        .markdown-content pre code {{
            background-color: transparent;
            padding: 0;
        }}
        
        .markdown-content blockquote {{
            border-left: 4px solid var(--primary-color);
            padding-left: 1rem;
            margin-bottom: 1rem;
            font-style: italic;
            color: #64748b;
        }}
        
        .markdown-content table {{
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 1rem;
        }}
        
        .markdown-content th, .markdown-content td {{
            border: 1px solid var(--border-color);
            padding: 0.75rem;
            text-align: left;
        }}
        
        .markdown-content th {{
            background-color: var(--secondary-color);
            font-weight: 600;
        }}
        
        .markdown-content a {{
            color: var(--primary-color);
            text-decoration: underline;
        }}
        
        .markdown-content a:hover {{
            text-decoration: none;
        }}
        
        .error-message {{
            padding: 1rem;
            background-color: #fef2f2;
            border: 1px solid #fecaca;
            border-radius: 8px;
            color: #991b1b;
            margin: 2rem 0;
        }}
        
        .loading {{
            text-align: center;
            padding: 3rem;
            color: #64748b;
        }}
        
        @media (max-width: 768px) {{
            .sidebar {{
                width: 100%;
                position: relative;
                height: auto;
            }}
            
            .content {{
                margin-left: 0;
                padding: 20px;
                max-width: 100%;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <nav class="sidebar" id="sidebar">
            <div class="logo" onclick="loadPage('overview.md')">📚 {self._escape_html(title)}</div>
            
            <div id="metadata-container"></div>
            <div id="navigation-container"></div>
        </nav>
        
        <main class="content">
            <div class="markdown-content" id="content">
                <div class="loading">Loading...</div>
            </div>
        </main>
    </div>
    
    <script>
        // Embedded data
        const MODULE_TREE = {module_tree_json};
        const METADATA = {metadata_json};
        const REPO_URL = {json.dumps(repository_url) if repository_url else "null"};
        const GITHUB_PAGES_URL = {json.dumps(github_pages_url) if github_pages_url else "null"};
        
        // Current state
        let currentPage = 'overview.md';
        
        // Initialize mermaid
        mermaid.initialize({{
            startOnLoad: false,
            theme: 'default',
            themeVariables: {{
                primaryColor: '#2563eb',
                primaryTextColor: '#334155',
                primaryBorderColor: '#e2e8f0',
                lineColor: '#64748b',
                sectionBkgColor: '#f8fafc',
                altSectionBkgColor: '#f1f5f9',
                gridColor: '#e2e8f0',
                secondaryColor: '#f1f5f9',
                tertiaryColor: '#f8fafc'
            }},
            flowchart: {{
                htmlLabels: true,
                curve: 'basis'
            }}
        }});
        
        // Configure marked
        marked.setOptions({{
            breaks: true,
            gfm: true,
            headerIds: true,
            mangle: false
        }});
        
        // Custom renderer to handle mermaid code blocks
        const renderer = new marked.Renderer();
        const originalCodeRenderer = renderer.code.bind(renderer);
        
        renderer.code = function(code, language) {{
            if (language === 'mermaid') {{
                return `<div class="mermaid">${{code}}</div>`;
            }}
            return originalCodeRenderer(code, language);
        }};
        
        marked.use({{ renderer }});
        
        // Escape HTML
        function escapeHtml(text) {{
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }}
        
        // Format title from filename
        function formatTitle(filename) {{
            return filename
                .replace('.md', '')
                .replace(/_/g, ' ')
                .split(' ')
                .map(word => word.charAt(0).toUpperCase() + word.slice(1))
                .join(' ');
        }}
        
        // Render metadata box
        function renderMetadata() {{
            if (!METADATA) return;
            
            const container = document.getElementById('metadata-container');
            const genInfo = METADATA.generation_info || {{}};
            const stats = METADATA.statistics || {{}};
            
            let html = '<div class="metadata-box"><h4>Generation Info</h4>';
            
            if (genInfo.main_model) {{
                html += `<div><strong>Model:</strong> ${{escapeHtml(genInfo.main_model)}}</div>`;
            }}
            
            if (genInfo.timestamp) {{
                const timestamp = genInfo.timestamp.substring(0, 16);
                html += `<div><strong>Generated:</strong> ${{escapeHtml(timestamp)}}</div>`;
            }}
            
            if (genInfo.commit_id) {{
                const commitShort = genInfo.commit_id.substring(0, 8);
                html += `<div><strong>Commit:</strong> ${{escapeHtml(commitShort)}}</div>`;
            }}
            
            if (stats.total_components) {{
                html += `<div><strong>Components:</strong> ${{stats.total_components}}</div>`;
            }}
            
            html += '</div>';
            container.innerHTML = html;
        }}
        
        // Render navigation from module tree
        function renderNavigation() {{
            const container = document.getElementById('navigation-container');
            
            // Add overview link
            let html = '<div class="nav-section">';
            html += '<div class="nav-item' + (currentPage === 'overview.md' ? ' active' : '') + '" onclick="loadPage(\'overview.md\')">Overview</div>';
            html += '</div>';
            
            // Render module tree
            html += renderModuleTree(MODULE_TREE, 0);
            
            container.innerHTML = html;
        }}
        
        // Recursively render module tree
        function renderModuleTree(tree, depth) {{
            let html = '';
            
            for (const [key, data] of Object.entries(tree)) {{
                const filename = key + '.md';
                const isActive = currentPage === filename;
                const displayName = formatTitle(key);
                
                if (depth === 0) {{
                    html += '<div class="nav-section">';
                }}
                
                // If has components, it's a clickable page
                if (data.components && data.components.length > 0) {{
                    const classes = 'nav-item' + (isActive ? ' active' : '');
                    html += `<div class="${{classes}}" onclick="loadPage('${{filename}}')">${{escapeHtml(displayName)}}</div>`;
                }} else {{
                    // Otherwise it's just a header
                    html += `<div class="nav-section-header">${{escapeHtml(displayName)}}</div>`;
                }}
                
                // Render children
                if (data.children && Object.keys(data.children).length > 0) {{
                    html += '<div class="nav-subsection">';
                    html += renderModuleTree(data.children, depth + 1);
                    html += '</div>';
                }}
                
                if (depth === 0) {{
                    html += '</div>';
                }}
            }}
            
            return html;
        }}
        
        // Load and render a markdown page
        async function loadPage(filename) {{
            currentPage = filename;
            const contentEl = document.getElementById('content');
            
            try {{
                // Show loading
                contentEl.innerHTML = '<div class="loading">Loading...</div>';
                
                // Fetch markdown file
                const response = await fetch(filename);
                
                if (!response.ok) {{
                    throw new Error(`Failed to load ${{filename}}: ${{response.statusText}}`);
                }}
                
                const markdown = await response.text();
                
                // Convert to HTML
                const html = marked.parse(markdown);
                contentEl.innerHTML = html;
                
                // Render mermaid diagrams
                await mermaid.run({{
                    querySelector: '.mermaid'
                }});
                
                // Update navigation
                renderNavigation();
                
                // Scroll to top
                window.scrollTo(0, 0);
                
                // Update URL hash
                window.location.hash = filename;
                
            }} catch (error) {{
                console.error('Error loading page:', error);
                contentEl.innerHTML = `
                    <div class="error-message">
                        <h3>Error Loading Page</h3>
                        <p>Could not load <strong>${{escapeHtml(filename)}}</strong></p>
                        <p>${{escapeHtml(error.message)}}</p>
                    </div>
                `;
            }}
        }}
        
        // Handle hash navigation
        function handleHashChange() {{
            const hash = window.location.hash.substring(1);
            if (hash && hash.endsWith('.md')) {{
                loadPage(hash);
            }}
        }}
        
        // Initialize app
        async function init() {{
            // Render metadata and navigation
            renderMetadata();
            renderNavigation();
            
            // Load initial page from hash or default to overview
            const hash = window.location.hash.substring(1);
            const initialPage = hash && hash.endsWith('.md') ? hash : 'overview.md';
            await loadPage(initialPage);
            
            // Listen for hash changes
            window.addEventListener('hashchange', handleHashChange);
        }}
        
        // Start when DOM is ready
        if (document.readyState === 'loading') {{
            document.addEventListener('DOMContentLoaded', init);
        }} else {{
            init();
        }}
    </script>
</body>
</html>'''
        
        return html
    
    def _escape_html(self, text: str) -> str:
        """
        Escape HTML special characters.
        
        Args:
            text: Text to escape
            
        Returns:
            Escaped text
        """
        if not text:
            return ""
        
        return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;"))

    
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

