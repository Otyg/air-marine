from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from app.store import SQLiteStore


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "migrate_hydro_cache_to_sqlite.py"
SPEC = importlib.util.spec_from_file_location("migrate_hydro_cache_to_sqlite", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_migrate_hydro_cache_to_sqlite_imports_cache_files_into_store(tmp_path) -> None:
    cache_dir = tmp_path / "cache" / "hydro"
    cache_dir.mkdir(parents=True)
    cache_payload = {
        "source": "hydro",
        "status": "ok",
        "error": None,
        "request": {
            "bbox": [18.0, 59.0, 18.2, 59.2],
            "range_km": 10.0,
        },
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "collection": "LandWaterBoundary",
                    "inspireId": "coast-1",
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[18.0, 59.0], [18.1, 59.1]],
                },
            }
        ],
        "details": {},
    }
    (cache_dir / "one.json").write_text(json.dumps(cache_payload), encoding="utf-8")

    store = SQLiteStore(tmp_path / "hydro.sqlite3")
    store.initialize()

    result = MODULE.migrate_hydro_cache_to_sqlite(
        store=store,
        cache_dir=cache_dir,
    )

    assert result["files_scanned"] == 1
    assert result["files_migrated"] == 1
    assert result["files_skipped"] == 0
    assert result["features_stored"] == 1

    cached = store.load_hydro_contours_by_bbox(bbox=(18.0, 59.0, 18.2, 59.2))
    assert cached is not None
    assert cached[0]["properties"]["inspireId"] == "coast-1"


def test_migrate_hydro_cache_to_sqlite_skips_invalid_payloads(tmp_path) -> None:
    cache_dir = tmp_path / "cache" / "hydro"
    cache_dir.mkdir(parents=True)
    (cache_dir / "bad.json").write_text(
        json.dumps({"source": "hydro", "request": {}, "features": []}),
        encoding="utf-8",
    )

    store = SQLiteStore(tmp_path / "hydro.sqlite3")
    store.initialize()

    result = MODULE.migrate_hydro_cache_to_sqlite(
        store=store,
        cache_dir=cache_dir,
    )

    assert result["files_scanned"] == 1
    assert result["files_migrated"] == 0
    assert result["files_skipped"] == 1
    assert result["errors"]
