#!/usr/bin/env python3
"""
Test script to demonstrate colored logging functionality.
This script showcases all log levels and their colors.
"""

import logging
from codewiki.src.be.dependency_analyzer.utils.logging_config import setup_logging

# Setup colored logging
setup_logging(level=logging.DEBUG)

# Get loggers for different modules
logger = logging.getLogger(__name__)
module_logger = logging.getLogger('codewiki.src.be.documentation_generator')
sub_module_logger = logging.getLogger('codewiki.src.be.agent_tools.generate_sub_module_documentations')

def main():
    """Demonstrate all log levels with colored output."""
    print("\n" + "="*80)
    print("CodeWiki Colored Logging Test")
    print("="*80 + "\n")
    
    # Test all log levels
    logger.debug("This is a DEBUG message - useful for development")
    logger.info("This is an INFO message - normal operation")
    logger.warning("This is a WARNING message - something needs attention")
    logger.error("This is an ERROR message - something went wrong")
    logger.critical("This is a CRITICAL message - urgent issue!")
    
    print("\n" + "-"*80)
    print("Different modules:")
    print("-"*80 + "\n")
    
    # Test with different module names
    module_logger.info("📄 Processing module: documentation_generator")
    sub_module_logger.info("  └─ Generating documentation for sub-module")
    
    print("\n" + "-"*80)
    print("Nested operations simulation:")
    print("-"*80 + "\n")
    
    # Simulate nested module processing
    logger.info("📚 Starting documentation generation")
    module_logger.info("📁 Processing parent module: backend")
    sub_module_logger.info("  └─ Processing sub-module: auth")
    sub_module_logger.info("    └─ Processing sub-module: user_manager")
    module_logger.info("✓ Module docs already exists at ./output/auth.md")
    logger.info("✓ Documentation generation completed")
    
    print("\n" + "-"*80)
    print("Error handling:")
    print("-"*80 + "\n")
    
    # Test error messages
    try:
        raise ValueError("Example error for testing")
    except ValueError as e:
        logger.error(f"Failed to process module: {str(e)}")
    
    print("\n" + "="*80)
    print("Test completed! Check the colors above.")
    print("="*80 + "\n")

if __name__ == "__main__":
    main()

