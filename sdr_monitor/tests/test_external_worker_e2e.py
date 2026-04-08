from __future__ import annotations

from pathlib import Path
import socket
import subprocess
import time

import pytest

from app.models import ScanBand
from app.radio_v2 import ExternalBackend, ObservationEvent


def _find_free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _wait_for_port(port: int, timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def test_external_worker_process_e2e() -> None:
    try:
        _ = _find_free_port()
    except PermissionError:
        pytest.skip("socket operations are not permitted in this environment")

    fixture_path = (
        Path(__file__).resolve().parent / "fixtures" / "mock_radio" / "mixed_cycle.json"
    )
    control_port = _find_free_port()
    data_port = _find_free_port()

    worker = subprocess.Popen(
        [
            "python3",
            "scripts/run_radio_worker.py",
            "--control-host",
            "127.0.0.1",
            "--control-port",
            str(control_port),
            "--data-host",
            "127.0.0.1",
            "--data-port",
            str(data_port),
            "--fixture-path",
            str(fixture_path),
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        if not _wait_for_port(control_port):
            pytest.skip("external worker control port did not become ready")
        if not _wait_for_port(data_port):
            pytest.skip("external worker data port did not become ready")

        backend = ExternalBackend(
            readers={},
            use_worker=True,
            control_host="127.0.0.1",
            control_port=control_port,
            data_host="127.0.0.1",
            data_port=data_port,
        )
        backend.start()

        backend.retune(162000000)
        events = backend.read(0.5, band=ScanBand.AIS)

        assert events
        assert isinstance(events[0], ObservationEvent)
        assert events[0].observation.target_id == "ais:265123456"
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=2)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait(timeout=2)
