"""OGN/FLARM/ADS-L ingest adapter for APRS-like decoder lines over TCP."""

from __future__ import annotations

import re
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import Config
from app.models import NormalizedObservation, ScanBand, Source, TargetKind

_POSITION_RE = re.compile(
    r"^[!/=@]?"
    r"(?P<timestamp>\d{6})h"
    r"(?P<lat_deg>\d{2})(?P<lat_min>\d{2}\.\d{2})(?P<lat_hemi>[NS])"
    r"(?P<symbol_table>.)"
    r"(?P<lon_deg>\d{3})(?P<lon_min>\d{2}\.\d{2})(?P<lon_hemi>[EW])"
    r"(?P<symbol>.)"
    r"(?P<course>[0-9.]{3})/(?P<speed>[0-9.]{3})"
    r"/A=(?P<altitude>\d{6})"
    r"(?:\s+!W..!)?"
    r"(?:\s+(?P<comment>.*))?$"
)
_HEADER_RE = re.compile(r"^(?P<sender>[^>]+)>(?P<tocall>[^,]+)(?:,(?P<path>.*))?$")
_DEVICE_RE = re.compile(r"\bid(?P<addr_type>[0-9A-Fa-f])(?P<aircraft_type>[0-9A-Fa-f])(?P<address>[0-9A-Fa-f]{6})\b")
_CLIMB_RE = re.compile(r"(?P<value>[+-]\d{3,4})fpm\b", re.IGNORECASE)
_TURN_RATE_RE = re.compile(r"(?P<value>[+-]\d+(?:\.\d+)?)rot\b", re.IGNORECASE)
_ICAO_SENDER_RE = re.compile(r"^ICA(?P<icao24>[0-9A-Fa-f]{6})$")

ADDRESS_TYPE_LABELS = {
    0: "unknown",
    1: "icao",
    2: "flarm",
    3: "ogn",
}
AIRCRAFT_TYPE_LABELS = {
    0: "unknown",
    1: "glider",
    2: "towplane",
    3: "helicopter",
    4: "parachute",
    5: "drop-plane",
    6: "hang-glider",
    7: "paraglider",
    8: "aircraft",
    9: "jet",
    10: "ufo",
    11: "balloon",
    12: "airship",
    13: "uav",
    14: "static-object",
    15: "other",
}


class OGNIngestError(RuntimeError):
    """Raised when OGN/FLARM/ADS-L data cannot be read from the configured source."""


@dataclass(slots=True)
class OGNTCPIngestor:
    """TCP-backed OGN ingestor that reads APRS lines and normalizes aircraft beacons."""

    host: str
    port: int

    @classmethod
    def from_config(cls, config: Config) -> "OGNTCPIngestor":
        return cls(host=config.ogn_tcp_host, port=config.ogn_tcp_port)

    def read_observations(
        self,
        *,
        timeout_seconds: float = 1.0,
        max_lines: int = 500,
    ) -> list[NormalizedObservation]:
        lines = read_ogn_lines_from_tcp(
            host=self.host,
            port=self.port,
            timeout_seconds=timeout_seconds,
            max_lines=max_lines,
        )
        return parse_ogn_aprs_lines(lines)


def read_ogn_lines_from_tcp(
    *,
    host: str,
    port: int,
    timeout_seconds: float = 1.0,
    max_lines: int = 500,
) -> list[str]:
    """Read OGN APRS lines from a TCP source."""

    if max_lines <= 0:
        raise ValueError("max_lines must be > 0")

    deadline = time.monotonic() + timeout_seconds
    last_error: OSError | None = None

    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        connect_timeout = max(0.05, min(1.0, remaining))
        try:
            with socket.create_connection((host, port), timeout=connect_timeout) as sock:
                with sock.makefile("r", encoding="utf-8", errors="replace") as stream:
                    lines: list[str] = []
                    while len(lines) < max_lines and time.monotonic() < deadline:
                        read_timeout = max(0.1, min(1.0, deadline - time.monotonic()))
                        sock.settimeout(read_timeout)
                        try:
                            line = stream.readline()
                        except socket.timeout:
                            continue
                        if not line:
                            break
                        stripped = line.strip()
                        if stripped:
                            lines.append(stripped)
                    if lines:
                        return lines
        except OSError as exc:
            last_error = exc
            if time.monotonic() < deadline:
                time.sleep(0.1)

    if last_error is not None:
        raise OGNIngestError(f"Failed to read OGN stream from {host}:{port}") from last_error
    return []


def parse_ogn_aprs_lines(
    lines: list[str],
    *,
    observed_at: datetime | None = None,
) -> list[NormalizedObservation]:
    """Parse APRS-like OGN lines into normalized aircraft observations."""

    reference_time = observed_at or datetime.now(timezone.utc)
    observations: list[NormalizedObservation] = []
    for line in lines:
        observation = parse_ogn_aprs_line(line, observed_at=reference_time)
        if observation is not None:
            observations.append(observation)
    return observations


def parse_ogn_aprs_line(
    line: str,
    *,
    observed_at: datetime,
) -> NormalizedObservation | None:
    """Parse one OGN APRS line into a normalized observation."""

    text = line.strip()
    if not text or text.startswith("#") or ":" not in text or ">" not in text:
        return None

    header, body = text.split(":", 1)
    header_match = _HEADER_RE.match(header.strip())
    if header_match is None:
        return None
    sender = header_match.group("sender").strip()
    tocall = header_match.group("tocall").strip().upper()
    relay_path = [
        part.strip()
        for part in (header_match.group("path") or "").split(",")
        if part.strip()
    ]
    if not sender or not tocall:
        return None

    match = _POSITION_RE.match(body.strip())
    if match is None:
        return None

    comment = (match.group("comment") or "").strip()
    device_match = _DEVICE_RE.search(comment)
    if device_match is None:
        return None

    address_type_code = int(device_match.group("addr_type"), 16)
    aircraft_type_code = int(device_match.group("aircraft_type"), 16)
    address = device_match.group("address").lower()
    address_type_label = ADDRESS_TYPE_LABELS.get(address_type_code, "unknown")
    aircraft_type_label = AIRCRAFT_TYPE_LABELS.get(aircraft_type_code, "other")

    lat = _parse_aprs_lat(
        match.group("lat_deg"),
        match.group("lat_min"),
        match.group("lat_hemi"),
    )
    lon = _parse_aprs_lon(
        match.group("lon_deg"),
        match.group("lon_min"),
        match.group("lon_hemi"),
    )

    if lat is None or lon is None:
        return None

    device_id = f"{address_type_label}-{address}"
    climb_rate = _extract_optional_float(comment, _CLIMB_RE)
    turn_rate = _extract_optional_float(comment, _TURN_RATE_RE)
    timestamp = _resolve_aprs_timestamp(match.group("timestamp"), reference_time=observed_at)
    sender_icao24 = _extract_sender_icao24(sender)
    protocol = _infer_protocol(
        sender=sender,
        tocall=tocall,
        relay_path=relay_path,
        address_type_label=address_type_label,
    )
    icao24 = sender_icao24 or (address if address_type_label == "icao" else None)

    return NormalizedObservation(
        target_id=f"ogn:{device_id}",
        source=Source.OGN,
        kind=TargetKind.AIRCRAFT,
        observed_at=timestamp,
        label=sender,
        lat=lat,
        lon=lon,
        course=_parse_optional_number(match.group("course")),
        speed=_parse_optional_number(match.group("speed")),
        altitude=float(int(match.group("altitude"))),
        last_scan_band=ScanBand.OGN,
        icao24=icao24,
        callsign=sender,
        vertical_rate=climb_rate,
        payload_json={
            "raw": text,
            "sender": sender,
            "tocall": tocall,
            "relay_path": relay_path,
            "network": tocall.lower(),
            "protocol": protocol,
            "device_id": device_id,
            "address": address,
            "address_type": address_type_label,
            "address_type_code": address_type_code,
            "aircraft_type": aircraft_type_label,
            "aircraft_type_code": aircraft_type_code,
            "sender_declared_icao24": sender_icao24,
            "climb_rate_fpm": climb_rate,
            "turn_rate_rot": turn_rate,
            "comment": comment,
        },
    )


def _parse_aprs_lat(degrees_text: str, minutes_text: str, hemisphere: str) -> float | None:
    try:
        degrees = int(degrees_text)
        minutes = float(minutes_text)
    except ValueError:
        return None
    value = degrees + (minutes / 60.0)
    if hemisphere.upper() == "S":
        value = -value
    if value < -90 or value > 90:
        return None
    return value


def _parse_aprs_lon(degrees_text: str, minutes_text: str, hemisphere: str) -> float | None:
    try:
        degrees = int(degrees_text)
        minutes = float(minutes_text)
    except ValueError:
        return None
    value = degrees + (minutes / 60.0)
    if hemisphere.upper() == "W":
        value = -value
    if value < -180 or value > 180:
        return None
    return value


def _parse_optional_number(value: str) -> float | None:
    stripped = value.strip()
    if not stripped or "." in stripped:
        return None
    try:
        return float(int(stripped))
    except ValueError:
        return None


def _extract_optional_float(text: str, pattern: re.Pattern[str]) -> float | None:
    match = pattern.search(text)
    if match is None:
        return None
    try:
        return float(match.group("value"))
    except ValueError:
        return None


def _resolve_aprs_timestamp(value: str, *, reference_time: datetime) -> datetime:
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)

    hours = int(value[0:2])
    minutes = int(value[2:4])
    seconds = int(value[4:6])
    candidate = reference_time.replace(hour=hours, minute=minutes, second=seconds, microsecond=0)
    if candidate - reference_time > timedelta(hours=12):
        candidate -= timedelta(days=1)
    elif reference_time - candidate > timedelta(hours=12):
        candidate += timedelta(days=1)
    return candidate


def _extract_sender_icao24(sender: str) -> str | None:
    match = _ICAO_SENDER_RE.match(sender.strip().upper())
    if match is None:
        return None
    return match.group("icao24").lower()


def _infer_protocol(
    *,
    sender: str,
    tocall: str,
    relay_path: list[str],
    address_type_label: str,
) -> str:
    sender_upper = sender.strip().upper()
    relay_markers = {part.strip().lower() for part in relay_path}
    if (
        tocall == "OGNSKY"
        or "safesky" in relay_markers
        or "ads-l" in relay_markers
        or "adsl" in relay_markers
        or sender_upper.startswith("ADL")
    ):
        return "ads-l"
    if address_type_label == "flarm" or sender_upper.startswith("FLR"):
        return "flarm"
    if address_type_label == "ogn" or tocall.startswith("OGN"):
        return "ogn"
    if address_type_label == "icao":
        return "icao-relay"
    return "unknown"
