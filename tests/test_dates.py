"""Date resolution, phase 1 tiers.

The 1969 case is drawn from the real library: a folder produced by zeroed timestamps.
It must stay unresolvable rather than being believed.
"""

from __future__ import annotations

from datetime import date

import pytest

from photocopier.dates import (
    coarse_date,
    date_from_filename,
    date_from_path,
    is_before_cutoff,
)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("PXL_20260601_112702768.jpg", date(2026, 6, 1)),
        ("PXL_20260601_223332404~2.jpg", date(2026, 6, 1)),
        ("PXL_20260601_223639008.PANO.jpg", date(2026, 6, 1)),
        ("IMG_20221231_235959.jpg", date(2022, 12, 31)),
        ("VID_20240103_101500.mp4", date(2024, 1, 3)),
        ("20210706_142116.jpg", date(2021, 7, 6)),
        ("2024-07-04 12.00.00.jpg", date(2024, 7, 4)),
        ("Signal-2024-07-04-120000.jpg", date(2024, 7, 4)),
    ],
)
def test_date_from_filename(filename: str, expected: date) -> None:
    assert date_from_filename(filename) == expected


@pytest.mark.parametrize(
    "filename",
    [
        "1J1A3188.jpg",           # camera-generated, no date in the name
        "holiday.jpg",
        "PXL_20261301_120000.jpg",  # month 13
        "PXL_20260632_120000.jpg",  # day 32
        "19700101_000000.jpg",      # epoch zero, below MIN_YEAR
        "",
    ],
)
def test_date_from_filename_rejects_implausible(filename: str) -> None:
    assert date_from_filename(filename) is None


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("2026/06/PXL_20260601_112702768.jpg", date(2026, 6, 1)),
        ("2025/12/photo.jpg", date(2025, 12, 1)),
        ("2024/1/photo.jpg", date(2024, 1, 1)),
    ],
)
def test_date_from_path(path: str, expected: date) -> None:
    assert date_from_path(path) == expected


def test_date_from_path_rejects_1969_folder() -> None:
    """The real library has a 1969/ folder created by zeroed timestamps."""
    assert date_from_path("1969/photo.jpg") is None


def test_date_from_path_rejects_impossible_month() -> None:
    assert date_from_path("2026/13/photo.jpg") is None


def test_filename_wins_over_path() -> None:
    """The filename is the more specific signal when both are present."""
    resolved, source = coarse_date("2026/06/PXL_20240704_120000.jpg", "PXL_20240704_120000.jpg")
    assert resolved == date(2024, 7, 4)
    assert source == "filename"


def test_falls_back_to_path() -> None:
    resolved, source = coarse_date("2026/06/1J1A3188.jpg", "1J1A3188.jpg")
    assert resolved == date(2026, 6, 1)
    assert source == "srcpath"


def test_reports_no_date_when_unresolvable() -> None:
    resolved, source = coarse_date("1969/1J1A3188.jpg", "1J1A3188.jpg")
    assert resolved is None
    assert source == "none"


class TestCutoff:
    def test_no_cutoff_admits_everything(self) -> None:
        assert not is_before_cutoff(
            "2020/01/PXL_20200101_120000.jpg", "PXL_20200101_120000.jpg", None
        )

    def test_excludes_confidently_older(self) -> None:
        assert is_before_cutoff(
            "2025/12/PXL_20251231_120000.jpg", "PXL_20251231_120000.jpg", date(2026, 1, 1)
        )

    def test_admits_on_or_after_cutoff(self) -> None:
        assert not is_before_cutoff(
            "2026/01/PXL_20260101_120000.jpg", "PXL_20260101_120000.jpg", date(2026, 1, 1)
        )

    def test_undatable_files_are_always_admitted(self) -> None:
        """Excluding a file we cannot date would be a silent, permanent skip.

        This is the same failure class as --max-age, rejected in D1: undatable material
        must come down and reach triage, where a human decides.
        """
        assert not is_before_cutoff("1969/1J1A3188.jpg", "1J1A3188.jpg", date(2026, 1, 1))
