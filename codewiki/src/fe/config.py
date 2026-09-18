#!/usr/bin/env python3
"""
Configuration settings for the CodeWiki web application.
"""

import os
from pathlib import Path


class WebAppConfig:
    """Configuration class for web application settings."""

    # Directories
    CACHE_DIR = "./output/cache"
    TEMP_DIR = "./output/temp"
    OUTPUT_DIR = "./output"

    # Queue settings
    QUEUE_SIZE = 100

    # Cache settings
    CACHE_EXPIRY_DAYS = 365

    # Job cleanup settings
    JOB_CLEANUP_HOURS = 24000
    RETRY_COOLDOWN_MINUTES = 3

    # Server settings
    DEFAULT_HOST = "127.0.0.1"
    DEFAULT_PORT = 8000

    # Git settings
    CLONE_TIMEOUT = 300
    CLONE_DEPTH = 1

    @classmethod
    def ensure_directories(cls):
        """Ensure all required directories exist."""
        directories = [cls.CACHE_DIR, cls.TEMP_DIR, cls.OUTPUT_DIR]

        for directory in directories:
            Path(directory).mkdir(parents=True, exist_ok=True)

    @classmethod
    def get_absolute_path(cls, path: str) -> str:
        """Get absolute path for a given relative path."""
        return os.path.abspath(path)
