"""The validate command — checking the resolver against an already-filed library."""

from __future__ import annotations

import shutil
from pathlib import Path

from photocopier.config import ClassifyConfig
from photocopier.validate import validate

from .conftest import FIXTURES

CLASSIFY = ClassifyConfig(
    photo_exts=frozenset({"jpg", "jpeg", "png"}), video_exts=frozenset({"mp4", "mov"})
)


def filed(root: Path, rel: str, fixture: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(FIXTURES / fixture, target)


class TestAgreement:
    def test_correctly_filed_photo_agrees(self, tmp_path: Path) -> None:
        filed(tmp_path, "2026/10/PXL_20261101_021009424.jpg", "PXL_20261101_021009424.jpg")

        result = validate(tmp_path, CLASSIFY)

        assert result.agree == 1
        assert result.disagree == 0
        assert result.tiers == {"exif": 1}

    def test_misfiled_photo_disagrees(self, tmp_path: Path) -> None:
        """Filed under November, but EXIF says 31 October."""
        filed(tmp_path, "2026/11/PXL_20261101_021009424.jpg", "PXL_20261101_021009424.jpg")

        result = validate(tmp_path, CLASSIFY)

        assert result.disagree == 1
        assert "filed 2026/11" in result.disagreements[0]

    def test_undated_file_counts_as_unresolved(self, tmp_path: Path) -> None:
        filed(tmp_path, "2026/07/1J1A3188.jpg", "1J1A3188.jpg")

        result = validate(tmp_path, CLASSIFY)

        assert result.unresolved == 1
        assert result.agree == 0
        assert "1J1A3188.jpg" in result.unresolved_examples[0]

    def test_does_not_read_the_answer_off_the_folder(self, tmp_path: Path) -> None:
        """The srcpath tier must be excluded, or validation would grade its own label.

        A file with no recoverable date sitting in 2026/07 must come back unresolved,
        not 'agree', otherwise the measurement is circular.
        """
        filed(tmp_path, "2026/07/1J1A3188.jpg", "1J1A3188.jpg")
        assert validate(tmp_path, CLASSIFY).agree == 0


class TestLayouts:
    def test_flat_year_layout(self, tmp_path: Path) -> None:
        """The video share files by year only, with no month folders."""
        (tmp_path / "2024").mkdir(parents=True)
        (tmp_path / "2024" / "PXL_20240315_101500.mp4").write_bytes(b"x")

        result = validate(tmp_path, CLASSIFY)

        assert result.agree == 1
        assert result.tiers == {"filename": 1}

    def test_year_month_and_flat_years_together(self, tmp_path: Path) -> None:
        (tmp_path / "2024").mkdir(parents=True)
        (tmp_path / "2024" / "PXL_20240315_101500.mp4").write_bytes(b"x")
        filed(tmp_path, "2026/10/PXL_20261101_021009424.jpg", "PXL_20261101_021009424.jpg")

        assert validate(tmp_path, CLASSIFY).agree == 2

    def test_non_year_directories_are_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "Yossi photos").mkdir(parents=True)
        (tmp_path / "Yossi photos" / "old.jpg").write_bytes(b"x")
        filed(tmp_path, "2026/10/PXL_20261101_021009424.jpg", "PXL_20261101_021009424.jpg")

        assert validate(tmp_path, CLASSIFY).checked == 1


class TestSkipping:
    def test_unknown_types_are_skipped_not_counted_wrong(self, tmp_path: Path) -> None:
        (tmp_path / "2026" / "10").mkdir(parents=True)
        (tmp_path / "2026" / "10" / "Thumbs.db").write_bytes(b"x")
        (tmp_path / "2026" / "10" / "notes.txt").write_bytes(b"x")

        result = validate(tmp_path, CLASSIFY)

        assert result.checked == 0
        assert result.skipped == 2

    def test_limit_stops_early(self, tmp_path: Path) -> None:
        for i in range(5):
            filed(tmp_path, f"2026/10/copy{i}.jpg", "PXL_20261101_021009424.jpg")

        assert validate(tmp_path, CLASSIFY, limit=2).checked <= 2


def test_empty_tree_is_not_an_error(tmp_path: Path) -> None:
    result = validate(tmp_path, CLASSIFY)
    assert result.checked == 0
    assert result.agreement == 0.0
