"""Environment-driven configuration.

Every tunable lives here and arrives from the environment. Importing this module
has exactly one side effect: it reads ``.env`` if present. Nothing else runs at
import time, so tests can construct their own ``Settings`` without touching the
filesystem.

Why the mock/live split matters: stages 1-9 are developed against the synthetic
generator, so credentials are optional while ``MOCK_MODE=1``. The moment live data
is switched on they become mandatory, and failing loudly at startup is far cheaper
than discovering a missing key halfway through a ten-year backfill.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the repo root regardless of the caller's working directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / ".env")


class ConfigError(RuntimeError):
    """Raised when required configuration is absent or malformed."""


def _env(key: str, default: str | None = None) -> str | None:
    value = os.getenv(key, default)
    return value.strip() if isinstance(value, str) else value


def _env_bool(key: str, default: bool) -> bool:
    raw = _env(key)
    if raw is None or raw == "":
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _parse_bbox(raw: str) -> tuple[float, float, float, float]:
    """Parse a ``west,south,east,north`` string as FIRMS expects it."""
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 4:
        raise ConfigError(
            f"INDIA_BBOX must be 'west,south,east,north', got {raw!r}"
        )
    try:
        west, south, east, north = (float(p) for p in parts)
    except ValueError as exc:
        raise ConfigError(f"INDIA_BBOX contains a non-numeric value: {raw!r}") from exc
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ConfigError(
            f"INDIA_BBOX is not a valid west<east, south<north box: {raw!r}"
        )
    return west, south, east, north


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    mock_mode: bool
    firms_map_key: str | None
    eog_username: str | None
    eog_password: str | None
    india_bbox: tuple[float, float, float, float]
    data_dir: Path
    log_level: str
    repo_root: Path = field(default=_REPO_ROOT)

    @property
    def bbox_str(self) -> str:
        """The bbox formatted for a FIRMS area request."""
        w, s, e, n = self.india_bbox
        return f"{w:g},{s:g},{e:g},{n:g}"

    @property
    def mock_dir(self) -> Path:
        return self.data_dir / "mock"


#: Variables that are only required once live data is switched on.
LIVE_REQUIRED = ("FIRMS_MAP_KEY", "EOG_USERNAME", "EOG_PASSWORD")


def load_settings() -> Settings:
    """Build a ``Settings`` from the current environment.

    Raises ``ConfigError`` naming the offending variable rather than failing
    later with an opaque ``None``.
    """
    mock_mode = _env_bool("MOCK_MODE", True)

    database_url = _env("DATABASE_URL")
    if not database_url:
        raise ConfigError(
            "DATABASE_URL is not set. Copy .env.example to .env and edit it."
        )

    if not mock_mode:
        missing = [k for k in LIVE_REQUIRED if not _env(k)]
        if missing:
            raise ConfigError(
                "MOCK_MODE=0 requires live credentials, but these are empty: "
                + ", ".join(missing)
                + ". Set them in .env, or set MOCK_MODE=1 to use synthetic data."
            )

    data_dir = Path(_env("DATA_DIR", "./data") or "./data")
    if not data_dir.is_absolute():
        data_dir = (_REPO_ROOT / data_dir).resolve()

    log_level = (_env("LOG_LEVEL", "INFO") or "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError(f"LOG_LEVEL must be a standard level name, got {log_level!r}")

    return Settings(
        database_url=database_url,
        mock_mode=mock_mode,
        firms_map_key=_env("FIRMS_MAP_KEY") or None,
        eog_username=_env("EOG_USERNAME") or None,
        eog_password=_env("EOG_PASSWORD") or None,
        india_bbox=_parse_bbox(_env("INDIA_BBOX", "68,6,98,37") or "68,6,98,37"),
        data_dir=data_dir,
        log_level=log_level,
    )


_cached: Settings | None = None


def settings() -> Settings:
    """Process-wide settings, built once on first use."""
    global _cached
    if _cached is None:
        _cached = load_settings()
    return _cached
