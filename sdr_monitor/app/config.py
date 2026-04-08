"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import mkdtemp
from typing import Mapping

ENV_PREFIX = "SDR_MONITOR_"
VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
VALID_MAP_SOURCES = {"hydro", "elevation"}


def _default_map_cache_dir() -> Path:
    root = Path(mkdtemp(prefix="sdr-monitor-", dir="/tmp"))
    cache_dir = root / "data" / "map" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


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


def _read_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw_value = env.get(key)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean for {key}: {raw_value!r}")


@dataclass(frozen=True, slots=True)
class Config:
    """Resolved runtime settings for the monitor service."""

    service_name: str = "sdr-monitor"
    log_level: str = "INFO"
    adsb_window_seconds: float = 8.0
    ogn_window_seconds: float = 0.0
    ais_window_seconds: float = 12.0
    dsc_window_seconds: float = 0.0
    inter_scan_pause_seconds: float = 2.0
    fresh_seconds: int = 30
    aging_seconds: int = 120
    max_positions_per_target: int = 5
    readsb_aircraft_json: Path = Path("/run/readsb/aircraft.json")
    ogn_tcp_host: str = "127.0.0.1"
    ogn_tcp_port: int = 50001
    ais_tcp_host: str = "127.0.0.1"
    ais_tcp_port: int = 10110
    dsc_tcp_host: str = "127.0.0.1"
    dsc_tcp_port: int = 6021
    dsc_rtl_host: str = "127.0.0.1"
    dsc_rtl_port: int = 1234
    dsc_rtl_sample_rate: int = 48000
    dsc_rtl_gain: int = 30
    sqlite_path: Path = Path("./data/sdr_monitor.sqlite3")
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    radar_center_lat: float = 0.0
    radar_center_lon: float = 0.0
    fixed_objects_path: Path = Path("./data/fixed_objects.json")
    map_source: str = "hydro"
    map_cache_ttl_seconds: int = 600
    map_cache_dir: Path = field(default_factory=_default_map_cache_dir)
    hydro_base_url: str = "https://api.lantmateriet.se/ogc-features/v1/hydrografi"
    hydro_username: str = ""
    hydro_password: str = ""
    markhojd_direct_base_url: str = "https://api.lantmateriet.se/distribution/produkter/markhojd/v1"
    markhojd_direct_username: str = ""
    markhojd_direct_password: str = ""
    markhojd_direct_srid: int = 3006
    markhojd_direct_sample_step_m: int = 25
    markhojd_direct_contour_interval_m: int = 10
    markhojd_direct_max_points_per_request: int = 1000

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
            ogn_window_seconds=_read_float(
                env_map, f"{ENV_PREFIX}OGN_WINDOW_SECONDS", defaults.ogn_window_seconds
            ),
            ais_window_seconds=_read_float(
                env_map, f"{ENV_PREFIX}AIS_WINDOW_SECONDS", defaults.ais_window_seconds
            ),
            dsc_window_seconds=_read_float(
                env_map, f"{ENV_PREFIX}DSC_WINDOW_SECONDS", defaults.dsc_window_seconds
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
            ogn_tcp_host=_read_str(env_map, f"{ENV_PREFIX}OGN_TCP_HOST", defaults.ogn_tcp_host),
            ogn_tcp_port=_read_int(
                env_map,
                f"{ENV_PREFIX}OGN_TCP_PORT",
                defaults.ogn_tcp_port,
            ),
            ais_tcp_host=_read_str(env_map, f"{ENV_PREFIX}AIS_TCP_HOST", defaults.ais_tcp_host),
            ais_tcp_port=_read_int(
                env_map,
                f"{ENV_PREFIX}AIS_TCP_PORT",
                defaults.ais_tcp_port,
            ),
            dsc_tcp_host=_read_str(env_map, f"{ENV_PREFIX}DSC_TCP_HOST", defaults.dsc_tcp_host),
            dsc_tcp_port=_read_int(
                env_map,
                f"{ENV_PREFIX}DSC_TCP_PORT",
                defaults.dsc_tcp_port,
            ),
            dsc_rtl_host=_read_str(env_map, f"{ENV_PREFIX}DSC_RTL_HOST", defaults.dsc_rtl_host),
            dsc_rtl_port=_read_int(
                env_map,
                f"{ENV_PREFIX}DSC_RTL_PORT",
                defaults.dsc_rtl_port,
            ),
            dsc_rtl_sample_rate=_read_int(
                env_map,
                f"{ENV_PREFIX}DSC_RTL_SAMPLE_RATE",
                defaults.dsc_rtl_sample_rate,
            ),
            dsc_rtl_gain=_read_int(
                env_map,
                f"{ENV_PREFIX}DSC_RTL_GAIN",
                defaults.dsc_rtl_gain,
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
            fixed_objects_path=Path(
                _read_str(
                    env_map,
                    f"{ENV_PREFIX}FIXED_OBJECTS_PATH",
                    str(defaults.fixed_objects_path),
                )
            ),
            map_source=_read_str(
                env_map,
                f"{ENV_PREFIX}MAP_SOURCE",
                defaults.map_source,
            ).lower(),
            map_cache_ttl_seconds=_read_int(
                env_map,
                f"{ENV_PREFIX}MAP_CACHE_TTL_SECONDS",
                defaults.map_cache_ttl_seconds,
            ),
            map_cache_dir=Path(
                _read_str(
                    env_map,
                    f"{ENV_PREFIX}MAP_CACHE_DIR",
                    str(defaults.map_cache_dir),
                )
            ),
            hydro_base_url=_read_str(
                env_map,
                f"{ENV_PREFIX}HYDRO_BASE_URL",
                defaults.hydro_base_url,
            ),
            hydro_username=_read_str(
                env_map,
                f"{ENV_PREFIX}HYDRO_USERNAME",
                defaults.hydro_username,
            ),
            hydro_password=_read_str(
                env_map,
                f"{ENV_PREFIX}HYDRO_PASSWORD",
                defaults.hydro_password,
            ),
            markhojd_direct_base_url=_read_str(
                env_map,
                f"{ENV_PREFIX}MARKHOJD_DIRECT_BASE_URL",
                defaults.markhojd_direct_base_url,
            ),
            markhojd_direct_username=_read_str(
                env_map,
                f"{ENV_PREFIX}MARKHOJD_DIRECT_USERNAME",
                _read_str(
                    env_map,
                    f"{ENV_PREFIX}ELEVATION_USERNAME",
                    defaults.markhojd_direct_username,
                ),
            ),
            markhojd_direct_password=_read_str(
                env_map,
                f"{ENV_PREFIX}MARKHOJD_DIRECT_PASSWORD",
                _read_str(
                    env_map,
                    f"{ENV_PREFIX}ELEVATION_PASSWORD",
                    defaults.markhojd_direct_password,
                ),
            ),
            markhojd_direct_srid=_read_int(
                env_map,
                f"{ENV_PREFIX}MARKHOJD_DIRECT_SRID",
                defaults.markhojd_direct_srid,
            ),
            markhojd_direct_sample_step_m=_read_int(
                env_map,
                f"{ENV_PREFIX}MARKHOJD_DIRECT_SAMPLE_STEP_M",
                defaults.markhojd_direct_sample_step_m,
            ),
            markhojd_direct_contour_interval_m=_read_int(
                env_map,
                f"{ENV_PREFIX}MARKHOJD_DIRECT_CONTOUR_INTERVAL_M",
                defaults.markhojd_direct_contour_interval_m,
            ),
            markhojd_direct_max_points_per_request=_read_int(
                env_map,
                f"{ENV_PREFIX}MARKHOJD_DIRECT_MAX_POINTS_PER_REQUEST",
                defaults.markhojd_direct_max_points_per_request,
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
        if self.ogn_window_seconds < 0:
            raise ValueError(f"{ENV_PREFIX}OGN_WINDOW_SECONDS must be >= 0.")
        if not (1 <= self.ogn_tcp_port <= 65535):
            raise ValueError(f"{ENV_PREFIX}OGN_TCP_PORT must be in the range 1..65535.")
        if self.ais_window_seconds <= 0:
            raise ValueError(f"{ENV_PREFIX}AIS_WINDOW_SECONDS must be > 0.")
        if self.dsc_window_seconds < 0:
            raise ValueError(f"{ENV_PREFIX}DSC_WINDOW_SECONDS must be >= 0.")
        if not (1 <= self.dsc_tcp_port <= 65535):
            raise ValueError(f"{ENV_PREFIX}DSC_TCP_PORT must be in the range 1..65535.")
        if not (1 <= self.dsc_rtl_port <= 65535):
            raise ValueError(f"{ENV_PREFIX}DSC_RTL_PORT must be in the range 1..65535.")
        if self.dsc_rtl_sample_rate <= 0:
            raise ValueError(f"{ENV_PREFIX}DSC_RTL_SAMPLE_RATE must be > 0.")
        if not (0 <= self.dsc_rtl_gain <= 50):
            raise ValueError(f"{ENV_PREFIX}DSC_RTL_GAIN must be in the range 0..50.")
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
        if not (1 <= self.dsc_tcp_port <= 65535):
            raise ValueError(f"{ENV_PREFIX}DSC_TCP_PORT must be in the range 1..65535.")
        if not (1 <= self.api_port <= 65535):
            raise ValueError(f"{ENV_PREFIX}API_PORT must be in the range 1..65535.")
        if not (-90 <= self.radar_center_lat <= 90):
            raise ValueError(f"{ENV_PREFIX}RADAR_CENTER_LAT must be in the range -90..90.")
        if not (-180 <= self.radar_center_lon <= 180):
            raise ValueError(f"{ENV_PREFIX}RADAR_CENTER_LON must be in the range -180..180.")
        if self.map_source not in VALID_MAP_SOURCES:
            valid_sources = ", ".join(sorted(VALID_MAP_SOURCES))
            raise ValueError(
                f"Invalid {ENV_PREFIX}MAP_SOURCE={self.map_source!r}. "
                f"Expected one of: {valid_sources}."
            )
        if self.map_cache_ttl_seconds <= 0:
            raise ValueError(f"{ENV_PREFIX}MAP_CACHE_TTL_SECONDS must be > 0.")
        if self.markhojd_direct_srid <= 0:
            raise ValueError(f"{ENV_PREFIX}MARKHOJD_DIRECT_SRID must be > 0.")
        if self.markhojd_direct_sample_step_m <= 0:
            raise ValueError(f"{ENV_PREFIX}MARKHOJD_DIRECT_SAMPLE_STEP_M must be > 0.")
        if self.markhojd_direct_contour_interval_m <= 0:
            raise ValueError(f"{ENV_PREFIX}MARKHOJD_DIRECT_CONTOUR_INTERVAL_M must be > 0.")
        if self.markhojd_direct_max_points_per_request <= 0:
            raise ValueError(f"{ENV_PREFIX}MARKHOJD_DIRECT_MAX_POINTS_PER_REQUEST must be > 0.")
        if self.markhojd_direct_max_points_per_request > 1000:
            raise ValueError(f"{ENV_PREFIX}MARKHOJD_DIRECT_MAX_POINTS_PER_REQUEST must be <= 1000.")


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Load configuration from process environment."""

    return Config.from_env(env)
