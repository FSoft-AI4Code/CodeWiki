"""Toy repository from Part 4 of the C3 redesign note, shared by the updater tests."""

from __future__ import annotations

from copy import deepcopy

from codewiki.src.be.dependency_analyzer.models.core import Node


def node(cid: str, ctype: str, body: str, params=None, bases=None, deps=()) -> Node:
    rel, name = cid.split("::", 1)
    return Node(
        id=cid,
        name=name,
        component_type=ctype,
        file_path=f"/repo/{rel}",
        relative_path=rel,
        depends_on=set(deps),
        source_code=body,
        parameters=list(params or []),
        base_classes=list(bases or []) or None,
        node_type=ctype,
        component_id=cid,
    )


LOGIN = "src/auth/login.py::login"
VALIDATE = "src/auth/login.py::validate"
REFRESH = "src/auth/token.py::refresh"
HANDLE = "src/api/routes.py::handle"
USER = "src/db/models.py::User"
PIPELINE = "src/pipeline/run.py::Pipeline"
OAUTH = "src/auth/oauth.py::OAuthClient"


def graph_r1() -> dict[str, Node]:
    return {
        LOGIN: node(
            LOGIN,
            "function",
            "def login(user, pw):\n    return validate(user, pw)\n",
            ["user", "pw"],
            deps=[VALIDATE],
        ),
        VALIDATE: node(
            VALIDATE, "function", "def validate(user, pw):\n    return pw == 'x'\n", ["user", "pw"]
        ),
        REFRESH: node(
            REFRESH, "function", "def refresh(self):\n    return new_token()\n", ["self"]
        ),
        HANDLE: node(
            HANDLE, "function", "def handle(req):\n    return refresh()\n", ["req"], deps=[REFRESH]
        ),
        USER: node(USER, "class", "class User(Base):\n    id = Column()\n", bases=["Base"]),
        PIPELINE: node(PIPELINE, "class", "class Pipeline:\n    def run(self):\n        pass\n"),
    }


def graph_r2() -> dict[str, Node]:
    g = graph_r1()
    g[VALIDATE] = node(
        VALIDATE,
        "function",
        "def validate(user, pw):\n    log('validate')\n    return pw == 'x'\n",
        ["user", "pw"],
    )
    g[REFRESH] = node(
        REFRESH,
        "function",
        "def refresh(self, force=False):\n    return new_token(force)\n",
        ["self", "force"],
    )
    g[OAUTH] = node(
        OAUTH,
        "class",
        "class OAuthClient:\n    def token(self):\n        return refresh(self)\n",
        deps=[REFRESH],
    )
    del g[USER]
    return g


def tree_r1() -> dict:
    return {
        "core": {
            "path": "src",
            "components": [LOGIN, VALIDATE, REFRESH, HANDLE],
            "children": {
                "auth": {
                    "path": "src/auth",
                    "components": [LOGIN, VALIDATE, REFRESH],
                    "children": {},
                },
                "api": {"path": "src/api", "components": [HANDLE], "children": {}},
            },
        },
        "storage": {"path": "src/db", "components": [USER], "children": {}},
        "pipeline": {"path": "src/pipeline", "components": [PIPELINE], "children": {}},
    }


PAGES_R1 = {
    "auth": "# auth\n\nHandles `login` and `refresh` of tokens. See [api](api.md).\n",
    "api": "# api\n\nRoutes call `refresh` from [auth](auth.md) (`src/auth/token.py::refresh`).\n",
    "core": "# core\n\nChildren: [auth](auth.md), [api](api.md).\n",
    "storage": "# storage\n\nThe `User` model.\n",
    "pipeline": "# pipeline\n\nTokens are refreshed by [auth](auth.md) every five minutes via `refresh`.\n",
    "overview": "# overview\n\n- [core](core.md)\n- [storage](storage.md)\n- [pipeline](pipeline.md)\n",
}


def write_pages(docs_dir, pages=None) -> None:
    pages = pages if pages is not None else PAGES_R1
    docs_dir.mkdir(parents=True, exist_ok=True)
    for stem, text in pages.items():
        (docs_dir / f"{stem}.md").write_text(text, encoding="utf-8")


def tracked_r2() -> set[str]:
    return {LOGIN, VALIDATE, REFRESH, HANDLE, PIPELINE, OAUTH}


def copy(x):
    return deepcopy(x)
