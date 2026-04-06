from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.ingest_ogn import (
    OGNIngestError,
    OGNTCPIngestor,
    parse_ogn_aprs_line,
    parse_ogn_aprs_lines,
    read_ogn_lines_from_tcp,
)
from app.models import ScanBand, Source, TargetKind


def test_parse_ogn_aprs_line_normalizes_flarm_beacon() -> None:
    observed_at = datetime(2026, 4, 7, 11, 46, tzinfo=timezone.utc)
    line = (
        "FLRDDDEAD>APRS,qAS,EDER:/114500h5029.86N/00956.98E'342/049/A=005524 "
        "id22DDE626 -454fpm -1.1rot 8.8dB 0e +51.2kHz gps4x5"
    )

    observation = parse_ogn_aprs_line(line, observed_at=observed_at)

    assert observation is not None
    assert observation.target_id == "ogn:flarm-dde626"
    assert observation.source == Source.OGN
    assert observation.kind == TargetKind.AIRCRAFT
    assert observation.last_scan_band == ScanBand.OGN
    assert observation.label == "FLRDDDEAD"
    assert observation.callsign == "FLRDDDEAD"
    assert observation.course == 342.0
    assert observation.speed == 49.0
    assert observation.altitude == 5524.0
    assert observation.vertical_rate == -454.0
    assert observation.icao24 is None
    assert observation.payload_json["device_id"] == "flarm-dde626"
    assert observation.payload_json["aircraft_type"] == "towplane"
    assert observation.payload_json["protocol"] == "flarm"
    assert observation.observed_at == datetime(2026, 4, 7, 11, 45, tzinfo=timezone.utc)
    assert observation.lat == pytest.approx(50.4976667, rel=1e-6)
    assert observation.lon == pytest.approx(9.9496667, rel=1e-6)


def test_parse_ogn_aprs_line_tags_known_adsl_packet_formats() -> None:
    observed_at = datetime(2026, 4, 7, 8, 40, tzinfo=timezone.utc)
    line = (
        "ICA48FD60>OGNSKY,qAS,SafeSky:/083915h5359.04N/01626.91E'290/099/A=004435 "
        "!W20! id2048FD60 +000fpm gps4x1"
    )

    observation = parse_ogn_aprs_line(line, observed_at=observed_at)

    assert observation is not None
    assert observation.target_id == "ogn:flarm-48fd60"
    assert observation.source == Source.OGN
    assert observation.last_scan_band == ScanBand.OGN
    assert observation.callsign == "ICA48FD60"
    assert observation.icao24 == "48fd60"
    assert observation.payload_json["tocall"] == "OGNSKY"
    assert observation.payload_json["relay_path"] == ["qAS", "SafeSky"]
    assert observation.payload_json["network"] == "ognsky"
    assert observation.payload_json["protocol"] == "ads-l"
    assert observation.payload_json["sender_declared_icao24"] == "48fd60"
    assert observation.observed_at == datetime(2026, 4, 7, 8, 39, 15, tzinfo=timezone.utc)
    assert observation.lat == pytest.approx(53.984, rel=1e-6)
    assert observation.lon == pytest.approx(16.4485, rel=1e-6)


def test_parse_ogn_aprs_lines_ignores_non_position_lines() -> None:
    observed_at = datetime(2026, 4, 7, 11, 46, tzinfo=timezone.utc)
    lines = [
        "# aprsc 2.1.10-g408d6a5",
        "receiver-status-without-position",
        (
            "ICA3D4E5F>APRS,qAS,TEST:/114500h5029.86N/00956.98E'342/049/A=005524 "
            "id18D4E5F0 +012fpm +0.1rot 8.8dB 0e +51.2kHz"
        ),
    ]

    observations = parse_ogn_aprs_lines(lines, observed_at=observed_at)

    assert len(observations) == 1
    assert observations[0].target_id == "ogn:icao-d4e5f0"


def test_ingestor_reads_from_tcp_loader(monkeypatch) -> None:
    observed_at = datetime(2026, 4, 7, 11, 46, tzinfo=timezone.utc)
    line = (
        "FLRDDDEAD>APRS,qAS,EDER:/114500h5029.86N/00956.98E'342/049/A=005524 "
        "id22DDE626 -454fpm -1.1rot 8.8dB 0e +51.2kHz gps4x5"
    )

    monkeypatch.setattr(
        "app.ingest_ogn.read_ogn_lines_from_tcp",
        lambda **kwargs: [line],  # noqa: ARG005
    )
    monkeypatch.setattr("app.ingest_ogn.datetime", type("FrozenDateTime", (), {"now": staticmethod(lambda tz: observed_at)}))

    ingestor = OGNTCPIngestor(host="127.0.0.1", port=50001)
    observations = ingestor.read_observations()

    assert len(observations) == 1
    assert observations[0].source == Source.OGN


def test_read_ogn_lines_from_tcp_raises_for_connection_error() -> None:
    with pytest.raises(OGNIngestError):
        read_ogn_lines_from_tcp(
            host="127.0.0.1",
            port=9,
            timeout_seconds=0.1,
            max_lines=1,
        )
