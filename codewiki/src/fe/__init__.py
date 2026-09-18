#!/usr/bin/env python3
"""
CodeWiki Frontend Module

Web interface components for the documentation generation service.
"""

from .web_app import app, main
from .models import JobStatus, JobStatusResponse, RepositorySubmission, CacheEntry
from .cache_manager import CacheManager
from .background_worker import BackgroundWorker
from .github_processor import GitHubRepoProcessor
from .routes import WebRoutes

__all__ = [
    "app",
    "main",
    "JobStatus",
    "JobStatusResponse",
    "RepositorySubmission",
    "CacheEntry",
    "CacheManager",
    "BackgroundWorker",
    "GitHubRepoProcessor",
    "WebRoutes",
]
