from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


class PlayHistory:
    """Track how many times each story has been played. Uses atomic writes."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        if not self._path.exists():
            return {}
        with open(self._path, "r") as f:
            raw = json.load(f)
        return raw.get("stories", {})

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"stories": self._data}, indent=2)
        # Atomic write: write to temp file in same directory, then rename.
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        closed = False
        try:
            os.write(fd, payload.encode())
            os.close(fd)
            closed = True
            os.replace(tmp, self._path)
        except BaseException:
            if not closed:
                os.close(fd)
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def record_listen(self, filename: str) -> None:
        """Increment play count for a story and update last_played timestamp."""
        entry = self._data.setdefault(filename, {"play_count": 0, "last_played": None})
        entry["play_count"] = entry.get("play_count", 0) + 1
        entry["last_played"] = datetime.now(timezone.utc).isoformat()
        self._save()

    def get_play_count(self, filename: str) -> int:
        return self._data.get(filename, {}).get("play_count", 0)

    def get_play_counts(self) -> dict[str, int]:
        """Return {filename: play_count} for all known stories."""
        return {name: info.get("play_count", 0) for name, info in self._data.items()}

    def get_all(self) -> dict[str, dict]:
        """Return the full history data (read-only copy)."""
        return dict(self._data)
