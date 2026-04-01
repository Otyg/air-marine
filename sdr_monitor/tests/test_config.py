from __future__ import annotations

import pytest

from app.config import Config


def test_config_uses_defaults_when_env_not_set() -> None:
    config = Config.from_env({})
    assert config.service_name == "sdr-monitor"
    assert config.log_level == "INFO"
    assert config.adsb_window_seconds == 8.0
    assert config.ais_window_seconds == 12.0
    assert config.inter_scan_pause_seconds == 2.0
    assert config.max_positions_per_target == 5
    assert config.radar_center_lat == 0.0
    assert config.radar_center_lon == 0.0
    assert str(config.fixed_objects_path) == "data/fixed_objects.json"
    assert config.map_source == "hydro"
    assert config.map_cache_ttl_seconds == 600
    assert config.hydro_base_url == "https://api.lantmateriet.se/ogc-features/v1/hydrografi"
    assert config.elevation_stac_base_url == "https://api.lantmateriet.se/stac-hojd/v1/"
    assert str(config.elevation_cache_dir) == "data/map/elevation_cache"
    assert config.elevation_contour_interval_m == 10
    assert config.elevation_max_tiles_per_request == 8
    assert config.elevation_enable_background_sync is True


def test_config_reads_environment_values() -> None:
    config = Config.from_env(
        {
            "SDR_MONITOR_SERVICE_NAME": "air-marine",
            "SDR_MONITOR_LOG_LEVEL": "debug",
            "SDR_MONITOR_ADSB_WINDOW_SECONDS": "5.5",
            "SDR_MONITOR_AIS_WINDOW_SECONDS": "9",
            "SDR_MONITOR_INTER_SCAN_PAUSE_SECONDS": "2.5",
            "SDR_MONITOR_FRESH_SECONDS": "15",
            "SDR_MONITOR_AGING_SECONDS": "60",
            "SDR_MONITOR_MAX_POSITIONS_PER_TARGET": "7",
            "SDR_MONITOR_AIS_TCP_PORT": "10111",
            "SDR_MONITOR_API_PORT": "18000",
            "SDR_MONITOR_RADAR_CENTER_LAT": "59.3345",
            "SDR_MONITOR_RADAR_CENTER_LON": "18.0732",
            "SDR_MONITOR_FIXED_OBJECTS_PATH": "/tmp/fixed-objects.json",
            "SDR_MONITOR_MAP_SOURCE": "elevation",
            "SDR_MONITOR_MAP_CACHE_TTL_SECONDS": "120",
            "SDR_MONITOR_HYDRO_BASE_URL": "https://hydro.example.test",
            "SDR_MONITOR_HYDRO_USERNAME": "hydro-user",
            "SDR_MONITOR_HYDRO_PASSWORD": "hydro-pass",
            "SDR_MONITOR_ELEVATION_STAC_BASE_URL": "https://elevation.example.test",
            "SDR_MONITOR_ELEVATION_USERNAME": "elevation-user",
            "SDR_MONITOR_ELEVATION_PASSWORD": "elevation-pass",
            "SDR_MONITOR_ELEVATION_CACHE_DIR": "/tmp/elevation-cache",
            "SDR_MONITOR_ELEVATION_CONTOUR_INTERVAL_M": "25",
            "SDR_MONITOR_ELEVATION_MAX_TILES_PER_REQUEST": "4",
            "SDR_MONITOR_ELEVATION_ENABLE_BACKGROUND_SYNC": "false",
        }
    )
    assert config.service_name == "air-marine"
    assert config.log_level == "DEBUG"
    assert config.adsb_window_seconds == 5.5
    assert config.ais_window_seconds == 9.0
    assert config.inter_scan_pause_seconds == 2.5
    assert config.fresh_seconds == 15
    assert config.aging_seconds == 60
    assert config.max_positions_per_target == 7
    assert config.ais_tcp_port == 10111
    assert config.api_port == 18000
    assert config.radar_center_lat == 59.3345
    assert config.radar_center_lon == 18.0732
    assert str(config.fixed_objects_path) == "/tmp/fixed-objects.json"
    assert config.map_source == "elevation"
    assert config.map_cache_ttl_seconds == 120
    assert config.hydro_base_url == "https://hydro.example.test"
    assert config.hydro_username == "hydro-user"
    assert config.hydro_password == "hydro-pass"
    assert config.elevation_stac_base_url == "https://elevation.example.test"
    assert config.elevation_username == "elevation-user"
    assert config.elevation_password == "elevation-pass"
    assert str(config.elevation_cache_dir) == "/tmp/elevation-cache"
    assert config.elevation_contour_interval_m == 25
    assert config.elevation_max_tiles_per_request == 4
    assert config.elevation_enable_background_sync is False


def test_config_rejects_invalid_freshness_thresholds() -> None:
    with pytest.raises(ValueError, match="AGING_SECONDS"):
        Config.from_env(
            {
                "SDR_MONITOR_FRESH_SECONDS": "50",
                "SDR_MONITOR_AGING_SECONDS": "49",
            }
        )


def test_config_rejects_invalid_radar_center_coordinates() -> None:
    with pytest.raises(ValueError, match="RADAR_CENTER_LAT"):
        Config.from_env({"SDR_MONITOR_RADAR_CENTER_LAT": "91"})

    with pytest.raises(ValueError, match="RADAR_CENTER_LON"):
        Config.from_env({"SDR_MONITOR_RADAR_CENTER_LON": "-181"})


def test_config_rejects_negative_inter_scan_pause() -> None:
    with pytest.raises(ValueError, match="INTER_SCAN_PAUSE_SECONDS"):
        Config.from_env({"SDR_MONITOR_INTER_SCAN_PAUSE_SECONDS": "-0.1"})


def test_config_rejects_invalid_map_settings() -> None:
    with pytest.raises(ValueError, match="MAP_SOURCE"):
        Config.from_env({"SDR_MONITOR_MAP_SOURCE": "unsupported"})

    with pytest.raises(ValueError, match="MAP_CACHE_TTL_SECONDS"):
        Config.from_env({"SDR_MONITOR_MAP_CACHE_TTL_SECONDS": "0"})

    with pytest.raises(ValueError, match="ELEVATION_CONTOUR_INTERVAL_M"):
        Config.from_env({"SDR_MONITOR_ELEVATION_CONTOUR_INTERVAL_M": "0"})

    with pytest.raises(ValueError, match="ELEVATION_ENABLE_BACKGROUND_SYNC"):
        Config.from_env({"SDR_MONITOR_ELEVATION_ENABLE_BACKGROUND_SYNC": "maybe"})


def test_config_reads_legacy_radar_coordinate_names() -> None:
    config = Config.from_env(
        {
            "SDR_MONITOR_RADAR_LATITUDE": "56.1619519",
            "SDR_MONITOR_RADAR_LONGITUDE": "15.5940978",
        }
    )
    assert config.radar_center_lat == 56.1619519
    assert config.radar_center_lon == 15.5940978
