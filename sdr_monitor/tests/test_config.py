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


def test_config_reads_legacy_radar_coordinate_names() -> None:
    config = Config.from_env(
        {
            "SDR_MONITOR_RADAR_LATITUDE": "56.1619519",
            "SDR_MONITOR_RADAR_LONGITUDE": "15.5940978",
        }
    )
    assert config.radar_center_lat == 56.1619519
    assert config.radar_center_lon == 15.5940978
