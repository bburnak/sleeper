from __future__ import annotations

import errno
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

    Strategies tried in order:
      0. GPIO pin via gpiozero (``backlight_gpio``, e.g. 18 on tft35a/Hosyond)
      1. ``/sys/class/backlight/<name>/bl_power``  (0 = on, 4 = off)
      2. ``/sys/class/graphics/<fb>/blank``        (0 = unblank, 4 = power-off)

    If none work, the instance still tracks idle state so callers can use
    ``is_on`` for first-tap-swallow logic, but power changes become no-ops.

    All methods are thread-safe.
    """

    def __init__(
        self,
        idle_timeout: float = 60.0,
        backlight_path: str | None = None,
        fb_device: str = "/dev/fb1",
        backlight_gpio: int | None = None,
        backlight_gpio_active_high: bool = True,
    ) -> None:
        self._timeout = float(idle_timeout)
        self._lock = threading.Lock()
        self._is_on = True
        self._last_activity = time.monotonic()

        self._gpio = None
        self._gpio_active_high = bool(backlight_gpio_active_high)
        self._bl_path: Path | None = None
        self._blank_path: Path | None = None

        # Strategy 0: GPIO backlight control
        if backlight_gpio is not None and backlight_gpio >= 0:
            try:
                from gpiozero import DigitalOutputDevice

                self._gpio = DigitalOutputDevice(
                    int(backlight_gpio),
                    active_high=self._gpio_active_high,
                    initial_value=True,
                )
            except Exception as exc:
                log.warning("screen_power: failed to init GPIO%s: %s", backlight_gpio, exc)
                self._gpio = None

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

        if self._gpio is not None:
            log.info(
                "screen_power: using GPIO%d (active_high=%s, timeout=%.0fs)",
                int(backlight_gpio), self._gpio_active_high, self._timeout,
            )
        elif self._bl_path:
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
        if self._gpio is not None:
            try:
                if on:
                    self._gpio.on()
                else:
                    self._gpio.off()
                return
            except Exception as exc:
                log.warning("screen_power: GPIO toggle failed: %s", exc)

        if self._bl_path is not None:
            value = b"0" if on else b"4"  # bl_power: 0=on, 4=off (FB_BLANK_POWERDOWN)
            try:
                with open(self._bl_path, "wb") as f:
                    f.write(value)
                return
            except OSError as exc:
                log.warning("screen_power: write %s failed: %s", self._bl_path, exc)

        if self._blank_path is not None:
            # Some fb drivers (e.g. fbtft) only support FB_BLANK_NORMAL (1),
            # not FB_BLANK_POWERDOWN (4); fall back on EINVAL.
            candidates = [b"0"] if on else [b"4", b"1"]
            for value in candidates:
                try:
                    with open(self._blank_path, "wb") as f:
                        f.write(value)
                    return
                except OSError as exc:
                    if exc.errno == errno.EINVAL and value != candidates[-1]:
                        continue
                    log.warning("screen_power: write %s failed: %s", self._blank_path, exc)
                    return
