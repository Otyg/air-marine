#!/usr/bin/env python3
"""Run external radio worker control/data servers.

This script is intentionally minimal and uses the shared protocol expected by
`ExternalBackend` in `app.radio_v2`.
"""

from __future__ import annotations

import argparse
import json
import logging
import socketserver
from threading import Thread
from typing import Any

from app.config import Config
from app.radio_worker import build_default_worker_service, payload_to_line


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run external radio worker")
    parser.add_argument("--control-host", default=None)
    parser.add_argument("--control-port", type=int, default=None)
    parser.add_argument("--data-host", default=None)
    parser.add_argument("--data-port", type=int, default=None)
    parser.add_argument("--fixture-path", default=None)
    parser.add_argument("--timing-mode", action="store_true")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    config = Config.from_env()

    control_host = args.control_host or config.radio_external_control_host
    control_port = args.control_port or config.radio_external_control_port
    data_host = args.data_host or config.radio_external_data_host
    data_port = args.data_port or config.radio_external_data_port
    fixture_path = args.fixture_path or str(config.mock_radio_fixture_path)
    timing_mode = bool(args.timing_mode or config.mock_radio_timing_enabled)

    service = build_default_worker_service(fixture_path=fixture_path, timing_mode=timing_mode)
    service.start()

    class ControlHandler(socketserver.StreamRequestHandler):
        def handle(self) -> None:  # noqa: D401
            raw = self.rfile.readline().decode("utf-8", errors="replace").strip()
            payload: dict[str, Any]
            try:
                payload = json.loads(raw) if raw else {}
                if not isinstance(payload, dict):
                    payload = {}
            except json.JSONDecodeError:
                payload = {}
            response = service.handle_control_payload(payload)
            self.wfile.write(payload_to_line(response))

    class DataHandler(socketserver.StreamRequestHandler):
        def handle(self) -> None:  # noqa: D401
            payloads = service.read_data_payloads(timeout_s=0.5)
            for payload in payloads:
                self.wfile.write(payload_to_line(payload))

    control_server = socketserver.ThreadingTCPServer((control_host, control_port), ControlHandler)
    data_server = socketserver.ThreadingTCPServer((data_host, data_port), DataHandler)
    control_server.daemon_threads = True
    data_server.daemon_threads = True

    control_thread = Thread(target=control_server.serve_forever, daemon=True)
    data_thread = Thread(target=data_server.serve_forever, daemon=True)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.info("Starting radio worker control=%s:%s data=%s:%s", control_host, control_port, data_host, data_port)

    control_thread.start()
    data_thread.start()

    try:
        control_thread.join()
        data_thread.join()
    except KeyboardInterrupt:
        logging.info("Stopping radio worker")
    finally:
        control_server.shutdown()
        data_server.shutdown()
        control_server.server_close()
        data_server.server_close()
        service.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
