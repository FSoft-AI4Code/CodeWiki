#!/usr/bin/env python3
"""
Git repository processing utilities.
Supports GitHub, GitLab, Gitee, Bitbucket, and any Git URL.
"""

import os
import subprocess
import re
from typing import Dict, Optional
from urllib.parse import urlparse

from .config import WebAppConfig


class GitRepoProcessor:
    """Handles Git repository processing for various platforms."""
    
    # Supported Git hosting platforms
    KNOWN_GIT_HOSTS = [
        'github.com',
        'gitlab.com',
        'gitee.com',
        'bitbucket.org',
        'coding.net',
    ]
    
    @staticmethod
    def is_valid_git_url(url: str) -> bool:
        """Validate if the URL is a valid Git repository URL."""
        try:
            # Check if it's a valid URL
            if url.startswith('git@') or url.startswith('ssh://'):
                # SSH format: git@host:owner/repo.git or ssh://git@host/owner/repo.git
                return True
            
            parsed = urlparse(url)
            
            # Must have a scheme (http/https) or be SSH format
            if not parsed.scheme and not url.startswith('git@'):
                return False
            
            # Must have a netloc (domain)
            if not parsed.netloc and not url.startswith('git@'):
                return False
            
            # Check path - should have at least owner/repo
            path_parts = parsed.path.strip('/').split('/')
            if len(path_parts) < 2:
                return False
            
            return True
        except Exception:
            return False
    
    @staticmethod
    def normalize_git_url(url: str) -> str:
        """Normalize Git URL to HTTPS format."""
        url = url.strip()
        
        # Convert SSH format to HTTPS
        # git@github.com:owner/repo.git -> https://github.com/owner/repo
        ssh_pattern = r'git@([^:]+):(.+?)(?:\.git)?$'
        ssh_match = re.match(ssh_pattern, url)
        if ssh_match:
            host = ssh_match.group(1)
            path = ssh_match.group(2)
            return f"https://{host}/{path}"
        
        # ssh://git@host/owner/repo.git -> https://host/owner/repo
        if url.startswith('ssh://git@'):
            url = url.replace('ssh://git@', 'https://')
        
        # Remove .git suffix
        if url.endswith('.git'):
            url = url[:-4]
        
        # Ensure https://
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        
        return url.rstrip('/')
    
    @staticmethod
    def get_repo_info(url: str) -> Dict[str, str]:
        """Extract repository information from Git URL."""
        # Normalize URL first
        normalized_url = GitRepoProcessor.normalize_git_url(url)
        parsed = urlparse(normalized_url)
        
        # Get host
        host = parsed.netloc
        
        # Get path parts
        path_parts = parsed.path.strip('/').split('/')
        
        # Extract owner and repo (handle different URL structures)
        if len(path_parts) >= 2:
            owner = path_parts[0]
            repo = path_parts[1]
        else:
            owner = 'unknown'
            repo = 'unknown'
        
        # Remove .git suffix if present
        if repo.endswith('.git'):
            repo = repo[:-4]
        
        # Determine platform
        platform = GitRepoProcessor._detect_platform(host)
        
        # Generate clone URL
        clone_url = GitRepoProcessor._generate_clone_url(host, owner, repo)
        
        return {
            'host': host,
            'owner': owner,
            'repo': repo,
            'full_name': f"{owner}/{repo}",
            'clone_url': clone_url,
            'platform': platform,
            'web_url': normalized_url
        }
    
    @staticmethod
    def _detect_platform(host: str) -> str:
        """Detect Git platform from host."""
        host_lower = host.lower()
        if 'github.com' in host_lower:
            return 'GitHub'
        elif 'gitlab.com' in host_lower or 'gitlab' in host_lower:
            return 'GitLab'
        elif 'gitee.com' in host_lower:
            return 'Gitee'
        elif 'bitbucket.org' in host_lower:
            return 'Bitbucket'
        elif 'coding.net' in host_lower:
            return 'Coding'
        else:
            return 'Git'
    
    @staticmethod
    def _generate_clone_url(host: str, owner: str, repo: str) -> str:
        """Generate clone URL."""
        return f"https://{host}/{owner}/{repo}.git"
    
    @staticmethod
    def clone_repository(clone_url: str, target_dir: str, commit_id: str = None) -> bool:
        """Clone a Git repository to the target directory, optionally checking out a specific commit."""
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


# Backward compatibility alias
GitHubRepoProcessor = GitRepoProcessor