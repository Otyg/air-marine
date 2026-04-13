from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from app.qt_live_view import (
    build_api_url,
    load_qt_live_view_config,
    normalize_backend_base_url,
    parse_live_ui_config,
    resolve_config,
    save_qt_live_view_config,
)


def test_normalize_backend_base_url() -> None:
    assert normalize_backend_base_url("127.0.0.1:8000") == "http://127.0.0.1:8000"
    assert normalize_backend_base_url("https://example.test/radar/") == "https://example.test/radar"


@pytest.mark.parametrize("raw", ["", "ftp://host", "http:///missing-host"])
def test_normalize_backend_base_url_rejects_invalid(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_backend_base_url(raw)


def test_build_api_url_with_query() -> None:
    url = build_api_url("http://127.0.0.1:8000", "/ui/map-contours", {"bbox": "1,2,3,4"})
    assert url == "http://127.0.0.1:8000/ui/map-contours?bbox=1%2C2%2C3%2C4"


def test_load_qt_live_view_config_from_json(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "backend_base_url": "http://127.0.0.1:8000",
                "window_title": "Radar",
                "service_name": "air-marine",
                "window_width": 1280,
                "window_height": 720,
                "poll_interval_ms": 4500,
                "request_timeout_ms": 7000,
                "default_range_km": 12,
                "trail_point_window_seconds": 240,
                "marker_size_scale": 1.3,
                "fixed_marker_size_scale": 1.6,
                "vessel_symbol_box_factor": 0.78,
                "zoom_visual_exponent": 0.22,
                "show_target_labels": True,
                "show_fixed_names": False,
                "show_map_contours": True,
                "show_low_speed": True,
                "target_type_filter": "aircraft",
                "map_source": "elevation",
                "fallback_center_lat": 56.16,
                "fallback_center_lon": 15.59,
                "use_backend_live_config": False,
                "fixed_objects_remove_names": ["Base Harbor", "Radar Mast"],
                "fixed_objects": [
                    {"name": "Harbor", "lat": 56.16, "lon": 15.59, "symbol": "H"},
                    {"name": "Mast", "latitude": 56.17, "longitude": 15.60},
                ],
            }
        ),
        encoding="utf-8",
    )

    config = load_qt_live_view_config(config_path)
    assert config.backend_base_url == "http://127.0.0.1:8000"
    assert config.window_title == "Radar"
    assert config.service_name == "air-marine"
    assert config.window_width == 1280
    assert config.window_height == 720
    assert config.poll_interval_ms == 4500
    assert config.request_timeout_ms == 7000
    assert config.default_range_km == 12.0
    assert config.trail_point_window_seconds == 240.0
    assert config.marker_size_scale == 1.3
    assert config.fixed_marker_size_scale == 1.6
    assert config.vessel_symbol_box_factor == 0.78
    assert config.zoom_visual_exponent == 0.22
    assert config.show_target_labels is True
    assert config.show_fixed_names is False
    assert config.show_map_contours is True
    assert config.show_low_speed is True
    assert config.target_type_filter == "aircraft"
    assert config.map_source == "elevation"
    assert config.use_backend_live_config is False
    assert config.fixed_objects_remove_names == ("Base Harbor", "Radar Mast")
    assert len(config.fixed_objects) == 2
    assert config.fixed_objects[0]["name"] == "Harbor"


def test_resolve_config_applies_cli_overrides(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "backend_base_url": "http://127.0.0.1:8000",
                "window_title": "Base",
            }
        ),
        encoding="utf-8",
    )

    args = argparse.Namespace(
        config=str(config_path),
        base_url="https://radar.example.test/prefix",
        title="Override",
    )
    resolved = resolve_config(args)
    assert resolved.backend_base_url == "https://radar.example.test/prefix"
    assert resolved.window_title == "Override"


def test_parse_live_ui_config() -> None:
    payload = {
        "service_name": "air-marine",
        "center_lat": 56.1619,
        "center_lon": 15.5940,
        "fixed_objects": [{"name": "Harbor", "lat": 56.16, "lon": 15.59}],
        "default_map_source": "hydro",
    }
    config = parse_live_ui_config(payload)
    assert config.service_name == "air-marine"
    assert config.center_lat == 56.1619
    assert config.center_lon == 15.594
    assert config.default_map_source == "hydro"
    assert config.fixed_objects[0]["name"] == "Harbor"


def test_load_qt_live_view_config_defaults_to_local_live_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "backend_base_url": "http://127.0.0.1:8000",
                "window_title": "Radar",
            }
        ),
        encoding="utf-8",
    )
    config = load_qt_live_view_config(config_path)
    assert config.use_backend_live_config is False


def test_load_qt_live_view_config_supports_fixed_marker_scale_alias(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "backend_base_url": "http://127.0.0.1:8000",
                "fixed_marker_scale": 1.9,
            }
        ),
        encoding="utf-8",
    )
    config = load_qt_live_view_config(config_path)
    assert config.fixed_marker_size_scale == 1.9


def test_save_qt_live_view_config_round_trip(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "backend_base_url": "http://127.0.0.1:8000",
                "window_title": "Radar",
                "trail_point_window_seconds": 180,
                "marker_size_scale": 1.15,
                "fixed_marker_size_scale": 1.25,
                "vessel_symbol_box_factor": 0.9,
                "zoom_visual_exponent": 0.2,
                "fixed_objects_remove_names": ["Base Harbor"],
                "fixed_objects": [{"name": "Harbor", "lat": 56.16, "lon": 15.59}],
            }
        ),
        encoding="utf-8",
    )
    config = load_qt_live_view_config(config_path)
    save_qt_live_view_config(config)
    reloaded = load_qt_live_view_config(config_path)
    assert reloaded.trail_point_window_seconds == 180.0
    assert reloaded.marker_size_scale == 1.15
    assert reloaded.fixed_marker_size_scale == 1.25
    assert reloaded.vessel_symbol_box_factor == 0.9
    assert reloaded.zoom_visual_exponent == 0.2
    assert reloaded.fixed_objects_remove_names == ("Base Harbor",)
    assert len(reloaded.fixed_objects) == 1
