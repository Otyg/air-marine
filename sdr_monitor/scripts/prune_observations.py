"""Delete rows from `observations` by age and/or low speed."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import load_config
from app.env_utils import load_local_dotenv

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency fallback
    load_dotenv = None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prune observations by age and/or speed threshold.",
    )
    parser.add_argument(
        "--sqlite-path",
        type=Path,
        default=None,
        help="SQLite path. Defaults to SDR_MONITOR_SQLITE_PATH from environment.",
    )
    parser.add_argument(
        "--older-than-days",
        type=float,
        default=None,
        help="Delete rows with observed_at older than this many days.",
    )
    parser.add_argument(
        "--speed-lt",
        type=float,
        default=None,
        help="Delete rows with speed strictly lower than this value (e.g. 1).",
    )
    parser.add_argument(
        "--mode",
        choices=("any", "all"),
        default="any",
        help="How to combine filters when both are set: any=OR, all=AND (default: any).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show how many rows would be deleted without deleting.",
    )
    return parser


def _build_where_clause(
    *,
    older_than_days: float | None,
    speed_lt: float | None,
    mode: str,
) -> tuple[str, list[object], str]:
    conditions: list[str] = []
    params: list[object] = []
    details: list[str] = []

    if older_than_days is not None:
        if older_than_days <= 0:
            raise ValueError("--older-than-days must be > 0")
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
        cutoff_iso = cutoff.isoformat(timespec="seconds")
        conditions.append("observed_at < ?")
        params.append(cutoff_iso)
        details.append(f"observed_at < {cutoff_iso}")

    if speed_lt is not None:
        conditions.append("speed IS NOT NULL AND speed < ?")
        params.append(float(speed_lt))
        details.append(f"speed < {float(speed_lt):g}")

    if not conditions:
        raise ValueError("At least one filter is required: --older-than-days and/or --speed-lt")

    joiner = " OR " if mode == "any" else " AND "
    where_clause = "(" + joiner.join(conditions) + ")"
    return where_clause, params, ", ".join(details)


def _count_rows(conn: sqlite3.Connection, where_clause: str, params: list[object]) -> int:
    query = f"SELECT COUNT(*) AS total FROM observations WHERE {where_clause}"
    row = conn.execute(query, tuple(params)).fetchone()
    return int(row[0] if row is not None else 0)


def _delete_rows(conn: sqlite3.Connection, where_clause: str, params: list[object]) -> int:
    query = f"DELETE FROM observations WHERE {where_clause}"
    cursor = conn.execute(query, tuple(params))
    return int(cursor.rowcount if cursor.rowcount is not None else 0)


def main() -> None:
    load_local_dotenv(load_dotenv, project_root=PROJECT_ROOT)

    parser = _build_parser()
    args = parser.parse_args()

    config = load_config()
    sqlite_path = args.sqlite_path if args.sqlite_path is not None else config.sqlite_path

    where_clause, params, detail = _build_where_clause(
        older_than_days=args.older_than_days,
        speed_lt=args.speed_lt,
        mode=args.mode,
    )

    conn = sqlite3.connect(str(sqlite_path))
    try:
        total = _count_rows(conn, where_clause, params)
        print(f"SQLite path: {sqlite_path}")
        print(f"Filters: {detail}")
        print(f"Mode: {args.mode}")
        print(f"Matching rows: {total}")

        if args.dry_run:
            print("Dry run: no rows deleted.")
            return

        deleted = _delete_rows(conn, where_clause, params)
        conn.commit()
        print(f"Deleted rows: {deleted}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
