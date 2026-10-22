"""Tests for the tree-sitter based Vue single-file component analyzer."""

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_language_pack")

from codewiki.src.be.dependency_analyzer.analyzers.artifact import (
    ArtifactOptions,
    classify_artifact,
)
from codewiki.src.be.dependency_analyzer.analyzers.vue import (
    analyze_vue_file,
    pascal_case,
    vue_component_name,
)
from codewiki.src.be.dependency_analyzer.ast_parser import DependencyParser

OPTIONS_SFC = """\
<template>
  <div class="list">
    <article-preview v-for="a in articles" :article="a" :key="a.slug" />
    <VPagination :pages="pages" @update="setPage" />
    <router-link to="/">home</router-link>
    <button @click="load()">{{ format(count) }}</button>
  </div>
</template>

<script>
import ArticlePreview from "./ArticlePreview";
import VPagination from "@/components/VPagination.vue";
import { fetchArticles as fetchAll } from "@/api/articles";
import { mapGetters } from "vuex";

function format(n) {
  return String(n);
}

export default {
  name: "RwvArticleList",
  components: { ArticlePreview, VPagination, GlobalWidget },
  computed: {
    ...mapGetters(["articles"]),
    pages() {
      return 3;
    },
  },
  watch: {
    type() {
      this.load();
    },
  },
  mounted() {
    this.load();
  },
  methods: {
    load() {
      fetchAll();
      format(1);
    },
    setPage: function (p) {
      this.page = p;
      this.load();
    },
  },
};
</script>

<style scoped>
.list { color: red; }
</style>
"""

SETUP_SFC = """\
<script lang="ts">
export interface Props {
  title: string;
}
</script>

<script setup lang="ts">
import { computed } from "vue";
import TagList from "../components/TagList.vue";

const props = defineProps<Props>();

function shout(text: string): string {
  return text.toUpperCase();
}

const loud = computed(() => shout(props.title));
</script>

<template>
  <h1 @click="shout(props.title)">{{ loud }}</h1>
  <TagList />
</template>
"""


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def vue_repo(tmp_path: Path) -> Path:
    _write(tmp_path, "src/components/ArticleList.vue", OPTIONS_SFC)
    _write(
        tmp_path,
        "src/components/ArticlePreview.vue",
        "<template><p>{{ article.title }}</p></template>\n"
        "<script>\nexport default { props: ['article'] };\n</script>\n",
    )
    _write(tmp_path, "src/components/VPagination.vue", "<template><nav /></template>\n")
    _write(
        tmp_path,
        "src/api/articles.js",
        "export function fetchArticles() {\n  return fetch('/api/articles');\n}\n",
    )
    _write(tmp_path, "src/views/Home.vue", SETUP_SFC)
    _write(tmp_path, "src/components/TagList.vue", "<template><ul /></template>\n")
    return tmp_path


def _analyze(repo: Path, rel: str):
    path = repo / rel
    nodes, rels = analyze_vue_file(str(path), path.read_text(), repo_path=str(repo))
    return {n.id: n for n in nodes}, {(r.caller, r.callee): r for r in rels}


# --------------------------------------------------------------------------- #
# names
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value, expected",
    [
        ("article-preview", "ArticlePreview"),
        ("ArticlePreview", "ArticlePreview"),
        ("article_list", "ArticleList"),
        ("app", "App"),
    ],
)
def test_pascal_case(value: str, expected: str) -> None:
    assert pascal_case(value) == expected


def test_component_name_from_path() -> None:
    assert vue_component_name("src/components/article-list.vue") == "ArticleList"
    assert vue_component_name("src/views/Settings/index.vue") == "Settings"
    assert vue_component_name("App.vue") == "App"


# --------------------------------------------------------------------------- #
# options API
# --------------------------------------------------------------------------- #


def test_component_and_members(vue_repo: Path) -> None:
    nodes, _ = _analyze(vue_repo, "src/components/ArticleList.vue")
    base = "src/components/ArticleList.vue::"
    component = nodes[base + "ArticleList"]
    assert component.component_type == "class"
    assert component.node_type == "component"
    assert component.language == "vue"
    assert component.start_line == 1 and component.end_line == OPTIONS_SFC.count("\n")

    for member in ("load", "setPage", "pages", "type", "mounted"):
        node = nodes[f"{base}ArticleList.{member}"]
        assert node.component_type == "method"
        assert node.class_name == "ArticleList"
    # spreads and non-function options are not members
    assert not any(i.endswith(("ArticleList.articles", "ArticleList.name")) for i in nodes)
    # script-level functions still come from the JavaScript analyzer
    assert base + "format" in nodes


def test_line_numbers_follow_the_vue_file(vue_repo: Path) -> None:
    nodes, _ = _analyze(vue_repo, "src/components/ArticleList.vue")
    lines = OPTIONS_SFC.splitlines()
    fmt = nodes["src/components/ArticleList.vue::format"]
    assert lines[fmt.start_line - 1].startswith("function format(n)")
    load = nodes["src/components/ArticleList.vue::ArticleList.load"]
    assert lines[load.start_line - 1].strip() == "load() {"
    assert load.source_code.strip().startswith("load() {")


def test_template_and_import_edges(vue_repo: Path) -> None:
    _, rels = _analyze(vue_repo, "src/components/ArticleList.vue")
    base = "src/components/ArticleList.vue::"
    component = base + "ArticleList"

    # kebab-case tag -> import resolved through the extension-less path
    edge = rels[(component, "src/components/ArticlePreview.vue::ArticlePreview")]
    assert edge.is_resolved
    # PascalCase tag -> `@/` import
    assert (component, "src/components/VPagination.vue::VPagination") in rels
    # globally registered component: bare name for CallGraphAnalyzer
    assert (component, "GlobalWidget") in rels
    # built-in tags and package imports are not edges
    assert not any(callee in ("RouterLink", "mapGetters") for _, callee in rels)
    # template handlers / bindings / interpolations -> members and script functions
    assert (component, base + "ArticleList.setPage") in rels
    assert (component, base + "ArticleList.load") in rels
    assert (component, base + "format") in rels
    # named import keeps the exported name
    assert (component, "src/api/articles.js::fetchArticles") in rels


def test_member_call_edges(vue_repo: Path) -> None:
    _, rels = _analyze(vue_repo, "src/components/ArticleList.vue")
    base = "src/components/ArticleList.vue::ArticleList."
    assert (base + "setPage", base + "load") in rels  # this.load()
    assert (base + "type", base + "load") in rels
    assert (base + "mounted", base + "load") in rels
    assert (base + "load", "src/components/ArticleList.vue::format") in rels
    assert (base + "load", "src/api/articles.js::fetchArticles") in rels  # aliased import


# --------------------------------------------------------------------------- #
# <script setup lang="ts">
# --------------------------------------------------------------------------- #


def test_script_setup_typescript(vue_repo: Path) -> None:
    nodes, rels = _analyze(vue_repo, "src/views/Home.vue")
    base = "src/views/Home.vue::"
    assert base + "Home" in nodes
    # both script blocks reach the TypeScript analyzer
    assert base + "Props" in nodes
    shout = nodes[base + "shout"]
    assert shout.language == "typescript"
    assert SETUP_SFC.splitlines()[shout.start_line - 1].startswith("function shout")
    assert (base + "Home", base + "shout") in rels
    assert (base + "Home", "src/components/TagList.vue::TagList") in rels


def test_file_without_script(vue_repo: Path) -> None:
    nodes, rels = _analyze(vue_repo, "src/components/VPagination.vue")
    assert list(nodes) == ["src/components/VPagination.vue::VPagination"]
    assert not rels


def test_broken_file_does_not_raise(tmp_path: Path) -> None:
    path = _write(tmp_path, "Broken.vue", "<template><div></template>\n<script>export default {")
    nodes, _ = analyze_vue_file(str(path), path.read_text(), repo_path=str(tmp_path))
    assert "Broken.vue::Broken" in {n.id for n in nodes}


# --------------------------------------------------------------------------- #
# pipeline
# --------------------------------------------------------------------------- #


def test_vue_is_code_not_artifact() -> None:
    assert classify_artifact("src/App.vue", "App.vue", 100, ArtifactOptions()) is None


def test_dependency_parser_resolves_across_files(vue_repo: Path) -> None:
    components = DependencyParser(str(vue_repo), use_gitignore=False).parse_repository()
    article_list = components["src/components/ArticleList.vue::ArticleList"]
    assert "src/components/ArticlePreview.vue::ArticlePreview" in article_list.depends_on
    assert "src/components/VPagination.vue::VPagination" in article_list.depends_on
    assert "src/api/articles.js::fetchArticles" in article_list.depends_on
    assert "src/components/TagList.vue::TagList" in (
        components["src/views/Home.vue::Home"].depends_on
    )
