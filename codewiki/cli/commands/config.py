"""
Configuration commands for CodeWiki CLI.
"""

import json
import os
import sys
import click
from typing import Optional, List

from codewiki.cli.config_manager import ConfigManager
from codewiki.cli.models.config import AgentInstructions
from codewiki.cli.utils.errors import (
    ConfigurationError, 
    handle_error, 
    EXIT_SUCCESS,
    EXIT_CONFIG_ERROR
)
from codewiki.cli.utils.validation import (
    validate_url,
    validate_api_key,
    validate_model_name,
    is_top_tier_model,
    mask_api_key
)


def parse_patterns(patterns_str: str) -> List[str]:
    """Parse comma-separated patterns into a list."""
    if not patterns_str:
        return []
    return [p.strip() for p in patterns_str.split(',') if p.strip()]


@click.group(name="config")
def config_group():
    """Manage CodeWiki configuration (API credentials and settings)."""
    pass


@config_group.command(name="set")
@click.option(
    "--api-key",
    type=str,
    help="LLM API key (stored securely in system keychain)"
)
@click.option(
    "--base-url",
    type=str,
    help="LLM API base URL (e.g., https://api.anthropic.com)"
)
@click.option(
    "--main-model",
    type=str,
    help="Primary model for documentation generation"
)
@click.option(
    "--cluster-model",
    type=str,
    help="Model for module clustering (recommend top-tier)"
)
@click.option(
    "--fallback-model",
    type=str,
    help="Fallback model for documentation generation"
)
@click.option(
    "--max-tokens",
    type=int,
    help="Maximum tokens for LLM response (default: 32768)"
)
@click.option(
    "--max-token-per-module",
    type=int,
    help="Maximum tokens per module for clustering (default: 36369)"
)
@click.option(
    "--max-token-per-leaf-module",
    type=int,
    help="Maximum tokens per leaf module (default: 16000)"
)
@click.option(
    "--max-depth",
    type=int,
    help="Maximum depth for hierarchical decomposition (default: 2)"
)
@click.option(
    "--provider",
    type=click.Choice(
        ['openai-compatible', 'atlas-cloud', 'anthropic', 'bedrock', 'azure-openai', 'claude-code', 'codex'],
        case_sensitive=False,
    ),
    help=(
        "LLM provider type (default: openai-compatible). "
        "Use 'atlas-cloud' for Atlas Cloud (base URL auto-set; reads ATLASCLOUD_API_KEY). "
        "Use 'claude-code' or 'codex' to run on a CLI subscription instead of an API key."
    ),
)
@click.option(
    "--aws-region",
    type=str,
    help="AWS region for Bedrock provider (default: us-east-1)"
)
@click.option(
    "--api-version",
    type=str,
    help="Azure OpenAI API version (default: 2024-12-01-preview)"
)
@click.option(
    "--azure-deployment",
    type=str,
    help="Azure OpenAI deployment name"
)
@click.option(
    "--use-gitignore/--no-gitignore",
    default=None,
    help="Apply Git ignore rules during repository analysis (default: enabled)",
)
@click.option(
    "--prompt-caching/--no-prompt-caching",
    default=None,
    help="Add prompt-cache breakpoints to agentic LLM calls; auto-falls back to "
         "normal calls if the provider rejects them (default: enabled)",
)
def config_set(
    api_key: Optional[str],
    base_url: Optional[str],
    main_model: Optional[str],
    cluster_model: Optional[str],
    fallback_model: Optional[str],
    max_tokens: Optional[int],
    max_token_per_module: Optional[int],
    max_token_per_leaf_module: Optional[int],
    max_depth: Optional[int],
    provider: Optional[str] = None,
    aws_region: Optional[str] = None,
    api_version: Optional[str] = None,
    azure_deployment: Optional[str] = None,
    use_gitignore: Optional[bool] = None,
    prompt_caching: Optional[bool] = None,
):
    """
    Set configuration values for CodeWiki.
    
    API keys are stored securely in your system keychain:
      • macOS: Keychain Access
      • Windows: Credential Manager  
      • Linux: Secret Service (GNOME Keyring, KWallet)
    
    Examples:

    \b
    # Set all configuration (API mode)
    $ codewiki config set --api-key sk-abc123 --base-url https://api.anthropic.com \\
        --main-model claude-sonnet-4 --cluster-model claude-sonnet-4 --fallback-model glm-4p5

    \b
    # Atlas Cloud (OpenAI-compatible) — base URL auto-set,
    # API key read from ATLASCLOUD_API_KEY if --api-key is omitted
    $ codewiki config set --provider atlas-cloud --main-model <atlas-model> --cluster-model <atlas-model>

    \b
    # Subscription mode (Claude Code) — no API key needed,
    # authenticate via 'claude login' on the host first
    $ codewiki config set --provider claude-code --main-model claude-sonnet-4-5

    \b
    # Subscription mode (Codex)
    $ codewiki config set --provider codex --main-model gpt-5.2-codex

    \b
    # Update only API key
    $ codewiki config set --api-key sk-new-key

    \b
    # Set max tokens for LLM response
    $ codewiki config set --max-tokens 16384

    \b
    # Set all max token settings
    $ codewiki config set --max-tokens 32768 --max-token-per-module 40000 --max-token-per-leaf-module 20000

    \b
    # Set max depth for hierarchical decomposition
    $ codewiki config set --max-depth 3

    \b
    # Persistently disable Git ignore filtering
    $ codewiki config set --no-gitignore
    """
    try:
        # Check if at least one option is provided
        if not any([api_key, base_url, main_model, cluster_model, fallback_model, max_tokens, max_token_per_module, max_token_per_leaf_module, max_depth, provider, aws_region, api_version, azure_deployment, use_gitignore is not None, prompt_caching is not None]):
            click.echo("No options provided. Use --help for usage information.")
            sys.exit(EXIT_CONFIG_ERROR)

        # Atlas Cloud convenience defaults: it's an OpenAI-compatible endpoint, so
        # auto-fill its base URL and pull the API key from ATLASCLOUD_API_KEY when the
        # user does not pass them explicitly.
        if provider and provider.lower() == "atlas-cloud":
            from codewiki.src.config import ATLAS_CLOUD_BASE_URL
            if not base_url:
                base_url = ATLAS_CLOUD_BASE_URL
            if not api_key:
                api_key = os.getenv("ATLASCLOUD_API_KEY")

        # Validate inputs before saving
        validated_data = {}
        
        if api_key:
            validated_data['api_key'] = validate_api_key(api_key)
        
        if base_url:
            validated_data['base_url'] = validate_url(base_url)
        
        if main_model:
            validated_data['main_model'] = validate_model_name(main_model)
        
        if cluster_model:
            validated_data['cluster_model'] = validate_model_name(cluster_model)
        
        if fallback_model:
            validated_data['fallback_model'] = validate_model_name(fallback_model)
        
        if max_tokens is not None:
            if max_tokens < 1:
                raise ConfigurationError("max_tokens must be a positive integer")
            validated_data['max_tokens'] = max_tokens
        
        if max_token_per_module is not None:
            if max_token_per_module < 1:
                raise ConfigurationError("max_token_per_module must be a positive integer")
            validated_data['max_token_per_module'] = max_token_per_module
        
        if max_token_per_leaf_module is not None:
            if max_token_per_leaf_module < 1:
                raise ConfigurationError("max_token_per_leaf_module must be a positive integer")
            validated_data['max_token_per_leaf_module'] = max_token_per_leaf_module
        
        if max_depth is not None:
            if max_depth < 1:
                raise ConfigurationError("max_depth must be a positive integer")
            validated_data['max_depth'] = max_depth

        if provider is not None:
            validated_data['provider'] = provider

        if aws_region is not None:
            validated_data['aws_region'] = aws_region

        if api_version is not None:
            validated_data['api_version'] = api_version

        if azure_deployment is not None:
            validated_data['azure_deployment'] = azure_deployment

        if use_gitignore is not None:
            validated_data['use_gitignore'] = use_gitignore

        if prompt_caching is not None:
            validated_data['prompt_caching'] = prompt_caching

        # Create config manager and save
        manager = ConfigManager()
        manager.load()  # Load existing config if present

        manager.save(
            api_key=validated_data.get('api_key'),
            base_url=validated_data.get('base_url'),
            main_model=validated_data.get('main_model'),
            cluster_model=validated_data.get('cluster_model'),
            fallback_model=validated_data.get('fallback_model'),
            max_tokens=validated_data.get('max_tokens'),
            max_token_per_module=validated_data.get('max_token_per_module'),
            max_token_per_leaf_module=validated_data.get('max_token_per_leaf_module'),
            max_depth=validated_data.get('max_depth'),
            provider=validated_data.get('provider'),
            aws_region=validated_data.get('aws_region'),
            api_version=validated_data.get('api_version'),
            azure_deployment=validated_data.get('azure_deployment'),
            use_gitignore=validated_data.get('use_gitignore'),
            prompt_caching=validated_data.get('prompt_caching'),
        )
        
        # Display success messages
        click.echo()
        if api_key:
            if manager.keyring_available:
                click.secho("✓ API key saved to system keychain", fg="green")
            else:
                click.secho(
                    "⚠️  System keychain unavailable. API key stored in encrypted file.",
                    fg="yellow"
                )
        
        if base_url:
            click.secho(f"✓ Base URL: {base_url}", fg="green")
        
        if main_model:
            click.secho(f"✓ Main model: {main_model}", fg="green")
        
        if cluster_model:
            click.secho(f"✓ Cluster model: {cluster_model}", fg="green")
            
            # Warn if not using top-tier model for clustering
            if not is_top_tier_model(cluster_model):
                click.secho(
                    "\n⚠️  Cluster model is not a top-tier LLM. "
                    "Documentation quality may be suboptimal.",
                    fg="yellow"
                )
                click.echo(
                    "   Recommended models: claude-opus, claude-sonnet-4, gpt-4, gpt-4-turbo"
                )
        
        if fallback_model:
            click.secho(f"✓ Fallback model: {fallback_model}", fg="green")
        
        if max_tokens:
            click.secho(f"✓ Max tokens: {max_tokens}", fg="green")
        
        if max_token_per_module:
            click.secho(f"✓ Max token per module: {max_token_per_module}", fg="green")
        
        if max_token_per_leaf_module:
            click.secho(f"✓ Max token per leaf module: {max_token_per_leaf_module}", fg="green")
        
        if max_depth:
            click.secho(f"✓ Max depth: {max_depth}", fg="green")

        if provider:
            click.secho(f"✓ Provider: {provider}", fg="green")

        if aws_region:
            click.secho(f"✓ AWS Region: {aws_region}", fg="green")

        if api_version:
            click.secho(f"✓ API Version: {api_version}", fg="green")

        if azure_deployment:
            click.secho(f"✓ Azure Deployment: {azure_deployment}", fg="green")

        if use_gitignore is not None:
            click.secho(f"✓ Use gitignore: {use_gitignore}", fg="green")

        if prompt_caching is not None:
            click.secho(f"✓ Prompt caching: {prompt_caching}", fg="green")

        click.echo("\n" + click.style("Configuration updated successfully.", fg="green", bold=True))
        
    except ConfigurationError as e:
        click.secho(f"\n✗ Configuration error: {e.message}", fg="red", err=True)
        sys.exit(e.exit_code)
    except Exception as e:
        sys.exit(handle_error(e))


@config_group.command(name="show")
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    help="Output in JSON format"
)
def config_show(output_json: bool):
    """
    Display current configuration.
    
    API keys are masked for security (showing only first and last 4 characters).
    
    Examples:
    
    \b
    # Display configuration
    $ codewiki config show
    
    \b
    # Display as JSON
    $ codewiki config show --json
    """
    try:
        manager = ConfigManager()
        
        if not manager.load():
            click.secho("\n✗ Configuration not found.", fg="red", err=True)
            click.echo("\nPlease run 'codewiki config set' to configure your API credentials:")
            click.echo("  codewiki config set --api-key <key> --base-url <url> \\")
            click.echo("    --main-model <model> --cluster-model <model> --fallback-model <model>")
            click.echo("\nFor more help: codewiki config set --help")
            sys.exit(EXIT_CONFIG_ERROR)
        
        config = manager.get_config()
        api_key = manager.get_api_key()
        
        if output_json:
            # JSON output
            output = {
                "api_key": mask_api_key(api_key) if api_key else "Not set",
                "api_key_storage": "keychain" if manager.keyring_available else "encrypted_file",
                "base_url": config.base_url if config else "",
                "main_model": config.main_model if config else "",
                "cluster_model": config.cluster_model if config else "",
                "fallback_model": config.fallback_model if config else "glm-4p5",
                "default_output": config.default_output if config else "docs",
                "max_tokens": config.max_tokens if config else 32768,
                "max_token_per_module": config.max_token_per_module if config else 36369,
                "max_token_per_leaf_module": config.max_token_per_leaf_module if config else 16000,
                "max_depth": config.max_depth if config else 2,
                "use_gitignore": config.use_gitignore if config else True,
                "prompt_caching": config.prompt_caching if config else True,
                "agent_instructions": config.agent_instructions.to_dict() if config and config.agent_instructions else {},
                "config_file": str(manager.config_file_path)
            }
            click.echo(json.dumps(output, indent=2))
        else:
            # Human-readable output
            click.echo()
            click.secho("CodeWiki Configuration", fg="blue", bold=True)
            click.echo("━" * 40)
            click.echo()
            
            from codewiki.src.be.backend import is_caw_provider
            caw_mode = bool(config) and is_caw_provider(config.provider)

            click.secho("Credentials", fg="cyan", bold=True)
            if caw_mode:
                cli_name = "claude" if config.provider == "claude-code" else "codex"
                click.secho(
                    f"  Subscription mode: authenticate via '{cli_name} login' (no API key needed)",
                    fg="cyan",
                )
            elif api_key:
                storage = "system keychain" if manager.keyring_available else "encrypted file"
                click.echo(f"  API Key:          {mask_api_key(api_key)} (in {storage})")
            else:
                click.secho("  API Key:          Not set", fg="yellow")

            click.echo()
            click.secho("API Settings", fg="cyan", bold=True)
            if config:
                click.echo(f"  Provider:         {config.provider}")
                click.echo(f"  Main Model:       {config.main_model or 'Not set'}")
                if not caw_mode:
                    click.echo(f"  Base URL:         {config.base_url or 'Not set'}")
                    click.echo(f"  Cluster Model:    {config.cluster_model or 'Not set'}")
                    click.echo(f"  Fallback Model:   {config.fallback_model or 'Not set'}")
                    if config.provider == "bedrock":
                        click.echo(f"  AWS Region:       {config.aws_region}")
                    elif config.provider == "azure-openai":
                        click.echo(f"  API Version:      {config.api_version}")
                        click.echo(f"  Azure Deployment: {config.azure_deployment or 'Not set'}")
            else:
                click.secho("  Not configured", fg="yellow")
            
            click.echo()
            click.secho("Output Settings", fg="cyan", bold=True)
            if config:
                click.echo(f"  Default Output:   {config.default_output}")
            
            click.echo()
            click.secho("Token Settings", fg="cyan", bold=True)
            if config:
                click.echo(f"  Max Tokens:              {config.max_tokens}")
                click.echo(f"  Max Token/Module:        {config.max_token_per_module}")
                click.echo(f"  Max Token/Leaf Module:   {config.max_token_per_leaf_module}")
                click.echo(f"  Prompt Caching:          {config.prompt_caching}")
            
            click.echo()
            click.secho("Decomposition Settings", fg="cyan", bold=True)
            if config:
                click.echo(f"  Max Depth:               {config.max_depth}")
                click.echo(f"  Use Gitignore:           {config.use_gitignore}")
            
            click.echo()
            click.secho("Agent Instructions", fg="cyan", bold=True)
            if config and config.agent_instructions and not config.agent_instructions.is_empty():
                agent = config.agent_instructions
                if agent.include_patterns:
                    click.echo(f"  Include patterns:   {', '.join(agent.include_patterns)}")
                if agent.exclude_patterns:
                    click.echo(f"  Exclude patterns:   {', '.join(agent.exclude_patterns)}")
                if agent.focus_modules:
                    click.echo(f"  Focus modules:      {', '.join(agent.focus_modules)}")
                if agent.doc_type:
                    click.echo(f"  Doc type:           {agent.doc_type}")
                if agent.custom_instructions:
                    click.echo(f"  Custom instructions: {agent.custom_instructions[:50]}...")
            else:
                click.secho("  Using defaults (no custom settings)", fg="yellow")
            
            click.echo()
            click.echo(f"Configuration file: {manager.config_file_path}")
            click.echo()
        
    except Exception as e:
        sys.exit(handle_error(e))


@config_group.command(name="validate")
@click.option(
    "--quick",
    is_flag=True,
    help="Skip API connectivity test"
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Show detailed validation steps"
)
def config_validate(quick: bool, verbose: bool):
    """
    Validate configuration and test LLM API connectivity.
    
    Checks:
      • Configuration file exists and is valid
      • API key is present
      • API settings are correctly formatted
      • (Optional) API connectivity test
    
    Examples:
    
    \b
    # Full validation with API test
    $ codewiki config validate
    
    \b
    # Quick validation (config only)
    $ codewiki config validate --quick
    
    \b
    # Verbose output
    $ codewiki config validate --verbose
    """
    try:
        click.echo()
        click.secho("Validating configuration...", fg="blue", bold=True)
        click.echo()
        
        manager = ConfigManager()
        
        # Step 1: Check config file
        if verbose:
            click.echo("[1/5] Checking configuration file...")
            click.echo(f"      Path: {manager.config_file_path}")
        
        if not manager.load():
            click.secho("✗ Configuration file not found", fg="red")
            click.echo()
            click.echo("Error: Configuration is incomplete. Run 'codewiki config set --help' for setup instructions.")
            sys.exit(EXIT_CONFIG_ERROR)
        
        if verbose:
            click.secho("      ✓ File exists", fg="green")
            click.secho("      ✓ Valid JSON format", fg="green")
        else:
            click.secho("✓ Configuration file exists", fg="green")
        
        # Load config early so we know the provider for the rest of the checks.
        config = manager.get_config()
        from codewiki.src.be.backend import is_caw_provider
        caw_mode = bool(config) and is_caw_provider(config.provider)

        # Step 2: Check API key (skipped for subscription providers)
        if verbose:
            click.echo()
            click.echo("[2/5] Checking API key...")

        if caw_mode:
            if verbose:
                click.secho("      ✓ API key not required (subscription mode)", fg="green")
            else:
                click.secho("✓ API key not required (subscription mode)", fg="green")
        else:
            if verbose:
                storage = "system keychain" if manager.keyring_available else "encrypted file"
                click.echo(f"      Storage: {storage}")

            api_key = manager.get_api_key()
            if not api_key:
                click.secho("✗ API key missing", fg="red")
                click.echo()
                click.echo("Error: API key not set. Run 'codewiki config set --api-key <key>'")
                sys.exit(EXIT_CONFIG_ERROR)

            if verbose:
                click.secho(f"      ✓ API key retrieved", fg="green")
                click.secho(f"      ✓ Length: {len(api_key)} characters", fg="green")
            else:
                click.secho("✓ API key present (stored in keychain)", fg="green")

        # Step 3: Check base URL (skipped for subscription providers)
        if verbose:
            click.echo()
            click.echo("[3/5] Checking base URL...")

        if caw_mode:
            if verbose:
                click.secho("      ✓ Base URL not required (subscription mode)", fg="green")
            else:
                click.secho("✓ Base URL not required (subscription mode)", fg="green")
        else:
            if verbose:
                click.echo(f"      URL: {config.base_url}")

            if not config.base_url:
                click.secho("✗ Base URL not set", fg="red")
                sys.exit(EXIT_CONFIG_ERROR)

            try:
                validate_url(config.base_url)
                if verbose:
                    click.secho("      ✓ Valid HTTPS URL", fg="green")
                else:
                    click.secho(f"✓ Base URL valid: {config.base_url}", fg="green")
            except ConfigurationError as e:
                click.secho(f"✗ Invalid base URL: {e.message}", fg="red")
                sys.exit(EXIT_CONFIG_ERROR)
        
        # Step 4: Check models
        if verbose:
            click.echo()
            click.echo("[4/5] Checking model configuration...")
            click.echo(f"      Main model: {config.main_model}")
            if not caw_mode:
                click.echo(f"      Cluster model: {config.cluster_model}")
                click.echo(f"      Fallback model: {config.fallback_model}")

        if caw_mode:
            if not config.main_model:
                click.secho("✗ Main model not configured", fg="red")
                sys.exit(EXIT_CONFIG_ERROR)
            if verbose:
                click.secho("      ✓ Main model configured", fg="green")
            else:
                click.secho(f"✓ Main model configured: {config.main_model}", fg="green")
        else:
            if not config.main_model or not config.cluster_model or not config.fallback_model:
                click.secho("✗ Models not configured", fg="red")
                sys.exit(EXIT_CONFIG_ERROR)

            if verbose:
                click.secho("      ✓ Models configured", fg="green")
            else:
                click.secho(f"✓ Main model configured: {config.main_model}", fg="green")
                click.secho(f"✓ Cluster model configured: {config.cluster_model}", fg="green")
                click.secho(f"✓ Fallback model configured: {config.fallback_model}", fg="green")

            # Warn about non-top-tier cluster model
            if not is_top_tier_model(config.cluster_model):
                click.secho(
                    "⚠️  Cluster model is not top-tier. Consider using claude-sonnet-4 or gpt-4.",
                    fg="yellow"
                )

        # Step 5: API connectivity test (unless --quick)
        if caw_mode:
            if verbose:
                click.echo()
                click.echo("[5/5] Checking CLI availability...")

            import shutil
            cli_name = "claude" if config.provider == "claude-code" else "codex"
            cli_path = shutil.which(cli_name)
            if not cli_path:
                click.secho(f"✗ {cli_name} CLI not found in PATH", fg="red")
                click.echo(
                    f"\nInstall the {cli_name} CLI and run '{cli_name} login' "
                    f"to authenticate, then re-run this command."
                )
                sys.exit(EXIT_CONFIG_ERROR)

            if verbose:
                click.secho(f"      ✓ {cli_name} CLI found at {cli_path}", fg="green")
                click.secho(
                    f"      ↳ Ensure '{cli_name} login' has been run on this host.",
                    fg="cyan",
                )
            else:
                click.secho(f"✓ {cli_name} CLI available (run '{cli_name} login' if not yet authenticated)", fg="green")
        elif not quick:
            if verbose:
                click.echo()
                click.echo("[5/5] Testing API connectivity...")
                click.echo(f"      URL: {config.base_url}")

            try:
                base_url_lower = (config.base_url or "").lower()
                provider = getattr(config, 'provider', 'openai-compatible')
                if provider == "azure-openai" or ".openai.azure.com" in base_url_lower:
                    # Use Azure OpenAI SDK
                    from openai import AzureOpenAI
                    client = AzureOpenAI(
                        api_key=api_key,
                        api_version=config.api_version,
                        azure_endpoint=config.base_url,
                    )
                    client.models.list()
                elif "api.anthropic.com" in base_url_lower:
                    # Use Anthropic SDK for native Anthropic endpoints
                    import anthropic
                    client = anthropic.Anthropic(api_key=api_key)
                    client.models.list(limit=1)
                else:
                    # Use OpenAI SDK for OpenAI-compatible endpoints
                    from openai import OpenAI
                    client = OpenAI(api_key=api_key, base_url=config.base_url)
                    client.models.list()

                if verbose:
                    click.secho("      ✓ API responded successfully", fg="green")
                else:
                    click.secho("✓ API connectivity test successful", fg="green")
            except Exception as e:
                click.secho("✗ API connectivity test failed", fg="red")
                if verbose:
                    click.echo(f"      Error: {e}")
                sys.exit(EXIT_CONFIG_ERROR)
        
        # Success
        click.echo()
        click.secho("✓ Configuration is valid!", fg="green", bold=True)
        click.echo()
        
    except ConfigurationError as e:
        click.secho(f"\n✗ Configuration error: {e.message}", fg="red", err=True)
        sys.exit(e.exit_code)
    except Exception as e:
        sys.exit(handle_error(e, verbose=verbose))


@config_group.command(name="agent")
@click.option(
    "--include",
    "-i",
    type=str,
    default=None,
    help="Comma-separated file patterns to include (e.g., '*.cs,*.py')",
)
@click.option(
    "--exclude",
    "-e",
    type=str,
    default=None,
    help="Comma-separated patterns to exclude (e.g., '*Tests*,*Specs*')",
)
@click.option(
    "--focus",
    "-f",
    type=str,
    default=None,
    help="Comma-separated modules/paths to focus on (e.g., 'src/core,src/api')",
)
@click.option(
    "--doc-type",
    "-t",
    type=click.Choice(['api', 'architecture', 'user-guide', 'developer'], case_sensitive=False),
    default=None,
    help="Default type of documentation to generate",
)
@click.option(
    "--instructions",
    type=str,
    default=None,
    help="Custom instructions for the documentation agent",
)
@click.option(
    "--clear",
    is_flag=True,
    help="Clear all agent instructions",
)
def config_agent(
    include: Optional[str],
    exclude: Optional[str],
    focus: Optional[str],
    doc_type: Optional[str],
    instructions: Optional[str],
    clear: bool
):
    """
    Configure default agent instructions for documentation generation.
    
    These settings are used as defaults when running 'codewiki generate'.
    Runtime options (--include, --exclude, etc.) override these defaults.
    
    Examples:
    
    \b
    # Set include patterns for C# projects
    $ codewiki config agent --include "*.cs"
    
    \b
    # Exclude test projects
    $ codewiki config agent --exclude "*Tests*,*Specs*,test_*"
    
    \b
    # Focus on specific modules
    $ codewiki config agent --focus "src/core,src/api"
    
    \b
    # Set default doc type
    $ codewiki config agent --doc-type architecture
    
    \b
    # Add custom instructions
    $ codewiki config agent --instructions "Focus on public APIs and include usage examples"
    
    \b
    # Clear all agent instructions
    $ codewiki config agent --clear
    """
    try:
        manager = ConfigManager()
        
        if not manager.load():
            click.secho("\n✗ Configuration not found.", fg="red", err=True)
            click.echo("\nPlease run 'codewiki config set' first to configure your API credentials.")
            sys.exit(EXIT_CONFIG_ERROR)
        
        config = manager.get_config()
        
        if clear:
            # Clear all agent instructions
            config.agent_instructions = AgentInstructions()
            manager.save()
            click.echo()
            click.secho("✓ Agent instructions cleared", fg="green")
            click.echo()
            return
        
        # Check if at least one option is provided
        if not any([include, exclude, focus, doc_type, instructions]):
            # Display current settings
            click.echo()
            click.secho("Agent Instructions", fg="blue", bold=True)
            click.echo("━" * 40)
            click.echo()
            
            agent = config.agent_instructions
            if agent and not agent.is_empty():
                if agent.include_patterns:
                    click.echo(f"  Include patterns:   {', '.join(agent.include_patterns)}")
                if agent.exclude_patterns:
                    click.echo(f"  Exclude patterns:   {', '.join(agent.exclude_patterns)}")
                if agent.focus_modules:
                    click.echo(f"  Focus modules:      {', '.join(agent.focus_modules)}")
                if agent.doc_type:
                    click.echo(f"  Doc type:           {agent.doc_type}")
                if agent.custom_instructions:
                    click.echo(f"  Custom instructions: {agent.custom_instructions}")
            else:
                click.secho("  No agent instructions configured (using defaults)", fg="yellow")
            
            click.echo()
            click.echo("Use 'codewiki config agent --help' for usage information.")
            click.echo()
            return
        
        # Update agent instructions
        current = config.agent_instructions or AgentInstructions()
        
        if include is not None:
            current.include_patterns = parse_patterns(include) if include else None
        if exclude is not None:
            current.exclude_patterns = parse_patterns(exclude) if exclude else None
        if focus is not None:
            current.focus_modules = parse_patterns(focus) if focus else None
        if doc_type is not None:
            current.doc_type = doc_type if doc_type else None
        if instructions is not None:
            current.custom_instructions = instructions if instructions else None
        
        config.agent_instructions = current
        manager.save()
        
        # Display success messages
        click.echo()
        if include:
            click.secho(f"✓ Include patterns: {parse_patterns(include)}", fg="green")
        if exclude:
            click.secho(f"✓ Exclude patterns: {parse_patterns(exclude)}", fg="green")
        if focus:
            click.secho(f"✓ Focus modules: {parse_patterns(focus)}", fg="green")
        if doc_type:
            click.secho(f"✓ Doc type: {doc_type}", fg="green")
        if instructions:
            click.secho(f"✓ Custom instructions set", fg="green")
        
        click.echo("\n" + click.style("Agent instructions updated successfully.", fg="green", bold=True))
        click.echo()
        
    except ConfigurationError as e:
        click.secho(f"\n✗ Configuration error: {e.message}", fg="red", err=True)
        sys.exit(e.exit_code)
    except Exception as e:
        sys.exit(handle_error(e))
