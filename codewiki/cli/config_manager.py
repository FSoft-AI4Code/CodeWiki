"""
Configuration manager with keyring integration for secure credential storage.

Supports fallback to file-based storage when system keyring is unavailable
(e.g. headless containers, RHEL without Secret Service). Set the environment
variable CODEWIKI_NO_KEYRING=1 to force file-based storage.
"""

import json
import os
import logging
from pathlib import Path
from typing import Optional
import keyring
from keyring.errors import KeyringError

from codewiki.cli.models.config import Configuration
from codewiki.cli.utils.errors import ConfigurationError, FileSystemError
from codewiki.cli.utils.fs import ensure_directory, safe_write, safe_read

logger = logging.getLogger(__name__)

# Keyring configuration
KEYRING_SERVICE = "codewiki"
KEYRING_API_KEY_ACCOUNT = "api_key"

# Configuration file location
CONFIG_DIR = Path.home() / ".codewiki"
CONFIG_FILE = CONFIG_DIR / "config.json"
CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"
CONFIG_VERSION = "1.0"


class ConfigManager:
    """
    Manages CodeWiki configuration with secure keyring storage for API keys.

    Storage:
        - API key: System keychain via keyring (macOS Keychain, Windows Credential Manager,
                  Linux Secret Service)
        - Fallback: ~/.codewiki/credentials.json when keyring is unavailable
        - Other settings: ~/.codewiki/config.json

    Set CODEWIKI_NO_KEYRING=1 to skip keyring and use file-based storage.
    """

    def __init__(self):
        """Initialize the configuration manager."""
        self._api_key: Optional[str] = None
        self._config: Optional[Configuration] = None
        self._force_no_keyring = os.environ.get("CODEWIKI_NO_KEYRING", "").strip() in ("1", "true", "yes")
        self._keyring_available = self._check_keyring_available()

    def _check_keyring_available(self) -> bool:
        """Check if system keyring is available."""
        if self._force_no_keyring:
            logger.debug("Keyring disabled via CODEWIKI_NO_KEYRING")
            return False
        try:
            # Try to get/set a test value
            keyring.get_password(KEYRING_SERVICE, "__test__")
            return True
        except (KeyringError, Exception):
            return False

    def _load_api_key_from_file(self) -> Optional[str]:
        """Load API key from fallback credentials file."""
        if not CREDENTIALS_FILE.exists():
            return None
        try:
            content = safe_read(CREDENTIALS_FILE)
            data = json.loads(content)
            return data.get("api_key")
        except (json.JSONDecodeError, FileSystemError):
            return None

    def _save_api_key_to_file(self, api_key: str):
        """Save API key to fallback credentials file (plaintext)."""
        ensure_directory(CONFIG_DIR)
        data = {"api_key": api_key}
        safe_write(CREDENTIALS_FILE, json.dumps(data, indent=2))
        # Restrict file permissions (owner read/write only)
        try:
            CREDENTIALS_FILE.chmod(0o600)
        except OSError:
            pass
    
    def load(self) -> bool:
        """
        Load configuration from file and keyring.
        
        Returns:
            True if configuration exists, False otherwise
        """
        # Load from JSON file
        if not CONFIG_FILE.exists():
            return False
        
        try:
            content = safe_read(CONFIG_FILE)
            data = json.loads(content)
            
            # Validate version
            if data.get('version') != CONFIG_VERSION:
                # Could implement migration here
                pass
            
            self._config = Configuration.from_dict(data)
            
            # Load API key from keyring, falling back to file
            if self._keyring_available:
                try:
                    self._api_key = keyring.get_password(KEYRING_SERVICE, KEYRING_API_KEY_ACCOUNT)
                except (KeyringError, Exception):
                    pass
            if self._api_key is None:
                self._api_key = self._load_api_key_from_file()
            
            return True
        except (json.JSONDecodeError, FileSystemError) as e:
            raise ConfigurationError(f"Failed to load configuration: {e}")
    
    def save(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        main_model: Optional[str] = None,
        cluster_model: Optional[str] = None,
        fallback_model: Optional[str] = None,
        default_output: Optional[str] = None,
        max_tokens: Optional[int] = None,
        max_token_per_module: Optional[int] = None,
        max_token_per_leaf_module: Optional[int] = None,
        max_depth: Optional[int] = None,
        provider: Optional[str] = None,
        aws_region: Optional[str] = None,
        api_version: Optional[str] = None,
        azure_deployment: Optional[str] = None,
        use_gitignore: Optional[bool] = None,
    ):
        """
        Save configuration to file and keyring.

        Args:
            api_key: API key (stored in keyring)
            base_url: LLM API base URL
            main_model: Primary model
            cluster_model: Clustering model
            fallback_model: Fallback model
            default_output: Default output directory
            max_tokens: Maximum tokens for LLM response
            max_token_per_module: Maximum tokens per module for clustering
            max_token_per_leaf_module: Maximum tokens per leaf module
            max_depth: Maximum depth for hierarchical decomposition
            provider: LLM provider type (openai-compatible, anthropic, bedrock, azure-openai)
            aws_region: AWS region for Bedrock provider
            api_version: Azure OpenAI API version
            azure_deployment: Azure OpenAI deployment name
            use_gitignore: Apply Git ignore rules during repository analysis
        """
        # Ensure config directory exists
        try:
            ensure_directory(CONFIG_DIR)
        except FileSystemError as e:
            raise ConfigurationError(f"Cannot create config directory: {e}")
        
        # Load existing config or create new
        if self._config is None:
            if CONFIG_FILE.exists():
                self.load()
            else:
                from codewiki.cli.models.config import AgentInstructions
                self._config = Configuration(
                    base_url="",
                    main_model="",
                    cluster_model="",
                    fallback_model="glm-4p5",
                    default_output="docs",
                    agent_instructions=AgentInstructions()
                )
        
        # Update fields if provided
        if base_url is not None:
            self._config.base_url = base_url
        if main_model is not None:
            self._config.main_model = main_model
        if cluster_model is not None:
            self._config.cluster_model = cluster_model
        if fallback_model is not None:
            self._config.fallback_model = fallback_model
        if default_output is not None:
            self._config.default_output = default_output
        if max_tokens is not None:
            self._config.max_tokens = max_tokens
        if max_token_per_module is not None:
            self._config.max_token_per_module = max_token_per_module
        if max_token_per_leaf_module is not None:
            self._config.max_token_per_leaf_module = max_token_per_leaf_module
        if max_depth is not None:
            self._config.max_depth = max_depth
        if provider is not None:
            self._config.provider = provider
        if aws_region is not None:
            self._config.aws_region = aws_region
        if api_version is not None:
            self._config.api_version = api_version
        if azure_deployment is not None:
            self._config.azure_deployment = azure_deployment
        if use_gitignore is not None:
            self._config.use_gitignore = use_gitignore

        # Validate configuration whenever the minimum required fields are set.
        # Caw providers only need main_model; API providers need base_url +
        # cluster_model on top of that.  The validate() method itself routes
        # by provider, so we only gate on whether enough is set to validate.
        from codewiki.src.be.backend import is_caw_provider
        if is_caw_provider(self._config.provider):
            if self._config.main_model:
                self._config.validate()
        elif self._config.base_url and self._config.main_model and self._config.cluster_model:
            self._config.validate()
        
        # Save API key to keyring, falling back to file
        if api_key is not None:
            self._api_key = api_key
            if self._keyring_available:
                try:
                    keyring.set_password(KEYRING_SERVICE, KEYRING_API_KEY_ACCOUNT, api_key)
                except (KeyringError, Exception):
                    # Keyring failed at runtime — fall back to file
                    self._keyring_available = False
                    self._save_api_key_to_file(api_key)
                    logger.warning(
                        "System keychain unavailable. API key stored in %s "
                        "(plaintext). Set CODEWIKI_NO_KEYRING=1 to suppress this warning.",
                        CREDENTIALS_FILE
                    )
            else:
                self._save_api_key_to_file(api_key)
        
        # Save non-sensitive config to JSON
        config_data = {
            "version": CONFIG_VERSION,
            **self._config.to_dict()
        }
        
        try:
            safe_write(CONFIG_FILE, json.dumps(config_data, indent=2))
        except FileSystemError as e:
            raise ConfigurationError(f"Failed to save configuration: {e}")
    
    def get_api_key(self) -> Optional[str]:
        """
        Get API key from keyring or fallback file.

        Returns:
            API key or None if not set
        """
        if self._api_key is None:
            if self._keyring_available:
                try:
                    self._api_key = keyring.get_password(KEYRING_SERVICE, KEYRING_API_KEY_ACCOUNT)
                except (KeyringError, Exception):
                    pass
            if self._api_key is None:
                self._api_key = self._load_api_key_from_file()

        return self._api_key
    
    def get_config(self) -> Optional[Configuration]:
        """
        Get current configuration.
        
        Returns:
            Configuration object or None if not loaded
        """
        return self._config
    
    def is_configured(self) -> bool:
        """
        Check if configuration is complete and valid.

        Subscription-mode providers (claude-code, codex) do not require an
        API key — they authenticate via the underlying CLI's OAuth.

        Returns:
            True if configured, False otherwise
        """
        if self._config is None:
            return False

        from codewiki.src.be.backend import is_caw_provider
        if not is_caw_provider(self._config.provider):
            # Check if API key is set
            if self.get_api_key() is None:
                return False

        # Check if config is complete
        return self._config.is_complete()
    
    def delete_api_key(self):
        """Delete API key from keyring and fallback file."""
        if self._keyring_available:
            try:
                keyring.delete_password(KEYRING_SERVICE, KEYRING_API_KEY_ACCOUNT)
            except (KeyringError, Exception):
                pass
        # Also remove fallback credentials file
        if CREDENTIALS_FILE.exists():
            try:
                CREDENTIALS_FILE.unlink()
            except OSError:
                pass
        self._api_key = None
    
    def clear(self):
        """Clear all configuration (file and keyring)."""
        # Delete API key from keyring
        self.delete_api_key()
        
        # Delete config file
        if CONFIG_FILE.exists():
            CONFIG_FILE.unlink()
        
        self._config = None
        self._api_key = None
    
    @property
    def keyring_available(self) -> bool:
        """Check if keyring is available."""
        return self._keyring_available
    
    @property
    def config_file_path(self) -> Path:
        """Get configuration file path."""
        return CONFIG_FILE
