"""Subprocess supervision for decoder process lifecycle."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from app.models import ScanBand

PopenFactory = Callable[..., Any]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class DecoderProcessConfig:
    adsb_command: tuple[str, ...] = ("readsb",)
    ais_command: tuple[str, ...] = ("rtl_ais",)
    cwd: str | None = None
    env: Mapping[str, str] | None = None


class ProcessSupervisor:
    """Manage one subprocess at a time with start/stop/restart semantics."""

    def __init__(
        self,
        *,
        popen_factory: PopenFactory | None = None,
        terminate_timeout_seconds: float = 2.0,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._popen_factory = popen_factory or subprocess.Popen
        self._terminate_timeout_seconds = terminate_timeout_seconds
        self._now_fn = now_fn or _utcnow

        self._process: Any | None = None
        self._active_name: str | None = None
        self._started_at: datetime | None = None
        self._last_error: str | None = None

    @property
    def active_name(self) -> str | None:
        return self._active_name if self.is_running() else None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(
        self,
        *,
        name: str,
        command: Sequence[str],
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if not command:
            raise ValueError("command must not be empty")

        if self.is_running() and self._active_name == name:
            return

        self.stop()

        try:
            self._process = self._popen_factory(
                list(command),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=cwd,
                env=dict(env) if env is not None else None,
            )
            self._active_name = name
            self._started_at = self._now_fn()
            self._last_error = None
        except Exception as exc:
            self._process = None
            self._active_name = None
            self._last_error = str(exc)
            raise

    def stop(self) -> None:
        process = self._process
        if process is None:
            self._active_name = None
            self._started_at = None
            return

        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=self._terminate_timeout_seconds)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=self._terminate_timeout_seconds)
                except Exception as exc:
                    self._last_error = str(exc)
                    raise

        self._process = None
        self._active_name = None
        self._started_at = None

    def status(self) -> dict[str, Any]:
        return {
            "active_name": self.active_name,
            "is_running": self.is_running(),
            "pid": getattr(self._process, "pid", None) if self._process else None,
            "started_at": self._started_at,
            "last_error": self._last_error,
        }


class DecoderSupervisor:
    """Band-aware decoder supervisor that switches between ADS-B and AIS decoders."""

    def __init__(
        self,
        config: DecoderProcessConfig | None = None,
        *,
        process_supervisor: ProcessSupervisor | None = None,
    ) -> None:
        self._config = config or DecoderProcessConfig()
        self._process_supervisor = process_supervisor or ProcessSupervisor()

    @property
    def active_band(self) -> ScanBand | None:
        active = self._process_supervisor.active_name
        if active == ScanBand.ADSB.value:
            return ScanBand.ADSB
        if active == ScanBand.AIS.value:
            return ScanBand.AIS
        return None

    @property
    def last_error(self) -> str | None:
        return self._process_supervisor.last_error

    def switch_to(self, band: ScanBand) -> None:
        if band == ScanBand.ADSB:
            command = self._config.adsb_command
        elif band == ScanBand.AIS:
            command = self._config.ais_command
        else:
            raise ValueError(f"Unsupported band: {band}")

        self._process_supervisor.start(
            name=band.value,
            command=command,
            cwd=self._config.cwd,
            env=self._config.env,
        )

    def stop_active(self) -> None:
        self._process_supervisor.stop()

    def status(self) -> dict[str, Any]:
        status = self._process_supervisor.status()
        status["active_band"] = self.active_band.value if self.active_band else None
        return status
