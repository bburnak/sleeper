from __future__ import annotations

from sleeper.display.base import DisplayBackend, DisplayState


class NoneDisplay(DisplayBackend):
    """No-op display backend for devices without a screen."""

    def update(self, state: DisplayState) -> None:
        pass

    def run(self) -> None:
        pass  # thread exits immediately; main thread blocks on shutdown_event

    def stop(self) -> None:
        pass
