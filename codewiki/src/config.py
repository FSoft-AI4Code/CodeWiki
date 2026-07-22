from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
import argparse
import os
import sys
from dotenv import load_dotenv
load_dotenv()

# Constants
OUTPUT_BASE_DIR = 'output'
DEPENDENCY_GRAPHS_DIR = 'dependency_graphs'
DOCS_DIR = 'docs'
FIRST_MODULE_TREE_FILENAME = 'first_module_tree.json'
MODULE_TREE_FILENAME = 'module_tree.json'
OVERVIEW_FILENAME = 'overview.md'
MAX_DEPTH = 2
# Default max token settings
DEFAULT_MAX_TOKENS = 32_768
DEFAULT_MAX_TOKEN_PER_MODULE = 36_369
DEFAULT_MAX_TOKEN_PER_LEAF_MODULE = 16_000
# Legacy constants (for backward compatibility)
MAX_TOKEN_PER_MODULE = DEFAULT_MAX_TOKEN_PER_MODULE
MAX_TOKEN_PER_LEAF_MODULE = DEFAULT_MAX_TOKEN_PER_LEAF_MODULE

# CLI context detection
_CLI_CONTEXT = False

def set_cli_context(enabled: bool = True):
    """Set whether we're running in CLI context (vs web app)."""
    global _CLI_CONTEXT
    _CLI_CONTEXT = enabled

def is_cli_context() -> bool:
    """Check if running in CLI context."""
    return _CLI_CONTEXT

# LLM services
# In CLI mode, these will be loaded from ~/.codewiki/config.json + keyring
# In web app mode, use environment variables
MAIN_MODEL = os.getenv('MAIN_MODEL', 'claude-sonnet-4')
FALLBACK_MODEL_1 = os.getenv('FALLBACK_MODEL_1', 'glm-4p5')
CLUSTER_MODEL = os.getenv('CLUSTER_MODEL', MAIN_MODEL)
LLM_BASE_URL = os.getenv('LLM_BASE_URL', 'http://0.0.0.0:4000/')
LLM_API_KEY = os.getenv('LLM_API_KEY', 'sk-1234')

# Atlas Cloud default endpoint (OpenAI-compatible). Used to auto-fill the base URL
# when the user selects the `atlas-cloud` provider without passing --base-url.
ATLAS_CLOUD_BASE_URL = "https://api.atlascloud.ai/v1"

@dataclass
class Config:
    """Configuration class for CodeWiki."""
    repo_path: str
    output_dir: str
    dependency_graph_dir: str
    docs_dir: str
    max_depth: int
    # LLM configuration
    llm_base_url: str
    llm_api_key: str
    main_model: str
    cluster_model: str
    fallback_model: str = FALLBACK_MODEL_1
    # Provider configuration
    provider: str = "openai-compatible"  # openai-compatible, atlas-cloud, anthropic, bedrock, azure-openai
    aws_region: str = "us-east-1"
    api_version: str = "2024-12-01-preview"  # Azure OpenAI API version
    azure_deployment: str = ""  # Azure OpenAI deployment name
    # Max token settings
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_token_per_module: int = DEFAULT_MAX_TOKEN_PER_MODULE
    max_token_per_leaf_module: int = DEFAULT_MAX_TOKEN_PER_LEAF_MODULE
    # Agent instructions for customization
    agent_instructions: Optional[Dict[str, Any]] = None
    # Apply Git ignore rules before dependency analysis
    use_gitignore: bool = True
    
    @property
    def include_patterns(self) -> Optional[List[str]]:
        """Get file include patterns from agent instructions."""
        if self.agent_instructions:
            return self.agent_instructions.get('include_patterns')
        return None
    
    @property
    def exclude_patterns(self) -> Optional[List[str]]:
        """Get file exclude patterns from agent instructions."""
        if self.agent_instructions:
            return self.agent_instructions.get('exclude_patterns')
        return None
    
    @property
    def focus_modules(self) -> Optional[List[str]]:
        """Get focus modules from agent instructions."""
        if self.agent_instructions:
            return self.agent_instructions.get('focus_modules')
        return None
    
    @property
    def doc_type(self) -> Optional[str]:
        """Get documentation type from agent instructions."""
        if self.agent_instructions:
            return self.agent_instructions.get('doc_type')
        return None
    
    @property
    def custom_instructions(self) -> Optional[str]:
        """Get custom instructions from agent instructions."""
        if self.agent_instructions:
            return self.agent_instructions.get('custom_instructions')
        return None
    
    def get_prompt_addition(self) -> str:
        """Generate prompt additions based on agent instructions."""
        if not self.agent_instructions:
            return ""
        
        additions = []
        
        if self.doc_type:
            doc_type_instructions = {
                'api': "Focus on API documentation: endpoints, parameters, return types, and usage examples.",
                'architecture': "Focus on architecture documentation: system design, component relationships, and data flow.",
                'user-guide': "Focus on user guide documentation: how to use features, step-by-step tutorials.",
                'developer': "Focus on developer documentation: code structure, contribution guidelines, and implementation details.",
            }
            if self.doc_type.lower() in doc_type_instructions:
                additions.append(doc_type_instructions[self.doc_type.lower()])
            else:
                additions.append(f"Focus on generating {self.doc_type} documentation.")
        
        if self.focus_modules:
            additions.append(f"Pay special attention to and provide more detailed documentation for these modules: {', '.join(self.focus_modules)}")
        
        if self.custom_instructions:
            additions.append(f"Additional instructions: {self.custom_instructions}")
        
        return "\n".join(additions) if additions else ""
    
    @classmethod
    def from_args(cls, args: argparse.Namespace) -> 'Config':
        """Create configuration from parsed arguments."""
        repo_name = os.path.basename(os.path.normpath(args.repo_path))
        sanitized_repo_name = ''.join(c if c.isalnum() else '_' for c in repo_name)
        
        return cls(
            repo_path=args.repo_path,
            output_dir=OUTPUT_BASE_DIR,
            dependency_graph_dir=os.path.join(OUTPUT_BASE_DIR, DEPENDENCY_GRAPHS_DIR),
            docs_dir=os.path.join(OUTPUT_BASE_DIR, DOCS_DIR, f"{sanitized_repo_name}-docs"),
            max_depth=MAX_DEPTH,
            llm_base_url=LLM_BASE_URL,
            llm_api_key=LLM_API_KEY,
            main_model=MAIN_MODEL,
            cluster_model=CLUSTER_MODEL,
            fallback_model=FALLBACK_MODEL_1,
            use_gitignore=getattr(args, "use_gitignore", True),
        )
    
    @classmethod
    def from_cli(
        cls,
        repo_path: str,
        output_dir: str,
        llm_base_url: str,
        llm_api_key: str,
        main_model: str,
        cluster_model: str,
        fallback_model: str = FALLBACK_MODEL_1,
        provider: str = "openai-compatible",
        aws_region: str = "us-east-1",
        api_version: str = "2024-12-01-preview",
        azure_deployment: str = "",
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_token_per_module: int = DEFAULT_MAX_TOKEN_PER_MODULE,
        max_token_per_leaf_module: int = DEFAULT_MAX_TOKEN_PER_LEAF_MODULE,
        max_depth: int = MAX_DEPTH,
        agent_instructions: Optional[Dict[str, Any]] = None,
        use_gitignore: bool = True,
    ) -> 'Config':
        """
        Create configuration for CLI context.

        Args:
            repo_path: Repository path
            output_dir: Output directory for generated docs
            llm_base_url: LLM API base URL
            llm_api_key: LLM API key
            main_model: Primary model
            cluster_model: Clustering model
            fallback_model: Fallback model
            provider: LLM provider type (openai-compatible, atlas-cloud, anthropic, bedrock, azure-openai)
            aws_region: AWS region for Bedrock provider
            api_version: Azure OpenAI API version
            azure_deployment: Azure OpenAI deployment name
            max_tokens: Maximum tokens for LLM response
            max_token_per_module: Maximum tokens per module for clustering
            max_token_per_leaf_module: Maximum tokens per leaf module
            max_depth: Maximum depth for hierarchical decomposition
            agent_instructions: Custom agent instructions dict
            use_gitignore: Whether to apply Git ignore rules

        Returns:
            Config instance
        """
        repo_name = os.path.basename(os.path.normpath(repo_path))
        base_output_dir = os.path.join(output_dir, "temp")

        return cls(
            repo_path=repo_path,
            output_dir=base_output_dir,
            dependency_graph_dir=os.path.join(base_output_dir, DEPENDENCY_GRAPHS_DIR),
            docs_dir=output_dir,
            max_depth=max_depth,
            llm_base_url=llm_base_url,
            llm_api_key=llm_api_key,
            main_model=main_model,
            cluster_model=cluster_model,
            fallback_model=fallback_model,
            provider=provider,
            aws_region=aws_region,
            api_version=api_version,
            azure_deployment=azure_deployment,
            max_tokens=max_tokens,
            max_token_per_module=max_token_per_module,
            max_token_per_leaf_module=max_token_per_leaf_module,
            agent_instructions=agent_instructions,
            use_gitignore=use_gitignore,
        )
