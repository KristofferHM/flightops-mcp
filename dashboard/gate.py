"""The passcode gate. A quick-tunnel URL is public, and whoever holds it can spend credits.

With a passcode, every route but the login page needs a signed cookie. Without one
there is no gate, so the board binds to localhost and refuses any other `Host`: a tunnel
pointed at localhost arrives with the tunnel's hostname and is turned away.

The signing key is generated at startup and never stored, so no secret lives in the
source and a restart signs everyone out.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import deque
from collections.abc import Callable
from typing import Final

COOKIE_NAME: Final = "flightops_board"
SESSION_TTL_S: Final = 12 * 3600
LOCAL_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1"})
LOCAL_BIND: Final = "127.0.0.1"
# One shared limit: behind a tunnel every visitor can look like the same address.
MAX_FAILURES: Final = 10
FAILURE_WINDOW_S: Final = 60.0


def bind_host(requested: str, passcode: str | None) -> str:
    """Where to listen. With no passcode that is localhost, whatever was asked for."""
    return requested if passcode else LOCAL_BIND


def host_name(header: str) -> str:
    """The name in a `Host` header, without its port or IPv6 brackets."""
    if header.startswith("["):
        return header[1:].partition("]")[0].lower()
    return header.partition(":")[0].lower()


class Gate:
    def __init__(self, passcode: str | None, *, clock: Callable[[], float] = time.time) -> None:
        self._passcode = passcode.encode("utf-8") if passcode else None
        self._key = secrets.token_bytes(32)
        self._clock = clock
        self._failures: deque[float] = deque()

    @property
    def enabled(self) -> bool:
        return self._passcode is not None

    def _signature(self, expires: str) -> str:
        return hmac.new(self._key, expires.encode("ascii"), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        expires = str(int(self._clock()) + SESSION_TTL_S)
        return f"{expires}.{self._signature(expires)}"

    def valid(self, cookie: str | None) -> bool:
        if not cookie:
            return False
        expires, _, signature = cookie.partition(".")
        if not expires.isascii() or not expires.isdigit():
            return False
        if not hmac.compare_digest(self._signature(expires), signature):
            return False
        return int(expires) > self._clock()

    def locked_out(self) -> bool:
        cutoff = self._clock() - FAILURE_WINDOW_S
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()
        return len(self._failures) >= MAX_FAILURES

    def attempt(self, offered: str) -> bool:
        """Check a passcode. A failure counts towards the lockout."""
        if self._passcode is None:
            return False
        if hmac.compare_digest(self._passcode, offered.encode("utf-8")):
            return True
        self._failures.append(self._clock())
        return False
