"""Typed errors. Every failure the user can cause should surface as one of these."""

from __future__ import annotations


class PhotocopierError(Exception):
    """Base for anything we raise deliberately."""


class ConfigError(PhotocopierError):
    """Configuration is missing, malformed, or internally inconsistent."""


class GuardError(PhotocopierError):
    """A precondition failed. Refusing to proceed is the correct outcome."""


class RcloneError(PhotocopierError):
    """rclone is missing, or exited non-zero."""

    def __init__(self, message: str, *, returncode: int | None = None, stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class LedgerError(PhotocopierError):
    """The ledger is unreadable, or an illegal state transition was attempted."""
