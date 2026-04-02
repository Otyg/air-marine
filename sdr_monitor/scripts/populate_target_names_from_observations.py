"""Populate `target_names` from historical `observations` rows."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

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
        description="Backfill target_names (id -> name) from observations payload_json.",
    )
    parser.add_argument(
        "--sqlite-path",
        type=Path,
        default=None,
        help="SQLite database path. Defaults to SDR_MONITOR_SQLITE_PATH from environment.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional maximum number of observations to scan.",
    )
    return parser


def main() -> None:
    load_local_dotenv(load_dotenv, project_root=PROJECT_ROOT)

    parser = _build_parser()
    args = parser.parse_args()

    if args.sqlite_path is None:
        config = load_config()
        sqlite_path = config.sqlite_path
    else:
        sqlite_path = args.sqlite_path

    store = SQLiteStore(sqlite_path)
    store.initialize()
    result = store.populate_target_names_from_observations(limit=args.limit)

    print(f"SQLite path: {sqlite_path}")
    print(f"Observations scanned: {result['observations_scanned']}")
    print(f"Names upserted: {result['names_upserted']}")


if __name__ == "__main__":
    main()
