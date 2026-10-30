"""Small helpers shared by every client that reads data from outside the process (ADR-037).

* ``loads``: json.loads that reports absurd nesting as a ValueError instead of crashing with RecursionError.
* ``NETWORK_ERRORS``: everything a flaky or hostile server can make an HTTP call raise (http.client's errors are not OSErrors).
* ``read_capped``: read a response up to a size limit under a *total* deadline, so a server that dribbles bytes cannot hold a worker open.
"""

from __future__ import annotations

import http.client
import json
import time
from typing import Any, Union

NETWORK_ERRORS = (OSError, TimeoutError, http.client.HTTPException)
CHUNK = 65536


def loads(raw: Union[str, bytes, bytearray]) -> Any:
    try:
        return json.loads(raw)
    except RecursionError:
        raise ValueError("JSON is nested too deeply") from None


def read_capped(resp, limit: int, total_s: float = 30.0) -> bytes:
    """Read up to ``limit`` bytes from an http.client-style response, giving up after ``total_s`` seconds in all."""
    deadline = time.monotonic() + total_s
    parts, size = [], 0
    while size < limit:
        if time.monotonic() > deadline:
            raise TimeoutError("the response took too long")
        chunk = resp.read1(min(CHUNK, limit - size)) if hasattr(resp, "read1") else resp.read(min(CHUNK, limit - size))
        if not chunk:
            break
        parts.append(chunk)
        size += len(chunk)
    return b"".join(parts)
