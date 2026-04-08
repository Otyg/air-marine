from __future__ import annotations

import pytest

from app.config import Config


def test_config_uses_defaults_when_env_not_set() -> None:
    config = Config.from_env({})
    assert config.service_name == "sdr-monitor"
    assert config.log_level == "INFO"
    assert config.adsb_window_seconds == 8.0
    assert config.ogn_window_seconds == 0.0
    assert config.ais_window_seconds == 12.0
    assert config.inter_scan_pause_seconds == 2.0
    assert config.max_positions_per_target == 5
    assert config.radar_center_lat == 0.0
    assert config.radar_center_lon == 0.0
    assert str(config.fixed_objects_path) == "data/fixed_objects.json"
    assert config.map_source == "hydro"
    assert config.map_cache_ttl_seconds == 600
    assert config.map_cache_dir.is_absolute()
    assert config.map_cache_dir.is_dir()
    assert config.map_cache_dir.parts[-3:] == ("data", "map", "cache")
    assert config.map_cache_dir.as_posix().startswith("/tmp/sdr-monitor-")
    assert config.hydro_base_url == "https://api.lantmateriet.se/ogc-features/v1/hydrografi"
    assert (
        config.markhojd_direct_base_url
        == "https://api.lantmateriet.se/distribution/produkter/markhojd/v1"
    )
    assert config.markhojd_direct_srid == 3006
    assert config.markhojd_direct_sample_step_m == 25
    assert config.markhojd_direct_contour_interval_m == 10
    assert config.markhojd_direct_max_points_per_request == 1000
    assert config.ogn_tcp_host == "127.0.0.1"
    assert config.ogn_tcp_port == 50001
    assert config.radio_backend == "legacy"
    assert config.adsb_inproc_source == "readsb"
    assert config.adsb_inproc_rtl_host == "127.0.0.1"
    assert config.adsb_inproc_rtl_port == 1234
    assert config.adsb_inproc_sample_rate == 2_000_000
    assert config.adsb_inproc_gain == 30
    assert config.adsb_inproc_frequency_hz == 1_090_000_000
    assert config.ais_inproc_source == "tcp"
    assert config.ais_inproc_rtl_host == "127.0.0.1"
    assert config.ais_inproc_rtl_port == 1234
    assert config.ais_inproc_sample_rate == 288_000
    assert config.ais_inproc_gain == 30
    assert config.ais_frequency_hz == 162_025_000
    assert config.ogn_frequency_hz == 868_200_000
    assert config.dsc_frequency_hz == 156_525_000
    assert config.radio_external_use_worker is False
    assert config.radio_external_control_host == "127.0.0.1"
    assert config.radio_external_control_port == 17601
    assert config.radio_external_data_host == "127.0.0.1"
    assert config.radio_external_data_port == 17602
    assert config.mock_radio_timing_enabled is False


def test_config_reads_environment_values() -> None:
    config = Config.from_env(
        {
            "SDR_MONITOR_SERVICE_NAME": "air-marine",
            "SDR_MONITOR_LOG_LEVEL": "debug",
            "SDR_MONITOR_ADSB_WINDOW_SECONDS": "5.5",
            "SDR_MONITOR_OGN_WINDOW_SECONDS": "4",
            "SDR_MONITOR_AIS_WINDOW_SECONDS": "9",
            "SDR_MONITOR_INTER_SCAN_PAUSE_SECONDS": "2.5",
            "SDR_MONITOR_FRESH_SECONDS": "15",
            "SDR_MONITOR_AGING_SECONDS": "60",
            "SDR_MONITOR_MAX_POSITIONS_PER_TARGET": "7",
            "SDR_MONITOR_OGN_TCP_HOST": "127.0.0.2",
            "SDR_MONITOR_OGN_TCP_PORT": "50002",
            "SDR_MONITOR_AIS_TCP_PORT": "10111",
            "SDR_MONITOR_RADIO_BACKEND": "mock",
            "SDR_MONITOR_ADSB_INPROC_SOURCE": "rtl_tcp",
            "SDR_MONITOR_ADSB_INPROC_RTL_HOST": "127.0.0.9",
            "SDR_MONITOR_ADSB_INPROC_RTL_PORT": "3333",
            "SDR_MONITOR_ADSB_INPROC_SAMPLE_RATE": "2400000",
            "SDR_MONITOR_ADSB_INPROC_GAIN": "37",
            "SDR_MONITOR_ADSB_INPROC_FREQUENCY_HZ": "1090000000",
            "SDR_MONITOR_AIS_INPROC_SOURCE": "rtl_tcp",
            "SDR_MONITOR_AIS_INPROC_RTL_HOST": "127.0.0.8",
            "SDR_MONITOR_AIS_INPROC_RTL_PORT": "2234",
            "SDR_MONITOR_AIS_INPROC_SAMPLE_RATE": "384000",
            "SDR_MONITOR_AIS_INPROC_GAIN": "22",
            "SDR_MONITOR_AIS_FREQUENCY_HZ": "161975000",
            "SDR_MONITOR_OGN_FREQUENCY_HZ": "868400000",
            "SDR_MONITOR_DSC_FREQUENCY_HZ": "156525000",
            "SDR_MONITOR_RADIO_EXTERNAL_USE_WORKER": "true",
            "SDR_MONITOR_RADIO_EXTERNAL_CONTROL_HOST": "10.0.0.2",
            "SDR_MONITOR_RADIO_EXTERNAL_CONTROL_PORT": "18601",
            "SDR_MONITOR_RADIO_EXTERNAL_DATA_HOST": "10.0.0.3",
            "SDR_MONITOR_RADIO_EXTERNAL_DATA_PORT": "18602",
            "SDR_MONITOR_MOCK_RADIO_FIXTURE_PATH": "/tmp/mock-radio.json",
            "SDR_MONITOR_MOCK_RADIO_TIMING_ENABLED": "true",
            "SDR_MONITOR_API_PORT": "18000",
            "SDR_MONITOR_RADAR_CENTER_LAT": "59.3345",
            "SDR_MONITOR_RADAR_CENTER_LON": "18.0732",
            "SDR_MONITOR_FIXED_OBJECTS_PATH": "/tmp/fixed-objects.json",
            "SDR_MONITOR_MAP_SOURCE": "elevation",
            "SDR_MONITOR_MAP_CACHE_TTL_SECONDS": "120",
            "SDR_MONITOR_MAP_CACHE_DIR": "/tmp/map-cache",
            "SDR_MONITOR_HYDRO_BASE_URL": "https://hydro.example.test",
            "SDR_MONITOR_HYDRO_USERNAME": "hydro-user",
            "SDR_MONITOR_HYDRO_PASSWORD": "hydro-pass",
            "SDR_MONITOR_MARKHOJD_DIRECT_BASE_URL": "https://markhojd.example.test",
            "SDR_MONITOR_MARKHOJD_DIRECT_USERNAME": "markhojd-user",
            "SDR_MONITOR_MARKHOJD_DIRECT_PASSWORD": "markhojd-pass",
            "SDR_MONITOR_MARKHOJD_DIRECT_SRID": "3006",
            "SDR_MONITOR_MARKHOJD_DIRECT_SAMPLE_STEP_M": "40",
            "SDR_MONITOR_MARKHOJD_DIRECT_CONTOUR_INTERVAL_M": "20",
            "SDR_MONITOR_MARKHOJD_DIRECT_MAX_POINTS_PER_REQUEST": "900",
        }
    )
    assert config.service_name == "air-marine"
    assert config.log_level == "DEBUG"
    assert config.adsb_window_seconds == 5.5
    assert config.ogn_window_seconds == 4.0
    assert config.ais_window_seconds == 9.0
    assert config.inter_scan_pause_seconds == 2.5
    assert config.fresh_seconds == 15
    assert config.aging_seconds == 60
    assert config.max_positions_per_target == 7
    assert config.ogn_tcp_host == "127.0.0.2"
    assert config.ogn_tcp_port == 50002
    assert config.ais_tcp_port == 10111
    assert config.radio_backend == "mock"
    assert config.adsb_inproc_source == "rtl_tcp"
    assert config.adsb_inproc_rtl_host == "127.0.0.9"
    assert config.adsb_inproc_rtl_port == 3333
    assert config.adsb_inproc_sample_rate == 2_400_000
    assert config.adsb_inproc_gain == 37
    assert config.adsb_inproc_frequency_hz == 1_090_000_000
    assert config.ais_inproc_source == "rtl_tcp"
    assert config.ais_inproc_rtl_host == "127.0.0.8"
    assert config.ais_inproc_rtl_port == 2234
    assert config.ais_inproc_sample_rate == 384_000
    assert config.ais_inproc_gain == 22
    assert config.ais_frequency_hz == 161_975_000
    assert config.ogn_frequency_hz == 868_400_000
    assert config.dsc_frequency_hz == 156_525_000
    assert config.radio_external_use_worker is True
    assert config.radio_external_control_host == "10.0.0.2"
    assert config.radio_external_control_port == 18601
    assert config.radio_external_data_host == "10.0.0.3"
    assert config.radio_external_data_port == 18602
    assert str(config.mock_radio_fixture_path) == "/tmp/mock-radio.json"
    assert config.mock_radio_timing_enabled is True
    assert config.api_port == 18000
    assert config.radar_center_lat == 59.3345
    assert config.radar_center_lon == 18.0732
    assert str(config.fixed_objects_path) == "/tmp/fixed-objects.json"
    assert config.map_source == "elevation"
    assert config.map_cache_ttl_seconds == 120
    assert str(config.map_cache_dir) == "/tmp/map-cache"
    assert config.hydro_base_url == "https://hydro.example.test"
    assert config.hydro_username == "hydro-user"
    assert config.hydro_password == "hydro-pass"
    assert config.markhojd_direct_base_url == "https://markhojd.example.test"
    assert config.markhojd_direct_username == "markhojd-user"
    assert config.markhojd_direct_password == "markhojd-pass"
    assert config.markhojd_direct_srid == 3006
    assert config.markhojd_direct_sample_step_m == 40
    assert config.markhojd_direct_contour_interval_m == 20
    assert config.markhojd_direct_max_points_per_request == 900


def test_direct_config_default_places_map_cache_in_unique_tmp_dir() -> None:
    first = Config()
    second = Config()

    assert first.map_cache_dir != second.map_cache_dir
    assert first.map_cache_dir.is_dir()
    assert second.map_cache_dir.is_dir()
    assert first.map_cache_dir.parts[-3:] == ("data", "map", "cache")
    assert second.map_cache_dir.parts[-3:] == ("data", "map", "cache")


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


def test_config_rejects_negative_ogn_window() -> None:
    with pytest.raises(ValueError, match="OGN_WINDOW_SECONDS"):
        Config.from_env({"SDR_MONITOR_OGN_WINDOW_SECONDS": "-0.1"})


def test_config_rejects_invalid_map_settings() -> None:
    with pytest.raises(ValueError, match="MAP_SOURCE"):
        Config.from_env({"SDR_MONITOR_MAP_SOURCE": "unsupported"})

    with pytest.raises(ValueError, match="MAP_CACHE_TTL_SECONDS"):
        Config.from_env({"SDR_MONITOR_MAP_CACHE_TTL_SECONDS": "0"})

    with pytest.raises(ValueError, match="MARKHOJD_DIRECT_SAMPLE_STEP_M"):
        Config.from_env({"SDR_MONITOR_MARKHOJD_DIRECT_SAMPLE_STEP_M": "0"})

    with pytest.raises(ValueError, match="MARKHOJD_DIRECT_CONTOUR_INTERVAL_M"):
        Config.from_env({"SDR_MONITOR_MARKHOJD_DIRECT_CONTOUR_INTERVAL_M": "0"})

    with pytest.raises(ValueError, match="MARKHOJD_DIRECT_MAX_POINTS_PER_REQUEST"):
        Config.from_env({"SDR_MONITOR_MARKHOJD_DIRECT_MAX_POINTS_PER_REQUEST": "1001"})


def test_config_rejects_invalid_radio_backend() -> None:
    with pytest.raises(ValueError, match="RADIO_BACKEND"):
        Config.from_env({"SDR_MONITOR_RADIO_BACKEND": "invalid-backend"})


def test_config_rejects_invalid_adsb_inproc_settings() -> None:
    with pytest.raises(ValueError, match="ADSB_INPROC_SOURCE"):
        Config.from_env({"SDR_MONITOR_ADSB_INPROC_SOURCE": "invalid"})
    with pytest.raises(ValueError, match="ADSB_INPROC_RTL_PORT"):
        Config.from_env({"SDR_MONITOR_ADSB_INPROC_RTL_PORT": "0"})
    with pytest.raises(ValueError, match="ADSB_INPROC_SAMPLE_RATE"):
        Config.from_env({"SDR_MONITOR_ADSB_INPROC_SAMPLE_RATE": "0"})
    with pytest.raises(ValueError, match="ADSB_INPROC_GAIN"):
        Config.from_env({"SDR_MONITOR_ADSB_INPROC_GAIN": "99"})
    with pytest.raises(ValueError, match="ADSB_INPROC_FREQUENCY_HZ"):
        Config.from_env({"SDR_MONITOR_ADSB_INPROC_FREQUENCY_HZ": "0"})
    with pytest.raises(ValueError, match="AIS_INPROC_SOURCE"):
        Config.from_env({"SDR_MONITOR_AIS_INPROC_SOURCE": "invalid"})
    with pytest.raises(ValueError, match="AIS_INPROC_RTL_PORT"):
        Config.from_env({"SDR_MONITOR_AIS_INPROC_RTL_PORT": "0"})
    with pytest.raises(ValueError, match="AIS_INPROC_SAMPLE_RATE"):
        Config.from_env({"SDR_MONITOR_AIS_INPROC_SAMPLE_RATE": "0"})
    with pytest.raises(ValueError, match="AIS_INPROC_GAIN"):
        Config.from_env({"SDR_MONITOR_AIS_INPROC_GAIN": "99"})
    with pytest.raises(ValueError, match="AIS_FREQUENCY_HZ"):
        Config.from_env({"SDR_MONITOR_AIS_FREQUENCY_HZ": "0"})
    with pytest.raises(ValueError, match="OGN_FREQUENCY_HZ"):
        Config.from_env({"SDR_MONITOR_OGN_FREQUENCY_HZ": "0"})
    with pytest.raises(ValueError, match="DSC_FREQUENCY_HZ"):
        Config.from_env({"SDR_MONITOR_DSC_FREQUENCY_HZ": "0"})


def test_config_rejects_invalid_external_radio_ports() -> None:
    with pytest.raises(ValueError, match="RADIO_EXTERNAL_CONTROL_PORT"):
        Config.from_env({"SDR_MONITOR_RADIO_EXTERNAL_CONTROL_PORT": "0"})
    with pytest.raises(ValueError, match="RADIO_EXTERNAL_DATA_PORT"):
        Config.from_env({"SDR_MONITOR_RADIO_EXTERNAL_DATA_PORT": "70000"})


def test_config_reads_legacy_radar_coordinate_names() -> None:
    config = Config.from_env(
        {
            "SDR_MONITOR_RADAR_LATITUDE": "56.1619519",
            "SDR_MONITOR_RADAR_LONGITUDE": "15.5940978",
        }
    )
    assert config.radar_center_lat == 56.1619519
    assert config.radar_center_lon == 15.5940978


def test_config_falls_back_to_legacy_elevation_credentials_for_markhojd_direct() -> None:
    config = Config.from_env(
        {
            "SDR_MONITOR_ELEVATION_USERNAME": "legacy-user",
            "SDR_MONITOR_ELEVATION_PASSWORD": "legacy-pass",
        }
    )

    assert config.markhojd_direct_username == "legacy-user"
    assert config.markhojd_direct_password == "legacy-pass"
