"""In-memory live state for normalized targets."""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from datetime import datetime, timezone
from threading import RLock
from typing import Callable

from app.models import (
    Freshness,
    LiveTargetState,
    NormalizedObservation,
    PositionSample,
    Target,
    TargetKind,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LiveState:
    """Thread-safe in-memory state of currently known targets."""

    def __init__(
        self,
        fresh_seconds: int = 30,
        aging_seconds: int = 120,
        max_positions_per_target: int = 5,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if fresh_seconds < 0:
            raise ValueError("fresh_seconds must be >= 0")
        if aging_seconds <= fresh_seconds:
            raise ValueError("aging_seconds must be > fresh_seconds")
        if max_positions_per_target <= 0:
            raise ValueError("max_positions_per_target must be > 0")

        self._fresh_seconds = fresh_seconds
        self._aging_seconds = aging_seconds
        self._max_positions_per_target = max_positions_per_target
        self._clock = clock or _utcnow
        self._targets: dict[str, LiveTargetState] = {}
        self._lock = RLock()

    def upsert_observation(self, observation: NormalizedObservation) -> LiveTargetState:
        """Insert/update a target from a normalized observation."""

        with self._lock:
            existing = self._targets.get(observation.target_id)
            now = self._clock()

            if existing is None:
                target = Target(
                    target_id=observation.target_id,
                    source=observation.source,
                    kind=observation.kind,
                    label=observation.label,
                    lat=observation.lat,
                    lon=observation.lon,
                    course=observation.course,
                    speed=observation.speed,
                    altitude=observation.altitude,
                    first_seen=observation.observed_at,
                    last_seen=observation.observed_at,
                    freshness=self._calculate_freshness(observation.observed_at, now),
                    last_scan_band=observation.last_scan_band,
                    icao24=observation.icao24,
                    callsign=observation.callsign,
                    squawk=observation.squawk,
                    vertical_rate=observation.vertical_rate,
                    mmsi=observation.mmsi,
                    shipname=observation.shipname,
                    nav_status=observation.nav_status,
                )

                state = LiveTargetState(
                    target=target,
                    positions=deque(maxlen=self._max_positions_per_target),
                    observation_count=1,
                    last_source_message_ts=observation.observed_at,
                )
                self._append_position_if_valid(state, observation)
                self._refresh_freshness(state, now)
                self._targets[observation.target_id] = state
                return self._snapshot(state)

            updated_target = replace(
                existing.target,
                source=observation.source,
                kind=observation.kind,
                label=observation.label
                if observation.label is not None
                else existing.target.label,
                lat=observation.lat if observation.lat is not None else existing.target.lat,
                lon=observation.lon if observation.lon is not None else existing.target.lon,
                course=(
                    observation.course
                    if observation.course is not None
                    else existing.target.course
                ),
                speed=observation.speed
                if observation.speed is not None
                else existing.target.speed,
                altitude=observation.altitude
                if observation.altitude is not None
                else existing.target.altitude,
                last_seen=observation.observed_at,
                last_scan_band=observation.last_scan_band
                if observation.last_scan_band is not None
                else existing.target.last_scan_band,
                icao24=observation.icao24
                if observation.icao24 is not None
                else existing.target.icao24,
                callsign=observation.callsign
                if observation.callsign is not None
                else existing.target.callsign,
                squawk=observation.squawk
                if observation.squawk is not None
                else existing.target.squawk,
                vertical_rate=observation.vertical_rate
                if observation.vertical_rate is not None
                else existing.target.vertical_rate,
                mmsi=observation.mmsi
                if observation.mmsi is not None
                else existing.target.mmsi,
                shipname=observation.shipname
                if observation.shipname is not None
                else existing.target.shipname,
                nav_status=observation.nav_status
                if observation.nav_status is not None
                else existing.target.nav_status,
            )
            existing.target = updated_target
            existing.observation_count += 1
            existing.last_source_message_ts = observation.observed_at

            self._append_position_if_valid(existing, observation)
            self._refresh_freshness(existing, now)
            return self._snapshot(existing)

    def list_targets(
        self,
        kind: TargetKind | None = None,
        fresh_only: bool = False,
    ) -> list[Target]:
        """Return current targets, optionally filtered by kind and freshness."""

        with self._lock:
            now = self._clock()
            matched: list[Target] = []

            for state in self._targets.values():
                self._refresh_freshness(state, now)
                if kind is not None and state.target.kind != kind:
                    continue
                if fresh_only and state.target.freshness == Freshness.STALE:
                    continue
                matched.append(state.target)

            matched.sort(key=lambda item: item.last_seen, reverse=True)
            return matched

    def get_target(self, target_id: str) -> Target | None:
        """Return one target by id."""

        with self._lock:
            state = self._targets.get(target_id)
            if state is None:
                return None
            self._refresh_freshness(state, self._clock())
            return state.target

    def get_target_state(self, target_id: str) -> LiveTargetState | None:
        """Return one full live-target state snapshot by id."""

        with self._lock:
            state = self._targets.get(target_id)
            if state is None:
                return None
            self._refresh_freshness(state, self._clock())
            return self._snapshot(state)

    def get_stats(self) -> dict[str, int]:
        """Return summary counters for current live memory state."""

        with self._lock:
            now = self._clock()
            fresh_count = 0
            aging_count = 0
            stale_count = 0
            live_aircraft_count = 0
            live_vessel_count = 0
            total_observations = 0

            for state in self._targets.values():
                self._refresh_freshness(state, now)
                total_observations += state.observation_count

                if state.target.kind == TargetKind.AIRCRAFT:
                    live_aircraft_count += 1
                elif state.target.kind == TargetKind.VESSEL:
                    live_vessel_count += 1

                if state.target.freshness == Freshness.FRESH:
                    fresh_count += 1
                elif state.target.freshness == Freshness.AGING:
                    aging_count += 1
                else:
                    stale_count += 1

            return {
                "live_aircraft_count": live_aircraft_count,
                "live_vessel_count": live_vessel_count,
                "total_live_targets": len(self._targets),
                "total_observations": total_observations,
                "fresh_count": fresh_count,
                "aging_count": aging_count,
                "stale_count": stale_count,
            }

    def _calculate_freshness(self, last_seen: datetime, now: datetime) -> Freshness:
        age_seconds = max(0.0, (now - _ensure_aware(last_seen)).total_seconds())
        if age_seconds <= self._fresh_seconds:
            return Freshness.FRESH
        if age_seconds <= self._aging_seconds:
            return Freshness.AGING
        return Freshness.STALE

    def _refresh_freshness(self, state: LiveTargetState, now: datetime) -> None:
        freshness = self._calculate_freshness(state.target.last_seen, now)
        stale_since = state.stale_since
        if freshness == Freshness.STALE:
            stale_since = stale_since or now
        else:
            stale_since = None
        state.target = replace(state.target, freshness=freshness)
        state.stale_since = stale_since

    def _append_position_if_valid(
        self, state: LiveTargetState, observation: NormalizedObservation
    ) -> None:
        if observation.lat is None or observation.lon is None:
            return

        sample = PositionSample(
            ts=observation.observed_at,
            lat=observation.lat,
            lon=observation.lon,
            course=observation.course,
            speed=observation.speed,
            altitude=observation.altitude,
        )

        if state.positions and _same_position(state.positions[-1], sample):
            return

        state.positions.append(sample)

    def _snapshot(self, state: LiveTargetState) -> LiveTargetState:
        return LiveTargetState(
            target=state.target,
            positions=deque(state.positions, maxlen=state.positions.maxlen),
            observation_count=state.observation_count,
            last_source_message_ts=state.last_source_message_ts,
            stale_since=state.stale_since,
        )


def _same_position(left: PositionSample, right: PositionSample) -> bool:
    return (
        left.lat == right.lat
        and left.lon == right.lon
        and left.course == right.course
        and left.speed == right.speed
        and left.altitude == right.altitude
    )


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
