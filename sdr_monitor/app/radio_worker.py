"""External radio worker service.

Provides a control/data socket protocol consumed by `ExternalBackend`.
This implementation is intentionally minimal and backend-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from threading import Lock
from typing import Any, Mapping

from app.models import NormalizedObservation, ScanBand
from app.radio_v2 import DecodedFrameEvent, IQChunkEvent, MockBackend, ObservationEvent, RadioBackend

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    control_host: str = "127.0.0.1"
    control_port: int = 17601
    data_host: str = "127.0.0.1"
    data_port: int = 17602


class RadioWorkerService:
    """Thread-safe worker facade over a `RadioBackend` implementation."""

    def __init__(self, backend: RadioBackend) -> None:
        self._backend = backend
        self._lock = Lock()
        self._started = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._backend.start()
            self._started = True

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._backend.stop()
            self._started = False

    def handle_control_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        command = str(payload.get("cmd") or "").strip().lower()
        if not command:
            return {"ok": False, "error": "cmd is required"}

        try:
            with self._lock:
                if command == "retune":
                    if "hz" not in payload:
                        return {"ok": False, "error": "hz is required"}
                    self._backend.retune(int(payload["hz"]))
                    return {"ok": True}

                if command == "set_gain":
                    if "db" not in payload:
                        return {"ok": False, "error": "db is required"}
                    self._backend.set_gain(int(payload["db"]))
                    return {"ok": True}

                if command in {"status", "health"}:
                    return {"ok": True, "status": self._backend.status().to_dict()}

                if command == "ping":
                    return {"ok": True, "pong": True}

                if command == "stop":
                    self.stop()
                    return {"ok": True, "stopped": True}

            return {"ok": False, "error": f"unsupported cmd: {command}"}
        except Exception as exc:
            logger.debug("Worker control command failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    def read_data_payloads(self, *, timeout_s: float, band: ScanBand | None = None) -> list[dict[str, Any]]:
        with self._lock:
            events = self._backend.read(timeout_s, band=band)
        return [self._serialize_event(event) for event in events]

    @staticmethod
    def _serialize_event(event: IQChunkEvent | DecodedFrameEvent | ObservationEvent) -> dict[str, Any]:
        if isinstance(event, ObservationEvent):
            observation: NormalizedObservation = event.observation
            return {
                "type": "observation",
                "source_band": event.source_band.value,
                "observation": observation.to_dict(),
            }

        if isinstance(event, DecodedFrameEvent):
            return {
                "type": "decoded_frame",
                "source_band": event.source_band.value,
                "frame_type": event.frame_type,
                "payload": event.payload,
            }

        return {
            "type": "iq_chunk",
            "source_band": event.source_band.value,
            "sample_rate": event.sample_rate,
            "iq_hex": event.iq_bytes.hex(),
        }


def build_default_worker_service(*, fixture_path: str, timing_mode: bool) -> RadioWorkerService:
    backend = MockBackend(fixture_path=fixture_path, enable_timing_mode=timing_mode)
    return RadioWorkerService(backend=backend)


def payload_to_line(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(payload), separators=(",", ":")) + "\n").encode("utf-8")
