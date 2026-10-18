"""The generated docs viewer in a real browser: sidebar, search, diagram viewer.

Optional: runs only when Playwright and a Chromium are available
(``pip install playwright && playwright install chromium``, or a system
Chromium on PATH). CI does not install them, so it skips there.
"""

import functools
import http.server
import json
import shutil
import threading

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

from codewiki.cli.html_generator import HTMLGenerator  # noqa: E402

GROUPS = 8
PAGES_PER_GROUP = 5  # 8 groups x 5 pages: a sidebar much taller than the window

DIAGRAM = "flowchart TD\n" + "\n".join(
    f"    N{i}[Stage {i} reads the input] --> N{i + 1}[Stage {i + 1} writes the output]"
    for i in range(30)
)


def _page(title: str) -> str:
    return (
        f"# {title}\n\n{title} handles one part of the pipeline.\n\n"
        f"## How it works\n\nThe {title.lower()} stage reads, checks and writes.\n\n"
        f"```mermaid\n{DIAGRAM}\n```\n"
    )


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    docs = tmp_path_factory.mktemp("viewer")
    tree = {}
    for g in range(GROUPS):
        group = f"group_{g}"
        tree[group] = {"components": [f"g{g}.py::G"], "children": {}}
        (docs / f"{group}.md").write_text(_page(f"Group {g}"), encoding="utf-8")
        for i in range(PAGES_PER_GROUP):
            name = f"{group}_page_{i}"
            tree[group]["children"][name] = {"components": [f"g{g}/p{i}.py::P"], "children": {}}
            (docs / f"{name}.md").write_text(_page(f"Topic {g} {i}"), encoding="utf-8")
    (docs / "overview.md").write_text(_page("Overview"), encoding="utf-8")
    (docs / "module_tree.json").write_text(json.dumps(tree))
    HTMLGenerator().generate(docs / "index.html", "demo", module_tree=tree, docs_dir=docs)

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(docs))
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:  # noqa: BLE001 — no bundled browser: try a system one
            exe = shutil.which("chromium") or shutil.which("chromium-browser")
            if not exe:
                pytest.skip("no Chromium for Playwright")
            b = p.chromium.launch(executable_path=exe)
        yield b
        b.close()


@pytest.fixture
def page(site, browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 600})
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(site + "/")
    pg.wait_for_selector(".nav-item")
    pg.wait_for_timeout(500)
    yield pg
    assert not errors, errors
    ctx.close()


ACTIVE_IN_VIEW = """() => {
    const a = document.querySelector('.nav-item.active');
    const s = document.getElementById('sidebar').getBoundingClientRect();
    const r = a.getBoundingClientRect();
    return [a.dataset.file, r.top >= s.top && r.bottom <= s.bottom];
}"""


def test_search_and_cleared_filter_bring_the_page_into_the_sidebar(page):
    last = f"group_{GROUPS - 1}_page_{PAGES_PER_GROUP - 1}.md"
    page.keyboard.press("Control+k")
    page.keyboard.type(f"Topic {GROUPS - 1} {PAGES_PER_GROUP - 1}")
    page.wait_for_timeout(400)
    page.keyboard.press("Enter")
    page.wait_for_timeout(1200)
    assert page.evaluate(ACTIVE_IN_VIEW) == [last, True]

    # Filter, open a page, then clear the filter with the keyboard.
    page.evaluate("document.getElementById('sidebar').scrollTop = 0")
    page.click("#nav-filter")
    page.keyboard.type("Group 6 Page 3")
    page.click(".nav-item:visible >> text=Group 6 Page 3")
    page.wait_for_timeout(800)
    page.click("#nav-filter")
    for _ in range(len("Group 6 Page 3")):
        page.keyboard.press("Backspace")
    page.wait_for_timeout(1200)
    assert page.evaluate(ACTIVE_IN_VIEW) == ["group_6_page_3.md", True]


def test_sidebar_toggles_are_easy_to_hit(page):
    box = page.locator(".nav-toggle").first.bounding_box()
    assert box["width"] >= 32 and box["height"] >= 32
    group = page.locator(".nav-group").first
    collapsed = "el => el.classList.contains('collapsed')"
    before = group.evaluate(collapsed)
    page.locator(".nav-toggle").first.click()
    assert group.evaluate(collapsed) != before


STAGE = """() => {
    const m = document.querySelector('.lightbox-stage').style.transform
        .match(/translate\\(([-\\d.]+)px, ([-\\d.]+)px\\) scale\\(([-\\d.]+)\\)/);
    return [+m[1], +m[2], +m[3]];
}"""


def test_diagram_viewer_pans_smoothly(page):
    page.wait_for_selector("#content .mermaid svg")
    page.locator("#content .mermaid").first.click()
    page.wait_for_selector(".mermaid-lightbox.open")
    page.wait_for_timeout(300)
    page.mouse.move(600, 300)
    page.mouse.wheel(0, -300)
    page.wait_for_timeout(200)
    start = page.evaluate(STAGE)
    page.mouse.down()
    for i in range(1, 41):
        page.mouse.move(600 + i * 5, 300 + i * 2)
    layer = page.evaluate("getComputedStyle(document.querySelector('.lightbox-stage')).willChange")
    page.mouse.up()
    page.wait_for_timeout(200)
    end = page.evaluate(STAGE)
    assert [round(end[0] - start[0]), round(end[1] - start[1])] == [200, 80]  # follows the mouse
    assert layer == "transform"  # own layer while dragging
    assert page.evaluate(
        "getComputedStyle(document.querySelector('.lightbox-stage')).willChange"
    ) == ("auto")  # sharp again after
    assert page.evaluate("String(getSelection())") == ""  # dragging selects no text
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    assert page.locator(".mermaid-lightbox.open").count() == 0
