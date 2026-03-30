from __future__ import annotations

import pytest

from app.config import Config


def test_config_uses_defaults_when_env_not_set() -> None:
    config = Config.from_env({})
    assert config.service_name == "sdr-monitor"
    assert config.log_level == "INFO"
    assert config.adsb_window_seconds == 8.0
    assert config.ais_window_seconds == 12.0
    assert config.max_positions_per_target == 5


def test_config_reads_environment_values() -> None:
    config = Config.from_env(
        {
            "SDR_MONITOR_SERVICE_NAME": "air-marine",
            "SDR_MONITOR_LOG_LEVEL": "debug",
            "SDR_MONITOR_ADSB_WINDOW_SECONDS": "5.5",
            "SDR_MONITOR_AIS_WINDOW_SECONDS": "9",
            "SDR_MONITOR_FRESH_SECONDS": "15",
            "SDR_MONITOR_AGING_SECONDS": "60",
            "SDR_MONITOR_MAX_POSITIONS_PER_TARGET": "7",
            "SDR_MONITOR_AIS_TCP_PORT": "10111",
            "SDR_MONITOR_API_PORT": "18000",
        }
    )
    assert config.service_name == "air-marine"
    assert config.log_level == "DEBUG"
    assert config.adsb_window_seconds == 5.5
    assert config.ais_window_seconds == 9.0
    assert config.fresh_seconds == 15
    assert config.aging_seconds == 60
    assert config.max_positions_per_target == 7
    assert config.ais_tcp_port == 10111
    assert config.api_port == 18000


def test_config_rejects_invalid_freshness_thresholds() -> None:
    with pytest.raises(ValueError, match="AGING_SECONDS"):
        Config.from_env(
            {
                "SDR_MONITOR_FRESH_SECONDS": "50",
                "SDR_MONITOR_AGING_SECONDS": "49",
            }
        )
