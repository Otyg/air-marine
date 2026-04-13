# SDR Monitor

Backend service for hybrid, single-RTL-SDR AIS/ADS-B monitoring.

## What it does

- Alternates one RTL-SDR between `readsb` (ADS-B) and `rtl_ais` (AIS)
- Can ingest OGN/FLARM/ADS-L traffic from a local APRS/TCP decoder feed
- Normalizes decoder output into a shared observation model
- Maintains live in-memory targets and a rolling 2-minute trail for moving objects (`speed > 1`)
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
- `SDR_MONITOR_STDOUT_LOG_PATH`: optional file path for process `stdout`; defaults to disabled
- `SDR_MONITOR_STDERR_LOG_PATH`: file path for process `stderr`; defaults to `./data/errors.log`
- `SDR_MONITOR_ADSB_WINDOW_SECONDS`: ADS-B scan window length
- `SDR_MONITOR_OGN_WINDOW_SECONDS`: optional OGN/FLARM/ADS-L scan window length; `0` disables glider polling in the scanner loop
- `SDR_MONITOR_AIS_WINDOW_SECONDS`: AIS scan window length
- `SDR_MONITOR_INTER_SCAN_PAUSE_SECONDS`: pause between AIS/ADS-B updates (default `2.0`)
- `SDR_MONITOR_FRESH_SECONDS`: freshness threshold lower bound
- `SDR_MONITOR_AGING_SECONDS`: freshness threshold upper bound
- Live recent-position trails are now time-windowed (2 minutes) and only sent for moving targets (`speed > 1`)
- `SDR_MONITOR_READSB_AIRCRAFT_JSON`: path to `readsb` `aircraft.json`
- `SDR_MONITOR_OGN_TCP_HOST`: host for decoded OGN/FLARM/ADS-L APRS traffic
- `SDR_MONITOR_OGN_TCP_PORT`: TCP port for decoded OGN/FLARM/ADS-L APRS traffic (commonly `50001`)
- `SDR_MONITOR_AIS_TCP_HOST`: AIS TCP host
- `SDR_MONITOR_AIS_TCP_PORT`: AIS TCP port
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

The service starts:

1. Configuration + logging
2. SQLite initialization
3. Optional recovery of latest targets into in-memory state
4. Background scanner loop
5. FastAPI server

Run native Qt desktop live view (REST-only, no web UI dependency):

```bash
cd sdr_monitor
pip install -r requirements-qt.txt
cp qt_client/config.example.json qt_client/config.json
python3 scripts/run_qt_live_view.py --config qt_client/config.json
```

Optional launch flags:

- `--base-url http://backend-host:8000`
- `--title "Air-Marine Live Radar"`

The Qt client reads all client-side settings from `qt_client/config.json`, so it can run on a separate machine and point to any reachable backend base URL.
By default, it does not consume backend `/ui/live-config`; center position, service name, map source, and fixed objects are read from `qt_client/config.json`.
Set `use_backend_live_config: true` in `qt_client/config.json` if you explicitly want backend-provided live config overrides.
When backend live config is enabled, fixed objects are merged: backend fixed objects are used as base, local `fixed_objects` are added/replaced by matching `name`, and `fixed_objects_remove_names` removes backend objects by name.
You can tune moving-object trail length with `trail_point_window_seconds`, moving-object marker sizing with `marker_size_scale`, fixed-object symbol sizing with `fixed_marker_size_scale`, vessel-vs-aircraft symbol visual parity with `vessel_symbol_box_factor`, and zoom-dependent marker/vector scaling aggressiveness with `zoom_visual_exponent`.
In the QT UI, use the `Installningar` button to open a dialog where you can save new defaults to `config.json` and apply a temporary marker-size multiplier for the current session.

## Run in background (systemd, survives logout)

Use a user-level `systemd` service so the app keeps running even when you are not logged in.

Install and start:

```bash
cd sdr_monitor
./scripts/install_systemd_user_service.sh
```

Enable lingering once so user services continue after logout/reboot:

```bash
sudo loginctl enable-linger "$USER"
```

Service commands:

```bash
systemctl --user status sdr-monitor.service
systemctl --user restart sdr-monitor.service
systemctl --user stop sdr-monitor.service
journalctl --user -u sdr-monitor.service -f
```

If you prefer manual setup, use the template unit file:

```bash
./deploy/systemd/sdr-monitor.service
```

## API endpoints

- `GET /` (radar-like web UI)
- `GET /ui/live-config`
- `GET /ui/targets-latest`
- `GET /ui/map-contours?bbox=min_lon,min_lat,max_lon,max_lat&range_km=...&source=hydro|elevation`
- `GET /scanner/scan`
- `POST /scanner/scan` with payload like `{ "scan": ["AIS", "ADS", "FLARM"] }`
- `GET /health`
- `GET /targets?kind=aircraft|vessel&fresh_only=true|false`
- `GET /targets/{target_id}`
- `GET /stats`
- `GET /history/{target_id}?limit=100`

Set scanner selection via API:

```bash
curl -X POST "http://127.0.0.1:8000/scanner/scan" \
  -H "Content-Type: application/json" \
  -d '{"scan":["AIS","ADS","FLARM"]}'
```

Examples:

- AIS only: `{"scan":["AIS"]}`
- ADS-B only: `{"scan":["ADS"]}`
- FLARM/OGN only: `{"scan":["FLARM"]}`
- Hybrid without FLARM: `{"scan":["AIS","ADS"]}`

## Nginx reverse proxy on `/radar/`

The frontend uses relative UI/API paths, so it works both:

- directly on `http://localhost:<port>/`
- behind an nginx path prefix like `/radar/`

Recommended nginx location block:

```nginx
location = /radar {
    return 301 /radar/;
}

location /radar/ {
    proxy_pass http://127.0.0.1:8000/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

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
