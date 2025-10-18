"""
Logging utilities for CLI with colored output and progress tracking.
"""

import sys
from datetime import datetime
from typing import Optional
import click


class CLILogger:
    """Logger for CLI with support for verbose and normal modes."""
    
    def __init__(self, verbose: bool = False):
        """
        Initialize the logger.
        
        Args:
            verbose: Enable verbose output
        """
        self.verbose = verbose
        self.start_time = datetime.now()
    
    def debug(self, message: str):
        """Log debug message (only in verbose mode)."""
        if self.verbose:
            timestamp = datetime.now().strftime("%H:%M:%S")
            click.secho(f"[{timestamp}] {message}", fg="cyan", dim=True)
    
    def info(self, message: str):
        """Log info message."""
        click.echo(message)
    
    def success(self, message: str):
        """Log success message in green."""
        click.secho(f"✓ {message}", fg="green")
    
    def warning(self, message: str):
        """Log warning message in yellow."""
        click.secho(f"⚠️  {message}", fg="yellow")
    
    def error(self, message: str):
        """Log error message in red."""
        click.secho(f"✗ {message}", fg="red", err=True)
    
    def step(self, message: str, step: Optional[int] = None, total: Optional[int] = None):
        """
        Log a processing step.
        
        Args:
            message: Step description
            step: Current step number
            total: Total number of steps
        """
        if step is not None and total is not None:
            prefix = f"[{step}/{total}]"
        else:
            prefix = "→"
        
        click.secho(f"{prefix} {message}", fg="blue", bold=True)
    
    def elapsed_time(self) -> str:
        """Get elapsed time since logger was created."""
        elapsed = datetime.now() - self.start_time
        minutes = int(elapsed.total_seconds() // 60)
        seconds = int(elapsed.total_seconds() % 60)
        
        if minutes > 0:
            return f"{minutes}m {seconds}s"
        else:
            return f"{seconds}s"


def create_logger(verbose: bool = False) -> CLILogger:
    """
    Create and return a CLI logger.
    
    Args:
        verbose: Enable verbose output
        
    Returns:
        Configured CLILogger instance
    """
    return CLILogger(verbose=verbose)

