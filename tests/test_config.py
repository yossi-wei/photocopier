from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from photocopier import config as config_module
from photocopier.config import ENV_SPOOL, Config, from_dict
from photocopier.errors import ConfigError

MINIMAL = {
    "destination": {
        "photos_root": "/nas/photos",
        "video_root": "/nas/video",
        "mount_point": "/nas",
    },
    "rclone": {"remote": "onedrive"},
    "source": [{"id": "phone-1", "path": "Camera Roll", "suffix": "phone-1"}],
}


def test_minimal_config_loads() -> None:
    cfg = from_dict(MINIMAL)
    assert cfg.rclone.remote == "onedrive"
    assert cfg.sources[0].id == "phone-1"
    assert cfg.sources[0].cutoff is None


def test_spool_defaults_are_not_a_temp_directory() -> None:
    """D7: a temp directory would be purged mid-travel, destroying the backlog."""
    cfg = from_dict(MINIMAL)
    resolved = str(cfg.spool.path)
    assert not resolved.startswith("/tmp")
    assert not resolved.startswith("/var/folders")
    assert cfg.spool.path.is_absolute()
    assert cfg.spool.cap_gb == 20


def test_env_overrides_spool_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_SPOOL, "/custom/spool")
    raw = {**MINIMAL, "spool": {"path": "/from/config"}}
    assert from_dict(raw).spool.path == Path("/custom/spool")


def test_tilde_and_vars_expand(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = {**MINIMAL, "spool": {"path": "~/spool"}}
    assert from_dict(raw).spool.path == Path.home() / "spool"


def test_cutoff_parses_iso_date() -> None:
    raw = {**MINIMAL, "source": [{**MINIMAL["source"][0], "cutoff": "2026-01-01"}]}
    assert from_dict(raw).sources[0].cutoff == date(2026, 1, 1)


def test_cutoff_rejects_garbage() -> None:
    raw = {**MINIMAL, "source": [{**MINIMAL["source"][0], "cutoff": "last tuesday"}]}
    with pytest.raises(ConfigError, match="not an ISO date"):
        from_dict(raw)


def test_duplicate_source_ids_are_rejected() -> None:
    """Two sources sharing an id would collide in the ledger and cross-contaminate."""
    raw = {
        **MINIMAL,
        "source": [
            {"id": "phone-1", "path": "Camera Roll", "suffix": "phone-1"},
            {"id": "phone-1", "path": "Pictures/Camera Roll", "suffix": "phone-2"},
        ],
    }
    with pytest.raises(ConfigError, match="duplicate source id"):
        from_dict(raw)


def test_at_least_one_source_required() -> None:
    raw = {**MINIMAL, "source": []}
    with pytest.raises(ConfigError, match="at least one"):
        from_dict(raw)


@pytest.mark.parametrize("missing", ["id", "path", "suffix"])
def test_source_requires_key(missing: str) -> None:
    source = {k: v for k, v in MINIMAL["source"][0].items() if k != missing}
    with pytest.raises(ConfigError, match=f"missing required key {missing!r}"):
        from_dict({**MINIMAL, "source": [source]})


def test_suffix_may_not_contain_path_separators() -> None:
    raw = {**MINIMAL, "source": [{**MINIMAL["source"][0], "suffix": "a/b"}]}
    with pytest.raises(ConfigError, match="path separators"):
        from_dict(raw)


def test_missing_destination_key_is_rejected() -> None:
    raw = {**MINIMAL, "destination": {"photos_root": "/nas/photos"}}
    with pytest.raises(ConfigError, match="missing required key"):
        from_dict(raw)


def test_remote_is_required() -> None:
    with pytest.raises(ConfigError, match="rclone.*remote"):
        from_dict({**MINIMAL, "rclone": {}})


@pytest.mark.parametrize("cap", [0, -1, "20"])
def test_cap_must_be_positive_integer(cap: object) -> None:
    with pytest.raises(ConfigError, match="cap_gb"):
        from_dict({**MINIMAL, "spool": {"cap_gb": cap}})


def test_extension_in_both_lists_is_rejected() -> None:
    raw = {**MINIMAL, "classify": {"photo_exts": ["jpg", "mp4"], "video_exts": ["mp4"]}}
    with pytest.raises(ConfigError, match="both"):
        from_dict(raw)


def test_classify_kind() -> None:
    cfg = from_dict(MINIMAL)
    assert cfg.classify.kind("PXL_20260601_112702768.jpg") == "photo"
    assert cfg.classify.kind("PXL_20260601_112702768.MP4") == "video"
    assert cfg.classify.kind("notes.txt") == "unknown"
    assert cfg.classify.kind("no_extension") == "unknown"


def test_lookup_unknown_source_names_the_known_ones() -> None:
    cfg = from_dict(MINIMAL)
    with pytest.raises(ConfigError, match="phone-1"):
        cfg.source("nobody")


class TestFileLoading:
    def test_loads_from_path(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text(
            """
            [destination]
            photos_root = "/nas/photos"
            video_root = "/nas/video"
            mount_point = "/nas"
            [rclone]
            remote = "onedrive"
            [[source]]
            id = "phone-1"
            path = "Camera Roll"
            suffix = "phone-1"
            """,
            encoding="utf-8",
        )
        cfg = config_module.load(path)
        assert isinstance(cfg, Config)
        assert cfg.source_file == path

    def test_invalid_toml_names_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("this is not toml {{{", encoding="utf-8")
        with pytest.raises(ConfigError, match="invalid TOML"):
            config_module.load(path)

    def test_missing_file_lists_what_it_tried(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="no config file found"):
            config_module.load(tmp_path / "absent.toml")


def test_example_config_is_valid() -> None:
    """The committed example must always parse — it is the onboarding path."""
    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    cfg = config_module.load(example)
    assert len(cfg.sources) == 2
    assert {s.id for s in cfg.sources} == {"phone-1", "phone-2"}
