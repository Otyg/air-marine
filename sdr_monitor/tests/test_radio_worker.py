from __future__ import annotations

from pathlib import Path

from app.models import ScanBand
from app.radio_worker import build_default_worker_service, payload_to_line


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "mock_radio"


def test_worker_control_commands_and_status() -> None:
    service = build_default_worker_service(
        fixture_path=str(FIXTURE_DIR / "retune_mid_window.json"),
        timing_mode=False,
    )
    service.start()

    ping = service.handle_control_payload({"cmd": "ping"})
    assert ping["ok"] is True
    assert ping.get("pong") is True

    retune = service.handle_control_payload({"cmd": "retune", "hz": 1090000000})
    assert retune["ok"] is True

    invalid_retune = service.handle_control_payload({"cmd": "retune", "hz": -1})
    assert invalid_retune["ok"] is False

    gain = service.handle_control_payload({"cmd": "set_gain", "db": 20})
    assert gain["ok"] is True

    status = service.handle_control_payload({"cmd": "status"})
    assert status["ok"] is True
    assert isinstance(status["status"], dict)

    service.stop()


def test_worker_data_payloads_are_serialized_to_protocol_shape() -> None:
    service = build_default_worker_service(
        fixture_path=str(FIXTURE_DIR / "mixed_cycle.json"),
        timing_mode=False,
    )
    service.start()

    payloads = service.read_data_payloads(timeout_s=0.1, band=ScanBand.AIS)
    assert payloads
    payload = payloads[0]
    assert payload["type"] == "observation"
    assert payload["source_band"] == "ais"
    assert isinstance(payload["observation"], dict)

    service.stop()


def test_payload_to_line_encodes_json_line() -> None:
    encoded = payload_to_line({"ok": True, "value": 1})
    assert encoded.endswith(b"\n")
    assert b'"ok":true' in encoded


def test_worker_unsupported_command() -> None:
    service = build_default_worker_service(
        fixture_path=str(FIXTURE_DIR / "nominal_adsb.json"),
        timing_mode=False,
    )
    service.start()
    response = service.handle_control_payload({"cmd": "unknown-cmd"})
    assert response["ok"] is False
    assert "unsupported cmd" in response["error"]
    service.stop()
