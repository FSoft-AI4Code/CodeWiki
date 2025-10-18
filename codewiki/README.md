# CodeWiki CLI

Transform your codebase into comprehensive documentation using AI-powered analysis.

## Features

- **Multi-Language Support**: Python, Java, JavaScript, TypeScript, C, C++, C#
- **AI-Powered Analysis**: Uses LLM models for intelligent documentation generation
- **Comprehensive Documentation**: Generates overview, module docs, and architecture diagrams
- **Dependency Analysis**: Tree-sitter based code parsing and dependency graph generation
- **Module Clustering**: Intelligent grouping of related code components
- **GitHub Pages Integration**: Generate beautiful, interactive HTML documentation
- **Git Workflow Support**: Automated branch creation and commit management
- **Secure Configuration**: API keys stored in system keychain
- **Progress Tracking**: Real-time progress updates with ETA
- **Mermaid Diagrams**: Automatic generation of architecture and data flow diagrams

## Installation

```bash
pip install codewiki
```

## Quick Start

### 1. Configure CodeWiki

```bash
codewiki config set \
  --api-key YOUR_API_KEY \
  --base-url https://api.anthropic.com \
  --main-model claude-sonnet-4 \
  --cluster-model claude-sonnet-4
```

### 2. Generate Documentation

```bash
cd /path/to/your/project
codewiki generate
```

Documentation will be created in `./docs/`

### 3. Generate with GitHub Pages

```bash
codewiki generate --github-pages
```

This creates an interactive HTML viewer at `./docs/index.html`

## Commands

### Configuration Management

```bash
# Set configuration
codewiki config set --api-key <key> --base-url <url> \
  --main-model <model> --cluster-model <model>

# Show configuration
codewiki config show

# Validate configuration
codewiki config validate
```

### Documentation Generation

```bash
# Basic generation
codewiki generate

# Custom output directory
codewiki generate --output ./documentation

# Create git branch
codewiki generate --create-branch

# Generate GitHub Pages HTML
codewiki generate --github-pages

# Full-featured
codewiki generate --create-branch --github-pages --verbose
```

## Configuration

Configuration is stored in:
- API keys: System keychain (macOS Keychain, Windows Credential Manager, Linux Secret Service)
- Settings: `~/.codewiki/config.json`

## Supported Languages

| Language   | Extensions          |
|------------|---------------------|
| Python     | `.py`               |
| Java       | `.java`             |
| JavaScript | `.js`, `.jsx`       |
| TypeScript | `.ts`, `.tsx`       |
| C          | `.c`, `.h`          |
| C++        | `.cpp`, `.hpp`, etc.|
| C#         | `.cs`               |

## Requirements

- Python 3.12+
- Git (optional, for branch management)
- LLM API access (Anthropic Claude, OpenAI, etc.)
- Tree-sitter language parsers (automatically installed)
- System keychain support (macOS Keychain, Windows Credential Manager, Linux Secret Service)

## Development

### Install from Source

```bash
git clone https://github.com/yourusername/codewiki.git
cd codewiki
pip install -e .
```

### Run Tests

```bash
pytest tests/
```

## License

MIT License - see LICENSE file for details

## Support

- Documentation: https://github.com/yourusername/codewiki/docs
- Issues: https://github.com/yourusername/codewiki/issues

