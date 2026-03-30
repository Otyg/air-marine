"""ADS-B ingest adapter for readsb aircraft.json snapshots."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from app.config import Config
from app.models import NormalizedObservation, ScanBand, Source, TargetKind

ICAO24_RE = re.compile(r"^[0-9a-fA-F]{6}$")


class ADSBIngestError(RuntimeError):
    """Raised when readsb data cannot be read or parsed."""


@dataclass(slots=True)
class ADSBAircraftJsonIngestor:
    """File-backed adapter that reads and parses readsb `aircraft.json`."""

    aircraft_json_path: Path

    @classmethod
    def from_config(cls, config: Config) -> "ADSBAircraftJsonIngestor":
        return cls(aircraft_json_path=config.readsb_aircraft_json)

    def read_observations(self) -> list[NormalizedObservation]:
        payload = load_readsb_aircraft_json(self.aircraft_json_path)
        return parse_readsb_aircraft_json(payload)


def load_readsb_aircraft_json(path: str | Path) -> dict[str, Any]:
    """Load the readsb aircraft snapshot payload from disk."""

    aircraft_path = Path(path)
    try:
        raw = aircraft_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ADSBIngestError(f"Failed to read ADS-B snapshot: {aircraft_path}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ADSBIngestError(f"Invalid JSON in ADS-B snapshot: {aircraft_path}") from exc

    if not isinstance(payload, dict):
        raise ADSBIngestError("ADS-B snapshot must be a JSON object.")
    return payload


def parse_readsb_aircraft_json(
    payload: Mapping[str, Any],
    *,
    fallback_observed_at: datetime | None = None,
) -> list[NormalizedObservation]:
    """Normalize readsb `aircraft.json` payload into shared observations."""

    fallback = fallback_observed_at or datetime.now(timezone.utc)
    snapshot_time = _parse_snapshot_time(payload.get("now"), fallback=fallback)
    rows = payload.get("aircraft")
    if not isinstance(rows, list):
        return []

    observations: list[NormalizedObservation] = []
    for row in rows:
        observation = _parse_aircraft_row(row, snapshot_time=snapshot_time)
        if observation is not None:
            observations.append(observation)
    return observations


def _parse_aircraft_row(
    row: Any,
    *,
    snapshot_time: datetime,
) -> NormalizedObservation | None:
    if not isinstance(row, Mapping):
        return None

    icao24 = _normalize_icao24(row.get("hex"))
    if icao24 is None:
        return None

    observed_at = _resolve_observed_at(snapshot_time, seen_value=row.get("seen"))
    callsign = _clean_text(row.get("flight"))

    lat = _parse_lat(row.get("lat"))
    lon = _parse_lon(row.get("lon"))
    course = _to_optional_float(row.get("track"))
    speed = _to_optional_float(row.get("gs"))
    altitude = _parse_altitude(row.get("alt_baro"), row.get("alt_geom"))
    vertical_rate = _parse_vertical_rate(row.get("baro_rate"), row.get("geom_rate"))

    return NormalizedObservation(
        target_id=f"adsb:{icao24}",
        source=Source.ADSB,
        kind=TargetKind.AIRCRAFT,
        observed_at=observed_at,
        label=callsign or icao24.upper(),
        lat=lat,
        lon=lon,
        course=course,
        speed=speed,
        altitude=altitude,
        last_scan_band=ScanBand.ADSB,
        icao24=icao24,
        callsign=callsign,
        squawk=_clean_text(row.get("squawk")),
        vertical_rate=vertical_rate,
        payload_json=dict(row),
    )


def _parse_snapshot_time(value: Any, fallback: datetime) -> datetime:
    now_ts = _to_optional_float(value)
    if now_ts is None:
        return _ensure_aware(fallback)
    return datetime.fromtimestamp(now_ts, tz=timezone.utc)


def _resolve_observed_at(snapshot_time: datetime, seen_value: Any) -> datetime:
    seen_seconds = _to_optional_float(seen_value)
    if seen_seconds is None or seen_seconds < 0:
        return snapshot_time
    observed_at = snapshot_time.timestamp() - seen_seconds
    return datetime.fromtimestamp(observed_at, tz=timezone.utc)


def _normalize_icao24(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not ICAO24_RE.fullmatch(candidate):
        return None
    return candidate.lower()


def _parse_altitude(primary: Any, secondary: Any) -> float | None:
    altitude = _to_optional_float(primary)
    if altitude is not None:
        return altitude
    return _to_optional_float(secondary)


def _parse_vertical_rate(primary: Any, secondary: Any) -> float | None:
    vertical_rate = _to_optional_float(primary)
    if vertical_rate is not None:
        return vertical_rate
    return _to_optional_float(secondary)


def _parse_lat(value: Any) -> float | None:
    lat = _to_optional_float(value)
    if lat is None or lat < -90 or lat > 90:
        return None
    return lat


def _parse_lon(value: Any) -> float | None:
    lon = _to_optional_float(value)
    if lon is None or lon < -180 or lon > 180:
        return None
    return lon


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _to_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        trimmed = value.strip().lower()
        if trimmed in {"", "ground", "nan", "none", "null"}:
            return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(parsed):
        return None
    return parsed


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
