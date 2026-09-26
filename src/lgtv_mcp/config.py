"""Registry of paired TVs, stored as a private JSON file.

The file holds each TV's pairing key, which is a bearer credential for the
TV, so it is written atomically with owner-only permissions and never logged.
"""

import json
import logging
import os
import re
import stat
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError, InvalidInput, NotConfigured, UnknownTv

log = logging.getLogger(__name__)

_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
LEGACY_NAME = "tv"


@dataclass
class TvEntry:
    host: str
    key: str = field(repr=False)
    uuid: str | None = None
    model: str | None = None
    macs: list[str] = field(default_factory=list)


@dataclass
class Config:
    default: str | None = None
    tvs: dict[str, TvEntry] = field(default_factory=dict)

    def get(self, name: str | None) -> tuple[str, TvEntry]:
        """Return (name, entry) for ``name``, or for the default TV when None."""
        if not self.tvs:
            raise NotConfigured()
        if name is None:
            if self.default in self.tvs:
                name = self.default
            elif len(self.tvs) == 1:
                name = next(iter(self.tvs))
            else:
                raise UnknownTv("(default)", list(self.tvs))
        if name not in self.tvs:
            raise UnknownTv(name, list(self.tvs))
        return name, self.tvs[name]

    def add(self, name: str, entry: TvEntry, *, make_default: bool = False) -> None:
        self.tvs[validate_name(name)] = entry
        if make_default or self.default not in self.tvs:
            self.default = name

    def remove(self, name: str) -> None:
        if name not in self.tvs:
            raise UnknownTv(name, list(self.tvs))
        del self.tvs[name]
        if self.default == name:
            self.default = next(iter(sorted(self.tvs)), None)


def validate_name(name: str) -> str:
    if not _NAME.fullmatch(name):
        raise InvalidInput(
            "TV names use 1 to 32 lowercase letters, digits, '-' or '_', "
            "starting with a letter or digit."
        )
    return name


def config_path() -> Path:
    if env := os.environ.get("LGTV_CONFIG"):
        return Path(env).expanduser()
    if sys.platform == "win32" and (appdata := os.environ.get("APPDATA")):
        base = Path(appdata)
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "lgtv" / "config.json"


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        return _migrate_legacy(path) or Config()
    _warn_if_exposed(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, RecursionError) as err:  # JSONDecodeError and UnicodeDecodeError
        raise ConfigError(f"Config file {path} is not valid JSON.") from err
    except OSError as err:
        raise ConfigError(f"Cannot read config file {path}: {err.strerror}.") from err
    return _parse(data, path)


def save(cfg: Config, path: Path | None = None) -> None:
    """Write the config atomically with owner-only permissions."""
    # Write through a symlinked config (dotfile setups) instead of replacing the link.
    path = Path(os.path.realpath(path or config_path()))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = {"default": cfg.default, "tvs": {n: asdict(e) for n, e in sorted(cfg.tvs.items())}}
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            if hasattr(os, "fchmod"):
                os.fchmod(f.fileno(), 0o600)
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def update(change: Callable[[Config], object], path: Path | None = None) -> Config:
    """Load, apply ``change`` and save, so concurrent writers lose less."""
    cfg = load(path)
    change(cfg)
    save(cfg, path)
    return cfg


def _parse(data: Any, path: Path) -> Config:
    bad = ConfigError(f"Config file {path} has an unexpected format.")
    if not isinstance(data, dict) or not isinstance(data.get("tvs", {}), dict):
        raise bad
    default = data.get("default")
    if default is not None and not isinstance(default, str):
        raise bad

    tvs: dict[str, TvEntry] = {}
    for name, raw in data.get("tvs", {}).items():
        if not isinstance(raw, dict) or not _NAME.fullmatch(str(name)):
            raise bad
        host, key = raw.get("host"), raw.get("key")
        macs = raw.get("macs", [])
        if not isinstance(host, str) or not isinstance(key, str) or not isinstance(macs, list):
            raise bad
        tvs[name] = TvEntry(
            host=host,
            key=key,
            uuid=_opt_str(raw.get("uuid")),
            model=_opt_str(raw.get("model")),
            macs=[m for m in macs if isinstance(m, str)],
        )
    return Config(default=default if default in tvs else None, tvs=tvs)


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _migrate_legacy(path: Path) -> Config | None:
    """Import the single-TV ``key`` and ``host`` files used by earlier scripts."""
    key_file, host_file = path.parent / "key", path.parent / "host"
    if not (key_file.is_file() and host_file.is_file()):
        return None
    try:
        key = key_file.read_text(encoding="utf-8").strip()
        host = host_file.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        log.warning("Ignoring unreadable legacy files in %s", path.parent)
        return None
    if not key or not host:
        return None
    cfg = Config()
    cfg.add(LEGACY_NAME, TvEntry(host=host, key=key), make_default=True)
    save(cfg, path)
    log.info("Imported TV '%s' from legacy files in %s", LEGACY_NAME, path.parent)
    return cfg


def _warn_if_exposed(path: Path) -> None:
    if sys.platform == "win32":
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        log.warning(
            "Config file %s is readable by other users (mode %o). Run: chmod 600 %s",
            path,
            mode,
            path,
        )
