from dataclasses import dataclass
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
    custom_instructions: str = None
    # Incremental updates: when set, ``str_replace_editor`` refuses any write
    # (create / str_replace / insert / undo_edit) to a docs file whose
    # absolute, resolved path is not in this set. ``None`` = unrestricted.
    allowed_write_paths: set[str] | None = None
