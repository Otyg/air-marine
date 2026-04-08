# SDR Monitor

Backend service for hybrid AIS/ADS-B/OGN/DSC monitoring with pluggable
radio/scanner backends (`legacy`, `inproc`, `external`, `mock`).

## What it does

- Alternates one RTL-SDR between `readsb` (ADS-B) and `rtl_ais` (AIS)
- Can ingest OGN/FLARM/ADS-L traffic from a local APRS/TCP decoder feed
- Normalizes decoder output into a shared observation model
- Maintains live in-memory targets with last five valid positions
- Persists observations and latest target state to SQLite
- Exposes HTTP endpoints for health, live targets, stats, and history
- Supports v2 radio backend architecture with optional external worker mode

## Project status

Implemented through phase 10 + v2 radio backend migration work:

- bootstrap, configuration, logging, domain models
- live state and persistence
- ADS-B and AIS ingest
- scanner and subprocess supervision
- API endpoints
- startup wiring and optional in-memory recovery from SQLite latest targets
- scanner/radio v2 backend contracts and orchestration
- hybrid mock-radio fixtures and tests
- external worker protocol support (control + data channels)

## System requirements

- Linux host with RTL-SDR support
- Python 3.11+ (tested on Python 3.13)
- `readsb` available on `PATH` (or adjusted command wiring)
- `rtl_ais` available on `PATH` (or adjusted command wiring)
- Optional: local OGN/FLARM/ADS-L decoder feed, for example `rtlsdr-ogn` exposing APRS traffic on TCP port `50001`
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
- `SDR_MONITOR_OGN_WINDOW_SECONDS`: optional OGN/FLARM/ADS-L scan window length; `0` disables glider polling in the scanner loop
- `SDR_MONITOR_AIS_WINDOW_SECONDS`: AIS scan window length
- `SDR_MONITOR_INTER_SCAN_PAUSE_SECONDS`: pause between AIS/ADS-B updates (default `2.0`)
- `SDR_MONITOR_FRESH_SECONDS`: freshness threshold lower bound
- `SDR_MONITOR_AGING_SECONDS`: freshness threshold upper bound
- `SDR_MONITOR_MAX_POSITIONS_PER_TARGET`: in-memory position history size
- `SDR_MONITOR_READSB_AIRCRAFT_JSON`: path to `readsb` `aircraft.json`
- `SDR_MONITOR_OGN_TCP_HOST`: host for decoded OGN/FLARM/ADS-L APRS traffic
- `SDR_MONITOR_OGN_TCP_PORT`: TCP port for decoded OGN/FLARM/ADS-L APRS traffic (commonly `50001`)
- `SDR_MONITOR_AIS_TCP_HOST`: AIS TCP host
- `SDR_MONITOR_AIS_TCP_PORT`: AIS TCP port
- `SDR_MONITOR_RADIO_BACKEND`: `legacy|inproc|external|mock` (default `legacy`)
- `SDR_MONITOR_ADSB_INPROC_SOURCE`: `readsb|rtl_tcp` for ADS-B when using `inproc`
- `SDR_MONITOR_ADSB_INPROC_RTL_HOST`: host for ADS-B rtl_tcp source
- `SDR_MONITOR_ADSB_INPROC_RTL_PORT`: port for ADS-B rtl_tcp source
- `SDR_MONITOR_ADSB_INPROC_SAMPLE_RATE`: ADS-B rtl_tcp sample rate
- `SDR_MONITOR_ADSB_INPROC_GAIN`: ADS-B rtl_tcp gain
- `SDR_MONITOR_ADSB_INPROC_FREQUENCY_HZ`: ADS-B retune frequency
- `SDR_MONITOR_AIS_FREQUENCY_HZ`: AIS retune frequency for v2 window switching
- `SDR_MONITOR_OGN_FREQUENCY_HZ`: OGN/FLARM retune frequency for v2 window switching
- `SDR_MONITOR_DSC_FREQUENCY_HZ`: DSC retune frequency for v2 window switching
- `SDR_MONITOR_RADIO_EXTERNAL_USE_WORKER`: enable external worker sockets for `external` backend
- `SDR_MONITOR_RADIO_EXTERNAL_CONTROL_HOST`: control socket host for external worker
- `SDR_MONITOR_RADIO_EXTERNAL_CONTROL_PORT`: control socket port for external worker
- `SDR_MONITOR_RADIO_EXTERNAL_DATA_HOST`: data socket host for external worker
- `SDR_MONITOR_RADIO_EXTERNAL_DATA_PORT`: data socket port for external worker
- `SDR_MONITOR_MOCK_RADIO_FIXTURE_PATH`: mock fixture JSON path when using `mock` backend
- `SDR_MONITOR_MOCK_RADIO_TIMING_ENABLED`: enable timing/jitter mode for mock backend
- `SDR_MONITOR_SQLITE_PATH`: SQLite database path
- `SDR_MONITOR_API_HOST`: API bind host
- `SDR_MONITOR_API_PORT`: API bind port
- `SDR_MONITOR_RADAR_CENTER_LAT`: radar center latitude (-90..90)
- `SDR_MONITOR_RADAR_CENTER_LON`: radar center longitude (-180..180)
- `SDR_MONITOR_FIXED_OBJECTS_PATH`: JSON file with static radar markers
- `SDR_MONITOR_MAP_SOURCE`: `hydro|elevation` (default `hydro`)
- `SDR_MONITOR_MAP_CACHE_TTL_SECONDS`: contour cache TTL in seconds
- `SDR_MONITOR_MAP_CACHE_DIR`: directory for persisted local contour cache
- `SDR_MONITOR_HYDRO_BASE_URL`: Hydrografi Direkt OGC Features base URL
- `SDR_MONITOR_HYDRO_USERNAME`: server-side Hydrografi Direkt username
- `SDR_MONITOR_HYDRO_PASSWORD`: server-side Hydrografi Direkt password
- `SDR_MONITOR_MARKHOJD_DIRECT_BASE_URL`: Markhöjd Direkt base URL
- `SDR_MONITOR_MARKHOJD_DIRECT_USERNAME`: Markhöjd Direkt username
- `SDR_MONITOR_MARKHOJD_DIRECT_PASSWORD`: Markhöjd Direkt password
- `SDR_MONITOR_MARKHOJD_DIRECT_SRID`: request SRID for Markhöjd Direkt sampling
- `SDR_MONITOR_MARKHOJD_DIRECT_SAMPLE_STEP_M`: planned sample spacing for contour generation
- `SDR_MONITOR_MARKHOJD_DIRECT_CONTOUR_INTERVAL_M`: contour interval in meters for local line generation
- `SDR_MONITOR_MARKHOJD_DIRECT_MAX_POINTS_PER_REQUEST`: cap for request batching, max `1000`

Map background notes:

- `hydro` is the production-ready source for coastline/lake contours
- `elevation` now samples `Markhöjd Direkt` using `MultiPoint` in EPSG `3006`
- local contour generation is currently based on a coarse sampled grid and simple contour segmentation
- larger views automatically use a coarser effective sample step to stay within the request point cap
- successful contour responses are persisted on disk and reused across restarts; external APIs are only called when a matching local cache file is missing
- hydro contour cache files can be migrated into SQLite for feature reuse across repeated bbox requests

Example static radar objects file (`./data/fixed_objects.json`):

```json
[
  {
    "name": "Home Harbor",
    "latitude": 56.1619519,
    "longitude": 15.5940978,
    "symbol": "H",
    "max_visible_range_km": 10
  },
  {
    "name": "Reference Mast",
    "latitude": 56.1692000,
    "longitude": 15.6023000
  }
]
```

Notes:

- `name`, `latitude`, and `longitude` are required
- `symbol` is optional; default symbol is `O`
- `max_visible_range_km` is optional; object is hidden when current range is larger
- the label (`name`) is drawn next to the symbol on the radar screen
- OGN/FLARM/ADS-L support in this service expects an already running local decoder which emits APRS-like aircraft lines over TCP; the common reference setup is `ogn-rf` + `ogn-decode` from `rtlsdr-ogn`
- ADS-L rides through the same APRS/OGN ingest path here; known `OGNSKY`/`SafeSky` packet formats are tagged with `payload_json.protocol = "ads-l"` and expose `icao24` when the sender declares an `ICAxxxxxx` identity
- parsed glider targets are stored with source `ogn` and target ids like `ogn:flarm-<address>` or `ogn:icao-<address>`

## Running

```bash
cd sdr_monitor
python -m app.main
```

### Backend selection examples

Use existing subprocess scanner (default):

```bash
SDR_MONITOR_RADIO_BACKEND=legacy python -m app.main
```

Use in-process v2 backend:

```bash
SDR_MONITOR_RADIO_BACKEND=inproc python -m app.main
```

Use one dongle with external `rtl_tcp` for ADS-B inproc decode:

```bash
SDR_MONITOR_RADIO_BACKEND=inproc \
SDR_MONITOR_ADSB_INPROC_SOURCE=rtl_tcp \
SDR_MONITOR_ADSB_INPROC_RTL_HOST=127.0.0.1 \
SDR_MONITOR_ADSB_INPROC_RTL_PORT=1234 \
python -m app.main
```

Use fixture-driven mock backend:

```bash
SDR_MONITOR_RADIO_BACKEND=mock \
SDR_MONITOR_MOCK_RADIO_FIXTURE_PATH=tests/fixtures/mock_radio/mixed_cycle.json \
python -m app.main
```

Use external backend with worker sockets:

```bash
SDR_MONITOR_RADIO_BACKEND=external \
SDR_MONITOR_RADIO_EXTERNAL_USE_WORKER=true \
python -m app.main
```

### Run external worker

Start worker service (control + data sockets):

```bash
cd sdr_monitor
python scripts/run_radio_worker.py
```

Optional flags:

- `--control-host`
- `--control-port`
- `--data-host`
- `--data-port`
- `--fixture-path`
- `--timing-mode`

The service starts:

1. Configuration + logging
2. SQLite initialization
3. Optional recovery of latest targets into in-memory state
4. Background scanner loop
5. FastAPI server

## API endpoints

- `GET /` (radar-like web UI)
- `GET /ui/targets-latest`
- `GET /ui/map-contours?bbox=min_lon,min_lat,max_lon,max_lat&range_km=...&source=hydro|elevation`
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

## Utility script

Backfill `target_names` (`id -> name`) from historical `observations`:

```bash
cd sdr_monitor
python scripts/populate_target_names_from_observations.py
```

Optional flags:

- `--sqlite-path /path/to/sdr_monitor.sqlite3`
- `--limit 50000`

Migrate persisted hydro contour cache files into SQLite:

```bash
cd sdr_monitor
python scripts/migrate_hydro_cache_to_sqlite.py
```

Optional flags:

- `--sqlite-path /path/to/sdr_monitor.sqlite3`
- `--cache-dir /path/to/data/map/cache/hydro`
- `--limit 100`
- `--dry-run`
