from __future__ import annotations

import logging
import threading
import time

from sleeper.input.base import Action, InputBackend

log = logging.getLogger(__name__)


class GpioInput(InputBackend):
    """Read physical buttons via gpiozero on Raspberry Pi GPIO pins."""

    def __init__(
        self,
        pin_mapping: dict[str, int] | None = None,
        long_press_seconds: float = 1.5,
    ) -> None:
        super().__init__()
        self._pin_mapping = pin_mapping or {
            "start_story": 17,
            "skip": 27,
            "volume_up": 22,
            "volume_down": 23,
            "pause_resume": 24,
            "stop": 25,
        }
        self._long_press_sec = long_press_seconds
        self._stop_event = threading.Event()

    def run(self) -> None:
        from gpiozero import Button

        buttons: dict[str, Button] = {}

        for action_name, pin in self._pin_mapping.items():
            btn = Button(pin, bounce_time=0.05, hold_time=self._long_press_sec)
            buttons[action_name] = btn

        # Wire up simple actions
        if "start_story" in buttons:
            buttons["start_story"].when_pressed = lambda: self.emit(Action.START_STORY)

        if "volume_up" in buttons:
            buttons["volume_up"].when_pressed = lambda: self.emit(Action.VOLUME_UP)

        if "volume_down" in buttons:
            buttons["volume_down"].when_pressed = lambda: self.emit(Action.VOLUME_DOWN)

        if "pause_resume" in buttons:
            buttons["pause_resume"].when_pressed = lambda: self.emit(Action.PAUSE_RESUME)

        # Skip: short press = next story, long press (held) = skip to noise
        if "skip" in buttons:
            skip_btn = buttons["skip"]
            skip_btn.when_released = lambda: (
                self.emit(Action.SKIP_TO_NEXT) if not skip_btn.is_held else None
            )
            skip_btn.when_held = lambda: self.emit(Action.SKIP_TO_NOISE)

        # Stop: only on long press
        if "stop" in buttons:
            buttons["stop"].when_held = lambda: self.emit(Action.STOP)

        log.info("GPIO input active on pins: %s", self._pin_mapping)

        # Block until stopped
        self._stop_event.wait()

        for btn in buttons.values():
            btn.close()

    def stop(self) -> None:
        self._stop_event.set()
