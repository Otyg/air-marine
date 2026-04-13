"""Config and launcher for native Qt live radar client."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse


KM_PER_DEG_LAT = 110.574
MIN_RANGE_KM = 0.2
MAX_RANGE_KM = 500.0
DEFAULT_POLL_INTERVAL_MS = 5000
DEFAULT_TIMEOUT_MS = 6000
DEFAULT_WINDOW_TITLE = "SDR Monitor Live Radar"
DEFAULT_CONFIG_PATH = Path("./qt_client/config.json")
DEFAULT_CONFIG_TEMPLATE = Path("./qt_client/config.example.json")
DEFAULT_TRAIL_POINT_WINDOW_SECONDS = 120.0
DEFAULT_MARKER_SIZE_SCALE = 1.0


@dataclass(frozen=True, slots=True)
class QtLiveViewConfig:
    backend_base_url: str
    window_title: str = DEFAULT_WINDOW_TITLE
    service_name: str = "sdr-monitor"
    config_path: str = str(DEFAULT_CONFIG_PATH)
    window_width: int = 1400
    window_height: int = 900
    poll_interval_ms: int = DEFAULT_POLL_INTERVAL_MS
    request_timeout_ms: int = DEFAULT_TIMEOUT_MS
    default_range_km: float = 10.0
    show_target_labels: bool = False
    show_fixed_names: bool = True
    show_map_contours: bool = True
    show_low_speed: bool = False
    target_type_filter: str = "all"
    map_source: str = "hydro"
    fallback_center_lat: float = 0.0
    fallback_center_lon: float = 0.0
    trail_point_window_seconds: float = DEFAULT_TRAIL_POINT_WINDOW_SECONDS
    marker_size_scale: float = DEFAULT_MARKER_SIZE_SCALE
    fixed_marker_size_scale: float = DEFAULT_MARKER_SIZE_SCALE
    vessel_symbol_box_factor: float = 0.82
    fixed_objects: tuple[dict[str, Any], ...] = ()
    use_backend_live_config: bool = False


@dataclass(frozen=True, slots=True)
class LiveUIConfig:
    service_name: str
    center_lat: float
    center_lon: float
    fixed_objects: tuple[dict[str, Any], ...] = ()
    default_map_source: str = "hydro"


@dataclass(slots=True)
class ViewState:
    center_lat: float
    center_lon: float
    range_km: float



def normalize_backend_base_url(raw_value: str) -> str:
    value = str(raw_value).strip()
    if not value:
        raise ValueError("backend_base_url cannot be empty")

    if "://" not in value:
        value = f"http://{value}"

    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("backend_base_url must use http or https")
    if not parsed.netloc:
        raise ValueError("backend_base_url must include host")

    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}"



def build_api_url(base_url: str, path: str, params: dict[str, Any] | None = None) -> str:
    normalized_base = normalize_backend_base_url(base_url)
    normalized_path = "/" + str(path).lstrip("/")
    if params:
        return f"{normalized_base}{normalized_path}?{urlencode(params)}"
    return f"{normalized_base}{normalized_path}"



def _to_int(payload: dict[str, Any], key: str, default: int, *, minimum: int) -> int:
    value = payload.get(key, default)
    try:
        resolved = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if resolved < minimum:
        raise ValueError(f"{key} must be >= {minimum}")
    return resolved



def _to_float(payload: dict[str, Any], key: str, default: float) -> float:
    value = payload.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be numeric") from exc



def _to_bool(payload: dict[str, Any], key: str, default: bool) -> bool:
    value = payload.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"{key} must be a boolean")



def _to_filter(payload: dict[str, Any], key: str, default: str) -> str:
    value = str(payload.get(key, default)).strip().lower()
    if value not in {"all", "aircraft", "vessel", "stopped"}:
        raise ValueError(f"{key} must be one of: all, stopped, aircraft, vessel")
    return value



def _to_map_source(payload: dict[str, Any], key: str, default: str) -> str:
    value = str(payload.get(key, default)).strip().lower()
    if value not in {"hydro", "elevation"}:
        raise ValueError(f"{key} must be one of: hydro, elevation")
    return value



def load_qt_live_view_config(config_path: Path) -> QtLiveViewConfig:
    if not config_path.exists():
        raise FileNotFoundError(
            f"Qt client config not found: {config_path}. "
            f"Create it from {DEFAULT_CONFIG_TEMPLATE}."
        )

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Qt client config root must be a JSON object")
    fixed_objects_payload = payload.get("fixed_objects", [])
    if not isinstance(fixed_objects_payload, list):
        raise ValueError("fixed_objects must be a JSON array")

    backend_base_url = normalize_backend_base_url(str(payload.get("backend_base_url", "")))
    config = QtLiveViewConfig(
        backend_base_url=backend_base_url,
        window_title=str(payload.get("window_title", DEFAULT_WINDOW_TITLE)).strip() or DEFAULT_WINDOW_TITLE,
        service_name=str(payload.get("service_name", "sdr-monitor")).strip() or "sdr-monitor",
        config_path=str(config_path),
        window_width=_to_int(payload, "window_width", 1400, minimum=640),
        window_height=_to_int(payload, "window_height", 900, minimum=480),
        poll_interval_ms=_to_int(payload, "poll_interval_ms", DEFAULT_POLL_INTERVAL_MS, minimum=1000),
        request_timeout_ms=_to_int(payload, "request_timeout_ms", DEFAULT_TIMEOUT_MS, minimum=1000),
        default_range_km=_to_float(payload, "default_range_km", 10.0),
        show_target_labels=_to_bool(payload, "show_target_labels", False),
        show_fixed_names=_to_bool(payload, "show_fixed_names", True),
        show_map_contours=_to_bool(payload, "show_map_contours", True),
        show_low_speed=_to_bool(payload, "show_low_speed", False),
        target_type_filter=_to_filter(payload, "target_type_filter", "all"),
        map_source=_to_map_source(payload, "map_source", "hydro"),
        fallback_center_lat=_to_float(payload, "fallback_center_lat", 0.0),
        fallback_center_lon=_to_float(payload, "fallback_center_lon", 0.0),
        trail_point_window_seconds=_to_float(
            payload,
            "trail_point_window_seconds",
            DEFAULT_TRAIL_POINT_WINDOW_SECONDS,
        ),
        marker_size_scale=_to_float(
            payload,
            "marker_size_scale",
            DEFAULT_MARKER_SIZE_SCALE,
        ),
        fixed_marker_size_scale=_to_float(
            payload,
            "fixed_marker_size_scale",
            DEFAULT_MARKER_SIZE_SCALE,
        ),
        vessel_symbol_box_factor=_to_float(payload, "vessel_symbol_box_factor", 0.82),
        fixed_objects=tuple(item for item in fixed_objects_payload if isinstance(item, dict)),
        use_backend_live_config=_to_bool(payload, "use_backend_live_config", False),
    )

    if not (MIN_RANGE_KM <= config.default_range_km <= MAX_RANGE_KM):
        raise ValueError(f"default_range_km must be in [{MIN_RANGE_KM}, {MAX_RANGE_KM}]")
    if not (-90.0 <= config.fallback_center_lat <= 90.0):
        raise ValueError("fallback_center_lat must be within -90..90")
    if not (-180.0 <= config.fallback_center_lon <= 180.0):
        raise ValueError("fallback_center_lon must be within -180..180")
    if not (5.0 <= config.trail_point_window_seconds <= 3600.0):
        raise ValueError("trail_point_window_seconds must be within 5..3600")
    if not (0.4 <= config.marker_size_scale <= 4.0):
        raise ValueError("marker_size_scale must be within 0.4..4.0")
    if not (0.4 <= config.fixed_marker_size_scale <= 4.0):
        raise ValueError("fixed_marker_size_scale must be within 0.4..4.0")
    if not (0.5 <= config.vessel_symbol_box_factor <= 1.5):
        raise ValueError("vessel_symbol_box_factor must be within 0.5..1.5")

    return config



def parse_live_ui_config(payload: dict[str, Any]) -> LiveUIConfig:
    fixed_objects_payload = payload.get("fixed_objects", [])
    if not isinstance(fixed_objects_payload, list):
        fixed_objects_payload = []

    service_name = str(payload.get("service_name", "sdr-monitor")).strip() or "sdr-monitor"
    center_lat = float(payload.get("center_lat", 0.0))
    center_lon = float(payload.get("center_lon", 0.0))
    map_source = str(payload.get("default_map_source", "hydro")).strip().lower() or "hydro"
    if map_source not in {"hydro", "elevation"}:
        map_source = "hydro"

    return LiveUIConfig(
        service_name=service_name,
        center_lat=center_lat,
        center_lon=center_lon,
        fixed_objects=tuple(item for item in fixed_objects_payload if isinstance(item, dict)),
        default_map_source=map_source,
    )



def parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Native Qt live radar client")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to Qt client JSON config (default: ./qt_client/config.json)",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Optional override for backend_base_url from config",
    )
    parser.add_argument(
        "--title",
        default=None,
        help="Optional override for window title",
    )
    return parser.parse_args()



def resolve_config(args: argparse.Namespace) -> QtLiveViewConfig:
    config_path = Path(args.config).expanduser()
    config = load_qt_live_view_config(config_path)

    payload = asdict(config)
    if args.base_url:
        payload["backend_base_url"] = normalize_backend_base_url(args.base_url)
    if args.title:
        payload["window_title"] = str(args.title).strip() or config.window_title
    payload["config_path"] = str(config_path)
    return QtLiveViewConfig(**payload)


def qt_live_view_config_to_payload(config: QtLiveViewConfig) -> dict[str, Any]:
    payload = asdict(config)
    payload.pop("config_path", None)
    payload["fixed_objects"] = [dict(item) for item in config.fixed_objects]
    return payload


def save_qt_live_view_config(config: QtLiveViewConfig, *, config_path: Path | None = None) -> None:
    target_path = config_path or Path(config.config_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(
        json.dumps(qt_live_view_config_to_payload(config), ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )



def run_qt_live_view(config: QtLiveViewConfig) -> int:
    try:
        from app.qt_live_view_native import run_native_live_view
    except ImportError as exc:
        raise RuntimeError(
            "PySide6 is not installed. Install it with: pip install -r requirements-qt.txt"
        ) from exc

    return run_native_live_view(config)



def main() -> int:
    args = parse_cli_args()
    config = resolve_config(args)
    return run_qt_live_view(config)


if __name__ == "__main__":
    raise SystemExit(main())
