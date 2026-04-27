from __future__ import annotations

import logging
import threading
import time

from sleeper.input.base import Action, InputBackend

log = logging.getLogger(__name__)

# Button grid must match display/pygame_fb.py layout:
#   Row 0: Play/Pause | Vol+  | Next
#   Row 1: Noise      | Vol-  | Stop
#
# The Action emitted per cell (None = no action):
_GRID: list[list[Action | None]] = [
    [None,              Action.VOLUME_UP,   Action.SKIP_TO_NEXT],  # row 0
    [Action.SKIP_TO_NOISE, Action.VOLUME_DOWN, Action.STOP],       # row 1
]

# Play/Pause cell (row 0, col 0) needs special handling — emit START_STORY when
# idle or PAUSE_RESUME otherwise.  The touchscreen backend doesn't know state,
# so it always emits PAUSE_RESUME.  SessionManager promotes it to START_STORY
# when the session is idle (same logic as the existing PAUSE_RESUME handler).
# For a cleaner UX we emit START_STORY here too and let SessionManager ignore it
# when already playing.
_PLAY_PAUSE_CELL = (0, 0)
_PLAY_PAUSE_ACTION = Action.PAUSE_RESUME  # session handles idle case


class TouchscreenInput(InputBackend):
    """Read ADS7846 / SPI touchscreen events via evdev and map to Actions.

    Coordinate calibration:
        raw_x_min / raw_x_max  — raw ABS_X range from the device
        raw_y_min / raw_y_max  — raw ABS_Y range from the device
        screen_width / screen_height   — display resolution

    With dtoverlay=tft35a:rotate=90, raw ABS_X maps to screen Y and
    raw ABS_Y maps to screen X (swap_xy=True).  Adjust swap_xy, invert_x,
    invert_y in config if the calibration is mirrored for your panel.
    """

    HEADER_H = 60   # must match pygame_fb.py HEADER_H
    COLS = 3
    ROWS = 2

    def __init__(
        self,
        device_path: str = "/dev/input/event1",
        screen_width: int = 480,
        screen_height: int = 320,
        raw_x_min: int = 150,
        raw_x_max: int = 3950,
        raw_y_min: int = 150,
        raw_y_max: int = 3950,
        swap_xy: bool = True,
        invert_x: bool = False,
        invert_y: bool = False,
    ) -> None:
        super().__init__()
        self._device_path = device_path
        self._sw = screen_width
        self._sh = screen_height
        self._raw_x_min = raw_x_min
        self._raw_x_max = raw_x_max
        self._raw_y_min = raw_y_min
        self._raw_y_max = raw_y_max
        self._swap_xy = swap_xy
        self._invert_x = invert_x
        self._invert_y = invert_y
        self._running = False
        # Debounce: track last fired action per grid cell
        self._last_action_time: float = 0.0
        self._last_action: Action | None = None
        self._debounce_sec: float = 0.25  # min seconds between firing same action

    # ------------------------------------------------------------------

    def _to_screen(self, raw_x: int, raw_y: int) -> tuple[int, int]:
        """Map raw ADS7846 coordinates to screen pixels."""
        # Normalise to 0.0–1.0
        nx = (raw_x - self._raw_x_min) / max(1, self._raw_x_max - self._raw_x_min)
        ny = (raw_y - self._raw_y_min) / max(1, self._raw_y_max - self._raw_y_min)
        nx = max(0.0, min(1.0, nx))
        ny = max(0.0, min(1.0, ny))

        if self._swap_xy:
            nx, ny = ny, nx
        if self._invert_x:
            nx = 1.0 - nx
        if self._invert_y:
            ny = 1.0 - ny

        return int(nx * self._sw), int(ny * self._sh)

    def _coords_to_action(self, sx: int, sy: int) -> Action | None:
        """Return the Action for a screen tap, or None if tapped in header."""
        if sy < self.HEADER_H:
            return None  # header area — ignore

        btn_w = self._sw // self.COLS
        btn_h = (self._sh - self.HEADER_H) // self.ROWS
        col = min(sx // btn_w, self.COLS - 1)
        row = min((sy - self.HEADER_H) // btn_h, self.ROWS - 1)

        if (row, col) == _PLAY_PAUSE_CELL:
            return _PLAY_PAUSE_ACTION
        return _GRID[row][col]

    # ------------------------------------------------------------------

    def run(self) -> None:
        import evdev

        try:
            dev = evdev.InputDevice(self._device_path)
        except (FileNotFoundError, PermissionError) as e:
            log.error("Cannot open touch device %s: %s", self._device_path, e)
            return

        log.info("Touchscreen input active: %s (%s)", dev.name, dev.path)
        self._running = True

        raw_x: int | None = None
        raw_y: int | None = None
        pressed = False

        try:
            for event in dev.read_loop():
                if not self._running:
                    break

                if event.type == evdev.ecodes.EV_ABS:
                    if event.code == evdev.ecodes.ABS_X:
                        raw_x = event.value
                    elif event.code == evdev.ecodes.ABS_Y:
                        raw_y = event.value
                    elif event.code == evdev.ecodes.ABS_PRESSURE:
                        is_down = event.value > 0
                        if is_down and not pressed:
                            pressed = True
                        elif not is_down and pressed:
                            pressed = False
                            if raw_x is not None and raw_y is not None:
                                sx, sy = self._to_screen(raw_x, raw_y)
                                action = self._coords_to_action(sx, sy)
                                if action is not None:
                                    now = time.monotonic()
                                    if (
                                        action == self._last_action
                                        and now - self._last_action_time < self._debounce_sec
                                    ):
                                        log.debug(
                                            "Touch debounced: %s (%.0fms since last)",
                                            action.name,
                                            (now - self._last_action_time) * 1000,
                                        )
                                    else:
                                        self._last_action = action
                                        self._last_action_time = now
                                        log.debug(
                                            "Touch: raw=(%d,%d) screen=(%d,%d) -> %s",
                                            raw_x, raw_y, sx, sy, action.name,
                                        )
                                        self.emit(action)
        except OSError as e:
            log.error("Touch device disconnected: %s", e)
        finally:
            dev.close()

    def stop(self) -> None:
        self._running = False
