from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable


@dataclass
class DisplayState:
    """Snapshot of session state passed to the display backend on every change."""

    session_state: str  # matches State.value: idle | playing_story | crossfading | playing_noise | fading_out
    story_name: str | None = None
    volume: int = 60
    is_paused: bool = False


class DisplayBackend(ABC):
    """Abstract base for all display backends.

    The backend is given state updates via ``update()`` and can also emit
    input actions (e.g. touchscreen taps) via the registered callback.
    ``run()`` is called in a dedicated thread by main.py.
    """

    def __init__(self) -> None:
        self._callback: Callable | None = None

    def set_action_callback(self, callback: Callable) -> None:
        self._callback = callback

    def emit(self, action) -> None:
        if self._callback:
            self._callback(action)

    @abstractmethod
    def update(self, state: DisplayState) -> None:
        """Receive a new state snapshot (called from any thread)."""
        ...

    @abstractmethod
    def run(self) -> None:
        """Blocking loop. Return to signal shutdown is complete."""
        ...

    @abstractmethod
    def stop(self) -> None:
        """Signal the loop to exit."""
        ...
