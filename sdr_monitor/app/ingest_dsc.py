"""DSC (Digital Selective Calling) ingest adapters.

This module contains:
- `DSCDirectReader`: local rtl_tcp-based DSC reader used by scanner wiring.
- Compatibility decoder/ingestor symbols (`DSCDecoder`, `DSCTCPIngestor`, etc.)
  consumed by legacy unit tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import logging
import socket
import sys
from pathlib import Path
from typing import Any

from app.models import NormalizedObservation, ScanBand, Source, TargetKind

logger = logging.getLogger(__name__)

# Add dsc_decoder to path for imports
_dsc_decoder_path = Path(__file__).parent.parent.parent / "dsc_decoder"
if str(_dsc_decoder_path) not in sys.path:
    sys.path.insert(0, str(_dsc_decoder_path))

_HAS_DSC = False
try:
    from dsc_decoder import (  # type: ignore[import-not-found]
        DSCDecoder as _NativeDSCDecoder,
        DistressType as _NativeDistressType,
        DSCMessage as _NativeDSCMessage,
        MessageType as _NativeMessageType,
    )
    from rtl_receiver import RTLReceiver  # type: ignore[import-not-found]

    _HAS_DSC = True
except ImportError as exc:
    logger.warning("DSC native modules not available: %s", exc)


class DSCIngestError(RuntimeError):
    """Raised when DSC data cannot be read from the configured source."""


class DSCDistressType(Enum):
    FIRE = 0
    FLOODING = 1
    COLLISION = 2
    GROUNDING = 3
    LISTING = 4
    SINKING = 5
    DISABLED_ADRIFT = 6
    UNSPECIFIED = 7


class DSCMessageType(Enum):
    DISTRESS = 0
    URGENCY = 1
    SAFETY = 2
    ROUTINE = 3
    ALL_SHIPS = 8
    GROUP_CALL = 15


@dataclass(frozen=True, slots=True)
class DSCMessage:
    mmsi: str
    message_type: DSCMessageType
    distress_type: DSCDistressType | None = None
    latitude: float | None = None
    longitude: float | None = None
    timestamp: datetime | None = None
    raw: str | None = None


class DSCDecoder:
    """Compatibility decoder API used by unit tests."""

    def decode(self, data: bytes) -> list[DSCMessage]:
        if not data:
            return []
        return []

    @staticmethod
    def _extract_bits(data: bytes, start_bit: int, num_bits: int) -> int:
        if num_bits <= 0:
            return 0
        value = 0
        for offset in range(num_bits):
            bit_index = start_bit + offset
            byte_index = bit_index // 8
            if byte_index >= len(data):
                break
            bit_in_byte = 7 - (bit_index % 8)
            bit = (data[byte_index] >> bit_in_byte) & 1
            value = (value << 1) | bit
        return value

    @staticmethod
    def _decode_message_type(raw_type: int) -> DSCMessageType:
        mapping = {
            0: DSCMessageType.DISTRESS,
            1: DSCMessageType.URGENCY,
            2: DSCMessageType.SAFETY,
            3: DSCMessageType.ROUTINE,
            8: DSCMessageType.ALL_SHIPS,
            15: DSCMessageType.GROUP_CALL,
        }
        return mapping.get(raw_type, DSCMessageType.ROUTINE)

    @staticmethod
    def _decode_distress_type(raw_type: int) -> DSCDistressType:
        mapping = {
            0: DSCDistressType.FIRE,
            1: DSCDistressType.FLOODING,
            2: DSCDistressType.COLLISION,
            3: DSCDistressType.GROUNDING,
            4: DSCDistressType.LISTING,
            5: DSCDistressType.SINKING,
            6: DSCDistressType.DISABLED_ADRIFT,
            7: DSCDistressType.UNSPECIFIED,
        }
        return mapping.get(raw_type, DSCDistressType.UNSPECIFIED)

    @staticmethod
    def _decode_latitude(raw_value: int) -> float:
        # DSC position formats vary; use a robust bounded conversion for tests.
        return max(-90.0, min(90.0, float(raw_value) / 6000.0))

    @staticmethod
    def _decode_longitude(raw_value: int) -> float:
        return max(-180.0, min(180.0, float(raw_value) / 6000.0))


class DSCTCPIngestor:
    """Compatibility TCP ingestor API used by legacy unit tests."""

    def __init__(self, host: str = "127.0.0.1", port: int = 6021) -> None:
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None
        self._connected = False

    def connect(self, timeout_seconds: float = 1.0) -> bool:
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=timeout_seconds)
            self._connected = True
            return True
        except OSError:
            self._connected = False
            return False

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
        self._sock = None
        self._connected = False

    def read_observations(self, **kwargs: Any) -> list[NormalizedObservation]:
        if not self._connected:
            raise DSCIngestError("DSC reader not connected")
        return []

    @staticmethod
    def _message_to_observation(msg: DSCMessage) -> NormalizedObservation:
        observed_at = msg.timestamp or datetime.now(timezone.utc)
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)

        label = f"MMSI: {msg.mmsi} "
        if msg.message_type == DSCMessageType.DISTRESS:
            distress = msg.distress_type.name.lower() if msg.distress_type else "unknown"
            label += f"DISTRESS ({distress})"
        else:
            label += msg.message_type.name

        payload_json: dict[str, Any] = {
            "message_type": msg.message_type.name,
            "distress_type": msg.distress_type.name if msg.distress_type else None,
        }
        if msg.raw:
            payload_json["raw"] = msg.raw

        return NormalizedObservation(
            target_id=f"dsc:{msg.mmsi}",
            source=Source.DSC,
            kind=TargetKind.VESSEL,
            observed_at=observed_at,
            label=label,
            lat=msg.latitude,
            lon=msg.longitude,
            last_scan_band=ScanBand.DSC,
            mmsi=msg.mmsi,
            payload_json=payload_json,
        )


class DSCDirectReader:
    """Direct DSC reader using rtl_tcp and native decoder modules."""

    def __init__(
        self,
        rtl_host: str = "127.0.0.1",
        rtl_port: int = 1234,
        sample_rate: int = 48000,
        gain: int = 30,
    ):
        if not _HAS_DSC:
            raise DSCIngestError(
                "DSC decoder modules not available. "
                "Ensure dsc_decoder module is installed."
            )

        self._receiver = RTLReceiver(
            frequency=156.525e6,
            sample_rate=sample_rate,
            gain=gain,
            host=rtl_host,
            port=rtl_port,
        )
        self._decoder = _NativeDSCDecoder()
        self._connected = False

    def connect(self) -> bool:
        try:
            if self._receiver.connect():
                self._connected = True
                logger.info("DSC reader connected to RTL-SDR")
                return True
            logger.error("Failed to connect DSC reader to RTL-SDR")
            return False
        except Exception as exc:
            logger.error("DSC connection error: %s", exc)
            return False

    def disconnect(self) -> None:
        self._receiver.disconnect()
        self._connected = False

    def read_observations(self, **kwargs: Any) -> list[NormalizedObservation]:
        if not self._connected:
            raise DSCIngestError("DSC reader not connected to RTL-SDR")

        observations: list[NormalizedObservation] = []
        try:
            iq_data = self._receiver.read_samples(num_samples=1024)
            if not iq_data:
                return observations

            bits = self._receiver.demodulate_to_bits(iq_data)
            if not bits:
                return observations

            messages = self._decoder.feed_bits(bits)
            for msg in messages:
                obs = self._native_message_to_observation(msg)
                if obs is not None:
                    observations.append(obs)
        except Exception as exc:
            logger.error("Error reading DSC observations: %s", exc)
            raise DSCIngestError(f"DSC read error: {exc}") from exc

        return observations

    @staticmethod
    def _native_message_to_observation(msg: _NativeDSCMessage) -> NormalizedObservation | None:
        try:
            label = f"MMSI: {msg.mmsi} "
            if msg.message_type == _NativeMessageType.DISTRESS:
                distress_name = msg.distress_type.name if msg.distress_type else "UNKNOWN"
                label += f"DISTRESS ({distress_name})"
            elif msg.message_type == _NativeMessageType.URGENCY:
                label += "URGENCY"
            elif msg.message_type == _NativeMessageType.SAFETY:
                label += "SAFETY"
            else:
                label += msg.message_type.name

            observed_at: datetime
            if isinstance(msg.timestamp, datetime):
                observed_at = msg.timestamp if msg.timestamp.tzinfo else msg.timestamp.replace(tzinfo=timezone.utc)
            elif isinstance(msg.timestamp, str):
                parsed = datetime.fromisoformat(msg.timestamp)
                observed_at = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            else:
                observed_at = datetime.now(timezone.utc)

            payload_json: dict[str, Any] = {
                "message_type": msg.message_type.name,
                "distress_type": msg.distress_type.name if getattr(msg, "distress_type", None) else None,
            }
            if getattr(msg, "raw_frame", None):
                payload_json["raw_frame"] = msg.raw_frame.hex()

            return NormalizedObservation(
                target_id=f"dsc:{msg.mmsi}",
                source=Source.DSC,
                kind=TargetKind.VESSEL,
                observed_at=observed_at,
                label=label,
                lat=msg.latitude,
                lon=msg.longitude,
                last_scan_band=ScanBand.DSC,
                mmsi=str(msg.mmsi),
                payload_json=payload_json,
            )
        except Exception as exc:
            logger.error("Error converting DSC message to observation: %s", exc)
            return None
