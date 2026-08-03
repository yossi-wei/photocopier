from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from photocopier.layout import destination_for, file_digest, same_content, suffixed_name


class TestNaming:
    def test_suffix_goes_before_the_extension(self) -> None:
        assert (
            suffixed_name("PXL_20260601_112702768.jpg", "phone-1")
            == "PXL_20260601_112702768_phone-1.jpg"
        )

    def test_suffix_last_preserves_chronological_sort(self) -> None:
        """Why the suffix trails rather than leads: merged folders still sort by time."""
        names = [
            suffixed_name("PXL_20260601_120000.jpg", "phone-2"),
            suffixed_name("PXL_20260601_110000.jpg", "phone-1"),
            suffixed_name("PXL_20260602_090000.jpg", "phone-1"),
        ]
        assert sorted(names) == [
            "PXL_20260601_110000_phone-1.jpg",
            "PXL_20260601_120000_phone-2.jpg",
            "PXL_20260602_090000_phone-1.jpg",
        ]

    def test_compound_camera_extensions_survive(self) -> None:
        """Real Pixel output: .MP, .PANO and .NIGHT are part of the name, not extensions."""
        assert (
            suffixed_name("PXL_20260601_223639008.PANO.jpg", "phone-1")
            == "PXL_20260601_223639008.PANO_phone-1.jpg"
        )

    def test_edited_variant_marker_survives(self) -> None:
        """`~2` marks a distinct edit made by the phone, not a duplicate."""
        assert (
            suffixed_name("PXL_20260601_223332404~2.jpg", "phone-1")
            == "PXL_20260601_223332404~2_phone-1.jpg"
        )

    def test_no_extension(self) -> None:
        assert suffixed_name("IMG_0001", "phone-1") == "IMG_0001_phone-1"

    def test_uppercase_extension_is_preserved(self) -> None:
        assert suffixed_name("VIDEO.MP4", "phone-2") == "VIDEO_phone-2.MP4"


class TestDestination:
    def test_photos_go_to_year_and_month(self) -> None:
        dest = destination_for("a.jpg", "photo", date(2026, 6, 1), "phone-1")
        assert str(dest.relative) == "photos/2026/06/a_phone-1.jpg"
        assert dest.bucket == "photos"

    def test_video_goes_to_year_only(self) -> None:
        """The video share has no month folders. This predates the tool by two decades."""
        dest = destination_for("a.mp4", "video", date(2026, 6, 1), "phone-1")
        assert str(dest.relative) == "video/2026/a_phone-1.mp4"
        assert dest.bucket == "video"

    def test_month_is_zero_padded(self) -> None:
        dest = destination_for("a.jpg", "photo", date(2026, 1, 9), "phone-1")
        assert str(dest.relative) == "photos/2026/01/a_phone-1.jpg"

    def test_december(self) -> None:
        dest = destination_for("a.jpg", "photo", date(2025, 12, 31), "phone-2")
        assert str(dest.relative) == "photos/2025/12/a_phone-2.jpg"

    def test_two_phones_never_collide(self) -> None:
        """Identical filenames from two phones are routine and must stay distinct."""
        first = destination_for("PXL_1.jpg", "photo", date(2026, 6, 1), "phone-1")
        second = destination_for("PXL_1.jpg", "photo", date(2026, 6, 1), "phone-2")
        assert first.relative != second.relative


class TestContentComparison:
    def test_identical_files(self, tmp_path: Path) -> None:
        first, second = tmp_path / "a", tmp_path / "b"
        first.write_bytes(b"same bytes")
        second.write_bytes(b"same bytes")
        assert same_content(first, second)

    def test_different_sizes(self, tmp_path: Path) -> None:
        first, second = tmp_path / "a", tmp_path / "b"
        first.write_bytes(b"short")
        second.write_bytes(b"much longer content")
        assert not same_content(first, second)

    def test_same_size_different_content(self, tmp_path: Path) -> None:
        """Two photos differing only in EXIF often share a size. Size alone is not identity."""
        first, second = tmp_path / "a", tmp_path / "b"
        first.write_bytes(b"AAAABBBB")
        second.write_bytes(b"AAAACCCC")
        assert not same_content(first, second)

    def test_digest_is_stable_across_chunk_boundaries(self, tmp_path: Path) -> None:
        import hashlib

        path = tmp_path / "big"
        payload = bytes(range(256)) * 8192  # 2 MB, spans the streaming chunk size
        path.write_bytes(payload)
        assert file_digest(path) == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize("kind", ["photo", "video"])
def test_destination_is_always_relative(kind: str) -> None:
    """The outbox is a replica; absolute paths would break the move at delivery."""
    dest = destination_for("a.jpg", kind, date(2026, 6, 1), "phone-1")
    assert not str(dest.relative).startswith("/")
