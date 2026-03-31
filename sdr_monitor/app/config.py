"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

ENV_PREFIX = "SDR_MONITOR_"
VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _read_str(env: Mapping[str, str], key: str, default: str) -> str:
    value = env.get(key)
    if value is None:
        return default
    trimmed = value.strip()
    return trimmed if trimmed else default


def _read_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw_value = env.get(key)
    if raw_value is None:
        return default
    try:
        return int(raw_value.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid integer for {key}: {raw_value!r}") from exc


def _read_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw_value = env.get(key)
    if raw_value is None:
        return default
    try:
        return float(raw_value.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid float for {key}: {raw_value!r}") from exc


@dataclass(frozen=True, slots=True)
class Config:
    """Resolved runtime settings for the monitor service."""

    service_name: str = "sdr-monitor"
    log_level: str = "INFO"
    adsb_window_seconds: float = 8.0
    ais_window_seconds: float = 12.0
    inter_scan_pause_seconds: float = 2.0
    fresh_seconds: int = 30
    aging_seconds: int = 120
    max_positions_per_target: int = 5
    readsb_aircraft_json: Path = Path("/run/readsb/aircraft.json")
    ais_tcp_host: str = "127.0.0.1"
    ais_tcp_port: int = 10110
    sqlite_path: Path = Path("./data/sdr_monitor.sqlite3")
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    radar_center_lat: float = 0.0
    radar_center_lon: float = 0.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env_map = os.environ if env is None else env
        defaults = cls()

        config = cls(
            service_name=_read_str(
                env_map, f"{ENV_PREFIX}SERVICE_NAME", defaults.service_name
            ),
            log_level=_read_str(env_map, f"{ENV_PREFIX}LOG_LEVEL", defaults.log_level).upper(),
            adsb_window_seconds=_read_float(
                env_map, f"{ENV_PREFIX}ADSB_WINDOW_SECONDS", defaults.adsb_window_seconds
            ),
            ais_window_seconds=_read_float(
                env_map, f"{ENV_PREFIX}AIS_WINDOW_SECONDS", defaults.ais_window_seconds
            ),
            inter_scan_pause_seconds=_read_float(
                env_map,
                f"{ENV_PREFIX}INTER_SCAN_PAUSE_SECONDS",
                defaults.inter_scan_pause_seconds,
            ),
            fresh_seconds=_read_int(
                env_map, f"{ENV_PREFIX}FRESH_SECONDS", defaults.fresh_seconds
            ),
            aging_seconds=_read_int(
                env_map, f"{ENV_PREFIX}AGING_SECONDS", defaults.aging_seconds
            ),
            max_positions_per_target=_read_int(
                env_map,
                f"{ENV_PREFIX}MAX_POSITIONS_PER_TARGET",
                defaults.max_positions_per_target,
            ),
            readsb_aircraft_json=Path(
                _read_str(
                    env_map,
                    f"{ENV_PREFIX}READSB_AIRCRAFT_JSON",
                    str(defaults.readsb_aircraft_json),
                )
            ),
            ais_tcp_host=_read_str(env_map, f"{ENV_PREFIX}AIS_TCP_HOST", defaults.ais_tcp_host),
            ais_tcp_port=_read_int(
                env_map,
                f"{ENV_PREFIX}AIS_TCP_PORT",
                defaults.ais_tcp_port,
            ),
            sqlite_path=Path(
                _read_str(env_map, f"{ENV_PREFIX}SQLITE_PATH", str(defaults.sqlite_path))
            ),
            api_host=_read_str(env_map, f"{ENV_PREFIX}API_HOST", defaults.api_host),
            api_port=_read_int(env_map, f"{ENV_PREFIX}API_PORT", defaults.api_port),
            radar_center_lat=_read_float(
                env_map,
                f"{ENV_PREFIX}RADAR_CENTER_LAT",
                _read_float(
                    env_map,
                    f"{ENV_PREFIX}RADAR_LATITUDE",
                    defaults.radar_center_lat,
                ),
            ),
            radar_center_lon=_read_float(
                env_map,
                f"{ENV_PREFIX}RADAR_CENTER_LON",
                _read_float(
                    env_map,
                    f"{ENV_PREFIX}RADAR_LONGITUDE",
                    defaults.radar_center_lon,
                ),
            ),
        )
        config._validate()
        return config

    def _validate(self) -> None:
        if self.log_level not in VALID_LOG_LEVELS:
            valid_levels = ", ".join(sorted(VALID_LOG_LEVELS))
            raise ValueError(
                f"Invalid {ENV_PREFIX}LOG_LEVEL={self.log_level!r}. "
                f"Expected one of: {valid_levels}."
            )
        if self.adsb_window_seconds <= 0:
            raise ValueError(f"{ENV_PREFIX}ADSB_WINDOW_SECONDS must be > 0.")
        if self.ais_window_seconds <= 0:
            raise ValueError(f"{ENV_PREFIX}AIS_WINDOW_SECONDS must be > 0.")
        if self.inter_scan_pause_seconds < 0:
            raise ValueError(f"{ENV_PREFIX}INTER_SCAN_PAUSE_SECONDS must be >= 0.")
        if self.fresh_seconds < 0:
            raise ValueError(f"{ENV_PREFIX}FRESH_SECONDS must be >= 0.")
        if self.aging_seconds <= self.fresh_seconds:
            raise ValueError(
                f"{ENV_PREFIX}AGING_SECONDS must be greater than {ENV_PREFIX}FRESH_SECONDS."
            )
        if self.max_positions_per_target <= 0:
            raise ValueError(f"{ENV_PREFIX}MAX_POSITIONS_PER_TARGET must be > 0.")
        if not (1 <= self.ais_tcp_port <= 65535):
            raise ValueError(f"{ENV_PREFIX}AIS_TCP_PORT must be in the range 1..65535.")
        if not (1 <= self.api_port <= 65535):
            raise ValueError(f"{ENV_PREFIX}API_PORT must be in the range 1..65535.")
        if not (-90 <= self.radar_center_lat <= 90):
            raise ValueError(f"{ENV_PREFIX}RADAR_CENTER_LAT must be in the range -90..90.")
        if not (-180 <= self.radar_center_lon <= 180):
            raise ValueError(f"{ENV_PREFIX}RADAR_CENTER_LON must be in the range -180..180.")


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Load configuration from process environment."""

    return Config.from_env(env)
