"""
Simple JSON file persistence.
Saves agents, campaigns and call history to disk so data survives restarts.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

STORAGE_FILE = os.environ.get("STORAGE_FILE", "dialer_data.json")


def _default(obj: Any) -> Any:
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    raise TypeError(f"Not serializable: {type(obj)}")


def save(agents: list, campaigns: dict, calls: list) -> None:
    """Write current state to disk."""
    try:
        data = {
            "agents":    [a.model_dump(mode="json") for a in agents],
            "campaigns": [c.model_dump(mode="json") for c in campaigns.values()],
            "calls":     [c.model_dump(mode="json") for c in calls
                          if c.status.value in ("completed", "dropped", "failed")],
        }
        tmp = STORAGE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, default=_default)
        os.replace(tmp, STORAGE_FILE)
    except Exception as exc:
        logger.error("Storage save failed: %s", exc)


def load() -> dict | None:
    """Load state from disk. Returns None if no file found."""
    if not os.path.exists(STORAGE_FILE):
        return None
    try:
        with open(STORAGE_FILE) as f:
            return json.load(f)
    except Exception as exc:
        logger.error("Storage load failed: %s", exc)
        return None
