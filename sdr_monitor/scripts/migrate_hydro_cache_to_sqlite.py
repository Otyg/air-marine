"""Migrate persisted hydro contour cache files into SQLite-backed contour storage."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import load_config
from app.env_utils import load_local_dotenv
from app.store import SQLiteStore

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency fallback
    load_dotenv = None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Migrate persisted hydro contour cache JSON files into SQLite.",
    )
    parser.add_argument(
        "--sqlite-path",
        type=Path,
        default=None,
        help="SQLite database path. Defaults to SDR_MONITOR_SQLITE_PATH from environment.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Hydro cache directory. Defaults to SDR_MONITOR_MAP_CACHE_DIR/hydro from environment.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional maximum number of cache files to process.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and validate cache files without writing to SQLite.",
    )
    return parser


def main() -> None:
    load_local_dotenv(load_dotenv, project_root=PROJECT_ROOT)

    parser = _build_parser()
    args = parser.parse_args()

    config = load_config()
    sqlite_path = args.sqlite_path or config.sqlite_path
    cache_dir = args.cache_dir or (config.map_cache_dir / "hydro")

    if args.limit is not None and args.limit <= 0:
        raise SystemExit("--limit must be > 0")

    store = SQLiteStore(sqlite_path)
    store.initialize()
    result = migrate_hydro_cache_to_sqlite(
        store=store,
        cache_dir=cache_dir,
        limit=args.limit,
        dry_run=args.dry_run,
    )

    print(f"SQLite path: {sqlite_path}")
    print(f"Hydro cache dir: {cache_dir}")
    print(f"Dry run: {'yes' if args.dry_run else 'no'}")
    print(f"Files scanned: {result['files_scanned']}")
    print(f"Files migrated: {result['files_migrated']}")
    print(f"Files skipped: {result['files_skipped']}")
    print(f"Features stored: {result['features_stored']}")
    if result["errors"]:
        print("Errors:")
        for error in result["errors"]:
            print(f"- {error}")


def migrate_hydro_cache_to_sqlite(
    *,
    store: SQLiteStore,
    cache_dir: Path,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    if limit is not None and limit <= 0:
        raise ValueError("limit must be > 0")

    files_scanned = 0
    files_migrated = 0
    files_skipped = 0
    features_stored = 0
    errors: list[str] = []

    if not cache_dir.exists():
        raise FileNotFoundError(f"Hydro cache directory does not exist: {cache_dir}")
    if not cache_dir.is_dir():
        raise NotADirectoryError(f"Hydro cache path is not a directory: {cache_dir}")

    for index, cache_path in enumerate(sorted(cache_dir.glob("*.json"))):
        if limit is not None and index >= limit:
            break

        files_scanned += 1
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            bbox, features = _extract_cache_payload(cache_path=cache_path, payload=payload)
        except Exception as exc:
            files_skipped += 1
            errors.append(f"{cache_path.name}: {exc}")
            continue

        if not dry_run:
            try:
                store.save_hydro_contours_for_bbox(
                    bbox=bbox,
                    features=features,
                )
            except Exception as exc:
                files_skipped += 1
                errors.append(f"{cache_path.name}: failed to store payload: {exc}")
                continue

        files_migrated += 1
        features_stored += len(features)

    return {
        "files_scanned": files_scanned,
        "files_migrated": files_migrated,
        "files_skipped": files_skipped,
        "features_stored": features_stored,
        "errors": errors,
    }


def _extract_cache_payload(
    *,
    cache_path: Path,
    payload: object,
) -> tuple[tuple[float, float, float, float], list[dict[str, Any]]]:
    if not isinstance(payload, dict):
        raise ValueError("payload is not a JSON object")

    source = payload.get("source")
    if source != "hydro":
        raise ValueError(f"expected source='hydro', got {source!r}")

    request = payload.get("request")
    if not isinstance(request, dict):
        raise ValueError("payload.request is missing")

    raw_bbox = request.get("bbox")
    if not isinstance(raw_bbox, list) or len(raw_bbox) != 4:
        raise ValueError("payload.request.bbox must contain four coordinates")
    bbox = tuple(float(value) for value in raw_bbox)

    raw_features = payload.get("features")
    if not isinstance(raw_features, list):
        raise ValueError("payload.features is not a list")

    features: list[dict[str, Any]] = []
    for feature_index, feature in enumerate(raw_features):
        if not isinstance(feature, dict):
            raise ValueError(f"feature {feature_index} is not an object")
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict):
            raise ValueError(f"feature {feature_index} is missing geometry")
        properties = feature.get("properties")
        if properties is not None and not isinstance(properties, dict):
            raise ValueError(f"feature {feature_index} has invalid properties")
        features.append(feature)

    if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
        raise ValueError(f"invalid bbox in {cache_path.name}")

    return bbox, features


if __name__ == "__main__":
    main()
