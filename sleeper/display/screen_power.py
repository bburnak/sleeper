from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

# Linux fb blanking levels (see linux/fb.h)
FB_BLANK_UNBLANK = 0
FB_BLANK_POWERDOWN = 4


class ScreenPower:
    """Tracks user activity and turns the LCD backlight on/off accordingly.

    Two strategies are tried in order:
      1. ``/sys/class/backlight/<name>/bl_power``  (0 = on, 4 = off)
      2. ``/sys/class/graphics/<fb>/blank``        (0 = unblank, 4 = power-off)

    If neither file is writable, the instance still tracks idle state so
    callers can use ``is_on`` for first-tap-swallow logic, but power changes
    become no-ops.

    All methods are thread-safe.
    """

    def __init__(
        self,
        idle_timeout: float = 60.0,
        backlight_path: str | None = None,
        fb_device: str = "/dev/fb1",
    ) -> None:
        self._timeout = float(idle_timeout)
        self._lock = threading.Lock()
        self._is_on = True
        self._last_activity = time.monotonic()

        self._bl_path: Path | None = None
        self._blank_path: Path | None = None

        # Strategy 1: explicit or auto-discovered backlight bl_power
        if backlight_path and backlight_path != "auto":
            p = Path(backlight_path)
            if p.exists():
                self._bl_path = p
            else:
                log.warning("screen_power: configured backlight path %s does not exist", p)
        else:
            bl_root = Path("/sys/class/backlight")
            if bl_root.is_dir():
                for entry in sorted(bl_root.iterdir()):
                    candidate = entry / "bl_power"
                    if candidate.exists():
                        self._bl_path = candidate
                        break

        # Strategy 2: framebuffer blank fallback (always discoverable from fb_device)
        try:
            fb_name = Path(fb_device).name  # e.g. "fb1"
            blank = Path("/sys/class/graphics") / fb_name / "blank"
            if blank.exists():
                self._blank_path = blank
        except Exception:
            pass

        if self._bl_path:
            log.info("screen_power: using backlight %s (timeout=%.0fs)", self._bl_path, self._timeout)
        elif self._blank_path:
            log.info("screen_power: using fb blank %s (timeout=%.0fs)", self._blank_path, self._timeout)
        else:
            log.warning("screen_power: no backlight/blank node found — power control disabled")

    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._timeout > 0

    @property
    def is_on(self) -> bool:
        with self._lock:
            return self._is_on

    @property
    def idle_timeout(self) -> float:
        return self._timeout

    def notify_activity(self) -> None:
        """Reset the idle timer. Does not change power state on its own."""
        with self._lock:
            self._last_activity = time.monotonic()

    def seconds_idle(self) -> float:
        with self._lock:
            return time.monotonic() - self._last_activity

    def should_sleep(self) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            if not self._is_on:
                return False
            return (time.monotonic() - self._last_activity) >= self._timeout

    # ------------------------------------------------------------------

    def wake(self) -> None:
        with self._lock:
            self._last_activity = time.monotonic()
            if self._is_on:
                return
            self._is_on = True
        self._set_power(on=True)
        log.info("screen: wake")

    def sleep(self) -> None:
        with self._lock:
            if not self._is_on:
                return
            self._is_on = False
        self._set_power(on=False)
        log.info("screen: sleep")

    def _set_power(self, on: bool) -> None:
        if self._bl_path is not None:
            value = b"0" if on else b"4"  # bl_power: 0=on, 4=off (FB_BLANK_POWERDOWN)
            try:
                with open(self._bl_path, "wb") as f:
                    f.write(value)
                return
            except OSError as exc:
                log.warning("screen_power: write %s failed: %s", self._bl_path, exc)

        if self._blank_path is not None:
            value = b"0" if on else b"4"
            try:
                with open(self._blank_path, "wb") as f:
                    f.write(value)
            except OSError as exc:
                log.warning("screen_power: write %s failed: %s", self._blank_path, exc)
