#!/usr/bin/env python3
"""
DSC (Digital Selective Calling) Decoder

Pure Python DSC frame decoder for maritime VHF Channel 70 (156.525 MHz).
Decodes 8-bit, 1200 baud GFSK modulated signals.

This module provides frame-level decoding only. It's designed to be used
as a library by decoders that handle RTL-SDR I/Q conversion.
"""

import struct
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, List

logger = logging.getLogger(__name__)


class MessageType(Enum):
    """DSC message types"""
    DISTRESS = 0
    URGENCY = 1
    SAFETY = 2
    ROUTINE = 3
    ALL_SHIPS = 4
    GROUP_CALL = 5


class DistressType(Enum):
    """DSC distress types"""
    FIRE = 1
    FLOODING = 2
    COLLISION = 3
    GROUNDING = 4
    LISTING = 5
    SINKING = 6
    DISABLED_ADRIFT = 7
    UNSPECIFIED = 15


@dataclass
class DSCMessage:
    """Decoded DSC message"""
    mmsi: int
    message_type: MessageType
    distress_type: Optional[DistressType] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    timestamp: Optional[str] = None
    raw_frame: Optional[bytes] = None

    def to_dict(self):
        """Convert to dictionary"""
        return {
            "mmsi": self.mmsi,
            "message_type": self.message_type.name,
            "distress_type": self.distress_type.name if self.distress_type else None,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timestamp": self.timestamp,
            "raw_frame": self.raw_frame.hex() if self.raw_frame else None,
        }


class DSCDecoder:
    """
    DSC frame decoder.
    
    Decodes binary DSC frames with:
    - Preamble: 32-bit sync pattern (0xAAAAAAAA or similar)
    - Frame Flag: 0x7E byte markers
    - Message: 20-21 bytes with MMSI, type, position, etc.
    """

    # DSC frame constants
    FRAME_FLAG = 0x7E
    PREAMBLE = 0xAAAAAAAA
    MIN_FRAME_LENGTH = 20  # Minimum DSC frame bytes
    MAX_FRAME_LENGTH = 25  # Maximum DSC frame bytes

    def __init__(self):
        """Initialize decoder"""
        self.bit_buffer = bytearray()
        self.frame_buffer = bytearray()
        self.synced = False
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    def feed_bits(self, bits: bytes) -> List[DSCMessage]:
        """
        Feed raw bits to decoder and extract complete frames.
        
        Args:
            bits: Raw bit data (can be nibbles, bytes, or bit array)
            
        Returns:
            List of decoded DSC messages
        """
        messages = []
        self.bit_buffer.extend(bits)

        # Try to find and decode frames
        while len(self.bit_buffer) >= 160:  # Minimum bits for a frame (20 bytes * 8)
            frame = self._extract_frame()
            if frame:
                msg = self._decode_frame(frame)
                if msg:
                    messages.append(msg)
            else:
                # No complete frame found, break
                break

        return messages

    def _extract_frame(self) -> Optional[bytes]:
        """
        Extract a complete frame from bit buffer.
        
        Looks for frame flags (0x7E) and extracts frame between them.
        """
        if len(self.bit_buffer) < 8:
            return None

        # Search for frame flags
        start_idx = -1
        for i in range(len(self.bit_buffer) - 7):
            byte_val = self._bits_to_byte(self.bit_buffer[i : i + 8])
            if byte_val == self.FRAME_FLAG:
                start_idx = i
                break

        if start_idx == -1:
            return None

        # Look for end flag
        search_start = start_idx + 8
        end_idx = -1
        for i in range(search_start, len(self.bit_buffer) - 7, 8):
            byte_val = self._bits_to_byte(self.bit_buffer[i : i + 8])
            if byte_val == self.FRAME_FLAG:
                end_idx = i
                break

        if end_idx == -1 or (end_idx - start_idx) // 8 < self.MIN_FRAME_LENGTH:
            # Not enough data yet, remove first byte and try again
            del self.bit_buffer[: start_idx + 8]
            return None

        # Extract frame bytes between flags
        frame_bits = self.bit_buffer[start_idx + 8 : end_idx]
        frame_bytes = bytearray()

        for i in range(0, len(frame_bits) - 7, 8):
            byte_val = self._bits_to_byte(frame_bits[i : i + 8])
            frame_bytes.append(byte_val)

        # Remove processed bits
        del self.bit_buffer[: end_idx + 8]

        if self.MIN_FRAME_LENGTH <= len(frame_bytes) <= self.MAX_FRAME_LENGTH:
            return bytes(frame_bytes)

        return None

    def _decode_frame(self, frame: bytes) -> Optional[DSCMessage]:
        """
        Decode a complete DSC frame.
        
        Frame structure (example):
        - Byte 0-2: FEC check (3 bytes)
        - Byte 3-6: MMSI (4 bytes, 30 bits used)
        - Byte 7: Format specifier and message type (4 bits)
        - Byte 8: Position request flag and distress type (4 bits each)
        - Byte 9-10: Latitude (optional)
        - Byte 11-12: Longitude (optional)
        """
        try:
            if len(frame) < 7:
                return None

            # Extract MMSI (30 bits from bytes 3-6)
            mmsi_bits = (frame[3] & 0x3F) << 24
            mmsi_bits |= frame[4] << 16
            mmsi_bits |= frame[5] << 8
            mmsi_bits |= frame[6]
            mmsi = mmsi_bits >> 0

            # Validate MMSI (should be reasonable maritime number)
            if mmsi == 0 or mmsi > 999999999:
                return None

            # Extract message type (4 bits)
            msg_type_val = (frame[7] >> 4) & 0x0F
            try:
                message_type = MessageType(msg_type_val)
            except ValueError:
                self.logger.debug(f"Unknown message type: {msg_type_val}")
                return None

            # Extract distress type if applicable
            distress_type = None
            if message_type == MessageType.DISTRESS and len(frame) >= 9:
                distress_val = (frame[8] >> 4) & 0x0F
                try:
                    distress_type = DistressType(distress_val)
                except ValueError:
                    pass

            # Extract position if present
            latitude = None
            longitude = None
            if len(frame) >= 13:
                lat_bits = (frame[9] << 19) | (frame[10] << 11) | (frame[11] << 3)
                lon_bits = (frame[11] << 20) | (frame[12] << 12) | (frame[13] << 4)

                # Convert to degrees (27 bits for lat, 28 bits for lon)
                latitude = ((lat_bits >> 0) - (1 << 26)) / (1 << 21) * 180 / 90
                longitude = (
                    (lon_bits >> 0) - (1 << 27)
                ) / (1 << 22) * 360 / 180

                # Validate coordinates
                if abs(latitude) > 90:
                    latitude = None
                if abs(longitude) > 180:
                    longitude = None

            msg = DSCMessage(
                mmsi=mmsi,
                message_type=message_type,
                distress_type=distress_type,
                latitude=latitude,
                longitude=longitude,
                raw_frame=frame,
            )

            self.logger.info(
                f"Decoded DSC: MMSI={mmsi} Type={message_type.name} "
                f"Distress={distress_type.name if distress_type else 'N/A'}"
            )

            return msg

        except Exception as e:
            self.logger.error(f"Error decoding DSC frame: {e}")
            return None

    @staticmethod
    def _bits_to_byte(bits: bytes) -> int:
        """Convert 8 bits to a byte"""
        if len(bits) < 8:
            return 0
        return sum(bit << (7 - i) for i, bit in enumerate(bits[:8]))

    @staticmethod
    def _bytes_to_bits(data: bytes) -> bytes:
        """Convert bytes to individual bits"""
        bits = bytearray()
        for byte in data:
            for i in range(8):
                bits.append((byte >> (7 - i)) & 1)
        return bytes(bits)


class GFSK_Demodulator:
    """
    GFSK (Gaussian Frequency Shift Keying) demodulator for DSC signals.
    
    DSC uses 1200 baud GFSK at 156.525 MHz with:
    - Frequency deviation: ±600 Hz (mark/space frequencies)
    - Baud rate: 1200 symbols/sec
    - Mark frequency: +600 Hz
    - Space frequency: -600 Hz
    """

    def __init__(self, sample_rate: int = 48000):
        """
        Initialize GFSK demodulator.
        
        Args:
            sample_rate: Sample rate of input I/Q data (Hz)
        """
        self.sample_rate = sample_rate
        self.samples_per_symbol = sample_rate // 1200
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    def demodulate(self, iq_data: bytes) -> bytes:
        """
        Demodulate I/Q data to binary bits.
        
        Args:
            iq_data: I/Q samples as little-endian signed shorts (i16, q16 pairs)
            
        Returns:
            Bits as bytes (0x00 or 0x01)
        """
        # Parse I/Q data
        samples = struct.unpack(f"<{len(iq_data)//4}h", iq_data)
        iq_pairs = [
            (samples[i], samples[i + 1])
            for i in range(0, len(samples) - 1, 2)
        ]

        bits = bytearray()

        for i in range(0, len(iq_pairs) - self.samples_per_symbol, self.samples_per_symbol):
            symbol_iq = iq_pairs[i : i + self.samples_per_symbol]

            # Calculate frequency via phase change
            phase_accum = 0.0
            for i_val, q_val in symbol_iq:
                magnitude = math.sqrt(i_val * i_val + q_val * q_val)
                if magnitude > 0:
                    phase = math.atan2(q_val, i_val)
                    phase_accum += phase

            avg_phase = phase_accum / len(symbol_iq)

            # Determine bit based on phase (simple slicer)
            bit = 1 if avg_phase > 0 else 0
            bits.append(bit)

        return bytes(bits)


class DSCReceiver:
    """
    Main DSC receiver that combines RTL-SDR tuning, demodulation, and decoding.
    """

    def __init__(
        self,
        sample_rate: int = 48000,
        frequency: float = 156.525e6,
        gain: int = 30,
    ):
        """
        Initialize DSC receiver.
        
        Args:
            sample_rate: RTL-SDR sample rate (Hz)
            frequency: Tuning frequency (Hz), default 156.525 MHz (VHF Channel 70)
            gain: RTL-SDR gain (0-50 typically)
        """
        self.sample_rate = sample_rate
        self.frequency = frequency
        self.gain = gain
        self.demodulator = GFSK_Demodulator(sample_rate)
        self.decoder = DSCDecoder()
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    def process_iq_data(self, iq_data: bytes) -> List[DSCMessage]:
        """
        Process raw I/Q data from RTL-SDR.
        
        Args:
            iq_data: Raw I/Q samples (little-endian int16 pairs)
            
        Returns:
            List of decoded DSC messages
        """
        bits = self.demodulator.demodulate(iq_data)
        messages = self.decoder.feed_bits(bits)
        return messages
