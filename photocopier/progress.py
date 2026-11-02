"""A one-line progress display for runs someone is watching.

Shown only when stderr is a terminal. The scheduled launcher captures output into its
log, so unattended runs never see any of this and their logs stay plain text.

The pipeline stages report into a `Task` and know nothing about terminals: `QUIET`, the
default, accepts every call and draws nothing.
"""

from __future__ import annotations

import shutil
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TextIO

from .guards import human_bytes

REFRESH_SECONDS = 0.25
RATE_WINDOW_SECONDS = 10.0
SPINNER = "|/-\\"
CLEAR_LINE = "\r\x1b[K"

# Returns (items done, bytes done) for work that cannot report as it goes, such as an
# rclone transfer: the display samples it on every redraw.
Poll = Callable[[], tuple[int, int]]


class Task:
    def __init__(
        self,
        label: str,
        *,
        items: int | None,
        total_bytes: int | None,
        poll: Poll | None,
        clock: Callable[[], float],
    ):
        self.label = label
        self.items_total = items
        self.bytes_total = total_bytes
        self.items_done = 0
        self.bytes_done = 0
        self.detail = ""
        self.poll = poll
        self._clock = clock
        self._started = clock()
        self._samples: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()

    def current(self, detail: str) -> None:
        """Name what is being worked on now, before it finishes."""
        with self._lock:
            self.detail = detail

    def advance(self, items: int = 1, nbytes: int = 0) -> None:
        with self._lock:
            self.items_done += items
            self.bytes_done += nbytes

    def sample(self) -> None:
        if self.poll is not None:
            try:
                items, nbytes = self.poll()
            except OSError:
                return  # files appearing and vanishing mid-walk; try again next redraw
            with self._lock:
                self.items_done, self.bytes_done = items, nbytes

    def rate(self) -> float:
        """Bytes per second over the last few seconds, or 0 until there is enough data."""
        now = self._clock()
        with self._lock:
            self._samples.append((now, self.bytes_done))
            while self._samples and now - self._samples[0][0] > RATE_WINDOW_SECONDS:
                self._samples.popleft()
            first_time, first_bytes = self._samples[0]
        elapsed = now - first_time
        if elapsed < 1.0:
            return 0.0
        return (self.bytes_done - first_bytes) / elapsed

    def line(self, width: int) -> str:
        rate = self.rate()
        with self._lock:
            parts = [f"{self.label:<7}"]
            if self.items_total is not None:
                parts.append(f"{self.items_done}/{self.items_total}")
            if self.bytes_total:
                done = min(self.bytes_done, self.bytes_total)
                percent = 100 * done // self.bytes_total
                parts.append(f"{human_bytes(done)} / {human_bytes(self.bytes_total)}  {percent}%")
                if rate > 0:
                    left = (self.bytes_total - done) / rate
                    parts.append(f"{human_bytes(int(rate))}/s  ~{_duration(left)} left")
            if self.items_total is None and not self.bytes_total:
                elapsed = self._clock() - self._started
                spin = SPINNER[int(elapsed / REFRESH_SECONDS) % len(SPINNER)]
                parts.append(f"{spin} {_clock_time(elapsed)}")
            if self.detail:
                parts.append(self.detail)
        return "  ".join(parts)[: max(0, width - 1)]


class Progress:
    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        enabled: bool | None = None,
        clock: Callable[[], float] = time.monotonic,
        refresh: float = REFRESH_SECONDS,
    ):
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = _is_terminal(self.stream) if enabled is None else enabled
        self._clock = clock
        self._refresh = refresh

    @contextmanager
    def task(
        self,
        label: str,
        *,
        items: int | None = None,
        total_bytes: int | None = None,
        poll: Poll | None = None,
    ) -> Iterator[Task]:
        """Show one line for the duration of a block, then clear it.

        Redraws run on a thread so a single blocking call (an rclone listing) still
        shows signs of life.
        """
        task = Task(label, items=items, total_bytes=total_bytes, poll=poll, clock=self._clock)
        if not self.enabled or items == 0:
            yield task
            return

        stop = threading.Event()
        thread = threading.Thread(target=self._redraw, args=(task, stop), daemon=True)
        thread.start()
        try:
            yield task
        finally:
            stop.set()
            thread.join()
            self.stream.write(CLEAR_LINE)
            self.stream.flush()

    def _redraw(self, task: Task, stop: threading.Event) -> None:
        while True:
            task.sample()
            width = shutil.get_terminal_size(fallback=(80, 24)).columns
            self.stream.write(CLEAR_LINE + task.line(width))
            self.stream.flush()
            if stop.wait(self._refresh):
                return


QUIET = Progress(enabled=False)


def _is_terminal(stream: TextIO) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


def _clock_time(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"
