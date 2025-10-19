"""
Configuration commands for CodeWiki CLI.
"""

import json
import sys
import click
from typing import Optional

from codewiki.cli.config_manager import ConfigManager
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
def config_set(
    api_key: Optional[str],
    base_url: Optional[str],
    main_model: Optional[str],
    cluster_model: Optional[str]
):
    """
    Set configuration values for CodeWiki.
    
    API keys are stored securely in your system keychain:
      • macOS: Keychain Access
      • Windows: Credential Manager  
      • Linux: Secret Service (GNOME Keyring, KWallet)
    
    Examples:
    
    \b
    # Set all configuration
    $ codewiki config set --api-key sk-abc123 --base-url https://api.anthropic.com \\
        --main-model claude-sonnet-4 --cluster-model claude-sonnet-4
    
    \b
    # Update only API key
    $ codewiki config set --api-key sk-new-key
    """
    try:
        # Check if at least one option is provided
        if not any([api_key, base_url, main_model, cluster_model]):
            click.echo("No options provided. Use --help for usage information.")
            sys.exit(EXIT_CONFIG_ERROR)
        
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
        
        # Create config manager and save
        manager = ConfigManager()
        manager.load()  # Load existing config if present
        
        manager.save(
            api_key=validated_data.get('api_key'),
            base_url=validated_data.get('base_url'),
            main_model=validated_data.get('main_model'),
            cluster_model=validated_data.get('cluster_model')
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
            click.echo("    --main-model <model> --cluster-model <model>")
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
                "default_output": config.default_output if config else "docs",
                "config_file": str(manager.config_file_path)
            }
            click.echo(json.dumps(output, indent=2))
        else:
            # Human-readable output
            click.echo()
            click.secho("CodeWiki Configuration", fg="blue", bold=True)
            click.echo("━" * 40)
            click.echo()
            
            click.secho("Credentials", fg="cyan", bold=True)
            if api_key:
                storage = "system keychain" if manager.keyring_available else "encrypted file"
                click.echo(f"  API Key:          {mask_api_key(api_key)} (in {storage})")
            else:
                click.secho("  API Key:          Not set", fg="yellow")
            
            click.echo()
            click.secho("API Settings", fg="cyan", bold=True)
            if config:
                click.echo(f"  Base URL:         {config.base_url or 'Not set'}")
                click.echo(f"  Main Model:       {config.main_model or 'Not set'}")
                click.echo(f"  Cluster Model:    {config.cluster_model or 'Not set'}")
            else:
                click.secho("  Not configured", fg="yellow")
            
            click.echo()
            click.secho("Output Settings", fg="cyan", bold=True)
            if config:
                click.echo(f"  Default Output:   {config.default_output}")
            
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
        
        # Step 2: Check API key
        if verbose:
            click.echo()
            click.echo("[2/5] Checking API key...")
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
        
        # Step 3: Check base URL
        config = manager.get_config()
        if verbose:
            click.echo()
            click.echo("[3/5] Checking base URL...")
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
            click.echo(f"      Cluster model: {config.cluster_model}")
        
        if not config.main_model or not config.cluster_model:
            click.secho("✗ Models not configured", fg="red")
            sys.exit(EXIT_CONFIG_ERROR)
        
        if verbose:
            click.secho("      ✓ Models configured", fg="green")
        else:
            click.secho(f"✓ Main model configured: {config.main_model}", fg="green")
            click.secho(f"✓ Cluster model configured: {config.cluster_model}", fg="green")
        
        # Warn about non-top-tier cluster model
        if not is_top_tier_model(config.cluster_model):
            click.secho(
                "⚠️  Cluster model is not top-tier. Consider using claude-sonnet-4 or gpt-4.",
                fg="yellow"
            )
        
        # Step 5: API connectivity test (unless --quick)
        if not quick:
            try:
                from openai import OpenAI
                client = OpenAI(api_key=api_key, base_url=config.base_url)
                response = client.models.list()
                click.secho("✓ API connectivity test successful", fg="green")
            except Exception as e:
                click.secho("✗ API connectivity test failed", fg="red")
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

