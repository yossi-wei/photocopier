"""Preconditions. Refusing to run is a feature.

The most dangerous failure in this system is delivering to an unmounted share: on macOS
the mount point still exists as an empty local directory, so a naive copy writes the
entire backlog onto the boot disk and reports complete success. check_mount_live is the
guard against that, and it is why a marker file is required rather than just a path test.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import GuardError

# Path components that indicate a cloud-sync root. A spool inside one of these would
# feed the tool its own output.
SYNC_ROOT_EXACT = {
    "Dropbox", "Google Drive", "GoogleDrive", "iCloud Drive", "iCloudDrive", "Box Sync",
}
SYNC_ROOT_PREFIXES = ("OneDrive",)
SYNC_ROOT_CONTAINS = {"CloudStorage", "Mobile Documents"}


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    fatal: bool = True

    def render(self) -> str:
        mark = "ok  " if self.ok else ("FAIL" if self.fatal else "warn")
        return f"[{mark}] {self.name}: {self.detail}"


def looks_like_sync_root(path: Path) -> str | None:
    """Return the offending path component, or None."""
    for part in path.parts:
        if part in SYNC_ROOT_EXACT or part in SYNC_ROOT_CONTAINS:
            return part
        if any(part.startswith(prefix) for prefix in SYNC_ROOT_PREFIXES):
            return part
    return None


def check_spool_not_in_sync_root(spool_root: Path) -> None:
    resolved = spool_root.expanduser().absolute()
    if offender := looks_like_sync_root(resolved):
        raise GuardError(
            f"spool path {resolved} is inside a cloud-sync folder ({offender!r}). "
            "The spool must not be synced — the tool would feed itself its own output. "
            "Set [spool].path or $PHOTOCOPIER_SPOOL to a local, unsynced directory."
        )


def check_same_filesystem(first: Path, second: Path) -> None:
    """incoming/ and outbox/ must share a device so restructuring is rename(), not copy."""
    try:
        first_dev = first.stat().st_dev
        second_dev = second.stat().st_dev
    except OSError as exc:
        raise GuardError(f"cannot stat spool directories: {exc}") from exc
    if first_dev != second_dev:
        raise GuardError(
            f"{first} and {second} are on different filesystems. Processing would copy "
            "every byte instead of renaming. Keep the whole spool on one volume."
        )


def check_mount_live(mount_point: Path, marker: str) -> None:
    """Verify a network share is genuinely mounted, not just an empty directory."""
    if not mount_point.exists():
        raise GuardError(f"destination mount point {mount_point} does not exist")
    if not os.path.ismount(mount_point):
        raise GuardError(
            f"{mount_point} exists but is not a mount point — the share is not mounted. "
            "Delivering now would write to the local disk and look like success."
        )
    marker_path = mount_point / marker
    if not marker_path.exists():
        raise GuardError(
            f"{mount_point} is mounted but the marker file {marker!r} is missing. "
            f"Create it with: touch {marker_path}"
        )


def check_free_space(path: Path, needed_bytes: int) -> None:
    import shutil

    probe = path if path.exists() else path.parent
    free = shutil.disk_usage(probe).free
    if free < needed_bytes:
        raise GuardError(
            f"not enough free space at {probe}: need {human_bytes(needed_bytes)}, "
            f"have {human_bytes(free)}"
        )


def human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"
