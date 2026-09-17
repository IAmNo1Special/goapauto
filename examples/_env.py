"""Minimal .env loader (stdlib only) for the runnable examples.

Finds a `.env` file in the repository root and loads `KEY=VALUE` pairs
into `os.environ`. Variables that are already set win over the file, so an
exported `TYPESAFE_API_KEY` still takes precedence.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: Path | None = None) -> Path | None:
    """Load `.env` into `os.environ`; return the file used, if any."""
    env_path = path or Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ[key] = value
    return env_path
