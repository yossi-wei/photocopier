"""Configuration loading and validation.

No machine-specific value is ever baked in: everything resolves from the config file
or the environment. See D7 for why the spool default is XDG-style rather than a temp
directory.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .errors import ConfigError

ENV_CONFIG = "PHOTOCOPIER_CONFIG"
ENV_SPOOL = "PHOTOCOPIER_SPOOL"

DEFAULT_SPOOL = "~/.local/share/photocopier/spool"
DEFAULT_CAP_GB = 20
DEFAULT_MOUNT_MARKER = ".photocopier-marker"

DEFAULT_PHOTO_EXTS = ("jpg", "jpeg", "heic", "heif", "png", "dng", "gif", "tif", "tiff", "webp")
DEFAULT_VIDEO_EXTS = ("mp4", "mov", "m4v", "avi", "mkv", "3gp")


def expand(value: str | os.PathLike[str]) -> Path:
    """Expand ~ and $VARS. Never returns a path with machine-specific text baked in."""
    return Path(os.path.expanduser(os.path.expandvars(str(value))))


@dataclass(frozen=True)
class SourceConfig:
    id: str
    path: str
    suffix: str
    cutoff: date | None = None


@dataclass(frozen=True)
class SpoolConfig:
    path: Path
    cap_gb: int

    @property
    def cap_bytes(self) -> int:
        return self.cap_gb * 1024**3


@dataclass(frozen=True)
class DestinationConfig:
    photos_root: Path
    video_root: Path
    mount_point: Path
    mount_marker: str


@dataclass(frozen=True)
class RcloneConfig:
    remote: str
    binary: str = "rclone"
    transfers: int = 4
    bwlimit: str = ""


@dataclass(frozen=True)
class ClassifyConfig:
    photo_exts: frozenset[str]
    video_exts: frozenset[str]

    def kind(self, filename: str) -> str:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext in self.photo_exts:
            return "photo"
        if ext in self.video_exts:
            return "video"
        return "unknown"


@dataclass(frozen=True)
class Config:
    spool: SpoolConfig
    destination: DestinationConfig
    rclone: RcloneConfig
    classify: ClassifyConfig
    sources: tuple[SourceConfig, ...]
    source_file: Path | None = None

    def source(self, source_id: str) -> SourceConfig:
        for src in self.sources:
            if src.id == source_id:
                return src
        known = ", ".join(s.id for s in self.sources) or "(none configured)"
        raise ConfigError(f"unknown source {source_id!r}; configured sources: {known}")


def find_config_file(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve which config file to use, in precedence order."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(expand(explicit))
    elif env := os.environ.get(ENV_CONFIG):
        candidates.append(expand(env))
    else:
        candidates.append(Path.cwd() / "config.toml")
        candidates.append(expand("~/.config/photocopier/config.toml"))

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    tried = "\n  ".join(str(c) for c in candidates)
    raise ConfigError(f"no config file found. Looked in:\n  {tried}")


def load(explicit: str | os.PathLike[str] | None = None) -> Config:
    path = find_config_file(explicit)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from exc
    return from_dict(raw, source_file=path)


def from_dict(raw: dict[str, Any], *, source_file: Path | None = None) -> Config:
    """Build and validate a Config. Kept separate from load() so tests need no file."""
    spool = _spool(raw.get("spool", {}))
    destination = _destination(raw.get("destination", {}))
    rclone = _rclone(raw.get("rclone", {}))
    classify = _classify(raw.get("classify", {}))
    sources = _sources(raw.get("source", []))
    return Config(
        spool=spool,
        destination=destination,
        rclone=rclone,
        classify=classify,
        sources=sources,
        source_file=source_file,
    )


def _spool(raw: dict[str, Any]) -> SpoolConfig:
    # Environment wins over config, so a NAS port or a test can redirect the spool
    # without editing a file.
    path = os.environ.get(ENV_SPOOL) or raw.get("path") or DEFAULT_SPOOL
    cap_gb = raw.get("cap_gb", DEFAULT_CAP_GB)
    if not isinstance(cap_gb, int) or cap_gb <= 0:
        raise ConfigError(f"[spool].cap_gb must be a positive integer, got {cap_gb!r}")
    return SpoolConfig(path=expand(path), cap_gb=cap_gb)


def _destination(raw: dict[str, Any]) -> DestinationConfig:
    missing = [k for k in ("photos_root", "video_root", "mount_point") if not raw.get(k)]
    if missing:
        raise ConfigError(f"[destination] missing required key(s): {', '.join(missing)}")
    return DestinationConfig(
        photos_root=expand(raw["photos_root"]),
        video_root=expand(raw["video_root"]),
        mount_point=expand(raw["mount_point"]),
        mount_marker=raw.get("mount_marker", DEFAULT_MOUNT_MARKER),
    )


def _rclone(raw: dict[str, Any]) -> RcloneConfig:
    remote = raw.get("remote")
    if not remote:
        raise ConfigError("[rclone].remote is required (the name of your rclone remote)")
    transfers = raw.get("transfers", 4)
    if not isinstance(transfers, int) or transfers <= 0:
        raise ConfigError(f"[rclone].transfers must be a positive integer, got {transfers!r}")
    return RcloneConfig(
        remote=str(remote),
        binary=raw.get("binary", "rclone"),
        transfers=transfers,
        bwlimit=str(raw.get("bwlimit", "")),
    )


def _classify(raw: dict[str, Any]) -> ClassifyConfig:
    photo = raw.get("photo_exts", DEFAULT_PHOTO_EXTS)
    video = raw.get("video_exts", DEFAULT_VIDEO_EXTS)
    photo_set = frozenset(e.lower().lstrip(".") for e in photo)
    video_set = frozenset(e.lower().lstrip(".") for e in video)
    if overlap := photo_set & video_set:
        raise ConfigError(
            f"[classify] extension(s) in both photo_exts and video_exts: {sorted(overlap)}"
        )
    return ClassifyConfig(photo_exts=photo_set, video_exts=video_set)


def _sources(raw: Any) -> tuple[SourceConfig, ...]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError("at least one [[source]] block is required")

    sources: list[SourceConfig] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        where = f"[[source]] #{index + 1}"
        for key in ("id", "path", "suffix"):
            if not entry.get(key):
                raise ConfigError(f"{where} missing required key {key!r}")

        source_id = str(entry["id"])
        if source_id in seen:
            raise ConfigError(f"{where} duplicate source id {source_id!r}; ids must be unique")
        seen.add(source_id)

        suffix = str(entry["suffix"])
        if "/" in suffix or "\\" in suffix:
            raise ConfigError(f"{where} suffix {suffix!r} must not contain path separators")

        sources.append(
            SourceConfig(
                id=source_id,
                path=str(entry["path"]).strip("/"),
                suffix=suffix,
                cutoff=_cutoff(entry.get("cutoff"), where),
            )
        )
    return tuple(sources)


def _cutoff(value: Any, where: str) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ConfigError(f"{where} cutoff {value!r} is not an ISO date (YYYY-MM-DD)") from exc
