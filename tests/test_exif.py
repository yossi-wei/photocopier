"""EXIF extraction.

Tag-selection logic is tested against dicts, which is exhaustive and fast. Integration
with exifread is tested separately against real (tiny) JPEGs in tests/fixtures, so a
change in the library's key format or return shape is caught rather than mocked away.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from photocopier.exif import capture_datetime, date_from_tags, read_tags

FIXTURES = Path(__file__).parent / "fixtures"


class TestTagSelection:
    def test_prefers_date_time_original(self) -> None:
        when, tag = date_from_tags(
            {
                "DateTimeOriginal": "2026:06:01 11:27:02",
                "DateTimeDigitized": "2026:06:02 11:27:02",
                "DateTime": "2026:06:03 11:27:02",
            }
        )
        assert when == datetime(2026, 6, 1, 11, 27, 2)
        assert tag == "DateTimeOriginal"

    def test_falls_back_to_digitized(self) -> None:
        when, tag = date_from_tags({"DateTimeDigitized": "2023:07:04 12:00:00"})
        assert when == datetime(2023, 7, 4, 12, 0, 0)
        assert tag == "DateTimeDigitized"

    def test_falls_back_to_datetime(self) -> None:
        when, tag = date_from_tags({"DateTime": "2024:03:15 08:00:00"})
        assert tag == "DateTime"
        assert when == datetime(2024, 3, 15, 8, 0, 0)

    def test_no_date_tags(self) -> None:
        assert date_from_tags({"Make": "Google", "Model": "Pixel 9"}) == (None, None)

    def test_empty(self) -> None:
        assert date_from_tags({}) == (None, None)

    @pytest.mark.parametrize(
        "value",
        ["not a date", "", "2026-06-01 11:27:02", "2026:13:45 99:99:99", "0000:00:00 00:00:00"],
    )
    def test_unparseable_values_are_not_guessed_at(self, value: str) -> None:
        assert date_from_tags({"DateTimeOriginal": value}) == (None, None)

    def test_skips_bad_tag_and_uses_the_next(self) -> None:
        """One corrupt tag must not discard a perfectly good one further down."""
        when, tag = date_from_tags(
            {"DateTimeOriginal": "garbage", "DateTime": "2024:03:15 08:00:00"}
        )
        assert tag == "DateTime"
        assert when == datetime(2024, 3, 15, 8, 0, 0)

    @pytest.mark.parametrize("value", ["1970:01:01 00:00:00", "1899:01:01 00:00:00"])
    def test_implausible_years_are_rejected(self, value: str) -> None:
        """A zeroed timestamp is the 1969-folder case: no answer beats a wrong one."""
        assert date_from_tags({"DateTimeOriginal": value}) == (None, None)


class TestRealFiles:
    def test_reads_date_time_original(self) -> None:
        when, tag = capture_datetime(FIXTURES / "PXL_20261101_021009424.jpg")
        assert tag == "DateTimeOriginal"
        assert when == datetime(2026, 10, 31, 22, 10, 9)

    def test_exif_beats_a_utc_filename(self) -> None:
        """The case that motivated the EXIF tier.

        Pixel filenames are UTC. This photo was taken at 22:10 local on 31 October and
        is named for 1 November. Filing on the filename puts it in the wrong month.
        """
        when, _tag = capture_datetime(FIXTURES / "PXL_20261101_021009424.jpg")
        assert when is not None
        assert (when.year, when.month, when.day) == (2026, 10, 31)

    def test_only_datetime_present(self) -> None:
        when, tag = capture_datetime(FIXTURES / "only_datetime.jpg")
        assert tag == "DateTime"
        assert when == datetime(2024, 3, 15, 8, 0, 0)

    def test_only_digitized_present(self) -> None:
        when, tag = capture_datetime(FIXTURES / "only_digitized.jpg")
        assert tag == "DateTimeDigitized"

    def test_stripped_file_has_no_date(self) -> None:
        """Mirrors the 1J1A files in the real library: EXIF present, no date tags."""
        assert capture_datetime(FIXTURES / "1J1A3188.jpg") == (None, None)

    def test_zeroed_timestamp_is_refused(self) -> None:
        assert capture_datetime(FIXTURES / "zeroed.jpg") == (None, None)

    def test_garbage_date_is_refused(self) -> None:
        assert capture_datetime(FIXTURES / "garbage_date.jpg") == (None, None)


class TestUnreadable:
    def test_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        """Unreadable input routes to triage like any other undated file."""
        assert read_tags(tmp_path / "absent.jpg") == {}
        assert capture_datetime(tmp_path / "absent.jpg") == (None, None)

    def test_not_an_image(self, tmp_path: Path) -> None:
        path = tmp_path / "notes.jpg"
        path.write_text("this is plain text pretending to be a photo")
        assert capture_datetime(path) == (None, None)

    def test_truncated_jpeg(self, tmp_path: Path) -> None:
        path = tmp_path / "truncated.jpg"
        path.write_bytes((FIXTURES / "PXL_20261101_021009424.jpg").read_bytes()[:40])
        assert capture_datetime(path) == (None, None)

    def test_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.jpg"
        path.touch()
        assert capture_datetime(path) == (None, None)


def test_png_without_exif_is_silent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """exifread logs 'PNG file does not have exif data' to the console for a case we
    handle by falling through to the next tier. That noise would corrupt command output.
    """
    png = tmp_path / "Screenshot_20260220-162939.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

    assert capture_datetime(png) == (None, None)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
