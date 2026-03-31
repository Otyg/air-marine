"""Loader for static radar objects configured in a JSON file."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class FixedRadarObject:
    """Renderable static object on the radar view."""

    name: str
    lat: float
    lon: float
    symbol: str = "O"
    max_visible_range_km: float | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "lat": self.lat,
            "lon": self.lon,
            "symbol": self.symbol or "O",
        }
        if self.max_visible_range_km is not None:
            payload["max_visible_range_km"] = self.max_visible_range_km
        return payload


def load_fixed_radar_objects(path: Path, *, logger: Any | None = None) -> list[FixedRadarObject]:
    """Load static radar objects from a JSON array config file."""

    resolved_path = path.expanduser()
    if not resolved_path.exists():
        return []

    try:
        raw_payload = json.loads(resolved_path.read_text(encoding="utf-8"))
    except Exception as exc:
        _warn(logger, "Ignoring fixed objects config %s (invalid JSON: %s).", resolved_path, exc)
        return []

    if not isinstance(raw_payload, list):
        _warn(
            logger,
            "Ignoring fixed objects config %s (expected a JSON array).",
            resolved_path,
        )
        return []

    objects: list[FixedRadarObject] = []
    for index, raw_item in enumerate(raw_payload, start=1):
        if not isinstance(raw_item, dict):
            _warn(
                logger,
                "Skipping fixed object #%s in %s (expected object).",
                index,
                resolved_path,
            )
            continue

        name = str(raw_item.get("name", "")).strip()
        if not name:
            _warn(
                logger,
                "Skipping fixed object #%s in %s (missing name).",
                index,
                resolved_path,
            )
            continue

        lat = _to_float(raw_item.get("latitude"))
        lon = _to_float(raw_item.get("longitude"))
        if not (-90 <= lat <= 90):
            _warn(
                logger,
                "Skipping fixed object %r in %s (latitude must be in -90..90).",
                name,
                resolved_path,
            )
            continue
        if not (-180 <= lon <= 180):
            _warn(
                logger,
                "Skipping fixed object %r in %s (longitude must be in -180..180).",
                name,
                resolved_path,
            )
            continue

        symbol = _normalize_symbol(raw_item.get("symbol"))
        max_visible_range_km: float | None = None
        if "max_visible_range_km" in raw_item:
            raw_max_visible_range_km = raw_item.get("max_visible_range_km")
            if raw_max_visible_range_km is not None:
                candidate = _to_float(raw_max_visible_range_km)
                if math.isfinite(candidate) and candidate > 0:
                    max_visible_range_km = candidate
                else:
                    _warn(
                        logger,
                        (
                            "Ignoring max_visible_range_km for fixed object %r in %s "
                            "(must be a positive number)."
                        ),
                        name,
                        resolved_path,
                    )

        objects.append(
            FixedRadarObject(
                name=name,
                lat=lat,
                lon=lon,
                symbol=symbol,
                max_visible_range_km=max_visible_range_km,
            )
        )

    return objects


def _to_float(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value.strip())
        except ValueError:
            return float("nan")
    return float("nan")


def _normalize_symbol(value: Any) -> str:
    if isinstance(value, str):
        trimmed = value.strip()
        if trimmed:
            return trimmed[0]
    return "O"


def _warn(logger: Any | None, message: str, *args: Any) -> None:
    if logger is None:
        return
    try:
        logger.warning(message, *args)
    except Exception:
        pass
