"""
Configuration data models for CodeWiki CLI.

This module contains the Configuration class which represents persistent
user settings stored in ~/.codewiki/config.json. These settings are converted
to the backend Config class when running documentation generation.
"""

from dataclasses import dataclass, asdict
from typing import Optional
from pathlib import Path

from codewiki.cli.utils.validation import (
    validate_url,
    validate_api_key,
    validate_model_name,
)


@dataclass
class Configuration:
    """
    CodeWiki configuration data model.
    
    Attributes:
        base_url: LLM API base URL
        main_model: Primary model for documentation generation
        cluster_model: Model for module clustering
        default_output: Default output directory
    """
    base_url: str
    main_model: str
    cluster_model: str
    default_output: str = "docs"
    
    def validate(self):
        """
        Validate all configuration fields.
        
        Raises:
            ConfigurationError: If validation fails
        """
        validate_url(self.base_url)
        validate_model_name(self.main_model)
        validate_model_name(self.cluster_model)
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> 'Configuration':
        """
        Create Configuration from dictionary.
        
        Args:
            data: Configuration dictionary
            
        Returns:
            Configuration instance
        """
        return cls(
            base_url=data.get('base_url', ''),
            main_model=data.get('main_model', ''),
            cluster_model=data.get('cluster_model', ''),
            default_output=data.get('default_output', 'docs'),
        )
    
    def is_complete(self) -> bool:
        """Check if all required fields are set."""
        return bool(
            self.base_url and 
            self.main_model and 
            self.cluster_model
        )
    
    def to_backend_config(self, repo_path: str, output_dir: str, api_key: str):
        """
        Convert CLI Configuration to Backend Config.
        
        This method bridges the gap between persistent user settings (CLI Configuration)
        and runtime job configuration (Backend Config).
        
        Args:
            repo_path: Path to the repository to document
            output_dir: Output directory for generated documentation
            api_key: LLM API key (from keyring)
            
        Returns:
            Backend Config instance ready for documentation generation
        """
        from codewiki.src.config import Config
        
        return Config.from_cli(
            repo_path=repo_path,
            output_dir=output_dir,
            llm_base_url=self.base_url,
            llm_api_key=api_key,
            main_model=self.main_model,
            cluster_model=self.cluster_model
        )

