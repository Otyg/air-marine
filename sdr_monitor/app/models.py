"""Core domain models for normalized target tracking."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

DEFAULT_POSITION_HISTORY_MAXLEN = 5


class Source(str, Enum):
    ADSB = "adsb"
    AIS = "ais"
    OGN = "ogn"


class TargetKind(str, Enum):
    AIRCRAFT = "aircraft"
    VESSEL = "vessel"


class Freshness(str, Enum):
    FRESH = "fresh"
    AGING = "aging"
    STALE = "stale"


class ScanBand(str, Enum):
    ADSB = "adsb"
    AIS = "ais"
    OGN = "ogn"


def _serialize_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _parse_dt(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(frozen=True, slots=True)
class PositionSample:
    ts: datetime
    lat: float
    lon: float
    course: float | None = None
    speed: float | None = None
    altitude: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": _serialize_dt(self.ts),
            "lat": self.lat,
            "lon": self.lon,
            "course": self.course,
            "speed": self.speed,
            "altitude": self.altitude,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PositionSample":
        return cls(
            ts=_parse_dt(payload["ts"]),
            lat=float(payload["lat"]),
            lon=float(payload["lon"]),
            course=_to_optional_float(payload.get("course")),
            speed=_to_optional_float(payload.get("speed")),
            altitude=_to_optional_float(payload.get("altitude")),
        )


@dataclass(frozen=True, slots=True)
class NormalizedObservation:
    target_id: str
    source: Source
    kind: TargetKind
    observed_at: datetime
    label: str | None = None
    lat: float | None = None
    lon: float | None = None
    course: float | None = None
    speed: float | None = None
    altitude: float | None = None
    last_scan_band: ScanBand | None = None

    icao24: str | None = None
    callsign: str | None = None
    squawk: str | None = None
    vertical_rate: float | None = None

    mmsi: str | None = None
    shipname: str | None = None
    nav_status: str | None = None

    payload_json: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "source": self.source.value,
            "kind": self.kind.value,
            "observed_at": _serialize_dt(self.observed_at),
            "label": self.label,
            "lat": self.lat,
            "lon": self.lon,
            "course": self.course,
            "speed": self.speed,
            "altitude": self.altitude,
            "last_scan_band": self.last_scan_band.value if self.last_scan_band else None,
            "icao24": self.icao24,
            "callsign": self.callsign,
            "squawk": self.squawk,
            "vertical_rate": self.vertical_rate,
            "mmsi": self.mmsi,
            "shipname": self.shipname,
            "nav_status": self.nav_status,
            "payload_json": self.payload_json,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "NormalizedObservation":
        return cls(
            target_id=payload["target_id"],
            source=Source(payload["source"]),
            kind=TargetKind(payload["kind"]),
            observed_at=_parse_dt(payload["observed_at"]),
            label=payload.get("label"),
            lat=_to_optional_float(payload.get("lat")),
            lon=_to_optional_float(payload.get("lon")),
            course=_to_optional_float(payload.get("course")),
            speed=_to_optional_float(payload.get("speed")),
            altitude=_to_optional_float(payload.get("altitude")),
            last_scan_band=(
                ScanBand(payload["last_scan_band"])
                if payload.get("last_scan_band")
                else None
            ),
            icao24=payload.get("icao24"),
            callsign=payload.get("callsign"),
            squawk=payload.get("squawk"),
            vertical_rate=_to_optional_float(payload.get("vertical_rate")),
            mmsi=payload.get("mmsi"),
            shipname=payload.get("shipname"),
            nav_status=payload.get("nav_status"),
            payload_json=dict(payload.get("payload_json", {})),
        )


@dataclass(frozen=True, slots=True)
class Target:
    target_id: str
    source: Source
    kind: TargetKind
    label: str | None
    lat: float | None
    lon: float | None
    course: float | None
    speed: float | None
    altitude: float | None
    first_seen: datetime
    last_seen: datetime
    freshness: Freshness
    last_scan_band: ScanBand | None
    icao24: str | None = None
    callsign: str | None = None
    squawk: str | None = None
    vertical_rate: float | None = None
    mmsi: str | None = None
    shipname: str | None = None
    nav_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "source": self.source.value,
            "kind": self.kind.value,
            "label": self.label,
            "lat": self.lat,
            "lon": self.lon,
            "course": self.course,
            "speed": self.speed,
            "altitude": self.altitude,
            "first_seen": _serialize_dt(self.first_seen),
            "last_seen": _serialize_dt(self.last_seen),
            "freshness": self.freshness.value,
            "last_scan_band": self.last_scan_band.value if self.last_scan_band else None,
            "icao24": self.icao24,
            "callsign": self.callsign,
            "squawk": self.squawk,
            "vertical_rate": self.vertical_rate,
            "mmsi": self.mmsi,
            "shipname": self.shipname,
            "nav_status": self.nav_status,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Target":
        return cls(
            target_id=payload["target_id"],
            source=Source(payload["source"]),
            kind=TargetKind(payload["kind"]),
            label=payload.get("label"),
            lat=_to_optional_float(payload.get("lat")),
            lon=_to_optional_float(payload.get("lon")),
            course=_to_optional_float(payload.get("course")),
            speed=_to_optional_float(payload.get("speed")),
            altitude=_to_optional_float(payload.get("altitude")),
            first_seen=_parse_dt(payload["first_seen"]),
            last_seen=_parse_dt(payload["last_seen"]),
            freshness=Freshness(payload["freshness"]),
            last_scan_band=(
                ScanBand(payload["last_scan_band"])
                if payload.get("last_scan_band")
                else None
            ),
            icao24=payload.get("icao24"),
            callsign=payload.get("callsign"),
            squawk=payload.get("squawk"),
            vertical_rate=_to_optional_float(payload.get("vertical_rate")),
            mmsi=payload.get("mmsi"),
            shipname=payload.get("shipname"),
            nav_status=payload.get("nav_status"),
        )


@dataclass(frozen=True, slots=True)
class HistoricalTargetSummary:
    target_id: str
    source: Source
    kind: TargetKind
    label: str | None
    last_seen: datetime
    position_count: int
    max_observed_speed: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "source": self.source.value,
            "kind": self.kind.value,
            "label": self.label,
            "last_seen": _serialize_dt(self.last_seen),
            "position_count": self.position_count,
            "max_observed_speed": self.max_observed_speed,
        }


@dataclass(slots=True)
class LiveTargetState:
    target: Target
    positions: deque[PositionSample] = field(
        default_factory=deque
    )
    observation_count: int = 0
    last_source_message_ts: datetime | None = None
    stale_since: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.positions, deque):
            self.positions = deque(self.positions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target.to_dict(),
            "positions": [sample.to_dict() for sample in self.positions],
            "observation_count": self.observation_count,
            "last_source_message_ts": _serialize_dt(self.last_source_message_ts),
            "stale_since": _serialize_dt(self.stale_since),
        }

    @classmethod
    def from_dict(
        cls, payload: dict[str, Any], max_positions: int = DEFAULT_POSITION_HISTORY_MAXLEN
    ) -> "LiveTargetState":
        samples = deque(
            (
                PositionSample.from_dict(item)
                for item in payload.get("positions", [])
            ),
        )

        return cls(
            target=Target.from_dict(payload["target"]),
            positions=samples,
            observation_count=int(payload.get("observation_count", 0)),
            last_source_message_ts=_parse_dt(payload.get("last_source_message_ts")),
            stale_since=_parse_dt(payload.get("stale_since")),
        )


def _to_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)
