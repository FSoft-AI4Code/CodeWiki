"""The Ask AI chat window in a real browser, against ``codewiki serve``.

Optional: runs only when Playwright and a Chromium are available
(``pip install playwright && playwright install chromium``, or a system
Chromium on PATH). CI does not install them, so it skips there.

The backend is scripted and slow (each LLM call sleeps), so the tests can
watch the chat while it thinks: progress, Stop, minimizing, and the
edit / retry / copy actions, plus Mermaid and margins in the answer.
"""

import json
import shutil
import socket
import threading
import time
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

from codewiki.cli.html_generator import HTMLGenerator  # noqa: E402
from codewiki.src.be.qa import prompts as P  # noqa: E402
from codewiki.src.be.qa.engine import QAEngine  # noqa: E402
from codewiki.src.be.qa.index import QAIndex  # noqa: E402
from codewiki.src.be.qa.server import create_app  # noqa: E402
from codewiki.src.be.qa.sessions import SessionStore  # noqa: E402
from qa_fixture import build  # noqa: E402

UNSIGN = "pkg/timed.py::TimestampSigner.unsign"
DELAY = 0.8  # seconds per LLM call; an answer takes three

ANSWER = """`TimestampSigner.unsign` checks the signature, then the age. `pkg/timed.py:L16-L22`

```mermaid
flowchart LR
    A[unsign] --> B{signature ok?}
    B -- no --> C[BadSignature]
    B -- yes --> D{age <= max_age?}
    D -- no --> E[SignatureExpired]
    D -- yes --> F[value]
```

| Step | Function | What it checks | Raises when it fails |
|------|----------|----------------|----------------------|
| 1 | `Signer.unsign` | the HMAC signature over value + timestamp | `BadSignature` |
| 2 | `TimestampSigner.unsign` | the age against `max_age` | `SignatureExpired` |

A long identifier: `pkg.timed.TimestampSigner.unsign_with_a_really_long_name_that_keeps_going_and_going`.

```python
def unsign(self, signed_value, max_age=MAX_AGE):  # a long line that scrolls inside its own box
    value, ts = super().unsign(signed_value).rsplit(self.sep, 1)
```

<SUMMARY>read pkg/timed.py; unsign checks signature then age</SUMMARY>"""


class SlowBackend:
    def __init__(self):
        self.last_usage = {"input_tokens": 100, "output_tokens": 20}
        self.started = 0

    def complete(self, prompt, *, model=None, system_prompt=None, allow_tools=True):
        self.started += 1
        time.sleep(DELAY)
        if system_prompt == P.PLAN_SYSTEM:
            return json.dumps({"intent": "explain", "keep": [UNSIGN], "confidence": 40})
        if system_prompt == P.ROUND_SYSTEM:
            return json.dumps({"keep": [UNSIGN], "confidence": 95})
        return ANSWER


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    import uvicorn

    work = tmp_path_factory.mktemp("chat")
    _, docs = build(work)
    HTMLGenerator().generate(
        docs / "index.html",
        "repo",
        module_tree=json.loads((docs / "module_tree.json").read_text()),
        repository_url="https://github.com/acme/repo",
        metadata={"generation_info": {"commit_id": "abc123"}},
        docs_dir=Path(docs),
    )
    backend, sessions = SlowBackend(), SessionStore()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    app = create_app(
        str(docs),
        {base},
        engine_factory=lambda: QAEngine(QAIndex(str(docs)), backend, sessions=sessions),
        log_questions=False,
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    yield base, backend
    server.should_exit = True
    thread.join(5)


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
def chat(site, browser):
    """A fresh page with the chat open; ``page.asks`` records /api/ask bodies."""
    base, backend = site
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=base)
    page = ctx.new_page()
    errors, asks = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on(
        "request",
        lambda r: asks.append(json.loads(r.post_data)) if r.url.endswith("/api/ask") else None,
    )
    page.goto(base + "/")
    page.wait_for_selector("#chat-trigger")
    page.click("#chat-trigger")
    page.wait_for_selector("#chat-input")
    page.asks, page.backend = asks, backend
    yield page
    assert not errors, errors
    ctx.close()


def ask(page, question):
    page.fill("#chat-input", question)
    page.keyboard.press("Enter")
    page.wait_for_selector("#chat-progress")


def wait_idle(page):
    page.wait_for_function(
        "!document.getElementById('chat-send').classList.contains('stop')", timeout=30000
    )
    page.wait_for_timeout(300)


def is_open(page):
    return page.evaluate("document.getElementById('chat-panel').classList.contains('open')")


def margin_offenders(page):
    """Blocks in the messages box that cross its padding, and any sideways scroll."""
    return page.evaluate(
        """() => {
        const box = document.getElementById('chat-messages');
        const r0 = box.getBoundingClientRect(), cs = getComputedStyle(box);
        const left = r0.left + parseFloat(cs.paddingLeft);
        const right = r0.left + box.clientWidth - parseFloat(cs.paddingRight);
        const bad = [];
        box.querySelectorAll('.chat-turn, .chat-msg, .chat-progress, .chat-action svg, table, pre, .mermaid, p')
            .forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width && (r.left < left - 0.5 || r.right > right + 0.5))
                    bad.push(el.className || el.tagName);
            });
        if (box.scrollWidth > box.clientWidth) bad.push('sideways scroll');
        return bad;
    }"""
    )


def test_progress_and_minimizing_while_thinking(chat):
    ask(chat, "How does unsign check expiry?")
    assert "Stop" in chat.inner_text("#chat-send")
    first = chat.inner_text(".chat-progress-time")
    chat.wait_for_timeout(2000)
    assert chat.inner_text(".chat-progress-time") != first  # the timer ticks
    assert chat.locator(".chat-steps li").count() >= 2
    assert chat.locator(".chat-steps li.done .chat-step-time").count() >= 1

    chat.keyboard.press("Enter")  # never resends or stops
    assert len(chat.asks) == 1 and "Stop" in chat.inner_text("#chat-send")

    chat.keyboard.press("Escape")  # focus is in the input
    chat.wait_for_timeout(300)
    assert not is_open(chat)
    trigger = "document.getElementById('chat-trigger')"
    assert chat.evaluate(f"{trigger}.classList.contains('busy')")
    assert chat.inner_text("#chat-trigger .chat-trigger-label") == "Thinking…"
    assert chat.evaluate("getComputedStyle(document.getElementById('chat-panel')).visibility") == (
        "hidden"
    )
    chat.wait_for_function(f"{trigger}.classList.contains('unread')", timeout=30000)
    chat.click("#chat-trigger")
    chat.wait_for_selector(".chat-turn.assistant")
    assert not chat.evaluate(f"{trigger}.classList.contains('unread')")

    # Esc with focus on the Stop button, then with nothing focused; the button too.
    ask(chat, "And the default?")
    chat.focus("#chat-send")
    chat.keyboard.press("Escape")
    chat.wait_for_timeout(300)
    assert not is_open(chat)
    chat.click("#chat-trigger")
    chat.wait_for_selector("#chat-progress")  # still thinking on reopening
    chat.evaluate("document.activeElement.blur()")
    chat.keyboard.press("Escape")
    chat.wait_for_timeout(300)
    assert not is_open(chat)
    chat.click("#chat-trigger")
    chat.wait_for_timeout(300)
    chat.click("#chat-close")
    assert not is_open(chat)
    chat.click("#chat-trigger")
    wait_idle(chat)


def test_answer_renders_mermaid_tables_and_keeps_margins(chat):
    ask(chat, "How does unsign check expiry?")
    wait_idle(chat)
    chat.wait_for_selector("#chat-messages .mermaid svg")
    assert chat.locator("#chat-messages code.language-mermaid").count() == 0
    assert chat.locator("#chat-messages a.chat-cite").count() >= 1
    assert margin_offenders(chat) == []
    split = chat.evaluate(
        """() => [...document.querySelectorAll('#chat-messages td code')]
            .filter(el => el.getClientRects().length > 1).map(el => el.innerText)"""
    )
    assert split == []  # names in table cells stay whole

    # The diagram opens above the panel; Esc closes it and keeps the chat open.
    chat.click("#chat-messages .mermaid")
    chat.wait_for_selector(".mermaid-lightbox.open")
    assert chat.evaluate(
        """() => document.querySelector('.mermaid-lightbox.open')
            .contains(document.elementFromPoint(innerWidth - 100, innerHeight / 2))"""
    )
    chat.keyboard.press("Escape")
    chat.wait_for_timeout(300)
    assert chat.locator(".mermaid-lightbox.open").count() == 0 and is_open(chat)

    # Dark theme re-renders the diagram; a phone-width window keeps the margins.
    chat.click("#chat-close")
    chat.click("#theme-toggle")
    chat.click("#chat-trigger")
    chat.wait_for_selector("#chat-messages .mermaid svg")
    assert margin_offenders(chat) == []
    chat.set_viewport_size({"width": 390, "height": 800})
    chat.wait_for_timeout(500)
    assert margin_offenders(chat) == []


def test_copy_edit_and_retry(chat):
    ask(chat, "How does unsign check expiry?")
    wait_idle(chat)
    user_actions = ".chat-turn.user .chat-actions button"
    assert chat.locator(user_actions).all_inner_texts() == ["Copy", "Edit", "Retry"]
    answer_actions = ".chat-turn.assistant .chat-actions button"
    assert chat.locator(answer_actions).all_inner_texts() == ["Copy", "Retry"]

    chat.click(".chat-turn.assistant .chat-action[title=Copy]")
    clip = chat.evaluate("navigator.clipboard.readText()")
    assert clip.startswith("`TimestampSigner.unsign` checks") and "```mermaid" in clip
    chat.click(".chat-turn.user .chat-action[title=Copy]")
    assert chat.evaluate("navigator.clipboard.readText()") == "How does unsign check expiry?"

    # A follow-up, then retrying the first answer forgets the follow-up.
    ask(chat, "And the default max age?")
    assert chat.locator(".chat-turn.user .chat-action[title=Edit]").count() == 0  # busy
    wait_idle(chat)
    assert chat.asks[-1]["keep_turns"] == 1
    chat.locator(".chat-turn.assistant .chat-action[title=Retry]").first.click()
    wait_idle(chat)
    assert chat.locator(".chat-turn.user .chat-msg").all_inner_texts() == [
        "How does unsign check expiry?"
    ]
    assert chat.asks[-1]["keep_turns"] == 0
    assert chat.locator(".chat-turn.assistant").count() == 1

    # Edit: Esc cancels (the chat stays open), Enter sends the new text.
    chat.click(".chat-turn.user .chat-action[title=Edit]")
    chat.keyboard.press("Escape")
    chat.wait_for_timeout(200)
    assert chat.locator(".chat-edit").count() == 0 and is_open(chat)
    chat.click(".chat-turn.user .chat-action[title=Edit]")
    chat.fill(".chat-edit textarea", "How does signing work?")
    chat.keyboard.press("Enter")
    wait_idle(chat)
    assert chat.locator(".chat-turn.user .chat-msg").all_inner_texts() == ["How does signing work?"]
    assert chat.asks[-1] == {
        "question": "How does signing work?",
        "session_id": chat.asks[-1]["session_id"],
        "keep_turns": 0,
    }

    # The conversation survives a reload.
    chat.reload()
    chat.click("#chat-trigger")
    chat.wait_for_selector(".chat-turn.assistant .mermaid svg")


def test_stop_cancels_on_the_server_and_new_clears(chat):
    ask(chat, "What raises SignatureExpired?")
    chat.wait_for_timeout(300)
    before = chat.backend.started
    chat.click("#chat-send")
    chat.wait_for_selector(".chat-turn.notice")
    assert "Stopped" in chat.inner_text(".chat-turn.notice")
    assert chat.inner_text("#chat-send").strip() == "Ask"
    assert chat.locator("#chat-progress").count() == 0
    time.sleep(DELAY * 3)
    assert chat.backend.started - before <= 1  # only the call already running finished
    assert chat.locator(".chat-turn.notice .chat-actions button").all_inner_texts() == ["Retry"]
    chat.click(".chat-turn.notice .chat-action[title=Retry]")
    wait_idle(chat)
    assert chat.locator(".chat-turn.notice").count() == 0
    assert chat.locator(".chat-turn.assistant").count() == 1

    # New while thinking stops it, and the stopped question leaves no trace.
    ask(chat, "One more?")
    chat.click("#chat-new")
    wait_idle(chat)
    assert chat.locator(".chat-turn").count() == 0
