"""Load KEY=value lines from a local .env file (gitignored). Existing
environment variables win, and values are never printed."""

from __future__ import annotations

import os
from pathlib import Path


def load_env(path: str | Path = ".env") -> list[str]:
    p = Path(path)
    if not p.exists():
        return []
    loaded = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded
