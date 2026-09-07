"""
Configuration data models for CodeWiki CLI.

This module contains the Configuration class which represents persistent
user settings stored in ~/.codewiki/config.json. These settings are converted
to the backend Config class when running documentation generation.
"""

from dataclasses import dataclass, asdict, field
from typing import Optional, List
from pathlib import Path

from codewiki.cli.utils.validation import (
    validate_url,
    validate_api_key,
    validate_model_name,
)


@dataclass
class AgentInstructions:
    """
    Custom instructions for the documentation agent.
    
    Allows users to customize:
    - File filtering (include/exclude patterns)
    - Module focus (prioritize certain modules)
    - Documentation type (API docs, architecture docs, etc.)
    - Custom instructions for the LLM
    
    Attributes:
        include_patterns: File patterns to include (e.g., ["*.cs", "*.py"])
        exclude_patterns: File/directory patterns to exclude (e.g., ["*Tests*", "*test*"])
        focus_modules: Modules to document in more detail
        doc_type: Type of documentation to generate
        custom_instructions: Additional instructions for the documentation agent
    """
    include_patterns: Optional[List[str]] = None  # e.g., ["*.cs"] for C# projects
    exclude_patterns: Optional[List[str]] = None  # e.g., ["*Tests*", "*Specs*"]
    focus_modules: Optional[List[str]] = None  # e.g., ["src/core", "src/api"]
    doc_type: Optional[str] = None  # e.g., "api", "architecture", "user-guide"
    custom_instructions: Optional[str] = None  # Free-form instructions
    
    def to_dict(self) -> dict:
        """Convert to dictionary, excluding None values."""
        result = {}
        if self.include_patterns:
            result['include_patterns'] = self.include_patterns
        if self.exclude_patterns:
            result['exclude_patterns'] = self.exclude_patterns
        if self.focus_modules:
            result['focus_modules'] = self.focus_modules
        if self.doc_type:
            result['doc_type'] = self.doc_type
        if self.custom_instructions:
            result['custom_instructions'] = self.custom_instructions
        return result
    
    @classmethod
    def from_dict(cls, data: dict) -> 'AgentInstructions':
        """Create AgentInstructions from dictionary."""
        return cls(
            include_patterns=data.get('include_patterns'),
            exclude_patterns=data.get('exclude_patterns'),
            focus_modules=data.get('focus_modules'),
            doc_type=data.get('doc_type'),
            custom_instructions=data.get('custom_instructions'),
        )
    
    def is_empty(self) -> bool:
        """Check if all fields are empty/None."""
        return not any([
            self.include_patterns,
            self.exclude_patterns,
            self.focus_modules,
            self.doc_type,
            self.custom_instructions,
        ])
    
    def get_prompt_addition(self) -> str:
        """Generate prompt additions based on instructions."""
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


@dataclass
class Configuration:
    """
    CodeWiki configuration data model.

    Attributes:
        base_url: LLM API base URL
        main_model: Primary model for documentation generation
        cluster_model: Model for module clustering
        fallback_model: Fallback model for documentation generation
        default_output: Default output directory
        provider: LLM provider type (openai-compatible, atlas-cloud, orcarouter, anthropic, bedrock, azure-openai)
        aws_region: AWS region for Bedrock provider
        api_version: Azure OpenAI API version
        azure_deployment: Azure OpenAI deployment name
        max_tokens: Maximum tokens for LLM response (default: 32768)
        max_token_per_module: Maximum tokens per module for clustering (default: 36369)
        max_token_per_leaf_module: Maximum tokens per leaf module (default: 16000)
        max_depth: Maximum depth for hierarchical decomposition (default: 2)
        use_gitignore: Apply Git ignore rules during repository analysis
        prompt_caching: Add prompt-cache breakpoints to agentic LLM calls (default: True)
        agent_instructions: Custom agent instructions for documentation generation
    """
    base_url: str
    main_model: str
    cluster_model: str
    fallback_model: str = "glm-4p5"
    default_output: str = "docs"
    provider: str = "openai-compatible"
    aws_region: str = "us-east-1"
    api_version: str = "2024-12-01-preview"
    azure_deployment: str = ""
    max_tokens: int = 32768
    max_token_per_module: int = 36369
    max_token_per_leaf_module: int = 16000
    max_depth: int = 2
    use_gitignore: bool = True
    prompt_caching: bool = True
    agent_instructions: AgentInstructions = field(default_factory=AgentInstructions)
    
    def validate(self):
        """
        Validate all configuration fields.

        Subscription-mode providers (claude-code, codex) authenticate via the
        underlying CLI's OAuth and do not require a base URL or fallback model.

        Raises:
            ConfigurationError: If validation fails
        """
        from codewiki.src.be.backend import is_caw_provider
        if is_caw_provider(self.provider):
            validate_model_name(self.main_model)
            return
        validate_url(self.base_url)
        validate_model_name(self.main_model)
        validate_model_name(self.cluster_model)
        validate_model_name(self.fallback_model)
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        result = {
            'base_url': self.base_url,
            'main_model': self.main_model,
            'cluster_model': self.cluster_model,
            'default_output': self.default_output,
            'provider': self.provider,
            'aws_region': self.aws_region,
            'api_version': self.api_version,
            'azure_deployment': self.azure_deployment,
            'max_tokens': self.max_tokens,
            'max_token_per_module': self.max_token_per_module,
            'max_token_per_leaf_module': self.max_token_per_leaf_module,
            'max_depth': self.max_depth,
            'use_gitignore': self.use_gitignore,
            'prompt_caching': self.prompt_caching,
            'fallback_model': self.fallback_model,
        }
        if self.agent_instructions and not self.agent_instructions.is_empty():
            result['agent_instructions'] = self.agent_instructions.to_dict()
        return result
    
    @classmethod
    def from_dict(cls, data: dict) -> 'Configuration':
        """
        Create Configuration from dictionary.
        
        Args:
            data: Configuration dictionary
            
        Returns:
            Configuration instance
        """
        agent_instructions = AgentInstructions()
        if 'agent_instructions' in data:
            agent_instructions = AgentInstructions.from_dict(data['agent_instructions'])
        
        return cls(
            base_url=data.get('base_url', ''),
            main_model=data.get('main_model', ''),
            cluster_model=data.get('cluster_model', ''),
            fallback_model=data.get('fallback_model', 'glm-4p5'),
            default_output=data.get('default_output', 'docs'),
            provider=data.get('provider', 'openai-compatible'),
            aws_region=data.get('aws_region', 'us-east-1'),
            api_version=data.get('api_version', '2024-12-01-preview'),
            azure_deployment=data.get('azure_deployment', ''),
            max_tokens=data.get('max_tokens', 32768),
            max_token_per_module=data.get('max_token_per_module', 36369),
            max_token_per_leaf_module=data.get('max_token_per_leaf_module', 16000),
            max_depth=data.get('max_depth', 2),
            use_gitignore=data.get('use_gitignore', True),
            prompt_caching=data.get('prompt_caching', True),
            agent_instructions=agent_instructions,
        )
    
    def is_complete(self) -> bool:
        """Check if all required fields are set.

        Subscription-mode providers (claude-code, codex) only require
        ``main_model``; ``base_url``, ``cluster_model`` and ``fallback_model``
        are unused.
        """
        from codewiki.src.be.backend import is_caw_provider
        if is_caw_provider(self.provider):
            return bool(self.main_model)
        return bool(
            self.base_url and
            self.main_model and
            self.cluster_model and
            self.fallback_model
        )
    
    def to_backend_config(self, repo_path: str, output_dir: str, api_key: str, runtime_instructions: AgentInstructions = None):
        """
        Convert CLI Configuration to Backend Config.
        
        This method bridges the gap between persistent user settings (CLI Configuration)
        and runtime job configuration (Backend Config).
        
        Args:
            repo_path: Path to the repository to document
            output_dir: Output directory for generated documentation
            api_key: LLM API key (from keyring)
            runtime_instructions: Runtime agent instructions (override persistent settings)
            
        Returns:
            Backend Config instance ready for documentation generation
        """
        from codewiki.src.config import Config
        
        # Merge runtime instructions with persistent settings
        # Runtime instructions take precedence
        final_instructions = self.agent_instructions
        if runtime_instructions and not runtime_instructions.is_empty():
            final_instructions = AgentInstructions(
                include_patterns=runtime_instructions.include_patterns or self.agent_instructions.include_patterns,
                exclude_patterns=runtime_instructions.exclude_patterns or self.agent_instructions.exclude_patterns,
                focus_modules=runtime_instructions.focus_modules or self.agent_instructions.focus_modules,
                doc_type=runtime_instructions.doc_type or self.agent_instructions.doc_type,
                custom_instructions=runtime_instructions.custom_instructions or self.agent_instructions.custom_instructions,
            )
        
        return Config.from_cli(
            repo_path=repo_path,
            output_dir=output_dir,
            llm_base_url=self.base_url,
            llm_api_key=api_key,
            main_model=self.main_model,
            cluster_model=self.cluster_model,
            fallback_model=self.fallback_model,
            provider=self.provider,
            aws_region=self.aws_region,
            api_version=self.api_version,
            azure_deployment=self.azure_deployment,
            max_tokens=self.max_tokens,
            max_token_per_module=self.max_token_per_module,
            max_token_per_leaf_module=self.max_token_per_leaf_module,
            max_depth=self.max_depth,
            agent_instructions=final_instructions.to_dict() if final_instructions else None,
            use_gitignore=self.use_gitignore,
            prompt_caching=self.prompt_caching,
        )
