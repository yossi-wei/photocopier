from __future__ import annotations

from pathlib import Path

import pytest

from photocopier.errors import GuardError
from photocopier.guards import (
    check_free_space,
    check_mount_live,
    check_same_filesystem,
    check_spool_not_in_sync_root,
    human_bytes,
    looks_like_sync_root,
)


class TestUnmountedShare:
    """The most dangerous failure in the system.

    When an SMB share is unmounted, macOS leaves the mount point behind as an ordinary
    empty directory. A naive delivery writes the entire backlog onto the boot disk and
    reports complete success.
    """

    def test_unmounted_share_is_refused(self, tmp_path: Path) -> None:
        looks_mounted = tmp_path / "Volumes" / "media"
        looks_mounted.mkdir(parents=True)
        (looks_mounted / ".photocopier-marker").touch()  # even with the marker present

        with pytest.raises(GuardError, match="not a mount point"):
            check_mount_live(looks_mounted, ".photocopier-marker")

    def test_absent_mount_point_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(GuardError, match="does not exist"):
            check_mount_live(tmp_path / "nope", ".photocopier-marker")

    def test_error_explains_the_consequence(self, tmp_path: Path) -> None:
        path = tmp_path / "media"
        path.mkdir()
        with pytest.raises(GuardError) as exc:
            check_mount_live(path, ".photocopier-marker")
        assert "local disk" in str(exc.value)

    def test_real_mount_without_marker_is_refused(self) -> None:
        """A genuine mount is not enough: the marker proves it is the right share."""
        with pytest.raises(GuardError, match="marker"):
            check_mount_live(Path("/"), ".photocopier-marker-that-does-not-exist")


class TestSyncRootGuard:
    @pytest.mark.parametrize(
        "path",
        [
            "/Users/someone/OneDrive/spool",
            "/Users/someone/OneDrive - Contoso/spool",
            "/Users/someone/Library/CloudStorage/OneDrive-Contoso/spool",
            "/Users/someone/Dropbox/spool",
            "/Users/someone/Google Drive/spool",
            "/Users/someone/Library/Mobile Documents/spool",
        ],
    )
    def test_sync_roots_are_detected(self, path: str) -> None:
        assert looks_like_sync_root(Path(path)) is not None

    @pytest.mark.parametrize(
        "path",
        [
            "/Users/someone/.local/share/photocopier/spool",
            "/srv/photocopier/spool",
            "/Users/someone/Documents/spool",
        ],
    )
    def test_ordinary_paths_are_allowed(self, path: str) -> None:
        assert looks_like_sync_root(Path(path)) is None
        check_spool_not_in_sync_root(Path(path))

    def test_spool_in_sync_root_is_refused(self) -> None:
        with pytest.raises(GuardError, match="cloud-sync"):
            check_spool_not_in_sync_root(Path("/Users/someone/OneDrive/spool"))

    def test_error_explains_the_feedback_loop(self) -> None:
        with pytest.raises(GuardError) as exc:
            check_spool_not_in_sync_root(Path("/Users/someone/Dropbox/spool"))
        assert "feed itself" in str(exc.value)


class TestSameFilesystem:
    def test_same_volume_passes(self, tmp_path: Path) -> None:
        first, second = tmp_path / "incoming", tmp_path / "outbox"
        first.mkdir()
        second.mkdir()
        check_same_filesystem(first, second)

    def test_missing_directory_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(GuardError, match="cannot stat"):
            check_same_filesystem(tmp_path / "absent", tmp_path)


class TestFreeSpace:
    def test_passes_when_space_is_available(self, tmp_path: Path) -> None:
        check_free_space(tmp_path, 1024)

    def test_refuses_absurd_request(self, tmp_path: Path) -> None:
        with pytest.raises(GuardError, match="not enough free space"):
            check_free_space(tmp_path, 1024**6)


@pytest.mark.parametrize(
    ("count", "expected"),
    [(0, "0 B"), (512, "512 B"), (1024, "1.0 KB"), (1536, "1.5 KB"), (1024**3, "1.0 GB")],
)
def test_human_bytes(count: int, expected: str) -> None:
    assert human_bytes(count) == expected
