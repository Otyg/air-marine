"""Native Qt UI implementation for live radar client."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import sqlite3
from typing import Any

from PySide6.QtCore import QPointF, QRectF, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app.qt_live_view import (
    KM_PER_DEG_LAT,
    MIN_RANGE_KM,
    QtLiveViewConfig,
    ViewState,
    build_api_url,
    parse_live_ui_config,
)

LOGGER = logging.getLogger(__name__)

SCAN_ORDER = ("AIS", "ADS")
RADAR_RING_COUNT = 5
DEFAULT_MAP_CACHE_DB_PATH = Path("./data/qt_map_contours.sqlite")
TRAIL_POINT_WINDOW_SECONDS = 120.0
TRAIL_STALE_START_SECONDS = 120.0
TRAIL_STALE_FADE_SECONDS = 270.0
LIVE_TRAIL_AGE_COLORS = (
    "#C1F5C1",
    "#90EE90",
    "#72E972",
    "#4AE34A",
    "#22DD22",
    "#1CB51C",
    "#168D16",
    "#106510",
    "#0A3E0A",
    "#031603",
)


class MapContourTileCache:
    """SQLite-backed cache of map contour features per source/zoom/tile."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS contour_tiles (
                source TEXT NOT NULL,
                zoom_level INTEGER NOT NULL,
                tile_x INTEGER NOT NULL,
                tile_y INTEGER NOT NULL,
                fetched_at TEXT NOT NULL,
                features_json TEXT NOT NULL,
                PRIMARY KEY (source, zoom_level, tile_x, tile_y)
            )
            """
        )
        self.conn.commit()

    def get_tile_features(
        self,
        *,
        source: str,
        zoom_level: int,
        tile_x: int,
        tile_y: int,
    ) -> list[dict[str, Any]] | None:
        row = self.conn.execute(
            """
            SELECT features_json
            FROM contour_tiles
            WHERE source = ? AND zoom_level = ? AND tile_x = ? AND tile_y = ?
            """,
            (source, zoom_level, tile_x, tile_y),
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(str(row[0]))
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, list):
            return None
        return [item for item in payload if isinstance(item, dict)]

    def upsert_tile_features(
        self,
        *,
        source: str,
        zoom_level: int,
        tile_x: int,
        tile_y: int,
        features: list[dict[str, Any]],
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO contour_tiles (source, zoom_level, tile_x, tile_y, fetched_at, features_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, zoom_level, tile_x, tile_y)
            DO UPDATE SET
                fetched_at = excluded.fetched_at,
                features_json = excluded.features_json
            """,
            (
                source,
                zoom_level,
                tile_x,
                tile_y,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(features, separators=(",", ":"), ensure_ascii=True),
            ),
        )
        self.conn.commit()


class RadarWidget(QWidget):
    """Native radar drawing area."""

    view_changed = Signal(float, float, float)
    target_selected = Signal(str)

    def __init__(self, state: ViewState) -> None:
        super().__init__()
        self.setMinimumSize(500, 400)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.state = state
        self.home_lat = state.center_lat
        self.home_lon = state.center_lon

        self.targets: list[dict[str, Any]] = []
        self.fixed_objects: list[dict[str, Any]] = []
        self.map_segments: list[tuple[QPointF, QPointF]] = []

        self.show_target_labels = False
        self.show_fixed_names = True
        self.show_map_contours = True
        self.show_stopped = False
        self.show_aircraft = True
        self.show_vessel = True
        self.selected_target_id: str | None = None
        self.local_trails: dict[str, list[tuple[float, float, float]]] = {}

    def set_home(self, lat: float, lon: float) -> None:
        self.home_lat = lat
        self.home_lon = lon
        self.state.center_lat = lat
        self.state.center_lon = lon
        self.view_changed.emit(self.state.center_lat, self.state.center_lon, self.state.range_km)
        self.update()

    def set_targets(self, targets: list[dict[str, Any]]) -> None:
        self.targets = targets
        self._update_local_trails(targets)
        self.update()

    def set_fixed_objects(self, fixed_objects: list[dict[str, Any]]) -> None:
        normalized: list[dict[str, Any]] = []
        for item in fixed_objects:
            if not isinstance(item, dict):
                continue
            normalized_item = dict(item)
            if normalized_item.get("lat") is None and normalized_item.get("latitude") is not None:
                normalized_item["lat"] = normalized_item.get("latitude")
            if normalized_item.get("lon") is None and normalized_item.get("longitude") is not None:
                normalized_item["lon"] = normalized_item.get("longitude")
            normalized.append(normalized_item)
        self.fixed_objects = normalized
        self.update()

    def set_map_segments(self, segments: list[tuple[QPointF, QPointF]]) -> None:
        self.map_segments = segments
        self.update()

    def set_selected_target(self, target_id: str | None) -> None:
        self.selected_target_id = target_id
        self.update()

    def set_range_km(self, range_km: float) -> None:
        self.state.range_km = max(MIN_RANGE_KM, min(500.0, float(range_km)))
        self.view_changed.emit(self.state.center_lat, self.state.center_lon, self.state.range_km)
        self.update()

    def zoom_in(self) -> None:
        self.set_range_km(self.state.range_km - 1.0)

    def zoom_out(self) -> None:
        self.set_range_km(self.state.range_km + 1.0)

    def _km_per_deg_lon(self, lat: float) -> float:
        value = 111.320 * math.cos(math.radians(lat))
        if abs(value) < 0.01:
            return 0.01
        return value

    def _latlon_to_xy(self, lat: float, lon: float, cx: float, cy: float, px_per_km: float) -> QPointF:
        dy_km = (lat - self.state.center_lat) * KM_PER_DEG_LAT
        dx_km = (lon - self.state.center_lon) * self._km_per_deg_lon(self.state.center_lat)
        return QPointF(cx + (dx_km * px_per_km), cy - (dy_km * px_per_km))

    def _target_color(self, target: dict[str, Any]) -> QColor:
        _kind = str(target.get("kind", "")).lower()
        return QColor("#c1f5c1")

    def _fixed_symbol_text(self, raw_symbol: str) -> str:
        symbol = (raw_symbol or "").strip()
        if not symbol:
            return "O"
        mapped = {
            "◬": "^",
            "▲": "^",
            "△": "^",
            "★": "*",
            "✈": "A",
        }.get(symbol, symbol)
        return mapped[:1] or "O"

    def _now_ms(self) -> float:
        return datetime.now(timezone.utc).timestamp() * 1000.0

    def _parse_timestamp_ms(self, value: Any) -> float | None:
        if not isinstance(value, str):
            return None
        raw = value.strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp() * 1000.0

    def _trail_fade_progress(self, last_seen_ms: float | None) -> float:
        if last_seen_ms is None:
            return 0.0
        inactive_seconds = (self._now_ms() - last_seen_ms) / 1000.0
        if not math.isfinite(inactive_seconds) or inactive_seconds <= TRAIL_STALE_START_SECONDS:
            return 0.0
        return max(
            0.0,
            min(1.0, (inactive_seconds - TRAIL_STALE_START_SECONDS) / TRAIL_STALE_FADE_SECONDS),
        )

    def _update_local_trails(self, targets: list[dict[str, Any]]) -> None:
        now_ms = self._now_ms()
        active_target_ids: set[str] = set()

        for target in targets:
            if not isinstance(target, dict):
                continue
            target_id = str(target.get("target_id") or "").strip()
            if not target_id:
                continue
            lat_value = target.get("lat")
            lon_value = target.get("lon")
            try:
                lat = float(lat_value)
                lon = float(lon_value)
            except (TypeError, ValueError):
                continue

            sample_ts_ms = self._parse_timestamp_ms(target.get("last_seen"))
            if sample_ts_ms is None or not math.isfinite(sample_ts_ms):
                sample_ts_ms = now_ms

            active_target_ids.add(target_id)
            trail = self.local_trails.setdefault(target_id, [])
            if trail:
                prev_ts_ms, prev_lat, prev_lon = trail[-1]
                same_position = (abs(prev_lat - lat) < 1e-7) and (abs(prev_lon - lon) < 1e-7)
                if sample_ts_ms <= prev_ts_ms and same_position:
                    continue
                if same_position:
                    trail[-1] = (max(prev_ts_ms, sample_ts_ms), prev_lat, prev_lon)
                    continue
            trail.append((sample_ts_ms, lat, lon))
            if len(trail) > 256:
                del trail[:-256]

        max_trail_age_seconds = TRAIL_POINT_WINDOW_SECONDS + TRAIL_STALE_START_SECONDS + TRAIL_STALE_FADE_SECONDS
        purge_before_ms = now_ms - (max_trail_age_seconds * 1000.0)
        stale_target_ids: list[str] = []
        for target_id, trail in self.local_trails.items():
            retained = [sample for sample in trail if sample[0] >= purge_before_ms]
            if retained:
                self.local_trails[target_id] = retained
            elif target_id in active_target_ids:
                self.local_trails[target_id] = []
            else:
                stale_target_ids.append(target_id)
        for target_id in stale_target_ids:
            self.local_trails.pop(target_id, None)

    def _trail_opacity_for_age_rank(self, age_rank: float, fade_progress: float) -> float:
        if fade_progress <= 0.0:
            return 1.0
        clamped_rank = max(0.0, min(1.0, float(age_rank)))
        fade_start = (1.0 - clamped_rank) * 0.65
        if fade_start >= 1.0:
            return 1.0
        local_progress = max(0.0, min(1.0, (fade_progress - fade_start) / (1.0 - fade_start)))
        return 1.0 - local_progress

    def _live_trail_color_for_age(self, age_rank: float) -> QColor:
        clamped_rank = max(0.0, min(1.0, float(age_rank)))
        palette_index = min(
            len(LIVE_TRAIL_AGE_COLORS) - 1,
            int(math.floor(clamped_rank * len(LIVE_TRAIL_AGE_COLORS))),
        )
        return QColor(LIVE_TRAIL_AGE_COLORS[palette_index])

    def _draw_recent_positions(
        self,
        painter: QPainter,
        *,
        target_id: str,
        last_seen: Any,
        cx: float,
        cy: float,
        px_per_km: float,
        radius: float,
        current_point: QPointF | None = None,
    ) -> None:
        raw_trail = self.local_trails.get(target_id)
        if not raw_trail:
            return

        cutoff_ms = self._now_ms() - (TRAIL_POINT_WINDOW_SECONDS * 1000.0)
        ordered = [sample for sample in raw_trail if sample[0] >= cutoff_ms]

        if not ordered:
            return

        ordered.sort(key=lambda item: item[0], reverse=True)
        points: list[QPointF] = []
        for _ts_ms, lat, lon in ordered:
            point = self._latlon_to_xy(lat, lon, cx, cy, px_per_km)
            if math.hypot(point.x() - cx, point.y() - cy) > radius:
                continue
            if current_point is not None:
                same_as_current = (
                    ((point.x() - current_point.x()) ** 2) + ((point.y() - current_point.y()) ** 2)
                ) < 1.0
                if same_as_current:
                    continue
            points.append(point)

        if not points:
            return

        last_seen_ms = self._parse_timestamp_ms(last_seen)
        if last_seen_ms is None and ordered:
            last_seen_ms = ordered[0][0]
        fade_progress = self._trail_fade_progress(last_seen_ms)
        painter.save()
        dashed_pen = QPen(QColor("#2c7a2c"), 1)
        dashed_pen.setDashPattern([4.0, 3.0])
        painter.setPen(dashed_pen)

        if current_point is not None:
            newest_opacity = self._trail_opacity_for_age_rank(0.0, fade_progress)
            if newest_opacity > 0.02:
                painter.setOpacity(newest_opacity)
                head_pen = QPen(self._live_trail_color_for_age(0.0), 1)
                head_pen.setDashPattern([4.0, 3.0])
                painter.setPen(head_pen)
                painter.drawLine(current_point, points[0])

        for index in range(0, len(points) - 1):
            age_rank = 1.0 if len(points) <= 1 else (index + 1) / (len(points) - 1)
            segment_opacity = self._trail_opacity_for_age_rank(age_rank, fade_progress)
            if segment_opacity <= 0.02:
                continue
            painter.setOpacity(segment_opacity)
            segment_pen = QPen(self._live_trail_color_for_age(age_rank), 1)
            segment_pen.setDashPattern([4.0, 3.0])
            painter.setPen(segment_pen)
            painter.drawLine(points[index], points[index + 1])

        for index, point in enumerate(points):
            age_rank = 1.0 if len(points) <= 1 else index / (len(points) - 1)
            point_opacity = self._trail_opacity_for_age_rank(age_rank, fade_progress)
            if point_opacity <= 0.02:
                continue
            painter.setOpacity(point_opacity)
            dot_color = self._live_trail_color_for_age(age_rank)
            painter.setPen(QPen(dot_color, 1))
            painter.setBrush(dot_color)
            painter.drawEllipse(point, 1.6, 1.6)

        painter.restore()

    def _is_target_visible(self, target: dict[str, Any]) -> bool:
        lat = target.get("lat")
        lon = target.get("lon")
        if lat is None or lon is None:
            return False
        try:
            dy_km = (float(lat) - self.state.center_lat) * KM_PER_DEG_LAT
            dx_km = (float(lon) - self.state.center_lon) * self._km_per_deg_lon(self.state.center_lat)
        except (TypeError, ValueError):
            return False
        return math.hypot(dx_km, dy_km) <= self.state.range_km

    def filtered_targets(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        visible: list[dict[str, Any]] = []
        outside: list[dict[str, Any]] = []
        for target in self.targets:
            kind = str(target.get("kind", "")).lower()
            if kind == "aircraft" and not self.show_aircraft:
                continue
            if kind == "vessel" and not self.show_vessel:
                continue

            speed_value = target.get("speed")
            try:
                speed = float(speed_value) if speed_value is not None else float("nan")
            except (TypeError, ValueError):
                speed = float("nan")

            if not self.show_stopped and math.isfinite(speed) and speed < 1.0:
                continue

            if self._is_target_visible(target):
                visible.append(target)
            else:
                outside.append(target)
        return visible, outside

    def wheelEvent(self, event) -> None:  # noqa: N802
        delta = event.angleDelta().y()
        if delta > 0:
            self.zoom_in()
        elif delta < 0:
            self.zoom_out()
        event.accept()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # Trigger a full view refresh when viewport geometry changes.
        self.view_changed.emit(self.state.center_lat, self.state.center_lon, self.state.range_km)
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return

        width = float(self.width())
        height = float(self.height())
        cx = width / 2.0
        cy = height / 2.0
        radius = max(30.0, min(width, height) * 0.45)
        px_per_km = radius / self.state.range_km

        click_pos = event.position()
        nearest_target_id: str | None = None
        nearest_distance_px = float("inf")
        for target in self.targets:
            target_id = str(target.get("target_id", ""))
            lat = target.get("lat")
            lon = target.get("lon")
            if not target_id or lat is None or lon is None:
                continue
            try:
                point = self._latlon_to_xy(float(lat), float(lon), cx, cy, px_per_km)
            except (TypeError, ValueError):
                continue
            distance_px = math.hypot(point.x() - click_pos.x(), point.y() - click_pos.y())
            if distance_px < nearest_distance_px:
                nearest_distance_px = distance_px
                nearest_target_id = target_id

        if nearest_target_id and nearest_distance_px <= 14.0:
            self.selected_target_id = nearest_target_id
            self.target_selected.emit(nearest_target_id)
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        width = float(self.width())
        height = float(self.height())
        cx = width / 2.0
        cy = height / 2.0
        radius = max(30.0, min(width, height) * 0.45)
        px_per_km = radius / self.state.range_km

        painter.fillRect(self.rect(), QColor("#000000"))

        painter.setPen(QPen(QColor("#2c7a2c"), 1))
        for index in range(1, RADAR_RING_COUNT + 1):
            ring_radius = radius * index / RADAR_RING_COUNT
            painter.drawEllipse(QPointF(cx, cy), ring_radius, ring_radius)

        painter.setPen(QPen(QColor("#154815"), 1))
        painter.drawLine(QPointF(cx - radius, cy), QPointF(cx + radius, cy))
        painter.drawLine(QPointF(cx, cy - radius), QPointF(cx, cy + radius))

        if self.show_map_contours:
            contour_pen = QPen(QColor("#143314"))
            contour_pen.setWidthF(0.3)
            painter.setPen(contour_pen)
            for start, end in self.map_segments:
                painter.drawLine(start, end)

        painter.setPen(QPen(QColor("#2c7a2c"), 1))
        for fixed in self.fixed_objects:
            lat = fixed.get("lat")
            lon = fixed.get("lon")
            if lat is None or lon is None:
                continue
            max_range = fixed.get("max_visible_range_km")
            if max_range is not None:
                try:
                    if self.state.range_km > float(max_range):
                        continue
                except (TypeError, ValueError):
                    pass
            point = self._latlon_to_xy(float(lat), float(lon), cx, cy, px_per_km)
            if math.hypot(point.x() - cx, point.y() - cy) > radius:
                continue
            raw_symbol = str(fixed.get("symbol", "")).strip()
            symbol = self._fixed_symbol_text(raw_symbol)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(point, 3.2, 3.2)
            painter.drawPoint(point)
            symbol_rect = QRectF(point.x() - 7.0, point.y() - 7.0, 14.0, 14.0)
            painter.drawText(symbol_rect, int(Qt.AlignmentFlag.AlignCenter), symbol)
            if self.show_fixed_names:
                raw_name = str(fixed.get("name", "")).strip()
                if raw_name:
                    name_lines = [segment for segment in raw_name.split() if segment]
                    if name_lines:
                        line_height = 12.0
                        start_y = point.y() - (((len(name_lines) - 1) * line_height) * 0.5)
                        painter.setPen(QPen(QColor("#9be89b"), 1))
                        for index, line in enumerate(name_lines):
                            text_point = QPointF(point.x() + 7.0, start_y + (index * line_height))
                            painter.drawText(text_point, line)
                        painter.setPen(QPen(QColor("#2c7a2c"), 1))

        visible_targets, outside_targets = self.filtered_targets()
        for target in visible_targets:
            lat = target.get("lat")
            lon = target.get("lon")
            target_id = str(target.get("target_id") or "")
            if lat is None or lon is None:
                continue
            if not target_id:
                continue
            point = self._latlon_to_xy(float(lat), float(lon), cx, cy, px_per_km)
            self._draw_recent_positions(
                painter,
                target_id=target_id,
                last_seen=target.get("last_seen"),
                cx=cx,
                cy=cy,
                px_per_km=px_per_km,
                radius=radius,
                current_point=point,
            )
        for target in outside_targets:
            target_id = str(target.get("target_id") or "")
            if not target_id:
                continue
            self._draw_recent_positions(
                painter,
                target_id=target_id,
                last_seen=target.get("last_seen"),
                cx=cx,
                cy=cy,
                px_per_km=px_per_km,
                radius=radius,
                current_point=None,
            )

        for target in visible_targets:
            lat = target.get("lat")
            lon = target.get("lon")
            target_id = str(target.get("target_id", ""))
            if lat is None or lon is None:
                continue
            point = self._latlon_to_xy(float(lat), float(lon), cx, cy, px_per_km)

            color = self._target_color(target)
            if target_id and target_id == self.selected_target_id:
                color = QColor("#ff4d4d")

            painter.setPen(QPen(color, 1))

            course_value = target.get("course")
            speed_value = target.get("speed")
            try:
                course = float(course_value) if course_value is not None else float("nan")
                speed = float(speed_value) if speed_value is not None else float("nan")
            except (TypeError, ValueError):
                course = float("nan")
                speed = float("nan")

            if math.isfinite(course) and math.isfinite(speed) and speed > 0.0:
                radians = math.radians(course % 360)
                length = max(8.0, min(26.0, 8.0 + (math.sqrt(speed) * 1.5)))
                end_point = QPointF(
                    point.x() + (math.sin(radians) * length),
                    point.y() - (math.cos(radians) * length),
                )
                painter.drawLine(point, end_point)

            symbol = "◆" if str(target.get("kind", "")).lower() == "vessel" else "●"
            symbol_rect = QRectF(point.x() - 7.0, point.y() - 7.0, 14.0, 14.0)
            painter.drawText(symbol_rect, int(Qt.AlignmentFlag.AlignCenter), symbol)

            if self.show_target_labels:
                label = str(target.get("label") or target_id)
                painter.drawText(point + QPointF(6, -6), label)

        painter.setPen(QPen(QColor("#d3d3d3"), 2))
        painter.drawPoint(QPointF(cx, cy))

        painter.setPen(QPen(QColor("#9be89b"), 1))
        painter.drawText(12, 20, f"Center: {self.state.center_lat:.6f}, {self.state.center_lon:.6f}")
        painter.drawText(12, 40, f"Range: {self.state.range_km:.2f} km")


class LiveRadarWindow(QMainWindow):
    """Main Qt window for native live radar view."""

    def __init__(self, config: QtLiveViewConfig) -> None:
        super().__init__()
        self.config = config

        self.setWindowTitle(config.window_title)
        self.resize(config.window_width, config.window_height)

        self.network = QNetworkAccessManager(self)
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(config.poll_interval_ms)
        self.poll_timer.timeout.connect(self.load_targets)

        self.live_config_timer = QTimer(self)
        self.live_config_timer.setInterval(max(30_000, config.poll_interval_ms * 6))
        self.live_config_timer.timeout.connect(self.load_live_ui_config)

        self.map_retry_timer = QTimer(self)
        self.map_retry_timer.setSingleShot(True)
        self.map_retry_timer.timeout.connect(self._on_map_retry_timeout)

        self.service_name = "sdr-monitor"
        self.default_map_source = config.map_source
        self.current_targets: list[dict[str, Any]] = []
        self.current_scanner_scan: list[str] = ["AIS", "ADS"]
        self.backend_reachable = False
        self.radio_connected = False
        self._last_view_range_km = float(config.default_range_km)
        self.map_loaded_key: str | None = None
        self.map_pending_key: str | None = None
        self.map_in_flight = False
        self.map_refresh_pending = False
        self.map_cache = MapContourTileCache(DEFAULT_MAP_CACHE_DB_PATH)

        self.view_state = ViewState(
            center_lat=config.fallback_center_lat,
            center_lon=config.fallback_center_lon,
            range_km=config.default_range_km,
        )

        self.radar_widget = RadarWidget(self.view_state)
        self.radar_widget.show_fixed_names = config.show_fixed_names
        self.radar_widget.show_target_labels = config.show_target_labels
        self.radar_widget.show_map_contours = config.show_map_contours
        self.radar_widget.show_stopped = bool(config.show_low_speed)
        if config.target_type_filter == "aircraft":
            self.radar_widget.show_aircraft = True
            self.radar_widget.show_vessel = False
        elif config.target_type_filter == "vessel":
            self.radar_widget.show_aircraft = False
            self.radar_widget.show_vessel = True
        else:
            self.radar_widget.show_aircraft = True
            self.radar_widget.show_vessel = True
        self.radar_widget.view_changed.connect(self.on_view_changed)
        self.radar_widget.target_selected.connect(self.on_target_selected)

        self.range_input = QLineEdit(f"{config.default_range_km:.1f}")
        self.range_input.setMaximumWidth(84)
        self.range_input.editingFinished.connect(self.on_range_input_changed)

        self.scan_labels = {
            "AIS": QLabel("AIS"),
            "ADS": QLabel("ADS"),
        }
        self.radio_status_label = QLabel("Radio")
        for label in list(self.scan_labels.values()) + [self.radio_status_label]:
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.zoom_out_button = QPushButton("-")
        self.zoom_out_button.setFixedWidth(34)
        self.zoom_out_button.clicked.connect(self.on_zoom_out)

        self.zoom_in_button = QPushButton("+")
        self.zoom_in_button.setFixedWidth(34)
        self.zoom_in_button.clicked.connect(self.on_zoom_in)

        self.zoom_reset_button = QPushButton("Hem")
        self.zoom_reset_button.clicked.connect(self.on_zoom_reset)
        self.range_input.setFixedHeight(self.zoom_in_button.sizeHint().height())

        self.target_type_filter_buttons: dict[str, QPushButton] = {}
        for value, label in (("stopped", "Stoppade"), ("aircraft", "Flygplan"), ("vessel", "Batar")):
            button = QPushButton(label)
            button.setCheckable(True)
            button.toggled.connect(
                lambda checked, selected=value: self.on_target_type_filter_changed(selected, checked)
            )
            self.target_type_filter_buttons[value] = button
        self._sync_target_type_filter_buttons()

        self.overlay_toggle_buttons: dict[str, QPushButton] = {}
        for value, label, checked, handler in (
            ("fixed_names", "Fasta namn", config.show_fixed_names, self.on_show_fixed_names_changed),
            ("target_labels", "Objektlabels", config.show_target_labels, self.on_show_target_labels_changed),
            ("map_contours", "Kartkonturer", config.show_map_contours, self.on_show_map_contours_changed),
        ):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setChecked(checked)
            button.toggled.connect(handler)
            self.overlay_toggle_buttons[value] = button
        self._sync_overlay_toggle_buttons()

        self.objects_summary_label = QLabel("0 synliga objekt")
        self.visible_objects_list = QListWidget()
        self.visible_objects_list.itemClicked.connect(self.on_visible_item_clicked)

        self.outside_summary_label = QLabel("0 objekt utanfor aktivt omrade")
        self.outside_objects_list = QListWidget()
        self.outside_objects_list.itemClicked.connect(self.on_outside_item_clicked)

        self.reception_warning_label = QLabel("")
        self.reception_warning_label.setStyleSheet("color: #ffd48f;")

        self._build_layout()
        self._apply_dark_style()
        self._sync_scan_labels()
        self._refresh_target_lists()

    def _build_layout(self) -> None:
        root = QWidget(self)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(8, 8, 8, 8)
        root_layout.setSpacing(8)

        top_bar = QHBoxLayout()
        top_bar.setSpacing(8)

        title_label = QLabel("RADAR VIEW")
        title_label.setObjectName("hudTitle")
        top_bar.addWidget(title_label)

        top_bar.addStretch(1)

        root_layout.addLayout(top_bar)
        root_layout.addWidget(self.reception_warning_label)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.radar_widget)

        side_panel = QWidget()
        side_layout = QVBoxLayout(side_panel)
        side_layout.setContentsMargins(8, 8, 8, 8)
        side_layout.setSpacing(8)

        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(6)
        for value in SCAN_ORDER:
            status_row.addWidget(self.scan_labels[value])
        status_row.addWidget(self.radio_status_label)
        side_layout.addLayout(status_row)

        zoom_row = QHBoxLayout()
        zoom_row.setContentsMargins(0, 0, 0, 0)
        zoom_row.setSpacing(6)
        zoom_row.addWidget(self.zoom_out_button)
        zoom_row.addWidget(self.range_input)
        zoom_row.addWidget(self.zoom_in_button)
        zoom_row.addWidget(self.zoom_reset_button)
        side_layout.addLayout(zoom_row)

        overlay_toggle_row = QHBoxLayout()
        overlay_toggle_row.setContentsMargins(0, 0, 0, 0)
        overlay_toggle_row.setSpacing(6)
        for value in ("fixed_names", "target_labels", "map_contours"):
            overlay_toggle_row.addWidget(self.overlay_toggle_buttons[value])
        side_layout.addLayout(overlay_toggle_row)

        target_filter_row = QHBoxLayout()
        target_filter_row.setContentsMargins(0, 0, 0, 0)
        target_filter_row.setSpacing(6)
        for value in ("stopped", "aircraft", "vessel"):
            target_filter_row.addWidget(self.target_type_filter_buttons[value])
        side_layout.addLayout(target_filter_row)

        side_layout.addWidget(self.objects_summary_label)
        side_layout.addWidget(self.visible_objects_list, stretch=1)
        side_layout.addWidget(QLabel("Objekt utanfor aktivt omrade"))
        side_layout.addWidget(self.outside_summary_label)
        side_layout.addWidget(self.outside_objects_list, stretch=1)

        splitter.addWidget(side_panel)
        splitter.setSizes([1000, 360])

        root_layout.addWidget(splitter, stretch=1)
        self.setCentralWidget(root)

    def _apply_dark_style(self) -> None:
        self.setStyleSheet(
            """
            QWidget {
              background: #000000;
              color: #9be89b;
              font-family: LCD, "DS-Digital", "Digital-7", "Courier New", monospace;
            }
            QLineEdit, QListWidget {
              background: #041104;
              border: 1px solid #226322;
              color: #c1f5c1;
            }
            QPushButton {
              background: #051805;
              border: 1px solid #226322;
              color: #c1f5c1;
              padding: 4px 8px;
            }
            QPushButton:hover {
              background: #0a260a;
            }
            QLabel#hudTitle {
              color: #c1f5c1;
              font-weight: bold;
            }
            """
        )

    def _sync_target_type_filter_buttons(self) -> None:
        button_states = {
            "stopped": self.radar_widget.show_stopped,
            "aircraft": self.radar_widget.show_aircraft,
            "vessel": self.radar_widget.show_vessel,
        }
        for option, button in self.target_type_filter_buttons.items():
            active = bool(button_states.get(option, False))
            button.blockSignals(True)
            button.setChecked(active)
            button.blockSignals(False)
            self._style_toggle_button(button, active)

    def _sync_overlay_toggle_buttons(self) -> None:
        states = {
            "fixed_names": self.radar_widget.show_fixed_names,
            "target_labels": self.radar_widget.show_target_labels,
            "map_contours": self.radar_widget.show_map_contours,
        }
        for option, button in self.overlay_toggle_buttons.items():
            active = bool(states.get(option, False))
            button.blockSignals(True)
            button.setChecked(active)
            button.blockSignals(False)
            self._style_toggle_button(button, active)

    def _style_toggle_button(self, button: QPushButton, active: bool) -> None:
        if active:
            button.setStyleSheet(
                "color: #9be89b; background: #051805; border: 1px solid #2f8b2f; padding: 2px 6px;"
            )
        else:
            button.setStyleSheet(
                "color: #5b9e5b; background: #000000; border: 1px solid #225522; padding: 2px 6px;"
            )

    def _style_status_label(self, label: QLabel, active: bool) -> None:
        if active:
            label.setStyleSheet(
                "color: #9be89b; background: #051805; border: 1px solid #2f8b2f; padding: 2px 6px;"
            )
        else:
            label.setStyleSheet(
                "color: #5b9e5b; background: #000000; border: 1px solid #225522; padding: 2px 6px;"
            )

    def _sync_scan_labels(self) -> None:
        for scan in SCAN_ORDER:
            label = self.scan_labels[scan]
            active = scan in self.current_scanner_scan
            self._style_status_label(label, active)
        radio_active = self.backend_reachable and self.radio_connected
        self.radio_status_label.setText("Radio")
        self._style_status_label(self.radio_status_label, radio_active)

    def on_zoom_in(self) -> None:
        self.radar_widget.zoom_in()
        self._sync_range_input()

    def on_zoom_out(self) -> None:
        self.radar_widget.zoom_out()
        self._sync_range_input()

    def on_zoom_reset(self) -> None:
        self.radar_widget.set_home(self.radar_widget.home_lat, self.radar_widget.home_lon)
        self.radar_widget.set_range_km(self.config.default_range_km)
        self._sync_range_input()

    def on_range_input_changed(self) -> None:
        raw = self.range_input.text().strip().replace(",", ".")
        try:
            value = float(raw)
        except ValueError:
            self._sync_range_input()
            return
        self.radar_widget.set_range_km(value)
        self._sync_range_input()

    def _sync_range_input(self) -> None:
        self.range_input.setText(f"{self.radar_widget.state.range_km:.2f}")

    def on_show_fixed_names_changed(self, checked: bool) -> None:
        self.radar_widget.show_fixed_names = checked
        self._sync_overlay_toggle_buttons()
        self.radar_widget.update()

    def on_show_target_labels_changed(self, checked: bool) -> None:
        self.radar_widget.show_target_labels = checked
        self._sync_overlay_toggle_buttons()
        self.radar_widget.update()

    def on_show_map_contours_changed(self, checked: bool) -> None:
        self.radar_widget.show_map_contours = checked
        self._sync_overlay_toggle_buttons()
        self.radar_widget.update()
        if checked:
            self.load_map_contours(force=True)
        else:
            self.map_retry_timer.stop()

    def on_target_type_filter_changed(self, selected: str, checked: bool) -> None:
        if selected == "stopped":
            self.radar_widget.show_stopped = checked
        elif selected == "aircraft":
            self.radar_widget.show_aircraft = checked
        elif selected == "vessel":
            self.radar_widget.show_vessel = checked
        self._sync_target_type_filter_buttons()
        self.radar_widget.update()
        self._refresh_target_lists()

    def on_visible_item_clicked(self, item: QListWidgetItem) -> None:
        target_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if target_id:
            self._handle_list_item_clicked(target_id, source="visible")

    def on_outside_item_clicked(self, item: QListWidgetItem) -> None:
        target_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if target_id:
            self._handle_list_item_clicked(target_id, source="outside")

    def on_target_selected(self, target_id: str) -> None:
        self.select_target(target_id, fit=False)

    def _find_target_by_id(self, target_id: str) -> dict[str, Any] | None:
        return next((item for item in self.current_targets if str(item.get("target_id", "")) == target_id), None)

    def _show_target_details_dialog(self, target: dict[str, Any]) -> None:
        target_id = str(target.get("target_id") or "okant")
        LOGGER.warning("QT dialog response: open target_id=%s", target_id)
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Objektdetaljer - {target_id}")
        dialog.resize(640, 520)

        layout = QVBoxLayout(dialog)
        text_view = QPlainTextEdit(dialog)
        text_view.setReadOnly(True)
        text_view.setPlainText(json.dumps(target, ensure_ascii=False, indent=2, sort_keys=True))
        layout.addWidget(text_view, stretch=1)

        close_button = QPushButton("Stang", dialog)
        close_button.clicked.connect(dialog.accept)
        layout.addWidget(close_button, alignment=Qt.AlignmentFlag.AlignRight)
        dialog.exec()
        LOGGER.warning("QT dialog response: closed target_id=%s", target_id)

    def _handle_list_item_clicked(self, target_id: str, *, source: str) -> None:
        LOGGER.warning("QT dialog request: source=%s target_id=%s", source, target_id)
        target = self._find_target_by_id(target_id)
        if target is None:
            LOGGER.warning("QT dialog response: target_id=%s not found in current_targets", target_id)
            return
        is_visible = self.radar_widget._is_target_visible(target)
        LOGGER.warning(
            "QT dialog response: target_id=%s found visible_in_active_view=%s",
            target_id,
            is_visible,
        )
        self.select_target(target_id, fit=False)
        self._show_target_details_dialog(target)

    def on_view_changed(self, _lat: float, _lon: float, _range: float) -> None:
        if abs(float(_range) - self._last_view_range_km) > 1e-6:
            self._last_view_range_km = float(_range)
            # Clear current contour render immediately when zoom changes,
            # then force a full re-render of all layers for the new view scale.
            self.map_loaded_key = None
            self.map_pending_key = None
            self.map_retry_timer.stop()
            self.radar_widget.set_map_segments([])
            self.radar_widget.set_fixed_objects(list(self.radar_widget.fixed_objects))
            self.radar_widget.set_targets(list(self.current_targets))
            self.schedule_map_contours(force=True)
        self._refresh_target_lists()
        self.schedule_map_contours()

    def select_target(self, target_id: str, *, fit: bool) -> None:
        self.radar_widget.set_selected_target(target_id)
        if fit:
            target = next((item for item in self.current_targets if item.get("target_id") == target_id), None)
            if target and target.get("lat") is not None and target.get("lon") is not None:
                self.radar_widget.state.center_lat = float(target["lat"])
                self.radar_widget.state.center_lon = float(target["lon"])
                self.radar_widget.view_changed.emit(
                    self.radar_widget.state.center_lat,
                    self.radar_widget.state.center_lon,
                    self.radar_widget.state.range_km,
                )
        self.radar_widget.update()

    def _target_label(self, target: dict[str, Any]) -> str:
        label = str(target.get("label") or target.get("target_id") or "okant")
        parts = [label]

        if target.get("speed") is not None:
            try:
                speed_value = float(target["speed"])
                if math.isfinite(speed_value):
                    parts.append(f"speed {speed_value:.1f}")
            except (TypeError, ValueError):
                pass

        if target.get("altitude") is not None:
            try:
                altitude_value = float(target["altitude"])
                if math.isfinite(altitude_value):
                    parts.append(f"alt {altitude_value:.0f}")
            except (TypeError, ValueError):
                pass

        return " | ".join(parts)

    def _refresh_target_lists(self) -> None:
        visible, outside = self.radar_widget.filtered_targets()
        self.objects_summary_label.setText(f"{len(visible)} synliga objekt")
        self.outside_summary_label.setText(f"{len(outside)} objekt utanfor aktivt omrade")

        self.visible_objects_list.clear()
        for target in visible:
            item = QListWidgetItem(self._target_label(target))
            item.setData(Qt.ItemDataRole.UserRole, target.get("target_id"))
            self.visible_objects_list.addItem(item)

        self.outside_objects_list.clear()
        for target in outside:
            item = QListWidgetItem(self._target_label(target))
            item.setData(Qt.ItemDataRole.UserRole, target.get("target_id"))
            self.outside_objects_list.addItem(item)

    def _request_json(self, path: str, *, params: dict[str, Any] | None, on_success, on_error) -> None:
        url = build_api_url(self.config.backend_base_url, path, params=params)
        LOGGER.info("QT REST request: GET %s", url)
        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(b"Accept", b"application/json")
        request.setTransferTimeout(self.config.request_timeout_ms)
        reply = self.network.get(request)

        def _finished() -> None:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                LOGGER.error(
                    "QT REST error: GET %s failed: %s",
                    url,
                    reply.errorString(),
                )
                on_error(reply.errorString())
                reply.deleteLater()
                return
            raw = bytes(reply.readAll())
            status_code = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            raw_text = raw.decode("utf-8", errors="replace")
            if len(raw_text) > 4000:
                raw_text = f"{raw_text[:4000]}...<truncated>"
            LOGGER.info(
                "QT REST response: GET %s status=%s body=%s",
                url,
                status_code,
                raw_text,
            )
            reply.deleteLater()
            try:
                on_success(json.loads(raw.decode("utf-8")))
            except Exception as exc:
                LOGGER.exception("QT REST parse error for %s", url)
                on_error(f"Invalid JSON response: {exc}")

        reply.finished.connect(_finished)

    def start(self) -> None:
        self.load_live_ui_config()
        self.live_config_timer.start()
        self.poll_timer.start()
        self.load_targets()

    def load_live_ui_config(self) -> None:
        def _on_success(payload: dict[str, Any]) -> None:
            self.backend_reachable = True
            parsed = parse_live_ui_config(payload)
            self.service_name = parsed.service_name
            self.default_map_source = self.config.map_source or parsed.default_map_source
            self.setWindowTitle(f"{self.config.window_title} - {self.service_name}")
            self.radar_widget.set_home(parsed.center_lat, parsed.center_lon)
            self.radar_widget.set_fixed_objects(list(parsed.fixed_objects))
            self._sync_scan_labels()
            self.schedule_map_contours()

        def _on_error(message: str) -> None:
            self.backend_reachable = False
            self.radio_connected = False
            self._sync_scan_labels()
            self.statusBar().showMessage(f"/ui/live-config unavailable: {message}", 5000)

        self._request_json("/ui/live-config", params=None, on_success=_on_success, on_error=_on_error)

    def load_targets(self) -> None:
        def _on_success(payload: dict[str, Any]) -> None:
            self.backend_reachable = True
            self.radio_connected = bool(payload.get("radio_connected"))
            targets = payload.get("targets", [])
            self.current_targets = [item for item in targets if isinstance(item, dict)] if isinstance(targets, list) else []
            self.radar_widget.set_targets(self.current_targets)

            scanner = payload.get("scanner")
            if isinstance(scanner, dict):
                scan = scanner.get("scan", [])
                if isinstance(scan, list):
                    selected: list[str] = []
                    for value in scan:
                        item = str(value).strip().upper()
                        if item in SCAN_ORDER and item not in selected:
                            selected.append(item)
                    self.current_scanner_scan = selected
                    self._sync_scan_labels()

            self._update_reception_warning(payload.get("reception_status"))
            self._refresh_target_lists()
            self._sync_scan_labels()
            self.schedule_map_contours()

        def _on_error(message: str) -> None:
            self.backend_reachable = False
            self.radio_connected = False
            self._sync_scan_labels()
            self.statusBar().showMessage(f"Failed to load /ui/targets-latest: {message}", 3000)

        self._request_json("/ui/targets-latest", params=None, on_success=_on_success, on_error=_on_error)

    def _update_reception_warning(self, status_payload: Any) -> None:
        if not isinstance(status_payload, dict):
            self.reception_warning_label.setText("")
            return
        threshold_hours = status_payload.get("threshold_hours")
        adsb_last = status_payload.get("adsb_last_position_at")
        ais_last = status_payload.get("ais_last_position_at")
        try:
            threshold = float(threshold_hours)
        except (TypeError, ValueError):
            threshold = 2.0

        if adsb_last in {None, ""} and ais_last in {None, ""}:
            self.reception_warning_label.setText(
                f"Varning: ingen positionsdata fran AIS eller ADS-B senaste {threshold:.0f} timmarna."
            )
        else:
            self.reception_warning_label.setText("")

    def _current_bbox(self) -> tuple[float, float, float, float]:
        lat_padding = self.radar_widget.state.range_km / KM_PER_DEG_LAT
        lon_padding = self.radar_widget.state.range_km / self.radar_widget._km_per_deg_lon(
            self.radar_widget.state.center_lat
        )
        return (
            self.radar_widget.state.center_lon - lon_padding,
            self.radar_widget.state.center_lat - lat_padding,
            self.radar_widget.state.center_lon + lon_padding,
            self.radar_widget.state.center_lat + lat_padding,
        )

    def _map_request_key(self) -> str:
        bbox = self._current_bbox()
        viewport_key = f"{self.radar_widget.width()}x{self.radar_widget.height()}"
        return f"{self.default_map_source}|{','.join(f'{value:.4f}' for value in bbox)}|{viewport_key}"

    def _zoom_level_for_range_km(self, range_km: float) -> int:
        clamped_range = max(MIN_RANGE_KM, min(500.0, float(range_km)))
        # Approximate slippy-map style zoom where tile width tracks current range.
        raw_zoom = math.log2(40075.0 / (max(0.1, clamped_range) * 2.0))
        return max(4, min(14, int(round(raw_zoom))))

    def _tile_size_degrees(self, zoom_level: int) -> tuple[float, float]:
        scale = float(2**zoom_level)
        return (360.0 / scale, 180.0 / scale)

    def _tile_bbox(self, zoom_level: int, tile_x: int, tile_y: int) -> tuple[float, float, float, float]:
        lon_step, lat_step = self._tile_size_degrees(zoom_level)
        min_lon = -180.0 + (tile_x * lon_step)
        min_lat = -90.0 + (tile_y * lat_step)
        max_lon = min_lon + lon_step
        max_lat = min_lat + lat_step
        return (min_lon, min_lat, max_lon, max_lat)

    def _tiles_for_bbox(self, bbox: tuple[float, float, float, float], zoom_level: int) -> list[tuple[int, int]]:
        min_lon, min_lat, max_lon, max_lat = bbox
        lon_step, lat_step = self._tile_size_degrees(zoom_level)
        x_count = int(round(360.0 / lon_step))
        y_count = int(round(180.0 / lat_step))

        min_x = max(0, min(x_count - 1, int(math.floor((min_lon + 180.0) / lon_step))))
        max_x = max(0, min(x_count - 1, int(math.floor((max_lon + 180.0) / lon_step))))
        min_y = max(0, min(y_count - 1, int(math.floor((min_lat + 90.0) / lat_step))))
        max_y = max(0, min(y_count - 1, int(math.floor((max_lat + 90.0) / lat_step))))

        keys: list[tuple[int, int]] = []
        for tile_x in range(min_x, max_x + 1):
            for tile_y in range(min_y, max_y + 1):
                keys.append((tile_x, tile_y))
        return keys

    def _feature_list_from_payload(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        raw_features = payload.get("features", [])
        if not isinstance(raw_features, list):
            return []
        return [feature for feature in raw_features if isinstance(feature, dict)]

    def _dedupe_features(self, features: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for feature in features:
            try:
                key = json.dumps(feature, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            except (TypeError, ValueError):
                continue
            if key in seen:
                continue
            seen.add(key)
            deduped.append(feature)
        return deduped

    def _schedule_map_contour_retry(self, delay_ms: int) -> None:
        self.map_retry_timer.start(max(50, int(delay_ms)))

    def _on_map_retry_timeout(self) -> None:
        self.load_map_contours(force=True)

    def schedule_map_contours(self, *, delay_ms: int = 200, force: bool = False) -> None:
        if not self.radar_widget.show_map_contours:
            return
        if not force and self.map_pending_key == self._map_request_key():
            return
        self._schedule_map_contour_retry(delay_ms)

    def load_map_contours(self, *, force: bool = False) -> None:
        if not self.radar_widget.show_map_contours:
            return
        if self.map_in_flight:
            self.map_refresh_pending = True
            return

        request_key = self._map_request_key()
        if request_key == self.map_loaded_key:
            return
        if not force and request_key == self.map_pending_key:
            return

        bbox = self._current_bbox()
        zoom_level = self._zoom_level_for_range_km(self.radar_widget.state.range_km)
        tile_keys = self._tiles_for_bbox(bbox, zoom_level)

        cached_features: list[dict[str, Any]] = []
        missing_tiles: list[tuple[int, int]] = []
        for tile_x, tile_y in tile_keys:
            features = self.map_cache.get_tile_features(
                source=self.default_map_source,
                zoom_level=zoom_level,
                tile_x=tile_x,
                tile_y=tile_y,
            )
            if features is None:
                missing_tiles.append((tile_x, tile_y))
            else:
                cached_features.extend(features)
        cached_features = self._dedupe_features(cached_features)

        def _finalize(features: list[dict[str, Any]], *, status: str, poll_after_seconds: float | None = None) -> None:
            self.map_in_flight = False
            if request_key != self._map_request_key():
                self.map_refresh_pending = True
            else:
                self.radar_widget.set_map_segments(self._extract_map_segments({"features": features}))
                if status == "ok":
                    self.map_loaded_key = request_key
                    self.map_pending_key = None
                elif status == "pending":
                    self.map_loaded_key = None
                    self.map_pending_key = request_key
                    retry_delay_ms = 750
                    if poll_after_seconds is not None:
                        retry_delay_ms = int(max(0.05, poll_after_seconds) * 1000.0)
                    self._schedule_map_contour_retry(retry_delay_ms)
                else:
                    self.map_loaded_key = None
                    self.map_pending_key = None
            if self.map_refresh_pending:
                self.map_refresh_pending = False
                self.schedule_map_contours(force=True)

        if not missing_tiles:
            _finalize(cached_features, status="ok")
            return

        self.map_in_flight = True
        requested_tile: tuple[int, int] | None = None
        request_bbox = bbox
        if missing_tiles:
            requested_tile = missing_tiles[0]
            request_bbox = self._tile_bbox(zoom_level, requested_tile[0], requested_tile[1])

        params = {
            "bbox": ",".join(f"{value:.6f}" for value in request_bbox),
            "range_km": f"{self.radar_widget.state.range_km:.4f}",
            "source": self.default_map_source,
        }

        def _on_success(payload: dict[str, Any]) -> None:
            status_raw = payload.get("status")
            status = status_raw if isinstance(status_raw, str) else "ok"
            features = self._feature_list_from_payload(payload)
            merged_features = self._dedupe_features(cached_features + features)

            poll_after_seconds: float | None = None
            details = payload.get("details")
            if isinstance(details, dict):
                poll_after = details.get("poll_after_seconds")
                if isinstance(poll_after, (int, float)):
                    poll_after_seconds = float(poll_after)

            if status == "ok":
                if requested_tile is not None:
                    self.map_cache.upsert_tile_features(
                        source=self.default_map_source,
                        zoom_level=zoom_level,
                        tile_x=requested_tile[0],
                        tile_y=requested_tile[1],
                        features=features,
                    )
                # Progressively render tile-by-tile: keep polling quickly
                # until all tiles are filled for this view request.
                if len(missing_tiles) > 1:
                    _finalize(merged_features, status="pending", poll_after_seconds=0.05)
                else:
                    _finalize(merged_features, status="ok")
                return

            if status == "pending":
                _finalize(merged_features, status="pending", poll_after_seconds=poll_after_seconds)
                return

            self.statusBar().showMessage(f"Map contours unavailable ({status}).", 3000)
            _finalize(cached_features, status=status)

        def _on_error(message: str) -> None:
            self.statusBar().showMessage(f"Failed to load /ui/map-contours: {message}", 3000)
            _finalize(cached_features, status="error")

        self._request_json("/ui/map-contours", params=params, on_success=_on_success, on_error=_on_error)

    def _extract_map_segments(self, payload: dict[str, Any]) -> list[tuple[QPointF, QPointF]]:
        features = payload.get("features", [])
        if not isinstance(features, list):
            return []

        width = float(max(1, self.radar_widget.width()))
        height = float(max(1, self.radar_widget.height()))
        cx = width / 2.0
        cy = height / 2.0
        radius = max(30.0, min(width, height) * 0.45)
        px_per_km = radius / self.radar_widget.state.range_km

        segments: list[tuple[QPointF, QPointF]] = []

        def _walk_coords(coords: Any) -> None:
            if not isinstance(coords, list) or len(coords) < 2:
                return
            if isinstance(coords[0], (int, float)):
                return
            if isinstance(coords[0], list) and len(coords[0]) >= 2 and isinstance(coords[0][0], (int, float)):
                previous: QPointF | None = None
                for pair in coords:
                    if not isinstance(pair, list) or len(pair) < 2:
                        continue
                    try:
                        lon = float(pair[0])
                        lat = float(pair[1])
                    except (TypeError, ValueError):
                        continue
                    point = self.radar_widget._latlon_to_xy(lat, lon, cx, cy, px_per_km)
                    if previous is not None:
                        segments.append((previous, point))
                    previous = point
                return
            for item in coords:
                _walk_coords(item)

        for feature in features:
            if not isinstance(feature, dict):
                continue
            geometry = feature.get("geometry")
            if not isinstance(geometry, dict):
                continue
            _walk_coords(geometry.get("coordinates"))

        return segments



def run_native_live_view(config: QtLiveViewConfig) -> int:
    app = QApplication([])
    window = LiveRadarWindow(config)
    window.show()
    window.start()
    return app.exec()
