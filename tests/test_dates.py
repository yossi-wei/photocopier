"""Date resolution, phase 1 tiers.

The 1969 case is drawn from the real library: a folder produced by zeroed timestamps.
It must stay unresolvable rather than being believed.
"""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest

from photocopier.dates import (
    coarse_date,
    date_from_filename,
    date_from_path,
    is_before_cutoff,
    is_on_or_after,
)
from photocopier.ingest import ingest
from photocopier.ledger import Ledger
from photocopier.process import process
from photocopier.rclone import Rclone

from .conftest import FIXTURES, populate

CAMERA_ROLL = "Camera Roll"
STRIPPED_JPEG = "1J1A3188.jpg"


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
        ("Screenshot_20250608-073110.png", date(2025, 6, 8)),
        ("1776778281073.mp4", date(2026, 4, 21)),
        ("img_6_1749913720949.jpg", date(2025, 6, 14)),
    ],
)
@pytest.mark.usefixtures("eastern")
def test_date_from_filename(filename: str, expected: date) -> None:
    assert date_from_filename(filename) == expected


@pytest.mark.usefixtures("eastern")
def test_pxl_name_without_six_digit_time_keeps_its_date() -> None:
    assert date_from_filename("PXL_20260601_1127.jpg") == date(2026, 6, 1)


@pytest.mark.usefixtures("eastern")
def test_pxl_dash_separated_name_keeps_its_date() -> None:
    assert date_from_filename("PXL-20260601-112702768.jpg") == date(2026, 6, 1)


@pytest.mark.usefixtures("eastern")
def test_home_zone_rule_files_a_video_shot_abroad_on_new_years_day_in_the_old_year() -> None:
    assert date_from_filename("PXL_20270101_010000000.mp4") == date(2026, 12, 31)


@pytest.mark.parametrize(
    "filename",
    [
        "1J1A3188.jpg",           # camera-generated, no date in the name
        "holiday.jpg",
        "PXL_20261301_120000.jpg",  # month 13
        "PXL_20260632_120000.jpg",  # day 32
        "19700101_000000.jpg",      # epoch zero, below MIN_YEAR
        "0000000000000.mp4",        # zeroed epoch, not a plausible timestamp
        "9999999999999.mp4",        # 13 digits, but not starting with 1
        "img_6_12345.jpg",          # short number is a counter, not an epoch
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


class TestUntil:
    def test_no_bound_admits_everything(self) -> None:
        assert not is_on_or_after(
            "2030/01/PXL_20300101_120000.jpg", "PXL_20300101_120000.jpg", None
        )

    def test_bound_is_exclusive(self) -> None:
        assert is_on_or_after(
            "2025/08/IMG_20250801_000000.jpg", "IMG_20250801_000000.jpg", date(2025, 8, 1)
        )
        assert not is_on_or_after(
            "2025/07/IMG_20250731_235959.jpg", "IMG_20250731_235959.jpg", date(2025, 8, 1)
        )

    def test_undatable_files_are_always_admitted(self) -> None:
        assert not is_on_or_after("1969/1J1A3188.jpg", "1J1A3188.jpg", date(2025, 8, 1))


def ingest_then_process(config, spool) -> None:
    with Ledger(spool.ledger_path) as ledger:
        ingest(config, Rclone(config.rclone), ledger, spool)
    with Ledger(spool.ledger_path) as ledger:
        process(config, ledger, spool)


def outbox(spool) -> set[str]:
    return {str(p.relative_to(spool.outbox)) for p in spool.outbox.rglob("*") if p.is_file()}


def place(remote_root: Path, rel: str, fixture: str) -> None:
    target = remote_root / CAMERA_ROLL / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(FIXTURES / fixture, target)


@pytest.mark.usefixtures("eastern")
def test_pixel_video_from_new_years_eve_is_filed_in_the_year_it_was_taken(
    config, spool, remote_root
):
    populate(remote_root / CAMERA_ROLL, {"2026/12/PXL_20270101_030000000.mp4": 512})
    ingest_then_process(config, spool)
    assert outbox(spool) == {"video/2026/PXL_20270101_030000000_phone-1.mp4"}


@pytest.mark.usefixtures("eastern")
def test_pixel_photo_without_exif_is_filed_in_the_local_month(config, spool, remote_root):
    place(remote_root, "2026/10/PXL_20261101_021009424.jpg", STRIPPED_JPEG)
    ingest_then_process(config, spool)
    assert outbox(spool) == {"photos/2026/10/PXL_20261101_021009424_phone-1.jpg"}


@pytest.mark.usefixtures("eastern")
def test_epoch_ms_photo_is_filed_in_the_local_month(config, spool, remote_root):
    place(remote_root, "2026/10/1793499009000.jpg", STRIPPED_JPEG)
    ingest_then_process(config, spool)
    assert outbox(spool) == {"photos/2026/10/1793499009000_phone-1.jpg"}
