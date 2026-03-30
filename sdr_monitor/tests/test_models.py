from __future__ import annotations

from collections import deque
from datetime import datetime, timezone

from app.models import (
    Freshness,
    LiveTargetState,
    NormalizedObservation,
    PositionSample,
    ScanBand,
    Source,
    Target,
    TargetKind,
)


def test_position_sample_round_trip() -> None:
    sample = PositionSample(
        ts=datetime(2026, 3, 30, 10, 15, tzinfo=timezone.utc),
        lat=59.334,
        lon=18.063,
        course=90.0,
        speed=12.3,
        altitude=500.0,
    )

    restored = PositionSample.from_dict(sample.to_dict())
    assert restored == sample


def test_normalized_observation_round_trip() -> None:
    observation = NormalizedObservation(
        target_id="adsb:abcdef",
        source=Source.ADSB,
        kind=TargetKind.AIRCRAFT,
        observed_at=datetime(2026, 3, 30, 11, 0, tzinfo=timezone.utc),
        label="SAS123",
        lat=59.0,
        lon=18.0,
        course=123.0,
        speed=220.2,
        altitude=10400.0,
        last_scan_band=ScanBand.ADSB,
        icao24="abcdef",
        callsign="SAS123 ",
        squawk="1234",
        vertical_rate=500.0,
        payload_json={"hex": "abcdef"},
    )

    restored = NormalizedObservation.from_dict(observation.to_dict())
    assert restored == observation


def test_target_round_trip() -> None:
    target = Target(
        target_id="ais:265123456",
        source=Source.AIS,
        kind=TargetKind.VESSEL,
        label="AMALIA",
        lat=58.0,
        lon=17.5,
        course=45.0,
        speed=14.7,
        altitude=None,
        first_seen=datetime(2026, 3, 30, 9, 45, tzinfo=timezone.utc),
        last_seen=datetime(2026, 3, 30, 10, 45, tzinfo=timezone.utc),
        freshness=Freshness.FRESH,
        last_scan_band=ScanBand.AIS,
        mmsi="265123456",
        shipname="AMALIA",
        nav_status="under way",
    )

    restored = Target.from_dict(target.to_dict())
    assert restored == target


def test_live_target_state_round_trip_keeps_positions_and_capacity() -> None:
    target = Target(
        target_id="adsb:123456",
        source=Source.ADSB,
        kind=TargetKind.AIRCRAFT,
        label="FIN45",
        lat=59.2,
        lon=18.1,
        course=70.0,
        speed=205.0,
        altitude=8100.0,
        first_seen=datetime(2026, 3, 30, 9, 45, tzinfo=timezone.utc),
        last_seen=datetime(2026, 3, 30, 10, 45, tzinfo=timezone.utc),
        freshness=Freshness.AGING,
        last_scan_band=ScanBand.ADSB,
        icao24="123456",
    )

    positions = deque(
        [
            PositionSample(
                ts=datetime(2026, 3, 30, 10, 44, tzinfo=timezone.utc),
                lat=59.2,
                lon=18.1,
                course=70.0,
                speed=205.0,
                altitude=8100.0,
            ),
            PositionSample(
                ts=datetime(2026, 3, 30, 10, 45, tzinfo=timezone.utc),
                lat=59.21,
                lon=18.12,
                course=71.0,
                speed=206.0,
                altitude=8120.0,
            ),
        ],
        maxlen=5,
    )
    state = LiveTargetState(
        target=target,
        positions=positions,
        observation_count=2,
        last_source_message_ts=datetime(2026, 3, 30, 10, 45, tzinfo=timezone.utc),
    )

    restored = LiveTargetState.from_dict(state.to_dict())
    assert restored.target == state.target
    assert list(restored.positions) == list(state.positions)
    assert restored.positions.maxlen == 5
    assert restored.observation_count == 2
