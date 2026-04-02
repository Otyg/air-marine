from __future__ import annotations

import json

from app.fixed_objects import load_fixed_radar_objects


def test_load_fixed_radar_objects_parses_valid_entries_and_default_symbol(tmp_path) -> None:
    config_path = tmp_path / "fixed_objects.json"
    config_path.write_text(
        json.dumps(
            [
                {
                    "name": "Mast",
                    "latitude": 56.1601,
                    "longitude": 15.5901,
                },
                {
                    "name": "Buoy",
                    "latitude": 56.1622,
                    "longitude": 15.5922,
                    "symbol": "*",
                    "max_visible_range_km": 10,
                },
            ]
        ),
        encoding="utf-8",
    )

    objects = load_fixed_radar_objects(config_path)

    assert len(objects) == 2
    assert objects[0].name == "Mast"
    assert objects[0].symbol == "O"
    assert objects[0].max_visible_range_km is None
    assert objects[1].name == "Buoy"
    assert objects[1].symbol == "*"
    assert objects[1].max_visible_range_km == 10.0


def test_load_fixed_radar_objects_skips_invalid_entries(tmp_path) -> None:
    config_path = tmp_path / "fixed_objects.json"
    config_path.write_text(
        json.dumps(
            [
                {"name": "Valid", "latitude": 56.0, "longitude": 15.0},
                {"name": "BadLat", "latitude": 156.0, "longitude": 15.0},
                {"name": "BadLon", "latitude": 56.0, "longitude": 215.0},
                {"name": "BadStr", "latitude": "abc", "longitude": 15.0},
                {"latitude": 56.0, "longitude": 15.0},
            ]
        ),
        encoding="utf-8",
    )

    objects = load_fixed_radar_objects(config_path)

    assert len(objects) == 1
    assert objects[0].name == "Valid"


def test_load_fixed_radar_objects_ignores_invalid_max_visible_range(tmp_path) -> None:
    config_path = tmp_path / "fixed_objects.json"
    config_path.write_text(
        json.dumps(
            [
                {
                    "name": "Beacon",
                    "latitude": 56.0,
                    "longitude": 15.0,
                    "max_visible_range_km": "not-a-number",
                },
                {
                    "name": "Tower",
                    "latitude": 56.1,
                    "longitude": 15.1,
                    "max_visible_range_km": -5,
                },
            ]
        ),
        encoding="utf-8",
    )

    objects = load_fixed_radar_objects(config_path)

    assert len(objects) == 2
    assert objects[0].max_visible_range_km is None
    assert objects[1].max_visible_range_km is None
