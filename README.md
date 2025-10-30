# CodeWiki: Evaluating AI’s Ability to Generate Holistic Documentation for Large-Scale Codebases

<div align="center">

![CodeWiki Architecture](img/framework-overview.png)

[![Python](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

**The first open-source framework for holistic, structured repository-level documentation across multilingual codebases**

[Demo](https://fsoft-ai4code.github.io/codewiki-demo/) • [Paper](https://arxiv.org/abs/2510.24428) • [Docker](docker/DOCKER_README.md) • [Development](DEVELOPMENT.md) • [Citation](#citation)

</div>

---

## Abstract

Given a large and evolving codebase, the ability to automatically generate holistic, architecture-aware documentation that captures not only individual functions but also their cross-file, cross-module, and system-level interactions remains an open challenge. We present **CodeWiki**, a unified framework for automated repository-level documentation across seven programming languages. CodeWiki introduces three key innovations: (i) hierarchical decomposition that preserves architectural context across multiple levels of granularity, (ii) recursive multi-agent processing with dynamic task delegation for scalable generation, and (iii) multi-modal synthesis that integrates textual descriptions with visual artifacts such as architecture diagrams and data-flow representations.

---

## Usage Example

![CLI Usage Example](img/cli-usage-example.gif)

---

## Overview

CodeWiki addresses the challenge of comprehensive documentation for large-scale repositories through three core innovations:

### Key Innovations

| Innovation | Description | Impact |
|------------|-------------|--------|
| **Hierarchical Decomposition** | Dynamic programming-inspired strategy that partitions repositories into coherent modules while preserving architectural context | Handles codebases of arbitrary size (86K-1.4M LOC tested) |
| **Recursive Agentic System** | Adaptive multi-agent processing with dynamic delegation capabilities for complex modules | Maintains quality while scaling to repository-level scope |
| **Multi-Modal Synthesis** | Generates textual documentation, architecture diagrams, data flows, and sequence diagrams | Comprehensive understanding from multiple perspectives |

### Multilingual Support

Supports **7 programming languages**: Python, Java, JavaScript, TypeScript, C, C++, C#

---

## Experimental Results

CodeWiki has been evaluated on **CodeWikiBench**, the first benchmark specifically designed for repository-level documentation quality assessment.

### Performance by Language Category

| Language Category | CodeWiki (Sonnet-4) | DeepWiki | Improvement |
|-------------------|---------------------|----------|-------------|
| High-Level (Python, JS, TS) | **79.14%** | 68.67% | **+10.47%** |
| Managed (C#, Java) | **68.84%** | 64.80% | **+4.04%** |
| Systems (C, C++) | 53.24% | 56.39% | -3.15% |
| **Overall Average** | **68.79%** | **64.06%** | **+4.73%** |

### Results on Representative Repositories

| Repository | Language | LOC | CodeWiki-Sonnet-4 | DeepWiki | Improvement |
|------------|----------|-----|-------------------|----------|-------------|
| All-Hands-AI--OpenHands | Python | 229K | **82.45%** | 73.04% | **+9.41%** |
| puppeteer--puppeteer | TypeScript | 136K | **83.00%** | 64.46% | **+18.54%** |
| sveltejs--svelte | JavaScript | 125K | **71.96%** | 68.51% | **+3.45%** |
| Unity-Technologies--ml-agents | C# | 86K | **79.78%** | 74.80% | **+4.98%** |
| elastic--logstash | Java | 117K | **57.90%** | 54.80% | **+3.10%** |

**View comprehensive results:** See [paper](https://arxiv.org/abs/2510.24428) for complete evaluation on 21 repositories spanning all supported languages.

---

## CLI Installation & Usage

### Prerequisites

- Python 3.12+
- Node.js (for mermaid diagram validation)
- LLM API access (Anthropic Claude, OpenAI, etc.)

### Installation

```bash
# Install from source
pip install git+https://github.com/FSoft-AI4Code/CodeWiki.git

# Verify installation
codewiki --version
```

### Quick Start

#### 1. Configure CodeWiki

```bash
codewiki config set \
  --api-key YOUR_API_KEY \
  --base-url https://api.anthropic.com \
  --main-model claude-sonnet-4 \
  --cluster-model claude-sonnet-4
```

Verify configuration:

```bash
codewiki config show
codewiki config validate
```

#### 2. Generate Documentation

```bash
# Navigate to your project
cd /path/to/your/project

# Generate documentation (saved to ./docs/)
codewiki generate

# Generate with GitHub Pages HTML viewer
codewiki generate --github-pages

# Full-featured generation
codewiki generate --create-branch --github-pages --verbose
```

### CLI Commands

```bash
# Configuration Management
codewiki config set --api-key <key> --base-url <url> \
  --main-model <model> --cluster-model <model>
codewiki config show
codewiki config validate

# Documentation Generation
codewiki generate                           # Basic generation
codewiki generate --output ./documentation  # Custom output directory
codewiki generate --create-branch           # Create git branch
codewiki generate --github-pages            # Generate HTML viewer
codewiki generate --verbose                 # Detailed logging
```

### Configuration Storage

- **API keys**: System keychain (macOS Keychain, Windows Credential Manager, Linux Secret Service)
- **Settings**: `~/.codewiki/config.json`

---

## Additional Documentation

- **[Docker Deployment](docker/DOCKER_README.md)** - Containerized deployment instructions
- **[Development Guide](DEVELOPMENT.md)** - Project structure, architecture, and contributing guidelines

---

## Documentation Output

Generated documentation includes:

### Textual Documentation
- Repository overview with architecture guide
- Module-level documentation with API references
- Usage examples and implementation patterns

### Visual Artifacts
- System architecture diagrams (Mermaid)
- Data flow visualizations
- Dependency graphs

### Output Structure

```
./docs/
├── overview.md              # Repository overview (start here!)
├── module1.md               # Module documentation
├── module2.md               # Additional modules...
├── module_tree.json         # Hierarchical module structure
├── first_module_tree.json   # Initial clustering result
├── metadata.json            # Generation metadata
└── index.html               # Interactive viewer (with --github-pages)
```

---

## Citation

If you use CodeWiki in your research, please cite:

```bibtex
@misc{anh2025codewikievaluatingaiabilitytogenerateholisticdocumentationforlargescalecodebase,
      title={CodeWiki: Evaluating AI's Ability to Generate Holistic Documentation for Large-Scale Codebases}, 
      author={Nguyen Hoang Anh and Minh Le-Anh and Bach Le and Nghi D. Q. Bui},
      year={2025},
      eprint={2510.24428},
      archivePrefix={arXiv},
      primaryClass={cs.SE},
      url={https://arxiv.org/abs/2510.24428}, 
}
```

---

## Resources

- **Live Demo**: [View documentation examples](https://fsoft-ai4code.github.io/codewiki-demo/)
- **Benchmark**: [CodeWikiBench](https://github.com/FSoft-AI4Code/CodeWikiBench)

---

## License

MIT License

---

<div align="center">

**Made with ❤️ by the CodeWiki Team**

[⬆ Back to Top](#codewiki)

</div>
