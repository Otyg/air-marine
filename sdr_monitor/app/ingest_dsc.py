"""DSC (Digital Selective Calling) ingest adapter for VHF Channel 70 (156.525 MHz).

Uses direct RTL-SDR integration via the dsc_decoder module.
No TCP intermediary needed - decoding happens locally in-process.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.models import NormalizedObservation, Source, TargetKind

logger = logging.getLogger(__name__)

# Add dsc_decoder to path for imports
_dsc_decoder_path = Path(__file__).parent.parent.parent / "dsc_decoder"
if str(_dsc_decoder_path) not in sys.path:
    sys.path.insert(0, str(_dsc_decoder_path))

# Try to import DSC modules
_HAS_DSC = False
try:
    from dsc_decoder import DSCDecoder, MessageType, DistressType, DSCMessage
    from rtl_receiver import RTLReceiver

    _HAS_DSC = True
except ImportError as e:
    logger.warning(f"DSC modules not available: {e}")


class DSCIngestError(RuntimeError):
    """Raised when DSC data cannot be read from the configured source."""


class DSCDirectReader:
    """
    Direct DSC reader using RTL-SDR with local decoding.
    
    This reader:
    1. Connects to an rtl_tcp server (if available)
    2. Receives I/Q samples on VHF Channel 70 (156.525 MHz)
    3. Demodulates and decodes DSC frames locally
    4. Returns normalized observations
    """

    def __init__(
        self,
        rtl_host: str = "127.0.0.1",
        rtl_port: int = 1234,
        sample_rate: int = 48000,
        gain: int = 30,
    ):
        """
        Initialize DSC reader.

        Args:
            rtl_host: rtl_tcp server host
            rtl_port: rtl_tcp server port
            sample_rate: Sample rate in Hz
            gain: Receiver gain in dB

        Raises:
            DSCIngestError: If DSC modules are not available
        """
        if not _HAS_DSC:
            raise DSCIngestError(
                "DSC decoder modules not available. "
                "Ensure dsc_decoder module is installed."
            )

        self._receiver = RTLReceiver(
            frequency=156.525e6,  # VHF Channel 70
            sample_rate=sample_rate,
            gain=gain,
            host=rtl_host,
            port=rtl_port,
        )
        self._decoder = DSCDecoder()
        self._connected = False

    def connect(self) -> bool:
        """
        Connect to RTL-SDR receiver.

        Returns:
            True if connected successfully
        """
        try:
            if self._receiver.connect():
                self._connected = True
                logger.info("DSC reader connected to RTL-SDR")
                return True
            else:
                logger.error("Failed to connect DSC reader to RTL-SDR")
                return False
        except Exception as e:
            logger.error(f"DSC connection error: {e}")
            return False

    def disconnect(self):
        """Disconnect from RTL-SDR receiver."""
        self._receiver.disconnect()
        self._connected = False

    def read_observations(self, **kwargs: Any) -> list[NormalizedObservation]:
        """
        Read DSC observations from RTL-SDR.

        Returns:
            List of normalized observations

        Raises:
            DSCIngestError: If reader is not connected or read fails
        """
        if not self._connected:
            raise DSCIngestError("DSC reader not connected to RTL-SDR")

        observations = []

        try:
            # Read raw I/Q samples from RTL-SDR
            iq_data = self._receiver.read_samples(num_samples=1024)
            if not iq_data:
                return observations

            # Demodulate I/Q to bits
            bits = self._receiver.demodulate_to_bits(iq_data)
            if not bits:
                return observations

            # Decode DSC frames
            messages = self._decoder.feed_bits(bits)

            # Convert to observations
            for msg in messages:
                obs = self._message_to_observation(msg)
                if obs:
                    observations.append(obs)

        except Exception as e:
            logger.error(f"Error reading DSC observations: {e}")
            raise DSCIngestError(f"DSC read error: {e}") from e

        return observations

    @staticmethod
    def _message_to_observation(msg: DSCMessage) -> NormalizedObservation | None:
        """Convert DSC message to normalized observation."""
        try:
            # Build descriptive label
            label = f"MMSI: {msg.mmsi} "

            if msg.message_type == MessageType.DISTRESS:
                distress_name = (
                    msg.distress_type.name if msg.distress_type else "UNKNOWN"
                )
                label += f"DISTRESS ({distress_name})"
            elif msg.message_type == MessageType.URGENCY:
                label += "URGENCY"
            elif msg.message_type == MessageType.SAFETY:
                label += "SAFETY"
            else:
                label += msg.message_type.name

            return NormalizedObservation(
                source=Source.DSC,
                target_id=f"dsc_{msg.mmsi}",
                target_kind=TargetKind.VESSEL,
                label=label,
                latitude=msg.latitude,
                longitude=msg.longitude,
                altitude=None,
                speed_knots=None,
                heading=None,
                climb_rate_fpm=None,
                timestamp=msg.timestamp
                or datetime.now(timezone.utc).isoformat(),
                raw=msg.raw_frame.hex() if msg.raw_frame else None,
            )
        except Exception as e:
            logger.error(f"Error converting DSC message to observation: {e}")
            return None
