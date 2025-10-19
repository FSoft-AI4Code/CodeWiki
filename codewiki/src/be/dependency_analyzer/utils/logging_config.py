"""
Colored logging configuration for CodeWiki.

This module provides a custom logging formatter with colored output for better readability.

Color Scheme:
    - DEBUG: Cyan (dim) - Development and debugging information
    - INFO: Green - Normal operational messages
    - WARNING: Yellow - Warning messages that need attention
    - ERROR: Red - Error messages
    - CRITICAL: Bright Red - Critical issues requiring immediate attention
    
    Additional Colors:
    - Timestamp: Blue
    - Module Name: Magenta
    
Usage:
    from codewiki.src.be.dependency_analyzer.utils.logging_config import setup_logging
    
    # Setup colored logging for the entire application
    setup_logging(level=logging.INFO)
    
    # Or setup for a specific module
    logger = setup_module_logging('my_module', level=logging.DEBUG)
"""

import logging
import sys
from colorama import Fore, Style, init

# Initialize colorama for cross-platform colored terminal output
init(autoreset=True)


class ColoredFormatter(logging.Formatter):
    """Custom formatter with colored output for better readability.
    
    This formatter adds colors to different log levels and components:
    - Log levels are colored based on severity
    - Timestamps are shown in blue
    - Module names are shown in magenta
    - Messages are shown in the default terminal color
    """
    
    # Define colors for different log levels
    COLORS = {
        'DEBUG': Fore.BLUE,
        'INFO': Fore.CYAN,
        'WARNING': Fore.YELLOW,
        'ERROR': Fore.RED,
        'CRITICAL': Fore.RED + Style.BRIGHT,
    }
    
    # Define colors for different components
    COMPONENT_COLORS = {
        'timestamp': Fore.BLUE,
        'module': Fore.MAGENTA,
        'reset': Style.RESET_ALL,
    }
    
    def format(self, record):
        """Format log record with colors."""
        # Get the color for this log level
        level_color = self.COLORS.get(record.levelname, '')
        
        # Format timestamp
        timestamp = self.formatTime(record, '%H:%M:%S')
        colored_timestamp = f"{self.COMPONENT_COLORS['timestamp']}[{timestamp}]{self.COMPONENT_COLORS['reset']}"
        
        # Format log level with color
        colored_level = f"{level_color}{record.levelname:8}{self.COMPONENT_COLORS['reset']}"
        
        # Format the message with the same color as the log level
        message = record.getMessage()
        colored_message = f"{level_color}{message}{self.COMPONENT_COLORS['reset']}"
        
        # Combine all parts (without module name column)
        log_line = f"{colored_timestamp} {colored_level} {colored_message}"
        
        # Handle exceptions
        if record.exc_info:
            log_line += "\n" + self.formatException(record.exc_info)
        
        return log_line


def setup_logging(level=logging.INFO):
    """
    Set up logging configuration with colored output.
    
    Args:
        level: Logging level (default: logging.INFO)
    """
    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    
    # Set colored formatter
    colored_formatter = ColoredFormatter()
    console_handler.setFormatter(colored_formatter)
    
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    
    # Remove existing handlers to avoid duplicates
    root_logger.handlers.clear()
    
    # Add our console handler
    root_logger.addHandler(console_handler)


def setup_module_logging(module_name: str, level=logging.INFO):
    """
    Set up logging for a specific module with colored output.
    
    Args:
        module_name: Name of the module to configure logging for
        level: Logging level (default: logging.INFO)
    """
    logger = logging.getLogger(module_name)
    logger.setLevel(level)
    
    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    
    # Set colored formatter
    colored_formatter = ColoredFormatter()
    console_handler.setFormatter(colored_formatter)
    
    # Remove existing handlers
    logger.handlers.clear()
    
    # Add console handler
    logger.addHandler(console_handler)
    
    # Prevent propagation to avoid duplicate logs
    logger.propagate = False
    
    return logger


