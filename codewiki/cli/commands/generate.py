"""
Generate command for documentation generation.
"""

import sys
import logging
from pathlib import Path
from typing import Optional
import click
import time

from codewiki.cli.config_manager import ConfigManager
from codewiki.cli.utils.errors import (
    ConfigurationError,
    RepositoryError,
    APIError,
    handle_error,
    EXIT_SUCCESS,
)
from codewiki.cli.utils.repo_validator import (
    validate_repository,
    check_writable_output,
    is_git_repository,
    get_git_commit_hash,
    get_git_branch,
)
from codewiki.cli.utils.logging import create_logger
from codewiki.cli.adapters.doc_generator import CLIDocumentationGenerator
from codewiki.cli.utils.instructions import display_post_generation_instructions
from codewiki.cli.models.job import GenerationOptions


@click.command(name="generate")
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    default="docs",
    help="Output directory for generated documentation (default: ./docs)",
)
@click.option(
    "--create-branch",
    is_flag=True,
    help="Create a new git branch for documentation changes",
)
@click.option(
    "--github-pages",
    is_flag=True,
    help="Generate index.html for GitHub Pages deployment",
)
@click.option(
    "--no-cache",
    is_flag=True,
    help="Force full regeneration, ignoring cache",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Show detailed progress and debug information",
)
@click.pass_context
def generate_command(
    ctx,
    output: str,
    create_branch: bool,
    github_pages: bool,
    no_cache: bool,
    verbose: bool
):
    """
    Generate comprehensive documentation for a code repository.
    
    Analyzes the current repository and generates documentation using LLM-powered
    analysis. Documentation is output to ./docs/ by default.
    
    Examples:
    
    \b
    # Basic generation
    $ codewiki generate
    
    \b
    # With git branch creation and GitHub Pages
    $ codewiki generate --create-branch --github-pages
    
    \b
    # Force full regeneration
    $ codewiki generate --no-cache
    """
    logger = create_logger(verbose=verbose)
    start_time = time.time()
    
    # Suppress httpx INFO logs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    
    try:
        # Pre-generation checks
        logger.step("Validating configuration...", 1, 4)
        
        # Load configuration
        config_manager = ConfigManager()
        if not config_manager.load():
            raise ConfigurationError(
                "Configuration not found or invalid.\n\n"
                "Please run 'codewiki config set' to configure your LLM API credentials:\n"
                "  codewiki config set --api-key <your-api-key> --base-url <api-url> \\\n"
                "    --main-model <model> --cluster-model <model>\n\n"
                "For more help: codewiki config --help"
            )
        
        if not config_manager.is_configured():
            raise ConfigurationError(
                "Configuration is incomplete. Please run 'codewiki config validate'"
            )
        
        config = config_manager.get_config()
        api_key = config_manager.get_api_key()
        
        logger.success("Configuration valid")
        
        # Validate repository
        logger.step("Validating repository...", 2, 4)
        
        repo_path = Path.cwd()
        repo_path, languages = validate_repository(repo_path)
        
        logger.success(f"Repository valid: {repo_path.name}")
        if verbose:
            logger.debug(f"Detected languages: {', '.join(f'{lang} ({count} files)' for lang, count in languages)}")
        
        # Check git repository
        if not is_git_repository(repo_path):
            if create_branch:
                raise RepositoryError(
                    "Not a git repository.\n\n"
                    "The --create-branch flag requires a git repository.\n\n"
                    "To initialize a git repository: git init"
                )
            else:
                logger.warning("Not a git repository. Git features unavailable.")
        
        # Validate output directory
        output_dir = Path(output).expanduser().resolve()
        check_writable_output(output_dir.parent)
        
        logger.success(f"Output directory: {output_dir}")
        
        # Check for existing documentation
        if output_dir.exists() and list(output_dir.glob("*.md")):
            if not click.confirm(
                f"\n{output_dir} already contains documentation. Overwrite?",
                default=True
            ):
                logger.info("Generation cancelled by user.")
                sys.exit(EXIT_SUCCESS)
        
        # Git branch creation (if requested)
        branch_name = None
        if create_branch:
            logger.step("Creating git branch...", 3, 4)
            
            from codewiki.cli.git_manager import GitManager
            
            git_manager = GitManager(repo_path)
            
            # Check clean working directory
            is_clean, status_msg = git_manager.check_clean_working_directory()
            if not is_clean:
                raise RepositoryError(
                    "Working directory has uncommitted changes.\n\n"
                    f"{status_msg}\n\n"
                    "Cannot create documentation branch with uncommitted changes.\n"
                    "Please commit or stash your changes first:\n"
                    "  git add -A && git commit -m \"Your message\"\n"
                    "  # or\n"
                    "  git stash"
                )
            
            # Create branch
            branch_name = git_manager.create_documentation_branch()
            logger.success(f"Created branch: {branch_name}")
        
        # Generate documentation
        logger.step("Generating documentation...", 4, 4)
        click.echo()
        
        # Create generation options
        generation_options = GenerationOptions(
            create_branch=create_branch,
            github_pages=github_pages,
            no_cache=no_cache,
            custom_output=output if output != "docs" else None
        )
        
        # Create generator
        generator = CLIDocumentationGenerator(
            repo_path=repo_path,
            output_dir=output_dir,
            config={
                'main_model': config.main_model,
                'cluster_model': config.cluster_model,
                'base_url': config.base_url,
                'api_key': api_key,
            },
            verbose=verbose,
            generate_html=github_pages
        )
        
        # Run generation
        job = generator.generate()
        
        # Post-generation
        generation_time = time.time() - start_time
        
        # Get repository info
        repo_url = None
        commit_hash = get_git_commit_hash(repo_path)
        current_branch = get_git_branch(repo_path)
        
        if is_git_repository(repo_path):
            try:
                import git
                repo = git.Repo(repo_path)
                if repo.remotes:
                    repo_url = repo.remotes.origin.url
            except:
                pass
        
        # Display instructions
        display_post_generation_instructions(
            output_dir=output_dir,
            repo_name=repo_path.name,
            repo_url=repo_url,
            branch_name=branch_name,
            github_pages=github_pages,
            files_generated=job.files_generated,
            statistics={
                'module_count': job.module_count,
                'total_files_analyzed': job.statistics.total_files_analyzed,
                'generation_time': generation_time,
                'total_tokens_used': job.statistics.total_tokens_used,
            }
        )
        
    except ConfigurationError as e:
        logger.error(e.message)
        sys.exit(e.exit_code)
    except RepositoryError as e:
        logger.error(e.message)
        sys.exit(e.exit_code)
    except APIError as e:
        logger.error(e.message)
        sys.exit(e.exit_code)
    except KeyboardInterrupt:
        click.echo("\n\nInterrupted by user")
        sys.exit(130)
    except Exception as e:
        sys.exit(handle_error(e, verbose=verbose))

