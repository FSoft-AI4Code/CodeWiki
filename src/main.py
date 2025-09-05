"""
DeepwikiAgent - A tool for generating comprehensive documentation from Python codebases.

This module orchestrates the documentation generation process by:
1. Analyzing code dependencies
2. Clustering related modules
3. Generating documentation using AI agents
4. Creating overview documentation
"""

import logging
import argparse
import asyncio

# Configure logging and monitoring
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Local imports
from documentation_generator import DocumentationGenerator
from config import (
    Config,
)


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Generate comprehensive documentation for Python components in dependency order.'
    )
    parser.add_argument(
        '--repo-path',
        type=str,
        required=True,
        help='Path to the repository'
    )
    
    return parser.parse_args()


async def main() -> None:
    """Main entry point for the documentation generation process."""
    try:
        # Parse arguments and create configuration
        args = parse_arguments()
        config = Config.from_args(args)
        
        # Create and run documentation generator
        doc_generator = DocumentationGenerator(config)
        await doc_generator.run()
        
    except KeyboardInterrupt:
        logger.info("Documentation generation interrupted by user")
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        raise


if __name__ == "__main__":
    asyncio.run(main())