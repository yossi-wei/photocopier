"""Typed errors. Every failure the user can cause should surface as one of these."""

from __future__ import annotations

# Enough rclone stderr to name the failure, bounded so one bad listing cannot flood the
# report, the CLI line, or the launcher's failure email.
STDERR_TAIL_LINES = 3
STDERR_TAIL_CHARS = 500


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

    def report(self) -> str:
        kept = [line.strip() for line in self.stderr.splitlines() if line.strip()]
        tail = " | ".join(kept[-STDERR_TAIL_LINES:])
        if len(kept) > STDERR_TAIL_LINES or len(tail) > STDERR_TAIL_CHARS:
            tail = "... " + tail[-STDERR_TAIL_CHARS:]
        return " | ".join(part for part in (str(self), tail) if part)


class LedgerError(PhotocopierError):
    """The ledger is unreadable, or an illegal state transition was attempted."""
