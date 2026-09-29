"""Environment-driven configuration.

Every tunable lives here and arrives from the environment. Importing this module
has exactly one side effect: it reads ``.env`` if present. Nothing else runs at
import time, so tests can construct their own ``Settings`` without touching the
filesystem.

Why no credential is required at startup: real data is the default, and the FIRMS
archive for India is public, so most of the pipeline needs no key at all. Each
credential is demanded by the one client that uses it -- ``require_firms_key()`` for
FIRMS API pulls, ``vnf_enabled`` for the optional VNF enrichment -- and the error
names the missing variable, so a missing key still fails loudly before any work
starts rather than halfway through a backfill.
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


def _parse_gate(raw: str) -> tuple[int, int, int]:
    """Parse ``months,days,years`` for the registry's recurrence gate."""
    parts = [p.strip() for p in raw.split(",")]
    try:
        months, days, years = (int(p) for p in parts)
    except ValueError as exc:
        raise ConfigError(f"REGISTRY_GATE must be 'months,days,years', got {raw!r}") from exc
    if not (1 <= months <= 12 and 1 <= days <= 366 and years >= 1):
        raise ConfigError(f"REGISTRY_GATE is out of range: {raw!r}")
    return months, days, years


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
    # Recurrence gate (months, days, years): a cell must burn in >= months distinct
    # months on >= days distinct days within a year, in >= years years. Two years
    # was approved on 2021-2023; the full-archive recalibration (Stage 3, rule
    # fixed before the sweep) moved it to three: reports/stage3/sweep.md.
    registry_gate: tuple[int, int, int] = (3, 10, 3)
    repo_root: Path = field(default=_REPO_ROOT)

    @property
    def bbox_str(self) -> str:
        """The bbox formatted for a FIRMS area request."""
        w, s, e, n = self.india_bbox
        return f"{w:g},{s:g},{e:g},{n:g}"

    @property
    def mock_dir(self) -> Path:
        return self.data_dir / "mock"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def vnf_enabled(self) -> bool:
        """True when EOG credentials are present.

        VNF is optional enrichment: it is licence-gated and night-only, so the
        pipeline must run end to end without it and simply skip the join.
        """
        return bool(self.eog_username and self.eog_password)

    def require_firms_key(self) -> str:
        """The FIRMS MAP_KEY, or a ``ConfigError`` naming it.

        Only API pulls need it -- 2025 onward and the live NRT feed. The yearly
        archive is public, so nothing asks for the key until an API client runs.
        """
        if not self.firms_map_key:
            raise ConfigError(
                "FIRMS_MAP_KEY is not set. It is needed only for FIRMS API pulls "
                "(2025 onward and live NRT); the yearly archive needs no key. "
                "Get one free at https://firms.modaps.eosdis.nasa.gov/api/map_key/"
            )
        return self.firms_map_key


def load_settings() -> Settings:
    """Build a ``Settings`` from the current environment.

    Raises ``ConfigError`` naming the offending variable rather than failing
    later with an opaque ``None``.
    """
    mock_mode = _env_bool("MOCK_MODE", False)

    database_url = _env("DATABASE_URL")
    if not database_url:
        raise ConfigError(
            "DATABASE_URL is not set. Copy .env.example to .env and edit it."
        )

    # Half a login is a typo, not a choice to skip VNF -- say so now rather than
    # letting the join silently turn itself off.
    eog_user, eog_pass = _env("EOG_USERNAME"), _env("EOG_PASSWORD")
    if bool(eog_user) != bool(eog_pass):
        missing = "EOG_PASSWORD" if eog_user else "EOG_USERNAME"
        raise ConfigError(
            f"{missing} is empty but its partner is set. Set both EOG_USERNAME and "
            "EOG_PASSWORD for VNF enrichment, or leave both empty to run without it."
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
        registry_gate=_parse_gate(_env("REGISTRY_GATE", "3,10,3") or "3,10,3"),
    )


_cached: Settings | None = None


def settings() -> Settings:
    """Process-wide settings, built once on first use."""
    global _cached
    if _cached is None:
        _cached = load_settings()
    return _cached
