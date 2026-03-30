"""SQLite persistence layer for observations and latest target state."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from app.models import Freshness, NormalizedObservation, ScanBand, Source, Target, TargetKind


class SQLiteStore:
    """Persistence adapter backed by SQLite."""

    def __init__(self, sqlite_path: str | Path) -> None:
        self._path = Path(sqlite_path)
        self._lock = RLock()

    @property
    def sqlite_path(self) -> Path:
        return self._path

    def initialize(self) -> None:
        """Create database directories and schema if needed."""

        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    lat REAL,
                    lon REAL,
                    course REAL,
                    speed REAL,
                    altitude REAL,
                    payload_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_observations_target_seen
                ON observations(target_id, observed_at DESC);

                CREATE TABLE IF NOT EXISTS targets_latest (
                    target_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    label TEXT,
                    icao24 TEXT,
                    mmsi TEXT,
                    callsign TEXT,
                    shipname TEXT,
                    last_seen TEXT NOT NULL,
                    last_lat REAL,
                    last_lon REAL,
                    last_course REAL,
                    last_speed REAL,
                    last_altitude REAL
                );
                """
            )

    def persist_observation_and_target(
        self, observation: NormalizedObservation, target: Target
    ) -> None:
        """Store one observation and upsert its latest target in one transaction."""

        with self._lock, self._connect() as conn:
            self._insert_observation(conn, observation)
            self._upsert_target(conn, target)

    def insert_observation(self, observation: NormalizedObservation) -> None:
        """Persist one normalized observation row."""

        with self._lock, self._connect() as conn:
            self._insert_observation(conn, observation)

    def upsert_latest_target(self, target: Target) -> None:
        """Persist latest-known state for a target."""

        with self._lock, self._connect() as conn:
            self._upsert_target(conn, target)

    def fetch_history(self, target_id: str, limit: int = 100) -> list[NormalizedObservation]:
        """Fetch recent historical observations for one target id."""

        if limit <= 0:
            raise ValueError("limit must be > 0")

        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    target_id,
                    source,
                    kind,
                    observed_at,
                    lat,
                    lon,
                    course,
                    speed,
                    altitude,
                    payload_json
                FROM observations
                WHERE target_id = ?
                ORDER BY observed_at DESC
                LIMIT ?
                """,
                (target_id, limit),
            ).fetchall()

        observations: list[NormalizedObservation] = []
        for row in rows:
            observations.append(
                NormalizedObservation(
                    target_id=row["target_id"],
                    source=Source(row["source"]),
                    kind=TargetKind(row["kind"]),
                    observed_at=_parse_dt(row["observed_at"]),
                    lat=row["lat"],
                    lon=row["lon"],
                    course=row["course"],
                    speed=row["speed"],
                    altitude=row["altitude"],
                    payload_json=json.loads(row["payload_json"]),
                )
            )
        return observations

    def load_latest_targets(self, limit: int | None = None) -> list[Target]:
        """Load latest target states from persistence for optional warm start."""

        query = """
            SELECT
                target_id,
                source,
                kind,
                label,
                icao24,
                mmsi,
                callsign,
                shipname,
                last_seen,
                last_lat,
                last_lon,
                last_course,
                last_speed,
                last_altitude
            FROM targets_latest
            ORDER BY last_seen DESC
        """
        params: tuple[object, ...] = ()
        if limit is not None:
            if limit <= 0:
                raise ValueError("limit must be > 0")
            query += " LIMIT ?"
            params = (limit,)

        with self._lock, self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        return [
            Target(
                target_id=row["target_id"],
                source=Source(row["source"]),
                kind=TargetKind(row["kind"]),
                label=row["label"],
                lat=row["last_lat"],
                lon=row["last_lon"],
                course=row["last_course"],
                speed=row["last_speed"],
                altitude=row["last_altitude"],
                first_seen=_parse_dt(row["last_seen"]),
                last_seen=_parse_dt(row["last_seen"]),
                freshness=Freshness.STALE,
                last_scan_band=self._infer_band(row["source"]),
                icao24=row["icao24"],
                mmsi=row["mmsi"],
                callsign=row["callsign"],
                shipname=row["shipname"],
            )
            for row in rows
        ]

    def count_observations(self) -> int:
        """Return total number of observation rows."""

        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM observations").fetchone()
        return int(row["count"])

    def _insert_observation(
        self, conn: sqlite3.Connection, observation: NormalizedObservation
    ) -> None:
        conn.execute(
            """
            INSERT INTO observations (
                target_id, source, kind, observed_at, lat, lon, course, speed, altitude, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation.target_id,
                observation.source.value,
                observation.kind.value,
                _to_iso(observation.observed_at),
                observation.lat,
                observation.lon,
                observation.course,
                observation.speed,
                observation.altitude,
                json.dumps(observation.payload_json),
            ),
        )

    def _upsert_target(self, conn: sqlite3.Connection, target: Target) -> None:
        conn.execute(
            """
            INSERT INTO targets_latest (
                target_id,
                source,
                kind,
                label,
                icao24,
                mmsi,
                callsign,
                shipname,
                last_seen,
                last_lat,
                last_lon,
                last_course,
                last_speed,
                last_altitude
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(target_id)
            DO UPDATE SET
                source = excluded.source,
                kind = excluded.kind,
                label = excluded.label,
                icao24 = excluded.icao24,
                mmsi = excluded.mmsi,
                callsign = excluded.callsign,
                shipname = excluded.shipname,
                last_seen = excluded.last_seen,
                last_lat = excluded.last_lat,
                last_lon = excluded.last_lon,
                last_course = excluded.last_course,
                last_speed = excluded.last_speed,
                last_altitude = excluded.last_altitude
            """,
            (
                target.target_id,
                target.source.value,
                target.kind.value,
                target.label,
                target.icao24,
                target.mmsi,
                target.callsign,
                target.shipname,
                _to_iso(target.last_seen),
                target.lat,
                target.lon,
                target.course,
                target.speed,
                target.altitude,
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _infer_band(self, source: str) -> ScanBand:
        return ScanBand.ADSB if source == Source.ADSB.value else ScanBand.AIS


def _to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed
