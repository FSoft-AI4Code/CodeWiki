from dataclasses import dataclass, field
from typing import Callable, Optional
from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.config import Config

@dataclass
class CodeWikiDeps:
    absolute_docs_path: str
    absolute_repo_path: str
    registry: dict
    components: dict[str, Node]
    path_to_current_module: list[str]
    current_module_name: str
    module_tree: dict[str, any]
    max_depth: int
    current_depth: int
    config: Config  # LLM configuration
    progress_callback: Optional[Callable] = None  # Progress callback for WebSocket updates
    # Token usage tracking (local to this module)
    token_usage: dict = field(default_factory=lambda: {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0
    })
    # Reference to global total_token_usage for real-time updates
    global_token_usage: Optional[dict] = None
    
    def add_token_usage(self, usage: dict):
        """Add token usage from an LLM call."""
        self.token_usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
        self.token_usage["completion_tokens"] += usage.get("completion_tokens", 0)
        self.token_usage["total_tokens"] += usage.get("total_tokens", 0)
        
        # Also update global token usage if available
        if self.global_token_usage is not None:
            self.global_token_usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
            self.global_token_usage["completion_tokens"] += usage.get("completion_tokens", 0)
            self.global_token_usage["total_tokens"] += usage.get("total_tokens", 0)