from __future__ import annotations

import os
from pathlib import Path

import pytest

from photocopier import config as config_module
from photocopier.ledger import Ledger
from photocopier.metrics import ENV_SCHEDULED
from photocopier.rclone import Rclone
from photocopier.spool import Spool

FAKE_RCLONE = Path(__file__).parent / "fake_rclone" / "rclone"
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let the developer's own environment leak into a test."""
    for name in (
        config_module.ENV_SPOOL,
        config_module.ENV_CONFIG,
        ENV_SCHEDULED,
        "FAKE_RCLONE_FAIL_ON",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def remote_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The tree the fake rclone serves, standing in for OneDrive."""
    root = tmp_path / "remote"
    root.mkdir()
    monkeypatch.setenv("FAKE_RCLONE_ROOT", str(root))
    return root


@pytest.fixture
def spool_root(tmp_path: Path) -> Path:
    return tmp_path / "spool"


def make_config(
    spool_root: Path,
    *,
    sources: list[dict] | None = None,
    cap_gb: int = 20,
    nas: Path | None = None,
):
    nas = nas or Path("/tmp/nas")
    raw = {
        "spool": {"path": str(spool_root), "cap_gb": cap_gb},
        "destination": {
            "photos_root": str(nas / "photos"),
            "video_root": str(nas / "video"),
            "mount_point": str(nas),
        },
        "rclone": {"remote": "onedrive", "binary": str(FAKE_RCLONE), "transfers": 2},
        "source": sources
        or [{"id": "phone-1", "path": "Camera Roll", "suffix": "phone-1"}],
    }
    return config_module.from_dict(raw)


@pytest.fixture
def config(spool_root: Path):
    return make_config(spool_root)


@pytest.fixture
def spool(config) -> Spool:
    spool = Spool.from_config(config.spool)
    spool.ensure()
    return spool


@pytest.fixture
def rclone(config) -> Rclone:
    return Rclone(config.rclone)


@pytest.fixture
def ledger(spool: Spool):
    with Ledger(spool.ledger_path) as ledger:
        yield ledger


def write_file(path: Path, content: bytes = b"x" * 1024) -> Path:
    """Create a fixture file, parents included."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def populate(root: Path, spec: dict[str, int]) -> None:
    """Create files under root: {relative_path: size_in_bytes}."""
    for relative, size in spec.items():
        write_file(root / relative, os.urandom(size) if size else b"")


@pytest.fixture
def nas(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in for the mounted share, with the marker file present.

    os.path.ismount is patched because a temp directory is not a real mount point. The
    guard's behaviour when that check *fails* is tested separately and unpatched — that
    is the case that matters.
    """
    root = tmp_path / "nas"
    (root / "photos").mkdir(parents=True)
    (root / "video").mkdir(parents=True)
    (root / ".photocopier-marker").touch()

    real_ismount = os.path.ismount
    monkeypatch.setattr(
        os.path, "ismount", lambda p: True if Path(p) == root else real_ismount(p)
    )
    return root


@pytest.fixture
def wired(spool_root: Path, nas: Path, remote_root: Path):
    config = make_config(spool_root, nas=nas)
    spool = Spool.from_config(config.spool)
    spool.ensure()
    return config, spool, nas, remote_root
