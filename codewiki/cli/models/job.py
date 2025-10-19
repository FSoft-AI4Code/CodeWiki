"""
Documentation job data models.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import List, Optional, Dict, Any
from enum import Enum
import uuid
import json


class JobStatus(str, Enum):
    """Documentation job status."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class GenerationOptions:
    """Options for documentation generation."""
    create_branch: bool = False
    github_pages: bool = False
    no_cache: bool = False
    custom_output: Optional[str] = None


@dataclass
class JobStatistics:
    """Statistics for a documentation job."""
    total_files_analyzed: int = 0
    leaf_nodes: int = 0
    max_depth: int = 0
    total_tokens_used: int = 0


@dataclass
class LLMConfig:
    """LLM configuration for a job."""
    main_model: str
    cluster_model: str
    base_url: str


@dataclass
class DocumentationJob:
    """
    Represents a documentation generation job.
    
    Attributes:
        job_id: Unique job identifier
        repository_path: Absolute path to repository
        repository_name: Repository name
        output_directory: Output directory path
        commit_hash: Git commit SHA
        branch_name: Git branch name (if applicable)
        timestamp_start: Job start time
        timestamp_end: Job end time (if completed)
        status: Current job status
        error_message: Error message (if failed)
        files_generated: List of generated files
        module_count: Number of modules documented
        generation_options: Generation options used
        llm_config: LLM configuration used
        statistics: Job statistics
    """
    job_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    repository_path: str = ""
    repository_name: str = ""
    output_directory: str = ""
    commit_hash: str = ""
    branch_name: Optional[str] = None
    timestamp_start: str = field(default_factory=lambda: datetime.now().isoformat())
    timestamp_end: Optional[str] = None
    status: JobStatus = JobStatus.PENDING
    error_message: Optional[str] = None
    files_generated: List[str] = field(default_factory=list)
    module_count: int = 0
    generation_options: GenerationOptions = field(default_factory=GenerationOptions)
    llm_config: Optional[LLMConfig] = None
    statistics: JobStatistics = field(default_factory=JobStatistics)
    
    def start(self):
        """Mark job as started."""
        self.status = JobStatus.RUNNING
        self.timestamp_start = datetime.now().isoformat()
    
    def complete(self):
        """Mark job as completed."""
        self.status = JobStatus.COMPLETED
        self.timestamp_end = datetime.now().isoformat()
    
    def fail(self, error_message: str):
        """Mark job as failed."""
        self.status = JobStatus.FAILED
        self.error_message = error_message
        self.timestamp_end = datetime.now().isoformat()
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        data = {
            "job_id": self.job_id,
            "repository_path": self.repository_path,
            "repository_name": self.repository_name,
            "output_directory": self.output_directory,
            "commit_hash": self.commit_hash,
            "branch_name": self.branch_name,
            "timestamp_start": self.timestamp_start,
            "timestamp_end": self.timestamp_end,
            "status": self.status.value if isinstance(self.status, JobStatus) else self.status,
            "error_message": self.error_message,
            "files_generated": self.files_generated,
            "module_count": self.module_count,
            "generation_options": asdict(self.generation_options),
            "llm_config": asdict(self.llm_config) if self.llm_config else None,
            "statistics": asdict(self.statistics),
        }
        return data
    
    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), indent=2)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'DocumentationJob':
        """Create from dictionary."""
        job = cls(
            job_id=data.get('job_id', str(uuid.uuid4())),
            repository_path=data.get('repository_path', ''),
            repository_name=data.get('repository_name', ''),
            output_directory=data.get('output_directory', ''),
            commit_hash=data.get('commit_hash', ''),
            branch_name=data.get('branch_name'),
            timestamp_start=data.get('timestamp_start', datetime.now().isoformat()),
            timestamp_end=data.get('timestamp_end'),
            status=JobStatus(data.get('status', 'pending')),
            error_message=data.get('error_message'),
            files_generated=data.get('files_generated', []),
            module_count=data.get('module_count', 0),
        )
        
        # Parse nested objects
        if 'generation_options' in data:
            opts = data['generation_options']
            job.generation_options = GenerationOptions(**opts)
        
        if 'llm_config' in data and data['llm_config']:
            job.llm_config = LLMConfig(**data['llm_config'])
        
        if 'statistics' in data:
            job.statistics = JobStatistics(**data['statistics'])
        
        return job

