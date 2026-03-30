# SDR Monitor

Backend service for hybrid, single-RTL-SDR AIS/ADS-B monitoring.

## What it does

- Alternates one RTL-SDR between `readsb` (ADS-B) and `rtl_ais` (AIS)
- Normalizes decoder output into a shared observation model
- Maintains live in-memory targets with last five valid positions
- Persists observations and latest target state to SQLite
- Exposes HTTP endpoints for health, live targets, stats, and history

## Project status

Implemented through phase 10:

- bootstrap, configuration, logging, domain models
- live state and persistence
- ADS-B and AIS ingest
- scanner and subprocess supervision
- API endpoints
- startup wiring and optional in-memory recovery from SQLite latest targets

## System requirements

- Linux host with RTL-SDR support
- Python 3.11+ (tested on Python 3.13)
- `readsb` available on `PATH` (or adjusted command wiring)
- `rtl_ais` available on `PATH` (or adjusted command wiring)
- Optional: TCP AIS feed compatible with NMEA AIVDM/AIVDO sentences

## Installation

```bash
cd sdr_monitor
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Create your env file from `.env.example`:

```bash
cp .env.example .env
```

Key runtime variables:

- `SDR_MONITOR_SERVICE_NAME`: service name shown in health payload
- `SDR_MONITOR_LOG_LEVEL`: `DEBUG|INFO|WARNING|ERROR|CRITICAL`
- `SDR_MONITOR_ADSB_WINDOW_SECONDS`: ADS-B scan window length
- `SDR_MONITOR_AIS_WINDOW_SECONDS`: AIS scan window length
- `SDR_MONITOR_FRESH_SECONDS`: freshness threshold lower bound
- `SDR_MONITOR_AGING_SECONDS`: freshness threshold upper bound
- `SDR_MONITOR_MAX_POSITIONS_PER_TARGET`: in-memory position history size
- `SDR_MONITOR_READSB_AIRCRAFT_JSON`: path to `readsb` `aircraft.json`
- `SDR_MONITOR_AIS_TCP_HOST`: AIS TCP host
- `SDR_MONITOR_AIS_TCP_PORT`: AIS TCP port
- `SDR_MONITOR_SQLITE_PATH`: SQLite database path
- `SDR_MONITOR_API_HOST`: API bind host
- `SDR_MONITOR_API_PORT`: API bind port

## Running

```bash
cd sdr_monitor
python -m app.main
```

The service starts:

1. Configuration + logging
2. SQLite initialization
3. Optional recovery of latest targets into in-memory state
4. Background scanner loop
5. FastAPI server

## API endpoints

- `GET /health`
- `GET /targets?kind=aircraft|vessel&fresh_only=true|false`
- `GET /targets/{target_id}`
- `GET /stats`
- `GET /history/{target_id}?limit=100`

## Tests

```bash
cd sdr_monitor
pytest -q
```
