from __future__ import annotations

import logging
import random
import time
from pathlib import Path

from sleeper.history import PlayHistory

log = logging.getLogger(__name__)

# Re-scan the stories directory at most once per this many seconds.
# Scanning a CIFS-mounted NAS with hundreds of files can take many seconds,
# which would otherwise add a multi-second delay between pressing Play and
# audio actually starting.
_LIST_CACHE_TTL_SEC = 60.0


class StorySelector:
    """Select stories from a directory, preferring least-played ones."""

    def __init__(self, stories_dir: Path, history: PlayHistory) -> None:
        self._dir = stories_dir
        self._history = history
        self._cache: list[str] | None = None
        self._cache_time: float = 0.0

    def list_stories(self) -> list[str]:
        """Return sorted list of .mp3 filenames in the stories directory.

        Result is cached for ``_LIST_CACHE_TTL_SEC`` to avoid re-scanning a
        slow (e.g. CIFS) filesystem on every story selection.
        """
        now = time.monotonic()
        if self._cache is not None and (now - self._cache_time) < _LIST_CACHE_TTL_SEC:
            return self._cache

        if not self._dir.is_dir():
            log.warning("Stories directory does not exist: %s", self._dir)
            self._cache = []
            self._cache_time = now
            return self._cache

        t0 = time.monotonic()
        result = sorted(p.name for p in self._dir.iterdir() if p.suffix.lower() == ".mp3")
        elapsed = time.monotonic() - t0
        if elapsed > 1.0:
            log.info("Scanned %d stories in %s in %.2fs", len(result), self._dir, elapsed)
        self._cache = result
        self._cache_time = now
        return result

    def pick(self, exclude: str | None = None) -> str | None:
        """Pick a story from the least-played pool, optionally excluding one.

        Returns the filename (not full path), or None if no stories available.
        """
        stories = self.list_stories()
        if not stories:
            log.error("No stories found in %s", self._dir)
            return None

        if exclude and exclude in stories:
            stories = [s for s in stories if s != exclude]
            if not stories:
                # Only one story exists; allow replaying it
                stories = self.list_stories()

        counts = self._history.get_play_counts()
        min_count = min((counts.get(s, 0) for s in stories), default=0)
        pool = [s for s in stories if counts.get(s, 0) == min_count]

        choice = random.choice(pool)
        log.info("Selected story '%s' (play_count=%d, pool_size=%d)", choice, min_count, len(pool))
        return choice

    def story_path(self, filename: str) -> Path:
        return self._dir / filename
