#!/usr/bin/env python3
"""
Data models and classes for the CodeWiki web application.
"""

from datetime import datetime
from typing import Optional
from dataclasses import dataclass
from pydantic import BaseModel, HttpUrl


class RepositorySubmission(BaseModel):
    """Pydantic model for repository submission form."""

    repo_url: HttpUrl


class JobStatusResponse(BaseModel):
    """Pydantic model for job status API response."""

    job_id: str
    repo_url: str
    status: str
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    progress: str = ""
    docs_path: Optional[str] = None
    main_model: Optional[str] = None
    commit_id: Optional[str] = None


@dataclass
class JobStatus:
    """Tracks the status of a documentation generation job."""

    job_id: str
    repo_url: str
    status: str  # 'queued', 'processing', 'completed', 'failed'
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    progress: str = ""
    docs_path: Optional[str] = None
    main_model: Optional[str] = None
    commit_id: Optional[str] = None


@dataclass
class CacheEntry:
    """Represents a cached documentation result."""

    repo_url: str
    repo_url_hash: str
    docs_path: str
    created_at: datetime
    last_accessed: datetime
