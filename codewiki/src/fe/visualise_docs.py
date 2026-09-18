#!/usr/bin/env python3
"""
Simple documentation server for hosting documentation folders.

This server serves documentation folders with the following structure:
- overview.md: The main overview document
- module_tree.json: Hierarchical structure of modules
- Various .md files for different modules

Usage:
    python docs_server.py --docs-folder path/to/docs --port 8080
"""

import argparse
import sys
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from markdown_it import MarkdownIt

from .template_utils import render_template
from .templates import DOCS_VIEW_TEMPLATE
from codewiki.src.utils import file_manager

app = FastAPI(
    title="Documentation Server",
    description="Simple documentation server for hosting markdown documentation folders",
)

# Global variables to store configuration
DOCS_FOLDER = None
MODULE_TREE = None


def initialize_globals():
    """Initialize global variables from environment or command line args if not already set."""
    global DOCS_FOLDER, MODULE_TREE

    if DOCS_FOLDER is None:
        # Try to get from environment variable or use a default
        import os

        docs_folder_path = os.environ.get("DOCS_FOLDER")
        if docs_folder_path and Path(docs_folder_path).exists():
            DOCS_FOLDER = docs_folder_path
            MODULE_TREE = load_module_tree(Path(docs_folder_path))
        else:
            # If no environment variable, we need to handle this gracefully
            # The FastAPI endpoints will need to check if DOCS_FOLDER is None
            pass


# Markdown parser
md = MarkdownIt()


def load_module_tree(docs_folder: Path) -> Optional[Dict]:
    """Load the module tree structure from module_tree.json."""
    tree_file = docs_folder / "module_tree.json"
    if not tree_file.exists():
        print(f"Warning: module_tree.json not found in {docs_folder}")
        return None

    try:
        return file_manager.load_json(tree_file)
    except Exception as e:
        print(f"Error loading module_tree.json: {e}")
        return None


def markdown_to_html(content: str) -> str:
    """Convert markdown content to HTML, with special handling for mermaid diagrams."""
    # First, convert markdown to HTML
    html = md.render(content)

    # Post-process to ensure mermaid code blocks are properly formatted
    # Look for code blocks with language-mermaid class and convert them to mermaid divs
    import re

    # Pattern to match mermaid code blocks
    pattern = r'<pre><code class="language-mermaid">(.*?)</code></pre>'

    def replace_mermaid(match):
        mermaid_code = match.group(1)
        # Decode HTML entities that might have been encoded
        import html

        mermaid_code = html.unescape(mermaid_code)
        return f'<div class="mermaid">{mermaid_code}</div>'

    # Replace mermaid code blocks with proper mermaid divs
    html = re.sub(pattern, replace_mermaid, html, flags=re.DOTALL)

    return html


def get_file_title(file_path: Path) -> str:
    """Extract title from markdown file, fallback to filename."""
    try:
        content = file_manager.load_text(file_path)
        first_line = content.split("\n")[0].strip()
        if first_line.startswith("# "):
            return first_line[2:].strip()
    except Exception:
        pass

    # Fallback to filename without extension
    return file_path.stem.replace("_", " ").title()


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the overview page as the main page."""
    initialize_globals()

    if DOCS_FOLDER is None:
        raise HTTPException(
            status_code=500,
            detail="Documentation folder not configured. Please set DOCS_FOLDER environment variable or run with --docs-folder argument.",
        )

    overview_file = Path(DOCS_FOLDER) / "overview.md"

    if not overview_file.exists():
        raise HTTPException(
            status_code=404, detail="overview.md not found in the documentation folder"
        )

    try:
        content = file_manager.load_text(overview_file)

        html_content = markdown_to_html(content)
        title = get_file_title(overview_file)

        context = {
            "title": title,
            "content": html_content,
            "navigation": MODULE_TREE,
            "current_page": "overview.md",
        }

        return HTMLResponse(content=render_template(DOCS_VIEW_TEMPLATE, context))

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading overview.md: {e}")


@app.get("/{filename:path}", response_class=HTMLResponse)
async def serve_doc(filename: str):
    """Serve individual documentation files."""
    initialize_globals()

    if DOCS_FOLDER is None:
        raise HTTPException(
            status_code=500,
            detail="Documentation folder not configured. Please set DOCS_FOLDER environment variable or run with --docs-folder argument.",
        )

    # Security check: ensure we're only serving .md files and they exist in the docs folder
    if not filename.endswith(".md"):
        raise HTTPException(status_code=404, detail="Only markdown files are supported")

    file_path = Path(DOCS_FOLDER) / filename

    # Ensure the file is within the docs folder (prevent directory traversal)
    try:
        file_path = file_path.resolve()
        docs_folder_resolved = Path(DOCS_FOLDER).resolve()
        if not str(file_path).startswith(str(docs_folder_resolved)):
            raise HTTPException(status_code=403, detail="Access denied")
    except Exception:
        raise HTTPException(status_code=403, detail="Invalid file path")

    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File {filename} not found")

    try:
        content = file_manager.load_text(file_path)

        html_content = markdown_to_html(content)
        title = get_file_title(file_path)

        context = {
            "title": title,
            "content": html_content,
            "navigation": MODULE_TREE,
            "current_page": filename,
        }

        return HTMLResponse(content=render_template(DOCS_VIEW_TEMPLATE, context))

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading {filename}: {e}")


# Mount static files
app.mount("/static", StaticFiles(directory="."), name="static")


def main():
    """Main function to run the documentation server."""
    parser = argparse.ArgumentParser(
        description="Simple documentation server for hosting markdown documentation folders"
    )
    parser.add_argument(
        "--docs-folder",
        type=str,
        required=True,
        help="Path to the documentation folder containing markdown files and module_tree.json",
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="Port to run the server on (default: 8000)"
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host to bind the server to (default: 127.0.0.1)",
    )
    parser.add_argument("--debug", action="store_true", help="Run the server in debug mode")

    args = parser.parse_args()

    # Validate docs folder
    docs_folder = Path(args.docs_folder)
    if not docs_folder.exists():
        print(f"Error: Documentation folder '{docs_folder}' does not exist")
        sys.exit(1)

    if not docs_folder.is_dir():
        print(f"Error: '{docs_folder}' is not a directory")
        sys.exit(1)

    # Check for overview.md
    overview_file = docs_folder / "overview.md"
    if not overview_file.exists():
        print(f"Warning: overview.md not found in '{docs_folder}'")

    # Set global variables and environment variable for uvicorn reload
    global DOCS_FOLDER, MODULE_TREE
    DOCS_FOLDER = str(docs_folder.resolve())
    MODULE_TREE = load_module_tree(docs_folder)

    # Set environment variable so uvicorn reload can pick it up
    import os

    os.environ["DOCS_FOLDER"] = DOCS_FOLDER

    print("📚 Starting documentation server...")
    print(f"📁 Documentation folder: {DOCS_FOLDER}")
    print(f"🌐 Server running at: http://{args.host}:{args.port}")
    print("📖 Main page: overview.md")

    if MODULE_TREE:
        modules_count = len(MODULE_TREE)
        print(f"🗂️  Found {modules_count} main modules in module_tree.json")

    print("\nPress Ctrl+C to stop the server")

    try:
        import uvicorn

        uvicorn.run(
            "visualise_docs:app",
            host=args.host,
            port=args.port,
            reload=args.debug,
            log_level="debug" if args.debug else "info",
        )
    except KeyboardInterrupt:
        print("\n👋 Server stopped")


if __name__ == "__main__":
    main()
