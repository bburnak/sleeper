from __future__ import annotations

import threading

from sleeper.input.base import InputBackend


class NoneInput(InputBackend):
    """No-op input backend for devices where all input comes from the display (touch)."""

    def __init__(self) -> None:
        super().__init__()
        self._stop_event = threading.Event()

    def run(self) -> None:
        self._stop_event.wait()

    def stop(self) -> None:
        self._stop_event.set()
