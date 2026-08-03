"""EXIF capture-date extraction.

EXIF datetimes are local time at the moment of capture, with no timezone attached.
That is exactly what is wanted here, because the destination library is filed by local
date. No offset arithmetic is applied, and none is needed.

This is also why EXIF beats the filename: Pixel filenames encode UTC, so a photo taken
at 22:10 on 31 October is named `PXL_20261101_021009424` and would be filed a month
late. Measured against the existing hand-filed library, the filename tier disagreed on
3 of 2268 files, all of them near a month boundary. The EXIF tier disagreed on none.

`read_tags` is a deliberate seam: it is the only function that touches exifread, so the
date-selection logic can be tested exhaustively without needing real image files.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import exifread

# exifread logs directly to the root handler for ordinary, expected conditions — a PNG
# without EXIF prints "PNG file does not have exif data" to the console. Those files are
# handled by falling through to the next tier, so the message is noise that would
# corrupt command output. Silenced at import because there is no per-call control.
logging.getLogger("exifread").setLevel(logging.CRITICAL)

# Ordered by authority. DateTimeOriginal is when the shutter fired; DateTime is when
# the file was last written, which is still better than nothing but can drift if the
# image was edited.
DATE_TAGS = ("DateTimeOriginal", "DateTimeDigitized", "DateTime")

EXIF_FORMAT = "%Y:%m:%d %H:%M:%S"

# Matches the plausibility window used elsewhere. A zeroed or garbage EXIF timestamp
# must be treated as no answer rather than a confident wrong one.
MIN_YEAR = 1990
MAX_YEAR = 2100


def read_tags(path: Path) -> dict[str, str]:
    """Return EXIF tags keyed by their short name, e.g. 'DateTimeOriginal'.

    Returns an empty dict for anything unreadable, non-image, or without EXIF. A file
    we cannot parse is not an error — it routes to triage like any other undated file.
    """
    try:
        with path.open("rb") as handle:
            raw = exifread.process_file(handle, details=False)
    except (OSError, ValueError, IndexError, KeyError, TypeError, MemoryError):
        # exifread is not defensive about malformed input and can raise a variety of
        # errors on truncated or corrupt files.
        return {}
    # Keys arrive as 'EXIF DateTimeOriginal' or 'Image DateTime'; the group prefix is
    # not meaningful for our purposes.
    return {str(key).split()[-1]: str(value) for key, value in raw.items()}


def date_from_tags(tags: dict[str, str]) -> tuple[datetime | None, str | None]:
    """Pick the most authoritative usable capture time. Returns (datetime, tag name)."""
    for tag in DATE_TAGS:
        raw = tags.get(tag)
        if not raw:
            continue
        try:
            parsed = datetime.strptime(raw.strip(), EXIF_FORMAT)
        except ValueError:
            continue
        if MIN_YEAR <= parsed.year <= MAX_YEAR:
            return parsed, tag
    return None, None


def capture_datetime(path: Path) -> tuple[datetime | None, str | None]:
    """Capture time for a local file, or (None, None) if it cannot be determined."""
    return date_from_tags(read_tags(path))
