from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from loguru import logger

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"


def load_env_file(path: str | Path = DEFAULT_ENV_FILE, *, override: bool = False) -> bool:
    env_path = Path(path)
    if not env_path.exists() and env_path == DEFAULT_ENV_FILE:
        fallback = Path.cwd() / ".env"
        if fallback.exists():
            env_path = fallback
    if not env_path.exists():
        return False

    loaded = 0
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if value and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]

        if override or key not in os.environ:
            os.environ[key] = value
            loaded += 1

    logger.info("Loaded {} env values from {}", loaded, env_path)
    return True


def ensure_env_loaded(required_keys: Iterable[str], path: str | Path = DEFAULT_ENV_FILE) -> None:
    missing = [key for key in required_keys if not os.getenv(key)]
    if missing:
        load_env_file(path)


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default
