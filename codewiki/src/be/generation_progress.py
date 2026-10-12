"""Progress of the documentation phase: done / total, running, ETA, pauses.

The documentation phase is nearly all of a run's time, so after every module
(and sub-module and overview page) the reporter prints one status line:

    ✓ Signing (2m 12s) · 5/12 done (42%) · 4 running · ETA ~4m

The total grows while the run goes, because agents split modules into
sub-modules as they work. Two kinds of pause are reported as they happen,
so a waiting run does not look hung:

* claude-code / codex subscription limits (caw waits until they reset)
* HTTP 429 from an API provider (the client retries after a delay)

``GenerationProgress`` is thread-safe: caw sub-agents finish on worker
threads. The run's reporter is also registered as the active one so the
HTTP hooks and the caw log handler, which have no reference to it, can find it.
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Finishing faster than this means the page already existed (a resumed run):
# such modules count as done but stay out of the time-per-module average.
_CACHED_SECONDS = 2.0
_THROTTLE_NOTE_EVERY = 60.0
_CAW_LIMIT = re.compile(r"Usage limit reached\. Auto-waiting (\d+) min")

_active: GenerationProgress | None = None
_active_lock = threading.Lock()


def format_duration(seconds: float) -> str:
    seconds = int(round(max(0.0, seconds)))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s" if minutes < 10 else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


class GenerationProgress:
    def __init__(
        self,
        concurrency: int = 1,
        emit: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        active: Callable[[], int] | None = None,
    ) -> None:
        self.concurrency = max(1, int(concurrency or 1))
        # Sessions talking to the model (AgentSlots.active). Without it, the
        # count of started-but-unfinished modules, which also includes ones
        # waiting for a slot or on their sub-modules.
        self._active = active
        self._emit = emit or logger.info
        self._clock = clock
        self._lock = threading.Lock()
        self._started = clock()
        self.total = 0
        self.done = 0
        self.failed = 0
        self.running = 0
        self._timings: list[tuple[str, float]] = []
        self._paused_until: float | None = None
        self._throttled = 0
        self._last_throttle_note = float("-inf")

    # -- counting ------------------------------------------------------

    def add(self, count: int = 1) -> None:
        """More work appeared (the initial tree, or an agent's sub-modules)."""
        with self._lock:
            self.total += count

    @contextlib.contextmanager
    def track(self, name: str) -> Iterator[None]:
        """Count one module from start to finish; the status line prints at the end."""
        with self._lock:
            self.running += 1
        started = self._clock()
        ok = False
        try:
            yield
            ok = True
        finally:
            seconds = self._clock() - started
            with self._lock:
                self.running -= 1
                if ok:
                    self.done += 1
                else:
                    self.failed += 1
                if seconds >= _CACHED_SECONDS:
                    self._timings.append((name, seconds))
                status = self._status_locked()
            if not ok:
                self._emit(f"✗ {name} failed after {format_duration(seconds)} · {status}")
            elif seconds < _CACHED_SECONDS:
                self._emit(f"✓ {name} (already documented) · {status}")
            else:
                self._emit(f"✓ {name} ({format_duration(seconds)}) · {status}")

    # -- estimates -----------------------------------------------------

    def eta_seconds(self) -> float | None:
        with self._lock:
            return self._eta_locked()

    def _eta_locked(self) -> float | None:
        if not self._timings:
            return None
        remaining = max(0, self.total - self.done - self.failed)
        if remaining == 0:
            return 0.0
        average = sum(s for _, s in self._timings) / len(self._timings)
        eta = remaining * average / min(self.concurrency, remaining)
        if self._paused_until is not None:
            eta += max(0.0, self._paused_until - self._clock())
        return eta

    def _status_locked(self) -> str:
        finished = self.done + self.failed
        total = max(self.total, finished)
        percent = int(100 * finished / total) if total else 100
        parts = [f"{finished}/{total} done ({percent}%)"]
        if self.failed:
            parts.append(f"{self.failed} failed")
        running = self._running_locked()
        if running:
            parts.append(f"{running} running")
        eta = self._eta_locked()
        if eta is None:
            parts.append("ETA after the first module")
        elif eta > 0:
            parts.append(f"ETA ~{format_duration(eta)}")
        if self._paused_until is not None and self._paused_until > self._clock():
            parts.append("paused (usage limit)")
        return " · ".join(parts)

    def _running_locked(self) -> int:
        return self._active() if self._active is not None else self.running

    def status(self) -> str:
        with self._lock:
            return self._status_locked()

    # -- pauses --------------------------------------------------------

    def note_usage_limit(self, wait_minutes: int) -> None:
        """A subscription CLI hit its usage limit and sleeps until it resets."""
        with self._lock:
            until = self._clock() + wait_minutes * 60
            first = self._paused_until is None or self._paused_until <= self._clock()
            self._paused_until = max(self._paused_until or 0.0, until)
            waiting = self._running_locked()
        if first:
            resume = (datetime.now() + timedelta(minutes=wait_minutes)).strftime("%H:%M")
            who = f"{waiting} agent(s) waiting, the run resumes" if waiting else "the run resumes"
            self._emit(
                f"⏸ Usage limit reached: paused until ~{resume} ({wait_minutes} min); "
                f"{who} by itself"
            )

    def note_throttled(self, retry_after: str | None = None) -> None:
        """The API provider answered HTTP 429; the client retries after a delay."""
        with self._lock:
            self._throttled += 1
            now = self._clock()
            if now - self._last_throttle_note < _THROTTLE_NOTE_EVERY:
                return
            self._last_throttle_note = now
            count = self._throttled
        wait = f" in {retry_after}s" if retry_after else ""
        self._emit(
            f"⏳ Rate-limited by the provider (HTTP 429, {count} so far): retrying{wait}. "
            "A lower --concurrency avoids this."
        )

    # -- summary -------------------------------------------------------

    def summary(self) -> list[str]:
        with self._lock:
            elapsed = self._clock() - self._started
            slowest = sorted(self._timings, key=lambda t: t[1], reverse=True)[:3]
            lines = [
                f"Documented {self.done} module page(s) in {format_duration(elapsed)} "
                f"at concurrency {self.concurrency}"
                + (f"; {self.failed} failed" if self.failed else "")
                + (f"; {self._throttled} rate-limited request(s)" if self._throttled else "")
            ]
            if slowest:
                lines.append(
                    "Slowest: " + ", ".join(f"{name} ({format_duration(s)})" for name, s in slowest)
                )
        return lines

    # -- the run's active reporter --------------------------------------

    @contextlib.contextmanager
    def activate(self) -> Iterator[GenerationProgress]:
        """Make this the reporter the HTTP and caw hooks report to."""
        global _active
        handler = _CawLimitHandler()
        caw_logger = logging.getLogger("codewiki._vendor.caw.agent")
        with _active_lock:
            previous, _active = _active, self
        caw_logger.addHandler(handler)
        try:
            yield self
        finally:
            caw_logger.removeHandler(handler)
            with _active_lock:
                _active = previous


class _NullProgress(GenerationProgress):
    """Stand-in when no run is being tracked (tests, the updater)."""

    def __init__(self) -> None:
        super().__init__(emit=lambda _msg: None)


NULL_PROGRESS = _NullProgress()


def progress_of(owner: object) -> GenerationProgress:
    """The reporter attached to a backend or deps object, or a no-op one."""
    progress = getattr(owner, "progress", None)
    return progress if isinstance(progress, GenerationProgress) else NULL_PROGRESS


class _CawLimitHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        match = _CAW_LIMIT.search(record.getMessage())
        if match and _active is not None:
            _active.note_usage_limit(int(match[1]))


def notify_http_response(status_code: int, retry_after: str | None) -> None:
    """Called by the HTTP clients' response hooks for every model response."""
    if status_code == 429 and _active is not None:
        _active.note_throttled(retry_after)
