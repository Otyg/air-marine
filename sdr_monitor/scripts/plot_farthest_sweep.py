"""Create AIS and ADS-B sweep figures for farthest positions around a home point."""

from __future__ import annotations

import argparse
import csv
import math
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import sys
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import load_config
from app.env_utils import load_local_dotenv

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency fallback
    load_dotenv = None

EARTH_RADIUS_KM = 6371.0088
DEFAULT_SOURCES = ("ais", "adsb")
MIN_DISTANCE_KM = 1.0


@dataclass(frozen=True, slots=True)
class SweepPoint:
    bearing_deg: float
    distance_km: float
    target_id: str
    observed_at: str
    lat: float
    lon: float


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * (math.sin(dlambda / 2) ** 2)
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return EARTH_RADIUS_KM * c


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)

    y = math.sin(dlambda) * math.cos(phi2)
    x = (math.cos(phi1) * math.sin(phi2)) - (
        math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    )
    bearing = math.degrees(math.atan2(y, x))
    return bearing % 360.0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compute farthest-position sweep around a home point and write one figure "
            "for AIS and one for ADS-B."
        )
    )
    parser.add_argument(
        "--sqlite-path",
        type=Path,
        default=None,
        help="SQLite path. Defaults to SDR_MONITOR_SQLITE_PATH from environment.",
    )
    parser.add_argument(
        "--home-lat",
        type=float,
        default=None,
        help="Override home latitude. Defaults to SDR_MONITOR_RADAR_CENTER_LAT.",
    )
    parser.add_argument(
        "--home-lon",
        type=float,
        default=None,
        help="Override home longitude. Defaults to SDR_MONITOR_RADAR_CENTER_LON.",
    )
    parser.add_argument(
        "--bin-size-deg",
        type=float,
        default=1.0,
        help="Angular bin size in degrees (default: 1.0).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("./data/reports"),
        help="Output directory for .svg and .csv files (default: ./data/reports).",
    )
    parser.add_argument(
        "--sources",
        nargs="+",
        default=list(DEFAULT_SOURCES),
        help="Sources to include (default: ais adsb).",
    )
    return parser


def _iter_positions(conn: sqlite3.Connection, source: str) -> Iterable[sqlite3.Row]:
    query = """
        SELECT target_id, observed_at, lat, lon
        FROM observations
        WHERE source = ?
          AND lat IS NOT NULL
          AND lon IS NOT NULL
        ORDER BY observed_at ASC
    """
    return conn.execute(query, (source,))


def _compute_sweep(
    rows: Iterable[sqlite3.Row],
    *,
    home_lat: float,
    home_lon: float,
    bin_size_deg: float,
) -> tuple[list[SweepPoint], int, float]:
    bins_count = max(1, int(round(360.0 / bin_size_deg)))
    bin_width = 360.0 / bins_count
    farthest_by_bin: list[SweepPoint | None] = [None] * bins_count

    rows_scanned = 0
    rows_used = 0
    distance_sum_km = 0.0
    for row in rows:
        rows_scanned += 1
        lat = float(row["lat"])
        lon = float(row["lon"])
        distance_km = _haversine_km(home_lat, home_lon, lat, lon)
        if distance_km <= MIN_DISTANCE_KM:
            continue
        rows_used += 1
        distance_sum_km += distance_km
        bearing_deg = _bearing_deg(home_lat, home_lon, lat, lon)
        bin_index = int(bearing_deg // bin_width) % bins_count

        current = farthest_by_bin[bin_index]
        if current is None or distance_km > current.distance_km:
            farthest_by_bin[bin_index] = SweepPoint(
                bearing_deg=bearing_deg,
                distance_km=distance_km,
                target_id=str(row["target_id"]),
                observed_at=str(row["observed_at"]),
                lat=lat,
                lon=lon,
            )

    points = [point for point in farthest_by_bin if point is not None]
    points.sort(key=lambda point: point.bearing_deg)
    mean_distance_km = (distance_sum_km / rows_used) if rows_used > 0 else 0.0
    return points, rows_used, mean_distance_km


def _to_plot_xy(
    *,
    center_x: float,
    center_y: float,
    radius_px: float,
    distance_km: float,
    max_distance_km: float,
    bearing_deg: float,
) -> tuple[float, float]:
    if max_distance_km <= 0:
        scaled = 0.0
    else:
        scaled = (distance_km / max_distance_km) * radius_px
    theta = math.radians(bearing_deg)
    x = center_x + scaled * math.sin(theta)
    y = center_y - scaled * math.cos(theta)
    return x, y


def _write_svg(
    output_path: Path,
    *,
    title: str,
    source: str,
    home_lat: float,
    home_lon: float,
    points: list[SweepPoint],
    rows_scanned: int,
    mean_distance_km: float,
) -> None:
    width = 920
    height = 920
    center_x = width / 2
    center_y = height / 2
    plot_radius = 350

    max_distance_km = max((point.distance_km for point in points), default=1.0)
    rings = 5

    lines: list[str] = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
    )
    lines.append('<rect width="100%" height="100%" fill="#ffffff"/>')
    lines.append(
        '<text x="460" y="44" text-anchor="middle" font-size="24" '
        'font-family="Verdana, sans-serif" fill="#111827">'
        f"{title}"
        "</text>"
    )
    lines.append(
        '<text x="460" y="70" text-anchor="middle" font-size="14" '
        'font-family="Verdana, sans-serif" fill="#4b5563">'
        f"Kalla: {source} | Hempunkt: ({home_lat:.6f}, {home_lon:.6f}) | Positioner: {rows_scanned}"
        "</text>"
    )

    for ring in range(1, rings + 1):
        r = (plot_radius * ring) / rings
        km_value = (max_distance_km * ring) / rings
        lines.append(
            f'<circle cx="{center_x:.2f}" cy="{center_y:.2f}" r="{r:.2f}" '
            'fill="none" stroke="#d1d5db" stroke-width="1"/>'
        )
        lines.append(
            f'<text x="{(center_x + 6):.2f}" y="{(center_y - r - 4):.2f}" '
            'font-size="11" font-family="Verdana, sans-serif" fill="#6b7280">'
            f"{km_value:.1f} km"
            "</text>"
        )

    if mean_distance_km > 0 and max_distance_km > 0:
        mean_radius = (mean_distance_km / max_distance_km) * plot_radius
        lines.append(
            f'<circle cx="{center_x:.2f}" cy="{center_y:.2f}" r="{mean_radius:.2f}" '
            'fill="none" stroke="#2563eb" stroke-width="2" stroke-dasharray="6 5"/>'
        )
        lines.append(
            f'<text x="{(center_x + mean_radius + 8):.2f}" y="{(center_y - 6):.2f}" '
            'font-size="12" font-family="Verdana, sans-serif" fill="#1d4ed8">'
            f"Medelavstånd: {mean_distance_km:.1f} km"
            "</text>"
        )

    for angle in range(0, 360, 30):
        x2, y2 = _to_plot_xy(
            center_x=center_x,
            center_y=center_y,
            radius_px=plot_radius,
            distance_km=max_distance_km,
            max_distance_km=max_distance_km,
            bearing_deg=float(angle),
        )
        lines.append(
            f'<line x1="{center_x:.2f}" y1="{center_y:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
            'stroke="#e5e7eb" stroke-width="1"/>'
        )

    lines.append(
        f'<text x="{center_x:.2f}" y="{(center_y - plot_radius - 16):.2f}" text-anchor="middle" '
        'font-size="14" font-family="Verdana, sans-serif" fill="#111827">N</text>'
    )
    lines.append(
        f'<text x="{(center_x + plot_radius + 12):.2f}" y="{(center_y + 5):.2f}" text-anchor="middle" '
        'font-size="14" font-family="Verdana, sans-serif" fill="#111827">E</text>'
    )
    lines.append(
        f'<text x="{center_x:.2f}" y="{(center_y + plot_radius + 24):.2f}" text-anchor="middle" '
        'font-size="14" font-family="Verdana, sans-serif" fill="#111827">S</text>'
    )
    lines.append(
        f'<text x="{(center_x - plot_radius - 12):.2f}" y="{(center_y + 5):.2f}" text-anchor="middle" '
        'font-size="14" font-family="Verdana, sans-serif" fill="#111827">W</text>'
    )

    plotted_points = [point for point in points if point.distance_km > mean_distance_km]

    plot_points: list[tuple[float, float]] = []
    for point in plotted_points:
        x, y = _to_plot_xy(
            center_x=center_x,
            center_y=center_y,
            radius_px=plot_radius,
            distance_km=point.distance_km,
            max_distance_km=max_distance_km,
            bearing_deg=point.bearing_deg,
        )
        plot_points.append((x, y))

    if plot_points:
        joined = " ".join(f"{x:.2f},{y:.2f}" for x, y in plot_points)
        lines.append(
            f'<polyline points="{joined}" fill="none" stroke="#dc2626" stroke-width="2"/>'
        )

    for x, y in plot_points:
        lines.append(
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2.5" fill="#b91c1c"/>'
        )

    lines.append(
        f'<circle cx="{center_x:.2f}" cy="{center_y:.2f}" r="4" fill="#2563eb" stroke="#1e40af" stroke-width="1"/>'
    )
    lines.append(
        '<text x="24" y="896" font-size="12" font-family="Verdana, sans-serif" fill="#4b5563">'
        f"Farthest-per-bearing bins: {len(points)} | Ritade (utanför medel): {len(plotted_points)}"
        "</text>"
    )
    lines.append("</svg>")

    output_path.write_text("\n".join(lines), encoding="utf-8")


def _write_csv(output_path: Path, points: list[SweepPoint]) -> None:
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "bearing_deg",
                "distance_km",
                "target_id",
                "observed_at",
                "lat",
                "lon",
            ]
        )
        for point in points:
            writer.writerow(
                [
                    f"{point.bearing_deg:.6f}",
                    f"{point.distance_km:.6f}",
                    point.target_id,
                    point.observed_at,
                    f"{point.lat:.7f}",
                    f"{point.lon:.7f}",
                ]
            )


def _validate_source(source: str) -> str:
    normalized = source.strip().lower()
    if not normalized:
        raise ValueError("Source entries must not be empty.")
    if normalized == "ads":
        return "adsb"
    return normalized


def main() -> None:
    load_local_dotenv(load_dotenv, project_root=PROJECT_ROOT)

    parser = _build_parser()
    args = parser.parse_args()

    if args.bin_size_deg <= 0:
        raise ValueError("--bin-size-deg must be > 0")

    config = load_config()
    sqlite_path = args.sqlite_path if args.sqlite_path is not None else config.sqlite_path
    home_lat = float(args.home_lat) if args.home_lat is not None else config.radar_center_lat
    home_lon = float(args.home_lon) if args.home_lon is not None else config.radar_center_lon

    if not (-90 <= home_lat <= 90):
        raise ValueError("Home latitude must be in range -90..90")
    if not (-180 <= home_lon <= 180):
        raise ValueError("Home longitude must be in range -180..180")

    sources = [_validate_source(source) for source in args.sources]
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(sqlite_path))
    conn.row_factory = sqlite3.Row

    try:
        for source in sources:
            rows = _iter_positions(conn, source)
            points, rows_scanned, mean_distance_km = _compute_sweep(
                rows,
                home_lat=home_lat,
                home_lon=home_lon,
                bin_size_deg=args.bin_size_deg,
            )
            base_name = f"farthest_sweep_{source}"
            svg_path = output_dir / f"{base_name}.svg"
            csv_path = output_dir / f"{base_name}.csv"

            _write_svg(
                svg_path,
                title=f"Farthest Sweep - {source.upper()}",
                source=source,
                home_lat=home_lat,
                home_lon=home_lon,
                points=points,
                rows_scanned=rows_scanned,
                mean_distance_km=mean_distance_km,
            )
            _write_csv(csv_path, points)

            max_distance = max((point.distance_km for point in points), default=0.0)
            print(
                f"[{source}] used_gt_{MIN_DISTANCE_KM:.1f}km={rows_scanned} bins_with_data={len(points)} "
                f"max_distance_km={max_distance:.2f} mean_distance_km={mean_distance_km:.2f}"
            )
            print(f"[{source}] svg={svg_path}")
            print(f"[{source}] csv={csv_path}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
