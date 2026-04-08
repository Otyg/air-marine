#!/usr/bin/env python3
"""
DSC Decoder Test Script

Simple test that demonstrates DSC decoding with simulated data.
Tests the decoder without needing actual RTL-SDR hardware.
"""

import sys
import logging
from pathlib import Path
from datetime import datetime, timezone

# Add modules to path
dsc_decoder_path = Path(__file__).parent / "dsc_decoder"
sdr_monitor_path = Path(__file__).parent / "sdr_monitor"

sys.path.insert(0, str(dsc_decoder_path))
sys.path.insert(0, str(sdr_monitor_path))

from dsc_decoder import DSCDecoder, MessageType, DistressType, DSCMessage

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def create_test_dsc_frame(mmsi: int, message_type: int, distress_type: int = 0) -> bytes:
    """
    Create a test DSC frame (20 bytes minimum).
    
    Frame format:
    - Frame flag: 0x7E
    - MMSI: 30 bits
    - Message Type: 4 bits  
    - Distress Type: 4 bits
    - Padding: remaining bits
    - Frame flag: 0x7E
    
    Args:
        mmsi: Maritime Mobile Service Identity (9 digits)
        message_type: Message type (0=distress, 1=urgency, etc)
        distress_type: Distress type (1=fire, 2=flooding, etc)
    
    Returns:
        Bytes representing DSC frame
    """
    frame = bytearray()
    
    # Frame flag (start)
    frame.append(0x7E)
    
    # Build the message data as bits, then convert to bytes
    # MMSI (30 bits) + Message Type (4 bits) + Distress Type (4 bits) = 38 bits minimum
    # Pad to at least 160 bits (20 bytes) for decoder
    
    bits = []
    
    # Add MMSI (30 bits, big-endian)
    mmsi_val = mmsi & 0x3FFFFFFF
    for i in range(29, -1, -1):
        bits.append((mmsi_val >> i) & 1)
    
    # Add Message Type (4 bits)
    msg_type_val = message_type & 0x0F
    for i in range(3, -1, -1):
        bits.append((msg_type_val >> i) & 1)
    
    # Add Distress Type (4 bits)
    distress_val = distress_type & 0x0F
    for i in range(3, -1, -1):
        bits.append((distress_val >> i) & 1)
    
    # Pad with zeros to reach at least 160 bits (20 bytes)
    while len(bits) < 160:
        bits.append(0)
    
    # Convert bits to bytes
    for i in range(0, len(bits), 8):
        byte = 0
        for j in range(8):
            if i + j < len(bits):
                byte = (byte << 1) | bits[i + j]
            else:
                byte = byte << 1
        frame.append(byte)
    
    # Frame flag (end)
    frame.append(0x7E)
    
    return bytes(frame)


def test_decoder():
    """Test the DSC decoder with simulated frames."""
    logger.info("=" * 60)
    logger.info("DSC DECODER TEST")
    logger.info("=" * 60)
    
    decoder = DSCDecoder()
    
    # Test 1: Distress message (sinking)
    logger.info("\n[Test 1] Decoding DISTRESS (SINKING) message")
    mmsi_1 = 123456789
    frame_bytes_1 = create_test_dsc_frame(
        mmsi=mmsi_1,
        message_type=0,  # DISTRESS
        distress_type=6  # SINKING
    )
    
    # Convert bytes to bits for the decoder
    frame_bits_1 = bytearray()
    for byte in frame_bytes_1:
        for i in range(7, -1, -1):
            frame_bits_1.append((byte >> i) & 1)
    
    logger.info(f"  Frame bytes: {frame_bytes_1.hex()}")
    logger.info(f"  Frame bits length: {len(frame_bits_1)}")
    
    messages_1 = decoder.feed_bits(bytes(frame_bits_1))
    if messages_1:
        msg = messages_1[0]
        logger.info(f"  ✓ Decoded: MMSI={msg.mmsi}, Type={msg.message_type.name}")
        logger.info(f"           Distress={msg.distress_type.name if msg.distress_type else 'N/A'}")
        logger.info(f"           Position: ({msg.latitude}, {msg.longitude})")
        logger.info(f"           Raw: {msg.raw_frame.hex() if msg.raw_frame else 'N/A'}")
    else:
        logger.warning("  ✗ No messages decoded")
    
    # Test 2: Urgency message
    logger.info("\n[Test 2] Decoding URGENCY message")
    decoder_2 = DSCDecoder()
    mmsi_2 = 987654321
    frame_bytes_2 = create_test_dsc_frame(
        mmsi=mmsi_2,
        message_type=1,  # URGENCY
        distress_type=0
    )
    
    # Convert bytes to bits
    frame_bits_2 = bytearray()
    for byte in frame_bytes_2:
        for i in range(7, -1, -1):
            frame_bits_2.append((byte >> i) & 1)
    
    logger.info(f"  Frame bytes: {frame_bytes_2.hex()}")
    
    messages_2 = decoder_2.feed_bits(bytes(frame_bits_2))
    if messages_2:
        msg = messages_2[0]
        logger.info(f"  ✓ Decoded: MMSI={msg.mmsi}, Type={msg.message_type.name}")
        logger.info(f"           Raw: {msg.raw_frame.hex() if msg.raw_frame else 'N/A'}")
    else:
        logger.warning("  ✗ No messages decoded")
    
    # Test 3: Multiple frames in sequence
    logger.info("\n[Test 3] Decoding MULTIPLE frames in sequence")
    decoder_3 = DSCDecoder()
    
    combined_bits = bytearray()
    for i in range(3):
        mmsi = 111111111 + i
        frame_bytes = create_test_dsc_frame(mmsi=mmsi, message_type=2)  # SAFETY
        
        # Convert to bits
        for byte in frame_bytes:
            for bit_idx in range(7, -1, -1):
                combined_bits.append((byte >> bit_idx) & 1)
        
        logger.info(f"  Added frame {i+1}: MMSI={mmsi}")
    
    logger.info(f"  Combined bit length: {len(combined_bits)}")
    messages_3 = decoder_3.feed_bits(bytes(combined_bits))
    logger.info(f"  ✓ Decoded {len(messages_3)} messages")
    for i, msg in enumerate(messages_3):
        logger.info(f"    - Frame {i+1}: MMSI={msg.mmsi}, Type={msg.message_type.name}")
    
    logger.info("\n" + "=" * 60)
    logger.info("TEST COMPLETE")
    logger.info("=" * 60)


def test_rtl_receiver():
    """Test RTL receiver (without actual hardware)."""
    logger.info("\n" + "=" * 60)
    logger.info("RTL RECEIVER TEST")
    logger.info("=" * 60)
    
    try:
        from rtl_receiver import RTLReceiver
        logger.info("\n[Test] RTLReceiver initialization")
        
        receiver = RTLReceiver(
            frequency=156.525e6,
            sample_rate=48000,
            gain=30,
            host="127.0.0.1",
            port=1234,
        )
        logger.info(f"  ✓ RTLReceiver created")
        logger.info(f"    - Frequency: {receiver.frequency / 1e6} MHz")
        logger.info(f"    - Sample rate: {receiver.sample_rate} Hz")
        logger.info(f"    - Gain: {receiver.gain} dB")
        logger.info(f"    - Host: {receiver.host}:{receiver.port}")
        
        logger.info("\n  Note: Not connecting (no RTL-SDR hardware available)")
        logger.info("  In production, RTLReceiver would connect to rtl_tcp")
        
    except Exception as e:
        logger.error(f"  ✗ Error testing RTLReceiver: {e}", exc_info=True)
    
    logger.info("\n" + "=" * 60)


def test_ingest_reader():
    """Test DSC ingest reader."""
    logger.info("\n" + "=" * 60)
    logger.info("DSC INGEST READER TEST")
    logger.info("=" * 60)
    
    try:
        from app.ingest_dsc import DSCDirectReader, DSCIngestError
        from app.models import NormalizedObservation, Source, TargetKind
        
        logger.info("\n[Test] DSCDirectReader initialization")
        
        # This will fail without RTL-SDR, but shows the interface
        try:
            reader = DSCDirectReader(
                rtl_host="127.0.0.1",
                rtl_port=1234,
                sample_rate=48000,
                gain=30,
            )
            logger.info(f"  ✓ DSCDirectReader created")
            
            # Try to connect (will fail without hardware, but that's OK)
            if reader.connect():
                logger.info(f"  ✓ Connected to RTL-SDR")
            else:
                logger.info(f"  ℹ Could not connect to RTL-SDR (expected without hardware)")
                
        except DSCIngestError as e:
            logger.info(f"  ℹ DSCIngestError (expected): {e}")
        
    except ImportError as e:
        logger.error(f"  ✗ Could not import DSC modules: {e}")
    except Exception as e:
        logger.error(f"  ✗ Error testing DSCDirectReader: {e}", exc_info=True)
    
    logger.info("\n" + "=" * 60)


if __name__ == "__main__":
    logger.info("\nStarting DSC Decoder Test Suite\n")
    
    test_decoder()
    test_rtl_receiver()
    test_ingest_reader()
    
    logger.info("\n✓ All tests completed!\n")
