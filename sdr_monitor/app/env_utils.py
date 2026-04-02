"""Helpers for loading local `.env` files predictably."""

from __future__ import annotations

from pathlib import Path
from typing import Callable


def load_local_dotenv(
    load_dotenv_func: Callable[..., bool] | None,
    *,
    project_root: Path,
    cwd: Path | None = None,
) -> tuple[Path, ...]:
    """Load `.env` from cwd first, then project root, without duplicates."""

    if load_dotenv_func is None:
        return ()

    current_dir = (cwd or Path.cwd()).resolve()
    candidate_paths = (
        current_dir / ".env",
        project_root.resolve() / ".env",
    )

    loaded_paths: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidate_paths:
        if candidate in seen:
            continue
        seen.add(candidate)
        if not candidate.is_file():
            continue
        load_dotenv_func(dotenv_path=candidate, override=False)
        loaded_paths.append(candidate)
    return tuple(loaded_paths)
