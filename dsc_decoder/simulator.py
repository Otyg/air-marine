#!/usr/bin/env python3
"""
DSC Decoder Test Simulator

Simulates RTL-SDR data and DSC frames for testing without hardware.
"""

import asyncio
import json
import logging
import struct
from datetime import datetime, timezone

from dsc_decoder import DSCDecoder, DSCMessage, MessageType, DistressType

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class RTLSimulator:
    """Simulates RTL-SDR TCP server for testing"""

    def __init__(self, host: str = "127.0.0.1", port: int = 1234):
        self.host = host
        self.port = port

    async def start(self):
        """Start simulated RTL-SDR server"""
        server = await asyncio.start_server(self.handle_client, self.host, self.port)
        logger.info(f"RTL simulator listening on {self.host}:{self.port}")

        async with server:
            await server.serve_forever()

    async def handle_client(self, reader, writer):
        """Handle client connection (ignore commands, just send fake I/Q data)"""
        logger.info("RTL client connected")

        try:
            # Ignore any commands sent by client
            task = asyncio.create_task(reader.read(1024))

            # Send fake I/Q data every 100ms
            while True:
                try:
                    await asyncio.wait_for(task, timeout=0.1)
                except asyncio.TimeoutError:
                    pass

                # Generate random I/Q data
                fake_iq = self._generate_fake_iq(4800)  # 100ms at 48kHz
                writer.write(fake_iq)
                await writer.drain()

        except Exception as e:
            logger.info(f"RTL client disconnected: {e}")
        finally:
            writer.close()
            await writer.wait_closed()

    @staticmethod
    def _generate_fake_iq(num_samples: int) -> bytes:
        """Generate fake I/Q data"""
        data = bytearray()
        for i in range(num_samples):
            # Simple sinusoidal I/Q
            i_val = int(100 * (i % 10))
            q_val = int(100 * ((i + 5) % 10))
            data.extend(struct.pack("<hh", i_val, q_val))
        return bytes(data)


class DSCTestClient:
    """Test client that connects to DSC decoder server"""

    def __init__(self, host: str = "127.0.0.1", port: int = 6021):
        self.host = host
        self.port = port

    async def start(self):
        """Connect and receive DSC messages"""
        logger.info(f"Connecting to DSC server at {self.host}:{self.port}")

        try:
            reader, writer = await asyncio.open_connection(self.host, self.port)
            logger.info("Connected to DSC server")

            # Read messages
            while True:
                try:
                    data = await asyncio.wait_for(reader.read(4096), timeout=30.0)
                    if not data:
                        logger.info("Server closed connection")
                        break

                    # Parse frame-delimited messages
                    self._parse_frames(data)

                except asyncio.TimeoutError:
                    logger.debug("No data received (timeout)")

        except Exception as e:
            logger.error(f"Client error: {e}")
        finally:
            writer.close()
            await writer.wait_closed()

    @staticmethod
    def _parse_frames(data: bytes):
        """Parse frame-delimited DSC messages"""
        frames = data.split(b"\x7e")
        for frame in frames:
            if frame:
                try:
                    msg = json.loads(frame.decode("utf-8"))
                    logger.info(f"Received DSC: {msg}")
                except Exception as e:
                    logger.warning(f"Error parsing frame: {e}")


def generate_test_frame(
    mmsi: int,
    message_type: MessageType,
    distress_type: Optional[DistressType] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
) -> bytes:
    """
    Generate a test DSC frame for decoder testing.
    
    This creates synthetic frames that the decoder can process.
    """
    frame = bytearray()

    # FEC bytes (simplified - just fill with zeros)
    frame.extend(b"\x00\x00\x00")

    # MMSI (4 bytes, 30 bits used)
    mmsi_bytes = struct.pack(">I", mmsi << 2)
    frame.extend(mmsi_bytes)

    # Message type (4 bits) + reserved bits
    frame.append((message_type.value << 4) | 0x00)

    # Distress type (if applicable)
    if distress_type:
        frame.append((distress_type.value << 4) | 0x00)
    else:
        frame.append(0x00)

    # Position (if applicable)
    if latitude is not None and longitude is not None:
        # Simplified encoding (27 bits for lat, 28 bits for lon)
        lat_int = int((latitude / 90 + 1) * (1 << 26))
        lon_int = int((longitude / 180 + 1) * (1 << 27))

        frame.extend(struct.pack(">I", lat_int)[:3])
        frame.extend(struct.pack(">I", lon_int)[:3])

    # Wrap in frame flags
    frame_with_flags = b"\x7e" + bytes(frame) + b"\x7e"

    return frame_with_flags


async def test_decoder():
    """Test the DSC decoder with synthetic frames"""
    logger.info("Testing DSC decoder with synthetic frames...")

    decoder = DSCDecoder()

    # Test frames
    test_cases = [
        {
            "mmsi": 123456789,
            "message_type": MessageType.DISTRESS,
            "distress_type": DistressType.SINKING,
            "latitude": 57.5,
            "longitude": 11.5,
        },
        {
            "mmsi": 987654321,
            "message_type": MessageType.URGENCY,
            "latitude": 58.0,
            "longitude": 12.0,
        },
    ]

    for test_case in test_cases:
        frame = generate_test_frame(**test_case)
        logger.info(f"Testing frame: {test_case}")

        # Extract frame bits
        bits = bytearray()
        for byte in frame:
            for i in range(8):
                bits.append((byte >> (7 - i)) & 1)

        # Decode
        messages = decoder.feed_bits(bytes(bits))

        for msg in messages:
            logger.info(f"  Decoded: {msg.to_dict()}")


async def main():
    """Run simulators and test"""
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "test":
        # Just run decoder test
        await test_decoder()
    else:
        # Run both RTL simulator and test client
        rtl_sim = RTLSimulator()
        client = DSCTestClient()

        await asyncio.gather(
            rtl_sim.start(),
            asyncio.sleep(1),  # Wait for server
            client.start(),
        )


if __name__ == "__main__":
    asyncio.run(main())
