"""Shared contour providers for radar background map layers."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field, replace
import hashlib
import json
import math
from pathlib import Path
import time
from threading import Lock, Thread
from typing import Any, Callable, Protocol
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

from app.config import Config
from app.logging_setup import get_logger
from app.store import SQLiteStore

VALID_MAP_SOURCES = ("hydro", "elevation")
GeoJSONFeature = dict[str, Any]
BBox = tuple[float, float, float, float]
LOGGER = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class MapContourRequest:
    source: str
    bbox: BBox
    range_km: float | None = None


@dataclass(frozen=True, slots=True)
class MapContourResult:
    source: str
    features: tuple[GeoJSONFeature, ...]
    status: str = "ok"
    error: str | None = None
    cache_hit: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_payload(self, *, bbox: BBox, range_km: float | None = None) -> dict[str, Any]:
        return {
            "type": "FeatureCollection",
            "features": list(self.features),
            "source": self.source,
            "status": self.status,
            "error": self.error,
            "cache_hit": self.cache_hit,
            "bbox": list(bbox),
            "range_km": range_km,
            "details": self.details,
        }


@dataclass(frozen=True, slots=True)
class HydroContourPage:
    collection: str
    request_url: str
    next_url: str | None
    features: tuple[GeoJSONFeature, ...]


class MapContourProvider(Protocol):
    def fetch(self, request: MapContourRequest) -> MapContourResult:
        """Return contours for the requested bbox."""


@dataclass(slots=True)
class _CacheEntry:
    expires_at: float
    result: MapContourResult


@dataclass(slots=True)
class _BackgroundHydroTask:
    thread: Thread
    last_result: MapContourResult | None = None


class CachingMapContourProvider:
    """Thin TTL cache around an underlying provider."""

    def __init__(
        self,
        provider: MapContourProvider,
        *,
        ttl_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._provider = provider
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: dict[tuple[str, BBox], _CacheEntry] = {}

    def fetch(self, request: MapContourRequest) -> MapContourResult:
        key = (request.source, _normalize_bbox_key(request.bbox))
        now = self._clock()
        cached = self._entries.get(key)
        if cached is not None and cached.expires_at > now:
            return replace(cached.result, cache_hit=True)

        result = replace(self._provider.fetch(request), cache_hit=False)
        if result.status == "ok":
            self._entries[key] = _CacheEntry(
                expires_at=now + self._ttl_seconds,
                result=result,
            )
        return result


class PersistentMapContourProvider:
    """Persist successful contour responses on disk and reuse them across restarts."""

    def __init__(
        self,
        provider: MapContourProvider,
        *,
        cache_dir: Path,
    ) -> None:
        self._provider = provider
        self._cache_dir = cache_dir

    def fetch(self, request: MapContourRequest) -> MapContourResult:
        cached = self._load(request)
        if cached is not None:
            return replace(cached, cache_hit=True)

        result = replace(self._provider.fetch(request), cache_hit=False)
        if result.status == "ok":
            self._save(request, result)
        return result

    def _load(self, request: MapContourRequest) -> MapContourResult | None:
        cache_path = self._cache_path(request)
        if not cache_path.is_file():
            return None
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        features = payload.get("features")
        details = payload.get("details")
        if not isinstance(features, list):
            return None
        if not isinstance(details, dict):
            details = {}
        source = payload.get("source")
        status = payload.get("status")
        error = payload.get("error")
        if not isinstance(source, str) or not isinstance(status, str):
            return None
        return MapContourResult(
            source=source,
            features=tuple(feature for feature in features if isinstance(feature, dict)),
            status=status,
            error=error if isinstance(error, str) else None,
            details=details,
            cache_hit=False,
        )

    def _save(self, request: MapContourRequest, result: MapContourResult) -> None:
        cache_path = self._cache_path(request)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "source": result.source,
            "status": result.status,
            "error": result.error,
            "features": list(result.features),
            "details": result.details,
            "request": {
                "bbox": list(request.bbox),
                "range_km": request.range_km,
            },
        }
        temp_path = cache_path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temp_path.replace(cache_path)

    def _cache_path(self, request: MapContourRequest) -> Path:
        digest = hashlib.sha256(
            json.dumps(
                {
                    "source": request.source,
                    "bbox": [round(value, 6) for value in request.bbox],
                    "range_km": None if request.range_km is None else round(request.range_km, 3),
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return self._cache_dir / request.source / f"{digest}.json"


class DatabaseHydroContourProvider:
    """Persist hydro contour features in SQLite and reuse them across bbox requests."""

    def __init__(
        self,
        provider: "HydroContourProvider",
        *,
        store: SQLiteStore,
    ) -> None:
        self._provider = provider
        self._store = store

    @property
    def is_available(self) -> bool:
        return self._provider.is_available

    def fetch(self, request: MapContourRequest) -> MapContourResult:
        try:
            cached = self._store.load_hydro_contours_by_bbox(bbox=request.bbox)
        except Exception:
            cached = None

        if cached is not None:
            return MapContourResult(
                source=request.source,
                features=tuple(cached),
                status="ok",
                cache_hit=False,
            )

        if not self._provider.is_available:
            return MapContourResult(
                source=request.source,
                features=(),
                status="unavailable",
                error="Hydrografi credentials are not configured.",
            )

        try:
            resume_collection, resume_url, reset_download = self._resolve_resume_state(
                bbox=request.bbox
            )
            self._store.begin_hydro_bbox_download(
                bbox=request.bbox,
                resume_collection=resume_collection,
                resume_url=resume_url,
                reset=reset_download,
            )
            self._download_to_store(
                bbox=request.bbox,
                resume_collection=resume_collection,
                resume_url=resume_url,
            )
            completed = self._store.load_hydro_contours_by_bbox(bbox=request.bbox)
            if completed is None:
                raise RuntimeError("Hydro contour bbox download finished without a complete cache entry.")
            return MapContourResult(
                source=request.source,
                features=tuple(completed),
                status="ok",
                cache_hit=False,
            )
        except Exception as exc:
            return MapContourResult(
                source=request.source,
                features=(),
                status="error",
                error=f"Hydrografi fetch failed: {exc}",
            )

    def _resolve_resume_state(self, *, bbox: BBox) -> tuple[str, str, bool]:
        state = self._store.load_hydro_bbox_download_state(bbox=bbox)
        if state is None or state.is_complete:
            first_collection = self._provider.COLLECTIONS[0]
            return (first_collection, self._provider.build_items_url(collection=first_collection, bbox=bbox), True)

        resume_collection = state.resume_collection or self._provider.COLLECTIONS[0]
        resume_url = state.resume_url
        if resume_url is None:
            resume_url = self._provider.build_items_url(collection=resume_collection, bbox=bbox)
        LOGGER.info(
            "Resuming incomplete Lantmateriet hydro download: bbox=%s collection=%s url=%s stored_features=%d",
            ",".join(f"{value:.6f}" for value in bbox),
            resume_collection,
            resume_url,
            state.feature_count,
        )
        return (resume_collection, resume_url, False)

    def _download_to_store(
        self,
        *,
        bbox: BBox,
        resume_collection: str,
        resume_url: str,
    ) -> None:
        collection_index = self._provider.COLLECTIONS.index(resume_collection)
        current_resume_url: str | None = resume_url

        for index in range(collection_index, len(self._provider.COLLECTIONS)):
            collection = self._provider.COLLECTIONS[index]
            current_url = current_resume_url or self._provider.build_items_url(collection=collection, bbox=bbox)

            while current_url:
                page = self._provider.fetch_collection_page(
                    collection=collection,
                    bbox=bbox,
                    url=current_url,
                )
                next_collection: str | None = collection
                next_url = page.next_url
                is_complete = False
                if next_url is None:
                    if index + 1 < len(self._provider.COLLECTIONS):
                        next_collection = self._provider.COLLECTIONS[index + 1]
                        next_url = self._provider.build_items_url(
                            collection=next_collection,
                            bbox=bbox,
                        )
                    else:
                        next_collection = None
                        is_complete = True
                self._store.append_hydro_contour_page(
                    bbox=bbox,
                    features=page.features,
                    next_collection=next_collection,
                    next_url=next_url,
                    is_complete=is_complete,
                )
                current_url = page.next_url

            current_resume_url = None


class BackgroundHydroContourProvider:
    """Run hydro downloads in a background thread and report pending state immediately."""

    def __init__(
        self,
        provider: DatabaseHydroContourProvider,
        *,
        store: SQLiteStore,
        poll_hint_seconds: float = 0.75,
    ) -> None:
        self._provider = provider
        self._store = store
        self._poll_hint_seconds = poll_hint_seconds
        self._lock = Lock()
        self._tasks: dict[tuple[str, BBox], _BackgroundHydroTask] = {}

    def fetch(self, request: MapContourRequest) -> MapContourResult:
        key = (request.source, _normalize_bbox_key(request.bbox))
        cached = self._load_complete(request)
        if cached is not None:
            with self._lock:
                self._tasks.pop(key, None)
            return cached

        if not self._provider.is_available:
            return MapContourResult(
                source=request.source,
                features=(),
                status="unavailable",
                error="Hydrografi credentials are not configured.",
            )

        download_state = self._load_download_state(request)
        pending_result = self._build_pending_result(request, download_state)

        with self._lock:
            task = self._tasks.get(key)
            if task is not None and task.thread.is_alive():
                return pending_result
            if task is not None and task.last_result is not None:
                self._tasks.pop(key, None)
                if task.last_result.status != "ok":
                    return replace(task.last_result, cache_hit=False)

            worker = Thread(
                target=self._run_download,
                args=(key, request),
                name=f"map-hydro-download-{abs(hash(key))}",
                daemon=True,
            )
            self._tasks[key] = _BackgroundHydroTask(thread=worker)
            worker.start()

        return pending_result

    def _run_download(self, key: tuple[str, BBox], request: MapContourRequest) -> None:
        try:
            result = replace(self._provider.fetch(request), cache_hit=False)
        except Exception as exc:  # pragma: no cover - defensive safeguard
            result = MapContourResult(
                source=request.source,
                features=(),
                status="error",
                error=f"Hydrografi fetch failed: {exc}",
            )

        with self._lock:
            task = self._tasks.get(key)
            if task is not None:
                task.last_result = result

    def _load_complete(self, request: MapContourRequest) -> MapContourResult | None:
        try:
            cached = self._store.load_hydro_contours_by_bbox(bbox=request.bbox)
        except Exception:
            return None
        if cached is None:
            return None
        return MapContourResult(
            source=request.source,
            features=tuple(cached),
            status="ok",
            cache_hit=False,
        )

    def _load_download_state(self, request: MapContourRequest) -> Any | None:
        try:
            return self._store.load_hydro_bbox_download_state(bbox=request.bbox)
        except Exception:
            return None

    def _build_pending_result(
        self,
        request: MapContourRequest,
        download_state: Any | None,
    ) -> MapContourResult:
        partial_features: tuple[GeoJSONFeature, ...] = ()
        try:
            cached_partial = self._store.load_hydro_partial_contours_by_bbox(bbox=request.bbox)
        except Exception:
            cached_partial = None
        if cached_partial is not None:
            partial_features = tuple(cached_partial)

        details: dict[str, Any] = {
            "poll_after_seconds": self._poll_hint_seconds,
            "download_in_progress": True,
        }
        if download_state is not None:
            details["partial_feature_count"] = download_state.feature_count
            details["resume_collection"] = download_state.resume_collection
            details["resume_url"] = download_state.resume_url
        return MapContourResult(
            source=request.source,
            features=partial_features,
            status="pending",
            details=details,
        )


class MapContourService:
    """Select the configured provider and return UI-friendly payloads."""

    def __init__(
        self,
        *,
        default_source: str,
        providers: dict[str, MapContourProvider],
    ) -> None:
        self._default_source = default_source
        self._providers = providers

    @property
    def default_source(self) -> str:
        return self._default_source

    def get_contours(
        self,
        *,
        bbox: BBox,
        source: str | None = None,
        range_km: float | None = None,
    ) -> MapContourResult:
        resolved_source = (source or self._default_source).strip().lower()
        provider = self._providers.get(resolved_source)
        if provider is None:
            valid_sources = ", ".join(VALID_MAP_SOURCES)
            raise ValueError(
                f"Unsupported map source: {resolved_source!r}. Expected one of {valid_sources}."
            )
        return provider.fetch(
            MapContourRequest(
                source=resolved_source,
                bbox=bbox,
                range_km=range_km,
            )
        )


class HydroContourProvider:
    """Fetch contour-like hydrography lines from Lantmateriet OGC Features."""

    COLLECTIONS = ("LandWaterBoundary", "StandingWater")

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        fetch_json: Callable[[str, dict[str, str]], dict[str, Any]] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._fetch_json = fetch_json or _fetch_json

    @property
    def is_available(self) -> bool:
        return bool(self._username and self._password)

    def fetch(self, request: MapContourRequest) -> MapContourResult:
        if not self.is_available:
            return MapContourResult(
                source=request.source,
                features=(),
                status="unavailable",
                error="Hydrografi credentials are not configured.",
            )

        try:
            features: list[GeoJSONFeature] = []
            for collection in self.COLLECTIONS:
                features.extend(self._fetch_collection(collection=collection, bbox=request.bbox))
            return MapContourResult(
                source=request.source,
                features=tuple(features),
                status="ok",
            )
        except Exception as exc:
            return MapContourResult(
                source=request.source,
                features=(),
                status="error",
                error=f"Hydrografi fetch failed: {exc}",
            )

    def _fetch_collection(self, *, collection: str, bbox: BBox) -> list[GeoJSONFeature]:
        headers = {"Authorization": _build_basic_auth_header(self._username, self._password)}
        features: list[GeoJSONFeature] = []
        url = self.build_items_url(collection=collection, bbox=bbox)

        while url:
            page = self.fetch_collection_page(collection=collection, bbox=bbox, url=url, headers=headers)
            features.extend(page.features)
            url = page.next_url

        return features

    def build_items_url(self, *, collection: str, bbox: BBox) -> str:
        params = urlencode(
            {
                "bbox": ",".join(f"{value:.6f}" for value in bbox),
                "limit": 1000,
                "f": "json",
            }
        )
        return f"{self._base_url}/collections/{collection}/items?{params}"

    def fetch_collection_page(
        self,
        *,
        collection: str,
        bbox: BBox,
        url: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> HydroContourPage:
        resolved_url = url or self.build_items_url(collection=collection, bbox=bbox)
        resolved_headers = headers or {
            "Authorization": _build_basic_auth_header(self._username, self._password)
        }
        LOGGER.info(
            "Calling Lantmateriet hydro API: collection=%s bbox=%s url=%s",
            collection,
            ",".join(f"{value:.6f}" for value in bbox),
            resolved_url,
        )
        payload = self._fetch_json(resolved_url, resolved_headers)
        raw_features = payload.get("features")
        features: tuple[GeoJSONFeature, ...] = ()
        if isinstance(raw_features, list):
            features = tuple(
                self._normalize_features(collection=collection, raw_features=raw_features)
            )
        return HydroContourPage(
            collection=collection,
            request_url=resolved_url,
            next_url=_extract_next_link(base_url=resolved_url, payload=payload),
            features=features,
        )

    def _normalize_features(
        self,
        *,
        collection: str,
        raw_features: list[dict[str, Any]],
    ) -> list[GeoJSONFeature]:
        normalized: list[GeoJSONFeature] = []
        for feature in raw_features:
            geometry = feature.get("geometry")
            if not isinstance(geometry, dict):
                continue
            properties = feature.get("properties")
            if not isinstance(properties, dict):
                properties = {}
            properties = {**properties, "collection": collection}
            normalized_geometry = _geometry_to_lines(geometry)
            if normalized_geometry is None:
                continue
            normalized.append(
                {
                    "type": "Feature",
                    "properties": properties,
                    "geometry": normalized_geometry,
                }
            )
        return normalized


@dataclass(frozen=True, slots=True)
class ProjectedPoint:
    easting: float
    northing: float


class Sweref99TmProjection:
    """Projection helper for WGS84 <-> SWEREF 99 TM (EPSG:3006)."""

    def __init__(self) -> None:
        axis = 6378137.0
        flattening = 1.0 / 298.257222101
        self._lambda_zero = math.radians(15.0)
        self._scale = 0.9996
        self._false_northing = 0.0
        self._false_easting = 500000.0

        e2 = flattening * (2.0 - flattening)
        n = flattening / (2.0 - flattening)
        self._a_roof = axis / (1.0 + n) * (
            1.0 + (n * n) / 4.0 + (n**4) / 64.0
        )
        self._delta1 = n / 2.0 - 2.0 * (n**2) / 3.0 + 5.0 * (n**3) / 16.0 + 41.0 * (n**4) / 180.0
        self._delta2 = 13.0 * (n**2) / 48.0 - 3.0 * (n**3) / 5.0 + 557.0 * (n**4) / 1440.0
        self._delta3 = 61.0 * (n**3) / 240.0 - 103.0 * (n**4) / 140.0
        self._delta4 = 49561.0 * (n**4) / 161280.0
        self._a_star = e2 + e2**2 + e2**3 + e2**4
        self._b_star = -(7.0 * e2**2 + 17.0 * e2**3 + 30.0 * e2**4) / 6.0
        self._c_star = (224.0 * e2**3 + 889.0 * e2**4) / 120.0
        self._d_star = -(4279.0 * e2**4) / 1260.0

    def to_grid(self, *, lat: float, lon: float) -> ProjectedPoint:
        phi = math.radians(lat)
        lam = math.radians(lon)
        phi_star = self._geodetic_latitude_to_conformal(phi)
        delta_lambda = lam - self._lambda_zero
        xi_prim = math.atan(math.tan(phi_star) / math.cos(delta_lambda))
        eta_prim = math.atanh(math.cos(phi_star) * math.sin(delta_lambda))
        northing = self._scale * self._a_roof * (
            xi_prim
            + self._delta1 * math.sin(2.0 * xi_prim) * math.cosh(2.0 * eta_prim)
            + self._delta2 * math.sin(4.0 * xi_prim) * math.cosh(4.0 * eta_prim)
            + self._delta3 * math.sin(6.0 * xi_prim) * math.cosh(6.0 * eta_prim)
            + self._delta4 * math.sin(8.0 * xi_prim) * math.cosh(8.0 * eta_prim)
        ) + self._false_northing
        easting = self._scale * self._a_roof * (
            eta_prim
            + self._delta1 * math.cos(2.0 * xi_prim) * math.sinh(2.0 * eta_prim)
            + self._delta2 * math.cos(4.0 * xi_prim) * math.sinh(4.0 * eta_prim)
            + self._delta3 * math.cos(6.0 * xi_prim) * math.sinh(6.0 * eta_prim)
            + self._delta4 * math.cos(8.0 * xi_prim) * math.sinh(8.0 * eta_prim)
        ) + self._false_easting
        return ProjectedPoint(easting=easting, northing=northing)

    def to_geodetic(self, *, easting: float, northing: float) -> tuple[float, float]:
        xi = (northing - self._false_northing) / (self._scale * self._a_roof)
        eta = (easting - self._false_easting) / (self._scale * self._a_roof)
        xi_prim = (
            xi
            - self._delta1 * math.sin(2.0 * xi) * math.cosh(2.0 * eta)
            - self._delta2 * math.sin(4.0 * xi) * math.cosh(4.0 * eta)
            - self._delta3 * math.sin(6.0 * xi) * math.cosh(6.0 * eta)
            - self._delta4 * math.sin(8.0 * xi) * math.cosh(8.0 * eta)
        )
        eta_prim = (
            eta
            - self._delta1 * math.cos(2.0 * xi) * math.sinh(2.0 * eta)
            - self._delta2 * math.cos(4.0 * xi) * math.sinh(4.0 * eta)
            - self._delta3 * math.cos(6.0 * xi) * math.sinh(6.0 * eta)
            - self._delta4 * math.cos(8.0 * xi) * math.sinh(8.0 * eta)
        )
        phi_star = math.asin(math.sin(xi_prim) / math.cosh(eta_prim))
        delta_lambda = math.atan(math.sinh(eta_prim) / math.cos(phi_star))
        lon = self._lambda_zero + delta_lambda
        lat = self._conformal_latitude_to_geodetic(phi_star)
        lat_deg = math.degrees(lat)
        lon_deg = math.degrees(lon)
        return self._refine_inverse(
            easting=easting,
            northing=northing,
            lat_deg=lat_deg,
            lon_deg=lon_deg,
        )

    def _geodetic_latitude_to_conformal(self, phi: float) -> float:
        sin_phi = math.sin(phi)
        cos_phi = math.cos(phi)
        sin_sq = sin_phi * sin_phi
        return phi - sin_phi * cos_phi * (
            self._a_star
            + self._b_star * sin_sq
            + self._c_star * sin_sq * sin_sq
            + self._d_star * sin_sq * sin_sq * sin_sq
        )

    def _conformal_latitude_to_geodetic(self, phi_star: float) -> float:
        sin_phi_star = math.sin(phi_star)
        cos_phi_star = math.cos(phi_star)
        sin_sq = sin_phi_star * sin_phi_star
        return phi_star + sin_phi_star * cos_phi_star * (
            self._a_star
            + self._b_star * sin_sq
            + self._c_star * sin_sq * sin_sq
            + self._d_star * sin_sq * sin_sq * sin_sq
        )

    def _refine_inverse(
        self,
        *,
        easting: float,
        northing: float,
        lat_deg: float,
        lon_deg: float,
    ) -> tuple[float, float]:
        current_lat = lat_deg
        current_lon = lon_deg
        delta = 1e-6

        for _ in range(4):
            base = self.to_grid(lat=current_lat, lon=current_lon)
            diff_e = easting - base.easting
            diff_n = northing - base.northing
            if abs(diff_e) < 0.001 and abs(diff_n) < 0.001:
                break

            step_lat = self.to_grid(lat=current_lat + delta, lon=current_lon)
            step_lon = self.to_grid(lat=current_lat, lon=current_lon + delta)
            de_dlat = (step_lat.easting - base.easting) / delta
            dn_dlat = (step_lat.northing - base.northing) / delta
            de_dlon = (step_lon.easting - base.easting) / delta
            dn_dlon = (step_lon.northing - base.northing) / delta
            determinant = (de_dlat * dn_dlon) - (de_dlon * dn_dlat)
            if abs(determinant) < 1e-12:
                break

            correction_lat = ((diff_e * dn_dlon) - (de_dlon * diff_n)) / determinant
            correction_lon = ((de_dlat * diff_n) - (diff_e * dn_dlat)) / determinant
            current_lat += correction_lat
            current_lon += correction_lon

        return (current_lat, current_lon)


class MarkhojdDirectContourProvider:
    """Sample Markhojd Direkt and generate local contour segments."""

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        srid: int,
        sample_step_m: int,
        contour_interval_m: int,
        max_points_per_request: int,
        projection: Sweref99TmProjection | None = None,
        post_json: Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._srid = srid
        self._sample_step_m = sample_step_m
        self._contour_interval_m = contour_interval_m
        self._max_points_per_request = max_points_per_request
        self._projection = projection or Sweref99TmProjection()
        self._post_json = post_json or _post_json

    def fetch(self, request: MapContourRequest) -> MapContourResult:
        if not self._username or not self._password:
            return MapContourResult(
                source=request.source,
                features=(),
                status="unavailable",
                error="Markhojd Direkt credentials are not configured.",
            )
        if self._srid != 3006:
            return MapContourResult(
                source=request.source,
                features=(),
                status="error",
                error="Markhojd Direkt contour generation currently requires SRID 3006.",
            )

        try:
            projected_bounds = _project_bbox(request.bbox, projection=self._projection)
            grid = _build_sampling_grid(
                projected_bounds=projected_bounds,
                min_step_m=self._sample_step_m,
                max_points=self._max_points_per_request,
            )
            LOGGER.info(
                "Calling Lantmateriet Markhojd Direkt: bbox=%s sample_points=%d srid=%d url=%s/hojd",
                ",".join(f"{value:.6f}" for value in request.bbox),
                len(grid.points),
                self._srid,
                self._base_url,
            )
            response_feature = self._fetch_heights(grid)
            nodata_value = _read_nodata_value(response_feature)
            heights = _decode_height_grid(
                response_feature=response_feature,
                x_count=len(grid.eastings),
                y_count=len(grid.northings),
                nodata_value=nodata_value,
            )
            features = _generate_contour_features(
                eastings=grid.eastings,
                northings=grid.northings,
                heights=heights,
                contour_interval_m=self._contour_interval_m,
                projection=self._projection,
            )
            return MapContourResult(
                source=request.source,
                features=tuple(features),
                status="ok",
                details={
                    "service": "markhojd-direkt",
                    "srid": self._srid,
                    "requested_sample_step_m": self._sample_step_m,
                    "effective_sample_step_m": round(grid.effective_step_m, 3),
                    "contour_interval_m": self._contour_interval_m,
                    "sample_point_count": len(grid.points),
                    "grid_width": len(grid.eastings),
                    "grid_height": len(grid.northings),
                    "segment_count": len(features),
                },
            )
        except Exception as exc:
            return MapContourResult(
                source=request.source,
                features=(),
                status="error",
                error=f"Markhojd Direkt contour generation failed: {exc}",
            )

    def _fetch_heights(self, grid: "SamplingGrid") -> dict[str, Any]:
        headers = {
            "Authorization": _build_basic_auth_header(self._username, self._password),
            "Content-Type": "application/json",
        }
        payload = {
            "type": "MultiPoint",
            "crs": {
                "type": "name",
                "properties": {
                    "name": f"urn:ogc:def:crs:EPSG::{self._srid}",
                },
            },
            "coordinates": [[point.easting, point.northing] for point in grid.points],
        }
        return self._post_json(f"{self._base_url}/hojd", headers, payload)


@dataclass(frozen=True, slots=True)
class SamplingGrid:
    eastings: tuple[float, ...]
    northings: tuple[float, ...]
    points: tuple[ProjectedPoint, ...]
    effective_step_m: float


def build_map_contour_service(
    config: Config,
    *,
    store: SQLiteStore | None = None,
) -> MapContourService:
    """Create cached contour providers from resolved runtime config."""

    hydro_origin = HydroContourProvider(
        base_url=config.hydro_base_url,
        username=config.hydro_username,
        password=config.hydro_password,
    )
    if store is not None:
        hydro_source = DatabaseHydroContourProvider(
            hydro_origin,
            store=store,
        )
        hydro_source = BackgroundHydroContourProvider(
            hydro_source,
            store=store,
        )
    else:
        hydro_source = PersistentMapContourProvider(
            hydro_origin,
            cache_dir=config.map_cache_dir,
        )

    hydro_provider = CachingMapContourProvider(
        hydro_source,
        ttl_seconds=config.map_cache_ttl_seconds,
    )
    elevation_provider = CachingMapContourProvider(
        PersistentMapContourProvider(
            MarkhojdDirectContourProvider(
                base_url=config.markhojd_direct_base_url,
                username=config.markhojd_direct_username,
                password=config.markhojd_direct_password,
                srid=config.markhojd_direct_srid,
                sample_step_m=config.markhojd_direct_sample_step_m,
                contour_interval_m=config.markhojd_direct_contour_interval_m,
                max_points_per_request=config.markhojd_direct_max_points_per_request,
            ),
            cache_dir=config.map_cache_dir,
        ),
        ttl_seconds=config.map_cache_ttl_seconds,
    )
    return MapContourService(
        default_source=config.map_source,
        providers={
            "hydro": hydro_provider,
            "elevation": elevation_provider,
        },
    )


def _build_sampling_grid(
    *,
    projected_bounds: tuple[float, float, float, float],
    min_step_m: int,
    max_points: int,
) -> SamplingGrid:
    min_easting, min_northing, max_easting, max_northing = projected_bounds
    if min_easting >= max_easting or min_northing >= max_northing:
        raise ValueError("Projected bbox must have positive width and height.")

    effective_step_m = float(min_step_m)
    while True:
        eastings = _axis_values(min_easting, max_easting, effective_step_m)
        northings = _axis_values(min_northing, max_northing, effective_step_m)
        point_count = len(eastings) * len(northings)
        if point_count <= max_points:
            break
        effective_step_m *= 1.2

    points = tuple(
        ProjectedPoint(easting=easting, northing=northing)
        for northing in northings
        for easting in eastings
    )
    return SamplingGrid(
        eastings=tuple(eastings),
        northings=tuple(northings),
        points=points,
        effective_step_m=effective_step_m,
    )


def _axis_values(start: float, end: float, step_m: float) -> list[float]:
    width = max(0.0, end - start)
    if width == 0:
        return [start, end]
    count = max(2, int(math.ceil(width / step_m)) + 1)
    if count == 2:
        return [start, end]
    actual_step = width / (count - 1)
    return [start + (index * actual_step) for index in range(count)]


def _project_bbox(
    bbox: BBox,
    *,
    projection: Sweref99TmProjection,
) -> tuple[float, float, float, float]:
    min_lon, min_lat, max_lon, max_lat = bbox
    corners = (
        projection.to_grid(lat=min_lat, lon=min_lon),
        projection.to_grid(lat=min_lat, lon=max_lon),
        projection.to_grid(lat=max_lat, lon=min_lon),
        projection.to_grid(lat=max_lat, lon=max_lon),
    )
    eastings = [corner.easting for corner in corners]
    northings = [corner.northing for corner in corners]
    return (min(eastings), min(northings), max(eastings), max(northings))


def _read_nodata_value(response_feature: dict[str, Any]) -> float | None:
    properties = response_feature.get("properties")
    if not isinstance(properties, dict):
        return None
    raw_value = properties.get("nodatavalue")
    if isinstance(raw_value, (int, float)):
        return float(raw_value)
    return None


def _decode_height_grid(
    *,
    response_feature: dict[str, Any],
    x_count: int,
    y_count: int,
    nodata_value: float | None,
) -> list[list[float | None]]:
    geometry = response_feature.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") != "MultiPoint":
        raise ValueError("Expected Markhojd Direkt to return a MultiPoint geometry.")
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list):
        raise ValueError("Expected coordinates in Markhojd Direkt response.")
    if len(coordinates) != x_count * y_count:
        raise ValueError("Unexpected number of points in Markhojd Direkt response.")

    values: list[float | None] = []
    for coordinate in coordinates:
        if not isinstance(coordinate, list) or len(coordinate) < 3:
            values.append(None)
            continue
        z_value = coordinate[2]
        if not isinstance(z_value, (int, float)):
            values.append(None)
            continue
        height = float(z_value)
        if nodata_value is not None and math.isclose(height, nodata_value, rel_tol=0.0, abs_tol=1e-9):
            values.append(None)
            continue
        values.append(height)

    rows: list[list[float | None]] = []
    index = 0
    for _ in range(y_count):
        row = values[index : index + x_count]
        rows.append(row)
        index += x_count
    return rows


def _generate_contour_features(
    *,
    eastings: tuple[float, ...],
    northings: tuple[float, ...],
    heights: list[list[float | None]],
    contour_interval_m: int,
    projection: Sweref99TmProjection,
) -> list[GeoJSONFeature]:
    valid_heights = [value for row in heights for value in row if value is not None]
    if not valid_heights:
        return []

    min_height = min(valid_heights)
    max_height = max(valid_heights)
    if math.isclose(min_height, max_height, rel_tol=0.0, abs_tol=1e-9):
        return []

    levels = _contour_levels(min_height=min_height, max_height=max_height, interval_m=contour_interval_m)
    features: list[GeoJSONFeature] = []
    for level in levels:
        for row_index in range(len(northings) - 1):
            for column_index in range(len(eastings) - 1):
                segments = _cell_contour_segments(
                    level=level,
                    bl=(eastings[column_index], northings[row_index], heights[row_index][column_index]),
                    br=(eastings[column_index + 1], northings[row_index], heights[row_index][column_index + 1]),
                    tl=(eastings[column_index], northings[row_index + 1], heights[row_index + 1][column_index]),
                    tr=(
                        eastings[column_index + 1],
                        northings[row_index + 1],
                        heights[row_index + 1][column_index + 1],
                    ),
                )
                for start_point, end_point in segments:
                    start_lat, start_lon = projection.to_geodetic(
                        easting=start_point[0],
                        northing=start_point[1],
                    )
                    end_lat, end_lon = projection.to_geodetic(
                        easting=end_point[0],
                        northing=end_point[1],
                    )
                    features.append(
                        {
                            "type": "Feature",
                            "properties": {
                                "elevation_m": level,
                                "source": "markhojd-direkt",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [
                                    [start_lon, start_lat],
                                    [end_lon, end_lat],
                                ],
                            },
                        }
                    )
    return features


def _contour_levels(*, min_height: float, max_height: float, interval_m: int) -> list[float]:
    start = math.floor(min_height / interval_m) * interval_m
    level = float(start)
    levels: list[float] = []
    while level <= max_height:
        if level >= min_height:
            levels.append(level)
        level += interval_m
    return levels


def _cell_contour_segments(
    *,
    level: float,
    bl: tuple[float, float, float | None],
    br: tuple[float, float, float | None],
    tl: tuple[float, float, float | None],
    tr: tuple[float, float, float | None],
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    corners = (bl, br, tl, tr)
    if any(corner[2] is None for corner in corners):
        return []

    crossings: list[tuple[float, float]] = []
    for edge_start, edge_end in ((bl, br), (br, tr), (tl, tr), (bl, tl)):
        point = _edge_crossing(level, edge_start, edge_end)
        if point is not None:
            crossings.append(point)

    if len(crossings) < 2:
        return []
    if len(crossings) == 2:
        return [(crossings[0], crossings[1])]
    if len(crossings) == 4:
        center_height = sum(float(corner[2]) for corner in corners) / 4.0
        if center_height < level:
            return [
                (crossings[0], crossings[3]),
                (crossings[1], crossings[2]),
            ]
        return [
            (crossings[0], crossings[1]),
            (crossings[2], crossings[3]),
        ]
    return []


def _edge_crossing(
    level: float,
    start: tuple[float, float, float | None],
    end: tuple[float, float, float | None],
) -> tuple[float, float] | None:
    z1 = float(start[2]) if start[2] is not None else math.nan
    z2 = float(end[2]) if end[2] is not None else math.nan
    if not math.isfinite(z1) or not math.isfinite(z2):
        return None
    if (level < min(z1, z2)) or (level > max(z1, z2)):
        return None
    if math.isclose(z1, z2, rel_tol=0.0, abs_tol=1e-9):
        return ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
    if math.isclose(level, z1, rel_tol=0.0, abs_tol=1e-9) and math.isclose(
        level,
        z2,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        return None
    ratio = (level - z1) / (z2 - z1)
    if ratio < 0.0 or ratio > 1.0:
        return None
    return (
        start[0] + ((end[0] - start[0]) * ratio),
        start[1] + ((end[1] - start[1]) * ratio),
    )


def _normalize_bbox_key(bbox: BBox) -> BBox:
    return tuple(round(value, 6) for value in bbox)  # type: ignore[return-value]


def _geometry_to_lines(geometry: dict[str, Any]) -> dict[str, Any] | None:
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "LineString" and isinstance(coordinates, list):
        return {"type": "LineString", "coordinates": coordinates}
    if geometry_type == "MultiLineString" and isinstance(coordinates, list):
        return {"type": "MultiLineString", "coordinates": coordinates}
    if geometry_type == "Polygon" and isinstance(coordinates, list):
        rings = [ring for ring in coordinates if isinstance(ring, list) and ring]
        if not rings:
            return None
        return {"type": "MultiLineString", "coordinates": rings}
    if geometry_type == "MultiPolygon" and isinstance(coordinates, list):
        rings = []
        for polygon in coordinates:
            if not isinstance(polygon, list):
                continue
            rings.extend(ring for ring in polygon if isinstance(ring, list) and ring)
        if not rings:
            return None
        return {"type": "MultiLineString", "coordinates": rings}
    return None


def _extract_next_link(*, base_url: str, payload: dict[str, Any]) -> str | None:
    links = payload.get("links")
    if not isinstance(links, list):
        return None
    for link in links:
        if not isinstance(link, dict):
            continue
        if link.get("rel") != "next":
            continue
        href = link.get("href")
        if not isinstance(href, str) or not href:
            continue
        return urljoin(base_url, href)
    return None


def _build_basic_auth_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _fetch_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    request = Request(url, headers=headers)
    with urlopen(request, timeout=15) as response:  # noqa: S310
        payload = response.read().decode("utf-8")
    data = json.loads(payload)
    return data if isinstance(data, dict) else {}


def _post_json(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310
        payload = response.read().decode("utf-8")
    data = json.loads(payload)
    return data if isinstance(data, dict) else {}
