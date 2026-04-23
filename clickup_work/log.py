from __future__ import annotations

import sys
import time

_verbose = False
_start = time.monotonic()


def set_verbose(on: bool) -> None:
    global _verbose
    _verbose = on


def is_verbose() -> bool:
    return _verbose


def vlog(msg: str) -> None:
    if _verbose:
        elapsed = time.monotonic() - _start
        print(f"[verbose +{elapsed:5.2f}s] {msg}", file=sys.stderr)
