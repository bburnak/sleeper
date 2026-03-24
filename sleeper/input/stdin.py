from __future__ import annotations

import logging
import sys

from sleeper.input.base import Action, InputBackend

log = logging.getLogger(__name__)

# Default key -> action mapping (single characters)
_DEFAULT_MAP: dict[str, Action] = {
    "s": Action.START_STORY,
    "n": Action.SKIP_TO_NEXT,
    "N": Action.SKIP_TO_NOISE,
    "p": Action.PAUSE_RESUME,
    "+": Action.VOLUME_UP,
    "-": Action.VOLUME_DOWN,
    "q": Action.STOP,
}


class StdinInput(InputBackend):
    """Read single-key commands from stdin. Works over SSH."""

    def __init__(self, key_mapping: dict[str, Action] | None = None) -> None:
        super().__init__()
        self._mapping = key_mapping or _DEFAULT_MAP
        self._running = False

    def run(self) -> None:
        self._running = True
        log.info("Stdin input active. Keys: %s", {k: v.name for k, v in self._mapping.items()})
        print("Controls: [s]tart  [n]ext  [N]oise  [p]ause  [+/-] vol  [q]uit")

        while self._running:
            try:
                line = input().strip()
            except EOFError:
                break

            if not line:
                continue

            key = line[0]
            action = self._mapping.get(key)
            if action is None:
                print(f"Unknown key '{key}'. Valid: {list(self._mapping.keys())}")
                continue

            log.debug("Stdin action: %s (key='%s')", action.name, key)
            self.emit(action)

    def stop(self) -> None:
        self._running = False
