from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.ingest_adsb import (
    ADSBAircraftJsonIngestor,
    ADSBIngestError,
    load_readsb_aircraft_json,
    parse_readsb_aircraft_json,
)
from app.models import ScanBand, Source, TargetKind


def test_parse_readsb_aircraft_json_normalizes_fields_and_filters_invalid() -> None:
    payload = {
        "now": 1711800000.0,
        "aircraft": [
            {
                "hex": "ABC123",
                "flight": "SAS123 ",
                "lat": 59.334,
                "lon": 18.063,
                "track": 270,
                "gs": 431.2,
                "alt_baro": 12000,
                "baro_rate": -64,
                "squawk": "1234",
                "seen": 2.0,
            },
            {"hex": "invalid!", "flight": "NOPE"},
            {"flight": "MISSING_HEX"},
            "not-a-dict",
        ],
    }

    observations = parse_readsb_aircraft_json(payload)
    assert len(observations) == 1

    obs = observations[0]
    assert obs.target_id == "adsb:abc123"
    assert obs.source == Source.ADSB
    assert obs.kind == TargetKind.AIRCRAFT
    assert obs.last_scan_band == ScanBand.ADSB
    assert obs.icao24 == "abc123"
    assert obs.callsign == "SAS123"
    assert obs.label == "SAS123"
    assert obs.squawk == "1234"
    assert obs.lat == 59.334
    assert obs.lon == 18.063
    assert obs.course == 270.0
    assert obs.speed == 431.2
    assert obs.altitude == 12000.0
    assert obs.vertical_rate == -64.0
    assert obs.observed_at == datetime.fromtimestamp(1711799998.0, tz=timezone.utc)


def test_parse_readsb_aircraft_json_applies_seen_offset() -> None:
    payload = {
        "now": 1000.0,
        "aircraft": [{"hex": "a1b2c3", "seen": 2.5}],
    }

    observations = parse_readsb_aircraft_json(payload)
    assert len(observations) == 1
    assert observations[0].observed_at == datetime.fromtimestamp(997.5, tz=timezone.utc)


def test_parse_readsb_aircraft_json_handles_bad_position_and_altitude_fallback() -> None:
    payload = {
        "now": 1000.0,
        "aircraft": [
            {
                "hex": "112233",
                "lat": 200.0,
                "lon": -190.0,
                "alt_baro": "ground",
                "alt_geom": 1500,
            }
        ],
    }
    observations = parse_readsb_aircraft_json(payload)

    assert len(observations) == 1
    assert observations[0].lat is None
    assert observations[0].lon is None
    assert observations[0].altitude == 1500.0
    assert observations[0].label == "112233"


def test_ingestor_reads_from_file(tmp_path) -> None:
    data = {
        "now": 2000.0,
        "aircraft": [{"hex": "abcdef", "flight": "FIN77"}],
    }
    file_path = tmp_path / "aircraft.json"
    file_path.write_text(json.dumps(data), encoding="utf-8")

    ingestor = ADSBAircraftJsonIngestor(file_path)
    observations = ingestor.read_observations()
    assert len(observations) == 1
    assert observations[0].target_id == "adsb:abcdef"
    assert observations[0].callsign == "FIN77"


def test_load_readsb_aircraft_json_raises_for_invalid_json(tmp_path) -> None:
    file_path = tmp_path / "bad.json"
    file_path.write_text("{bad-json", encoding="utf-8")

    with pytest.raises(ADSBIngestError):
        load_readsb_aircraft_json(file_path)
