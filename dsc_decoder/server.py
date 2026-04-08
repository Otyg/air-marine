#!/usr/bin/env python3
"""
DSC Decoder TCP Server

Connects to RTL-SDR (via rtl_tcp or local interface), demodulates and decodes
DSC messages, then serves them via TCP to connected clients (like SDR Monitor).
"""

import asyncio
import json
import logging
import struct
import sys
from argparse import ArgumentParser
from datetime import datetime, timezone
from typing import Optional

from dsc_decoder import DSCReceiver

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class DSCDecoderServer:
    """
    TCP server that decodes DSC messages and publishes them.
    """

    def __init__(
        self,
        rtl_host: str = "127.0.0.1",
        rtl_port: int = 1234,
        server_host: str = "127.0.0.1",
        server_port: int = 6021,
        sample_rate: int = 48000,
        gain: int = 30,
    ):
        """
        Initialize DSC decoder server.
        
        Args:
            rtl_host: Host of RTL-SDR TCP server
            rtl_port: Port of RTL-SDR TCP server
            server_host: Host to bind server to
            server_port: Port to bind server to
            sample_rate: RTL-SDR sample rate
            gain: RTL-SDR gain
        """
        self.rtl_host = rtl_host
        self.rtl_port = rtl_port
        self.server_host = server_host
        self.server_port = server_port
        self.sample_rate = sample_rate
        self.gain = gain

        self.receiver = DSCReceiver(
            sample_rate=sample_rate,
            frequency=156.525e6,
            gain=gain,
        )

        self.clients = set()
        self.rtl_reader = None
        self.rtl_writer = None
        self.running = False

    async def connect_rtl_sdr(self) -> bool:
        """Connect to RTL-SDR TCP server"""
        try:
            logger.info(f"Connecting to RTL-SDR at {self.rtl_host}:{self.rtl_port}")
            self.rtl_reader, self.rtl_writer = await asyncio.open_connection(
                self.rtl_host, self.rtl_port
            )

            # Send RTL command: SET_FREQUENCY (0x01)
            freq_bytes = struct.pack("<BI", 1, int(156.525e6))
            self.rtl_writer.write(freq_bytes)
            await self.rtl_writer.drain()

            # Send RTL command: SET_SAMPLE_RATE (0x02)
            sr_bytes = struct.pack("<BI", 2, self.sample_rate)
            self.rtl_writer.write(sr_bytes)
            await self.rtl_writer.drain()

            # Send RTL command: SET_GAIN (0x04)
            gain_bytes = struct.pack("<BI", 4, self.gain)
            self.rtl_writer.write(gain_bytes)
            await self.rtl_writer.drain()

            logger.info("RTL-SDR connected and configured")
            return True

        except Exception as e:
            logger.error(f"Failed to connect to RTL-SDR: {e}")
            return False

    async def read_rtl_loop(self):
        """Read I/Q data from RTL-SDR and decode DSC"""
        if not self.rtl_reader:
            return

        chunk_size = self.sample_rate // 10  # 100ms of I/Q data
        bytes_per_chunk = chunk_size * 4  # 2 shorts per sample (I, Q)

        try:
            while self.running:
                try:
                    iq_data = await self.rtl_reader.readexactly(bytes_per_chunk)

                    if not iq_data:
                        logger.warning("RTL-SDR connection closed")
                        break

                    # Process I/Q data
                    messages = self.receiver.process_iq_data(iq_data)

                    # Broadcast decoded messages to clients
                    for msg in messages:
                        await self._broadcast_message(msg)

                except asyncio.IncompleteReadError as e:
                    logger.warning(f"Incomplete read from RTL-SDR: {e}")
                    break
                except Exception as e:
                    logger.error(f"Error reading RTL-SDR data: {e}")
                    break

        except Exception as e:
            logger.error(f"RTL-SDR read loop error: {e}")
        finally:
            logger.info("RTL-SDR read loop ended")
            self.running = False

    async def _broadcast_message(self, msg):
        """Broadcast decoded DSC message to all connected clients"""
        timestamp = datetime.now(timezone.utc).isoformat()

        # Create frame format: 0x7E + JSON data + 0x7E
        frame_data = {
            "timestamp": timestamp,
            "mmsi": msg.mmsi,
            "message_type": msg.message_type.name,
            "distress_type": msg.distress_type.name if msg.distress_type else None,
            "latitude": msg.latitude,
            "longitude": msg.longitude,
            "raw": msg.raw_frame.hex() if msg.raw_frame else None,
        }

        frame_json = json.dumps(frame_data)
        frame_bytes = frame_json.encode("utf-8")
        frame_with_flags = b"\x7e" + frame_bytes + b"\x7e"

        # Send to all clients
        dead_clients = set()
        for client in self.clients:
            try:
                client.write(frame_with_flags)
                await client.drain()
                logger.debug(
                    f"Sent DSC message to client: MMSI={msg.mmsi} Type={msg.message_type.name}"
                )
            except Exception as e:
                logger.warning(f"Error sending to client: {e}")
                dead_clients.add(client)

        # Clean up dead clients
        self.clients -= dead_clients

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        """Handle new client connection"""
        client_addr = writer.get_extra_info("peername")
        logger.info(f"Client connected: {client_addr}")
        self.clients.add(writer)

        try:
            # Client can send commands or just receive
            while self.running:
                try:
                    data = await asyncio.wait_for(reader.read(1024), timeout=60.0)
                    if not data:
                        break

                    # Simple protocol: empty data means keep-alive
                    logger.debug(f"Received from client: {data}")

                except asyncio.TimeoutError:
                    # No data, client still alive
                    pass

        except Exception as e:
            logger.warning(f"Client error: {e}")
        finally:
            logger.info(f"Client disconnected: {client_addr}")
            self.clients.discard(writer)
            writer.close()
            await writer.wait_closed()

    async def start_server(self):
        """Start TCP server"""
        server = await asyncio.start_server(
            self.handle_client, self.server_host, self.server_port
        )

        addr = server.sockets[0].getsockname()
        logger.info(f"DSC Decoder server listening on {addr[0]}:{addr[1]}")

        async with server:
            await server.serve_forever()

    async def run(self):
        """Run the DSC decoder server"""
        self.running = True

        # Connect to RTL-SDR
        if not await self.connect_rtl_sdr():
            logger.error("Failed to connect to RTL-SDR, exiting")
            self.running = False
            return

        try:
            # Start server and RTL read loop
            await asyncio.gather(
                self.start_server(),
                self.read_rtl_loop(),
            )
        except KeyboardInterrupt:
            logger.info("Shutting down...")
        finally:
            self.running = False
            if self.rtl_writer:
                self.rtl_writer.close()
                await self.rtl_writer.wait_closed()


async def main():
    """Main entry point"""
    parser = ArgumentParser(description="DSC Decoder TCP Server")
    parser.add_argument(
        "--rtl-host",
        default="127.0.0.1",
        help="RTL-SDR TCP server host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--rtl-port",
        type=int,
        default=1234,
        help="RTL-SDR TCP server port (default: 1234)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="DSC server bind host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=6021,
        help="DSC server bind port (default: 6021)",
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=48000,
        help="RTL-SDR sample rate (default: 48000)",
    )
    parser.add_argument(
        "--gain",
        type=int,
        default=30,
        help="RTL-SDR gain (default: 30)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )

    args = parser.parse_args()

    # Set logging level
    logging.getLogger().setLevel(args.log_level)

    # Create and run server
    server = DSCDecoderServer(
        rtl_host=args.rtl_host,
        rtl_port=args.rtl_port,
        server_host=args.host,
        server_port=args.port,
        sample_rate=args.sample_rate,
        gain=args.gain,
    )

    await server.run()


if __name__ == "__main__":
    asyncio.run(main())
