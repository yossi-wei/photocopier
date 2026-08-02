from __future__ import annotations

import os
from pathlib import Path

import pytest

from photocopier import config as config_module
from photocopier.ledger import Ledger
from photocopier.rclone import Rclone
from photocopier.spool import Spool

FAKE_RCLONE = Path(__file__).parent / "fake_rclone" / "rclone"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let the developer's own environment leak into a test."""
    for name in (config_module.ENV_SPOOL, config_module.ENV_CONFIG, "FAKE_RCLONE_FAIL_ON"):
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


def make_config(spool_root: Path, *, sources: list[dict] | None = None, cap_gb: int = 20):
    raw = {
        "spool": {"path": str(spool_root), "cap_gb": cap_gb},
        "destination": {
            "photos_root": "/tmp/nas/photos",
            "video_root": "/tmp/nas/video",
            "mount_point": "/tmp/nas",
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
