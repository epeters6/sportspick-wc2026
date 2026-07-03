"""
Persistent state layout for Pavlov (Kalshi + Polymarket learning logs, caches, pending).

Set environment variable ``STATE_DIRECTORY`` to an absolute path on a **Railway volume**
(or any persistent disk) so ``logs/``, ``data/``, ``logs_poly/``, and ``data_poly/``
survive redeploys. If unset, these directories live under the project root (next to
``main.py``), which is ephemeral on Railway unless you use a volume.
"""

from __future__ import annotations

import os

from config import CONFIG

_APP_ROOT = os.path.dirname(os.path.abspath(__file__))


def app_root() -> str:
    """Repository / install directory (where ``main.py`` lives). Not affected by ``STATE_DIRECTORY``."""
    return _APP_ROOT


def state_root() -> str:
    """Root directory containing ``logs/``, ``data/``, ``logs_poly/``, ``data_poly/``."""
    raw = str(CONFIG.get("STATE_DIRECTORY") or "").strip()
    return os.path.abspath(raw) if raw else _APP_ROOT


def logs_dir() -> str:
    return os.path.join(state_root(), "logs")


def data_dir() -> str:
    return os.path.join(state_root(), "data")


def logs_poly_dir() -> str:
    return os.path.join(state_root(), "logs_poly")


def data_poly_dir() -> str:
    return os.path.join(state_root(), "data_poly")


def warn_if_learning_state_ephemeral(logger) -> None:
    """Log once if learning files are on the app directory (lost on typical PaaS redeploy).

    Skips the warning when ``STATE_DIRECTORY`` / volume mount points elsewhere.
    """
    on_railway = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
    if not on_railway:
        return
    if os.path.abspath(state_root()) != os.path.abspath(_APP_ROOT):
        return
    logger.warning(
        "Learning persistence: logs/signals.json, logs_poly/signals.json, ensemble bias, and "
        "station scores are stored under the app directory — they will reset on redeploy. "
        "Mount a Railway volume and set STATE_DIRECTORY to its path (or set "
        "RAILWAY_VOLUME_MOUNT_PATH so CONFIG picks it up)."
    )
