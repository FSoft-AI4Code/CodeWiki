from dataclasses import dataclass, field
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
    # Token usage tracking
    token_usage: dict = field(default_factory=lambda: {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0
    })
    
    def add_token_usage(self, usage: dict):
        """Add token usage from an LLM call."""
        self.token_usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
        self.token_usage["completion_tokens"] += usage.get("completion_tokens", 0)
        self.token_usage["total_tokens"] += usage.get("total_tokens", 0)