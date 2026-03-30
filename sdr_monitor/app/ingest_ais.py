"""AIS ingest adapter for AIVDM/AIVDO sentences over TCP."""

from __future__ import annotations

import math
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import Config
from app.models import NormalizedObservation, ScanBand, Source, TargetKind

AIS_TEXT_ALPHABET = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"
NAV_STATUS_MAP = {
    0: "under way using engine",
    1: "at anchor",
    2: "not under command",
    3: "restricted manoeuverability",
    4: "constrained by draft",
    5: "moored",
    6: "aground",
    7: "engaged in fishing",
    8: "under way sailing",
    9: "reserved",
    10: "reserved",
    11: "reserved",
    12: "reserved",
    13: "reserved",
    14: "ais-sart",
    15: "not defined",
}


class AISIngestError(RuntimeError):
    """Raised when AIS data cannot be read from the configured source."""


@dataclass(frozen=True, slots=True)
class AISSentence:
    message_type: str
    total_fragments: int
    fragment_number: int
    sequence_id: str | None
    channel: str | None
    payload: str
    fill_bits: int
    raw: str


@dataclass(slots=True)
class _PendingFragments:
    total_fragments: int
    created_at: datetime
    payload_by_index: dict[int, str]
    fill_bits: int = 0


class AISFragmentAssembler:
    """Assembler for multipart AIVDM/AIVDO sentences."""

    def __init__(self, max_age_seconds: float = 60.0) -> None:
        self._max_age = timedelta(seconds=max_age_seconds)
        self._pending: dict[tuple[str, str], _PendingFragments] = {}

    def add(
        self,
        sentence: AISSentence,
        *,
        now: datetime,
    ) -> tuple[str, int, list[str]] | None:
        self._purge_stale(now)

        if sentence.total_fragments <= 1:
            return sentence.payload, sentence.fill_bits, [sentence.raw]

        key = self._build_key(sentence)
        pending = self._pending.get(key)
        if pending is None or sentence.fragment_number == 1:
            pending = _PendingFragments(
                total_fragments=sentence.total_fragments,
                created_at=now,
                payload_by_index={},
                fill_bits=sentence.fill_bits if sentence.fragment_number == sentence.total_fragments else 0,
            )
            self._pending[key] = pending

        if pending.total_fragments != sentence.total_fragments:
            pending = _PendingFragments(
                total_fragments=sentence.total_fragments,
                created_at=now,
                payload_by_index={},
                fill_bits=0,
            )
            self._pending[key] = pending

        pending.payload_by_index[sentence.fragment_number] = sentence.payload
        if sentence.fragment_number == sentence.total_fragments:
            pending.fill_bits = sentence.fill_bits

        if len(pending.payload_by_index) < sentence.total_fragments:
            return None

        payload = "".join(
            pending.payload_by_index[index]
            for index in range(1, sentence.total_fragments + 1)
        )
        raw_parts = [
            pending.payload_by_index[index]
            for index in range(1, sentence.total_fragments + 1)
        ]
        del self._pending[key]
        return payload, pending.fill_bits, raw_parts

    def _build_key(self, sentence: AISSentence) -> tuple[str, str]:
        if sentence.sequence_id:
            return sentence.message_type, sentence.sequence_id
        return sentence.message_type, f"anonymous-{sentence.channel or '?'}"

    def _purge_stale(self, now: datetime) -> None:
        to_delete = [
            key
            for key, pending in self._pending.items()
            if now - pending.created_at > self._max_age
        ]
        for key in to_delete:
            del self._pending[key]


@dataclass(slots=True)
class AISTCPIngestor:
    """TCP-backed AIS ingestor that reads NMEA lines and normalizes them."""

    host: str
    port: int

    @classmethod
    def from_config(cls, config: Config) -> "AISTCPIngestor":
        return cls(host=config.ais_tcp_host, port=config.ais_tcp_port)

    def read_observations(
        self,
        *,
        timeout_seconds: float = 1.0,
        max_lines: int = 500,
    ) -> list[NormalizedObservation]:
        lines = read_ais_lines_from_tcp(
            host=self.host,
            port=self.port,
            timeout_seconds=timeout_seconds,
            max_lines=max_lines,
        )
        return parse_ais_nmea_lines(lines)


def read_ais_lines_from_tcp(
    *,
    host: str,
    port: int,
    timeout_seconds: float = 1.0,
    max_lines: int = 500,
) -> list[str]:
    """Read AIS NMEA lines from a TCP source."""

    if max_lines <= 0:
        raise ValueError("max_lines must be > 0")

    try:
        with socket.create_connection((host, port), timeout=timeout_seconds) as sock:
            sock.settimeout(timeout_seconds)
            with sock.makefile("r", encoding="utf-8", errors="replace") as stream:
                lines: list[str] = []
                while len(lines) < max_lines:
                    try:
                        line = stream.readline()
                    except socket.timeout:
                        break
                    if not line:
                        break
                    stripped = line.strip()
                    if stripped:
                        lines.append(stripped)
                return lines
    except OSError as exc:
        raise AISIngestError(f"Failed to read AIS stream from {host}:{port}") from exc


def parse_ais_nmea_lines(
    lines: list[str],
    *,
    observed_at: datetime | None = None,
) -> list[NormalizedObservation]:
    """Parse AIVDM/AIVDO lines and emit normalized observations."""

    timestamp = observed_at or datetime.now(timezone.utc)
    assembler = AISFragmentAssembler()
    observations: list[NormalizedObservation] = []

    for line in lines:
        sentence = parse_ais_sentence(line)
        if sentence is None:
            continue

        assembled = assembler.add(sentence, now=timestamp)
        if assembled is None:
            continue

        payload, fill_bits, raw_parts = assembled
        decoded = decode_ais_payload(payload, fill_bits=fill_bits)
        observation = _to_observation(decoded, observed_at=timestamp, raw_parts=raw_parts)
        if observation is not None:
            observations.append(observation)

    return observations


def parse_ais_sentence(line: str) -> AISSentence | None:
    """Parse one raw NMEA AIS sentence."""

    text = line.strip()
    if not text.startswith("!AIVDM") and not text.startswith("!AIVDO"):
        return None
    if "*" not in text:
        return None

    body, checksum_text = text.split("*", 1)
    if len(checksum_text) < 2:
        return None
    expected = _nmea_checksum(body)
    try:
        given = int(checksum_text[:2], 16)
    except ValueError:
        return None
    if expected != given:
        return None

    parts = body.split(",")
    if len(parts) < 7:
        return None

    try:
        total_fragments = int(parts[1])
        fragment_number = int(parts[2])
        fill_bits = int(parts[6])
    except ValueError:
        return None

    if total_fragments <= 0 or fragment_number <= 0 or fragment_number > total_fragments:
        return None
    if fill_bits < 0 or fill_bits > 5:
        return None

    return AISSentence(
        message_type=parts[0][1:],
        total_fragments=total_fragments,
        fragment_number=fragment_number,
        sequence_id=parts[3] or None,
        channel=parts[4] or None,
        payload=parts[5],
        fill_bits=fill_bits,
        raw=text,
    )


def decode_ais_payload(payload: str, *, fill_bits: int) -> dict[str, Any]:
    """Decode AIS six-bit payload into a field dictionary."""

    bitstring = _payload_to_bitstring(payload, fill_bits=fill_bits)
    if len(bitstring) < 38:
        return {}

    message_type = _u(bitstring, 0, 6)
    mmsi = _u(bitstring, 8, 30)
    if mmsi == 0:
        return {}

    decoded: dict[str, Any] = {
        "message_type": message_type,
        "mmsi": f"{mmsi:09d}",
    }

    if message_type in {1, 2, 3}:
        decoded["nav_status"] = NAV_STATUS_MAP.get(_u(bitstring, 38, 4))
        decoded["speed"] = _decode_speed(_u(bitstring, 50, 10))
        decoded["lon"] = _decode_lon(_s(bitstring, 61, 28))
        decoded["lat"] = _decode_lat(_s(bitstring, 89, 27))
        decoded["course"] = _decode_course(_u(bitstring, 116, 12))
    elif message_type in {18, 19}:
        decoded["speed"] = _decode_speed(_u(bitstring, 46, 10))
        decoded["lon"] = _decode_lon(_s(bitstring, 57, 28))
        decoded["lat"] = _decode_lat(_s(bitstring, 85, 27))
        decoded["course"] = _decode_course(_u(bitstring, 112, 12))
        if message_type == 19 and len(bitstring) >= 263:
            decoded["shipname"] = _decode_text(bitstring, 143, 120)
    elif message_type == 5 and len(bitstring) >= 232:
        decoded["shipname"] = _decode_text(bitstring, 112, 120)
    elif message_type == 24 and len(bitstring) >= 160:
        part_no = _u(bitstring, 38, 2)
        decoded["part_no"] = part_no
        if part_no == 0:
            decoded["shipname"] = _decode_text(bitstring, 40, 120)

    return decoded


def _to_observation(
    decoded: dict[str, Any],
    *,
    observed_at: datetime,
    raw_parts: list[str],
) -> NormalizedObservation | None:
    mmsi = decoded.get("mmsi")
    if not isinstance(mmsi, str) or not mmsi:
        return None

    shipname = _clean_text(decoded.get("shipname"))
    nav_status = _clean_text(decoded.get("nav_status"))
    label = shipname or mmsi

    return NormalizedObservation(
        target_id=f"ais:{mmsi}",
        source=Source.AIS,
        kind=TargetKind.VESSEL,
        observed_at=_ensure_aware(observed_at),
        label=label,
        lat=_to_optional_float(decoded.get("lat")),
        lon=_to_optional_float(decoded.get("lon")),
        course=_to_optional_float(decoded.get("course")),
        speed=_to_optional_float(decoded.get("speed")),
        altitude=None,
        last_scan_band=ScanBand.AIS,
        mmsi=mmsi,
        shipname=shipname,
        nav_status=nav_status,
        payload_json={
            "decoded": decoded,
            "raw_payload_parts": raw_parts,
        },
    )


def _payload_to_bitstring(payload: str, *, fill_bits: int) -> str:
    bits = "".join(f"{_ais_char_to_sixbit(ch):06b}" for ch in payload)
    if fill_bits:
        if fill_bits > len(bits):
            return ""
        bits = bits[:-fill_bits]
    return bits


def _ais_char_to_sixbit(ch: str) -> int:
    value = ord(ch) - 48
    if value > 40:
        value -= 8
    if value < 0 or value > 63:
        raise ValueError(f"Invalid AIS payload char: {ch!r}")
    return value


def _nmea_checksum(body: str) -> int:
    checksum = 0
    for char in body[1:]:
        checksum ^= ord(char)
    return checksum


def _u(bits: str, offset: int, length: int) -> int:
    segment = bits[offset : offset + length]
    if len(segment) < length:
        return 0
    return int(segment, 2)


def _s(bits: str, offset: int, length: int) -> int:
    value = _u(bits, offset, length)
    sign_bit = 1 << (length - 1)
    if value & sign_bit:
        value -= 1 << length
    return value


def _decode_lon(raw: int) -> float | None:
    if raw == 0x6791AC0:
        return None
    value = raw / 600000.0
    if value < -180 or value > 180:
        return None
    return value


def _decode_lat(raw: int) -> float | None:
    if raw == 0x3412140:
        return None
    value = raw / 600000.0
    if value < -90 or value > 90:
        return None
    return value


def _decode_speed(raw: int) -> float | None:
    if raw in {1023, 1022}:
        return None
    return raw / 10.0


def _decode_course(raw: int) -> float | None:
    if raw == 3600:
        return None
    return raw / 10.0


def _decode_text(bits: str, offset: int, length: int) -> str | None:
    segment = bits[offset : offset + length]
    if len(segment) < length:
        return None
    chars: list[str] = []
    for idx in range(0, len(segment), 6):
        value = int(segment[idx : idx + 6], 2)
        if value >= len(AIS_TEXT_ALPHABET):
            continue
        chars.append(AIS_TEXT_ALPHABET[value])
    text = "".join(chars).replace("@", " ").strip()
    return text or None


def _to_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(parsed):
        return None
    return parsed


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
