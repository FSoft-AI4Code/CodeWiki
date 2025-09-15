#!/usr/bin/env python3
"""
GitHub repository processing utilities.
"""

import os
import subprocess
from typing import Dict
from urllib.parse import urlparse

from .config import WebAppConfig


class GitHubRepoProcessor:
    """Handles GitHub repository processing."""
    
    @staticmethod
    def is_valid_github_url(url: str) -> bool:
        """Validate if the URL is a valid GitHub repository URL."""
        try:
            parsed = urlparse(url)
            if parsed.netloc.lower() not in ['github.com', 'www.github.com']:
                return False
            
            path_parts = parsed.path.strip('/').split('/')
            if len(path_parts) < 2:
                return False
            
            # Check if it's a valid repo path (owner/repo)
            return len(path_parts) >= 2 and all(part for part in path_parts[:2])
        except Exception:
            return False
    
    @staticmethod
    def get_repo_info(url: str) -> Dict[str, str]:
        """Extract repository information from GitHub URL."""
        parsed = urlparse(url)
        path_parts = parsed.path.strip('/').split('/')
        
        owner = path_parts[0]
        repo = path_parts[1]
        
        # Remove .git suffix if present
        if repo.endswith('.git'):
            repo = repo[:-4]
        
        return {
            'owner': owner,
            'repo': repo,
            'full_name': f"{owner}/{repo}",
            'clone_url': f"https://github.com/{owner}/{repo}.git"
        }
    
    @staticmethod
    def clone_repository(clone_url: str, target_dir: str, commit_id: str = None) -> bool:
        """Clone a GitHub repository to the target directory, optionally checking out a specific commit."""
        try:
            # Ensure target directory exists
            os.makedirs(os.path.dirname(target_dir), exist_ok=True)
            
            # If specific commit is requested, don't use shallow clone
            if commit_id:
                # Clone full repository to access specific commit
                result = subprocess.run([
                    'git', 'clone', clone_url, target_dir
                ], capture_output=True, text=True, timeout=WebAppConfig.CLONE_TIMEOUT)
                
                if result.returncode != 0:
                    print(f"Error cloning repository: {result.stderr}")
                    return False
                
                # Checkout specific commit
                result = subprocess.run([
                    'git', 'checkout', commit_id
                ], cwd=target_dir, capture_output=True, text=True, timeout=30)
                
                if result.returncode != 0:
                    print(f"Error checking out commit {commit_id}: {result.stderr}")
                    return False
            else:
                # Clone repository with shallow depth (default behavior)
                result = subprocess.run([
                    'git', 'clone', '--depth', str(WebAppConfig.CLONE_DEPTH), clone_url, target_dir
                ], capture_output=True, text=True, timeout=WebAppConfig.CLONE_TIMEOUT)
                
                if result.returncode != 0:
                    print(f"Error cloning repository: {result.stderr}")
                    return False
            
            return True
        except Exception as e:
            print(f"Error cloning repository: {e}")
            return False