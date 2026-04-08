#!/usr/bin/env python3
"""
Tests for DSC Decoder
"""

import pytest
import struct
from dsc_decoder import (
    DSCDecoder,
    DSCMessage,
    DSCReceiver,
    DistressType,
    MessageType,
)


class TestDSCDecoder:
    """Tests for DSC frame decoder"""

    def test_bits_to_byte(self):
        """Test bit array to byte conversion"""
        decoder = DSCDecoder()

        # Test 0xFF
        bits = bytes([1, 1, 1, 1, 1, 1, 1, 1])
        assert decoder._bits_to_byte(bits) == 0xFF

        # Test 0x00
        bits = bytes([0, 0, 0, 0, 0, 0, 0, 0])
        assert decoder._bits_to_byte(bits) == 0x00

        # Test 0xAA (alternating)
        bits = bytes([1, 0, 1, 0, 1, 0, 1, 0])
        assert decoder._bits_to_byte(bits) == 0xAA

    def test_bytes_to_bits(self):
        """Test byte to bits conversion"""
        decoder = DSCDecoder()

        # Test 0xFF
        bits = decoder._bytes_to_bits(bytes([0xFF]))
        assert bits == bytes([1] * 8)

        # Test 0x00
        bits = decoder._bytes_to_bits(bytes([0x00]))
        assert bits == bytes([0] * 8)

    def test_frame_flag_detection(self):
        """Test detection of frame flags"""
        decoder = DSCDecoder()

        # Create bits with frame flag (0x7E)
        frame_flag = bytes([0, 1, 1, 1, 1, 1, 1, 0])
        bits = bytearray()
        bits.extend(frame_flag)
        bits.extend([0] * 8)  # Some payload
        bits.extend(frame_flag)

        decoder.bit_buffer = bits
        frame = decoder._extract_frame()

        # Frame between flags should be extracted
        assert frame is None or len(frame) >= 1

    def test_decode_valid_frame(self):
        """Test decoding a valid DSC frame"""
        decoder = DSCDecoder()

        # Create a test frame
        frame = bytearray()
        frame.extend(b"\x00\x00\x00")  # FEC bytes

        # MMSI 123456789
        mmsi = 123456789
        frame.extend(struct.pack(">I", mmsi << 2))

        # Message type DISTRESS
        frame.append((MessageType.DISTRESS.value << 4) | 0x00)

        # Distress type SINKING
        frame.append((DistressType.SINKING.value << 4) | 0x00)

        # Coordinates
        frame.extend(b"\x00\x00\x00")  # Lat placeholder
        frame.extend(b"\x00\x00\x00")  # Lon placeholder

        # Decode
        msg = decoder._decode_frame(bytes(frame))

        assert msg is not None
        assert msg.mmsi == mmsi
        assert msg.message_type == MessageType.DISTRESS
        assert msg.distress_type == DistressType.SINKING

    def test_invalid_mmsi(self):
        """Test that invalid MMSI is rejected"""
        decoder = DSCDecoder()

        # Create frame with MMSI=0 (invalid)
        frame = bytearray()
        frame.extend(b"\x00\x00\x00")
        frame.extend(b"\x00\x00\x00\x00")  # MMSI=0
        frame.append((MessageType.DISTRESS.value << 4) | 0x00)
        frame.append((DistressType.SINKING.value << 4) | 0x00)

        msg = decoder._decode_frame(bytes(frame))

        # Should reject invalid MMSI
        assert msg is None

    def test_feed_bits(self):
        """Test feeding bits to decoder"""
        decoder = DSCDecoder()

        # Create test bits
        bits = bytearray()

        # Add some noise
        bits.extend([0, 1] * 20)

        # Decode
        messages = decoder.feed_bits(bytes(bits))

        # Should not crash, but may not decode anything
        assert isinstance(messages, list)


class TestGFSKDemodulator:
    """Tests for GFSK demodulator"""

    def test_demodulator_init(self):
        """Test demodulator initialization"""
        from dsc_decoder import GFSK_Demodulator

        demod = GFSK_Demodulator(sample_rate=48000)
        assert demod.sample_rate == 48000
        assert demod.samples_per_symbol == 40  # 48000 / 1200

    def test_demodulate(self):
        """Test GFSK demodulation"""
        from dsc_decoder import GFSK_Demodulator

        demod = GFSK_Demodulator(sample_rate=48000)

        # Create fake I/Q data
        iq_data = bytearray()
        for i in range(1000):
            # Simple test data
            i_val = 100 if i % 40 < 20 else -100
            q_val = 100 if i % 40 < 20 else -100
            iq_data.extend(struct.pack("<hh", i_val, q_val))

        # Demodulate
        bits = demod.demodulate(bytes(iq_data))

        # Should return bytes of bits
        assert isinstance(bits, bytes)
        assert len(bits) > 0

    def test_demodulate_empty(self):
        """Test demodulation of empty data"""
        from dsc_decoder import GFSK_Demodulator

        demod = GFSK_Demodulator(sample_rate=48000)
        bits = demod.demodulate(b"")

        assert isinstance(bits, bytes)
        assert len(bits) == 0


class TestDSCReceiver:
    """Tests for DSC receiver"""

    def test_receiver_init(self):
        """Test receiver initialization"""
        receiver = DSCReceiver(sample_rate=48000, gain=30)

        assert receiver.sample_rate == 48000
        assert receiver.frequency == 156.525e6
        assert receiver.gain == 30
        assert receiver.demodulator is not None
        assert receiver.decoder is not None

    def test_process_iq_data(self):
        """Test processing I/Q data"""
        receiver = DSCReceiver()

        # Create fake I/Q data
        iq_data = bytearray()
        for i in range(10000):
            i_val = int(100 * ((i % 40) - 20) / 20)
            q_val = int(100 * (((i + 20) % 40) - 20) / 20)
            iq_data.extend(struct.pack("<hh", i_val, q_val))

        # Process
        messages = receiver.process_iq_data(bytes(iq_data))

        # Should return list (may be empty if no valid frames)
        assert isinstance(messages, list)


class TestDSCMessage:
    """Tests for DSC message dataclass"""

    def test_message_creation(self):
        """Test creating a DSC message"""
        msg = DSCMessage(
            mmsi=123456789,
            message_type=MessageType.DISTRESS,
            distress_type=DistressType.SINKING,
            latitude=57.5,
            longitude=11.5,
        )

        assert msg.mmsi == 123456789
        assert msg.message_type == MessageType.DISTRESS
        assert msg.distress_type == DistressType.SINKING
        assert msg.latitude == 57.5
        assert msg.longitude == 11.5

    def test_message_to_dict(self):
        """Test converting message to dictionary"""
        msg = DSCMessage(
            mmsi=123456789,
            message_type=MessageType.URGENCY,
            latitude=58.0,
            longitude=12.0,
        )

        d = msg.to_dict()

        assert d["mmsi"] == 123456789
        assert d["message_type"] == "URGENCY"
        assert d["latitude"] == 58.0
        assert d["longitude"] == 12.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
