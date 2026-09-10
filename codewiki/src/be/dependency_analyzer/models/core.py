from pydantic import BaseModel


class Node(BaseModel):
    id: str

    name: str

    component_type: str

    file_path: str

    relative_path: str

    depends_on: set[str] = set()

    source_code: str | None = None

    start_line: int = 0

    end_line: int = 0

    has_docstring: bool = False

    docstring: str = ""

    parameters: list[str] | None = None

    node_type: str | None = None

    base_classes: list[str] | None = None

    class_name: str | None = None

    display_name: str | None = None

    component_id: str | None = None

    language: str | None = None

    qualified_name: str | None = None

    # Set only on artifact nodes (component_type == "artifact"): one of the
    # classes in analyzers/artifact.py CLASS_PRIORITY (build, ci, container, ...).
    artifact_class: str | None = None

    def get_display_name(self) -> str:
        return self.display_name or self.name


class CallRelationship(BaseModel):
    caller: str

    callee: str

    call_line: int | None = None

    is_resolved: bool = False


class Repository(BaseModel):
    url: str

    name: str

    clone_path: str

    analysis_id: str
