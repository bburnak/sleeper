from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from typing import Callable


class Action(enum.Enum):
    START_STORY = "start_story"
    SKIP_TO_NEXT = "skip_to_next"
    SKIP_TO_NOISE = "skip_to_noise"
    VOLUME_UP = "volume_up"
    VOLUME_DOWN = "volume_down"
    PAUSE_RESUME = "pause_resume"
    STOP = "stop"


class InputBackend(ABC):
    """Abstract base for all input backends.

    Subclasses must implement ``run()`` which blocks and invokes
    the registered action callback whenever a user action is detected.
    """

    def __init__(self) -> None:
        self._callback: Callable[[Action], None] | None = None

    def set_callback(self, callback: Callable[[Action], None]) -> None:
        self._callback = callback

    def emit(self, action: Action) -> None:
        if self._callback:
            self._callback(action)

    @abstractmethod
    def run(self) -> None:
        """Blocking event loop. Call ``emit()`` to deliver actions."""
        ...

    @abstractmethod
    def stop(self) -> None:
        """Signal the event loop to exit."""
        ...
