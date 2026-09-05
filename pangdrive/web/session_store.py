"""In-memory session storage for the web UI."""

import secrets
import threading
import time


class SessionStore:
    """Store opaque session tokens until their fixed expiry."""

    def __init__(self) -> None:
        self._sessions: dict[str, float] = {}
        self._lock = threading.Lock()

    def create(self, ttl: int = 86400) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = time.time() + ttl
        return token

    def validate(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            expiry = self._sessions.get(token)
            if expiry is None:
                return False
            if expiry <= time.time():
                del self._sessions[token]
                return False
            return True

    def revoke(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._sessions.pop(token, None)
