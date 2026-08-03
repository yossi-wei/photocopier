"""Destination mapping and naming.

The outbox is a byte-exact replica of what will land on the NAS, so this module decides
the final path once and delivery does no thinking at all.

Two buckets with deliberately different shapes, matching a filing convention that
predates this tool by two decades:

    photos/YYYY/MM/<name>
    video/YYYY/<name>        — yearly only, no month folders

The source suffix goes last, before the extension, so filenames still sort
chronologically when two phones are merged into one folder.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath

PHOTOS = "photos"
VIDEO = "video"

_HASH_CHUNK = 1 << 20


@dataclass(frozen=True)
class Destination:
    """Where a file belongs, relative to the outbox root."""

    bucket: str  # 'photos' or 'video'
    relative: PurePosixPath  # e.g. photos/2026/06/PXL_..._phone-1.jpg

    def __str__(self) -> str:
        return str(self.relative)


def suffixed_name(filename: str, suffix: str) -> str:
    """Insert the source suffix before the extension.

    `PXL_20260601_112702768.jpg` + `phone-1` -> `PXL_20260601_112702768_phone-1.jpg`

    Compound extensions produced by the camera are left intact, so
    `PXL_20260601_223639008.PANO.jpg` keeps `.PANO` as part of its stem rather than
    having the suffix injected into the middle of it.
    """
    path = PurePosixPath(filename)
    if not path.suffix:
        return f"{filename}_{suffix}"
    return f"{path.stem}_{suffix}{path.suffix}"


def destination_for(
    filename: str,
    kind: str,
    when: date,
    suffix: str,
) -> Destination:
    """Map a classified, dated file to its place in the outbox."""
    name = suffixed_name(filename, suffix)
    if kind == "video":
        return Destination(VIDEO, PurePosixPath(VIDEO) / f"{when:%Y}" / name)
    return Destination(PHOTOS, PurePosixPath(PHOTOS) / f"{when:%Y}" / f"{when:%m}" / name)


def file_digest(path: Path) -> str:
    """SHA-256 of a local file, streamed so a large video does not land in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def same_content(first: Path, second: Path) -> bool:
    """Whether two files are byte-identical.

    Size is checked first because it rules out the common case without reading either
    file. Two photos that differ only in EXIF have the same size surprisingly often, so
    a size match alone is not treated as identity.
    """
    if first.stat().st_size != second.stat().st_size:
        return False
    return file_digest(first) == file_digest(second)
