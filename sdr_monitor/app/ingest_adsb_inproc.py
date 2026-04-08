"""In-process ADS-B ingest from raw rtl_tcp I/Q samples.

This is a first vertical slice that avoids readsb for ADS-B by decoding
Mode S frames directly from rtl_tcp magnitude samples.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import math
import socket
import struct
from typing import Any

from app.models import NormalizedObservation, ScanBand, Source, TargetKind

logger = logging.getLogger(__name__)

MODE_S_FREQUENCY_HZ = 1_090_000_000
MODE_S_SAMPLE_RATE_HZ = 2_000_000
MODE_S_POLY = 0xFFF409


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class ADSBInprocError(RuntimeError):
    """Raised when direct ADS-B ingest fails."""


@dataclass(slots=True)
class RTLTCPClient:
    host: str
    port: int
    sample_rate: int
    gain: int
    frequency: int

    _sock: socket.socket | None = field(default=None, init=False, repr=False)
    _connected: bool = field(default=False, init=False, repr=False)

    def connect(self) -> bool:
        if self._connected and self._sock is not None:
            return True
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=1.0)
            self._set_frequency(self.frequency)
            self._set_sample_rate(self.sample_rate)
            self._set_gain(self.gain)
            self._connected = True
            return True
        except Exception as exc:
            logger.debug("ADSB rtl_tcp connect failed: %s", exc)
            self.close()
            return False

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
        self._sock = None
        self._connected = False

    def read_samples(self, num_samples: int) -> bytes | None:
        if not self._connected or self._sock is None:
            return None
        try:
            payload = self._sock.recv(num_samples * 2)
            if not payload:
                self._connected = False
                return None
            return payload
        except Exception as exc:
            logger.debug("ADSB rtl_tcp read failed: %s", exc)
            self._connected = False
            return None

    def _send_cmd(self, cmd: int, value: int) -> None:
        if self._sock is None:
            raise ADSBInprocError("rtl_tcp socket not connected")
        self._sock.sendall(struct.pack("<BI", cmd, int(value)))

    def _set_frequency(self, hz: int) -> None:
        self._send_cmd(0x01, hz)

    def _set_sample_rate(self, hz: int) -> None:
        self._send_cmd(0x02, hz)

    def _set_gain(self, db: int) -> None:
        self._send_cmd(0x04, db)


class ADSBModeSDecoder:
    """Minimal Mode S detector/decoder for 2MHz rtl_tcp I/Q streams."""

    def __init__(self, sample_rate: int = MODE_S_SAMPLE_RATE_HZ) -> None:
        self.sample_rate = sample_rate
        if sample_rate != 2_000_000:
            logger.warning("ADSB decoder tuned for 2MHz sample rate; got %s", sample_rate)
        self._mag_buffer: list[float] = []

    def feed_iq(self, iq_data: bytes) -> list[str]:
        mags = self._iq_to_magnitude(iq_data)
        if not mags:
            return []

        self._mag_buffer.extend(mags)
        if len(self._mag_buffer) < 256:
            return []

        messages: list[str] = []
        i = 0
        max_index = len(self._mag_buffer) - 240
        while i < max_index:
            if not self._looks_like_preamble(self._mag_buffer, i):
                i += 1
                continue

            bits112 = self._decode_bits(self._mag_buffer, i + 16, 112)
            if bits112 is None:
                i += 1
                continue

            bit_len = self._resolve_message_bits(bits112)
            bits = bits112[:bit_len]
            msg_hex = self._bits_to_hex(bits)
            if msg_hex and self._crc_ok(bits):
                messages.append(msg_hex)
                i += 16 + bit_len * 2
            else:
                i += 1

        # Keep trailing samples for next chunk.
        self._mag_buffer = self._mag_buffer[max(0, len(self._mag_buffer) - 512) :]
        return messages

    @staticmethod
    def _iq_to_magnitude(iq_data: bytes) -> list[float]:
        mags: list[float] = []
        for idx in range(0, len(iq_data) - 1, 2):
            i_val = iq_data[idx] - 127.5
            q_val = iq_data[idx + 1] - 127.5
            mags.append(math.hypot(i_val, q_val))
        return mags

    @staticmethod
    def _looks_like_preamble(mags: list[float], i: int) -> bool:
        # 2 MHz preamble samples (8us => 16 samples), rough pattern:
        # high around [0,2,7,9], low around [1,3,4,5,6,8].
        if i + 16 >= len(mags):
            return False
        p0 = mags[i]
        p2 = mags[i + 2]
        p7 = mags[i + 7]
        p9 = mags[i + 9]
        baseline = (mags[i + 1] + mags[i + 3] + mags[i + 4] + mags[i + 5] + mags[i + 6] + mags[i + 8]) / 6.0
        threshold = baseline * 1.8 if baseline > 0 else 12.0
        return p0 > threshold and p2 > threshold and p7 > threshold and p9 > threshold

    @staticmethod
    def _decode_bits(mags: list[float], start: int, nbits: int) -> list[int] | None:
        bits: list[int] = []
        for bit_idx in range(nbits):
            a = start + bit_idx * 2
            b = a + 1
            if b >= len(mags):
                return None
            m0 = mags[a]
            m1 = mags[b]
            if m0 == m1:
                return None
            bits.append(1 if m0 > m1 else 0)
        return bits

    @staticmethod
    def _resolve_message_bits(bits112: list[int]) -> int:
        # First 5 bits are DF.
        df = 0
        for b in bits112[:5]:
            df = (df << 1) | b
        if df in {0, 4, 5, 11}:
            return 56
        return 112

    @staticmethod
    def _bits_to_hex(bits: list[int]) -> str:
        if len(bits) % 8 != 0:
            return ""
        value = 0
        for b in bits:
            value = (value << 1) | b
        return f"{value:0{len(bits)//4}X}"

    @staticmethod
    def _crc_ok(bits: list[int]) -> bool:
        if len(bits) not in {56, 112}:
            return False
        msg = 0
        for b in bits:
            msg = (msg << 1) | b

        if len(bits) == 112:
            data = msg >> 24
            parity = msg & 0xFFFFFF
            crc = ADSBModeSDecoder._modes_crc(data, 88)
            return crc == parity

        data = msg >> 24
        parity = msg & 0xFFFFFF
        crc = ADSBModeSDecoder._modes_crc(data, 32)
        return crc == parity

    @staticmethod
    def _modes_crc(msg_no_parity: int, bit_len: int) -> int:
        reg = msg_no_parity << 24
        top_bit = 1 << (bit_len + 23)
        for _ in range(bit_len):
            if reg & top_bit:
                reg ^= MODE_S_POLY << (bit_len - 1)
            reg <<= 1
        return (reg >> bit_len) & 0xFFFFFF


def _decode_callsign_from_me(me: int) -> str | None:
    charset = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ#####_###############0123456789######"
    chars = []
    for shift in range(42, -1, -6):
        code = (me >> shift) & 0x3F
        if code >= len(charset):
            chars.append(" ")
        else:
            ch = charset[code]
            chars.append(" " if ch in {"#", "_"} else ch)
    callsign = "".join(chars).strip()
    return callsign or None


def parse_modes_message_to_observation(msg_hex: str, observed_at: datetime | None = None) -> NormalizedObservation | None:
    if len(msg_hex) != 28:
        return None
    try:
        msg = int(msg_hex, 16)
    except ValueError:
        return None

    df = (msg >> 107) & 0x1F
    if df not in {17, 18}:
        return None

    icao = (msg >> 80) & 0xFFFFFF
    icao_hex = f"{icao:06X}".lower()

    me = (msg >> 24) & ((1 << 56) - 1)
    type_code = (me >> 51) & 0x1F
    callsign = _decode_callsign_from_me(me) if 1 <= type_code <= 4 else None

    ts = observed_at or _now_utc()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    return NormalizedObservation(
        target_id=f"adsb:{icao_hex}",
        source=Source.ADSB,
        kind=TargetKind.AIRCRAFT,
        observed_at=ts,
        label=callsign or icao_hex.upper(),
        lat=None,
        lon=None,
        last_scan_band=ScanBand.ADSB,
        icao24=icao_hex,
        callsign=callsign,
        payload_json={"raw_hex": msg_hex, "df": df, "type_code": type_code},
    )


class ADSBInprocReader:
    """Direct ADS-B reader from rtl_tcp I/Q samples."""

    def __init__(
        self,
        *,
        rtl_host: str = "127.0.0.1",
        rtl_port: int = 1234,
        sample_rate: int = MODE_S_SAMPLE_RATE_HZ,
        gain: int = 30,
        frequency_hz: int = MODE_S_FREQUENCY_HZ,
    ) -> None:
        self._client = RTLTCPClient(
            host=rtl_host,
            port=rtl_port,
            sample_rate=sample_rate,
            gain=gain,
            frequency=frequency_hz,
        )
        self._decoder = ADSBModeSDecoder(sample_rate=sample_rate)

    def read_observations(
        self,
        *,
        timeout_seconds: float = 0.2,
        num_samples: int = 16_384,
    ) -> list[NormalizedObservation]:
        if not self._client.connect():
            return []

        iq_data = self._client.read_samples(num_samples=num_samples)
        if not iq_data:
            return []

        messages = self._decoder.feed_iq(iq_data)
        if not messages:
            return []

        observations: list[NormalizedObservation] = []
        seen_target_ids: set[str] = set()
        now = _now_utc()
        for msg_hex in messages:
            obs = parse_modes_message_to_observation(msg_hex, observed_at=now)
            if obs is None:
                continue
            if obs.target_id in seen_target_ids:
                continue
            seen_target_ids.add(obs.target_id)
            observations.append(obs)
        return observations

    def close(self) -> None:
        self._client.close()
