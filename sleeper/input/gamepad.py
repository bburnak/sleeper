from __future__ import annotations

import logging
import time

from sleeper.input.base import Action, InputBackend

log = logging.getLogger(__name__)

# Action name strings from config -> Action enum
_ACTION_MAP = {
    "start_story": Action.START_STORY,
    "skip": None,  # resolved at runtime (short vs long press)
    "pause_resume": Action.PAUSE_RESUME,
    "stop": Action.STOP,
}


class GamepadInput(InputBackend):
    """Read gamepad events via python-evdev."""

    def __init__(
        self,
        device_path: str = "auto",
        button_mapping: dict[int, str] | None = None,
        volume_axis: int = 17,
        long_press_seconds: float = 1.5,
    ) -> None:
        super().__init__()
        self._device_path = device_path
        self._mapping = button_mapping or {}
        self._volume_axis = volume_axis
        self._long_press_sec = long_press_seconds
        self._running = False

    def _find_device(self) -> str:
        import evdev

        if self._device_path != "auto":
            return self._device_path

        for path in evdev.list_devices():
            dev = evdev.InputDevice(path)
            caps = dev.capabilities(verbose=False)
            # Look for a device with EV_KEY (1) and EV_ABS (3) — likely a gamepad
            if 1 in caps and 3 in caps:
                log.info("Auto-detected gamepad: %s (%s)", dev.name, dev.path)
                return dev.path
            dev.close()

        raise RuntimeError("No gamepad found. Connect a controller or set input_device in config.")

    def run(self) -> None:
        import evdev

        path = self._find_device()
        dev = evdev.InputDevice(path)
        log.info("Listening on gamepad: %s (%s)", dev.name, dev.path)

        self._running = True
        press_times: dict[int, float] = {}

        try:
            for event in dev.read_loop():
                if not self._running:
                    break

                # Button events (EV_KEY = 1)
                if event.type == 1:
                    code = event.code
                    action_name = self._mapping.get(code)
                    if action_name is None:
                        continue

                    if event.value == 1:  # press
                        press_times[code] = time.monotonic()

                    elif event.value == 0:  # release
                        pressed_at = press_times.pop(code, None)
                        if pressed_at is None:
                            continue
                        held = time.monotonic() - pressed_at
                        long = held >= self._long_press_sec

                        if action_name == "skip":
                            action = Action.SKIP_TO_NOISE if long else Action.SKIP_TO_NEXT
                        elif action_name == "stop" and long:
                            action = Action.STOP
                        elif action_name == "stop":
                            continue  # short press on stop does nothing
                        else:
                            action = _ACTION_MAP.get(action_name)
                            if action is None:
                                continue

                        log.debug("Gamepad action: %s (held=%.1fs)", action.name, held)
                        self.emit(action)

                # Axis events for volume (EV_ABS = 3)
                elif event.type == 3 and event.code == self._volume_axis:
                    if event.value == -1:
                        self.emit(Action.VOLUME_UP)
                    elif event.value == 1:
                        self.emit(Action.VOLUME_DOWN)

        except OSError as e:
            log.error("Gamepad disconnected: %s", e)
        finally:
            dev.close()

    def stop(self) -> None:
        self._running = False
