"""SQLite persistence layer for observations and latest target state."""

from __future__ import annotations

from dataclasses import dataclass
import json
import hashlib
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Sequence

from app.models import (
    Freshness,
    HistoricalTargetSummary,
    NormalizedObservation,
    ScanBand,
    Source,
    Target,
    TargetKind,
)


@dataclass(frozen=True, slots=True)
class HydroBBoxDownloadState:
    bbox_key: str
    is_complete: bool
    resume_collection: str | None
    resume_url: str | None
    feature_count: int


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

                CREATE TABLE IF NOT EXISTS map_hydro_features (
                    feature_id TEXT PRIMARY KEY,
                    inspire_id TEXT,
                    collection TEXT NOT NULL,
                    properties_json TEXT NOT NULL,
                    geometry_json TEXT NOT NULL,
                    feature_min_lon REAL,
                    feature_min_lat REAL,
                    feature_max_lon REAL,
                    feature_max_lat REAL,
                    updated_at TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_map_hydro_features_inspire_id
                ON map_hydro_features(inspire_id);

                CREATE TABLE IF NOT EXISTS map_hydro_bbox_cache (
                    bbox_key TEXT PRIMARY KEY,
                    min_lon REAL NOT NULL,
                    min_lat REAL NOT NULL,
                    max_lon REAL NOT NULL,
                    max_lat REAL NOT NULL,
                    is_complete INTEGER NOT NULL DEFAULT 1,
                    resume_collection TEXT,
                    resume_url TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS map_hydro_bbox_features (
                    bbox_key TEXT NOT NULL,
                    feature_order INTEGER NOT NULL,
                    feature_id TEXT NOT NULL,
                    PRIMARY KEY (bbox_key, feature_order)
                );

                CREATE INDEX IF NOT EXISTS idx_map_hydro_bbox_features_feature_id
                ON map_hydro_bbox_features(feature_id);
                """
            )
            self._ensure_column(
                conn,
                table_name="map_hydro_bbox_cache",
                column_name="is_complete",
                definition="INTEGER NOT NULL DEFAULT 1",
            )
            self._ensure_column(
                conn,
                table_name="map_hydro_bbox_cache",
                column_name="resume_collection",
                definition="TEXT",
            )
            self._ensure_column(
                conn,
                table_name="map_hydro_bbox_cache",
                column_name="resume_url",
                definition="TEXT",
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

    def fetch_history(
        self,
        target_id: str,
        limit: int = 100,
        *,
        observed_after: datetime | None = None,
        observed_before: datetime | None = None,
    ) -> list[NormalizedObservation]:
        """Fetch recent historical observations for one target id."""

        if limit <= 0:
            raise ValueError("limit must be > 0")

        params: list[object] = [target_id]
        query = """
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
        """
        if observed_after is not None:
            query += "\n                AND observed_at >= ?"
            params.append(_to_iso(observed_after))
        if observed_before is not None:
            query += "\n                AND observed_at <= ?"
            params.append(_to_iso(observed_before))
        query += "\n                ORDER BY observed_at DESC\n                LIMIT ?"
        params.append(limit)

        with self._lock, self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()

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

    def list_historical_targets(
        self,
        limit: int | None = None,
        *,
        observed_after: datetime | None = None,
        observed_before: datetime | None = None,
    ) -> list[HistoricalTargetSummary]:
        """List tracked objects that have persisted historical observations."""

        query = """
            SELECT
                observations.target_id AS target_id,
                observations.source AS source,
                observations.kind AS kind,
                COUNT(*) AS position_count,
                MAX(observations.observed_at) AS last_seen,
                MAX(observations.speed) AS max_observed_speed,
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
        """
        params: list[object] = []
        if observed_after is not None:
            query += "\n            WHERE observations.observed_at >= ?"
            params.append(_to_iso(observed_after))
        if observed_before is not None:
            query += "\n            AND observations.observed_at <= ?" if observed_after is not None else "\n            WHERE observations.observed_at <= ?"
            params.append(_to_iso(observed_before))
        query += """
            GROUP BY
                observations.target_id,
                observations.source,
                observations.kind,
                target_names.name,
                targets_latest.label
            ORDER BY MAX(observations.observed_at) DESC
        """
        if limit is not None:
            if limit <= 0:
                raise ValueError("limit must be > 0")
            query += " LIMIT ?"
            params.append(limit)

        with self._lock, self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()

        return [
            HistoricalTargetSummary(
                target_id=row["target_id"],
                source=Source(row["source"]),
                kind=TargetKind(row["kind"]),
                label=row["resolved_label"],
                last_seen=_parse_dt(row["last_seen"]),
                position_count=int(row["position_count"]),
                max_observed_speed=(
                    float(row["max_observed_speed"])
                    if row["max_observed_speed"] is not None
                    else None
                ),
            )
            for row in rows
        ]

    def list_historical_target_ids_in_view(
        self,
        *,
        center_lat: float,
        center_lon: float,
        range_km: float,
        observed_after: datetime | None = None,
        observed_before: datetime | None = None,
    ) -> list[str]:
        """List target ids that have at least one historical position inside the active radar view."""

        if range_km <= 0:
            raise ValueError("range_km must be > 0")

        lat_padding = range_km / _KM_PER_DEG_LAT
        lon_padding = range_km / _km_per_deg_lon(center_lat)

        params: list[object] = [
            center_lat - lat_padding,
            center_lat + lat_padding,
            center_lon - lon_padding,
            center_lon + lon_padding,
        ]
        query = """
                SELECT target_id, lat, lon
                FROM observations
                WHERE lat IS NOT NULL
                  AND lon IS NOT NULL
                  AND lat BETWEEN ? AND ?
                  AND lon BETWEEN ? AND ?
        """
        if observed_after is not None:
            query += "\n                  AND observed_at >= ?"
            params.append(_to_iso(observed_after))
        if observed_before is not None:
            query += "\n                  AND observed_at <= ?"
            params.append(_to_iso(observed_before))

        with self._lock, self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()

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

    def load_hydro_contours_by_bbox(
        self,
        *,
        bbox: Sequence[float],
    ) -> tuple[dict[str, Any], ...] | None:
        """Return cached hydro contour features for one bbox, or None on cache miss."""

        bbox_key = _hydro_bbox_key(bbox)
        with self._lock, self._connect() as conn:
            bbox_row = conn.execute(
                """
                SELECT bbox_key, is_complete
                FROM map_hydro_bbox_cache
                WHERE bbox_key = ?
                """,
                (bbox_key,),
            ).fetchone()
            if bbox_row is None:
                return None
            if not bool(bbox_row["is_complete"]):
                return None

            rows = conn.execute(
                """
                SELECT
                    map_hydro_features.feature_id,
                    map_hydro_features.inspire_id,
                    map_hydro_features.collection,
                    map_hydro_features.properties_json,
                    map_hydro_features.geometry_json
                FROM map_hydro_bbox_features
                LEFT JOIN map_hydro_features
                    ON map_hydro_features.feature_id = map_hydro_bbox_features.feature_id
                WHERE map_hydro_bbox_features.bbox_key = ?
                ORDER BY map_hydro_bbox_features.feature_order ASC
                """,
                (bbox_key,),
            ).fetchall()

        features: list[dict[str, Any]] = []
        for row in rows:
            if row["feature_id"] is None:
                return None
            features.append(_deserialize_hydro_feature_row(row))
        return tuple(features)

    def load_hydro_partial_contours_by_bbox(
        self,
        *,
        bbox: Sequence[float],
    ) -> tuple[dict[str, Any], ...] | None:
        """Return stored hydro contour features for one bbox, even if it is still incomplete."""

        bbox_key = _hydro_bbox_key(bbox)
        with self._lock, self._connect() as conn:
            bbox_row = conn.execute(
                """
                SELECT bbox_key
                FROM map_hydro_bbox_cache
                WHERE bbox_key = ?
                """,
                (bbox_key,),
            ).fetchone()
            if bbox_row is None:
                return None

            rows = conn.execute(
                """
                SELECT
                    map_hydro_features.feature_id,
                    map_hydro_features.inspire_id,
                    map_hydro_features.collection,
                    map_hydro_features.properties_json,
                    map_hydro_features.geometry_json
                FROM map_hydro_bbox_features
                LEFT JOIN map_hydro_features
                    ON map_hydro_features.feature_id = map_hydro_bbox_features.feature_id
                WHERE map_hydro_bbox_features.bbox_key = ?
                ORDER BY map_hydro_bbox_features.feature_order ASC
                """,
                (bbox_key,),
            ).fetchall()

        features: list[dict[str, Any]] = []
        for row in rows:
            if row["feature_id"] is None:
                return None
            features.append(_deserialize_hydro_feature_row(row))
        return tuple(features)

    def load_hydro_bbox_download_state(
        self,
        *,
        bbox: Sequence[float],
    ) -> HydroBBoxDownloadState | None:
        """Return cached hydro bbox download state, including resume pointers."""

        bbox_key = _hydro_bbox_key(bbox)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    map_hydro_bbox_cache.bbox_key,
                    map_hydro_bbox_cache.is_complete,
                    map_hydro_bbox_cache.resume_collection,
                    map_hydro_bbox_cache.resume_url,
                    COUNT(map_hydro_bbox_features.feature_id) AS feature_count
                FROM map_hydro_bbox_cache
                LEFT JOIN map_hydro_bbox_features
                    ON map_hydro_bbox_features.bbox_key = map_hydro_bbox_cache.bbox_key
                WHERE map_hydro_bbox_cache.bbox_key = ?
                GROUP BY
                    map_hydro_bbox_cache.bbox_key,
                    map_hydro_bbox_cache.is_complete,
                    map_hydro_bbox_cache.resume_collection,
                    map_hydro_bbox_cache.resume_url
                """,
                (bbox_key,),
            ).fetchone()

        if row is None:
            return None
        return HydroBBoxDownloadState(
            bbox_key=str(row["bbox_key"]),
            is_complete=bool(row["is_complete"]),
            resume_collection=(
                str(row["resume_collection"]) if row["resume_collection"] is not None else None
            ),
            resume_url=str(row["resume_url"]) if row["resume_url"] is not None else None,
            feature_count=int(row["feature_count"]),
        )

    def begin_hydro_bbox_download(
        self,
        *,
        bbox: Sequence[float],
        resume_collection: str,
        resume_url: str,
        reset: bool = False,
    ) -> None:
        """Mark a hydro bbox as incomplete and record where paging should resume."""

        normalized_collection = resume_collection.strip()
        normalized_url = resume_url.strip()
        if not normalized_collection:
            raise ValueError("resume_collection must not be empty.")
        if not normalized_url:
            raise ValueError("resume_url must not be empty.")

        bbox_key = _hydro_bbox_key(bbox)
        min_lon, min_lat, max_lon, max_lat = [float(value) for value in bbox]
        now = _to_iso(datetime.now(timezone.utc))

        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO map_hydro_bbox_cache (
                    bbox_key,
                    min_lon,
                    min_lat,
                    max_lon,
                    max_lat,
                    is_complete,
                    resume_collection,
                    resume_url,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                ON CONFLICT(bbox_key)
                DO UPDATE SET
                    min_lon = excluded.min_lon,
                    min_lat = excluded.min_lat,
                    max_lon = excluded.max_lon,
                    max_lat = excluded.max_lat,
                    is_complete = excluded.is_complete,
                    resume_collection = excluded.resume_collection,
                    resume_url = excluded.resume_url,
                    updated_at = excluded.updated_at
                """,
                (
                    bbox_key,
                    min_lon,
                    min_lat,
                    max_lon,
                    max_lat,
                    normalized_collection,
                    normalized_url,
                    now,
                    now,
                ),
            )
            if reset:
                conn.execute(
                    """
                    DELETE FROM map_hydro_bbox_features
                    WHERE bbox_key = ?
                    """,
                    (bbox_key,),
                )

    def append_hydro_contour_page(
        self,
        *,
        bbox: Sequence[float],
        features: Sequence[dict[str, Any]],
        next_collection: str | None,
        next_url: str | None,
        is_complete: bool,
    ) -> None:
        """Append one page of hydro features and update bbox resume status."""

        if is_complete and (next_collection is not None or next_url is not None):
            raise ValueError("Completed bbox downloads must not keep resume pointers.")
        if not is_complete and next_collection is None:
            raise ValueError("Incomplete bbox downloads require a resume collection.")

        bbox_key = _hydro_bbox_key(bbox)
        min_lon, min_lat, max_lon, max_lat = [float(value) for value in bbox]
        now = _to_iso(datetime.now(timezone.utc))

        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(MAX(feature_order), -1) AS max_feature_order
                FROM map_hydro_bbox_features
                WHERE bbox_key = ?
                """,
                (bbox_key,),
            ).fetchone()
            next_feature_order = int(row["max_feature_order"]) + 1

            conn.execute(
                """
                INSERT INTO map_hydro_bbox_cache (
                    bbox_key,
                    min_lon,
                    min_lat,
                    max_lon,
                    max_lat,
                    is_complete,
                    resume_collection,
                    resume_url,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bbox_key)
                DO UPDATE SET
                    min_lon = excluded.min_lon,
                    min_lat = excluded.min_lat,
                    max_lon = excluded.max_lon,
                    max_lat = excluded.max_lat,
                    is_complete = excluded.is_complete,
                    resume_collection = excluded.resume_collection,
                    resume_url = excluded.resume_url,
                    updated_at = excluded.updated_at
                """,
                (
                    bbox_key,
                    min_lon,
                    min_lat,
                    max_lon,
                    max_lat,
                    1 if is_complete else 0,
                    next_collection,
                    next_url,
                    now,
                    now,
                ),
            )

            for feature_order, feature in enumerate(features, start=next_feature_order):
                normalized = _normalize_hydro_feature(feature)
                conn.execute(
                    """
                    INSERT INTO map_hydro_features (
                        feature_id,
                        inspire_id,
                        collection,
                        properties_json,
                        geometry_json,
                        feature_min_lon,
                        feature_min_lat,
                        feature_max_lon,
                        feature_max_lat,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(feature_id)
                    DO UPDATE SET
                        inspire_id = excluded.inspire_id,
                        collection = excluded.collection,
                        properties_json = excluded.properties_json,
                        geometry_json = excluded.geometry_json,
                        feature_min_lon = excluded.feature_min_lon,
                        feature_min_lat = excluded.feature_min_lat,
                        feature_max_lon = excluded.feature_max_lon,
                        feature_max_lat = excluded.feature_max_lat,
                        updated_at = excluded.updated_at
                    """,
                    (
                        normalized["feature_id"],
                        normalized["inspire_id"],
                        normalized["collection"],
                        normalized["properties_json"],
                        normalized["geometry_json"],
                        normalized["feature_min_lon"],
                        normalized["feature_min_lat"],
                        normalized["feature_max_lon"],
                        normalized["feature_max_lat"],
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO map_hydro_bbox_features (
                        bbox_key,
                        feature_order,
                        feature_id
                    )
                    VALUES (?, ?, ?)
                    """,
                    (bbox_key, feature_order, normalized["feature_id"]),
                )

    def save_hydro_contours_for_bbox(
        self,
        *,
        bbox: Sequence[float],
        features: Sequence[dict[str, Any]],
    ) -> None:
        """Upsert hydro contour features and map one bbox to their identifiers."""

        bbox_key = _hydro_bbox_key(bbox)
        min_lon, min_lat, max_lon, max_lat = [float(value) for value in bbox]
        now = _to_iso(datetime.now(timezone.utc))

        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO map_hydro_bbox_cache (
                    bbox_key,
                    min_lon,
                    min_lat,
                    max_lon,
                    max_lat,
                    is_complete,
                    resume_collection,
                    resume_url,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, 1, NULL, NULL, ?, ?)
                ON CONFLICT(bbox_key)
                DO UPDATE SET
                    min_lon = excluded.min_lon,
                    min_lat = excluded.min_lat,
                    max_lon = excluded.max_lon,
                    max_lat = excluded.max_lat,
                    is_complete = excluded.is_complete,
                    resume_collection = excluded.resume_collection,
                    resume_url = excluded.resume_url,
                    updated_at = excluded.updated_at
                """,
                (bbox_key, min_lon, min_lat, max_lon, max_lat, now, now),
            )
            conn.execute(
                """
                DELETE FROM map_hydro_bbox_features
                WHERE bbox_key = ?
                """,
                (bbox_key,),
            )

            for feature_order, feature in enumerate(features):
                normalized = _normalize_hydro_feature(feature)
                conn.execute(
                    """
                    INSERT INTO map_hydro_features (
                        feature_id,
                        inspire_id,
                        collection,
                        properties_json,
                        geometry_json,
                        feature_min_lon,
                        feature_min_lat,
                        feature_max_lon,
                        feature_max_lat,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(feature_id)
                    DO UPDATE SET
                        inspire_id = excluded.inspire_id,
                        collection = excluded.collection,
                        properties_json = excluded.properties_json,
                        geometry_json = excluded.geometry_json,
                        feature_min_lon = excluded.feature_min_lon,
                        feature_min_lat = excluded.feature_min_lat,
                        feature_max_lon = excluded.feature_max_lon,
                        feature_max_lat = excluded.feature_max_lat,
                        updated_at = excluded.updated_at
                    """,
                    (
                        normalized["feature_id"],
                        normalized["inspire_id"],
                        normalized["collection"],
                        normalized["properties_json"],
                        normalized["geometry_json"],
                        normalized["feature_min_lon"],
                        normalized["feature_min_lat"],
                        normalized["feature_max_lon"],
                        normalized["feature_max_lat"],
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO map_hydro_bbox_features (
                        bbox_key,
                        feature_order,
                        feature_id
                    )
                    VALUES (?, ?, ?)
                    """,
                    (bbox_key, feature_order, normalized["feature_id"]),
                )

    def load_hydro_feature_by_inspire_id(self, inspire_id: str) -> dict[str, Any] | None:
        """Return one stored hydro feature by inspireId."""

        normalized_inspire_id = inspire_id.strip()
        if not normalized_inspire_id:
            raise ValueError("inspire_id must not be empty.")

        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    feature_id,
                    inspire_id,
                    collection,
                    properties_json,
                    geometry_json
                FROM map_hydro_features
                WHERE inspire_id = ?
                """,
                (normalized_inspire_id,),
            ).fetchone()

        if row is None:
            return None
        return _deserialize_hydro_feature_row(row)

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

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        *,
        table_name: str,
        column_name: str,
        definition: str,
    ) -> None:
        columns = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if column_name in columns:
            return
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")

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


def _hydro_bbox_key(bbox: Sequence[float]) -> str:
    if len(bbox) != 4:
        raise ValueError("bbox must contain exactly four coordinates.")
    return ",".join(f"{float(value):.6f}" for value in bbox)


def _normalize_hydro_feature(feature: dict[str, Any]) -> dict[str, Any]:
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict):
        raise ValueError("Hydro contour feature is missing a geometry object.")

    properties = feature.get("properties")
    if not isinstance(properties, dict):
        properties = {}

    inspire_id = properties.get("inspireId")
    normalized_inspire_id = None
    if isinstance(inspire_id, str):
        normalized_inspire_id = inspire_id.strip() or None

    geometry_json = json.dumps(geometry, ensure_ascii=False, sort_keys=True)
    properties_json = json.dumps(properties, ensure_ascii=False, sort_keys=True)
    feature_id = normalized_inspire_id or (
        "anon:" + hashlib.sha256(f"{geometry_json}|{properties_json}".encode("utf-8")).hexdigest()
    )
    collection = str(properties.get("collection") or "unknown")
    feature_bounds = _hydro_geometry_bounds(geometry)

    return {
        "feature_id": feature_id,
        "inspire_id": normalized_inspire_id,
        "collection": collection,
        "properties_json": properties_json,
        "geometry_json": geometry_json,
        "feature_min_lon": feature_bounds[0],
        "feature_min_lat": feature_bounds[1],
        "feature_max_lon": feature_bounds[2],
        "feature_max_lat": feature_bounds[3],
    }


def _deserialize_hydro_feature_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": json.loads(row["properties_json"]),
        "geometry": json.loads(row["geometry_json"]),
    }


def _hydro_geometry_bounds(
    geometry: dict[str, Any],
) -> tuple[float | None, float | None, float | None, float | None]:
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    points: list[tuple[float, float]] = []

    if geometry_type == "LineString" and isinstance(coordinates, list):
        points = _flatten_coordinate_pairs(coordinates)
    elif geometry_type == "MultiLineString" and isinstance(coordinates, list):
        for line in coordinates:
            if isinstance(line, list):
                points.extend(_flatten_coordinate_pairs(line))

    if not points:
        return (None, None, None, None)

    longitudes = [point[0] for point in points]
    latitudes = [point[1] for point in points]
    return (min(longitudes), min(latitudes), max(longitudes), max(latitudes))


def _flatten_coordinate_pairs(raw_pairs: list[Any]) -> list[tuple[float, float]]:
    pairs: list[tuple[float, float]] = []
    for pair in raw_pairs:
        if (
            isinstance(pair, list)
            and len(pair) >= 2
            and isinstance(pair[0], (int, float))
            and isinstance(pair[1], (int, float))
        ):
            pairs.append((float(pair[0]), float(pair[1])))
    return pairs


def _clean_payload_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
