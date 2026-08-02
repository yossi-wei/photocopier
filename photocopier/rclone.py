"""Thin wrapper around the rclone binary.

rclone is a transfer engine here, never a sync engine. It is told exactly which files to
move; it is never asked to work out what is new by comparing source against destination.
The spool is emptied after delivery, so a destination comparison would re-download the
entire library on every run. See D1.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import RcloneConfig
from .errors import RcloneError

# rclone reports OneDrive for Business hashes under this key.
PREFERRED_HASH_KEYS = ("QuickXorHash", "SHA256", "SHA1", "MD5")


@dataclass(frozen=True)
class RemoteItem:
    """One file in a remote source folder, as reported by `rclone lsjson`."""

    path: str  # relative to the source root
    name: str
    size: int
    mod_time: str
    hash: str  # '' when the remote reports none — never None, see Ledger for why

    @property
    def parent(self) -> str:
        return str(Path(self.path).parent) if "/" in self.path else ""


class Rclone:
    def __init__(self, config: RcloneConfig):
        self.config = config

    # -- process plumbing ------------------------------------------------

    def _run(
        self, args: list[str], *, timeout: int | None = None
    ) -> subprocess.CompletedProcess[str]:
        argv = [self.config.binary, *args]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as exc:
            raise RcloneError(
                f"rclone binary {self.config.binary!r} not found on PATH. "
                "Install it from https://rclone.org/install/"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RcloneError(f"rclone timed out after {timeout}s: {' '.join(args[:2])}") from exc

        if proc.returncode != 0:
            raise RcloneError(
                f"rclone {args[0]} failed with exit code {proc.returncode}",
                returncode=proc.returncode,
                stderr=proc.stderr.strip(),
            )
        return proc

    def available(self) -> bool:
        return shutil.which(self.config.binary) is not None

    def version(self) -> str:
        out = self._run(["version"], timeout=30).stdout.strip()
        return out.splitlines()[0] if out else "unknown"

    def remotes(self) -> list[str]:
        out = self._run(["listremotes"], timeout=30).stdout
        return [line.strip().rstrip(":") for line in out.splitlines() if line.strip()]

    # -- operations ------------------------------------------------------

    def lsjson(self, source_path: str) -> list[RemoteItem]:
        """List every file under a source folder. Metadata only; downloads nothing."""
        target = f"{self.config.remote}:{source_path}"
        proc = self._run(["lsjson", "--recursive", "--hash", target])
        try:
            entries = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RcloneError(f"could not parse rclone lsjson output: {exc}") from exc

        items: list[RemoteItem] = []
        for entry in entries:
            if entry.get("IsDir"):
                continue
            items.append(
                RemoteItem(
                    path=entry["Path"],
                    name=entry.get("Name") or Path(entry["Path"]).name,
                    size=int(entry.get("Size") or 0),
                    mod_time=str(entry.get("ModTime") or ""),
                    hash=_extract_hash(entry.get("Hashes") or {}),
                )
            )
        return items

    def copy_files(self, source_path: str, dest_dir: Path, rel_paths: list[str]) -> None:
        """Copy an explicit list of files, preserving their relative paths.

        The list is passed via --files-from-raw so that filenames containing commas,
        quotes, or newline-adjacent oddities survive intact.
        """
        if not rel_paths:
            return
        dest_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as handle:
            handle.write("\n".join(rel_paths) + "\n")
            list_path = Path(handle.name)

        args = [
            "copy",
            f"{self.config.remote}:{source_path}",
            str(dest_dir),
            "--files-from-raw",
            str(list_path),
            "--transfers",
            str(self.config.transfers),
        ]
        if self.config.bwlimit:
            args += ["--bwlimit", self.config.bwlimit]

        try:
            self._run(args)
        finally:
            list_path.unlink(missing_ok=True)


def _extract_hash(hashes: dict[str, str]) -> str:
    for key in PREFERRED_HASH_KEYS:
        if value := hashes.get(key):
            return str(value)
    for value in hashes.values():  # any hash beats no hash
        if value:
            return str(value)
    return ""
