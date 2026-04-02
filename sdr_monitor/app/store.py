"""SQLite persistence layer for observations and latest target state."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from app.models import (
    Freshness,
    HistoricalTargetSummary,
    NormalizedObservation,
    ScanBand,
    Source,
    Target,
    TargetKind,
)


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

                CREATE TABLE IF NOT EXISTS target_names (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL
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
            self._upsert_identifier_name_from_observation(conn, observation, target)

    def insert_observation(self, observation: NormalizedObservation) -> None:
        """Persist one normalized observation row."""

        with self._lock, self._connect() as conn:
            self._insert_observation(conn, observation)

    def upsert_latest_target(self, target: Target) -> None:
        """Persist latest-known state for a target."""

        with self._lock, self._connect() as conn:
            self._upsert_target(conn, target)
            self._upsert_identifier_name_from_target(conn, target)

    def delete_latest_targets_older_than(self, cutoff: datetime) -> int:
        """Delete targets_latest rows with last_seen older than cutoff."""

        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM targets_latest
                WHERE last_seen < ?
                """,
                (_to_iso(cutoff),),
            )
        return int(cursor.rowcount if cursor.rowcount is not None else 0)

    def populate_target_names_from_observations(self, limit: int | None = None) -> dict[str, int]:
        """Backfill `target_names` from historical `observations.payload_json`."""

        query = """
            SELECT source, target_id, observed_at, payload_json
            FROM observations
            ORDER BY observed_at ASC
        """
        params: tuple[object, ...] = ()
        if limit is not None:
            if limit <= 0:
                raise ValueError("limit must be > 0")
            query += " LIMIT ?"
            params = (limit,)

        scanned = 0
        upserted = 0
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS target_names (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL
                )
                """
            )
            rows = conn.execute(query, params).fetchall()
            for row in rows:
                scanned += 1
                payload = _parse_payload_json(row["payload_json"])
                identifier, name = _extract_identifier_name_from_observation(
                    source=row["source"],
                    target_id=row["target_id"],
                    payload=payload,
                )
                if identifier is None or name is None:
                    continue
                self._upsert_identifier_name(conn, identifier=identifier, name=name)
                upserted += 1
        return {"observations_scanned": scanned, "names_upserted": upserted}

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

    def list_historical_targets(self, limit: int | None = None) -> list[HistoricalTargetSummary]:
        """List tracked objects that have persisted historical observations."""

        query = """
            SELECT
                observations.target_id AS target_id,
                observations.source AS source,
                observations.kind AS kind,
                COUNT(*) AS position_count,
                MAX(observations.observed_at) AS last_seen,
                COALESCE(target_names.name, targets_latest.label) AS resolved_label
            FROM observations
            LEFT JOIN targets_latest
                ON targets_latest.target_id = observations.target_id
            LEFT JOIN target_names
                ON target_names.id = (
                    CASE
                        WHEN observations.source = 'ais'
                            THEN COALESCE(
                                targets_latest.mmsi,
                                substr(observations.target_id, instr(observations.target_id, ':') + 1)
                            )
                        WHEN observations.source = 'adsb'
                            THEN lower(COALESCE(
                                targets_latest.icao24,
                                substr(observations.target_id, instr(observations.target_id, ':') + 1)
                            ))
                        ELSE substr(observations.target_id, instr(observations.target_id, ':') + 1)
                    END
                )
            GROUP BY
                observations.target_id,
                observations.source,
                observations.kind,
                target_names.name,
                targets_latest.label
            ORDER BY MAX(observations.observed_at) DESC
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
            HistoricalTargetSummary(
                target_id=row["target_id"],
                source=Source(row["source"]),
                kind=TargetKind(row["kind"]),
                label=row["resolved_label"],
                last_seen=_parse_dt(row["last_seen"]),
                position_count=int(row["position_count"]),
            )
            for row in rows
        ]

    def list_historical_target_ids_in_view(
        self,
        *,
        center_lat: float,
        center_lon: float,
        range_km: float,
    ) -> list[str]:
        """List target ids that have at least one historical position inside the active radar view."""

        if range_km <= 0:
            raise ValueError("range_km must be > 0")

        lat_padding = range_km / _KM_PER_DEG_LAT
        lon_padding = range_km / _km_per_deg_lon(center_lat)

        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT target_id, lat, lon
                FROM observations
                WHERE lat IS NOT NULL
                  AND lon IS NOT NULL
                  AND lat BETWEEN ? AND ?
                  AND lon BETWEEN ? AND ?
                """,
                (
                    center_lat - lat_padding,
                    center_lat + lat_padding,
                    center_lon - lon_padding,
                    center_lon + lon_padding,
                ),
            ).fetchall()

        matched: set[str] = set()
        km_lon = _km_per_deg_lon(center_lat)
        max_distance_sq = range_km * range_km
        for row in rows:
            dy = (float(row["lat"]) - center_lat) * _KM_PER_DEG_LAT
            dx = (float(row["lon"]) - center_lon) * km_lon
            if (dx * dx) + (dy * dy) <= max_distance_sq:
                matched.add(str(row["target_id"]))

        return sorted(matched)

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
                last_altitude,
                target_names.name AS resolved_name
            FROM targets_latest
            LEFT JOIN target_names
                ON target_names.id = (
                    CASE
                        WHEN targets_latest.source = 'ais' THEN targets_latest.mmsi
                        WHEN targets_latest.source = 'adsb' THEN lower(targets_latest.icao24)
                        ELSE COALESCE(targets_latest.mmsi, lower(targets_latest.icao24))
                    END
                )
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
                label=row["resolved_name"] or row["label"],
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

    def _upsert_identifier_name_from_observation(
        self,
        conn: sqlite3.Connection,
        observation: NormalizedObservation,
        target: Target,
    ) -> None:
        if observation.source == Source.ADSB:
            identifier = _normalize_identifier(observation.icao24 or target.icao24, Source.ADSB)
            name = _normalize_name(observation.callsign or target.callsign)
        elif observation.source == Source.AIS:
            identifier = _normalize_identifier(observation.mmsi or target.mmsi, Source.AIS)
            name = _normalize_name(observation.shipname or target.shipname)
        else:
            return

        if identifier is None or name is None:
            return

        self._upsert_identifier_name(conn, identifier=identifier, name=name)

    def _upsert_identifier_name_from_target(self, conn: sqlite3.Connection, target: Target) -> None:
        if target.source == Source.ADSB:
            identifier = _normalize_identifier(target.icao24, Source.ADSB)
            name = _normalize_name(target.callsign)
        elif target.source == Source.AIS:
            identifier = _normalize_identifier(target.mmsi, Source.AIS)
            name = _normalize_name(target.shipname)
        else:
            return

        if identifier is None or name is None:
            return

        self._upsert_identifier_name(conn, identifier=identifier, name=name)

    def _upsert_identifier_name(self, conn: sqlite3.Connection, *, identifier: str, name: str) -> None:
        conn.execute(
            """
            INSERT INTO target_names (id, name)
            VALUES (?, ?)
            ON CONFLICT(id)
            DO UPDATE SET name = excluded.name
            """,
            (identifier, name),
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


_KM_PER_DEG_LAT = 110.574


def _km_per_deg_lon(lat: float) -> float:
    cosine = math.cos((lat * math.pi) / 180)
    return max(111.320 * abs(cosine), 0.000001)


def _normalize_identifier(value: str | None, source: Source) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if source == Source.ADSB:
        return normalized.lower()
    return normalized


def _normalize_name(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized


def _extract_identifier_name_from_observation(
    *,
    source: str,
    target_id: str,
    payload: dict[str, object],
) -> tuple[str | None, str | None]:
    if source == Source.ADSB.value:
        identifier = _normalize_identifier(
            _clean_payload_text(payload.get("hex"))
            or _clean_payload_text(payload.get("icao24"))
            or _identifier_from_target_id(target_id, Source.ADSB),
            Source.ADSB,
        )
        name = _normalize_name(
            _clean_payload_text(payload.get("flight"))
            or _clean_payload_text(payload.get("callsign"))
        )
        return identifier, name

    if source == Source.AIS.value:
        decoded = payload.get("decoded")
        decoded_map = decoded if isinstance(decoded, dict) else {}
        identifier = _normalize_identifier(
            _clean_payload_text(decoded_map.get("mmsi"))
            or _clean_payload_text(payload.get("mmsi"))
            or _identifier_from_target_id(target_id, Source.AIS),
            Source.AIS,
        )
        name = _normalize_name(
            _clean_payload_text(decoded_map.get("shipname"))
            or _clean_payload_text(payload.get("shipname"))
        )
        return identifier, name

    return None, None


def _identifier_from_target_id(target_id: str, source: Source) -> str | None:
    prefix = f"{source.value}:"
    if not target_id.startswith(prefix):
        return None
    suffix = target_id[len(prefix) :].strip()
    return suffix or None


def _parse_payload_json(value: str) -> dict[str, object]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if isinstance(payload, dict):
        return payload
    return {}


def _clean_payload_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
