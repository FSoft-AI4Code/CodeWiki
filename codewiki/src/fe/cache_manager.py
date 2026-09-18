#!/usr/bin/env python3
"""
Cache management for documentation generation results.
"""

import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict

from .models import CacheEntry
from .config import WebAppConfig
from codewiki.src.utils import file_manager


class CacheManager:
    """Manages documentation cache."""

    def __init__(self, cache_dir: str = None, cache_expiry_days: int = None):
        self.cache_dir = Path(cache_dir or WebAppConfig.CACHE_DIR)
        self.cache_expiry_days = cache_expiry_days or WebAppConfig.CACHE_EXPIRY_DAYS
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_index: Dict[str, CacheEntry] = {}
        self.load_cache_index()

    def load_cache_index(self):
        """Load cache index from disk."""
        index_file = self.cache_dir / "cache_index.json"
        if index_file.exists():
            try:
                data = file_manager.load_json(index_file)
                for key, value in data.items():
                    self.cache_index[key] = CacheEntry(
                        repo_url=value["repo_url"],
                        repo_url_hash=value["repo_url_hash"],
                        docs_path=value["docs_path"],
                        created_at=datetime.fromisoformat(value["created_at"]),
                        last_accessed=datetime.fromisoformat(value["last_accessed"]),
                    )
            except Exception as e:
                print(f"Error loading cache index: {e}")

    def save_cache_index(self):
        """Save cache index to disk."""
        index_file = self.cache_dir / "cache_index.json"
        try:
            data = {}
            for key, entry in self.cache_index.items():
                data[key] = {
                    "repo_url": entry.repo_url,
                    "repo_url_hash": entry.repo_url_hash,
                    "docs_path": entry.docs_path,
                    "created_at": entry.created_at.isoformat(),
                    "last_accessed": entry.last_accessed.isoformat(),
                }

            file_manager.save_json(data, index_file)
        except Exception as e:
            print(f"Error saving cache index: {e}")

    def get_repo_hash(self, repo_url: str) -> str:
        """Generate hash for repository URL."""
        return hashlib.sha256(repo_url.encode()).hexdigest()[:16]

    def get_cached_docs(self, repo_url: str) -> Optional[str]:
        """Get cached documentation path if available."""
        repo_hash = self.get_repo_hash(repo_url)

        if repo_hash in self.cache_index:
            entry = self.cache_index[repo_hash]

            # Check if cache is still valid
            if datetime.now() - entry.created_at < timedelta(days=self.cache_expiry_days):
                # Update last accessed
                entry.last_accessed = datetime.now()
                self.save_cache_index()
                return entry.docs_path
            else:
                # Cache expired, remove it
                self.remove_from_cache(repo_url)

        return None

    def add_to_cache(self, repo_url: str, docs_path: str):
        """Add documentation to cache."""
        repo_hash = self.get_repo_hash(repo_url)
        now = datetime.now()

        self.cache_index[repo_hash] = CacheEntry(
            repo_url=repo_url,
            repo_url_hash=repo_hash,
            docs_path=docs_path,
            created_at=now,
            last_accessed=now,
        )

        self.save_cache_index()

    def remove_from_cache(self, repo_url: str):
        """Remove documentation from cache."""
        repo_hash = self.get_repo_hash(repo_url)
        if repo_hash in self.cache_index:
            del self.cache_index[repo_hash]
            self.save_cache_index()

    def cleanup_expired_cache(self):
        """Remove expired cache entries."""
        expired_entries = []
        cutoff = datetime.now() - timedelta(days=self.cache_expiry_days)

        for repo_hash, entry in self.cache_index.items():
            if entry.created_at < cutoff:
                expired_entries.append(repo_hash)

        for repo_hash in expired_entries:
            del self.cache_index[repo_hash]

        if expired_entries:
            self.save_cache_index()
