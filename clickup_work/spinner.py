from __future__ import annotations

import sys
import threading
from types import TracebackType
from typing import Type

from clickup_work.log import is_verbose

# Braille spinner frames — narrow, monospaced, terminal-safe.
_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_INTERVAL = 0.08  # seconds per frame


class Spinner:
    """Minimal stdlib spinner as a context manager.

    Animates on stderr when stderr is a TTY and verbose mode is off. In
    non-interactive contexts (CI, pipes, `-v`) it degrades to a single
    "→ label…" line so there's still visible progress without mangled
    carriage-return output.

    Usage:
        with Spinner("fetching tickets") as sp:
            tasks = client.get_open_tasks(...)
            sp.ok(f"found {len(tasks)} ticket(s)")

    On clean exit with `.ok(...)` it prints `✓ <msg>`. On exception it
    prints `✗ <label>`. Call `.silent()` to suppress the summary line.
    """

    def __init__(self, label: str):
        self._label = label
        self._final: str | None = label
        self._ok = True
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._animated = sys.stderr.isatty() and not is_verbose()

    def __enter__(self) -> "Spinner":
        if self._animated:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        else:
            # Give non-TTY callers a breadcrumb that work has started.
            print(f"→ {self._label}…", file=sys.stderr, flush=True)
        return self

    def _spin(self) -> None:
        i = 0
        while not self._stop.is_set():
            frame = _FRAMES[i % len(_FRAMES)]
            sys.stderr.write(f"\r{frame} {self._label}")
            sys.stderr.flush()
            i += 1
            # Event.wait returns early if .set() is called — snappy stop.
            self._stop.wait(_INTERVAL)

    def ok(self, message: str | None = None) -> None:
        self._ok = True
        if message is not None:
            self._final = message

    def fail(self, message: str | None = None) -> None:
        self._ok = False
        if message is not None:
            self._final = message

    def silent(self) -> None:
        """Suppress the ✓/✗ summary line on exit."""
        self._final = None

    def __exit__(
        self,
        exc_type: Type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool:
        if self._animated:
            self._stop.set()
            if self._thread is not None:
                self._thread.join()
            # Clear the spinner line before printing the summary.
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()
        if exc_type is not None:
            self._ok = False
        if self._final is not None:
            mark = "✓" if self._ok else "✗"
            print(f"{mark} {self._final}", file=sys.stderr, flush=True)
        return False  # never suppress exceptions
