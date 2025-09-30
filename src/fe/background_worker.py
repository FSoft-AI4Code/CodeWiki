#!/usr/bin/env python3
"""
Background worker for processing documentation generation jobs.
"""

import os
import json
import time
import threading
import subprocess
import asyncio
from datetime import datetime
from pathlib import Path
from queue import Queue
from typing import Dict
from dataclasses import asdict

from be.documentation_generator import DocumentationGenerator
from config import Config, MAIN_MODEL
from .models import JobStatus
from .cache_manager import CacheManager
from .github_processor import GitHubRepoProcessor
from .config import WebAppConfig
from utils import file_manager

class BackgroundWorker:
    """Background worker for processing documentation generation jobs."""
    
    def __init__(self, cache_manager: CacheManager, temp_dir: str = None):
        self.cache_manager = cache_manager
        self.temp_dir = temp_dir or WebAppConfig.TEMP_DIR
        self.running = False
        self.processing_queue = Queue(maxsize=WebAppConfig.QUEUE_SIZE)
        self.job_status: Dict[str, JobStatus] = {}
        self.jobs_file = Path(WebAppConfig.CACHE_DIR) / "jobs.json"
        self.load_job_statuses()
    
    def start(self):
        """Start the background worker thread."""
        if not self.running:
            self.running = True
            thread = threading.Thread(target=self._worker_loop, daemon=True)
            thread.start()
            print("Background worker started")
    
    def stop(self):
        """Stop the background worker."""
        self.running = False
    
    def add_job(self, job_id: str, job: JobStatus):
        """Add a job to the processing queue."""
        self.job_status[job_id] = job
        self.processing_queue.put(job_id)
    
    def get_job_status(self, job_id: str) -> JobStatus:
        """Get job status by ID."""
        return self.job_status.get(job_id)
    
    def get_all_jobs(self) -> Dict[str, JobStatus]:
        """Get all job statuses."""
        return self.job_status
    
    def load_job_statuses(self):
        """Load job statuses from disk."""
        if not self.jobs_file.exists():
            # Try to reconstruct from cache if no job file exists
            self._reconstruct_jobs_from_cache()
            return
        
        try:
            data = file_manager.load_json(self.jobs_file)
                
            for job_id, job_data in data.items():
                # Only load completed jobs to avoid inconsistent state
                if job_data.get('status') == 'completed':
                    self.job_status[job_id] = JobStatus(
                        job_id=job_data['job_id'],
                        repo_url=job_data['repo_url'],
                        status=job_data['status'],
                        created_at=datetime.fromisoformat(job_data['created_at']),
                        started_at=datetime.fromisoformat(job_data['started_at']) if job_data.get('started_at') else None,
                        completed_at=datetime.fromisoformat(job_data['completed_at']) if job_data.get('completed_at') else None,
                        error_message=job_data.get('error_message'),
                        progress=job_data.get('progress', ''),
                        docs_path=job_data.get('docs_path')
                    )
            print(f"Loaded {len([j for j in self.job_status.values() if j.status == 'completed'])} completed jobs from disk")
        except Exception as e:
            print(f"Error loading job statuses: {e}")
    
    def _reconstruct_jobs_from_cache(self):
        """Reconstruct job statuses from cache entries for backward compatibility."""
        try:
            cache_entries = self.cache_manager.cache_index
            reconstructed_count = 0
            
            for repo_hash, cache_entry in cache_entries.items():
                # Extract repo info to create job_id
                from .github_processor import GitHubRepoProcessor
                try:
                    repo_info = GitHubRepoProcessor.get_repo_info(cache_entry.repo_url)
                    job_id = repo_info['full_name'].replace('/', '--')
                    
                    # Only add if job doesn't already exist
                    if job_id not in self.job_status:
                        self.job_status[job_id] = JobStatus(
                            job_id=job_id,
                            repo_url=cache_entry.repo_url,
                            status='completed',
                            created_at=cache_entry.created_at,
                            completed_at=cache_entry.created_at,
                            docs_path=cache_entry.docs_path,
                            progress="Reconstructed from cache"
                        )
                        reconstructed_count += 1
                except Exception as e:
                    print(f"Failed to reconstruct job for {cache_entry.repo_url}: {e}")
            
            if reconstructed_count > 0:
                print(f"Reconstructed {reconstructed_count} job statuses from cache")
                self.save_job_statuses()
                
        except Exception as e:
            print(f"Error reconstructing jobs from cache: {e}")
    
    def save_job_statuses(self):
        """Save job statuses to disk."""
        try:
            # Ensure cache directory exists
            self.jobs_file.parent.mkdir(parents=True, exist_ok=True)
            
            data = {}
            for job_id, job in self.job_status.items():
                data[job_id] = {
                    'job_id': job.job_id,
                    'repo_url': job.repo_url,
                    'status': job.status,
                    'created_at': job.created_at.isoformat(),
                    'started_at': job.started_at.isoformat() if job.started_at else None,
                    'completed_at': job.completed_at.isoformat() if job.completed_at else None,
                    'error_message': job.error_message,
                    'progress': job.progress,
                    'docs_path': job.docs_path
                }
            
            file_manager.save_json(data, self.jobs_file)
        except Exception as e:
            print(f"Error saving job statuses: {e}")
    
    def _worker_loop(self):
        """Main worker loop."""
        while self.running:
            try:
                if not self.processing_queue.empty():
                    job_id = self.processing_queue.get(timeout=1)
                    self._process_job(job_id)
                else:
                    time.sleep(1)
            except Exception as e:
                print(f"Worker error: {e}")
                time.sleep(1)
    
    def _process_job(self, job_id: str):
        """Process a single documentation generation job."""
        if job_id not in self.job_status:
            return
        
        job = self.job_status[job_id]
        
        try:
            # Update job status
            job.status = 'processing'
            job.started_at = datetime.now()
            job.progress = "Starting repository clone..."
            job.main_model = MAIN_MODEL
            
            # Check cache first
            cached_docs = self.cache_manager.get_cached_docs(job.repo_url)
            if cached_docs and Path(cached_docs).exists():
                job.status = 'completed'
                job.completed_at = datetime.now()
                job.docs_path = cached_docs
                job.progress = "Documentation retrieved from cache"
                if not job.main_model:  # Only set if not already set
                    job.main_model = MAIN_MODEL
                
                # Save job status to disk
                self.save_job_statuses()
                
                print(f"Job {job_id}: Using cached documentation")
                return
            
            # Clone repository
            repo_info = GitHubRepoProcessor.get_repo_info(job.repo_url)
            # Use repo full name for temp directory (already URL-safe since job_id is URL-safe)
            temp_repo_dir = os.path.join(self.temp_dir, job_id)
            
            job.progress = f"Cloning repository {repo_info['full_name']}..."
            
            if not GitHubRepoProcessor.clone_repository(repo_info['clone_url'], temp_repo_dir, job.commit_id):
                raise Exception("Failed to clone repository")
            
            # Generate documentation
            job.progress = "Analyzing repository structure..."
            
            # Create config for documentation generation
            config = Config(
                repo_path=temp_repo_dir,
                output_dir="output",
                dependency_graph_dir=os.path.join("output", "dependency_graphs"),
                docs_dir=os.path.join("output", "docs", f"{job_id}-docs"),
                max_depth=2
            )
            
            job.progress = "Generating documentation..."
            
            # Generate documentation
            doc_generator = DocumentationGenerator(config, job.commit_id)
            
            # Run the async documentation generation in a new event loop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(doc_generator.run())
            finally:
                loop.close()
            
            # Cache the results
            docs_path = os.path.abspath(config.docs_dir)
            self.cache_manager.add_to_cache(job.repo_url, docs_path)
            
            # Update job status
            job.status = 'completed'
            job.completed_at = datetime.now()
            job.docs_path = docs_path
            job.progress = "Documentation generation completed"
            
            # Save job status to disk
            self.save_job_statuses()
            
            print(f"Job {job_id}: Documentation generated successfully")
            
        except Exception as e:
            # Update job status with error
            job.status = 'failed'
            job.completed_at = datetime.now()
            job.error_message = str(e)
            job.progress = f"Failed: {str(e)}"
            
            print(f"Job {job_id}: Failed with error: {e}")
        
        finally:
            # Cleanup temporary repository
            if 'temp_repo_dir' in locals() and os.path.exists(temp_repo_dir):
                try:
                    subprocess.run(['rm', '-rf', temp_repo_dir], check=True)
                except Exception as e:
                    print(f"Failed to cleanup temp directory: {e}")