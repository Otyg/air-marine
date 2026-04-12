"""Native Qt UI implementation for live radar client."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
from typing import Any

from PySide6.QtCore import QPointF, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
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

SCAN_ORDER = ("AIS", "ADS", "FLARM")
RADAR_RING_COUNT = 5
DEFAULT_MAP_CACHE_DB_PATH = Path("./data/qt_map_contours.sqlite")


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

    def set_home(self, lat: float, lon: float) -> None:
        self.home_lat = lat
        self.home_lon = lon
        self.state.center_lat = lat
        self.state.center_lon = lon
        self.view_changed.emit(self.state.center_lat, self.state.center_lon, self.state.range_km)
        self.update()

    def set_targets(self, targets: list[dict[str, Any]]) -> None:
        self.targets = targets
        self.update()

    def set_fixed_objects(self, fixed_objects: list[dict[str, Any]]) -> None:
        self.fixed_objects = fixed_objects
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
        kind = str(target.get("kind", "")).lower()
        if kind == "vessel":
            return QColor("#8fd3ff")
        return QColor("#c1f5c1")

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
            painter.setPen(QPen(QColor("#1b5e8b"), 1))
            for start, end in self.map_segments:
                painter.drawLine(start, end)

        painter.setPen(QPen(QColor("#8fd3ff"), 1))
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
            symbol = str(fixed.get("symbol", "O"))[:1] or "O"
            painter.drawText(point, symbol)
            if self.show_fixed_names:
                painter.drawText(point + QPointF(6, -4), str(fixed.get("name", "")))

        visible_targets, _outside_targets = self.filtered_targets()
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
            painter.setBrush(color)
            painter.drawEllipse(point, 3.5, 3.5)

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

        self.map_retry_timer = QTimer(self)
        self.map_retry_timer.setSingleShot(True)
        self.map_retry_timer.timeout.connect(self.load_map_contours)

        self.service_name = "sdr-monitor"
        self.default_map_source = config.map_source
        self.current_targets: list[dict[str, Any]] = []
        self.current_scanner_scan: list[str] = ["AIS", "ADS"]
        self.map_loaded_key: str | None = None
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
            "FLARM": QLabel("FLARM"),
        }

        self.show_fixed_names_checkbox = QCheckBox("Visa namn fasta punkter")
        self.show_fixed_names_checkbox.setChecked(config.show_fixed_names)
        self.show_fixed_names_checkbox.toggled.connect(self.on_show_fixed_names_changed)

        self.show_target_labels_checkbox = QCheckBox("Visa labels objekt")
        self.show_target_labels_checkbox.setChecked(config.show_target_labels)
        self.show_target_labels_checkbox.toggled.connect(self.on_show_target_labels_changed)

        self.show_map_contours_checkbox = QCheckBox("Visa kust/sjo-konturer")
        self.show_map_contours_checkbox.setChecked(config.show_map_contours)
        self.show_map_contours_checkbox.toggled.connect(self.on_show_map_contours_changed)

        self.target_type_filter_buttons: dict[str, QPushButton] = {}
        for value, label in (("stopped", "Stoppade"), ("aircraft", "Flygplan"), ("vessel", "Batar")):
            button = QPushButton(label)
            button.setCheckable(True)
            button.toggled.connect(
                lambda checked, selected=value: self.on_target_type_filter_changed(selected, checked)
            )
            self.target_type_filter_buttons[value] = button
        self._sync_target_type_filter_buttons()

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

        zoom_out_btn = QPushButton("-")
        zoom_out_btn.setFixedWidth(34)
        zoom_out_btn.clicked.connect(self.on_zoom_out)
        top_bar.addWidget(zoom_out_btn)

        top_bar.addWidget(self.range_input)

        zoom_in_btn = QPushButton("+")
        zoom_in_btn.setFixedWidth(34)
        zoom_in_btn.clicked.connect(self.on_zoom_in)
        top_bar.addWidget(zoom_in_btn)

        zoom_reset_btn = QPushButton("Hem")
        zoom_reset_btn.clicked.connect(self.on_zoom_reset)
        top_bar.addWidget(zoom_reset_btn)

        for value in SCAN_ORDER:
            top_bar.addWidget(self.scan_labels[value])

        top_bar.addWidget(self.show_fixed_names_checkbox)
        top_bar.addWidget(self.show_target_labels_checkbox)
        top_bar.addWidget(self.show_map_contours_checkbox)
        top_bar.addStretch(1)

        root_layout.addLayout(top_bar)
        root_layout.addWidget(self.reception_warning_label)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.radar_widget)

        side_panel = QWidget()
        side_layout = QVBoxLayout(side_panel)
        side_layout.setContentsMargins(8, 8, 8, 8)
        side_layout.setSpacing(8)

        side_layout.addWidget(QLabel("Typ"))
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
              font-family: Courier New, monospace;
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
            if active:
                button.setStyleSheet("color: #9be89b; border: 1px solid #2f8b2f; padding: 2px 6px;")
            else:
                button.setStyleSheet("color: #5b9e5b; border: 1px solid #225522; padding: 2px 6px;")

    def _sync_scan_labels(self) -> None:
        for scan in SCAN_ORDER:
            label = self.scan_labels[scan]
            active = scan in self.current_scanner_scan
            if active:
                label.setStyleSheet("color: #9be89b; border: 1px solid #2f8b2f; padding: 2px 6px;")
            else:
                label.setStyleSheet("color: #5b9e5b; border: 1px solid #225522; padding: 2px 6px;")

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
        self.radar_widget.update()

    def on_show_target_labels_changed(self, checked: bool) -> None:
        self.radar_widget.show_target_labels = checked
        self.radar_widget.update()

    def on_show_map_contours_changed(self, checked: bool) -> None:
        self.radar_widget.show_map_contours = checked
        self.radar_widget.update()
        if checked:
            self.load_map_contours()

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
            self.select_target(target_id, fit=True)

    def on_outside_item_clicked(self, item: QListWidgetItem) -> None:
        target_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if target_id:
            self.select_target(target_id, fit=True)

    def on_target_selected(self, target_id: str) -> None:
        self.select_target(target_id, fit=False)

    def on_view_changed(self, _lat: float, _lon: float, _range: float) -> None:
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
        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(b"Accept", b"application/json")
        request.setTransferTimeout(self.config.request_timeout_ms)
        reply = self.network.get(request)

        def _finished() -> None:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                on_error(reply.errorString())
                reply.deleteLater()
                return
            raw = bytes(reply.readAll())
            reply.deleteLater()
            try:
                on_success(json.loads(raw.decode("utf-8")))
            except Exception as exc:
                on_error(f"Invalid JSON response: {exc}")

        reply.finished.connect(_finished)

    def start(self) -> None:
        self.load_live_ui_config()
        self.poll_timer.start()
        self.load_targets()

    def load_live_ui_config(self) -> None:
        def _on_success(payload: dict[str, Any]) -> None:
            parsed = parse_live_ui_config(payload)
            self.service_name = parsed.service_name
            self.default_map_source = self.config.map_source or parsed.default_map_source
            self.setWindowTitle(f"{self.config.window_title} - {self.service_name}")
            self.radar_widget.set_home(parsed.center_lat, parsed.center_lon)
            self.radar_widget.set_fixed_objects(list(parsed.fixed_objects))
            self.schedule_map_contours()

        def _on_error(message: str) -> None:
            self.statusBar().showMessage(f"/ui/live-config unavailable: {message}", 5000)

        self._request_json("/ui/live-config", params=None, on_success=_on_success, on_error=_on_error)

    def load_targets(self) -> None:
        def _on_success(payload: dict[str, Any]) -> None:
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
            self.schedule_map_contours()

        def _on_error(message: str) -> None:
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
        return f"{self.default_map_source}|{','.join(f'{value:.4f}' for value in bbox)}"

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

    def schedule_map_contours(self) -> None:
        if not self.radar_widget.show_map_contours:
            return
        self.map_retry_timer.start(200)

    def load_map_contours(self) -> None:
        if not self.radar_widget.show_map_contours:
            return
        if self.map_in_flight:
            self.map_refresh_pending = True
            return

        request_key = self._map_request_key()
        if request_key == self.map_loaded_key:
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

        def _finalize(features: list[dict[str, Any]]) -> None:
            self.map_in_flight = False
            if request_key != self._map_request_key():
                self.map_refresh_pending = True
            else:
                self.map_loaded_key = request_key
                self.radar_widget.set_map_segments(self._extract_map_segments({"features": features}))
            if self.map_refresh_pending:
                self.map_refresh_pending = False
                self.schedule_map_contours()

        if not missing_tiles:
            _finalize(cached_features)
            return

        self.map_in_flight = True
        fetched_features = list(cached_features)

        def _fetch_missing(index: int) -> None:
            if index >= len(missing_tiles):
                _finalize(fetched_features)
                return

            tile_x, tile_y = missing_tiles[index]
            tile_bbox = self._tile_bbox(zoom_level, tile_x, tile_y)
            params = {
                "bbox": ",".join(f"{value:.6f}" for value in tile_bbox),
                "range_km": f"{self.radar_widget.state.range_km:.4f}",
                "source": self.default_map_source,
            }

            def _on_success(payload: dict[str, Any]) -> None:
                features = self._feature_list_from_payload(payload)
                self.map_cache.upsert_tile_features(
                    source=self.default_map_source,
                    zoom_level=zoom_level,
                    tile_x=tile_x,
                    tile_y=tile_y,
                    features=features,
                )
                fetched_features.extend(features)
                _fetch_missing(index + 1)

            def _on_error(message: str) -> None:
                self.statusBar().showMessage(f"Failed to load /ui/map-contours: {message}", 3000)
                _fetch_missing(index + 1)

            self._request_json("/ui/map-contours", params=params, on_success=_on_success, on_error=_on_error)

        _fetch_missing(0)

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
