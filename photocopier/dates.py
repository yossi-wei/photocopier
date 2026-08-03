"""Date resolution.

Two entry points, because two different amounts of information are available:

- `coarse_date` runs at ingest, before a file has been downloaded. Only the filename
  and the source path exist, so only those tiers can be consulted.
- `resolve` runs at process time, when the bytes are local, and adds EXIF on top as the
  highest-priority tier.

Measured against 2268 hand-filed files in the existing library, the full chain agreed
with the manual filing on 99.16% and disagreed on none; the remainder had no date
recoverable from any tier and belong in triage.

File modification time is deliberately absent and must stay absent: OneDrive and rclone
both rewrite it, so it records when the file moved rather than when the photo was taken.
A confidently wrong date is worse than no date, because wrong dates file silently into
the wrong month while missing dates route to triage. See D4.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .exif import capture_datetime

# Years outside this range are treated as unresolvable rather than believed. The real
# library contains a 1969/ folder produced by zeroed timestamps.
MIN_YEAR = 1990
MAX_YEAR = 2100

# Ordered by confidence. Each must capture year/month/day groups.
FILENAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    # PXL_20260601_112702768.jpg, VID_20240103_101500.mp4, IMG_20221231_235959.jpg
    re.compile(
        r"(?:PXL|IMG|VID|MVIMG|PANO)[_-](?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})[_-]",
        re.IGNORECASE,
    ),
    # 20210706_142116.jpg
    re.compile(r"^(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})[_-]\d{6}"),
    # 2024-07-04 12.00.00.jpg  /  2024-07-04_120000.jpg
    re.compile(r"^(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})[ _-]"),
    # Signal-2024-07-04-120000.jpg and similar prefixed forms
    re.compile(r"[_-](?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})[_-]"),
)

# .../2026/06/... as produced by the OneDrive mobile app's date organization.
PATH_PATTERN = re.compile(r"(?:^|/)(?P<y>\d{4})/(?P<m>\d{1,2})(?:/|$)")


def _build(year: int, month: int, day: int = 1) -> date | None:
    if not (MIN_YEAR <= year <= MAX_YEAR):
        return None
    if not (1 <= month <= 12):
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def date_from_filename(filename: str) -> date | None:
    """Extract a capture date encoded in the filename, if one is plausibly there."""
    for pattern in FILENAME_PATTERNS:
        match = pattern.search(filename)
        if not match:
            continue
        resolved = _build(int(match["y"]), int(match["m"]), int(match["d"]))
        if resolved:
            return resolved
    return None


def date_from_path(path: str) -> date | None:
    """Extract a YYYY/MM bucket from the source path.

    Day is not knowable from a path, so this returns the first of the month. That is
    sufficient for both cutoff comparison and month-level filing.
    """
    match = PATH_PATTERN.search(path.replace("\\", "/"))
    if not match:
        return None
    return _build(int(match["y"]), int(match["m"]))


def coarse_date(path: str, filename: str) -> tuple[date | None, str]:
    """Best date obtainable without downloading the file.

    Returns (date, source) where source is one of 'filename', 'srcpath', or 'none'.
    """
    if resolved := date_from_filename(filename):
        return resolved, "filename"
    if resolved := date_from_path(path):
        return resolved, "srcpath"
    return None, "none"


def resolve(
    local_path: Path,
    src_path: str,
    filename: str,
    *,
    try_exif: bool = True,
) -> tuple[date | None, str]:
    """Full resolution chain for a file that is present on disk.

    Returns (date, source) where source is 'exif', 'filename', 'srcpath', or 'none'.
    A source of 'none' means the file must go to triage — it is never guessed at.

    `try_exif` is False for formats that carry no EXIF (video), which avoids parsing
    every mp4 header to learn nothing.
    """
    if try_exif:
        captured, _tag = capture_datetime(local_path)
        if captured is not None:
            return captured.date(), "exif"
    return coarse_date(src_path, filename)


def is_before_cutoff(path: str, filename: str, cutoff: date | None) -> bool:
    """Whether an item can be *confidently* dated earlier than the cutoff.

    Undatable files always return False, so they are ingested and left for triage.
    Excluding a file we cannot date would be a silent, permanent skip — the same class
    of failure as --max-age, which D1 rejected.
    """
    if cutoff is None:
        return False
    resolved, _ = coarse_date(path, filename)
    if resolved is None:
        return False
    return resolved < cutoff
