"""Optional DSP backend boundary for future C/C++ acceleration.

The public scanner/radio interfaces stay unchanged regardless of whether
an accelerated extension is available.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True, slots=True)
class DSPBackendInfo:
    name: str
    accelerated: bool


class DSPBackend:
    """Dispatches DSP primitives to accelerated extension when available."""

    def __init__(self) -> None:
        self._impl_name = "python"
        self._accelerated = False
        self._demodulate_fn: Callable[[bytes], bytes] = _python_demodulate
        self._decode_ais_fn: Callable[..., list[str]] | None = None

        try:
            from app import _radio_dsp  # type: ignore[attr-defined]

            demodulate = getattr(_radio_dsp, "demodulate", None)
            if callable(demodulate):
                self._demodulate_fn = demodulate
                self._impl_name = "cpp"
                self._accelerated = True
            decode_ais = getattr(_radio_dsp, "decode_ais_nmea_lines", None)
            if callable(decode_ais):
                self._decode_ais_fn = decode_ais
        except Exception:
            # Python fallback is expected in CI/dev environments.
            self._impl_name = "python"
            self._accelerated = False

    def demodulate(self, iq_data: bytes) -> bytes:
        return self._demodulate_fn(iq_data)

    def decode_ais_nmea_lines(self, iq_data: bytes, sample_rate: int) -> list[str]:
        decode_fn = getattr(self, "_decode_ais_fn", None)
        if callable(decode_fn):
            try:
                decoded = decode_fn(iq_data, sample_rate)
            except TypeError:
                decoded = decode_fn(iq_data)
            if isinstance(decoded, list):
                return [str(line) for line in decoded if isinstance(line, str)]
        return []

    def info(self) -> DSPBackendInfo:
        return DSPBackendInfo(name=self._impl_name, accelerated=self._accelerated)


def _python_demodulate(iq_data: bytes) -> bytes:
    # Placeholder fallback: pass-through bytes to keep behavior deterministic
    # until accelerated DSP primitives are introduced.
    return bytes(iq_data)
