"""In-process AIS ingest from rtl_tcp I/Q samples.

Decoder is intentionally pluggable through `DSPBackend` so a C/C++ include can
provide the actual AIS demod/decode path without changing app contracts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging

from app.dsp_backend import DSPBackend
from app.ingest_adsb_inproc import RTLTCPClient
from app.ingest_ais import parse_ais_nmea_lines
from app.models import NormalizedObservation

logger = logging.getLogger(__name__)

AIS_SAMPLE_RATE_HZ = 288_000
AIS_CHANNEL_A_HZ = 161_975_000


@dataclass(slots=True)
class AISInprocReader:
    """Direct AIS reader from rtl_tcp I/Q samples."""

    rtl_host: str = "127.0.0.1"
    rtl_port: int = 1234
    sample_rate: int = AIS_SAMPLE_RATE_HZ
    gain: int = 30
    frequency_hz: int = AIS_CHANNEL_A_HZ
    client: RTLTCPClient | None = None
    dsp_backend: DSPBackend | None = None
    _client: RTLTCPClient = field(init=False, repr=False)
    _dsp: DSPBackend = field(init=False, repr=False)
    _warned_no_decoder: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self._client = self.client or RTLTCPClient(
            host=self.rtl_host,
            port=self.rtl_port,
            sample_rate=self.sample_rate,
            gain=self.gain,
            frequency=self.frequency_hz,
        )
        self._dsp = self.dsp_backend or DSPBackend()

    def read_observations(
        self,
        *,
        timeout_seconds: float = 0.2,  # noqa: ARG002 - kept for shared reader contract
        num_samples: int = 32_768,
    ) -> list[NormalizedObservation]:
        if not self._client.connect():
            return []

        iq_data = self._client.read_samples(num_samples=num_samples)
        if not iq_data:
            return []

        decode_fn = getattr(self._dsp, "decode_ais_nmea_lines", None)
        if not callable(decode_fn):
            if not self._warned_no_decoder:
                logger.debug("AIS inproc decoder not available in active DSP backend; returning no AIS lines.")
                self._warned_no_decoder = True
            return []

        try:
            nmea_lines = decode_fn(iq_data, self.sample_rate)
        except Exception as exc:
            logger.debug("AIS inproc decode failed: %s", exc)
            return []

        if not isinstance(nmea_lines, list):
            return []
        lines = [str(line) for line in nmea_lines if isinstance(line, str)]
        if not lines:
            return []
        return parse_ais_nmea_lines(lines)

    def retune(self, frequency_hz: int) -> None:
        self._client.retune(frequency_hz)

    def set_gain(self, gain_db: int) -> None:
        self._client.set_gain(gain_db)

    def close(self) -> None:
        self._client.close()
