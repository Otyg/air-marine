"""Tests for DSC ingest adapter."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.ingest_dsc import (
    DSCDecoder,
    DSCDistressType,
    DSCIngestError,
    DSCMessage,
    DSCMessageType,
    DSCTCPIngestor,
)
from app.models import Source, TargetKind


class TestDSCDecoder:
    """Tests for DSCDecoder class."""

    def test_decoder_initialization(self) -> None:
        """Test decoder can be initialized."""
        decoder = DSCDecoder()
        assert decoder is not None

    def test_decode_empty_data(self) -> None:
        """Test decoding empty data returns empty list."""
        decoder = DSCDecoder()
        messages = decoder.decode(b"")
        assert messages == []

    def test_extract_bits(self) -> None:
        """Test bit extraction utility."""
        # Create test data: 11111111 00000000 10101010 = 0xFF00AA
        data = bytes([0xFF, 0x00, 0xAA])

        # Extract first 8 bits (all 1s) = 255
        value = DSCDecoder._extract_bits(data, 0, 8)
        assert value == 0xFF

        # Extract second 8 bits (all 0s) = 0
        value = DSCDecoder._extract_bits(data, 8, 8)
        assert value == 0x00

        # Extract third 8 bits (10101010) = 170
        value = DSCDecoder._extract_bits(data, 16, 8)
        assert value == 0xAA

    def test_decode_message_type(self) -> None:
        """Test message type decoding."""
        assert DSCDecoder._decode_message_type(0) == DSCMessageType.DISTRESS
        assert DSCDecoder._decode_message_type(1) == DSCMessageType.URGENCY
        assert DSCDecoder._decode_message_type(2) == DSCMessageType.SAFETY
        assert DSCDecoder._decode_message_type(3) == DSCMessageType.ROUTINE
        assert DSCDecoder._decode_message_type(8) == DSCMessageType.ALL_SHIPS
        assert DSCDecoder._decode_message_type(15) == DSCMessageType.GROUP_CALL
        assert DSCDecoder._decode_message_type(7) == DSCMessageType.ROUTINE  # Unknown defaults to ROUTINE

    def test_decode_distress_type(self) -> None:
        """Test distress type decoding."""
        assert DSCDecoder._decode_distress_type(0) == DSCDistressType.FIRE
        assert DSCDecoder._decode_distress_type(1) == DSCDistressType.FLOODING
        assert DSCDecoder._decode_distress_type(2) == DSCDistressType.COLLISION
        assert DSCDecoder._decode_distress_type(3) == DSCDistressType.GROUNDING
        assert DSCDecoder._decode_distress_type(4) == DSCDistressType.LISTING
        assert DSCDecoder._decode_distress_type(5) == DSCDistressType.SINKING
        assert DSCDecoder._decode_distress_type(6) == DSCDistressType.DISABLED_ADRIFT
        assert DSCDecoder._decode_distress_type(7) == DSCDistressType.UNSPECIFIED

    def test_decode_latitude(self) -> None:
        """Test latitude decoding."""
        # Test 0 degrees
        lat = DSCDecoder._decode_latitude(0)
        assert abs(lat) < 0.01

        # Test positive value
        lat = DSCDecoder._decode_latitude(180000)  # ~30 degrees
        assert lat > 0

        # Test latitude is clamped to valid range
        lat = DSCDecoder._decode_latitude(1000000)
        assert -90 <= lat <= 90

    def test_decode_longitude(self) -> None:
        """Test longitude decoding."""
        # Test 0 degrees
        lon = DSCDecoder._decode_longitude(0)
        assert abs(lon) < 0.01

        # Test positive value
        lon = DSCDecoder._decode_longitude(360000)  # ~60 degrees
        assert lon > 0

        # Test longitude is clamped to valid range
        lon = DSCDecoder._decode_longitude(2000000)
        assert -180 <= lon <= 180


class TestDSCMessage:
    """Tests for DSCMessage dataclass."""

    def test_create_message(self) -> None:
        """Test creating a DSC message."""
        msg = DSCMessage(
            mmsi="123456789",
            message_type=DSCMessageType.DISTRESS,
            distress_type=DSCDistressType.SINKING,
            latitude=57.5,
            longitude=11.5,
            timestamp=datetime.now(timezone.utc),
        )

        assert msg.mmsi == "123456789"
        assert msg.message_type == DSCMessageType.DISTRESS
        assert msg.distress_type == DSCDistressType.SINKING
        assert msg.latitude == 57.5
        assert msg.longitude == 11.5

    def test_message_is_frozen(self) -> None:
        """Test that DSCMessage is immutable."""
        msg = DSCMessage(
            mmsi="123456789",
            message_type=DSCMessageType.ROUTINE,
        )

        with pytest.raises(Exception):
            msg.mmsi = "987654321"


class TestDSCTCPIngestor:
    """Tests for DSCTCPIngestor class."""

    def test_ingestor_initialization(self) -> None:
        """Test ingestor can be initialized."""
        ingestor = DSCTCPIngestor(host="127.0.0.1", port=6021)
        assert ingestor is not None
        ingestor.close()

    def test_message_to_observation(self) -> None:
        """Test converting DSC message to observation."""
        ingestor = DSCTCPIngestor(host="127.0.0.1", port=6021)

        msg = DSCMessage(
            mmsi="123456789",
            message_type=DSCMessageType.DISTRESS,
            distress_type=DSCDistressType.SINKING,
            latitude=57.5,
            longitude=11.5,
            timestamp=datetime.now(timezone.utc),
            raw="7e1234567e",
        )

        obs = ingestor._message_to_observation(msg)

        assert obs is not None
        assert obs.source == Source.DSC
        assert "123456789" in obs.target_id
        assert obs.target_kind == TargetKind.VESSEL
        assert obs.latitude == 57.5
        assert obs.longitude == 11.5
        assert obs.label == "MMSI: 123456789 DISTRESS (sinking)"

        ingestor.close()

    def test_message_to_observation_without_coordinates(self) -> None:
        """Test converting DSC message without coordinates."""
        ingestor = DSCTCPIngestor(host="127.0.0.1", port=6021)

        msg = DSCMessage(
            mmsi="987654321",
            message_type=DSCMessageType.URGENCY,
        )

        obs = ingestor._message_to_observation(msg)

        assert obs is not None
        assert obs.source == Source.DSC
        assert obs.latitude is None
        assert obs.longitude is None
        assert obs.target_kind == TargetKind.VESSEL
        assert "987654321" in obs.target_id
        assert "URGENCY" in obs.label

        ingestor.close()

    def test_read_observations_not_connected(self) -> None:
        """Test that reading without connection raises error."""
        ingestor = DSCTCPIngestor(host="localhost", port=9999)

        with pytest.raises(DSCIngestError):
            ingestor.read_observations()

        ingestor.close()
