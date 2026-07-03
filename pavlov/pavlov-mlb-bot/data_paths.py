"""
Persistent layout for pavlov-mlb-bot.

Set ``STATE_DIRECTORY`` to a volume mount so ``logs/`` and ``data/`` survive redeploys.
On Railway, ``RAILWAY_VOLUME_MOUNT_PATH`` is applied when ``STATE_DIRECTORY`` is unset
(see ``config._load_config``). Use :func:`ensure_state_dirs` at startup so both folders exist.
"""

from __future__ import annotations

import os

from config import CONFIG

_APP_ROOT = os.path.dirname(os.path.abspath(__file__))


def app_root() -> str:
    return _APP_ROOT


def state_root() -> str:
    raw = str(CONFIG.get("STATE_DIRECTORY") or "").strip()
    return os.path.abspath(raw) if raw else _APP_ROOT


def logs_dir() -> str:
    return os.path.join(state_root(), "logs")


def data_dir() -> str:
    return os.path.join(state_root(), "data")


def ensure_state_dirs() -> None:
    """Create ``logs/`` and ``data/`` under :func:`state_root`.

    Call once at process startup so Railway volume mounts (via ``STATE_DIRECTORY`` /
    ``RAILWAY_VOLUME_MOUNT_PATH``) contain the expected layout before any reads/writes.
    """
    os.makedirs(logs_dir(), exist_ok=True)
    os.makedirs(data_dir(), exist_ok=True)


def warn_if_learning_state_ephemeral(logger) -> None:
    on_railway = bool(
        os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID")
    )
    if not on_railway:
        return
    rvm = str(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or "").strip()
    if rvm and os.path.abspath(state_root()) == os.path.abspath(_APP_ROOT):
        logger.warning(
            "MLB bot: RAILWAY_VOLUME_MOUNT_PATH is %r but STATE_DIRECTORY resolves to the app "
            "root (%r). All logs/ and data/ writes will be wiped on redeploy. "
            "Set STATE_DIRECTORY to the same path as your volume mount (often equal to "
            "RAILWAY_VOLUME_MOUNT_PATH), or remove STATE_DIRECTORY so it auto-defaults to the volume.",
            rvm,
            _APP_ROOT,
        )
        return
    if os.path.abspath(state_root()) != os.path.abspath(_APP_ROOT):
        return
    logger.warning(
        "MLB bot: logs/ and data/ are under the app path and will reset on redeploy. "
        "Mount a Railway volume and set STATE_DIRECTORY (or rely on RAILWAY_VOLUME_MOUNT_PATH)."
    )
