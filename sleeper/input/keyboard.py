from __future__ import annotations

import logging
import time

from sleeper.input.base import Action, InputBackend

log = logging.getLogger(__name__)

_ACTION_MAP = {
    "start_story": Action.START_STORY,
    "skip": None,  # resolved via short/long press
    "pause_resume": Action.PAUSE_RESUME,
    "stop": Action.STOP,
}


class KeyboardInput(InputBackend):
    """Read keyboard events via python-evdev. Useful for dev/testing."""

    def __init__(
        self,
        device_path: str = "auto",
        key_mapping: dict[int, str] | None = None,
        long_press_seconds: float = 1.5,
    ) -> None:
        super().__init__()
        self._device_path = device_path
        self._mapping = key_mapping or {}
        self._long_press_sec = long_press_seconds
        self._running = False

    def _find_device(self) -> str:
        import evdev

        if self._device_path != "auto":
            return self._device_path

        for path in evdev.list_devices():
            dev = evdev.InputDevice(path)
            caps = dev.capabilities(verbose=False)
            # A keyboard has EV_KEY (1) but typically no EV_ABS
            if 1 in caps and 3 not in caps:
                log.info("Auto-detected keyboard: %s (%s)", dev.name, dev.path)
                return dev.path
            dev.close()

        raise RuntimeError("No keyboard found. Connect one or set input_device in config.")

    def run(self) -> None:
        import evdev

        path = self._find_device()
        dev = evdev.InputDevice(path)
        log.info("Listening on keyboard: %s (%s)", dev.name, dev.path)

        self._running = True
        press_times: dict[int, float] = {}

        try:
            for event in dev.read_loop():
                if not self._running:
                    break

                if event.type != 1:  # EV_KEY only
                    continue

                code = event.code
                action_name = self._mapping.get(code)
                if action_name is None:
                    continue

                if event.value == 1:  # press
                    press_times[code] = time.monotonic()

                    # Volume keys fire on press (repeatable)
                    if action_name == "volume_up":
                        self.emit(Action.VOLUME_UP)
                    elif action_name == "volume_down":
                        self.emit(Action.VOLUME_DOWN)

                elif event.value == 0:  # release
                    pressed_at = press_times.pop(code, None)
                    if pressed_at is None:
                        continue
                    held = time.monotonic() - pressed_at
                    long = held >= self._long_press_sec

                    if action_name in ("volume_up", "volume_down"):
                        continue  # already handled on press

                    if action_name == "skip":
                        action = Action.SKIP_TO_NOISE if long else Action.SKIP_TO_NEXT
                    elif action_name == "stop" and long:
                        action = Action.STOP
                    elif action_name == "stop":
                        continue
                    else:
                        action = _ACTION_MAP.get(action_name)
                        if action is None:
                            continue

                    log.debug("Keyboard action: %s (held=%.1fs)", action.name, held)
                    self.emit(action)

                elif event.value == 2:  # repeat (auto-repeat)
                    if action_name == "volume_up":
                        self.emit(Action.VOLUME_UP)
                    elif action_name == "volume_down":
                        self.emit(Action.VOLUME_DOWN)

        except OSError as e:
            log.error("Keyboard disconnected: %s", e)
        finally:
            dev.close()

    def stop(self) -> None:
        self._running = False
