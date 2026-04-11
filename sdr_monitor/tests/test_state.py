from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import Freshness, NormalizedObservation, Source, TargetKind
from app.state import LiveState


class FakeClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


def _obs(
    target_id: str,
    kind: TargetKind,
    seen_at: datetime,
    lat: float | None = 59.0,
    lon: float | None = 18.0,
    callsign: str | None = None,
    speed: float | None = 120.0,
) -> NormalizedObservation:
    return NormalizedObservation(
        target_id=target_id,
        source=Source.ADSB if kind == TargetKind.AIRCRAFT else Source.AIS,
        kind=kind,
        observed_at=seen_at,
        lat=lat,
        lon=lon,
        course=90.0,
        speed=speed,
        altitude=1000.0 if kind == TargetKind.AIRCRAFT else None,
        callsign=callsign,
    )


def test_upsert_creates_target_and_position() -> None:
    now = datetime(2026, 3, 30, 12, 0, tzinfo=timezone.utc)
    state = LiveState(clock=FakeClock(now))
    created = state.upsert_observation(_obs("adsb:abc", TargetKind.AIRCRAFT, now))

    assert created.target.target_id == "adsb:abc"
    assert created.observation_count == 1
    assert len(created.positions) == 1
    assert created.target.first_seen == now
    assert created.target.last_seen == now
    assert created.target.freshness == Freshness.FRESH


def test_metadata_update_without_position_does_not_append_position() -> None:
    now = datetime(2026, 3, 30, 12, 0, tzinfo=timezone.utc)
    state = LiveState(clock=FakeClock(now))
    state.upsert_observation(
        _obs("adsb:meta", TargetKind.AIRCRAFT, now - timedelta(seconds=5), callsign="OLD")
    )

    updated = state.upsert_observation(
        _obs(
            "adsb:meta",
            TargetKind.AIRCRAFT,
            now,
            lat=None,
            lon=None,
            callsign="NEW",
        )
    )

    assert len(updated.positions) == 1
    assert updated.target.callsign == "NEW"
    assert updated.observation_count == 2


def test_position_retention_uses_two_minute_window_and_duplicate_filter() -> None:
    now = datetime(2026, 3, 30, 12, 0, tzinfo=timezone.utc)
    clock = FakeClock(now)
    state = LiveState(max_positions_per_target=5, clock=clock)

    for idx in range(16):
        seen_at = now - timedelta(seconds=(15 - idx) * 10)
        state.upsert_observation(
            _obs(
                "ais:123",
                TargetKind.VESSEL,
                seen_at,
                lat=58.0 + (idx * 0.001),
                lon=17.0 + (idx * 0.001),
            )
        )

    current = state.get_target_state("ais:123")
    assert current is not None
    assert len(current.positions) == 13
    assert current.positions[0].ts == now - timedelta(seconds=120)
    assert current.positions[-1].ts == now

    before = len(current.positions)
    state.upsert_observation(
        _obs(
            "ais:123",
            TargetKind.VESSEL,
            now + timedelta(seconds=1),
            lat=58.0,
            lon=17.0,
        )
    )
    after = state.get_target_state("ais:123")
    assert after is not None
    assert len(after.positions) == before

def test_filters_and_stats_by_freshness() -> None:
    now = datetime(2026, 3, 30, 12, 0, tzinfo=timezone.utc)
    clock = FakeClock(now)
    state = LiveState(fresh_seconds=30, aging_seconds=120, clock=clock)

    state.upsert_observation(
        _obs("adsb:fresh", TargetKind.AIRCRAFT, now - timedelta(seconds=5))
    )
    state.upsert_observation(
        _obs("ais:aging", TargetKind.VESSEL, now - timedelta(seconds=60))
    )
    state.upsert_observation(
        _obs("adsb:stale", TargetKind.AIRCRAFT, now - timedelta(seconds=130))
    )

    all_targets = state.list_targets()
    assert len(all_targets) == 3
    fresh_or_aging = state.list_targets(fresh_only=True)
    assert len(fresh_or_aging) == 2
    only_vessels = state.list_targets(kind=TargetKind.VESSEL)
    assert len(only_vessels) == 1

    stats = state.get_stats()
    assert stats["live_aircraft_count"] == 2
    assert stats["live_vessel_count"] == 1
    assert stats["total_live_targets"] == 3
    assert stats["fresh_count"] == 1
    assert stats["aging_count"] == 1
    assert stats["stale_count"] == 1
